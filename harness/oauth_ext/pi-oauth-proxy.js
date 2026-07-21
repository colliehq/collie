// pi extension: route Anthropic requests through collie's oauth-proxy so pi runs on the flat
// Claude subscription (Opus) instead of a per-token API key.
//
//   OAUTH_PROXY_PORT=8788 python -m harness.oauth_proxy &            # start the proxy
//   ANTHROPIC_API_KEY=dummy pi -p --provider claudesub --model claude-opus-4-8 \
//       --extension harness/oauth_ext/pi-oauth-proxy.js "task"
//
// The proxy injects the OAuth token + Claude-Code identity headers + CC system block and scrubs
// fingerprints, so pi's own prompt can't meter the request. Port via OAUTH_PROXY_PORT (default 8788).
export default function (pi) {
  const port = process.env.OAUTH_PROXY_PORT || "8788";
  const baseUrl = process.env.OAUTH_PROXY_URL || `http://127.0.0.1:${port}`;
  pi.registerProvider("claudesub", {
    name: "Claude (flat subscription via collie oauth-proxy)",
    baseUrl,
    apiKey: "ANTHROPIC_API_KEY", // proxy ignores the value; any non-empty key satisfies pi
    api: "anthropic-messages",
    models: [
      {
        id: "claude-opus-4-8",
        name: "Claude Opus 4.8 (subscription)",
        reasoning: true,
        input: ["text", "image"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }, // flat sub — no per-token cost
        contextWindow: 200000,
        maxTokens: 8192,
      },
      {
        id: "claude-sonnet-4-5",
        name: "Claude Sonnet 4.5 (subscription)",
        reasoning: true,
        input: ["text", "image"],
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        contextWindow: 200000,
        maxTokens: 8192,
      },
    ],
  });
}
