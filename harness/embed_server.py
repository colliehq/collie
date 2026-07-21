"""Resident embedding daemon — keep the local embedding model WARM across CLI invocations.

collie is a fresh process per call, so the ~1.3s fastembed/ONNX cold-load was paid EVERY run
(the dominant latency on a trivial query — see the timing breakdown in docs). This daemon loads
the model ONCE and serves vectors over a Unix socket in ~50-150ms, so every subsequent `collie`
call is fast — trivial chat and real coding tasks alike. It is the root-cause fix for per-call
latency. The client (embeddings.DaemonEmbedding) auto-spawns it and falls back to in-process on
ANY failure, so nothing breaks if the daemon can't start.

Protocol: line-delimited JSON over AF_UNIX, one request per connection.
  {"op":"ping"}                    -> {"ok":true,"dim":1024,"name":"jina-embeddings-v3"}
  {"texts":[...],"kind":"query"}   -> {"ok":true,"vectors":[[...],...]}
  {"op":"shutdown"}                -> {"ok":true}   (then exits)
Idle self-shutdown after COLLIE_EMBED_IDLE seconds (default 1800) so it never leaks memory.
"""
import hashlib
import json
import os
import socket
import sys
import threading
import time

_DEFAULT_MODEL = "jinaai/jina-embeddings-v3"


def sock_path(model):
    base = os.path.expanduser("~/.collie")
    os.makedirs(base, exist_ok=True)
    h = hashlib.sha1(model.encode()).hexdigest()[:8]
    return os.path.join(base, "embed-%s.sock" % h)


def _alive(path):
    """True if a daemon is already accepting on `path`."""
    if not os.path.exists(path):
        return False
    try:
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(2)
        c.connect(path)
        c.close()
        return True
    except OSError:
        return False


def serve(model=_DEFAULT_MODEL, sock=None, idle=None):
    import fcntl
    path = sock or sock_path(model)
    # Serialize cold starts: two `collie` procs starting at once must NOT both load the ~1GB
    # model before one binds. Hold an exclusive flock across the load+bind; the loser wakes,
    # sees the socket already live, and yields without loading.
    lockf = open(path + ".lock", "w")
    fcntl.flock(lockf, fcntl.LOCK_EX)
    try:
        if _alive(path):                  # someone bound while we waited -> yield, no load
            return
        if os.path.exists(path):
            try:
                os.unlink(path)           # stale socket from a dead daemon -> reclaim
            except OSError:
                pass
        from .embeddings import LocalEmbedding
        emb = LocalEmbedding(model)       # THE one-time cold load (kept warm hereafter)
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(path)
        srv.listen(64)
    finally:
        fcntl.flock(lockf, fcntl.LOCK_UN)  # release AFTER bind so a waiter sees _alive()==True
    lock = threading.Lock()               # serialize ONNX inference (one session, be safe)
    state = {"last": time.time()}
    idle = int(idle if idle is not None else os.environ.get("COLLIE_EMBED_IDLE", "1800"))

    def reaper():
        while True:
            time.sleep(30)
            # Hold the inference lock before exiting so we can NEVER kill the process during an
            # in-flight embed_batch (which holds `lock`). A new embed can't start while we hold
            # it, so unlink+exit under the lock is race-free.
            with lock:
                if time.time() - state["last"] > idle:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                    os._exit(0)
    threading.Thread(target=reaper, daemon=True).start()

    def handle(conn):
        try:
            conn.settimeout(120)
            buf = b""
            while b"\n" not in buf:
                chunk = conn.recv(1 << 16)
                if not chunk:
                    return
                buf += chunk
                if len(buf) > (32 << 20):     # 32MB cap: a client that never sends \n can't OOM us
                    return
            req = json.loads(buf.split(b"\n", 1)[0] or b"{}")
            state["last"] = time.time()
            op = req.get("op")
            if op == "ping":
                resp = {"ok": True, "dim": emb.dim, "name": emb.name}
            elif op == "shutdown":
                with lock:                    # acquire the embed lock so we never exit mid-embed
                    try:
                        conn.sendall((json.dumps({"ok": True}) + "\n").encode())
                        os.unlink(path)
                    except OSError:
                        pass
                    os._exit(0)
            else:
                with lock:
                    vecs = emb.embed_batch(req.get("texts", []), req.get("kind", "passage"))
                    state["last"] = time.time()   # restamp AFTER work so a long embed isn't
                resp = {"ok": True, "vectors": vecs}  # judged idle mid-flight
            conn.sendall((json.dumps(resp) + "\n").encode())
        except Exception as e:
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(e)}) + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()

    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def _stop(model=_DEFAULT_MODEL, sock=None):
    path = sock or sock_path(model)
    if not _alive(path):
        print("no daemon at %s" % path)
        return
    try:
        c = socket.socket(socket.AF_UNIX)
        c.settimeout(5)
        c.connect(path)
        c.sendall(b'{"op":"shutdown"}\n')
        c.recv(256)
        c.close()
        print("stopped daemon at %s" % path)
    except OSError as e:
        print("could not stop: %s" % e)


def main():
    import argparse
    ap = argparse.ArgumentParser(prog="collie-embed-daemon")
    ap.add_argument("--model", default=os.environ.get("COLLIE_EMBED_MODEL", _DEFAULT_MODEL))
    ap.add_argument("--sock", default=None)
    ap.add_argument("--idle", type=int, default=None)
    ap.add_argument("--stop", action="store_true")
    a = ap.parse_args()
    if a.stop:
        _stop(a.model, a.sock)
    else:
        serve(a.model, a.sock, a.idle)


if __name__ == "__main__":
    main()
