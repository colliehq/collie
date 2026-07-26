"""The collie pair code — a bespoke optical code, readable only by something that knows this layout.

Why not a QR: a QR carrying `http://host:port/?token=…` is readable by every camera app on earth, so
the pairing secret leaks to anyone who points a phone at your screen (or at a screen share). This
code is a private channel between `collie web` and CollieIOS: no standard decoder recognises it, and
what it carries is a one-shot secret that is useless without also reaching the machine.

PROTOCOL v1 — keep these numbers in lockstep with the JS renderer below and the Swift decoder in
CollieIOS (`PairCode.swift`); `tests/test_paircode.py` asserts the JS copy matches this module.

  payload    15 bytes: [0] version=1 · [1:5] IPv4 · [5:7] port (big endian) · [7:15] secret
  ecc        Reed-Solomon over GF(256), 10 check bytes -> 25 bytes total = 200 bits
  geometry   normalised to the code's outer radius R = 1.0
               1.00 .. 0.90   locator: a solid dark annulus (what the decoder finds first)
               0.90 .. 0.86   quiet gap
               0.86 .. 0.78   data ring 0   (bit 0 is the most significant of byte 0)
               0.78 .. 0.70   data ring 1
               0.70 .. 0.62   data ring 2
               0.62 .. 0.54   data ring 3
               0.42 .. 0.00   brand disc (carries nothing; the decoder never samples here)
  sectors    52, clockwise from 12 o'clock. Sector 0 is dark on all four rings and sector 1 is light
             on all four: that pair is the orientation spoke. Sectors 2..51 carry the 200 data bits,
             ring-major (ring 0 sector 2, ring 1 sector 2, … — so one damaged ring loses 1 bit in 4
             rather than the first 50 bits in a row).
  dark = 1
"""
from __future__ import annotations

import struct
import zlib

from .qr import _ec_codewords

VERSION = 1
SECTORS = 52
RINGS = 4
DATA_SECTORS = SECTORS - 2                # sector 0 and 1 are the orientation spoke
DATA_BITS = RINGS * DATA_SECTORS          # 200
PAYLOAD_BYTES = 15
ECC_BYTES = 10                            # RS(25,15): corrects up to 5 byte errors
# radial band edges, outer -> inner, as fractions of the outer radius
LOCATOR = (1.00, 0.90)
GAP = (0.90, 0.86)
RING_BANDS = ((0.86, 0.78), (0.78, 0.70), (0.70, 0.62), (0.62, 0.54))
BRAND_RADIUS = 0.42
# Decoration, not protocol: two pointed ears above the locator make the symbol read as the collie
# head from the logo. They sit outside the locator on purpose — the decoder locates the ring by the
# median edge radius and discards outliers, so ears cannot be mistaken for it.
EAR_ANGLE = 41.0            # degrees either side of 12 o'clock, at the base
EAR_HALF_WIDTH = 14.0       # angular half-width at the base
EAR_TIP = 1.36              # tip radius, in units of the outer radius
EAR_LEAN = 11.0             # degrees the tip leans outward — what makes it a collie ear, not a cone
EAR_BASE = 0.93


def payload_bytes(host: str, port: int, secret: str) -> bytes:
    """version + IPv4 + port + 8-byte secret. Refuses anything that would not fit exactly."""
    octets = [int(p) for p in host.split(".")]
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        raise ValueError("pair code carries an IPv4 literal, got %r" % host)
    raw = bytes.fromhex(secret)
    if len(raw) != 8:
        raise ValueError("pairing secret must be 8 bytes, got %d" % len(raw))
    out = bytes([VERSION]) + bytes(octets) + struct.pack(">H", int(port)) + raw
    assert len(out) == PAYLOAD_BYTES
    return out


def read_payload(data: bytes):
    """The inverse: (host, port, secret_hex). Raises on a wrong version/length."""
    if len(data) != PAYLOAD_BYTES:
        raise ValueError("expected %d payload bytes, got %d" % (PAYLOAD_BYTES, len(data)))
    if data[0] != VERSION:
        raise ValueError("unsupported pair-code version %d" % data[0])
    host = ".".join(str(b) for b in data[1:5])
    port = struct.unpack(">H", data[5:7])[0]
    return host, port, data[7:15].hex()


def codewords(payload: bytes) -> bytes:
    """Payload + Reed-Solomon check bytes."""
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError("payload must be %d bytes" % PAYLOAD_BYTES)
    return bytes(payload) + bytes(_ec_codewords(list(payload), ECC_BYTES))


