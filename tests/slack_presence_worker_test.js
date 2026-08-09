/**
 * Slack's native green dot, as an optional PresencePack side effect.
 *
 * No request here reaches Slack. The injected fetch and credential store pin the
 * security-sensitive order: rotate -> durable CAS -> export -> optional update.
 *
 *   node tests/slack_presence_worker_test.js
 */
import { readFileSync } from "node:fs";
import {
  SlackPresenceController,
  SlackPresenceError,
  credentialFromRotation,
  manifestWithAlwaysOnline,
} from "../relay/slack_presence.js";

const fails = [];
function check(cond, message) {
  console.log((cond ? "  PASS " : "  FAIL ") + message);
  if (!cond) fails.push(message);
}

function clone(value) { return JSON.parse(JSON.stringify(value)); }

function reply(payload, httpOK = true) {
  return { ok: httpOK, async json() { return clone(payload); } };
}

function credential(overrides = {}) {
  return {
    workspaceId: "T111",
    configToken: "access-secret-old",
    refreshToken: "refresh-secret-old",
    userId: "U111",
    issuedAt: 100,
    expiresAt: 10_000,
    version: 4,
    appMap: { Rowan: "A111" },             // store-owned metadata must survive rotation
    ...overrides,
  };
}

function fakeStore(initial, events = [], options = {}) {
  const rows = new Map(Object.entries(initial).map(([key, value]) => [key, clone(value)]));
  return {
    rows,
    async load(workspaceId) {
      events.push("load:" + workspaceId);
      if (options.loadThrows) throw new Error("refresh-secret-old");
      const row = rows.get(workspaceId);
      return row ? clone(row) : null;
    },
    async compareAndSwap(workspaceId, expectedVersion, replacement) {
      events.push("cas:" + workspaceId);
      if (options.casThrows) throw new Error("access-secret-new");
      if (options.casFails) return false;
      const current = rows.get(workspaceId);
      if (!current || current.version !== expectedVersion) return false;
      rows.set(workspaceId, clone(replacement));
      return true;
    },
  };
}

function methodOf(url) { return url.slice(url.lastIndexOf("/") + 1); }

async function rejected(operation) {
  try {
    await operation();
    return null;
  } catch (e) {
    return e;
  }
}

