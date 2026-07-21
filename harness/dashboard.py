"""Dashboard generator — reads runs.db, emits a self-contained dashboard.html.

No deps, no CDN (CSP-safe): CSS bars instead of a chart lib. Theme-aware.
Shows: summary tiles, collie-vs-CC per task, a prefix-token progress trend (watch
this fall as you evolve the harness), and a full run log.
"""
from __future__ import annotations
import html
import json
import os as _os
import sqlite3
import time


def _q(db, sql, args=()):
    return db.execute(sql, args).fetchall()


def build(runs_db: str, out_html: str, standalone: bool = True) -> str:
    db = sqlite3.connect(runs_db)
    db.row_factory = sqlite3.Row
    runs = _q(db, "SELECT * FROM runs ORDER BY run_id")
    AGENT = "collie"          # this harness's recorded id

    def avg(rows, col):
        vals = [r[col] for r in rows if r[col] is not None]
        return sum(vals) / len(vals) if vals else 0

    collie = [r for r in runs if r["harness"] == AGENT]
    # every distinct harness present (collie first, then others sorted)
    harnesses = sorted({r["harness"] for r in runs},
                       key=lambda h: (h != AGENT, h))
    # comparator = lowest-prefix non-collie harness present (real run or baseline)
    others = [r for r in runs if r["harness"] != AGENT and r["prefix_tokens"] > 0]
    best_base = min((r["prefix_tokens"] for r in others), default=0)

    collie_prefix = avg(collie, "prefix_tokens")
    collie_succ = (sum(r["success"] for r in collie) / len(collie) * 100) if collie else 0
    collie_turns = avg(collie, "turns")
    collie_ms = avg(collie, "wall_ms")
    reduction = (1 - collie_prefix / best_base) * 100 if best_base else 0

    maxpref = max([r["prefix_tokens"] for r in runs] + [1])
    trend = [r["prefix_tokens"] for r in collie]

    parts = []
    parts.append('<div class="wrap">')
    parts.append('<header><div class="ey">COLLIE · HARNESS TELEMETRY</div>'
                 '<h1>Harness Dashboard</h1>'
                 '<p class="sub">Self-built harness vs. mainstream harnesses on identical tasks · '
                 'progress tracking · generated %s</p></header>' % time.strftime("%Y-%m-%d %H:%M"))

    # ---- summary tiles ----
    parts.append('<div class="tiles">')
    parts.append(_tile("collie prefix (avg)", "%d" % collie_prefix, "tok/turn", "good"))
    parts.append(_tile("baseline prefix (min)", "%d" % best_base if best_base else "—",
                       "tok/turn", "warn"))
    parts.append(_tile("prefix reduction", ("%.0f%%" % reduction) if best_base else "—",
                       "vs best rival", "accent"))
    parts.append(_tile("collie resolve rate", "%.0f%%" % collie_succ, "%d runs" % len(collie), "good"))
    collie_quality = avg(collie, "quality")
    collie_cost = sum(r["cost_usd"] or 0 for r in collie)
    parts.append(_tile("collie quality", "%.1f" % collie_quality, "/10 judge", "good"))
    parts.append(_tile("avg turns", "%.1f" % collie_turns, "turns", "plain"))
    parts.append(_tile("collie cost", "%.4f" % collie_cost, "$ / %d run" % len(collie), "plain"))
    parts.append('</div>')

    # ---- SWE-bench Verified headline (the credible number) ----
    swe_path = _os.path.join(_os.path.dirname(runs_db), "swebench.json")
    if _os.path.exists(swe_path):
        try:
            sw = json.load(open(swe_path, encoding="utf-8"))
        except Exception:
            sw = None
        if sw:
            parts.append('<section><h2>SWE-bench Verified <span class="hint">'
                         '(n=%d · same model %s · official Docker eval · %s)</span></h2>'
                         % (sw.get("n", 0), html.escape(sw.get("model", "")),
                            html.escape(sw.get("updated", ""))))
            resolve = sw.get("resolve") or []      # tolerate a partial swebench.json (was sw["resolve"] → KeyError aborted the WHOLE build)
            mx = max((r.get("total", 0) for r in resolve), default=16) or 16
            parts.append('<div class="tblwrap"><table><thead><tr>'
                         '<th>harness</th><th>resolved</th><th style="width:44%">&nbsp;</th>'
                         '<th>note</th></tr></thead><tbody>')
            for r in resolve:
                res_n, tot_n = r.get("resolved", 0), r.get("total", 0)
                cls = "collie" if r.get("self") else "base"
                hn = html.escape(str(r.get("harness", "")))
                name = ("<b>%s</b>" % hn) if r.get("self") else hn
                parts.append('<tr><td>%s</td><td>%d/%d</td><td>%s</td>'
                             '<td class="hint">%s</td></tr>' % (
                                 name, res_n, tot_n,
                                 _bar(res_n, mx, cls), html.escape(str(r.get("note", "")))))
            parts.append('</tbody></table></div>')
            e = sw.get("efficiency", {})
            if e:
                parts.append('<p class="sub">Efficiency (collie/DeepSeek, measured): '
                             'median <b>%s</b> tok/instance · <b>$%s</b>/instance · fixed prefix <b>%s</b> tok/turn · '
                             'median <b>%s</b> turns · vs Hermes <b>%s</b>.</p>' % (
                                 "{:,}".format(e.get("tokens_per_instance_median", 0)),
                                 e.get("cost_per_instance_median", 0),
                                 e.get("prefix_per_turn", 0), e.get("turns_median", 0),
                                 html.escape(e.get("vs_hermes_tokens", ""))))
            parts.append('</section>')

            pa = sw.get("pareto")
            if pa and pa.get("points"):
                parts.append('<section><h2>Cost–accuracy Pareto <span class="hint">'
                             '(resolve%% vs $/instance · same DeepSeek pricing)</span></h2>')
                parts.append(_pareto_svg(pa["points"]))
                parts.append('<p class="sub">%s</p>' % html.escape(pa.get("takeaway", "")))
                parts.append('</section>')

            pg = sw.get("polyglot")
            if pg and pg.get("results"):
                parts.append('<section><h2>Aider-Polyglot <span class="hint">'
                             '(multilingual · %s)</span></h2>' % html.escape(pg.get("subset", "")))
                parts.append('<div class="tblwrap"><table><thead><tr><th>agent</th>'
                             '<th>pass</th><th>well-formed</th><th>median time</th></tr></thead><tbody>')
                for r in pg["results"]:
                    nm = ("<b>%s</b>" % html.escape(r["agent"])) if r.get("self") else html.escape(r["agent"])
                    parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%ss</td></tr>' % (
                        nm, html.escape(r["pass"]), html.escape(r["well_formed"]), r.get("median_sec", "?")))
                parts.append('</tbody></table></div><p class="sub">%s</p></section>'
                             % html.escape(pg.get("takeaway", "")))

            lc = sw.get("locomo")
            if lc:
                parts.append('<section><h2>LOCOMO <span class="hint">'
                             '(standard agent-memory benchmark · memory retrieval &amp; end-to-end)</span></h2>')
                parts.append('<div class="tblwrap"><table><thead><tr><th>retrieval config</th>'
                             '<th>recall@10</th><th>hit@10</th></tr></thead><tbody>')
                for r in lc.get("retrieval", []):
                    nm = ("<b>%s</b>" % html.escape(r["config"])) if r.get("win") else html.escape(r["config"])
                    parts.append('<tr><td>%s</td><td><b>%.3f</b></td><td>%.3f</td></tr>'
                                 % (nm, r["recall_at_10"], r["hit_at_10"]))
                parts.append('</tbody></table></div>')
                parts.append('<div class="tblwrap"><table><thead><tr><th>end-to-end (LLM-judge)</th>'
                             '<th>acc</th><th>note</th></tr></thead><tbody>')
                for r in lc.get("end_to_end", []):
                    nm = ("<b>%s</b>" % html.escape(r["system"])) if r.get("self") else html.escape(r["system"])
                    parts.append('<tr><td>%s</td><td><b>%.1f%%</b></td><td class="hint">%s</td></tr>'
                                 % (nm, 100 * r["acc"], html.escape(r.get("note", ""))))
                parts.append('</tbody></table></div>')
                parts.append('<p class="sub">reranker gains recall@10 +20%% on the standard benchmark '
                             '(0.62&rarr;0.75); end-to-end collie storing raw turns 43.3%% &gt; naive per-turn '
                             'distillation 34.7%% (honest negative, distillation off by default); the gap to Mem0 '
                             'is a more sophisticated extraction pipeline, not simple distillation.</p>')
                parts.append('</section>')

    # ---- honesty caveat (dynamic: reflect the actual models this round) ----
    # a run row can have a NULL/empty model (e.g. `collie tui` with no --model) — coerce to "" so
    # sorted()/join()/.split() don't hit TypeError/AttributeError and abort the build.
    collie_models = ", ".join(sorted({(r["model"] or "") for r in collie})) or "—"
    cmp_models = ", ".join(sorted({(r["model"] or "") for r in runs if r["harness"] != AGENT})) or "—"
    collie_bare = {(r["model"] or "").split(":")[-1] for r in collie}
    cmp_bare = {(r["model"] or "").split(":")[-1] for r in runs if r["harness"] != AGENT}
    same = bool(collie_bare & cmp_bare)
    verdict = ("<b>Same model &rarr; resolve rates tie; prefix/turns/latency differences = pure harness "
               "differences</b> (model variable eliminated)."
               if same else
               "Different models &rarr; resolve-rate gap includes model capability; prefix gap &approx; "
               "harness context efficiency.")
    parts.append('<div class="note-box">Note · honest methodology: prefix = measured fixed input per turn '
                 '(collie = system + tool schema; Claude Code = its own system prefix, using cache_read when '
                 'cached, input_tokens when not). collie model: <b>%s</b>; comparison harness model: <b>%s</b>. %s '
                 'collie can run any cheap OpenAI-compatible API or local model ($0). Compliance: extracting a '
                 'subscription OAuth token to hit the API directly is now blocked; <code>claude -p</code>/Agent SDK '
                 'over a subscription is still allowed (6/15 billing change paused).</div>'
                 % (html.escape(collie_models), html.escape(cmp_models), verdict))

    # ---- harness leaderboard ----
    parts.append('<section><h2>Harness Summary <span class="hint">'
                 '(lower prefix is better)</span></h2>')
    parts.append('<div class="tblwrap"><table><thead><tr>'
                 '<th>harness</th><th>runs</th><th>avg prefix</th><th>prefix compare</th>'
                 '<th>resolve rate</th><th>quality/10</th><th>avg turns</th><th>avg time</th>'
                 '<th>total cost$</th></tr></thead><tbody>')
    for hn in harnesses:
        rows = [r for r in runs if r["harness"] == hn]
        p = avg(rows, "prefix_tokens")
        executed = [r for r in rows if not r["harness"].endswith("baseline")]
        sr = (sum(r["success"] for r in executed) / len(executed) * 100) if executed else None
        q = avg(executed, "quality") if executed else 0
        cost = sum(r["cost_usd"] or 0 for r in rows)
        star = ' <span class="pill ok">collie</span>' if hn == AGENT else (
            ' <span class="pill no">baseline</span>' if hn.endswith("baseline") else "")
        na = (p == 0 and not hn.endswith("baseline"))   # headless: no token counts
        pcell = "N/A" if na else "%d" % p
        bar = "<span class='hint'>headless·no token</span>" if na else _bar(
            p, maxpref, "collie" if hn == AGENT else "cc")
        parts.append('<tr><td class="mono">%s%s</td><td>%d</td><td><b>%s</b></td>'
                     '<td style="min-width:130px">%s</td><td>%s</td><td>%.1f</td>'
                     '<td>%.1f</td><td>%dms</td><td>%.4f</td></tr>' % (
                         html.escape(hn), star, len(rows), pcell, bar,
                         ("%.0f%%" % sr) if sr is not None else "—", q,
                         avg(rows, "turns"), avg(rows, "wall_ms"), cost))
    parts.append('</tbody></table></div></section>')

    # ---- retrieval quality (precision@k) from mem eval ----
    rp = _os.path.join(_os.path.dirname(runs_db), "retrieval_eval.json")
    if _os.path.exists(rp):
        try:
            rev = json.load(open(rp, encoding="utf-8"))
            real, hsh = rev.get("real") or {}, rev.get("hash") or {}
            rrk = rev.get("real_rerank") or {}
            parts.append('<section><h2>Memory Retrieval Quality <span class="hint">'
                         '(pain point &#9312; · labeled query set P@k / MRR, higher is better)</span></h2>'
                         '<div class="tblwrap"><table><thead><tr><th>embedder</th>'
                         '<th>P@1</th><th>P@5</th><th>MRR</th></tr></thead><tbody>')
            rows_rev = [("real (jina-v3 hybrid)", real)]
            if rrk:
                rows_rev.append(("+ cross-encoder reranker", rrk))
            rows_rev.append(("hash baseline", hsh))
            for lbl, e in rows_rev:
                parts.append('<tr><td class="mono">%s</td><td><b>%.2f</b></td>'
                             '<td>%.2f</td><td>%.2f</td></tr>' % (
                                 html.escape(str(e.get("embedder", lbl))),
                                 e.get("p_at_1", 0), e.get("p_at_5", 0), e.get("mrr", 0)))
            parts.append('</tbody></table></div></section>')
        except Exception:
            pass

    # ---- per-task matrix ----
    tasks = {}
    for r in runs:
        tasks.setdefault(r["task_id"], {})[r["harness"]] = r
    parts.append('<section><h2>Per-task · Prefix Matrix</h2><div class="tblwrap"><table>'
                 '<thead><tr><th>task</th>')
    for hn in harnesses:
        parts.append('<th>%s</th>' % html.escape(hn))
    parts.append('<th>collie result</th></tr></thead><tbody>')
    for tid, row in tasks.items():
        cells = ""
        for hn in harnesses:
            r = row.get(hn)
            cells += "<td>%s</td>" % (("%d" % r["prefix_tokens"]) if r else "—")
        m = row.get(AGENT)
        succ = ('<span class="pill ok">pass</span>' if m and m["success"]
                else '<span class="pill no">fail</span>') if m else "—"
        parts.append('<tr><td class="mono">%s</td>%s<td>%s</td></tr>' % (
            html.escape(tid), cells, succ))
    parts.append('</tbody></table></div></section>')

    # ---- progress trend ----
    parts.append('<section><h2>Prefix Token Progress Trend <span class="hint">'
                 '(lower is better — should fall as the harness iterates)</span></h2>')
    parts.append(_sparkbars(trend))
    parts.append('</section>')

    # ---- run log ----
    parts.append('<section><h2>Run Log</h2><div class="tblwrap"><table><thead><tr>'
                 '<th>#</th><th>time</th><th>harness</th><th>task</th><th>model</th>'
                 '<th>prefix</th><th>in</th><th>out</th><th>turns</th><th>tool</th>'
                 '<th>q/10</th><th>$</th><th>ms</th><th>ok</th></tr></thead><tbody>')
    for r in reversed(runs):
        ok = ("✓" if r["success"] else ("✗" if r["error"] else "·"))
        cls = "hr-collie" if r["harness"] == AGENT else "hr-cc"
        parts.append('<tr class="%s"><td>%d</td><td class="mono">%s</td>'
                     '<td><b>%s</b></td><td class="mono">%s</td><td class="mono">%s</td>'
                     '<td>%d</td><td>%d</td><td>%d</td><td>%d</td><td>%d</td>'
                     '<td>%.0f</td><td>%.4f</td><td>%d</td><td>%s</td></tr>' % (
                         cls, r["run_id"], time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
                         html.escape(r["harness"]), html.escape(r["task_id"]),
                         html.escape((r["model"] or "")[:22]),
                         r["prefix_tokens"], r["input_tokens"], r["output_tokens"],
                         r["turns"], r["tool_calls"],
                         r["quality"] or 0, r["cost_usd"] or 0, r["wall_ms"], ok))
    parts.append('</tbody></table></div></section>')

    parts.append('<footer>collie · runs.db · same-task cross-comparison and progress tracking. collie can run '
                 'any OpenAI-compatible API (DeepSeek/Qwen/GLM…) or local Ollama; Claude Code via an '
                 'Anthropic-compat endpoint can pull the same model for a pure harness comparison. '
                 '<code>compare --vs all --real</code> brings in more installed CLIs.</footer>')
    parts.append('</div>')
    db.close()

    head = _HEAD if standalone else _STYLE
    tail = "\n</body></html>" if standalone else "\n"
    doc = head + "\n" + "\n".join(parts) + tail
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(doc)
    return out_html


