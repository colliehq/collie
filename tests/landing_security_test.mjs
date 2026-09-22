/** Landing chat privacy boundary: keyed limiter buckets and fail-closed bindings. */
import { webcrypto } from "node:crypto";
import { onRequestPost } from "../landing/functions/api/chat.js";

if (!globalThis.crypto) globalThis.crypto = webcrypto;

const failures = [];
function check(value, message) {
  console.log((value ? "  PASS " : "  FAIL ") + message);
  if (!value) failures.push(message);
}

function chatRequest(ip = "203.0.113.42") {
  const headers = { "content-type": "application/json" };
  if (ip !== null) headers["CF-Connecting-IP"] = ip;
  return new Request("https://collie.run/api/chat", {
    method: "POST", headers, body: JSON.stringify({ message: "What is Collie?" }),
  });
}

function environment({ salt = "s".repeat(32), limiterStatus = 200 } = {}) {
  const seen = { buckets: [], ai: 0, limiter: 0 };
  return {
    seen,
    env: {
      RATE_LIMIT_SALT: salt,
      RATE_LIMITER: {
        idFromName(value) { seen.buckets.push(value); return value; },
        get() {
          return { async fetch(_url, request) {
            seen.limiter += 1;
            seen.limit = new Headers(request.headers).get("x-collie-limit");
            return new Response("{}", { status: limiterStatus });
          } };
        },
      },
      AI: { async run() { seen.ai += 1; return { response: "Collie coordinates durable work." }; } },
    },
  };
}

async function main() {
  {
    const fixture = environment({ salt: "short" });
    const response = await onRequestPost({ request: chatRequest(), env: fixture.env });
    check(response.status === 503 && fixture.seen.buckets.length === 0 && fixture.seen.ai === 0,
          "a missing or weak limiter secret fails closed before model use");
  }
  {
    const fixture = environment();
    const response = await onRequestPost({ request: chatRequest(null), env: fixture.env });
    check(response.status === 503 && fixture.seen.buckets.length === 0 && fixture.seen.ai === 0,
          "a request without Cloudflare's client-address boundary fails closed");
  }
  {
    const fixture = environment();
    const ip = "203.0.113.42";
    const first = await onRequestPost({ request: chatRequest(ip), env: fixture.env });
    const second = await onRequestPost({ request: chatRequest(ip), env: fixture.env });
    const [one, two] = fixture.seen.buckets;
    check(first.status === 200 && second.status === 200 && fixture.seen.limit === "20",
          "the atomic limiter is charged before successful model requests");
    check(/^[0-9a-f]{64}$/.test(one) && one === two && !one.includes(ip) &&
          !one.includes(new Date().toISOString().slice(0, 10)),
          "the same address gets a stable daily HMAC bucket with no raw address in its object name");
  }
  {
    const fixture = environment({ limiterStatus: 429 });
    const response = await onRequestPost({ request: chatRequest(), env: fixture.env });
    check(response.status === 429 && fixture.seen.limiter === 1 && fixture.seen.ai === 0,
          "rate-limit exhaustion prevents the Workers AI call");
  }

  console.log(failures.length ? `\n  ${failures.length} FAILED` : "\n  landing security: all green");
  process.exit(failures.length ? 1 : 0);
}

main().catch((error) => { console.error(error); process.exit(1); });
