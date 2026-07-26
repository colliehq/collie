"""QR encoder — stdlib only, just enough of ISO/IEC 18004 to print a pairing code in the terminal.

`collie web --lan` prints the pairing URL as a QR so the phone (CollieIOS) can be paired by pointing
a camera at the terminal instead of retyping a 32-hex token.

Deliberately a SUBSET, because the whole payload is one short URL:
  • byte mode only (a URL is not alphanumeric-clean: it has `:` `/` `?` `=`)
  • ECC level M (~15% recovery — the usual default for screens)
  • versions 1–6 only. That caps the payload at 108 bytes, and — the real reason — versions ≥ 7 are
    the ones that carry a separate version-info block, and v2–6 have exactly ONE alignment pattern.
    Skipping both removes the fiddliest tables in the spec.
Public API: `encode(text) -> list[list[int]]` (1 = dark) and `ansi(text)` for a terminal.
"""
from __future__ import annotations

# (data codewords, ec codewords per block, block count) per version at level M. Every one of these
# versions splits into EQUAL blocks, which is why the interleaver below needs no group1/group2 case.
_SPEC_M = {
    1: (16, 10, 1),
    2: (28, 16, 1),
    3: (44, 26, 1),
    4: (64, 18, 2),
    5: (86, 24, 2),
    6: (108, 16, 4),
}
# BCH(15,5)-encoded format strings for level M, mask 0..7 (spec Annex C), already XOR-masked with
# 0x5412. Precomputed: the generator arithmetic is not worth carrying for 8 constants.
_FORMAT_M = (0x5412, 0x5125, 0x5E7C, 0x5B4B, 0x45F9, 0x40CE, 0x4F97, 0x4AA0)

# GF(256) with the QR primitive polynomial 0x11D.
_EXP = [1] * 512
_LOG = [0] * 256
_x = 1
for _i in range(255):
    _EXP[_i] = _x
    _LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _mul(a, b):
    if a == 0 or b == 0:
        return 0
    return _EXP[_LOG[a] + _LOG[b]]


def _generator(degree):
    """Reed–Solomon generator polynomial (x-a^0)(x-a^1)…(x-a^(degree-1))."""
    poly = [1]
    for i in range(degree):
        poly = _poly_mul(poly, [1, _EXP[i]])
    return poly


def _poly_mul(p, q):
    out = [0] * (len(p) + len(q) - 1)
    for i, a in enumerate(p):
        for j, b in enumerate(q):
            out[i + j] ^= _mul(a, b)
    return out


def _ec_codewords(data, count):
    """The `count` Reed–Solomon check bytes for one data block (polynomial long division)."""
    gen = _generator(count)
    rem = list(data) + [0] * count
    for i in range(len(data)):
        coef = rem[i]
        if coef:
            for j, g in enumerate(gen):
                rem[i + j] ^= _mul(g, coef)
    return rem[len(data):]


def _pick_version(length):
    for version in sorted(_SPEC_M):
        if length + 2 <= _SPEC_M[version][0]:      # +2 = mode nibble + 8-bit length byte
            return version
    raise ValueError("payload of %d bytes exceeds the %d-byte limit of this encoder (versions 1–6, "
                     "level M)" % (length, _SPEC_M[max(_SPEC_M)][0] - 2))


def _bitstream(payload, version):
    """Mode indicator (0100 = byte) + 8-bit length + data + terminator + pad, to exact capacity."""
    data_words = _SPEC_M[version][0]
    bits = [0, 1, 0, 0]
    for i in range(7, -1, -1):
        bits.append((len(payload) >> i) & 1)
    for byte in payload:
        for i in range(7, -1, -1):
            bits.append((byte >> i) & 1)
    capacity = data_words * 8
    bits.extend([0] * min(4, capacity - len(bits)))          # terminator
    bits.extend([0] * (-len(bits) % 8))                      # to a byte boundary
    words = [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]
    for pad in _cycle_pad(data_words - len(words)):
        words.append(pad)
    return words


def _cycle_pad(n):
    return [0xEC if i % 2 == 0 else 0x11 for i in range(max(0, n))]


def _interleave(words, version):
    """Split into blocks, append each block's EC bytes, then interleave data-then-EC as the spec
    requires (a burst of damage must not wipe one block's data)."""
    data_words, ec_per_block, blocks = _SPEC_M[version]
    per = data_words // blocks
    data_blocks = [words[i * per:(i + 1) * per] for i in range(blocks)]
    ec_blocks = [_ec_codewords(b, ec_per_block) for b in data_blocks]
    out = []
    for i in range(per):
        for b in data_blocks:
            out.append(b[i])
    for i in range(ec_per_block):
        for b in ec_blocks:
            out.append(b[i])
    return out


def _blank(size):
    return [[None] * size for _ in range(size)]


