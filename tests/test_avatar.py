"""A face per dog (harness/avatar.py).

The premise is identity, so the test that matters most is not "does it draw" but "does the SAME
name give the SAME face" — a dog whose avatar changes on reinstall is worse than one with no
avatar. The rest pins the two claims the design rests on: the eyes really are the pair of fills
this recolours, and geometry is never touched.

    python3 tests/test_avatar.py
"""
import os
import re
import struct
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []
KENNEL = ["Rowan", "Meg", "Bracken", "Nell", "Fly", "Tess", "Moss", "Gwen",
          "Cap", "Jess", "Pip", "Skye", "Roy", "Bess", "Glen", "Juno"]


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def png_pixels(data):
    """Decode our own PNG back to rows of (r,g,b) — the encoder is ours, so nothing else would."""
    import zlib
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    idat = b""
    i = 8
    while i < len(data):
        n = struct.unpack(">I", data[i:i + 4])[0]
        tag = data[i + 4:i + 8]
        if tag == b"IDAT":
            idat += data[i + 8:i + 8 + n]
        i += 12 + n
    raw = zlib.decompress(idat)
    stride = w * 3
    rows = []
    for y in range(h):
        off = y * (stride + 1)
        assert raw[off] == 0, "only filter 0 is written"
        r = raw[off + 1:off + 1 + stride]
        rows.append([tuple(r[x * 3:x * 3 + 3]) for x in range(w)])
    return w, h, rows


def main():
    from harness import avatar

    # --- the whole premise ---------------------------------------------------------------------
    def face(n):
        # everything except `name`, which echoes what was passed in for display
        return {k: v for k, v in avatar.traits(n).items() if k != "name"}

    check(avatar.traits("Rowan") == avatar.traits("Rowan"), "the same name gives the same traits")
    check(face("Rowan") == face("  rowan  "),
          "the FACE is insensitive to case and stray spacing, so a name typed twice is one dog")
    check(avatar.traits("  rowan  ")["name"] == "  rowan  ",
          "...while the name itself is echoed as given, for display")
    check(avatar.png("Rowan", 64) == avatar.png("Rowan", 64),
          "the same name gives byte-identical PNGs — a face must survive a reinstall")
    check(avatar.traits("Rowan") != avatar.traits("Meg"), "different names differ")

    seen = {(t["eye"], t["plate"], t["coat"], t["shade"])
            for t in (avatar.traits(n) for n in KENNEL)}
    check(len(seen) == len(KENNEL),
          "all %d kennel names get distinct faces (%d/%d)" % (len(KENNEL), len(seen), len(KENNEL)))

    # --- the claim the design rests on: those two fills are the eyes -----------------------------
    src = open(avatar.LOGO, encoding="utf-8").read()
    marked = avatar.svg("Rowan", src)
    eye_hex = avatar.traits("Rowan")["eye_hex"]
    check(marked.count('fill="%s"' % eye_hex) == 2,
          "exactly two fills become the eye colour — one eye each, nothing else recoloured with it")

    # and it must be VISIBLE in the raster, not merely present in the markup
    w, h, rows = png_pixels(avatar.png("Rowan", 128))
    want = tuple(int(eye_hex[i:i + 2], 16) for i in (1, 3, 5))

    def close(p, q, tol=26):
        return all(abs(a - b) <= tol for a, b in zip(p, q))

    hits = [(x, y) for y in range(h) for x, px in enumerate(rows[y]) if close(px, want)]
    check(len(hits) > 20, "the eye colour survives rasterising (%d px)" % len(hits))
    if hits:
        xs = [x for x, _ in hits]
        ys = [y for _, y in hits]
        # Two blobs, side by side, in the upper half of a face — that is what eyes are.
        mid = (min(xs) + max(xs)) / 2
        left = [x for x in xs if x < mid]
        right = [x for x in xs if x >= mid]
        check(len(left) > 5 and len(right) > 5, "in two groups, left and right of centre")
        check(max(ys) < h * 0.6, "in the upper part of the head, where eyes are")

    # --- geometry is never touched ---------------------------------------------------------------
    d_src = re.findall(r'd="([^"]*)"', src)
    d_out = re.findall(r'd="([^"]*)"', marked)
    check(d_src == d_out and len(d_src) > 20,
          "every path's geometry is byte-identical — only fills change (%d paths)" % len(d_src))
    check('<rect' in marked and marked.index("<rect") < marked.index("<path"),
          "the plate is inserted BEHIND the head, not over it")

    # --- the white face stays the face -----------------------------------------------------------
    for n in ("Rowan", "Juno", "Skye"):
        out = avatar.svg(n, src)
        check('fill="#FCFCFB"' in out, "%s keeps the white blaze — it is what makes it a collie" % n)

    # --- PNG is a PNG -----------------------------------------------------------------------------
    data = avatar.png("Meg", 64)
    check(data[:8] == b"\x89PNG\r\n\x1a\n", "the output is a real PNG")
    w, h, depth, ctype = struct.unpack(">IIBB", data[16:26])
    check((w, h, depth, ctype) == (64, 64, 8, 2), "64x64, 8-bit truecolour")
    check(len(png_pixels(data)[2]) == 64, "and it decodes back to 64 rows")

    # --- stdlib only, because collie's core is ----------------------------------------------------
    mod = open(os.path.join(ROOT, "harness", "avatar.py"), encoding="utf-8").read()
    third_party = [m for m in re.findall(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)", mod, re.M)
                   if m.split(".")[0] not in
                   {"os", "re", "sys", "zlib", "struct", "hashlib", "colorsys", "math", "io"}]
    check(not third_party, "no third-party imports (found %s)" % (third_party or "none"))

    print("\n  " + ("%d FAILED" % len(fails) if fails else "avatar: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
