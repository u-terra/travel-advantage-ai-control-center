"""Minimal, self-contained QR code encoder (byte mode only) -> SVG.

Pure stdlib, no third-party deps. Dev-only tool, run once offline to
(re)generate the static app/static/demo-qr.svg asset committed to the
repo for GET /demo (see app.web_api.demo_qr_asset). Not imported by the
running app - regenerate with:

    python tools/gen_qr.py [text]

text defaults to the production demo URL below; pass a different value
only if ORCHESTRAVEL_PUBLIC_BASE_URL ever changes for real.
"""
from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------- GF(256)
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for i in range(255):
    EXP[i] = _x
    LOG[_x] = i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for i in range(255, 512):
    EXP[i] = EXP[i - 255]


def gf_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return EXP[LOG[a] + LOG[b]]


def rs_generator_poly(degree: int) -> list[int]:
    poly = [1]
    for i in range(degree):
        poly = poly_mul(poly, [1, EXP[i]])
    return poly


def poly_mul(a: list[int], b: list[int]) -> list[int]:
    res = [0] * (len(a) + len(b) - 1)
    for i, ac in enumerate(a):
        if ac == 0:
            continue
        for j, bc in enumerate(b):
            if bc == 0:
                continue
            res[i + j] ^= gf_mul(ac, bc)
    return res


def rs_encode(data: list[int], ec_len: int) -> list[int]:
    gen = rs_generator_poly(ec_len)
    res = data + [0] * ec_len
    for i in range(len(data)):
        coef = res[i]
        if coef == 0:
            continue
        for j, g in enumerate(gen):
            res[i + j] ^= gf_mul(g, coef)
    return res[len(data):]


# --------------------------------------------------------- capacity table
# (version 1-5), EC level M, byte mode data-codeword counts & module size.
# Values below are QR spec constants for the versions we actually need.
VERSIONS = {
    1: dict(total_cw=26, ec_cw=10, size=21),
    2: dict(total_cw=44, ec_cw=16, size=25),
    3: dict(total_cw=70, ec_cw=26, size=29),
    4: dict(total_cw=100, ec_cw=18, size=33),
    5: dict(total_cw=134, ec_cw=24, size=37),
}


def choose_version(byte_len: int):
    for v, info in VERSIONS.items():
        data_cw = info["total_cw"] - info["ec_cw"]
        header_bits = 4 + 8  # byte mode indicator + 8-bit count (v1-9)
        cap_bits = data_cw * 8
        if header_bits + byte_len * 8 + 4 <= cap_bits:
            return v, info
    raise ValueError("payload too long for supported versions")


def encode_bitstream(data: bytes, version: int, info: dict) -> list[int]:
    bits = []

    def put(val: int, n: int):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)  # byte mode
    put(len(data), 8)
    for b in data:
        put(b, 8)

    data_cw = info["total_cw"] - info["ec_cw"]
    cap_bits = data_cw * 8
    put(0, min(4, cap_bits - len(bits)))  # terminator
    while len(bits) % 8:
        bits.append(0)
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    i = 0
    while len(codewords) < data_cw:
        codewords.append(pad[i % 2])
        i += 1
    return codewords


def build_codewords(data: bytes):
    version, info = choose_version(len(data))
    data_cw = encode_bitstream(data, version, info)
    ec_cw = rs_encode(data_cw, info["ec_cw"])
    return version, info, data_cw + ec_cw


# --------------------------------------------------------------- matrix
def make_matrix(size: int):
    return [[None] * size for _ in range(size)]


def place_finder(m, row, col):
    for r in range(-1, 8):
        for c in range(-1, 8):
            rr, cc = row + r, col + c
            if not (0 <= rr < len(m) and 0 <= cc < len(m)):
                continue
            if 0 <= r <= 6 and 0 <= c <= 6:
                is_border = r in (0, 6) or c in (0, 6)
                is_core = 2 <= r <= 4 and 2 <= c <= 4
                m[rr][cc] = 1 if (is_border or is_core) else 0
            else:
                m[rr][cc] = 0  # separator


