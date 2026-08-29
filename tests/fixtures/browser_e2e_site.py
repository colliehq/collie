"""Deterministic local site for manual Collie Browser Bridge acceptance tests.

The site deliberately exercises real browser I/O without touching a third-party account:
form publication, image rendering, file upload, download, scrolling, hover, console capture,
and separate-page navigation. State lives only in this process.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


POSTS: list[dict[str, str]] = []
UPLOADS: list[dict[str, object]] = []
DOWNLOAD_NAME = "collie-e2e-download-20260829.txt"
DOWNLOAD_BYTES = b"Collie browser download E2E receipt\nmarker=download-ok\n"


def page(body: str, title: str = "Collie Browser E2E") -> bytes:
    return ("""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>%s</title><style>
body{font:16px/1.5 system-ui;margin:0;background:#f6f5f1;color:#161817}.wrap{max-width:820px;margin:auto;padding:32px}
section{background:white;border:1px solid #d7d8d3;border-radius:16px;padding:20px;margin:18px 0}label{display:block;margin:10px 0}
input,textarea,select,button,a.action{font:inherit;padding:9px 12px;border:1px solid #aaa;border-radius:9px}textarea{width:95%%;min-height:90px}
button,a.action{background:#183c31;color:white;text-decoration:none;cursor:pointer}.photo{width:100%%;max-width:600px;border-radius:14px}
.hover-menu .secret{display:none;margin-left:10px}.hover-menu:hover .secret{display:inline}.spacer{height:650px;background:linear-gradient(#fff,#dce8e2);border-radius:12px}
.ok{color:#17653b;font-weight:700}.post{border-left:4px solid #183c31;padding-left:12px;margin:10px 0}</style>
<main class="wrap">%s</main><script>console.log("collie-e2e-console-ready");</script>""" % (html.escape(title), body)).encode("utf-8")


def home() -> bytes:
    posts = "".join(
        '<article class="post"><h3>%s</h3><p>%s</p></article>' %
        (html.escape(item["title"]), html.escape(item["body"])) for item in POSTS
    ) or "<p id=post-empty>No posts yet.</p>"
    upload = ("<p class=ok id=upload-state>Uploaded %s (%s bytes)</p>" %
              (html.escape(str(UPLOADS[-1]["name"])), UPLOADS[-1]["bytes"])) if UPLOADS else \
             "<p id=upload-state>No upload yet.</p>"
    return page("""
<h1>Collie Browser Bridge · Real I/O Lab</h1><p id=ready class=ok>ready:e2e-v1</p>
<section><h2>Photo inspection</h2><img class=photo src="/photo.svg" alt="Blue Collie test card with three gold stars and a green circle"></section>
<section><h2>Publish a local test post</h2><form method=post action="/post">
<label>Post title <input name=title required placeholder="E2E title"></label>
<label>Post body <textarea name=body required placeholder="E2E body"></textarea></label>
<button type=submit id=publish>Publish test post</button></form><div id=posts>%s</div></section>
<section><h2>Upload</h2><form method=post action="/upload" enctype="multipart/form-data">
<label>Test attachment <input name=attachment type=file required></label><button type=submit id=upload>Upload test file</button></form>%s</section>
<section><h2>Download and controls</h2><a class=action href="/download" download>Download test receipt</a>
<a class=action href="/child-tab" target="_blank" rel="noopener">Open child tab</a>
<label>Test choice <select id=choice><option value=alpha>Alpha</option><option value=beta>Beta</option></select></label>
<div class=hover-menu><button type=button>Hover test menu</button><a class=secret href="/hover-target">Revealed target</a></div></section>
<section><h2>Long-page screenshot</h2><div class=spacer></div><p id=below-fold class=ok>below-fold-marker</p></section>
""" % (posts, upload))


class Handler(BaseHTTPRequestHandler):
    server_version = "CollieE2E/1"

    def log_message(self, fmt, *args):
        print("E2E", self.address_string(), fmt % args, flush=True)

    def send_bytes(self, data: bytes, content_type: str, status: int = 200, **headers):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in headers.items():
            self.send_header(key.replace("_", "-"), value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send_bytes(home(), "text/html; charset=utf-8")
        if path == "/photo.svg":
            svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="900" height="420" viewBox="0 0 900 420">
<rect width="900" height="420" rx="36" fill="#17345f"/><circle cx="160" cy="210" r="95" fill="#42b983"/>
<g fill="#ffd55a"><path d="M400 90l14 30 33 4-24 23 6 33-29-16-29 16 6-33-24-23 33-4z"/>
<path d="M570 90l14 30 33 4-24 23 6 33-29-16-29 16 6-33-24-23 33-4z"/>
<path d="M740 90l14 30 33 4-24 23 6 33-29-16-29 16 6-33-24-23 33-4z"/></g>
<text x="350" y="275" fill="white" font-family="system-ui" font-size="54" font-weight="700">COLLIE PHOTO TEST</text>
<text x="350" y="330" fill="#cfe5ff" font-family="system-ui" font-size="28">blue card · green circle · three gold stars</text></svg>'''.encode("utf-8")
            return self.send_bytes(svg, "image/svg+xml")
        if path == "/download":
            return self.send_bytes(DOWNLOAD_BYTES, "text/plain; charset=utf-8",
                                   Content_Disposition='attachment; filename="%s"' % DOWNLOAD_NAME)
        if path == "/hover-target":
            return self.send_bytes(page("<h1 class=ok>hover-target-reached</h1><a href='/'>Back</a>"), "text/html; charset=utf-8")
        if path == "/child-tab":
            return self.send_bytes(page("<h1 class=ok>child-tab-reached</h1><p id=child-marker>marker:child-ok</p>"),
                                   "text/html; charset=utf-8")
        if path == "/api/state":
            data = json.dumps({"posts": POSTS, "uploads": UPLOADS, "download": DOWNLOAD_NAME}).encode()
            return self.send_bytes(data, "application/json")
        return self.send_bytes(page("<h1>Not found</h1>"), "text/html; charset=utf-8", 404)

    def do_POST(self):
        path = urlparse(self.path).path
        length = min(int(self.headers.get("Content-Length", "0") or 0), 2_000_000)
        raw = self.rfile.read(length)
        if path == "/post":
            values = parse_qs(raw.decode("utf-8", "replace"))
            title = (values.get("title") or [""])[0][:200]
            body = (values.get("body") or [""])[0][:2000]
            if not title or not body:
                return self.send_bytes(page("<h1>Post rejected</h1>"), "text/html; charset=utf-8", 400)
            POSTS.append({"title": title, "body": body})
            return self.send_bytes(page("<h1 class=ok>published:local-test-post</h1><h2>%s</h2><p>%s</p><a href='/'>Back to lab</a>" %
                                        (html.escape(title), html.escape(body))), "text/html; charset=utf-8", 201)
        if path == "/upload":
            match = re.search(br'filename="([^"]*)"', raw)
            name = match.group(1).decode("utf-8", "replace") if match else "unknown"
            marker = b"collie-e2e-upload-marker" in raw
            UPLOADS.append({"name": name[:240], "bytes": len(raw), "marker": marker})
            return self.send_bytes(page("<h1 class=ok>uploaded:local-test-file</h1><p id=upload-name>%s</p><p id=upload-marker>marker:%s</p><a href='/'>Back to lab</a>" %
                                        (html.escape(name), "ok" if marker else "missing")), "text/html; charset=utf-8", 201)
        return self.send_bytes(page("<h1>Not found</h1>"), "text/html; charset=utf-8", 404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("COLLIE_E2E_URL=http://127.0.0.1:%d/" % server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