def rings(payload: bytes):
    """The drawn code as RINGS lists of SECTORS ints (1 = dark), including the orientation spoke."""
    words = codewords(payload)
    bits = [(w >> (7 - i)) & 1 for w in words for i in range(8)]
    assert len(bits) == DATA_BITS, "%d bits" % len(bits)

    grid = [[0] * SECTORS for _ in range(RINGS)]
    for ring in range(RINGS):
        grid[ring][0] = 1            # orientation spoke: all-dark sector…
        grid[ring][1] = 0            # …immediately followed by all-light
    i = 0
    for sector in range(2, SECTORS):
        for ring in range(RINGS):    # ring-major: spreads consecutive bits across radii
            grid[ring][sector] = bits[i]
            i += 1
    return grid


def bits_from_rings(grid):
    """Inverse of `rings` for the data area only — the decoder's final step, kept here so the layout
    lives in exactly one place."""
    bits = []
    for sector in range(2, SECTORS):
        for ring in range(RINGS):
            bits.append(grid[ring][sector])
    out = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for b in bits[i:i + 8]:
            byte = (byte << 1) | b
        out.append(byte)
    return bytes(out)


# ---------------------------------------------------------------- reference raster (tests, --png)

def _face(nx, ny):
    """The collie head drawn inside the brand disc, in units of the outer radius (y grows downward).

    Returns True where the pixel should be LIGHT. Everything here lives at radius < BRAND_RADIUS,
    which no decoder ever samples, so the face is free to be as detailed as it likes. The shapes
    follow the logo: a dark head, the white blaze down the middle, two eyes and a light muzzle.
    """
    def ellipse(cx, cy, rx, ry):
        return ((nx - cx) / rx) ** 2 + ((ny - cy) / ry) ** 2 <= 1.0

    if ellipse(0.0, 0.175, 0.150, 0.105):                # muzzle
        return not ellipse(0.0, 0.130, 0.058, 0.042)     # …with a dark nose in it
    if ellipse(-0.135, -0.055, 0.055, 0.070):            # eyes
        return True
    if ellipse(0.135, -0.055, 0.055, 0.070):
        return True
    # the blaze: a wedge, narrow at the crown and widening toward the muzzle, as in the logo
    if -0.42 < ny < 0.06:
        width = 0.022 + 0.055 * max(0.0, (ny + 0.42) / 0.48)
        if abs(nx) < width:
            return True
    return False


def _ear(nx, ny):
    """True inside either pointed ear. Ears sit OUTSIDE the locator ring (radius > 1), which is why
    the decoder's circle finder keeps only edge points near the median radius — otherwise an ear
    would be mistaken for the ring."""
    import math
    r = math.hypot(nx, ny)
    if r < EAR_BASE or r > EAR_TIP:
        return False
    angle = math.degrees(math.atan2(nx, -ny))            # 0 = 12 o'clock, clockwise
    t = (r - EAR_BASE) / (EAR_TIP - EAR_BASE)            # 0 at the base, 1 at the tip
    span = EAR_HALF_WIDTH * (1.0 - t) ** 0.75            # tapers to a point
    for side in (-1.0, 1.0):
        centre = side * (EAR_ANGLE + EAR_LEAN * t)       # the tip leans away from the muzzle
        offset = abs(angle - centre)
        if offset > 180:
            offset = 360 - offset
        if offset <= span:
            return True
    return False


def raster(payload: bytes, size: int = 512, background: int = 255, ink: int = 0):
    """An 8-bit grayscale bitmap of the code, as (size, bytes) — the reference rendering every
    decoder is tested against. Pure integer maths so it matches everywhere.

    The symbol is drawn as a collie head: the data rings are the ruff, two pointed ears rise above
    the locator, and the brand disc holds the face. Only the locator and the four rings carry meaning;
    the ears and face are decoration the decoder is built to ignore.
    """
    import math
    grid = rings(payload)
    cx = cy = (size - 1) / 2.0
    outer = size * 0.37                                  # room for the ears above the ring
    rows = bytearray()
    for y in range(size):
        for x in range(size):
            dx, dy = x - cx, y - cy
            nx, ny = dx / outer, dy / outer
            r = math.hypot(nx, ny)
            value = background
            if LOCATOR[1] <= r <= LOCATOR[0]:
                value = ink
            elif r <= BRAND_RADIUS:
                value = background if _face(nx, ny) else ink
            elif r > LOCATOR[0]:
                if _ear(nx, ny):
                    value = ink
            else:
                for ring, (r_out, r_in) in enumerate(RING_BANDS):
                    if r_in <= r <= r_out:
                        angle = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
                        sector = int(angle / (360.0 / SECTORS)) % SECTORS
                        value = ink if grid[ring][sector] else background
                        break
            rows.append(value)
    return size, bytes(rows)


