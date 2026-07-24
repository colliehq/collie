"""Regenerate the webui's Traditional-Chinese (ZHTW) i18n dict from the Simplified (ZH) one.

The GUI keeps one hand-written Chinese dict (ZH, Simplified) and derives Traditional from it with
OpenCC's s2twp profile — which does Taiwan *idiom*, not a blind character swap: 設定 (not 設置),
儲存 (not 保存), 載入 (not 加载), 訊息 (not 消息), 執行 (not 运行), 連線 (not 连接), 登入/傳送.
OpenCC only touches Han characters, so English keys, punctuation and <kbd> tags pass through intact.

Run this after editing the ZH dict so the two never drift:

    pip install opencc            # maintainer-only; not a runtime dep
    python installer/gen_zhtw.py  # rewrites the ZHTW block in harness/webui/index.html in place
"""
import os
import re
import sys

HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                    "harness", "webui", "index.html")


def block(src, decl):
    """Return (start, end) span of `var <decl> = { ... };` by brace-matching."""
    start = src.index("  var %s = {" % decl)
    depth = 0
    for j in range(start + len("  var %s = " % decl), len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return start, src.index(";", j) + 1
    raise SystemExit("could not find the %s block" % decl)


def main():
    try:
        from opencc import OpenCC
    except ImportError:
        sys.exit("opencc not installed — `pip install opencc` (maintainer-only tool).")
    cc = OpenCC("s2twp")
    src = open(HTML, encoding="utf-8").read()

    zs, ze = block(src, "ZH")
    zh_text = src[zs:ze]
    zhtw_text = cc.convert(zh_text).replace("var ZH = {", "var ZHTW = {")

    ts, te = block(src, "ZHTW")
    new = src[:ts] + zhtw_text + src[te:]
    if new == src:
        print("ZHTW already up to date.")
        return
    open(HTML, "w", encoding="utf-8", newline="\n").write(new)
    print("Regenerated the ZHTW block (%d chars) from ZH." % len(zhtw_text))


if __name__ == "__main__":
    main()