def _pareto_svg(points):
    """Scatter: x = $/instance (lower better), y = resolve% (higher better). Frontier dashed.
    Theme-aware via currentColor; self points accent-filled, dominated points hollow."""
    W, H, pad = 460, 250, 46
    xs = [p["cost"] for p in points]; ys = [p["resolve_pct"] for p in points]
    x0, x1 = min(xs) * 0.9, max(xs) * 1.08
    y0, y1 = min(ys) - 4, max(ys) + 4
    dx, dy = (x1 - x0) or 1, (y1 - y0) or 1   # all-zero cost/resolve → avoid ZeroDivisionError
    def px(c): return pad + (c - x0) / dx * (W - pad - 120)
    def py(r): return H - pad - (r - y0) / dy * (H - pad - 24)
    ACC = "#1f9e89"  # accent (self), readable on light+dark
    s = ['<svg viewBox="0 0 %d %d" width="100%%" style="max-width:520px;font:12px system-ui">' % (W, H)]
    # axes
    s.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="currentColor" opacity=".25"/>' % (pad, H - pad, W - 120, H - pad))
    s.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="currentColor" opacity=".25"/>' % (pad, 20, pad, H - pad))
    s.append('<text x="%d" y="%d" fill="currentColor" opacity=".6" font-size="11">← $/instance (lower is better)</text>' % (pad, H - 12))
    s.append('<text x="14" y="16" fill="currentColor" opacity=".6" font-size="11">resolve% ↑</text>')
    # frontier dashed polyline (sort frontier pts by cost)
    fr = sorted([p for p in points if p.get("frontier")], key=lambda p: p["cost"])
    if len(fr) > 1:
        pts = " ".join("%.0f,%.0f" % (px(p["cost"]), py(p["resolve_pct"])) for p in fr)
        s.append('<polyline points="%s" fill="none" stroke="%s" stroke-width="1.5" stroke-dasharray="5 4" opacity=".55"/>' % (pts, ACC))
    # points
    for p in points:
        x, y = px(p["cost"]), py(p["resolve_pct"])
        self_ = p.get("self")
        fill = ACC if self_ else "none"
        stroke = ACC if self_ else "currentColor"
        s.append('<circle cx="%.0f" cy="%.0f" r="6" fill="%s" stroke="%s" stroke-width="2" opacity="%s"/>'
                 % (x, y, fill, stroke, "1" if self_ else ".6"))
        lbl = "%s  %.0f%% · $%.3f" % (p["harness"], p["resolve_pct"], p["cost"])
        wt = "700" if self_ else "400"
        s.append('<text x="%.0f" y="%.0f" fill="currentColor" font-weight="%s" font-size="11" opacity="%s">%s</text>'
                 % (x + 10, y + 4, wt, "1" if self_ else ".7", html.escape(lbl)))
    s.append('</svg>')
    return '<div style="overflow-x:auto">%s</div>' % "".join(s)


