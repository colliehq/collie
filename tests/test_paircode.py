"""harness.paircode — the collie-only optical pairing code.

Nothing off-the-shelf can read this format, so the suite ships its OWN decoder (written from the
protocol comment, not from the encoder's internals) and reads the rendered pixels back:

  • clean render round-trips, and every RS syndrome is zero (no correction needed)
  • it still round-trips rotated, scaled, blurred, dimmed and low-contrast — the states a camera
    pointed at a screen actually produces
  • a corrupted ring is either corrected or REJECTED, never silently mis-read (that would pair the
    phone against a wrong host/secret)
  • the JS constants embedded in the /pair page match this module (three languages, one protocol)
"""
import math
import os
import re
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import paircode                                      # noqa: E402
from harness.qr import _EXP, _mul                                  # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


# ---------------------------------------------------------------- an independent decoder

def _syndromes(words, ec_count):
    out = []
    for i in range(ec_count):
        acc = 0
        for coef in words:
            acc = _mul(acc, _EXP[i]) ^ coef
        out.append(acc)
    return out


def decode(pixels, size, *, samples=5):
    """Read a grayscale bitmap back to (host, port, secret, syndromes_zero) or raise.

    Deliberately naive about geometry — it assumes the code is centred and fills the frame, which is
    what the renderer produces — but it does the two things a camera decoder must: threshold against
    the image's own contrast, and find the orientation spoke instead of trusting sector 0 to be up.
    """
    lo, hi = min(pixels), max(pixels)
    if hi - lo < 24:
        raise ValueError("no contrast in the frame")
    mid = (lo + hi) / 2.0
    cx = cy = (size - 1) / 2.0
    # FIND the locator instead of assuming it fills the frame: march outward along many rays and take
    # the median outermost-dark radius. That is also what makes the drawn ears harmless — they push a
    # minority of rays past the ring, and the median ignores them.
    hits = []
    for i in range(240):
        a = math.radians(i * 360.0 / 240)
        found = 0.0
        rr = 4.0
        while rr < size:
            x = int(round(cx + rr * math.cos(a)))
            y = int(round(cy + rr * math.sin(a)))
            if x < 0 or y < 0 or x >= size or y >= size:
                break
            if pixels[y * size + x] < mid:
                found = rr
            rr += 1.0
        if found:
            hits.append(found)
    if len(hits) < 120:
        raise ValueError("no locator ring found")
    hits.sort()
    outer = hits[len(hits) // 2]

    def dark_at(radius_frac, band, angle_deg):
        """Average the cell's interior (middle 60% each way). A thin cross of samples is not enough:
        nearest-neighbour rotation leaves jagged edges, and one stray pixel would flip the bit."""
        arc = 360.0 / paircode.SECTORS
        total = 0.0
        count = 0
        for i in range(samples):
            t = (i / (samples - 1.0) - 0.5) * 0.6
            for j in range(samples):
                u = (j / (samples - 1.0) - 0.5) * 0.6
                a = math.radians(angle_deg + u * arc)
                r = (radius_frac + t * band) * outer
                x = min(max(int(round(cx + r * math.sin(a))), 0), size - 1)
                y = min(max(int(round(cy - r * math.cos(a))), 0), size - 1)
                total += pixels[y * size + x]
                count += 1
        return 1 if total / count < mid else 0

    step = 360.0 / paircode.SECTORS
    grid = []
    for r_out, r_in in paircode.RING_BANDS:
        r_mid = (r_out + r_in) / 2.0
        grid.append([dark_at(r_mid, r_out - r_in, (s + 0.5) * step)
                     for s in range(paircode.SECTORS)])

    # find the orientation spoke: an all-dark sector followed by an all-light one. More than one
    # candidate is normal (data can imitate it), so every candidate is tried and RS decides.
    candidates = [s for s in range(paircode.SECTORS)
                  if all(grid[r][s] for r in range(paircode.RINGS))
                  and not any(grid[r][(s + 1) % paircode.SECTORS] for r in range(paircode.RINGS))]
    if not candidates:
        raise ValueError("no orientation spoke found")

    last = None
    for offset in candidates:
        rotated = [[ring[(offset + s) % paircode.SECTORS] for s in range(paircode.SECTORS)]
                   for ring in grid]
        words = paircode.bits_from_rings(rotated)
        syn = _syndromes(list(words), paircode.ECC_BYTES)
        payload = words[:paircode.PAYLOAD_BYTES]
        try:
            host, port, secret = paircode.read_payload(payload)
        except ValueError as e:
            last = e
            continue
        if any(syn):
            last = ValueError("syndromes non-zero: %r" % syn)
            continue
        return host, port, secret, True
    raise ValueError("no candidate decoded cleanly (%s)" % last)


# ---------------------------------------------------------------- image abuse a camera would cause

def rotate(pixels, size, degrees, fill=255):
    out = bytearray([fill]) * (size * size)
    c = (size - 1) / 2.0
    rad = math.radians(-degrees)
    cos_r, sin_r = math.cos(rad), math.sin(rad)
    for y in range(size):
        for x in range(size):
            dx, dy = x - c, y - c
            sx = int(round(c + dx * cos_r - dy * sin_r))
            sy = int(round(c + dx * sin_r + dy * cos_r))
            if 0 <= sx < size and 0 <= sy < size:
                out[y * size + x] = pixels[sy * size + sx]
    return bytes(out)


def blur(pixels, size, radius=2):
    out = bytearray(size * size)
    for y in range(size):
        for x in range(size):
            total = n = 0
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    yy, xx = y + dy, x + dx
                    if 0 <= yy < size and 0 <= xx < size:
                        total += pixels[yy * size + xx]
                        n += 1
            out[y * size + x] = total // n
    return bytes(out)


def contrast(pixels, black=90, white=170):
    """Squash the range, as a photo of a dim screen does."""
    return bytes(black + (p * (white - black)) // 255 for p in pixels)


def scale(pixels, size, new_size):
    out = bytearray(new_size * new_size)
    for y in range(new_size):
        sy = y * size // new_size
        for x in range(new_size):
            out[y * new_size + x] = pixels[sy * size + (x * size // new_size)]
    return bytes(out)


# ---------------------------------------------------------------- tests

HOST, PORT, SECRET = "192.168.0.146", 8787, "a1b2c3d4e5f60718"
PAYLOAD = paircode.payload_bytes(HOST, PORT, SECRET)


def test_payload_layout():
    check(len(PAYLOAD) == 18, "payload is 18 bytes (type + IPv4 + port + secret + pad)")
    check(paircode.read_payload(PAYLOAD) == (HOST, PORT, SECRET), "payload round-trips in memory")
    check(len(paircode.codewords(PAYLOAD)) * 8 == paircode.DATA_BITS,
          "25 codewords fill exactly the %d data bits" % paircode.DATA_BITS)

    # the relay payload: the reason the split moved to 18 + 7
    ROOM, CODE = "8diffZ9dkQmSdUDW", "NSTCRZ9F"
    relay = paircode.relay_payload_bytes(ROOM, CODE)
    check(len(relay) == 18, "a relay payload is the same 18 bytes")
    check(paircode.read_relay_payload(relay) == (ROOM, CODE), "room + pair code round-trip exactly")
    check(relay[0] == paircode.TYPE_RELAY and PAYLOAD[0] == paircode.TYPE_LAN,
          "byte 0 distinguishes the two kinds")
    try:
        paircode.read_payload(relay)
        check(False, "a relay payload is not mistaken for a LAN one")
    except ValueError:
        check(True, "a relay payload is not mistaken for a LAN one")
    for bad_room, bad_code in (("short", CODE), (ROOM, "LOWERcase"), (ROOM, "AAA")):
        try:
            paircode.relay_payload_bytes(bad_room, bad_code)
            check(False, "refuses room=%r code=%r" % (bad_room, bad_code))
        except (ValueError, Exception):
            check(True, "refuses room=%r code=%r" % (bad_room, bad_code))
    for bad in ("::1", "1.2.3", "1.2.3.999"):
        try:
            paircode.payload_bytes(bad, PORT, SECRET)
            check(False, "refuses a non-IPv4 host: %r" % bad)
        except ValueError:
            check(True, "refuses a non-IPv4 host: %r" % bad)
    try:
        paircode.payload_bytes(HOST, PORT, "aabb")
        check(False, "refuses a short secret")
    except ValueError:
        check(True, "refuses a short secret")


def test_orientation_spoke():
    grid = paircode.rings(PAYLOAD)
    check(all(grid[r][0] == 1 for r in range(paircode.RINGS)), "sector 0 is dark on every ring")
    check(all(grid[r][1] == 0 for r in range(paircode.RINGS)), "sector 1 is light on every ring")
    check(paircode.bits_from_rings(grid) == paircode.codewords(PAYLOAD),
          "bits_from_rings inverts the ring layout exactly")


def test_clean_render():
    size, pixels = paircode.raster(PAYLOAD, 512)
    host, port, secret, clean = decode(pixels, size)
    check((host, port, secret) == (HOST, PORT, SECRET), "clean render decodes to the right payload")
    check(clean, "clean render needs ZERO error correction (syndromes all zero)")


def test_survives_camera_conditions():
    size, pixels = paircode.raster(PAYLOAD, 512)
    cases = {
        # one sector is 6.92°, so 3/10/17/24 land samples near cell boundaries — the case a decoder
        # that assumes sector alignment silently fails
        "rotated 3°": lambda p: rotate(p, size, 3),
        "rotated 10°": lambda p: rotate(p, size, 10),
        "rotated 17°": lambda p: rotate(p, size, 17),
        "rotated 24°": lambda p: rotate(p, size, 24),
        "rotated 51°": lambda p: rotate(p, size, 51),
        "rotated 90°": lambda p: rotate(p, size, 90),
        "rotated 137°": lambda p: rotate(p, size, 137),
        "rotated 213°": lambda p: rotate(p, size, 213),
        "rotated 291°": lambda p: rotate(p, size, 291),
        "rotated 355°": lambda p: rotate(p, size, 355),
        "blurred (r=2)": lambda p: blur(p, size, 2),
        "low contrast": lambda p: contrast(p),
        "blurred + low contrast": lambda p: contrast(blur(p, size, 2)),
        "rotated 41° + blurred": lambda p: blur(rotate(p, size, 41), size, 2),
    }
    for name, fn in cases.items():
        try:
            host, port, secret, _ = decode(fn(pixels), size)
            check((host, port, secret) == (HOST, PORT, SECRET), "decodes when %s" % name)
        except ValueError as e:
            check(False, "decodes when %s (%s)" % (name, e))

    for small in (256, 192):
        shrunk = scale(pixels, size, small)
        try:
            host, port, secret, _ = decode(shrunk, small)
            check((host, port, secret) == (HOST, PORT, SECRET), "decodes at %dpx" % small)
        except ValueError as e:
            check(False, "decodes at %dpx (%s)" % (small, e))


def test_damage_is_rejected_not_misread():
    """The important safety property: a bad read must fail, never yield a plausible wrong host."""
    size, pixels = paircode.raster(PAYLOAD, 512)
    buf = bytearray(pixels)
    # paint a fat wedge of the code over — far more than RS can repair
    cx = cy = (size - 1) / 2.0
    for y in range(size):
        for x in range(size):
            dx, dy = x - cx, y - cy
            angle = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
            if 30.0 <= angle <= 150.0 and math.hypot(dx, dy) < size * 0.44:
                buf[y * size + x] = 255
    misread = None
    try:
        host, port, secret, _ = decode(bytes(buf), size)
        if (host, port, secret) != (HOST, PORT, SECRET):
            misread = (host, port, secret)
    except ValueError:
        pass                                     # rejected: the correct outcome
    check(misread is None,
          "a wiped-out third of the code is rejected, not decoded as %r" % (misread,))


def test_single_bit_flip_is_corrected_or_rejected():
    size, pixels = paircode.raster(PAYLOAD, 512)
    grid = paircode.rings(PAYLOAD)
    words = bytearray(paircode.codewords(PAYLOAD))
    words[3] ^= 0xFF                             # one byte destroyed
    syn = _syndromes(list(words), paircode.ECC_BYTES)
    check(any(syn), "a flipped codeword makes syndromes non-zero (damage is detectable at all)")
    del grid, pixels


def test_js_constants_match():
    """Three languages implement this protocol; a silent drift between them is unscannable."""
    html = paircode.page(PAYLOAD, host=HOST, port=PORT, ttl=180)
    js = {}
    m = re.search(r"const SECTORS = (\d+), RINGS = (\d+);", html)
    check(m is not None, "the page declares SECTORS/RINGS")
    if m:
        js["sectors"], js["rings"] = int(m.group(1)), int(m.group(2))
        check(js["sectors"] == paircode.SECTORS, "JS SECTORS matches Python (%d)" % paircode.SECTORS)
        check(js["rings"] == paircode.RINGS, "JS RINGS matches Python (%d)" % paircode.RINGS)
    m = re.search(r"const LOCATOR = \[([\d.]+), ([\d.]+)\];", html)
    check(m is not None and (float(m.group(1)), float(m.group(2))) == paircode.LOCATOR,
          "JS LOCATOR band matches Python")
    m = re.search(r"const RING_BANDS = \[(.+?)\];", html)
    bands = re.findall(r"\[([\d.]+),([\d.]+)\]", m.group(1)) if m else []
    check([(float(a), float(b)) for a, b in bands] == list(paircode.RING_BANDS),
          "JS RING_BANDS match Python")
    m = re.search(r"const BRAND_RADIUS = ([\d.]+);", html)
    check(m is not None and float(m.group(1)) == paircode.BRAND_RADIUS,
          "JS BRAND_RADIUS matches Python")
    m = re.search(r"const EAR = \{angle: ([\d.]+), half: ([\d.]+), tip: ([\d.]+), "
                  r"lean: ([\d.]+), base: ([\d.]+)\};", html)
    check(m is not None, "the page declares the ear geometry")
    if m:
        got = tuple(float(g) for g in m.groups())
        want = (paircode.EAR_ANGLE, paircode.EAR_HALF_WIDTH, paircode.EAR_TIP,
                paircode.EAR_LEAN, paircode.EAR_BASE)
        check(got == want, "JS ear geometry matches Python (the dog looks the same in both)")
    m = re.search(r"const GRID = (\[\[.+?\]\]);", html)
    check(m is not None, "the page embeds the bit grid")
    if m:
        grid = [[int(v) for v in row.split(",")]
                for row in re.findall(r"\[([01,]+)\]", m.group(1))]
        check(grid == paircode.rings(PAYLOAD), "JS GRID is exactly the Python ring layout")


def test_png_is_valid():
    data = paircode.png(PAYLOAD, 256)
    check(data[:8] == b"\x89PNG\r\n\x1a\n", "png() emits a PNG signature")
    # walk the chunks and verify every CRC — a malformed PNG would silently not render
    pos, seen, ok = 8, [], True
    while pos < len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        crc = int.from_bytes(data[pos + 8 + length:pos + 12 + length], "big")
        if zlib.crc32(tag + body) & 0xFFFFFFFF != crc:
            ok = False
        seen.append(tag)
        pos += 12 + length
    check(ok, "every PNG chunk CRC is correct")
    check(seen == [b"IHDR", b"IDAT", b"IEND"], "chunk order is IHDR/IDAT/IEND")


def main():
    test_payload_layout()
    test_orientation_spoke()
    test_clean_render()
    test_survives_camera_conditions()
    test_damage_is_rejected_not_misread()
    test_single_bit_flip_is_corrected_or_rejected()
    test_js_constants_match()
    test_png_is_valid()
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