def png(payload: bytes, size: int = 512) -> bytes:
    """The reference raster as a PNG (stdlib zlib only) — used by the test suite and `collie pair`."""
    side, pixels = raster(payload, size)
    raw = b"".join(b"\x00" + pixels[y * side:(y + 1) * side] for y in range(side))

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------- the /pair screen

_PAGE = """<!doctype html>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pair collie</title>
<style>
  :root { color-scheme: light dark; --bg:#EBEDF1; --ink:#1A1F2B; --muted:#5E6473; --pine:#3D4E8F; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0E0F14; --ink:#E7E9F0; --muted:#969CAD; --pine:#8AA0E8; }
  }
  html,body { margin:0; height:100%%; background:var(--bg); color:var(--ink);
              font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  main { min-height:100%%; display:flex; flex-direction:column; align-items:center;
         justify-content:center; gap:22px; padding:28px; box-sizing:border-box; text-align:center; }
  h1 { font-size:22px; margin:0; font-weight:650; }
  /* the logo is a near-black head on transparency, so on the dark theme it needs lifting */
  .logo { display:block; }
  @media (prefers-color-scheme: dark) { .logo { filter:invert(1) hue-rotate(180deg); } }
  p  { margin:0; color:var(--muted); max-width:34em; }
  .stage { position:relative; width:min(74vmin,560px); aspect-ratio:1; background:#FBFCFE;
           border-radius:24px; padding:14px; box-sizing:border-box; }
  canvas { width:100%%; height:100%%; display:block; }
  .mark { position:absolute; inset:0; margin:auto; width:26%%; height:26%%;
          display:flex; align-items:center; justify-content:center; pointer-events:none; }
  .mark img { width:100%%; height:100%%; }
  code { font:13px/1.5 ui-monospace,"SF Mono",Menlo,monospace; color:var(--ink); }
  .ttl { font:13px/1.5 ui-monospace,Menlo,monospace; color:var(--muted); }
  .dead { opacity:.25; }
  button { font:inherit; padding:10px 18px; border:0; border-radius:9px;
           background:var(--pine); color:#fff; cursor:pointer; }
</style>
<main>
  <img class="logo" src="/logo.svg" alt="collie" width="56" height="56">
  <h1>Scan with CollieIOS</h1>
  <p>Open the app, tap <b>Scan the pair code</b>, and point the camera at this screen. The code
     carries a one-time secret — never your token.</p>
  <div class="stage" id="stage">
    <canvas id="code" width="1024" height="1024"></canvas>
  </div>
  <div class="ttl" id="ttl"></div>
  <div><code>%(host)s:%(port)d</code></div>
  <button id="again" hidden>New code</button>
</main>
<script>
// PROTOCOL v1 — these constants MUST match harness/paircode.py (tests/test_paircode.py checks).
const SECTORS = %(sectors)d, RINGS = %(rings)d;
const LOCATOR = [%(loc_out).2f, %(loc_in).2f];
const RING_BANDS = %(bands)s;
const BRAND_RADIUS = %(brand).2f;
const EAR = {angle: %(ear_angle).1f, half: %(ear_half).1f, tip: %(ear_tip).2f, lean: %(ear_lean).1f, base: %(ear_base).2f};
const GRID = %(grid)s;            // GRID[ring][sector], 1 = dark
const TTL = %(ttl)d;

function draw() {
  const c = document.getElementById('code'), ctx = c.getContext('2d');
  const size = c.width, cx = size / 2, cy = size / 2, outer = size * 0.37;
  // Fixed polarity, NOT the page's theme colours: the code means "dark = 1", and rendering it
  // inverted on a dark background makes every bit read backwards.
  const dark = '#0F0E19', light = '#FBFCFE';
  ctx.fillStyle = light;
  ctx.fillRect(0, 0, size, size);
  // the locator annulus: one stroked circle, thickness = the band
  ctx.strokeStyle = dark;
  ctx.lineWidth = (LOCATOR[0] - LOCATOR[1]) * outer;
  ctx.beginPath();
  ctx.arc(cx, cy, (LOCATOR[0] + LOCATOR[1]) / 2 * outer, 0, Math.PI * 2);
  ctx.stroke();
  // data rings: one filled arc per dark sector
  const step = Math.PI * 2 / SECTORS;
  ctx.fillStyle = dark;
  for (let ring = 0; ring < RINGS; ring++) {
    const [rOut, rIn] = RING_BANDS[ring];
    for (let s = 0; s < SECTORS; s++) {
      if (!GRID[ring][s]) continue;
      // sector 0 starts at 12 o'clock and they run clockwise, matching the Python renderer
      const a0 = -Math.PI / 2 + s * step, a1 = a0 + step;
      ctx.beginPath();
      ctx.arc(cx, cy, rOut * outer, a0, a1);
      ctx.arc(cx, cy, rIn * outer, a1, a0, true);
      ctx.closePath();
      ctx.fill();
    }
  }
  drawEars(ctx, cx, cy, outer, dark);
  drawFace(ctx, cx, cy, outer, dark, light);
}

// Decoration only. The ears sit OUTSIDE the locator and the face INSIDE the brand disc, so neither
// touches a bit the decoder reads — see harness/paircode.py for why that placement is deliberate.
function polar(cx, cy, outer, r, deg) {
  const a = (deg - 90) * Math.PI / 180;       // 0 deg = 12 o'clock, clockwise
  return [cx + r * outer * Math.cos(a), cy + r * outer * Math.sin(a)];
}

function drawEars(ctx, cx, cy, outer, dark) {
  ctx.fillStyle = dark;
  for (const side of [-1, 1]) {
    const base = side * EAR.angle, tipAngle = side * (EAR.angle + EAR.lean);
    const p0 = polar(cx, cy, outer, EAR.base, base - EAR.half);
    const p1 = polar(cx, cy, outer, EAR.base, base + EAR.half);
    const tip = polar(cx, cy, outer, EAR.tip, tipAngle);
    const mid0 = polar(cx, cy, outer, EAR.base + (EAR.tip - EAR.base) * 0.55,
                       base - EAR.half * 0.45 + EAR.lean * 0.4 * side);
    const mid1 = polar(cx, cy, outer, EAR.base + (EAR.tip - EAR.base) * 0.55,
                       base + EAR.half * 0.45 + EAR.lean * 0.4 * side);
    ctx.beginPath();
    ctx.moveTo(p0[0], p0[1]);
    ctx.quadraticCurveTo(mid0[0], mid0[1], tip[0], tip[1]);
    ctx.quadraticCurveTo(mid1[0], mid1[1], p1[0], p1[1]);
    ctx.closePath();
    ctx.fill();
  }
}

function drawFace(ctx, cx, cy, outer, dark, light) {
  const u = (v) => v * outer;
  ctx.fillStyle = dark;                       // the head
  ctx.beginPath();
  ctx.arc(cx, cy, BRAND_RADIUS * outer, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = light;                      // the blaze: narrow at the crown, wide at the muzzle
  ctx.beginPath();
  ctx.moveTo(cx - u(0.022), cy - u(0.42));
  ctx.lineTo(cx + u(0.022), cy - u(0.42));
  ctx.lineTo(cx + u(0.077), cy + u(0.06));
  ctx.lineTo(cx - u(0.077), cy + u(0.06));
  ctx.closePath();
  ctx.fill();

  for (const dx of [-0.135, 0.135]) {         // eyes
    ctx.beginPath();
    ctx.ellipse(cx + u(dx), cy - u(0.055), u(0.055), u(0.070), 0, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.beginPath();                            // muzzle
  ctx.ellipse(cx, cy + u(0.175), u(0.150), u(0.105), 0, 0, Math.PI * 2);
  ctx.fill();

  ctx.fillStyle = dark;                       // nose
  ctx.beginPath();
  ctx.ellipse(cx, cy + u(0.130), u(0.058), u(0.042), 0, 0, Math.PI * 2);
  ctx.fill();
}

draw();

let left = TTL;
const ttl = document.getElementById('ttl'), again = document.getElementById('again');
function tick() {
  if (left <= 0) {
    ttl.textContent = 'this code has expired';
    document.getElementById('stage').classList.add('dead');
    again.hidden = false;
    return;
  }
  ttl.textContent = 'expires in ' + left + 's';
  left -= 1;
  setTimeout(tick, 1000);
}
tick();
again.addEventListener('click', () => location.reload());
</script>
"""


def page(payload: bytes, host: str, port: int, ttl: int) -> str:
    """The /pair screen. The bits are computed server-side; the JS only draws them."""
    grid = rings(payload)
    return _PAGE % {
        "host": host,
        "port": port,
        "sectors": SECTORS,
        "rings": RINGS,
        "ear_angle": EAR_ANGLE,
        "ear_half": EAR_HALF_WIDTH,
        "ear_tip": EAR_TIP,
        "ear_lean": EAR_LEAN,
        "ear_base": EAR_BASE,
        "loc_out": LOCATOR[0],
        "loc_in": LOCATOR[1],
        "bands": "[" + ",".join("[%.2f,%.2f]" % b for b in RING_BANDS) + "]",
        "brand": BRAND_RADIUS,
        "grid": "[" + ",".join("[" + ",".join(str(v) for v in row) + "]" for row in grid) + "]",
        "ttl": ttl,
    }