ALIGNMENT_CENTERS = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30]}


def place_alignment(m, size):
    version = next(v for v, info in VERSIONS.items() if info["size"] == size)
    centers = ALIGNMENT_CENTERS[version]
    for row in centers:
        for col in centers:
            if (row, col) in ((6, 6), (6, size - 7), (size - 7, 6)):
                continue
            for r in range(-2, 3):
                for c in range(-2, 3):
                    is_border = r in (-2, 2) or c in (-2, 2)
                    m[row + r][col + c] = 1 if (is_border or (r == 0 and c == 0)) else 0


def place_timing(m, size):
    for i in range(8, size - 8):
        m[6][i] = 1 - (i % 2)
        m[i][6] = 1 - (i % 2)


def place_dark_module(m, size):
    m[size - 8][8] = 1


def reserve_format_areas(m, size):
    for i in range(9):
        if m[8][i] is None:
            m[8][i] = -1
        if m[i][8] is None:
            m[i][8] = -1
    for i in range(8):
        if m[8][size - 1 - i] is None:
            m[8][size - 1 - i] = -1
        if m[size - 1 - i][8] is None:
            m[size - 1 - i][8] = -1


def place_data(m, size, codewords):
    bits = []
    for cw in codewords:
        for i in range(7, -1, -1):
            bits.append((cw >> i) & 1)
    bit_idx = 0
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
            continue
        cols = (col, col - 1)
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in cols:
                if m[row][c] is None:
                    val = bits[bit_idx] if bit_idx < len(bits) else 0
                    bit_idx += 1
                    m[row][c] = val
        upward = not upward
        col -= 2
    return m


MASK_FNS = [
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
]


def apply_mask(m, fm, mask_idx):
    size = len(m)
    fn = MASK_FNS[mask_idx]
    out = [row[:] for row in m]
    for r in range(size):
        for c in range(size):
            if fm[r][c] is not None:
                continue
            if fn(r, c):
                out[r][c] ^= 1
    return out


def penalty_score(m):
    size = len(m)
    score = 0
    for r in range(size):
        run = 1
        for c in range(1, size):
            if m[r][c] == m[r][c - 1]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for c in range(size):
        run = 1
        for r in range(1, size):
            if m[r][c] == m[r - 1][c]:
                run += 1
            else:
                if run >= 5:
                    score += 3 + (run - 5)
                run = 1
        if run >= 5:
            score += 3 + (run - 5)
    for r in range(size - 1):
        for c in range(size - 1):
            block = m[r][c] + m[r][c + 1] + m[r + 1][c] + m[r + 1][c + 1]
            if block in (0, 4):
                score += 3
    dark = sum(sum(row) for row in m)
    total = size * size
    ratio = abs(dark * 100 // total - 50) // 5
    score += ratio * 10
    return score


FORMAT_EC_BITS = {"L": 0b01, "M": 0b00, "Q": 0b11, "H": 0b10}


def format_info_bits(ec_level: str, mask_idx: int) -> list[int]:
    data = (FORMAT_EC_BITS[ec_level] << 3) | mask_idx
    # BCH(15,5) remainder computation, generator 0b10100110111 (0x537)
    val = data << 10
    gpoly = 0b10100110111
    for i in range(14, 9, -1):
        if val & (1 << i):
            val ^= gpoly << (i - 10)
    fmt = ((data << 10) | val) ^ 0b101010000010010
    return [(fmt >> i) & 1 for i in range(14, -1, -1)]


def place_format_info(m, size, ec_level, mask_idx):
    bits = format_info_bits(ec_level, mask_idx)
    for i in range(6):
        m[8][i] = bits[i]
    m[8][7] = bits[6]
    m[8][8] = bits[7]
    m[7][8] = bits[8]
    for i in range(9, 15):
        m[14 - i][8] = bits[i]
    for i in range(8):
        m[size - 1 - i][8] = bits[i]
    for i in range(8, 15):
        m[8][size - 15 + i] = bits[i]


def generate_matrix(codewords, size, ec_level="M"):
    m = make_matrix(size)
    place_finder(m, 0, 0)
    place_finder(m, 0, size - 7)
    place_finder(m, size - 7, 0)
    place_alignment(m, size)
    place_timing(m, size)
    place_dark_module(m, size)
    reserve_format_areas(m, size)
    fm = [row[:] for row in m]  # function-module map (None = data)
    place_data(m, size, codewords)
    for r in range(size):
        for c in range(size):
            if m[r][c] == -1:
                m[r][c] = 0
            if fm[r][c] == -1:
                fm[r][c] = 0

    best = None
    best_score = None
    best_mask = 0
    for mask_idx in range(8):
        candidate = apply_mask(m, fm, mask_idx)
        s = penalty_score(candidate)
        if best_score is None or s < best_score:
            best_score = s
            best = candidate
            best_mask = mask_idx
    place_format_info(best, size, ec_level, best_mask)
    return best, fm, best_mask


def matrix_to_svg(m, module_px=8, quiet=4):
    size = len(m)
    total = size + quiet * 2
    dim = total * module_px
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {dim} {dim}" '
        f'width="{dim}" height="{dim}" shape-rendering="crispEdges">',
        f'<rect width="{dim}" height="{dim}" fill="#ffffff"/>',
    ]
    for r in range(size):
        for c in range(size):
            if m[r][c]:
                x = (c + quiet) * module_px
                y = (r + quiet) * module_px
                parts.append(f'<rect x="{x}" y="{y}" width="{module_px}" height="{module_px}" fill="#0a0f1c"/>')
    parts.append("</svg>")
    return "\n".join(parts)


