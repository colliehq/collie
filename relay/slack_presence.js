/**
 * Optional Slack green-dot control for the relay presence authority.
 *
 * Slack Events API / Socket Mode bots cannot drive their presence through
 * users.setPresence. Their supported switch is features.bot_user.always_online in
 * the app manifest. Manifest updates replace the complete manifest, so this module
 * always exports the live document, changes one boolean in a deep copy, and sends
 * the whole document back. It is intentionally not wired into worker.js: a future
 * PresencePack integration can call
 *
 *   controller.setOnline(workspaceId, appId, online)
 *
 * only on the pack's offline -> online and online -> offline edges.
 *
 * App-configuration credentials are user/workspace scoped and rotate. The injected
 * store owns their encrypted persistence. Its contract is:
 *
 *   load(workspaceId) -> credential | null
 *   compareAndSwap(workspaceId, expectedVersion, replacement) -> boolean
 *
 * `compareAndSwap` must durably persist the complete replacement (including the new
 * refresh token) before resolving true. It must never log either credential. One
 * controller instance serializes rotation per workspace and transitions per app;
 * CAS additionally fails closed if two coordination boundaries were accidentally
 * configured for one workspace.
 */

const SLACK_API = "https://slack.com/api/";
const DEFAULT_REFRESH_BEFORE_SECONDS = 10 * 60;
const SLACK_ID = /^[A-Z][A-Z0-9]{1,63}$/;

export class SlackPresenceError extends Error {
  constructor(message) {
    super(message);
    this.name = "SlackPresenceError";
  }
}

function fail(message) {
  throw new SlackPresenceError(message);
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function id(value, kind) {
  if (typeof value !== "string" || !SLACK_ID.test(value))
    fail("invalid Slack " + kind);
  return value;
}

function jsonClone(value) {
  try {
    const encoded = JSON.stringify(value);
    if (encoded === undefined) fail("invalid Slack manifest");
    return JSON.parse(encoded);
  } catch (e) {
    if (e instanceof SlackPresenceError) throw e;
    fail("invalid Slack manifest");
  }
}

/** Return a full manifest copy with only bot_user.always_online changed. */
export function manifestWithAlwaysOnline(liveManifest, online) {
  if (!object(liveManifest) || typeof online !== "boolean")
    fail("invalid Slack manifest transition");

  const features = liveManifest.features;
  const bot = object(features) ? features.bot_user : null;
  if (!object(bot) || typeof bot.display_name !== "string" || !bot.display_name.trim())
    fail("Slack manifest has no valid bot user");

  const hasValue = Object.prototype.hasOwnProperty.call(bot, "always_online");
  if (hasValue && typeof bot.always_online !== "boolean")
    fail("Slack manifest has invalid always_online state");

  // Slack's omitted default is false. Do not turn an absent false into an update:
  // state equality, not JSON shape equality, decides whether a transition exists.
  const manifest = jsonClone(liveManifest);
  const current = hasValue ? bot.always_online : false;
  if (current === online) return { manifest, changed: false };

  manifest.features.bot_user.always_online = online;
  return { manifest, changed: true };
}

function finiteInteger(value) {
  return Number.isSafeInteger(value) && value >= 0;
}

function credentialFor(workspaceId, row) {
  if (!object(row)) fail("Slack configuration credential is unavailable");
  if (row.workspaceId !== workspaceId)
    fail("Slack configuration credential belongs to another workspace");
  if (typeof row.configToken !== "string" || typeof row.refreshToken !== "string" ||
      !row.refreshToken || !finiteInteger(row.issuedAt) || !finiteInteger(row.expiresAt) ||
      !finiteInteger(row.version))
    fail("Slack configuration credential is invalid");
  if (row.configToken && row.expiresAt < row.issuedAt)
    fail("Slack configuration credential is invalid");
  return row;
}

/**
 * Parse one tooling.tokens.rotate response without ever reflecting response data
 * into an exception. Returning a new refresh token is mandatory: persisting the
 * access token alone would strand the next twelve-hour rotation.
 */
export function credentialFromRotation(workspaceId, previous, response, nowSeconds) {
  id(workspaceId, "workspace id");
  const old = credentialFor(workspaceId, previous);
  if (!finiteInteger(nowSeconds) || !object(response) || response.ok !== true ||
      response.team_id !== workspaceId ||
      typeof response.token !== "string" || !response.token ||
      typeof response.refresh_token !== "string" || !response.refresh_token ||
      typeof response.user_id !== "string" || !response.user_id ||
      !finiteInteger(response.iat) || !finiteInteger(response.exp) ||
      response.exp <= response.iat || response.exp <= nowSeconds ||
      old.version >= Number.MAX_SAFE_INTEGER)
    fail("Slack configuration token rotation was invalid");

  // Preserve store-owned metadata (for example a dog -> app mapping) while replacing
  // every rotating field as one versioned unit.
  return {
    ...old,
    workspaceId,
    configToken: response.token,
    refreshToken: response.refresh_token,
    userId: response.user_id,
    issuedAt: response.iat,
    expiresAt: response.exp,
    version: old.version + 1,
  };
}

class WorkspaceSerial {
  constructor() { this.tails = new Map(); }

  async run(key, operation) {
    const prior = this.tails.get(key) || Promise.resolve();
    let release;
    const current = new Promise((resolve) => { release = resolve; });
    this.tails.set(key, current);
    await prior;
    try {
      return await operation();
    } finally {
      release();
      if (this.tails.get(key) === current) this.tails.delete(key);
    }
  }
}

/** Minimal Slack API client. It has no logging hook by design. */
export class SlackManifestAPI {
  constructor(fetchImpl) {
    if (typeof fetchImpl !== "function") fail("Slack fetch implementation is unavailable");
    this.fetchImpl = fetchImpl;
  }

  async _post(method, body, configToken = "") {
    const headers = { "content-type": "application/json; charset=utf-8" };
    if (configToken) headers.authorization = "Bearer " + configToken;

    let response;
    try {
      response = await this.fetchImpl(SLACK_API + method, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
        // Never follow a redirect carrying an Authorization header or refresh body.
        redirect: "error",
      });
    } catch (e) {
      fail("Slack API request failed");
    }

    let payload;
    try {
      payload = await response.json();
    } catch (e) {
      fail("Slack API returned an invalid response");
    }
    // Deliberately do not include Slack's error/body in the exception. A proxy or
    // test endpoint can echo request credentials there, and callers may log errors.
    if (!response.ok || !object(payload) || payload.ok !== true)
      fail("Slack API request was refused");
    return payload;
  }

  async rotateConfigToken(refreshToken) {
    if (typeof refreshToken !== "string" || !refreshToken)
      fail("Slack refresh credential is unavailable");
    return this._post("tooling.tokens.rotate", { refresh_token: refreshToken });
  }

  async exportManifest(configToken, appId) {
    const response = await this._post("apps.manifest.export", { app_id: appId }, configToken);
    if (!object(response.manifest)) fail("Slack exported an invalid manifest");
    return response.manifest;
  }

  async updateManifest(configToken, appId, manifest) {
    let encoded;
    try { encoded = JSON.stringify(manifest); } catch (e) { fail("invalid Slack manifest"); }
    const response = await this._post(
      "apps.manifest.update", { app_id: appId, manifest: encoded }, configToken);
    return { permissionsUpdated: response.permissions_updated === true };
  }
}

