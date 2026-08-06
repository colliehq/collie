"""A face for each dog, derived from its name.

A dog is an identity — a name, an address, something you can @ — so it needs a face, and the face
has to be the SAME face everywhere. That rules out `random`: it must come out identical on this
laptop, on the mac, after a reinstall, and in the channel where colleagues have learned to
recognise it. The only entropy is sha256(name), sliced into fields.

What varies is colour, never geometry. The silhouette is what makes it a collie; recolouring within
fixed roles keeps every variant obviously the same breed, and the logo cooperates — it is a
low-poly trace whose 23 paths are flat fills of straight segments, with the two eyes as their own
addressable pair.

Sizes decided the palette, not taste. Rendered at the sizes Slack actually draws a bot avatar
(20px in the member list, 36-48px beside a message), a lightness-preserving coat tint is invisible
and the eyes are the only thing that reads — so the eyes carry the identity, and a background plate
carries what is left at 20px, where the head itself is a smudge.

Stdlib only, because collie's core is (`dependencies = []`): the PNG encoder is zlib + struct, and
the rasteriser is a scanline fill, which is enough because every path in the logo is straight
segments with no stroke, curve or gradient.
"""
import colorsys
import hashlib
import os
import re
import struct
import zlib

LOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "logo.svg")

# The logo's fills, grouped by what they ARE. Two of these are the eyes — the symmetric pair at
# translate(180,173) and (341,173) — which is why this needs no redrawing.
# Paths by what they are, found by rendering the logo 23 times with one path flooded and looking.
# It has the tonal structure of a real border collie, which is what makes recolouring possible:
# a near-black mass of ears and skull, a mid-tone that frames the eyes, a pale ruff, a white blaze.
EARS   = (1, 2, 7, 8, 11, 12)          # the dark mass — ears and the top of the head
CHEEKS = (4, 5)                        # mid-tone, second-largest area, frames the eyes
RUFF   = (0, 6, 9, 10, 16, 17, 22)     # the pale chest
BLAZE  = (3,)                          # the white stripe and muzzle ring: never recoloured
NOSE   = (13, 18, 19)
EYES   = (14, 15)
CATCH  = (20, 21)                      # the catchlights inside the eyes

# ONE natural dark brown, for every dog. An earlier version varied the eye colour and saturated it
# hard, on the evidence that at 36px the eye was the only thing that read. It read, all right: an
# iris at lightness .59 on a face whose darks sit at .05 is a light bulb, and every dog looked
# startled. An eye that varies is also an eye you look at, and the identity belongs to the coat.
EYE = "#2B2118"

# (name, hue, ear lightness, ear saturation, cheek lightness, cheek saturation, ruff warmth)
#
# The dark mass must genuinely CHANGE LIGHTNESS, which is the correction that made this work. The
# first attempt preserved lightness exactly, so a hue rotation at lightness .05 was invisible and
# the coat appeared not to read at small sizes at all — a property of the method, not of coats. A
# red collie is red because its "black" areas are mid-brown.
COATS = (
    ("black",     0.620, 0.075, 0.16, 0.20, 0.12, 0.00),
    ("red",       0.045, 0.155, 0.44, 0.35, 0.38, 0.07),
    ("chocolate", 0.075, 0.130, 0.40, 0.29, 0.32, 0.05),
    ("blue",      0.600, 0.140, 0.24, 0.32, 0.20, 0.02),
    ("sable",     0.095, 0.170, 0.36, 0.37, 0.30, 0.08),
    ("lilac",     0.870, 0.150, 0.16, 0.32, 0.13, 0.03),
    ("slate",     0.575, 0.115, 0.15, 0.27, 0.11, 0.01),
    ("fawn",      0.110, 0.185, 0.30, 0.40, 0.24, 0.09),
)

# The plate is the largest flat area in the tile and the only thing that survives the member list at
# 20px, where the head is a smudge. Its hue is an OFFSET FROM THE COAT rather than an independent
# roll, because the two axes are not independent in practice: land the plate on the coat's own hue
# and the dog disappears into its own background.
PLATE_S = 0.42                         # rich enough to be a colour, not so much it becomes a sticker
PLATE_CONTRAST = 1.35                  # how far the plate sits from the head's darkest mass
# Evenly spaced around the wheel and all strictly inside one turn. An earlier table ran 0.10 to
# 1.25 in steps of .05, and hue is taken mod 1 — so its last four offsets landed exactly on four it
# already had, costing 17% of the range without any symptom except a few more lookalikes. Dividing
# by 25 also means no offset is ever 0, so the plate never sits on the coat's own hue.
PLATE_OFFSETS = tuple(round((i + 1) / 25.0, 4) for i in range(24))
# In hue order starting at 0, which is RED. The first version of this list started at "moss", so
# every plate was named a third of the way around the wheel from its actual colour: rowan's navy
# plate was reported as plum, and bracken's plum as clay. Nobody would have caught it from the
# picture, because the picture was right — only the word for it was wrong.
PLATE_NAMES = ("wine", "clay", "amber", "moss", "pine", "sea",
               "teal", "navy", "indigo", "violet", "plum", "rose")


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(r, g, b):
    return "#%02X%02X%02X" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _lightness(hex_colour):
    r, g, b = (v / 255 for v in _hex2rgb(hex_colour))
    return colorsys.rgb_to_hls(r, g, b)[1]


