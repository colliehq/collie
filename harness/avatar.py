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
ROLES = {
    "eye":        ("#04030E",),
    "catchlight": ("#ECEAEB", "#ECEBEC"),
    "blaze":      ("#FCFCFB",),                          # the white stripe and ruff: never recoloured
    "dark":       ("#0F0E19", "#020206", "#030206", "#2E2E3E",
                   "#2E2E3D", "#1D1B27", "#070510", "#05040D"),
    "mid":        ("#373746", "#5D5F6E", "#70717C", "#9998A2"),
    "light":      ("#B2B1BB", "#C7C6CD", "#C5C4CB"),
}
_ROLE_OF = {c: r for r, cs in ROLES.items() for c in cs}

# Eye colours a border collie actually has, saturated hard on purpose: at 36px an eye is a few
# pixels, and the muted versions of these read as "dark" like everything else around them.
EYES = (
    ("amber",  "#FFA31A"), ("ice",    "#7FD4F5"), ("moss",   "#7FD154"),
    ("copper", "#FF6B2C"), ("gold",   "#FFD22E"), ("violet", "#B98CFF"),
    ("blue",   "#4FA8FF"), ("rose",   "#FF7BA8"),
)

# The plate behind the head. This is the only element with enough area to survive the member list,
# so it does the work the coat cannot. Kept dark and low-chroma so a row of them still reads as
# Slack chrome rather than a row of stickers.
PLATES = (
    ("ink",    "#171622"), ("pine",   "#132420"), ("wine",   "#241520"),
    ("navy",   "#131C2C"), ("moor",   "#1E2016"), ("plum",   "#1F1526"),
    ("clay",   "#261C15"), ("steel",  "#171D22"),
)

# Coat families. Kept because it reads on the profile card at 96px, and because a red collie beside
# a blue one is simply nicer — but it is NOT load-bearing, so the range stays believable.
COATS = (
    ("black", None, 1.00), ("blue", 215, 0.55), ("red", 18, 0.50), ("chocolate", 25, 0.42),
    ("slate", 205, 0.30), ("sable", 35, 0.38), ("merle", 250, 0.30), ("gold", 42, 0.45),
)


def _hex2rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _rgb2hex(r, g, b):
    return "#%02X%02X%02X" % (max(0, min(255, r)), max(0, min(255, g)), max(0, min(255, b)))


def _tint(hex_colour, hue_deg, sat_mul, dl=0.0):
    """Push a colour toward a hue, KEEPING ITS LIGHTNESS.

    Lightness carries the low-poly shading — the drawing reads as a face because each facet sits at
    a particular brightness relative to its neighbours. Recolour in HLS, put L back, and a red
    collie is the same drawing in another coat rather than a smear.
    """
    r, g, b = (v / 255 for v in _hex2rgb(hex_colour))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    if hue_deg is not None:
        h = hue_deg / 360.0
        s = min(1.0, max(s, 0.10) * 3.0 * sat_mul)       # the source greys are nearly neutral
    l = min(1.0, max(0.0, l + dl))
    return _rgb2hex(*(round(v * 255) for v in colorsys.hls_to_rgb(h, l, s)))


def traits(name: str) -> dict:
    """The face of a name. Same name in, same face out — on any machine, forever."""
    d = hashlib.sha256(name.strip().lower().encode("utf-8")).digest()
    eye, plate, coat = EYES[d[0] % 8], PLATES[d[1] % 8], COATS[d[2] % 8]
    return {"name": name, "eye": eye[0], "eye_hex": eye[1],
            "plate": plate[0], "plate_hex": plate[1],
            "coat": coat[0], "_hue": coat[1], "_sat": coat[2],
            "shade": (d[3] % 5 - 2) * 0.035}


def svg(name: str, source: str = "") -> str:
    """The logo recoloured for one dog, on its plate. Geometry is never touched."""
    t = traits(name)
    src = source or open(LOGO, encoding="utf-8").read()

    def swap(m):
        role = _ROLE_OF.get(m.group(1).upper())
        if role == "eye":
            return 'fill="%s"' % t["eye_hex"]
        if role in ("catchlight", "blaze", None):
            return m.group(0)                            # the white face is the face; leave it
        return 'fill="%s"' % _tint(m.group(1).upper(), t["_hue"], t["_sat"], t["shade"])

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