/** Workspace-scoped twelve-hour config-token rotation. */
export class RotatingConfigTokens {
  constructor({ store, api, clock = () => Math.floor(Date.now() / 1000),
                refreshBeforeSeconds = DEFAULT_REFRESH_BEFORE_SECONDS } = {}) {
    if (!store || typeof store.load !== "function" ||
        typeof store.compareAndSwap !== "function")
      fail("Slack credential store is unavailable");
    if (!api || typeof api.rotateConfigToken !== "function" || typeof clock !== "function" ||
        !finiteInteger(refreshBeforeSeconds))
      fail("Slack token rotation is misconfigured");
    this.store = store;
    this.api = api;
    this.clock = clock;
    this.refreshBeforeSeconds = refreshBeforeSeconds;
    this.serial = new WorkspaceSerial();
  }

  async get(workspaceId) {
    id(workspaceId, "workspace id");
    return this.serial.run(workspaceId, async () => {
      let current;
      try { current = await this.store.load(workspaceId); }
      catch (e) { fail("Slack configuration credential storage failed"); }
      current = credentialFor(workspaceId, current);

      const now = this.clock();
      if (!finiteInteger(now)) fail("Slack token clock is invalid");
      if (current.configToken && current.expiresAt - now > this.refreshBeforeSeconds)
        return current.configToken;

      const response = await this.api.rotateConfigToken(current.refreshToken);
      const replacement = credentialFromRotation(workspaceId, current, response, now);
      if (replacement.expiresAt - now <= this.refreshBeforeSeconds)
        fail("Slack configuration token rotation was invalid");
      // Do not use the new access token until its paired refresh token is durable.
      let saved;
      try {
        saved = await this.store.compareAndSwap(workspaceId, current.version, replacement);
      } catch (e) {
        fail("Slack configuration credential storage failed");
      }
      if (saved !== true)
        fail("Slack configuration credential changed during rotation");
      return replacement.configToken;
    });
  }
}

/**
 * Public integration boundary for PresencePack. The return value contains no
 * credential or manifest, so it is safe for ordinary diagnostics/metrics.
 */
export class SlackPresenceController {
  constructor({ fetch: fetchImpl, store, clock, refreshBeforeSeconds } = {}) {
    this.api = new SlackManifestAPI(fetchImpl);
    this.tokens = new RotatingConfigTokens({
      store, api: this.api, clock, refreshBeforeSeconds,
    });
    this.serial = new WorkspaceSerial();
  }

  async setOnline(workspaceId, appId, online) {
    id(workspaceId, "workspace id");
    id(appId, "app id");
    if (!appId.startsWith("A")) fail("invalid Slack app id");
    if (typeof online !== "boolean") fail("invalid Slack online state");

    // Serialize the entire read/modify/write for an app. Two opposite edges cannot
    // export the same base manifest and then overwrite each other out of order.
    return this.serial.run(workspaceId + "\0" + appId, async () => {
      const configToken = await this.tokens.get(workspaceId);
      const live = await this.api.exportManifest(configToken, appId);
      const transition = manifestWithAlwaysOnline(live, online);
      if (!transition.changed)
        return { workspaceId, appId, online, changed: false, permissionsUpdated: false };

      const update = await this.api.updateManifest(
        configToken, appId, transition.manifest);
      return {
        workspaceId,
        appId,
        online,
        changed: true,
        permissionsUpdated: update.permissionsUpdated,
      };
    });
  }
}
