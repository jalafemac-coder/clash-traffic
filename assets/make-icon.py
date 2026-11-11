"""Draw the application mark at every size Windows asks for.

One stream arrives and the rules split it in two: the solid arm is the half
that leaves through a proxy, the dimmed one goes out direct. Geometry is
described once in a 256 unit square and rasterised per size with its own
stroke weight, because a mark thinned by bilinear downscaling turns to mush
at 16px -- which is exactly the size the title bar uses.

No imaging library: shapes are distance fields, antialiasing is 4x
supersampling, and the PNGs are assembled by hand into a multi-entry ICO.
"""

import math
import struct
import sys
import zlib

TOP = (0x55, 0x97, 0xEC)        # plate gradient, top
BOTTOM = (0x25, 0x6E, 0xC6)     # plate gradient, bottom
INK = (0xFF, 0xFF, 0xFF)

SS = 4                           # supersampling factor
DIM = 0.45                       # opacity of the direct-path arm

# size -> (stroke, terminal radius, glyph scale)
WEIGHTS = {
    256: (22, 15, 1.00),
    128: (24, 16, 1.00),
    64:  (28, 18, 0.96),
    48:  (31, 20, 0.94),
    32:  (35, 22, 0.90),
    16:  (42, 26, 0.84),
}

# Symmetric arms read as the letter Y; the asymmetric set reads as a path
# that forks, which is both more distinctive and closer to the truth -- the
# traffic rarely splits evenly.
SHAPES = {
    "sym": {"junction": (128.0, 150.0), "foot": (128.0, 208.0),
            "dim": (82.0, 92.0), "solid": (174.0, 92.0)},
    "asym": {"junction": (116.0, 162.0), "foot": (116.0, 212.0),
             "dim": (62.0, 112.0), "solid": (182.0, 66.0)},
}
SHAPE = SHAPES["sym"]


def scaled(point, k):
    cx, cy = 128.0, 134.0        # optical centre, a little above geometric
    return (cx + (point[0] - cx) * k, cy + (point[1] - cy) * k)


def seg_distance(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    span = dx * dx + dy * dy
    if span == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / span))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def rounded_box(x, y, size, radius):
    """Coverage test for the plate, corners included."""
    inner = size - radius
    cx = radius if x < radius else (inner if x > inner else x)
    cy = radius if y < radius else (inner if y > inner else y)
    if (x < radius or x > inner) and (y < radius or y > inner):
        return math.hypot(x - cx, y - cy) <= radius
    return True


def render(size):
    stroke, tip_r, k = WEIGHTS[size]
    half = stroke / 2.0
    j = scaled(SHAPE["junction"], k)
    foot = scaled(SHAPE["foot"], k)
    left = scaled(SHAPE["dim"], k)
    right = scaled(SHAPE["solid"], k)

    n = size * SS
    unit = 256.0 / n            # one device sample in glyph units
    radius = 0.2227 * 256.0     # plate corner, in glyph units
    rows = []

    for py in range(n):
        gy = (py + 0.5) * unit
        # plate gradient is a straight vertical lerp
        mix = gy / 256.0
        plate = tuple(int(round(TOP[i] + (BOTTOM[i] - TOP[i]) * mix)) for i in range(3))
        row = bytearray()
        for px in range(n):
            gx = (px + 0.5) * unit
            if not rounded_box(gx, gy, 256.0, radius):
                row += b"\x00\x00\x00\x00"
                continue

            solid = (seg_distance(gx, gy, foot, j) <= half
                     or seg_distance(gx, gy, j, right) <= half
                     or math.hypot(gx - right[0], gy - right[1]) <= tip_r)
            if solid:
                row += bytes(INK) + b"\xff"
                continue

            dim = (seg_distance(gx, gy, j, left) <= half
                   or math.hypot(gx - left[0], gy - left[1]) <= tip_r)
            if dim:
                blended = tuple(int(round(plate[i] + (INK[i] - plate[i]) * DIM))
                                for i in range(3))
                row += bytes(blended) + b"\xff"
                continue

            row += bytes(plate) + b"\xff"
        rows.append(bytes(row))
    return downsample(rows, size)


def downsample(rows, size):
    out = []
    area = SS * SS
    for y in range(size):
        line = bytearray()
        for x in range(size):
            acc = [0, 0, 0, 0]
            for dy in range(SS):
                src = rows[y * SS + dy]
                base = (x * SS) * 4
                for dx in range(SS):
                    off = base + dx * 4
                    acc[0] += src[off]
                    acc[1] += src[off + 1]
                    acc[2] += src[off + 2]
                    acc[3] += src[off + 3]
            line += bytes(v // area for v in acc)
        out.append(bytes(line))
    return out


def chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png(rows, size):
    raw = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico(images):
    count = len(images)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, blobs = b"", b""
    for size, blob in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0,
                               1, 32, len(blob), offset)
        offset += len(blob)
        blobs += blob
    return header + entries + blobs


if __name__ == "__main__":
    if len(sys.argv) > 2:
        SHAPE = SHAPES[sys.argv[2]]
    images = []
    for size in sorted(WEIGHTS, reverse=True):
        blob = png(render(size), size)
        images.append((size, blob))
        print("  %3dpx  %6d bytes" % (size, len(blob)))
    data = ico(images)
    with open(sys.argv[1], "wb") as fh:
        fh.write(data)
    print("wrote %s (%d bytes, %d sizes)" % (sys.argv[1], len(data), len(images)))
