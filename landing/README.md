# collie.run

What Cloudflare Pages serves at collie.run — the deployed page itself, not a draft of one.

The Pages project (`collie`) is a **direct upload** with no git connection, so for a while the only
copy of this file lived on Cloudflare and nothing in the repo matched what visitors saw. That is how
the macOS panel went on offering `pip install -e ".[local]"` under "a one-click `.dmg` app is on the
way" for eight releases after the dmg started shipping, and how the version label stayed at v0.20.0.
Whatever used to be in this directory was a different, older page that had not been deployed since
before v0.20.0 — it has been replaced by the real thing.

## Deploy

    set -a; . ~/.cloudflare-collie.env; set +a          # Pages:Edit; the scoped relay token lacks it
    npx wrangler pages deploy landing --project-name collie
    # then purge the zone cache, or collie.run keeps serving the old bytes from the edge

One file plus the logo. Analytics is Cloudflare Web Analytics — no cookies, no cross-site
identifiers; its beacon token is public by design, identifying the site rather than a visitor.
