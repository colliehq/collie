"""collie web GUI — a local, stdlib-only Server-Sent-Events front end for the harness.

    python -m harness.webapp            # serve on :8787 and open a browser
    COLLIE_PROVIDER=anthropic python -m harness.webapp --port 9000

Why stdlib-only: collie ships no web framework. `http.server.ThreadingHTTPServer` gives us
concurrent request handling (the long-lived SSE stream in one thread never blocks the sidebar's
`/api/sessions` poll in another), and `webbrowser.open` pops the UI — zero pip installs.

The run itself streams LIVE: we set `h.emit` to a callback that writes each harness event
(tool / edit / repro / receipt) straight onto the open SSE socket as it happens, so the browser
paints the verification gate flipping fail -> pass in real time instead of waiting for one blob.
Because `Harness.run` is synchronous and calls `emit` on THIS handler thread, no queue is needed —
the callback writes to `self.wfile` directly. `Harness._emit` already swallows callback
exceptions, so a client that closes mid-run can never crash the run.

Routes:
    GET /                       -> webui/index.html (the whole UI, one self-contained file)
    GET /api/sessions           -> sessions.recent()           (sidebar thread list)
    GET /api/session/<id>       -> sessions.load(id)           (click a thread to reload it)
    GET /api/stream?session=&q= -> text/event-stream           (one run, streamed live)
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(HERE, "webui", "index.html")
# logo ships INSIDE the package (webui/logo.svg) so a pip-installed wheel serves it; the repo's
# assets/ copy is the fallback for an editable checkout that predates the move.
LOGO_SVG = os.path.join(HERE, "webui", "logo.svg")
if not os.path.exists(LOGO_SVG):
    LOGO_SVG = os.path.join(os.path.dirname(HERE), "assets", "collie-logo.svg")

# CSRF guard. `collie web` binds 127.0.0.1, but that does NOT stop a *drive-by* request: any web
# page the user has open can fire `new Image().src="http://127.0.0.1:8787/api/stream?q=rm -rf …"`,
# and the agent (which runs bash + edits files) would execute it server-side — the attacker never
# needs to read the response. Defence: a per-process secret minted at import, injected into the
# served HTML (same-origin, so cross-site JS can't read it), and required on every state-changing /
# code-executing route. Same-origin requests from our own page carry it; cross-site ones can't.
TOKEN = os.urandom(16).hex()


def _provider() -> str:
    """Zero-config stays mock ($0 local dev): settings.apply() runs per query, so a Provider
    saved in the Settings panel (default choice: anthropic API) lands in the env and wins here;
    only a fresh install with nothing saved falls through to mock."""
    return os.environ.get("COLLIE_PROVIDER", "mock")


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.0 (the default) closes the connection when the handler returns, which is exactly
    # what we want for SSE: after the `done` event we return and the socket closes cleanly. The
    # client listens for `done` and calls es.close(), so there is no reconnect storm.
    server_version = "collie-web/0.1"

    # ------------------------------------------------------------------ live event bus
    # A run streams its events to the client that started it (via _serve_stream). We ALSO fan the
    # structural events (start/tool/edit/repro/done — never the token firehose) out to a process-wide
    # bus so OTHER open pages — the Map and the main page's mini-map — render Collie's work in real
    # time. Subscribers are plain queues; a run publishes, each /api/live connection drains its queue.
    _live_lock = threading.Lock()
    _live_subs: list = []

    # attached-image store: the composer POSTs an image to /api/upload, gets an id back, then the
    # next /api/stream?imgs=<id> references it. Kept in memory (a run consumes it right away), bounded
    # so a burst of screenshots can't grow unbounded.
    _img_lock = threading.Lock()
    _imgs: dict = {}
    _img_order: list = []

    # mid-run steering: while a run streams, the user can send more text (Claude-Code style). It's
    # queued here per session; the run's loop drains it at the next turn boundary (loop._drain_steering)
    # and injects it as a user message. Bounded so a jammed run can't grow the queue without limit.
    _steer_lock = threading.Lock()
    _steer_runs: dict = {}          # sid -> queue.Queue[str], present only while a run is in flight

    @classmethod
    def _steer_open(cls, sid):
        q: queue.Queue = queue.Queue(maxsize=64)
        with cls._steer_lock:
            cls._steer_runs[sid] = q
        return q

    @classmethod
    def _steer_close(cls, sid):
        with cls._steer_lock:
            cls._steer_runs.pop(sid, None)

    @classmethod
    def _steer_push(cls, sid, text):
        """Queue a steer for an in-flight run. False if the session has no active run (already done)."""
        with cls._steer_lock:
            q = cls._steer_runs.get(sid)
        if q is None:
            return False
        try:
            q.put_nowait(text)
            return True
        except queue.Full:
            return False

    @classmethod
    def _img_put(cls, media_type, data):
        iid = os.urandom(8).hex()
        with cls._img_lock:
            cls._imgs[iid] = (media_type, data)
            cls._img_order.append(iid)
            while len(cls._img_order) > 24:
                cls._imgs.pop(cls._img_order.pop(0), None)
        return iid

    @classmethod
    def _img_get(cls, iid):
        with cls._img_lock:
            return cls._imgs.get(iid)

    @classmethod
    def _live_pub(cls, kind, data):
        with cls._live_lock:
            subs = list(cls._live_subs)
        for q in subs:
            try:
                q.put_nowait((kind, data))
            except queue.Full:
                pass          # a stalled listener drops frames rather than blocking the run

    def _serve_live(self):
        """GET /api/live -> an SSE feed of every run's structural events, for live map rendering."""
        q: queue.Queue = queue.Queue(maxsize=256)
        with Handler._live_lock:
            Handler._live_subs.append(q)
        self._sse_open()
        try:
            self._sse("live_hello", {"cwd": os.getcwd()})
            while True:
                try:
                    kind, data = q.get(timeout=15)
                    self._sse(kind, data)
                except queue.Empty:
                    self._sse("ping", {})     # keep-alive so proxies/browsers hold the connection
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            with Handler._live_lock:
                try:
                    Handler._live_subs.remove(q)
                except ValueError:
                    pass

    # ------------------------------------------------------------------ helpers
    def _send_html(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj, code: int = 200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self._send_html(body, code, "application/json; charset=utf-8")

    def _read_json(self, maxlen: int = 8192):
        """Read + parse a JSON POST body, or None on any problem (missing/oversize/parse)."""
        try:
            n = int(self.headers.get("content-length") or 0)
        except ValueError:
            return None
        if n <= 0 or n > maxlen:
            return None
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            return None
        return body if isinstance(body, dict) else None

    def _sse_open(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        # Connection: close (NOT keep-alive) — this is a one-shot stream. Without a Content-Length or
        # chunked terminator, a keep-alive SSE connection never signals end-of-response, so after the
        # `done` frame it LINGERS open until something (a proxy, the browser) drops it — which the
        # client sees as EventSource.onerror -> "stream interrupted". Closing after the handler returns
        # gives a clean EOF right after `done`. close_connection makes BaseHTTPRequestHandler honor it.
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # defeat any reverse-proxy buffering
        self.end_headers()
        self.close_connection = True

    def _sse(self, event: str, data) -> None:
        """Write one SSE frame and flush it onto the wire immediately. When a heartbeat thread is
        active (_serve_stream), a per-request lock serializes writes so a ping can't interleave
        mid-frame with the run's token/tool events and corrupt the stream."""
        payload = "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=False, default=str))
        buf = payload.encode("utf-8")
        lock = getattr(self, "_wlock", None)
        if lock is not None:
            with lock:
                self.wfile.write(buf)
                self.wfile.flush()
        else:
            self.wfile.write(buf)
            self.wfile.flush()

    def log_message(self, fmt, *args):   # keep the terminal quiet; SSE is the real feedback
        pass

    # ------------------------------------------------------------------ routing
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        try:
            if path in ("/", "/index.html"):
                return self._serve_index()
            if path in ("/logo.svg", "/favicon.ico", "/favicon.svg"):
                return self._serve_logo()
            if path == "/map":
                return self._serve_static("map.html", "text/html; charset=utf-8")
            if path == "/meadow":
                return self._serve_static("meadow.html", "text/html; charset=utf-8")
            if path == "/map/three.min.js":
                return self._serve_static("three.min.js", "application/javascript; charset=utf-8")
            if path in ("/dog_sprite.png", "/sheep_sprite.png"):
                return self._serve_static(path.lstrip("/"), "image/png")
            if path == "/api/tree":
                return self._serve_tree(urllib.parse.parse_qs(parsed.query))
            if path == "/api/repos":
                return self._serve_repos()
            if path == "/api/session_map":
                return self._serve_session_map(urllib.parse.parse_qs(parsed.query))
            if path == "/api/file":
                return self._serve_file(urllib.parse.parse_qs(parsed.query))
            if path == "/api/live":
                return self._serve_live()
            if path == "/api/sessions":
                return self._serve_sessions(urllib.parse.parse_qs(parsed.query))
            if path == "/api/settings":
                from . import settings
                return self._send_json({"schema": settings.SCHEMA, "values": settings.all_values()})
            if path == "/api/models":
                # The model-picker catalog: one flat list of runnable (provider, model) entries
                # with auth badge + price. ?discover=1 also queries each authed provider's model
                # endpoint (codex/openai/ollama/…) — slower, so it's opt-in from the UI.
                from . import catalog, settings
                q = urllib.parse.parse_qs(parsed.query)
                live = q.get("discover", ["0"])[0] in ("1", "true", "on")
                vals = settings.all_values()
                custom = {"base_url": os.environ.get("COLLIE_COMPAT_BASE", ""),
                          "model": vals.get("MODEL", "")} if vals.get("PROVIDER") == "openai-compat" else None
                entries = catalog.list_entries(discover_live=live, custom=custom)
                current = "%s:%s" % (vals.get("PROVIDER", ""), vals.get("MODEL", ""))
                return self._send_json({"entries": entries, "current": current})
            if path.startswith("/api/delete/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import sessions
                return self._send_json({"ok": sessions.delete(urllib.parse.unquote(path[len("/api/delete/"):]))})
            if path.startswith("/api/rename/"):
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                from . import sessions
                sid = urllib.parse.unquote(path[len("/api/rename/"):])
                title = urllib.parse.parse_qs(parsed.query).get("title", [""])[0]
                return self._send_json({"ok": sessions.set_title(sid, title)})
            if path.startswith("/api/session/"):
                return self._serve_session(path[len("/api/session/"):])
            if path == "/api/mission":                    # delegate: one mission's live status
                from .missionweb import MissionService
                mid = urllib.parse.parse_qs(parsed.query).get("id", [""])[0]
                svc = MissionService()
                try:
                    return self._send_json(svc.status(mid) if mid else {"error": "id required"})
                finally:
                    svc.close()
            if path == "/api/missions":                   # delegate: the mission list (sidebar)
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    return self._send_json({"missions": svc.missions()})
                finally:
                    svc.close()
            if path == "/api/stream":
                if not self._authed(parsed):
                    # SSE clients read errors as events; but headers aren't sent yet, so a plain
                    # 403 is fine and the drive-by never starts the agent.
                    return self._send_json({"error": "forbidden"}, 403)
                return self._serve_stream(urllib.parse.parse_qs(parsed.query))
            self._send_html(b"not found", 404, "text/plain; charset=utf-8")
        except BrokenPipeError:
            pass
        except Exception as e:                       # never take the server down on one bad request
            try:
                self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._host_ok():
            return self._send_json({"error": "forbidden host"}, 403)
        try:
            if path == "/api/settings":
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 65536:                       # config is tiny; reject junk/oversize
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                if not isinstance(body, dict):
                    return self._send_json({"error": "expected object"}, 400)
                from . import settings
                saved = settings.save(body)
                settings.apply()                              # take effect for the next query now
                return self._send_json({"ok": True, "values": settings.all_values(), "saved": saved})
            if path == "/api/model":
                # Model picker's one-click switch: merge PROVIDER+MODEL into settings (never
                # clobbers other keys) and apply, so the next run uses the chosen model.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 4096:
                    return self._send_json({"error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                from . import settings, catalog
                provider, model = catalog.resolve((body or {}).get("id", ""))
                if not provider:
                    return self._send_json({"error": "bad model id"}, 400)
                partial = {"PROVIDER": provider}
                if model:
                    partial["MODEL"] = model
                settings.update(partial)
                settings.apply()
                return self._send_json({"ok": True, "provider": provider, "model": model or ""})
            if path in ("/api/mission", "/api/mission/confirm", "/api/mission/resume",
                        "/api/mission/tick"):
                # The NL front door: start/gate/carry a delegate mission from the chat.
                # CSRF-gated like every state-changing route — a mission runs the model
                # and can fire (gated) real-world actions, so a drive-by must never start one.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "bad body"}, 400)
                from . import settings
                settings.apply()                          # run on the Settings-panel provider
                from .missionweb import MissionService
                svc = MissionService()
                try:
                    if path == "/api/mission":
                        goal = (body.get("goal") or "").strip()
                        if not goal:
                            return self._send_json({"error": "goal required"}, 400)
                        bounds = {}
                        if body.get("price_floor") is not None:
                            bounds["price_floor"] = body["price_floor"]
                        return self._send_json(svc.start(
                            goal, autonomous=bool(body.get("autonomous")), **bounds))
                    mid = (body.get("id") or "").strip()
                    if not mid:
                        return self._send_json({"error": "id required"}, 400)
                    if path == "/api/mission/confirm":
                        return self._send_json(svc.confirm(mid, (body.get("nonce") or "").strip()))
                    if path == "/api/mission/resume":
                        return self._send_json(svc.resume(mid))
                    return self._send_json(svc.tick(mid))     # /api/mission/tick
                finally:
                    svc.close()
            if path == "/api/route":
                # The classifying "head": type a message (chat/code/mission) so the UI
                # routes it. CSRF-gated (it runs the model). Model down -> 503, honestly.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                body = self._read_json(8192)
                if body is None:
                    return self._send_json({"error": "bad body"}, 400)
                text = (body.get("text") or "").strip()
                if not text:
                    return self._send_json({"error": "text required"}, 400)
                from . import settings
                settings.apply()
                from .router import classify, ModelUnavailable, DEFAULT_ROUTER_MODEL
                from .providers import make_provider
                try:
                    # the router runs on every message's critical path. Default it to
                    # Sonnet (fast + capable; all of haiku/sonnet/opus scored 28/28 on the
                    # battery, so this trades only latency), overridable up (opus) or down
                    # (haiku) via COLLIE_ROUTER_MODEL. Only anthropic providers take a claude
                    # model id; others keep their own default.
                    _name = os.environ.get("COLLIE_PROVIDER", "mock")
                    _rmodel = os.environ.get("COLLIE_ROUTER_MODEL") or (
                        DEFAULT_ROUTER_MODEL if _name in ("anthropic-oauth", "anthropic") else None)
                    prov = make_provider(_name, _rmodel)
                    return self._send_json(classify(text, prov))
                except ModelUnavailable as e:
                    return self._send_json({"error": "model_unavailable", "detail": str(e)}, 503)
            if path == "/api/write":
                # code-editor write-back: verify (compile + relevant tests) then write, or reject.
                # CSRF-gated like every state-changing route; a whole file can be up to ~2MB.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 2_000_000:
                    return self._send_json({"ok": False, "stage": "guard", "error": "bad body size"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"ok": False, "stage": "guard", "error": "bad json"}, 400)
                rel = (body or {}).get("path")
                content = (body or {}).get("content")
                if not isinstance(rel, str) or not isinstance(content, str):
                    return self._send_json({"ok": False, "stage": "guard",
                                            "error": "need path + content strings"}, 400)
                from . import webedit
                res = webedit.write_checked(os.getcwd(), rel, content)
                return self._send_json(res, 200 if res.get("ok") else 200)
            if path == "/api/upload":
                # stash an attached image; the next /api/stream?imgs=<id> references it. CSRF-gated;
                # base64 up to ~16MB (a big screenshot). Returns {id}.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 16_000_000:
                    return self._send_json({"error": "bad body size"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"error": "bad json"}, 400)
                mt = (body or {}).get("media_type") or "image/png"
                data = (body or {}).get("data") or ""
                if not isinstance(data, str) or not data or not str(mt).startswith("image/"):
                    return self._send_json({"error": "need image data + media_type"}, 400)
                return self._send_json({"id": Handler._img_put(mt, data)})
            if path == "/api/steer":
                # mid-run steering: queue user text for the session's in-flight run. The loop injects
                # it as a user message at the next turn boundary. CSRF-gated; tiny body.
                # {queued:false} means no active run — the client falls back to starting a new turn.
                if not self._authed(parsed):
                    return self._send_json({"error": "forbidden"}, 403)
                try:
                    n = int(self.headers.get("content-length") or 0)
                except ValueError:
                    n = 0
                if n <= 0 or n > 65536:
                    return self._send_json({"queued": False, "error": "bad body"}, 400)
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                except (ValueError, UnicodeDecodeError):
                    return self._send_json({"queued": False, "error": "bad json"}, 400)
                sid = (body or {}).get("session") or ""
                text = ((body or {}).get("q") or "").strip()
                if not sid or not text:
                    return self._send_json({"queued": False, "error": "need session + q"}, 400)
                return self._send_json({"queued": Handler._steer_push(sid, text[:4000])})
            self._send_json({"error": "not found"}, 404)
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            except Exception:
                pass

    # ------------------------------------------------------------------ handlers
    def _serve_logo(self):
        try:
            with open(LOGO_SVG, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("content-type", "image/svg+xml")
            self.send_header("cache-control", "max-age=86400")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self._send_html(b"logo missing", 404, "text/plain; charset=utf-8")

    def _serve_index(self):
        try:
            with open(INDEX_HTML, "rb") as f:
                html = f.read()
            # inject the CSRF secret so same-origin JS can read it (cross-site JS can't reach it).
            # Robust anchor: prefer the charset meta, else the <head>/doctype, else prepend — a
            # silent no-op would give JS an empty token and 403 every /api/* call (whole app dead).
            meta = ('<meta name="collie-token" content="%s">\n' % TOKEN).encode()
            for anchor in (b'<meta charset="utf-8">', b'<head>', b'<!doctype html>', b'<!DOCTYPE html>'):
                if anchor in html:
                    html = html.replace(anchor, anchor + b"\n" + meta, 1)
                    break
            else:
                html = meta + html            # no known anchor — prepend so the token is never missing
            self._send_html(html)
        except FileNotFoundError:
            self._send_html(b"index.html missing next to webapp.py", 500,
                            "text/plain; charset=utf-8")

    # ------------------------------------------------------------------ Map view (galaxy)
    def _serve_static(self, name, ctype):
        """Serve a file from webui/ (three.min.js verbatim). HTML files (map.html) get the CSRF token
        injected like the index so same-origin JS — e.g. the Map's code-editor Commit — can read it."""
        try:
            with open(os.path.join(HERE, "webui", name), "rb") as f:
                data = f.read()
            if name.endswith(".html"):
                meta = ('<meta name="collie-token" content="%s">\n' % TOKEN).encode()
                for anchor in (b'<meta charset="utf-8">', b'<head>', b'<!doctype html>', b'<!DOCTYPE html>'):
                    if anchor in data:
                        data = data.replace(anchor, anchor + b"\n" + meta, 1)
                        break
                else:
                    data = meta + data
            self._send_html(data, 200, ctype)
        except FileNotFoundError:
            self._send_html(("missing %s" % name).encode(), 404, "text/plain; charset=utf-8")

    _TREE_CACHE: dict = {}
    def _serve_tree(self, qs=None):
        """GET /api/tree[?repo=ABS] -> a project's code galaxy (files with loc/defs/names/imports).
        `repo` (must be a git repo under the user's home) picks any discovered project; default = the
        server's cwd. Cached on the dir's mtime so repeated Map loads don't re-walk the tree."""
        from . import codemap
        cwd = os.getcwd()
        repo = ((qs or {}).get("repo", [""])[0] or "").strip()
        if repo:
            home = os.path.realpath(os.path.expanduser("~"))
            cand = os.path.realpath(os.path.expanduser(repo))
            # only a real git repo under home may be mapped (never an arbitrary path)
            if (cand == home or cand.startswith(home + os.sep)) and codemap.git_root(cand) == cand:
                cwd = cand
        try:
            key = (cwd, os.path.getmtime(cwd))
        except OSError:
            key = (cwd, 0)
        if key not in Handler._TREE_CACHE:
            if len(Handler._TREE_CACHE) > 8:
                Handler._TREE_CACHE.clear()            # bounded LRU-ish; keep a few repos warm
            Handler._TREE_CACHE[key] = codemap.build_tree(cwd)
        self._send_json({"cwd": cwd, "repo": os.path.basename(cwd), "files": Handler._TREE_CACHE[key]})

    _REPOS_CACHE: dict = {}
    def _serve_repos(self):
        """GET /api/repos -> git projects under the user's home, one galaxy each."""
        from . import codemap
        home = os.path.expanduser("~")
        if "repos" not in Handler._REPOS_CACHE:
            Handler._REPOS_CACHE["repos"] = codemap.discover_repos(home)
        self._send_json({"cwd": os.getcwd(), "repos": Handler._REPOS_CACHE["repos"]})

    def _serve_session_map(self, qs):
        """GET /api/session_map?id=SID -> the files THAT run touched, grouped by repo (nebulae), plus
        a probe replaying the touches. Single-repo runs light one nebula; cross-repo runs span many."""
        from . import codemap, sessions
        sid = (qs.get("id", [""])[0] or "").strip()
        s = sessions.load(urllib.parse.unquote(sid)) if sid else None
        if not s:
            return self._send_json({"error": "no such session"}, 404)
        self._send_json(codemap.session_map(s, os.getcwd()))

    def _serve_file(self, qs):
        """GET /api/file?path=REL (under cwd) or ?abs=ABS (a repo file elsewhere under home) -> a
        file's source for the code sidebar. Both are guarded (no traversal, home-scoped, source ext)."""
        from . import codemap
        ab = (qs.get("abs", [""])[0] or "").strip()
        if ab:
            src = codemap.read_abs(ab)
            key = ab
        else:
            key = (qs.get("path", [""])[0] or "").strip()
            src = codemap.read_source(os.getcwd(), key)
        if src is None:
            return self._send_json({"error": "not found"}, 404)
        self._send_json({"path": key, "source": src})

    def _host_ok(self) -> bool:
        """Anti-DNS-rebinding: serve only requests whose Host header is a loopback name. Binding
        127.0.0.1 alone doesn't stop rebinding — a page on attacker.com can lower its TTL and
        re-resolve attacker.com -> 127.0.0.1, becoming same-origin with this server and reading the
        CSRF token out of the served HTML. Rejecting a non-loopback Host closes that: the browser
        always sends the navigated hostname (attacker.com) as Host, which never matches loopback."""
        h = (self.headers.get("Host", "") or "").strip()
        host = h.rsplit(":", 1)[0].strip("[]").lower() if h else ""
        return host in ("", "127.0.0.1", "localhost", "::1", "collie.localhost")

    def _authed(self, parsed) -> bool:
        """State-changing / code-executing routes require the per-process token (query param).
        Same-origin page JS supplies it; a drive-by cross-site request cannot read it."""
        import hmac
        got = urllib.parse.parse_qs(parsed.query).get("token", [""])[0]
        return hmac.compare_digest(got, TOKEN)     # constant-time compare

    def _serve_sessions(self, qs=None):
        from . import sessions
        # ?n= (the Map asks for more so it can surface older runs that actually edited code, sorting
        # edited-first client-side); the main composer uses the default.
        try:
            n = max(1, min(80, int(((qs or {}).get("n", ["20"])[0]) or 20)))
        except ValueError:
            n = 20
        self._send_json({"sessions": sessions.recent(n)})

    def _serve_session(self, sid: str):
        from . import sessions
        s = sessions.load(urllib.parse.unquote(sid))
        if not s:
            return self._send_json({"error": "no such session"}, 404)
        # load() rehydrates tool_calls into ToolCall dataclasses; JSON's default=str would emit them
        # as repr strings ("ToolCall(id=…, name=…, args=…)"), which the Map's replay can't parse.
        # Normalize to plain {id, name, args} dicts so /api/session carries structured tool calls.
        for m in s.get("messages", []):
            tcs = m.get("tool_calls")
            if tcs:
                m["tool_calls"] = [
                    tc if isinstance(tc, dict) else
                    {"id": getattr(tc, "id", None), "name": getattr(tc, "name", None),
                     "args": getattr(tc, "args", None)}
                    for tc in tcs]
        self._send_json(s)

    def _serve_stream(self, qs):
        from .cli import make_harness
        from . import sessions, settings
        settings.apply()   # a Settings-panel save takes effect on the next query, no restart

        q = (qs.get("q", [""])[0] or "").strip()
        sid = (qs.get("session", [""])[0] or "").strip() or sessions.new_id()
        # attached images: /api/stream?imgs=<id>,<id> references what the composer POSTed to /api/upload.
        # With images the user_msg becomes a multimodal list (text + image blocks) the provider layer
        # reshapes into each vendor's vision format.
        img_ids = [i for i in (qs.get("imgs", [""])[0] or "").split(",") if i]
        imgs = [Handler._img_get(i) for i in img_ids]
        imgs = [im for im in imgs if im]
        user_msg = q
        if imgs:
            user_msg = ([{"type": "text", "text": q}] if q else []) + \
                       [{"type": "image", "media_type": mt, "data": data} for (mt, data) in imgs]
        self._sse_open()
        if not q and not imgs:
            self._sse("done", {"session": sid, "answer": "", "error": "empty message"})
            return

        # seed the full prior thread so the web UI has the same --continue continuity the CLI has
        prior = sessions.load(sid) if qs.get("session", [""])[0] else None
        history = (prior or {}).get("messages") or []
        cwd = os.getcwd()
        start_d = {"session": sid, "provider": _provider(), "cwd": cwd,
                   "prior_turns": sum(1 for m in history if m.get("role") == "user")}
        self._sse("start", start_d)
        Handler._live_pub("start", start_d)   # let open Maps enter live mode for this run

        # PACK mode (🎯 best-of-N): run the task N times in isolation, pick the winner by what
        # actually PASSES. No token stream — each attempt runs silently; we push a `pack_attempt`
        # event as each one lands, then a `done` carrying the winning answer + why it won.
        if (qs.get("mode", ["normal"])[0]) == "pack":
            from . import pack as _pack
            try:
                n = int(qs.get("n", ["3"])[0] or 3)
            except ValueError:
                n = 3
            check = (qs.get("check", [""])[0] or "").strip() or None
            apply_ = qs.get("apply", ["0"])[0] in ("1", "on", "true")
            self._sse("pack_start", {"n": max(1, min(8, n)), "check": check or "", "apply": apply_})

            def _emit(i, rec):
                self._sse("pack_attempt", {
                    "idx": rec.get("idx", i), "verified": bool(rec.get("verified")),
                    "turns": rec.get("turns", 0), "error": (rec.get("error") or "")[:120],
                    "cost_usd": rec.get("cost_usd", 0.0), "check_pass": rec.get("check_pass")})
            try:
                pr = _pack.run_pack(q, cwd, n=n, check=check, provider=_provider(),
                                    apply=apply_, emit=_emit)
            except BrokenPipeError:
                return
            except Exception as e:
                self._sse("done", {"session": sid, "answer": "", "error": "pack failed: %s" % e})
                return
            win = pr.get("winner")
            ans = pr.get("answer", "") if win is not None else ""
            if ans:
                sessions.save(sid, [{"role": "user", "content": q},
                                    {"role": "assistant", "content": ans}],
                              project="web", cwd=cwd, answer=ans)
            self._sse("done", {
                "session": sid, "answer": ans,
                "error": None if win is not None else ("no winner — " + pr.get("reason", "nothing passed")),
                "pack": True, "winner": win, "reason": pr.get("reason", ""),
                "applied": pr.get("applied", False), "attempts": pr.get("attempts", []),
                "n": pr.get("n"), "cost_usd": pr.get("total_cost_usd", 0.0),
                "subscription": _provider() in ("anthropic-oauth", "claude-cli")})
            return

        h = None
        try:
            # build INSIDE the try: make_harness -> AnthropicOAuth can raise on a missing token
            # (the advertised real path), and the SSE headers are already committed — so a
            # provider error must arrive as a clean `done{error}` frame, not an escaped 500.
            h = make_harness(cwd, provider=_provider(), project="web",
                             code_search=True, web_search=True, exec_code=True, delegate=True)
            # run mode: "herding" (🐕 Extreme Herding) pushes harder — more turns + the executed
            # assert-verify gate on (won't finish until a reproduction prints green).
            # Turn ceilings sit well above typical need: session 10e8 (2026-07-14) exhausted the old
            # 10-turn normal cap mid info-hunt and shipped its half-way plan as the "answer".
            # Subscription providers (anthropic-oauth / claude-cli) make extra turns cost $0.
            if (qs.get("mode", ["normal"])[0]) == "herding":
                h.max_turns = max(h.max_turns, 48)   # never shallower than 48; a bigger panel value wins
                h.verify_gate = True
                h.require_assert = True
                if hasattr(h, "verify_max"):
                    h.verify_max = 4
            elif not settings.get("MAX_TURNS"):
                h.max_turns = 40                     # raised default; the Settings panel / env still wins
            # every structural event hits BOTH the starting client's socket and the live bus (so the
            # Map / mini-map render it in real time); the token firehose stays client-only.
            h.emit = lambda kind, d: (self._sse(kind, d), Handler._live_pub(kind, d))
            h.stream_cb = lambda piece: self._sse("token", {"t": piece})  # real token streaming
            # mid-run steering: register a per-session queue; the loop drains it at each turn boundary.
            # POST /api/steer pushes onto it. Text typed while Collie works becomes the next user turn.
            steer_q = Handler._steer_open(sid)
            def _drain_steer():
                out = []
                while True:
                    try:
                        out.append(steer_q.get_nowait())
                    except queue.Empty:
                        break
                return out
            h.steering = _drain_steer
            # HEARTBEAT: h.run is synchronous, so during a silent gap (a slow tool, then the next
            # turn's time-to-first-token) NO bytes cross the SSE socket. On flaky forwarders (WSL2
            # localhost, some proxies) an idle connection gets dropped mid-run -> the browser shows
            # "stream interrupted" even though the run finished and the answer was saved. A daemon
            # thread pings every 10s (serialized with the run's writes via self._wlock) to keep the
            # connection warm. It stops the instant the socket breaks or the run ends.
            self._wlock = threading.Lock()
            stop_hb = threading.Event()

            def _heartbeat():
                while not stop_hb.wait(10):
                    try:
                        self._sse("ping", {})
                    except Exception:
                        break                  # socket gone — stop pinging (the run's own writes will end it)
            hb = threading.Thread(target=_heartbeat, daemon=True)
            hb.start()
            try:
                res = h.run("web", user_msg, consolidate=True, history=history)
            finally:
                stop_hb.set()                  # end the heartbeat before we send `done`
            sessions.save(sid, res.messages, project="web", cwd=cwd, answer=res.answer or "")
            Handler._live_pub("done", {"session": sid, "turns": res.turns})   # live map: run finished
            self._sse("done", {
                "session": sid, "answer": res.answer or "", "error": res.error,
                "model": res.model, "prefix_tokens": res.prefix_tokens,
                "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
                "total_tokens": res.total_tokens, "turns": res.turns,
                "max_turns": getattr(h, "max_turns", None),
                "tool_calls": res.tool_calls, "wall_ms": res.wall_ms,
                "cost_usd": res.cost_usd,
                # flat-subscription paths draw a fixed bucket, so the real charge is $0 — cost_usd is
                # only a per-token ESTIMATE of what it'd cost on the metered API. Flag it so the UI
                # doesn't present the estimate as a real charge.
                "subscription": _provider() in ("anthropic-oauth", "claude-cli")})
        except BrokenPipeError:
            pass
        except Exception as e:
            try:
                self._sse("done", {"session": sid, "answer": "",
                                   "error": "%s: %s" % (type(e).__name__, e)})
            except Exception:
                pass
        finally:
            Handler._steer_close(sid)      # run over: reject further steers for this session
            if h is not None:
                try:
                    h.memory.close(); h.recorder.close()
                except Exception:
                    pass


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    port = 8787
    open_browser = True
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1]); i += 2; continue
        if a == "--no-open":
            open_browser = False; i += 1; continue
        i += 1

    if not os.path.exists(INDEX_HTML):
        print("warning: %s not found — GET / will 500 until it exists" % INDEX_HTML)

    # Bind gracefully: if the port is taken (a stale `collie web`, or the user re-launching),
    # try the next few ports instead of crashing with a raw traceback. allow_reuse_address so a
    # just-closed server's TIME_WAIT socket doesn't block an immediate restart.
    ThreadingHTTPServer.allow_reuse_address = True
    requested = port
    httpd = None
    for cand in range(requested, requested + 12):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", cand), Handler)
            port = cand
            break
        except OSError as e:
            if e.errno in (98, 48):        # address already in use (Linux 98 / macOS 48)
                continue
            raise
    if httpd is None:
        print("error: ports %d–%d are all in use. Is `collie web` already running? "
              "Open http://127.0.0.1:%d/ , or pass --port <free port>." % (requested, requested + 11, requested))
        return 1
    # a nicer local URL than a bare loopback IP: browsers resolve any *.localhost name to the
    # loopback address per RFC 6761 (zero setup, no /etc/hosts), so collie.localhost:PORT works
    # out of the box while the server still binds 127.0.0.1. VS Code parses the 127.0.0.1 line below.
    url = "http://collie.localhost:%d/" % port
    ip_url = "http://127.0.0.1:%d/" % port
    note = "" if port == requested else "  (%d was busy → auto-picked %d)" % (requested, port)
    # print BOTH: the pretty one for humans, the 127.0.0.1 one so the VS Code extension's regex finds a port.
    print("collie web · %s · provider=%s · Ctrl-C to stop%s" % (url, _provider(), note), flush=True)
    print("            %s" % ip_url, flush=True)
    if open_browser:
        # open the browser a beat after the server is actually accepting connections
        threading.Timer(0.6, lambda: _open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


def _open(url):
    try:
        import webbrowser
        webbrowser.open(url)
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
