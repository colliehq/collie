# collie.run

The canonical source for the static landing page and its Cloudflare Pages Function. Do not deploy
the archived `C:\workspace\collie-web` copy.

## Safe build boundary

Run `npm run build` in this directory. The build script recreates `dist/` from an explicit allowlist,
so drafts, internal identity notes, deployment configuration, and source files cannot be published by
an accidental directory upload. `wrangler.toml` points Pages at `dist`.

## Bindings

| Binding | Type | Purpose |
|---|---|---|
| `AI` | Workers AI | Powers the prompt-scoped `/api/chat` website demo |
| `RATE_LIMITER` | External Durable Object | Atomic 20-request per-address/day abuse limit |
| `RATE_LIMIT_SALT` | Encrypted Pages secret | HMAC key for pseudonymous daily limiter buckets |

The rate limiter intentionally lives in `rate-limiter-worker/`: Cloudflare Pages can bind to a
Durable Object hosted by a Worker, but cannot define the object class inside a Pages project.

## Release order

No command below is run automatically.

```powershell
cd landing\rate-limiter-worker
npx wrangler deploy

cd ..
npx wrangler pages secret put RATE_LIMIT_SALT --project-name collie
npm run build
npx wrangler pages deploy dist --project-name collie --branch main
```

Deploy the Durable Object worker first on its initial release, then deploy Pages. The website endpoint
fails closed with `503` if the atomic limiter, a `RATE_LIMIT_SALT` secret of at least 32 random bytes,
or Workers AI binding is absent. The raw network address is processed to select the daily bucket but
is never used as a Durable Object name; the object stores only a counter and expiry. The site has no
analytics beacon; the optional Ask Collie form explains its Cloudflare data flow before submission.
