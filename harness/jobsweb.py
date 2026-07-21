"""A delegation-first local dashboard for the delegate spine (plan §15).

`collie web` is the coding/chat GUI; this is the WORK surface: Today (jobs and
their state), Inbox (pending confirmations + jobs needing you), and Receipts
(what fired, under which leash, how it verified) — with a Confirm button per
gated action and a one-line "run" form. Reads the real ~/.collie stores live.

Lean + stdlib only (http.server), on brand. Loopback-bound with the same CSRF /
DNS-rebinding gate as the browser bridge: GET is a normal page load, but every
state-changing POST (confirm / run) requires a same-origin custom header a
drive-by web page cannot set cross-origin without a preflight we refuse — so no
web page can drive your delegate.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import capabilities as _caps
from .actions import ActionStore, RefusedError
from .jobs import Executor, JobStore

_HDR = "X-Collie-Jobs"

_PROV = {"built": False, "p": None}


def _provider():
    """The configured model provider for the mandate compiler, built once and
    cached. Returns None if it can't be built — the compiler then uses its
    no-model heuristic, so the NL box still works offline."""
    if not _PROV["built"]:
        _PROV["built"] = True
        try:
            from . import settings as _s
            from .providers import make_provider
            _s.apply()
            _PROV["p"] = make_provider(_s.get("PROVIDER"), _s.get("MODEL"))
        except Exception:
            _PROV["p"] = None
    return _PROV["p"]

PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<title>collie · delegate</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--card:#fff;--line:#e6e6e3;--ok:#0a7d3f;--wait:#b26a00;--need:#a11;--acc:#2b6cb0}
@media(prefers-color-scheme:dark){:root{--bg:#16171a;--fg:#e8e8e6;--mut:#9a9a97;--card:#1e2024;--line:#2c2f34;--ok:#4ade80;--wait:#fbbf24;--need:#f87171;--acc:#7cb3f0}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
header{padding:16px 22px;border-bottom:1px solid var(--line);display:flex;align-items:baseline;gap:12px}
h1{font-size:18px;margin:0;font-weight:650}.sub{color:var(--mut);font-size:12px}
main{max-width:960px;margin:0 auto;padding:18px 22px;display:grid;gap:22px}
section h2{font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:var(--mut);margin:0 0 10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:8px;display:flex;gap:12px;align-items:center;justify-content:space-between}
.card .m{min-width:0}.goal{font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meta{color:var(--mut);font-size:12px;margin-top:2px;word-break:break-all}
.pill{font-size:11px;font-weight:650;padding:2px 9px;border-radius:999px;white-space:nowrap}
.done_verified{color:var(--ok);background:color-mix(in srgb,var(--ok) 15%,transparent)}
.done_accepted{color:var(--acc);background:color-mix(in srgb,var(--acc) 15%,transparent)}
.waiting,.running,.queued{color:var(--wait);background:color-mix(in srgb,var(--wait) 15%,transparent)}
.needs_you,.failed,.cancelled{color:var(--need);background:color-mix(in srgb,var(--need) 15%,transparent)}
button{font:inherit;font-weight:600;border:1px solid var(--acc);color:#fff;background:var(--acc);border-radius:8px;padding:6px 12px;cursor:pointer}
button.ghost{background:transparent;color:var(--acc)}
button:disabled{opacity:.5;cursor:default}
form.run{display:flex;gap:8px;flex-wrap:wrap;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
input{font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 9px}
input.grow{flex:1;min-width:120px}
.empty{color:var(--mut);font-size:13px;padding:6px 2px}
.ev{color:var(--mut);font-size:12px;margin-top:3px}
.flash{position:fixed;bottom:16px;left:50%;transform:translateX(-50%);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px;box-shadow:0 4px 16px rgba(0,0,0,.15);opacity:0;transition:opacity .2s}
.flash.on{opacity:1}
</style></head><body>
<header><h1>collie · delegate</h1><span class="sub" id="sub">loading…</span></header>
<main>
<section><h2>Tell collie what to do</h2>
<form class="run" id="askf">
<input class="grow" id="ask" placeholder="e.g. 记一下 今晚买菜记得带伞" autofocus>
<button>Go</button></form>
<div class="ev" id="askout"></div></section>
<section><h2>…or run a capability directly</h2>
<form class="run" id="runf">
<input id="cap" value="note.append" title="capability">
<input class="grow" id="args" value='{"file":"todo.txt","text":"buy milk"}' title="JSON args">
<input id="leash" value='{"may":["note.*"]}' title="leash JSON" size="18">
<button class="ghost">Run</button></form></section>
<section><h2>Inbox — needs you</h2><div id="inbox"></div></section>
<section><h2>Today — jobs</h2><div id="jobs"></div></section>
<section><h2>Receipts</h2><div id="receipts"></div></section>
</main>
<div class="flash" id="flash"></div>
<script>
const H={'Content-Type':'application/json','X-Collie-Jobs':'1'};
const esc=s=>(s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function flash(m){const f=document.getElementById('flash');f.textContent=m;f.classList.add('on');setTimeout(()=>f.classList.remove('on'),1800);}
async function post(url,body){const r=await fetch(url,{method:'POST',headers:H,body:JSON.stringify(body)});return r.json();}
function pill(s){return `<span class="pill ${esc(s)}">${esc(s)}</span>`;}
async function load(){
 let d; try{d=await (await fetch('/api/state',{headers:H})).json();}catch(e){return;}
 document.getElementById('sub').textContent=`${d.jobs.length} jobs · ${d.pending.length} awaiting confirm · ${d.receipts.length} receipts`;
 const inbox=document.getElementById('inbox');inbox.innerHTML='';
 d.pending.forEach(p=>{const el=document.createElement('div');el.className='card';
  el.innerHTML=`<div class="m"><div class="goal">${esc(p.capability)}</div><div class="meta">${esc(p.args_json)}</div></div>
  <button data-n="${esc(p.nonce)}">Confirm & run</button>`;
  el.querySelector('button').onclick=async e=>{e.target.disabled=true;const r=await post('/api/confirm',{nonce:p.nonce});flash(r.status?`${r.status}: ${r.reason||''}`:(r.error||'done'));load();};
  inbox.appendChild(el);});
 d.jobs.filter(j=>j.state==='needs_you').forEach(j=>{const el=document.createElement('div');el.className='card';
  el.innerHTML=`<div class="m"><div class="goal">${esc(j.goal)}</div><div class="meta">${esc(j.job_id)}</div></div>${pill(j.state)}`;inbox.appendChild(el);});
 if(!inbox.children.length)inbox.innerHTML='<div class="empty">nothing waiting on you</div>';
 const jobs=document.getElementById('jobs');jobs.innerHTML='';
 d.jobs.slice().reverse().forEach(j=>{const el=document.createElement('div');el.className='card';
  el.innerHTML=`<div class="m"><div class="goal">${esc(j.goal)}</div><div class="meta">${esc(j.job_id)}${j.result?' · '+esc(j.result):''}</div></div>${pill(j.state)}`;jobs.appendChild(el);});
 if(!jobs.children.length)jobs.innerHTML='<div class="empty">no jobs yet — run one above</div>';
 const rc=document.getElementById('receipts');rc.innerHTML='';
 d.receipts.slice().reverse().slice(0,40).forEach(r=>{const el=document.createElement('div');el.className='card';
  el.innerHTML=`<div class="m"><div class="goal">${esc(r.capability)} ${r.fired?'':'(not fired)'}</div>
  <div class="ev">${esc(r.evidence||r.verdict_reason||'')}</div></div>${pill(r.verdict)}`;rc.appendChild(el);});
 if(!rc.children.length)rc.innerHTML='<div class="empty">no receipts yet</div>';
}
document.getElementById('askf').onsubmit=async e=>{e.preventDefault();
 const box=document.getElementById('ask');const t=box.value.trim();if(!t)return;
 const out=document.getElementById('askout');out.textContent='thinking…';
 const r=await post('/api/ask',{text:t});
 const p=r.interpreted||{};
 if(!p.capability){out.textContent='🤔 '+(r.message||p.clarify||'not sure what you mean');return;}
 out.innerHTML=`understood → <b>${esc(p.capability)}</b> ${esc(JSON.stringify(p.args))} `
  +`<span class="pill ${esc(r.status||'')}">${esc(r.status||'')}</span> ${esc(r.reason||'')}`;
 box.value='';flash(r.status?`${r.status}: ${r.reason||''}`:(r.error||'done'));load();};
document.getElementById('runf').onsubmit=async e=>{e.preventDefault();
 let args,leash;try{args=JSON.parse(document.getElementById('args').value||'{}');leash=JSON.parse(document.getElementById('leash').value||'{}');}
 catch(err){flash('bad JSON: '+err.message);return;}
 const r=await post('/api/run',{capability:document.getElementById('cap').value.trim(),args,leash});
 flash(r.status?`${r.status}: ${r.reason||''}`:(r.error||'ok'));load();};
load();setInterval(load,3000);
</script></body></html>"""