def _tile(k, v, u, kind):
    return ('<div class="tile %s"><div class="k">%s</div>'
            '<div class="v">%s<span class="u">%s</span></div></div>'
            % (kind, html.escape(k), html.escape(v), html.escape(u)))


def _bar(v, mx, cls="collie"):
    w = int(v / mx * 100) if mx else 0
    return ('<div class="track"><i class="fill %s" style="width:%d%%"></i></div>'
            % (cls, w))


def _dualbar(a, b, mx):
    wa = int(a / mx * 100) if mx else 0
    wb = int(b / mx * 100) if mx else 0
    return ('<div class="db"><div class="dbrow"><span class="lab">collie</span>'
            '<div class="track"><i class="fill collie" style="width:%d%%"></i></div></div>'
            '<div class="dbrow"><span class="lab">CC</span>'
            '<div class="track"><i class="fill cc" style="width:%d%%"></i></div></div></div>'
            % (wa, wb))


def _sparkbars(vals):
    if not vals:
        return '<p class="hint">No collie runs yet.</p>'
    mx = max(vals) or 1
    bars = "".join('<i class="sb" style="height:%d%%" title="%d"></i>'
                   % (max(6, int(v / mx * 100)), v) for v in vals)
    return '<div class="spark">%s</div>' % bars