def extract_codewords(m, fm, size, mask_idx, num_codewords):
    """Read data codewords back out of a finished matrix - used only by
    this module's self-check to confirm encode/place round-trips before
    the SVG is written."""
    fn = MASK_FNS[mask_idx]
    bits = []
    col = size - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
            continue
        cols = (col, col - 1)
        rows = range(size - 1, -1, -1) if upward else range(size)
        for row in rows:
            for c in cols:
                if fm[row][c] is None:
                    v = m[row][c]
                    if fn(row, c):
                        v ^= 1
                    bits.append(v)
        upward = not upward
        col -= 2
    codewords = []
    for i in range(0, len(bits), 8):
        chunk = bits[i:i + 8]
        if len(chunk) < 8:
            break
        val = 0
        for b in chunk:
            val = (val << 1) | b
        codewords.append(val)
        if len(codewords) >= num_codewords:
            break
    return codewords


def decode_byte_mode(codewords: list[int]) -> bytes:
    mode = codewords[0] >> 4
    assert mode == 0b0100, f"unexpected mode {mode:04b}"
    length = ((codewords[0] & 0x0F) << 4) | (codewords[1] >> 4)
    out = bytearray()
    prev_low = codewords[1] & 0x0F
    for i in range(2, 2 + length):
        cur = codewords[i]
        byte = (prev_low << 4) | (cur >> 4)
        out.append(byte)
        prev_low = cur & 0x0F
    return bytes(out)


def make_qr_svg(text: str) -> str:
    data = text.encode("utf-8")
    version, info, codewords = build_codewords(data)
    m, fm, mask_idx = generate_matrix(codewords, info["size"], ec_level="M")

    # Self-check: decode our own matrix back to the source bytes before
    # writing anything, so a broken encoder never silently ships a QR
    # that real scanners can't read.
    data_cw = info["total_cw"] - info["ec_cw"]
    extracted = extract_codewords(m, fm, info["size"], mask_idx, data_cw)
    decoded = decode_byte_mode(extracted)
    if decoded != data:
        raise AssertionError(f"QR self-check failed: decoded {decoded!r} != {data!r}")

    return matrix_to_svg(m)


DEFAULT_TEXT = "https://app.orchestravel.ru/demo"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "app", "static", "demo-qr.svg")

if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TEXT
    svg = make_qr_svg(text)
    out_path = os.path.normpath(OUTPUT_PATH)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"written {out_path} for {text!r} (self-check passed)")