async function main() {
  // apps.manifest.update is a complete replacement. Keep every live/unknown field and
  // mutate neither the input nor anything except the one supported presence leaf.
  {
    const live = {
      _metadata: { major_version: 2, minor_version: 7 },
      display_information: { name: "Rowan", description: "hand-edited" },
      features: {
        bot_user: { display_name: "Rowan", always_online: false },
        app_home: { messages_tab_enabled: true, home_tab_enabled: false },
      },
      oauth_config: {
        scopes: { bot: ["app_mentions:read", "chat:write"] },
        pkce_enabled: false,
      },
      settings: {
        socket_mode_enabled: true,
        interactivity: { is_enabled: true },
        vendor_future_flag: { untouched: [1, 2, 3] },
      },
    };
    const before = JSON.stringify(live);
    const changed = manifestWithAlwaysOnline(live, true);
    const expected = JSON.parse(before);
    expected.features.bot_user.always_online = true;
    check(changed.changed && JSON.stringify(changed.manifest) === JSON.stringify(expected),
          "the full exported manifest is preserved while only always_online changes");
    check(JSON.stringify(live) === before && changed.manifest !== live,
          "manifest mutation works on a deep copy and leaves the live input alone");

    const omitted = clone(live);
    delete omitted.features.bot_user.always_online;
    check(manifestWithAlwaysOnline(omitted, false).changed === false,
          "an omitted always_online already means offline and causes no write");

    let malformed;
    try {
      manifestWithAlwaysOnline({ features: { bot_user: {
        display_name: "Rowan", always_online: "false", secret: "manifest-secret",
      } } }, true);
    } catch (e) { malformed = e; }
    check(malformed instanceof SlackPresenceError &&
          !String(malformed).includes("manifest-secret"),
          "a malformed bot manifest fails closed without reflecting its contents");
  }

  // A live credential and equal state: one read, zero writes, zero rotations.
  {
    const events = [];
    const store = fakeStore({ T111: credential() }, events);
    const calls = [];
    const fetch = async (url, init) => {
      const method = methodOf(url);
      calls.push({ method, init, body: JSON.parse(init.body) });
      events.push("fetch:" + method);
      if (method === "apps.manifest.export") return reply({
        ok: true,
        manifest: { display_information: { name: "Rowan" }, features: {
          bot_user: { display_name: "Rowan", always_online: true },
        } },
      });
      throw new Error("unexpected request");
    };
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const result = await ctl.setOnline("T111", "A111", true);
    check(!result.changed && calls.length === 1 &&
          calls[0].method === "apps.manifest.export",
          "equal presence exports once and does not update or rotate");
    check(calls[0].init.headers.authorization === "Bearer access-secret-old" &&
          calls[0].init.redirect === "error",
          "manifest reads use the workspace config token and forbid credential redirects");
    check(!JSON.stringify(result).includes("access-secret-old"),
          "the public transition result contains no credential");
  }

  // A real edge sends the complete exported document back, not a partial patch.
  {
    const store = fakeStore({ T111: credential() });
    const live = {
      _metadata: { major_version: 1 },
      display_information: { name: "Rowan", description: "keep me" },
      features: {
        bot_user: { display_name: "Rowan", always_online: false },
        app_home: { messages_tab_enabled: true },
      },
      oauth_config: { scopes: { bot: ["chat:write"] }, pkce_enabled: false },
      settings: { socket_mode_enabled: true, interactivity: { is_enabled: false } },
    };
    const calls = [];
    const fetch = async (url, init) => {
      const method = methodOf(url);
      const body = JSON.parse(init.body);
      calls.push({ method, body, init });
      if (method === "apps.manifest.export") return reply({ ok: true, manifest: live });
      if (method === "apps.manifest.update")
        return reply({ ok: true, permissions_updated: false });
      throw new Error("unexpected request");
    };
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const result = await ctl.setOnline("T111", "A111", true);
    const pushed = JSON.parse(calls[1].body.manifest);
    const expected = clone(live);
    expected.features.bot_user.always_online = true;
    check(result.changed && calls.map((c) => c.method).join(",") ===
          "apps.manifest.export,apps.manifest.update",
          "an offline-to-online edge performs one export and one update");
    check(JSON.stringify(pushed) === JSON.stringify(expected) && calls[1].body.app_id === "A111",
          "the update carries the same app id and the entire preserved manifest");
  }

  // Near expiry, the paired access+refresh token must be durably replaced before
  // the new access token is allowed to touch a manifest.
  {
    const events = [];
    const store = fakeStore({ T111: credential({ expiresAt: 1_300 }) }, events);
    const calls = [];
    const fetch = async (url, init) => {
      const method = methodOf(url);
      const body = JSON.parse(init.body);
      calls.push({ method, body, init });
      events.push("fetch:" + method);
      if (method === "tooling.tokens.rotate") return reply({
        ok: true,
        token: "access-secret-new",
        refresh_token: "refresh-secret-new",
        team_id: "T111",
        user_id: "U111",
        iat: 1_000,
        exp: 44_200,
      });
      if (method === "apps.manifest.export") return reply({ ok: true, manifest: {
        display_information: { name: "Rowan" },
        features: { bot_user: { display_name: "Rowan", always_online: false } },
      } });
      throw new Error("unexpected request");
    };
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const result = await ctl.setOnline("T111", "A111", false);
    const rotated = store.rows.get("T111");
    check(events.indexOf("fetch:tooling.tokens.rotate") < events.indexOf("cas:T111") &&
          events.indexOf("cas:T111") < events.indexOf("fetch:apps.manifest.export"),
          "rotation is persisted by CAS before the new access token is used");
    check(rotated.configToken === "access-secret-new" &&
          rotated.refreshToken === "refresh-secret-new" && rotated.version === 5 &&
          rotated.appMap.Rowan === "A111",
          "access token, refresh token, version, and store metadata are replaced atomically");
    check(calls[0].init.headers.authorization === undefined &&
          calls[0].body.refresh_token === "refresh-secret-old" &&
          calls[1].init.headers.authorization === "Bearer access-secret-new",
          "rotation sends no bearer header and subsequent calls use only the new token");
    check(!result.changed && !JSON.stringify(result).includes("secret"),
          "a rotated no-op still returns a credential-free result");
  }

  // A response for another team must never be persisted or used, even if every
  // token field in it otherwise looks valid.
  {
    const events = [];
    const store = fakeStore({ T111: credential({ expiresAt: 1_001 }) }, events);
    const fetch = async (url) => {
      events.push("fetch:" + methodOf(url));
      return reply({
        ok: true,
        token: "wrong-workspace-access-secret",
        refresh_token: "wrong-workspace-refresh-secret",
        team_id: "T222",
        user_id: "U222",
        iat: 1_000,
        exp: 44_200,
      });
    };
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const error = await rejected(() => ctl.setOnline("T111", "A111", true));
    check(error instanceof SlackPresenceError && !events.includes("cas:T111") &&
          events.filter((e) => e.startsWith("fetch:")).length === 1,
          "a rotated team mismatch fails before persistence or manifest access");
    check(!String(error).includes("wrong-workspace") &&
          !String(error).includes("refresh-secret-old"),
          "team-mismatch errors never echo old or returned credentials");
  }

  // Persistence failure leaves the outcome intentionally unusable. A fresh access
  // token without its fresh refresh token is not a valid committed credential.
  {
    for (const options of [{ casFails: true }, { casThrows: true }]) {
      const events = [];
      const store = fakeStore({ T111: credential({ expiresAt: 1_001 }) }, events, options);
      const fetch = async (url) => {
        const method = methodOf(url);
        events.push("fetch:" + method);
        if (method !== "tooling.tokens.rotate") throw new Error("new token was used");
        return reply({ ok: true, token: "access-secret-new",
          refresh_token: "refresh-secret-new", team_id: "T111", user_id: "U111",
          iat: 1_000, exp: 44_200 });
      };
      const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
      const error = await rejected(() => ctl.setOnline("T111", "A111", true));
      check(error instanceof SlackPresenceError &&
            events.every((e) => e !== "fetch:apps.manifest.export") &&
            !String(error).includes("access-secret-new"),
            "CAS failure fails closed before using or exposing the rotated token");
    }
  }

  // Store rows are workspace-bound, independently of which lookup key returned them.
  {
    const store = fakeStore({ T111: credential({ workspaceId: "T222" }) });
    let fetched = false;
    const ctl = new SlackPresenceController({
      fetch: async () => { fetched = true; throw new Error("must not fetch"); },
      store,
      clock: () => 1_000,
    });
    const error = await rejected(() => ctl.setOnline("T111", "A111", true));
    check(error instanceof SlackPresenceError && !fetched,
          "a credential stored under the wrong workspace key fails before the network");
  }

  // Slack/proxies can put arbitrary text in an error response. Treat it as tainted.
  {
    const store = fakeStore({ T111: credential() });
    const fetch = async () => reply({
      ok: false,
      error: "access-secret-old",
      token: "refresh-secret-old",
    });
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const error = await rejected(() => ctl.setOnline("T111", "A111", true));
    check(error instanceof SlackPresenceError &&
          !String(error).includes("access-secret-old") &&
          !String(error).includes("refresh-secret-old"),
          "Slack refusal bodies are treated as tainted and never copied into exceptions");
  }

  // Same-workspace concurrency must not spend one refresh token twice or perform
  // the same manifest transition twice.
  {
    const store = fakeStore({ T111: credential({ expiresAt: 1_001 }) });
    let rotateCalls = 0;
    let updateCalls = 0;
    let serverManifest = {
      display_information: { name: "Rowan" },
      features: { bot_user: { display_name: "Rowan", always_online: false } },
      settings: { socket_mode_enabled: true },
    };
    const fetch = async (url, init) => {
      const method = methodOf(url);
      const body = JSON.parse(init.body);
      if (method === "tooling.tokens.rotate") {
        rotateCalls += 1;
        await Promise.resolve();
        return reply({ ok: true, token: "access-secret-new",
          refresh_token: "refresh-secret-new", team_id: "T111", user_id: "U111",
          iat: 1_000, exp: 44_200 });
      }
      if (method === "apps.manifest.export")
        return reply({ ok: true, manifest: serverManifest });
      if (method === "apps.manifest.update") {
        updateCalls += 1;
        serverManifest = JSON.parse(body.manifest);
        return reply({ ok: true, permissions_updated: false });
      }
      throw new Error("unexpected request");
    };
    const ctl = new SlackPresenceController({ fetch, store, clock: () => 1_000 });
    const results = await Promise.all([
      ctl.setOnline("T111", "A111", true),
      ctl.setOnline("T111", "A111", true),
    ]);
    check(rotateCalls === 1 && updateCalls === 1,
          "concurrent equal edges rotate once and update the manifest once");
    check(results.filter((r) => r.changed).length === 1 &&
          serverManifest.features.bot_user.always_online === true,
          "the second serialized call observes the committed state and becomes a no-op");
  }

  // Pin direct helper redaction too: invalid response fields are never interpolated.
  {
    const old = credential();
    let error;
    try {
      credentialFromRotation("T111", old, {
        ok: true, team_id: "T222", token: "returned-access-secret",
        refresh_token: "returned-refresh-secret", user_id: "U222", iat: 1, exp: 2,
      }, 1);
    } catch (e) { error = e; }
    check(error instanceof SlackPresenceError && !String(error).includes("returned"),
          "the pure rotation validator is secret-safe on invalid responses");
  }

  const source = readFileSync(new URL("../relay/slack_presence.js", import.meta.url), "utf8");
  check(!source.includes("console.") && !source.includes("logger"),
        "the credential-bearing module has no logging path");

  if (fails.length) {
    console.error("\n" + fails.length + " failed");
    process.exit(1);
  }
  console.log("\nall Slack presence controller checks passed");
}

main().catch((error) => {
  console.error(error && error.stack || error);
  process.exit(1);
});