def _hls(h, l, s):
    return _rgb2hex(*(round(v * 255) for v in colorsys.hls_to_rgb(h % 1.0, l, s)))


def _lum(hex_colour):
    """Relative luminance — what actually decides whether two areas separate to the eye."""
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _hex2rgb(hex_colour)
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def _plate(hue, against, target=PLATE_CONTRAST):
    """A plate of this hue, light enough to separate from `against` by `target`.

    Solved in LUMINANCE, not lightness, and this is the point: HLS lightness treats a blue and a
    yellow-green at L .17 as equals, while blue carries a twelfth of green's luminance. A fixed
    plate lightness therefore landed on the same luminance as the ears for half the coats — measured
    contrast 1.00 — and those dogs dissolved into their own background with nothing in the code
    looking wrong. Luminance is monotonic in L for a fixed hue, so bisection is exact enough.
    """
    want = (_lum(against) + 0.05) * target - 0.05
    lo, hi = 0.02, 0.42
    for _ in range(30):
        mid = (lo + hi) / 2
        if _lum(_hls(hue, mid, PLATE_S)) < want:
            lo = mid
        else:
            hi = mid
    return _hls(hue, (lo + hi) / 2, PLATE_S)


def traits(name: str) -> dict:
    """The face of a name. Same name in, same face out — on any machine, forever.

    Two axes, and only two: the coat of the coloured regions, and the plate behind them. 8 coats x
    24 plate offsets is 192 faces, which is not the same as 192 dogs before a repeat — this is the
    birthday problem, so two dogs in a kennel of sixteen may well look alike. That is accepted
    rather than fixed: de-duplicating against the OTHER dogs on this machine would mean a dog's face
    changed when it moved machine, and being the same dog everywhere is the entire point.
    """
    d = hashlib.sha256(name.strip().lower().encode("utf-8")).digest()
    coat = COATS[d[2] % len(COATS)]
    off = PLATE_OFFSETS[d[1] % len(PLATE_OFFSETS)]
    ears = _hls(coat[1], coat[2], coat[3])         # the head's darkest mass, which the plate must clear
    plate_hex = _plate(coat[1] + off, ears)
    # Named from the FINAL hex, not from the hue that produced it. Rounding to 8-bit moves the hue
    # slightly, so a value sitting on a name boundary would otherwise be labelled from one side of
    # the line and drawn from the other. Deriving the word from the colour makes them unable to
    # disagree.
    r, g, b = (v / 255 for v in _hex2rgb(plate_hex))
    hue = colorsys.rgb_to_hls(r, g, b)[0]
    return {"name": name, "coat": coat[0], "eye_hex": EYE,
            "plate": PLATE_NAMES[int(hue * len(PLATE_NAMES)) % len(PLATE_NAMES)],
            "plate_hex": plate_hex, "_coat": coat}


def svg(name: str, source: str = "") -> str:
    """The logo recoloured for one dog, on its plate. Geometry is never touched."""
    t = traits(name)
    _, hue, ear_l, ear_s, cheek_l, cheek_s, warm = t["_coat"]
    src = source or open(LOGO, encoding="utf-8").read()
    seen = [0]

    def swap(m):
        i = seen[0]
        seen[0] += 1
        l0 = _lightness(m.group(1).upper())
        if i in EYES:
            return 'fill="%s"' % EYE
        if i in CATCH or i in BLAZE:
            return m.group(0)                            # the white face is the face; leave it
        if i in NOSE:
            return 'fill="%s"' % _hls(hue, min(0.16, l0 + 0.04), 0.20)
        if i in EARS:
            # the source's own offset is carried through, so shading WITHIN the dark mass survives
            return 'fill="%s"' % _hls(hue, max(0.03, ear_l + (l0 - 0.06) * 0.8), ear_s)
        if i in CHEEKS:
            return 'fill="%s"' % _hls(hue, cheek_l, cheek_s)
        if i in RUFF:
            return 'fill="%s"' % _hls(hue, l0, min(1.0, warm * 2.2))
        return m.group(0)

    out = re.sub(r'fill="(#[0-9A-Fa-f]{6})"', swap, src)
    # The plate goes in as the first child so it sits behind everything, and outside the logo's own
    # <g transform>, which is why it is inserted after the opening <svg> rather than wrapped around.
    return out.replace(">", '><rect x="0" y="0" width="590" height="590" fill="%s"/>'
                       % t["plate_hex"], 1)