def _make_handler(state_dir: str, enforce_host: bool = True):
    apath = os.path.join(state_dir, "actions.db")
    jpath = os.path.join(state_dir, "jobs.db")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        # --- CSRF / DNS-rebinding gate (mirrors browserbridge) ---
        def _bad_host(self):
            if not enforce_host:
                return False
            h = (self.headers.get("Host", "") or "").rsplit(":", 1)[0].strip("[]").lower()
            return h not in ("", "127.0.0.1", "localhost", "::1")

        def _post_blocked(self):
            # state-changing POSTs need the same-origin custom header. THIS is the
            # CSRF gate: a cross-origin page cannot set a custom header without a
            # CORS preflight, and we answer none, so its request never lands. Do
            # NOT reject on a bare http Origin — this dashboard IS a web page and
            # its own same-origin fetch legitimately sends Origin; rejecting it
            # 403'd the dashboard's own calls (which surfaced as "not sure").
            # DNS-rebinding (a rebound host that becomes same-origin and could then
            # set the header) is closed by _bad_host: loopback Host only.
            return self._bad_host() or not self.headers.get(_HDR)

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")   # live dashboard — never cache
            self.end_headers()
            self.wfile.write(data)

        def _json(self, code, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False, default=str))

        def do_GET(self):
            if self._bad_host():
                self._send(403, b"bad host"); return
            if self.path == "/" or self.path.startswith("/index"):
                self._send(200, PAGE, "text/html"); return
            if self.path.startswith("/api/state"):
                self._json(200, self._state()); return
            self._send(404, b"not found")

        def do_POST(self):
            if self._post_blocked():
                self._json(403, {"error": "blocked (loopback + same-origin only)"}); return
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except Exception:
                self._json(400, {"error": "bad json"}); return
            if self.path.startswith("/api/ask"):
                self._json(200, self._ask(body)); return
            if self.path.startswith("/api/confirm"):
                self._json(200, self._confirm(body)); return
            if self.path.startswith("/api/run"):
                self._json(200, self._run(body)); return
            self._json(404, {"error": "not found"})

        # --- data ---
        def _stores(self):
            return ActionStore(apath), JobStore(jpath)

        def _state(self):
            acts, jobs = self._stores()
            try:
                return {
                    "jobs": [vars(j) for j in jobs.list()],
                    "pending": acts.pending(),
                    "receipts": acts.receipts(),
                }
            finally:
                acts.close(); jobs.close()

        def _confirm(self, body):
            nonce = (body or {}).get("nonce", "")
            acts, jobs = self._stores()
            try:
                rec = acts.get(nonce)
                if not rec:
                    return {"error": "unknown nonce"}
                try:
                    acts.confirm(nonce)
                except RefusedError as e:
                    pass  # already approved/executed — let the executor reconcile
                try:
                    v = Executor(acts, jobs).run_confirmed(nonce, job_id=rec.job_id)
                    return {"status": v.status, "reason": v.reason}
                except RefusedError as e:
                    return {"error": str(e)}
            finally:
                acts.close(); jobs.close()

        def _ask(self, body):
            """Natural language -> compile to a job -> drive it. Shows the chosen
            interpretation so the user can see (and, being a job, undo) it."""
            import secrets
            from . import mandate
            text = (body or {}).get("text", "").strip()
            if not text:
                return {"interpreted": {}, "message": "say something"}
            plan = mandate.compile(text, _provider())
            if not plan.get("capability"):
                return {"interpreted": plan, "message": plan.get("clarify")
                        or "not sure what to do"}
            acts, jobs = self._stores()
            try:
                jid = "job-" + secrets.token_hex(4)
                jobs.create(jid, plan.get("goal") or text, leash=plan.get("leash") or {})
                nonce = acts.propose(plan["capability"], plan.get("args") or {}, job_id=jid)
                try:
                    v = Executor(acts, jobs).drive(nonce)
                    return {"interpreted": plan, "status": v.status, "reason": v.reason,
                            "job_id": jid}
                except RefusedError as e:
                    return {"interpreted": plan, "error": str(e), "job_id": jid}
            finally:
                acts.close(); jobs.close()

        def _run(self, body):
            import secrets
            cap = (body or {}).get("capability", "").strip()
            if not cap:
                return {"error": "capability required"}
            args = (body or {}).get("args") or {}
            leash = (body or {}).get("leash") or {}
            goal = (body or {}).get("goal") or cap
            acts, jobs = self._stores()
            try:
                jid = "job-" + secrets.token_hex(4)
                jobs.create(jid, goal, leash=leash)
                nonce = acts.propose(cap, args, job_id=jid)
                try:
                    v = Executor(acts, jobs).drive(nonce)
                    return {"status": v.status, "reason": v.reason, "job_id": jid}
                except RefusedError as e:
                    return {"error": str(e), "job_id": jid}
            finally:
                acts.close(); jobs.close()

    return H


def serve(host: str = "127.0.0.1", port: int = 8794, state_dir: str = None,
          open_browser: bool = True):
    state_dir = state_dir or os.path.expanduser("~/.collie")
    os.makedirs(state_dir, exist_ok=True)
    _caps.register_builtins()
    srv = ThreadingHTTPServer((host, port), _make_handler(state_dir))
    url = f"http://{host}:{port}/"
    print(f"collie delegate dashboard on {url}  (Ctrl-C to stop)", flush=True)
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")
    finally:
        srv.shutdown()
