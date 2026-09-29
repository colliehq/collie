"""Write harness/google_oauth_client.json from the GOOGLE_OAUTH_CLIENT_JSON secret at release time.

Collie's Google connection (harness/google_connect.py) signs in with Collie's own Google OAuth app,
a *Desktop app* client. Google does not treat a Desktop client's secret as confidential (it ships
inside every installed copy), but it still never belongs in Git: GitHub push protection refuses a
Google client secret, and a committed copy would outlive any rotation. So the release jobs run this
before they build, and the wheel, the Windows installer and the macOS app pick the file up as
package data.

With no secret, a branch or pull-request build (a fork, or a dry run) prints one line and exits 0;
that build has no Google client and `collie google status` says Google is not set up. A tagged
release (refs/tags/…) without the secret fails instead, and so does a secret that is set but is not
a Desktop client, rather than shipping a connection that cannot work. The value is never printed.

    python installer/write_google_oauth_client.py [target-path]
"""
import json
import os
import re
import sys

_CLIENT_ID = re.compile(r"[A-Za-z0-9._-]{1,200}\.apps\.googleusercontent\.com\Z")
_KEEP = ("client_id", "client_secret", "project_id", "auth_uri", "token_uri",
         "auth_provider_x509_cert_url", "redirect_uris")


def main(argv):
    target = argv[1] if len(argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "harness",
        "google_oauth_client.json")
    raw = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "")
    tagged = (os.environ.get("GITHUB_REF_TYPE") == "tag"
              or os.environ.get("GITHUB_REF", "").startswith("refs/tags/"))
    if not raw.strip():
        if tagged:
            # A version tag is a release people install. Skipping here would ship a Google
            # connection that can only ever say "not set up".
            print("::error::GOOGLE_OAUTH_CLIENT_JSON is not set; a tagged release must carry "
                  "Collie's Google OAuth client")
            return 1
        print("GOOGLE_OAUTH_CLIENT_JSON is not set: this build has no Google OAuth client")
        return 0
    try:
        doc = json.loads(raw)
    except ValueError:
        print("::error::GOOGLE_OAUTH_CLIENT_JSON is not JSON")
        return 1
    inner = doc.get("installed") if isinstance(doc, dict) else None
    client_id = inner.get("client_id") if isinstance(inner, dict) else None
    secret = inner.get("client_secret") if isinstance(inner, dict) else None
    if not (isinstance(client_id, str) and _CLIENT_ID.match(client_id)
            and isinstance(secret, str) and secret and not any(c.isspace() for c in secret)):
        print('::error::GOOGLE_OAUTH_CLIENT_JSON must be a Desktop-app client: '
              '{"installed": {"client_id": "...apps.googleusercontent.com", "client_secret": ...}}')
        return 1
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"installed": {k: inner[k] for k in _KEEP if k in inner}}, handle, indent=1)
        handle.write("\n")
    print("wrote %s from GOOGLE_OAUTH_CLIENT_JSON" % os.path.basename(target))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