# ---------------------------------------------------------------- rasterising, stdlib only

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _polygons(svg_text: str):
    """Every path as (points, rgb), in document order — painter's algorithm, like the SVG itself.

    Only M/L/Z absolute commands appear in this logo (VTracer polygon mode emits nothing else), so
    a full path parser would be dead code pretending to be generality.
    """
    outer = re.search(r'<g transform="translate\(([-\d.]+),([-\d.]+)\)"', svg_text)
    ox, oy = (float(outer.group(1)), float(outer.group(2))) if outer else (0.0, 0.0)
    out = []
    for m in re.finditer(r"<(path|rect)\b([^>]*)>", svg_text):
        tag, attrs = m.group(1), m.group(2)
        fill = re.search(r'fill="(#[0-9A-Fa-f]{6})"', attrs)
        if not fill:
            continue
        rgb = _hex2rgb(fill.group(1))
        tm = re.search(r'transform="translate\(([-\d.]+),([-\d.]+)\)"', attrs)
        tx, ty = (float(tm.group(1)), float(tm.group(2))) if tm else (0.0, 0.0)
        if tag == "rect":
            x, y = float(re.search(r'x="([-\d.]+)"', attrs).group(1)), \
                   float(re.search(r'y="([-\d.]+)"', attrs).group(1))
            w, h = float(re.search(r'width="([-\d.]+)"', attrs).group(1)), \
                   float(re.search(r'height="([-\d.]+)"', attrs).group(1))
            out.append(([(x, y), (x + w, y), (x + w, y + h), (x, y + h)], rgb))
            continue                                     # the plate is not inside the logo's <g>
        d = re.search(r'd="([^"]*)"', attrs)
        if not d:
            continue
        nums = [float(v) for v in _NUM.findall(d.group(1))]
        pts = [(nums[i] + tx + ox, nums[i + 1] + ty + oy) for i in range(0, len(nums) - 1, 2)]
        if len(pts) >= 3:
            out.append((pts, rgb))
    return out


def _raster(polys, size, ss=3):
    """Scanline fill at `ss`x resolution, box-filtered down. `ss` is the anti-aliasing: at 1 the
    low-poly edges alias badly enough to look like a different logo at 48px."""
    n = size * ss
    buf = bytearray(n * n * 3)
    for pts, (r, g, b) in polys:
        ys = [p[1] for p in pts]
        y0 = max(0, int(min(ys) * ss * n / (590 * ss)))
        y1 = min(n - 1, int(max(ys) * ss * n / (590 * ss)) + 1)
        k = n / 590.0
        for y in range(y0, y1 + 1):
            yc = (y + 0.5) / k
            xs = []
            for i in range(len(pts)):
                (x1, ya), (x2, yb) = pts[i], pts[(i + 1) % len(pts)]
                if (ya <= yc < yb) or (yb <= yc < ya):
                    xs.append(x1 + (yc - ya) * (x2 - x1) / (yb - ya))
            xs.sort()
            row = y * n * 3
            for i in range(0, len(xs) - 1, 2):           # even-odd, as SVG's default fill-rule
                a, z = int(xs[i] * k + 0.5), int(xs[i + 1] * k + 0.5)
                for x in range(max(0, a), min(n, z)):
                    o = row + x * 3
                    buf[o] = r; buf[o + 1] = g; buf[o + 2] = b
    if ss == 1:
        return buf
    small = bytearray(size * size * 3)
    m = ss * ss
    for y in range(size):
        for x in range(size):
            r = g = b = 0
            for dy in range(ss):
                base = ((y * ss + dy) * n + x * ss) * 3
                for dx in range(ss):
                    o = base + dx * 3
                    r += buf[o]; g += buf[o + 1]; b += buf[o + 2]
            o = (y * size + x) * 3
            small[o] = r // m; small[o + 1] = g // m; small[o + 2] = b // m
    return small


def _png(rgb: bytes, size: int) -> bytes:
    """A minimal PNG. zlib and struct are the whole dependency list."""
    raw = b"".join(b"\x00" + bytes(rgb[y * size * 3:(y + 1) * size * 3]) for y in range(size))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def png(name: str, size: int = 512, source: str = "") -> bytes:
    """This dog's avatar as a PNG. 512 is Slack's recommended app-icon size."""
    return _png(_raster(_polygons(svg(name, source)), size), size)


def write(name: str, directory: str = "", size: int = 512) -> str:
    """Write <dir>/<name>.png and .svg; returns the PNG path."""
    d = directory or os.path.join(os.path.expanduser("~"), ".collie", "avatars")
    os.makedirs(d, exist_ok=True)
    stem = os.path.join(d, re.sub(r"[^A-Za-z0-9_-]+", "", name.lower()) or "collie")
    with open(stem + ".png", "wb") as f:
        f.write(png(name, size))
    with open(stem + ".svg", "w", encoding="utf-8") as f:
        f.write(svg(name))
    return stem + ".png"