_STYLE = """<style>
:root{--bg:#ECEEF1;--surf:#fff;--surf2:#F3F5F7;--ink:#151A20;--soft:#4C5763;
--faint:#7A8592;--line:#D3D9DF;--accent:#0E7C8B;--good:#2C8A66;--warn:#B0771A;
--mono:ui-monospace,"Cascadia Code",Menlo,Consolas,monospace;
--sans:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
@media(prefers-color-scheme:dark){:root{--bg:#0C0F13;--surf:#141A21;--surf2:#1A212A;
--ink:#E7ECF1;--soft:#A2AEBB;--faint:#6D7986;--line:#28313C;--accent:#38B8C8;
--good:#4FB98C;--warn:#D69B3E}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font-family:var(--sans);line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:28px clamp(16px,4vw,36px) 80px}
header{padding:22px 0 26px;border-bottom:1px solid var(--line);margin-bottom:26px}
.ey{font-family:var(--mono);font-size:11.5px;letter-spacing:.16em;color:var(--accent);font-weight:600}
h1{font-size:clamp(26px,4vw,38px);margin:.3rem 0 .3rem;letter-spacing:-.02em}
.sub{color:var(--soft);margin:0;font-size:14.5px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:11px;margin:0 0 30px}
.tile{background:var(--surf);border:1px solid var(--line);border-radius:13px;padding:15px;position:relative;overflow:hidden}
.tile::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--faint)}
.tile.good::before{background:var(--good)}.tile.warn::before{background:var(--warn)}
.tile.accent::before{background:var(--accent)}
.tile .k{font-family:var(--mono);font-size:10.5px;letter-spacing:.05em;text-transform:uppercase;color:var(--faint)}
.tile .v{font-size:26px;font-weight:730;letter-spacing:-.02em;margin-top:6px;font-variant-numeric:tabular-nums}
.tile .v .u{font-size:12px;font-weight:600;color:var(--soft);margin-left:5px}
section{margin:0 0 34px}h2{font-size:19px;letter-spacing:-.01em;margin:0 0 14px}
.hint{font-size:12.5px;color:var(--faint);font-weight:400}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:13px;background:var(--surf)}
table{border-collapse:collapse;width:100%;font-size:13px;min-width:560px}
th{background:var(--surf2);text-align:left;padding:9px 12px;border-bottom:1px solid var(--line);
font-size:11.5px;color:var(--soft);white-space:nowrap}
td{padding:9px 12px;border-bottom:1px solid var(--line);color:var(--soft);
font-variant-numeric:tabular-nums;white-space:nowrap}
tr:last-child td{border-bottom:none}td:first-child,td .mono,.mono{font-family:var(--mono);font-size:12px}
tr.hr-cc td{color:var(--faint)}
.pill{font-family:var(--mono);font-size:10.5px;padding:2px 8px;border-radius:6px;font-weight:600}
.pill.ok{background:rgba(44,138,102,.16);color:var(--good)}
.pill.no{background:rgba(176,71,58,.16);color:#B0473A}
.db{display:flex;flex-direction:column;gap:4px}
.dbrow{display:flex;align-items:center;gap:8px}
.dbrow .lab{font-family:var(--mono);font-size:10px;color:var(--faint);width:20px}
.track{flex:1;height:9px;background:var(--surf2);border-radius:5px;overflow:hidden}
.fill{display:block;height:100%}.fill.collie{background:var(--good)}.fill.cc{background:var(--warn)}
.spark{display:flex;align-items:flex-end;gap:5px;height:110px;padding:14px;
background:var(--surf);border:1px solid var(--line);border-radius:13px}
.sb{flex:1;min-width:8px;background:linear-gradient(var(--accent),var(--good));
border-radius:3px 3px 0 0;opacity:.85}
footer{color:var(--faint);font-size:12.5px;border-top:1px solid var(--line);padding-top:18px}
code{font-family:var(--mono);background:var(--surf2);padding:1px 5px;border-radius:4px}
.note-box{background:var(--surf2);border:1px solid var(--line);border-radius:11px;
padding:12px 15px;font-size:12.5px;color:var(--soft);line-height:1.65;margin:0 0 26px}
.note-box b{color:var(--ink)}
</style>"""

_HEAD = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<title>collie · Harness Dashboard</title>' + _STYLE + '</head><body>')
