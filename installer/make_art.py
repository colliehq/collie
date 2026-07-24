"""Generate the installer's wizard artwork from collie's own logo — reproducibly.

Inno Setup wants plain 24-bit BMPs at fixed logical sizes, and it picks the closest one for the
user's DPI. Rather than committing a pile of opaque binaries, this script draws them: a deep-space
gradient with collie's live star-map motif (the same identity as the wallpaper), the logo mark, and
the wordmark. Deterministic (fixed seed), so a rebuild produces byte-identical art.

    python installer/make_art.py        ->  installer/art/*.bmp

Sizes are the ones Inno documents for WizardImageFile / WizardSmallImageFile scaling.
"""
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "art")
LOGO = os.path.join(HERE, os.pardir, "harness", "browser_ext", "icon128.png")

# Inno's documented scaling ladders. The base (first) size is what 100% DPI gets.
BIG = [(164, 314), (192, 386), (256, 492), (328, 628), (355, 700), (410, 797)]
SMALL = [(55, 58), (64, 68), (92, 97), (110, 116), (119, 123), (138, 140)]

TOP = (10, 12, 20)          # deep space at the top
BOTTOM = (23, 27, 40)       # slightly lifted at the bottom
GLOW = (61, 78, 143)        # collie's accent, used as the nebula behind the mark
ACCENT = (143, 160, 224)    # dark-theme accent — constellation lines, hairline
MUTED = (138, 144, 160)

FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")


def font(name, size):
    """Segoe UI when present (Windows), else whatever PIL can find. Art degrades, build doesn't."""
    for cand in (os.path.join(FONTS, name), name):
        try:
            return ImageFont.truetype(cand, size)
        except Exception:
            pass
    return ImageFont.load_default()


def tracked(draw, xy, text, fnt, fill, track=0.0, center_w=None):
    """Draw text with letter-spacing (PIL has none). Returns the drawn width."""
    x, y = xy
    widths = [draw.textlength(ch, font=fnt) + track for ch in text]
    total = sum(widths) - (track if widths else 0)
    if center_w is not None:
        x = (center_w - total) / 2.0
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=fnt, fill=fill)
        x += w
    return total


def starfield(w, h):
    """Gradient + nebula + stars + constellation, rendered at 2x and downsampled (cheap AA)."""
    W, H = w * 2, h * 2
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        t = y / float(H - 1)
        # ease the gradient so the top stays inky and the lift happens low on the panel
        e = t * t * (3 - 2 * t)
        row = tuple(int(TOP[i] + (BOTTOM[i] - TOP[i]) * e) for i in range(3))
        for x in range(W):
            px[x, y] = row

    # nebula: a soft radial bloom behind where the mark sits
    neb = Image.new("L", (W, H), 0)
    nd = ImageDraw.Draw(neb)
    cx, cy, r = W * 0.5, H * 0.30, W * 0.78
    for i in range(26):
        k = 1 - i / 26.0
        nd.ellipse([cx - r * k, cy - r * k, cx + r * k, cy + r * k], fill=int(4 + 5 * (1 - k)))
    neb = neb.filter(ImageFilter.GaussianBlur(W * 0.06))
    img = Image.composite(Image.new("RGB", (W, H), GLOW), img, neb)

    rnd = random.Random(20260723)
    d = ImageDraw.Draw(img)
    pts = []
    for _ in range(230):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        mag = rnd.random() ** 2.4                      # mostly faint, a few bright
        rad = 0.7 + mag * 2.6
        v = int(70 + 185 * mag)
        d.ellipse([x - rad, y - rad, x + rad, y + rad], fill=(v, v, min(255, int(v * 1.06))))
        if mag > 0.55:
            pts.append((x, y, mag))

    # a few of the brightest stars get a halo and get wired into a constellation — the star map
    for x, y, mag in pts:
        if mag > 0.86:
            g = Image.new("L", (W, H), 0)
            ImageDraw.Draw(g).ellipse([x - 9, y - 9, x + 9, y + 9], fill=90)
            img = Image.composite(Image.new("RGB", (W, H), (200, 210, 245)),
                                  img, g.filter(ImageFilter.GaussianBlur(5)))
    pts.sort(key=lambda p: p[1])
    lines = Image.new("RGB", (W, H), (0, 0, 0))
    ld = ImageDraw.Draw(lines)
    chain = [p for p in pts if p[2] > 0.62][:14]
    for a, b in zip(chain, chain[1:]):
        if math.hypot(a[0] - b[0], a[1] - b[1]) < W * 1.15:
            ld.line([a[0], a[1], b[0], b[1]], fill=ACCENT, width=2)
    img = Image.blend(img, Image.blend(img, lines, 0.0), 0.0)
    img = Image.composite(Image.new("RGB", (W, H), ACCENT), img,
                          lines.convert("L").point(lambda v: min(46, v)))
    return img.resize((w, h), Image.LANCZOS)