def _place_function_patterns(m, version):
    size = len(m)

    def finder(row, col):
        for r in range(-1, 8):
            for c in range(-1, 8):
                rr, cc = row + r, col + c
                if not (0 <= rr < size and 0 <= cc < size):
                    continue
                if r in (-1, 7) or c in (-1, 7):
                    m[rr][cc] = 0                                   # separator
                elif r in (0, 6) or c in (0, 6) or (2 <= r <= 4 and 2 <= c <= 4):
                    m[rr][cc] = 1
                else:
                    m[rr][cc] = 0

    finder(0, 0)
    finder(0, size - 7)
    finder(size - 7, 0)

    for i in range(8, size - 8):                                    # timing patterns
        bit = 1 if i % 2 == 0 else 0
        m[6][i] = bit
        m[i][6] = bit

    if version >= 2:                                                # the single alignment pattern
        center = 4 * version + 10
        for r in range(-2, 3):
            for c in range(-2, 3):
                m[center + r][center + c] = 1 if max(abs(r), abs(c)) != 1 else 0

    m[size - 8][8] = 1                                              # the always-dark module

    for i in range(9):                                              # reserve the format areas
        if m[8][i] is None:
            m[8][i] = 0
        if m[i][8] is None:
            m[i][8] = 0
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = 0
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = 0


def _free(m, reserved, row, col):
    return not reserved[row][col]


def _place_data(m, reserved, words):
    """Zigzag two-module columns, right to left, skipping the vertical timing column."""
    size = len(m)
    bits = [(w >> i) & 1 for w in words for i in range(7, -1, -1)]
    idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:                                                # timing column is never data
            col -= 1
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in (col, col - 1):
                if _free(m, reserved, row, c):
                    m[row][c] = bits[idx] if idx < len(bits) else 0
                    idx += 1
        col -= 2
        upward = not upward


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _apply_mask(m, reserved, mask):
    out = [row[:] for row in m]
    for r in range(len(m)):
        for c in range(len(m)):
            if not reserved[r][c] and _MASKS[mask](r, c):
                out[r][c] ^= 1
    return out


def _place_format(m, mask):
    size = len(m)
    bits = [(_FORMAT_M[mask] >> i) & 1 for i in range(14, -1, -1)]
    # copy 1: around the top-left finder
    coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8),
              (7, 8), (5, 8), (4, 8), (3, 8), (2, 8), (1, 8), (0, 8)]
    for bit, (r, c) in zip(bits, coords):
        m[r][c] = bit
    # copy 2: bottom-left column then top-right row
    for i in range(7):
        m[size - 1 - i][8] = bits[i]
    for i in range(8):
        m[8][size - 8 + i] = bits[7 + i]


def _penalty(m):
    """Spec's four mask-selection penalties. Lower is better."""
    size = len(m)
    score = 0

    def runs(line):
        total = 0
        run, prev = 0, None
        for v in line:
            if v == prev:
                run += 1
            else:
                if run >= 5:
                    total += 3 + (run - 5)
                run, prev = 1, v
        if run >= 5:
            total += 3 + (run - 5)
        return total

    for i in range(size):                                           # rule 1: runs of 5+
        score += runs(m[i])
        score += runs([m[r][i] for r in range(size)])

    for r in range(size - 1):                                       # rule 2: 2x2 blocks
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3

    finder = [1, 0, 1, 1, 1, 0, 1, 0, 0, 0, 0]                      # rule 3: finder-like patterns
    rev = finder[::-1]
    for i in range(size):
        row = m[i]
        col = [m[r][i] for r in range(size)]
        for line in (row, col):
            for j in range(size - 10):
                window = line[j:j + 11]
                if window == finder or window == rev:
                    score += 40

    dark = sum(sum(row) for row in m)                                # rule 4: dark/light balance
    pct = dark * 100 // (size * size)
    score += 10 * (abs(pct - 50) // 5)
    return score


def encode(text: str):
    """The QR matrix for `text` as a list of rows of 0/1, no quiet zone. 1 = dark."""
    payload = text.encode("utf-8")
    version = _pick_version(len(payload))
    words = _interleave(_bitstream(payload, version), version)

    size = version * 4 + 17
    base = _blank(size)
    _place_function_patterns(base, version)
    reserved = [[cell is not None for cell in row] for row in base]
    _place_data(base, reserved, words)

    best, best_score = None, None
    for mask in range(8):
        candidate = _apply_mask(base, reserved, mask)
        _place_format(candidate, mask)
        score = _penalty(candidate)
        if best_score is None or score < best_score:
            best, best_score = candidate, score
    return best


def ansi(text: str, quiet: int = 2) -> str:
    """The code as terminal text, two module rows per character row (▀ ▄ █).

    Colours are set explicitly (black on white) rather than inherited: a QR scanner needs dark
    modules on a light background, and half the world's terminals are dark-themed.
    """
    m = encode(text)
    size = len(m)
    pad = [0] * (size + quiet * 2)
    rows = [pad[:] for _ in range(quiet)] + \
           [[0] * quiet + row + [0] * quiet for row in m] + \
           [pad[:] for _ in range(quiet)]
    if len(rows) % 2:
        rows.append(pad[:])

    out = []
    for i in range(0, len(rows), 2):
        top, bottom = rows[i], rows[i + 1]
        line = []
        for t, b in zip(top, bottom):
            line.append(" █"[1] if t and b else "▀" if t else "▄" if b else " ")
        out.append("\x1b[30;47m" + "".join(line) + "\x1b[0m")
    return "\n".join(out)
