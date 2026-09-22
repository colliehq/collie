const DEFAULT_LIMIT = 20;
const RETENTION_MS = 48 * 60 * 60 * 1000;

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8", "cache-control": "no-store",
      "x-content-type-options": "nosniff", "referrer-policy": "no-referrer",
      "x-frame-options": "DENY",
    },
  });
}

export class ChatRateLimiter {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== "POST" || url.pathname !== "/take") return response({ error: "Not found." }, 404);
    const requested = Number.parseInt(request.headers.get("x-collie-limit") || "", 10);
    const limit = Number.isFinite(requested) && requested > 0 && requested <= 100 ? requested : DEFAULT_LIMIT;
    let count = 0;
    let allowed = false;
    await this.state.storage.transaction(async (tx) => {
      count = Number(await tx.get("count")) || 0;
      if (count < limit) {
        count += 1;
        await tx.put("count", count);
        allowed = true;
      }
    });
    if (allowed && (await this.state.storage.getAlarm()) === null) {
      await this.state.storage.setAlarm(Date.now() + RETENTION_MS);
    }
    return response({ allowed, remaining: Math.max(0, limit - count) }, allowed ? 200 : 429);
  }

  async alarm() {
    await this.state.storage.deleteAll();
  }
}

export default {
  fetch() {
    return response({ error: "Not found." }, 404);
  },
};