def wizard(w, h, logo):
    """The tall welcome/finish panel: star map, mark, wordmark, tagline."""
    img = starfield(w, h)
    d = ImageDraw.Draw(img)
    s = w / 164.0                                       # everything scales off the base width

    # mark, with a soft halo so it reads against the star field
    size = int(w * 0.36)
    mark = logo.resize((size, size), Image.LANCZOS)
    mx, my = (w - size) // 2, int(h * 0.20)
    halo = Image.new("L", (w, h), 0)
    hr = size * 0.86
    hd = ImageDraw.Draw(halo)
    for i in range(18):
        k = 1 - i / 18.0
        hd.ellipse([mx + size / 2 - hr * k, my + size / 2 - hr * k,
                    mx + size / 2 + hr * k, my + size / 2 + hr * k], fill=int(3 + 7 * (1 - k)))
    img = Image.composite(Image.new("RGB", (w, h), (150, 165, 225)), img,
                          halo.filter(ImageFilter.GaussianBlur(size * 0.16)))
    img.paste(mark, (mx, my), mark)
    d = ImageDraw.Draw(img)

    y = my + size + int(14 * s)
    f_word = font("seguisb.ttf", max(11, int(25 * s)))
    tracked(d, (0, y), "Collie", f_word, (245, 247, 252), track=1.2 * s, center_w=w)
    y += int(34 * s)
    f_tag = font("segoeui.ttf", max(6, int(7.0 * s)))
    tracked(d, (0, y), "MEMORY-FIRST CODING AGENT", f_tag, MUTED, track=1.05 * s, center_w=w)

    # hairline + footer, bottom-anchored so it survives every aspect ratio in the ladder
    d.line([int(w * 0.22), h - int(40 * s), int(w * 0.78), h - int(40 * s)],
           fill=(int(ACCENT[0] * .42), int(ACCENT[1] * .42), int(ACCENT[2] * .42)))
    f_foot = font("segoeui.ttf", max(6, int(6.6 * s)))
    tracked(d, (0, h - int(31 * s)), "OPEN SOURCE  ·  RUNS LOCALLY", f_foot,
            (105, 112, 130), track=0.7 * s, center_w=w)
    return img


def small(w, h, logo):
    """Top-right badge on the inner pages. Those pages are white, so this one is too."""
    S = 4
    img = Image.new("RGB", (w * S, h * S), (255, 255, 255))
    size = int(min(w, h) * 0.92) * S
    mark = logo.resize((size, size), Image.LANCZOS)
    img.paste(mark, ((w * S - size) // 2, (h * S - size) // 2), mark)
    return img.resize((w, h), Image.LANCZOS)


def main():
    os.makedirs(OUT, exist_ok=True)
    logo = Image.open(LOGO).convert("RGBA")
    made = []
    for w, h in BIG:
        p = os.path.join(OUT, "wizard-%dx%d.bmp" % (w, h))
        wizard(w, h, logo).save(p)
        made.append(p)
    for w, h in SMALL:
        p = os.path.join(OUT, "wizard-small-%dx%d.bmp" % (w, h))
        small(w, h, logo).save(p)
        made.append(p)
    for p in made:
        print("  %-44s %6.1f KB" % (os.path.basename(p), os.path.getsize(p) / 1024.0))


if __name__ == "__main__":
    main()
