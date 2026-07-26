"""harness.qr — the stdlib QR encoder behind `collie web --lan`'s pairing code.

A phone camera is the only consumer, so "it decoded once on my Mac" is a weak proof: Reed–Solomon
will silently repair up to ~15% of a wrongly-encoded symbol at level M, hiding real bugs. So this
suite READS THE MATRIX BACK the hard way — un-mask, un-place the zigzag, de-interleave, then check
every block's RS syndromes are exactly zero (zero syndromes = no errors needed, not merely
correctable) and that the payload bytes round-trip.

An extra end-to-end check runs the finished PNG through macOS Vision when available (a genuinely
independent decoder); it is skipped elsewhere rather than faked.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import qr                                            # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


# ---------------------------------------------------------------- a decoder, written independently

def _function_map(size, version):
    """True where a module is NOT data: finders + separators, timing, alignment, format, dark module."""
    reserved = [[False] * size for _ in range(size)]

    def block(r0, c0, r1, c1):
        for r in range(max(0, r0), min(size, r1 + 1)):
            for c in range(max(0, c0), min(size, c1 + 1)):
                reserved[r][c] = True

    block(0, 0, 8, 8)                                  # top-left finder + separator + format
    block(0, size - 8, 8, size - 1)                    # top-right finder + format
    block(size - 8, 0, size - 1, 8)                    # bottom-left finder + format
    for i in range(size):
        reserved[6][i] = True                          # timing
        reserved[i][6] = True
    if version >= 2:
        center = 4 * version + 10
        block(center - 2, center - 2, center + 2, center + 2)
    return reserved


def _unmask(matrix, reserved, mask):
    out = [row[:] for row in matrix]
    for r in range(len(matrix)):
        for c in range(len(matrix)):
            if not reserved[r][c] and qr._MASKS[mask](r, c):
                out[r][c] ^= 1
    return out


def _read_mask(matrix):
    """The 5 format bits sit at (8,0..5) + (8,7); level M is 00, then 3 mask bits — recovered by
    matching the full 15-bit string against the table instead of trusting one copy."""
    size = len(matrix)
    coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
              (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    bits = 0
    for r, c in coords:
        bits = (bits << 1) | matrix[r][c]
    for mask, expected in enumerate(qr._FORMAT_M):
        if bits == expected:
            return mask
    return None


def _read_codewords(matrix, reserved):
    """Walk the same zigzag the encoder used and collect the bits back into bytes."""
    size = len(matrix)
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if not reserved[row][c]:
                    bits.append(matrix[row][c])
        col -= 2
        upward = not upward
    return [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits) // 8 * 8, 8)]


def _syndromes_zero(block, ec_count):
    """Evaluate the received polynomial at a^0..a^(ec-1). All zero => a valid, undamaged codeword."""
    for i in range(ec_count):
        acc = 0
        for coef in block:                              # Horner in GF(256)
            acc = qr._mul(acc, qr._EXP[i]) ^ coef
        if acc != 0:
            return False
    return True


def decode(matrix):
    """(payload, mask, version, all_syndromes_zero) read back out of a finished matrix."""
    size = len(matrix)
    version = (size - 17) // 4
    mask = _read_mask(matrix)
    assert mask is not None, "format bits did not match any level-M entry"
    reserved = _function_map(size, version)
    plain = _unmask(matrix, reserved, mask)
    interleaved = _read_codewords(plain, reserved)

    data_words, ec_per_block, blocks = qr._SPEC_M[version]
    per = data_words // blocks
    data = [[0] * per for _ in range(blocks)]
    idx = 0
    for i in range(per):
        for b in range(blocks):
            data[b][i] = interleaved[idx]
            idx += 1
    ec = [[0] * ec_per_block for _ in range(blocks)]
    for i in range(ec_per_block):
        for b in range(blocks):
            ec[b][i] = interleaved[idx]
            idx += 1

    clean = all(_syndromes_zero(data[b] + ec[b], ec_per_block) for b in range(blocks))
    words = [w for block in data for w in block]

    # byte mode: 4-bit mode + 8-bit length, then the payload
    bits = "".join(format(w, "08b") for w in words)
    mode = int(bits[:4], 2)
    length = int(bits[4:12], 2)
    payload = bytes(int(bits[12 + i * 8:20 + i * 8], 2) for i in range(length))
    return payload.decode("utf-8"), mask, version, clean, mode


# ---------------------------------------------------------------- tests

CASES = [
    "hi",
    "collie",
    "http://127.0.0.1:8787/?token=0123456789abcdef0123456789abcdef",
    "http://192.168.0.4:8787/?token=f7c2eaa181c3592c828d1f4485a3c10e",
    "http://10.0.0.42:9000/?token=" + "ab" * 16,
    "x" * 106,                            # the exact ceiling: v6-M's 108 codewords minus mode+length
]


def test_roundtrip_and_syndromes():
    for text in CASES:
        matrix = qr.encode(text)
        got, mask, version, clean, mode = decode(matrix)
        check(got == text, "round-trips %d bytes (v%d, mask %d)" % (len(text), version, mask))
        check(mode == 4, "byte mode indicator for %r" % text[:20])
        check(clean, "every RS block has ZERO syndromes (not merely correctable): %r" % text[:20])


def test_structure():
    matrix = qr.encode(CASES[3])
    size = len(matrix)
    check(size == 37, "the 63-byte pairing URL lands in version 5 (37x37)")
    for (r, c) in ((0, 0), (0, size - 7), (size - 7, 0)):
        ok = all(matrix[r + i][c + j] == (1 if i in (0, 6) or j in (0, 6)
                                         or (2 <= i <= 4 and 2 <= j <= 4) else 0)
                 for i in range(7) for j in range(7))
        check(ok, "finder pattern intact at (%d,%d)" % (r, c))
    check(all(matrix[6][i] == (1 if i % 2 == 0 else 0) for i in range(8, size - 8)),
          "horizontal timing pattern alternates")
    check(matrix[size - 8][8] == 1, "the always-dark module is dark")
    check(len(qr.encode("hi")) == 21, "a short payload stays version 1 (21x21)")


def test_capacity_ceiling():
    check(len(qr.encode("x" * 106)) == 41, "106 bytes still encodes (version 6, 41x41)")
    try:
        qr.encode("x" * 107)
        check(False, "107 bytes must be refused (v6-M carries 108 codewords incl. mode+length)")
    except ValueError:
        check(True, "107 bytes raises ValueError instead of emitting a corrupt symbol")


def test_ansi_renderer_matches_the_matrix():
    """The half-block renderer is what a camera actually sees — reconstruct the modules from it."""
    text = CASES[3]
    matrix = qr.encode(text)
    quiet = 2
    lines = [ln.replace("\x1b[30;47m", "").replace("\x1b[0m", "") for ln in qr.ansi(text).split("\n")]
    rebuilt = []
    for line in lines:
        top, bottom = [], []
        for ch in line:
            top.append(1 if ch in ("█", "▀") else 0)
            bottom.append(1 if ch in ("█", "▄") else 0)
        rebuilt.append(top)
        rebuilt.append(bottom)
    inner = [row[quiet:quiet + len(matrix)] for row in rebuilt[quiet:quiet + len(matrix)]]
    check(inner == matrix, "the ANSI half-block rendering reproduces the matrix exactly")
    check(all(all(v == 0 for v in row) for row in rebuilt[:quiet]),
          "a quiet zone is printed (scanners need it)")


def test_vision_decodes_the_png():
    """End-to-end through an independent decoder — macOS only, skipped elsewhere."""
    if sys.platform != "darwin":
        print("  SKIP macOS Vision round-trip (not darwin)")
        return
    swift = subprocess.run(["xcrun", "--find", "swift"], capture_output=True, text=True)
    if swift.returncode != 0:
        print("  SKIP macOS Vision round-trip (no swift toolchain)")
        return
    import struct
    import tempfile
    import zlib

    text = CASES[3]
    matrix = qr.encode(text)
    scale, quiet = 8, 4
    side = (len(matrix) + quiet * 2) * scale
    raw = b""
    for y in range(side):
        my = y // scale - quiet
        row = bytearray([0])
        for x in range(side):
            mx = x // scale - quiet
            dark = 0 <= my < len(matrix) and 0 <= mx < len(matrix) and matrix[my][mx]
            row.append(0 if dark else 255)
        raw += bytes(row)

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    tmp = tempfile.mkdtemp()
    png = os.path.join(tmp, "pair.png")
    with open(png, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", side, side, 8, 0, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(raw, 9)))
        f.write(chunk(b"IEND", b""))

    script = os.path.join(tmp, "decode.swift")
    with open(script, "w") as f:
        f.write('''import Foundation
import Vision
import AppKit
let path = CommandLine.arguments[1]
guard let image = NSImage(contentsOfFile: path), let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff), let cg = bitmap.cgImage else { print("LOAD-FAIL"); exit(1) }
let request = VNDetectBarcodesRequest(); request.symbologies = [.qr]
try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request])
print((request.results ?? []).compactMap { $0.payloadStringValue }.first ?? "NO-CODE")
''')
    env = dict(os.environ)
    env["DEVELOPER_DIR"] = env.get("DEVELOPER_DIR", "/Applications/Xcode.app/Contents/Developer")
    for var in ("LD", "CC", "CXX", "CFLAGS", "CXXFLAGS", "CPPFLAGS", "LDFLAGS"):
        env.pop(var, None)                    # a conda env's toolchain vars break swiftc
    try:
        out = subprocess.run(["xcrun", "swift", script, png], capture_output=True, text=True,
                             timeout=300, env=env)
    except subprocess.TimeoutExpired:
        print("  SKIP macOS Vision round-trip (swift timed out)")
        return
    decoded = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else ""
    if not decoded or decoded in ("LOAD-FAIL", "NO-CODE") and out.returncode != 0:
        print("  SKIP macOS Vision round-trip (toolchain unavailable: %s)" % (out.stderr or "")[:80])
        return
    check(decoded == text, "macOS Vision decodes the rendered PNG back to the pairing URL")


def main():
    test_roundtrip_and_syndromes()
    test_structure()
    test_capacity_ceiling()
    test_ansi_renderer_matches_the_matrix()
    test_vision_decodes_the_png()
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
