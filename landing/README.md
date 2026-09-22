# collie.run

The canonical source for the static landing page and its Cloudflare Pages Function. Do not deploy
the archived `C:\workspace\collie-web` copy.

## Product and homepage versioning

The homepage belongs in this product repository because its promises are part of the product
contract. It should not be copied into one repository per release or duplicated in the private
Online service repository:

- every product release tag freezes `landing/`, including the exact homepage and privacy notice;
- `site-version.json` records the source product version, positioning/content version, privacy
  notice version, and the public availability stage of each product layer;
- the root `collie.run` deployment represents the current stable public release; feature branches
  use Cloudflare preview deployments until their availability labels are ready for stable;
- a private Online repository may own service code, operations pages, and authenticated account
  screens, but the public Collie/Online promise remains here so it cannot drift from the client;
- historical copy is recovered from the matching Git tag or release artifact instead of being
  manually maintained as another homepage.

`build.mjs` fails when the manifest, package version, homepage version, or privacy-notice version
disagree. Update those values deliberately whenever positioning or data behavior changes.

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
