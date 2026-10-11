#!/usr/bin/env python3
"""
build-aero-symbols.py — builds AeroSymbols.ttf, the window-button symbols of Arch Aero OS (doc 49).

Why a font: the title-bar plugin (hyprbars) can only draw TEXT, and it centers the text's LINE box,
not the glyph. Symbols taken from regular fonts (✖ ▲ ▼ ●) have different sizes and sit at different
heights. Here the four symbols are drawn with the SAME 600x600 box, centered in the line
(em = 1000, ascender 800, descender -200: line center = y 300), so every place that uses them
(title bars, Settings, EZ File Manager) shows them identical and perfectly aligned.

Codepoints (Private Use Area, unused by every installed font):
  U+F8F0 close (X)   U+F8F1 maximize/restore (triangle up)
  U+F8F2 minimize (triangle down)   U+F8F3 always visible (circle)

The TrueType file is written by hand (no fontTools needed): just the tables FreeType needs.
"""
import math
import os
import struct
import sys

UPEM, ASC, DESC = 1000, 800, -200
BOX = (200, 0, 800, 600)            # xMin, yMin, xMax, yMax of every symbol (center 500,300)
W = 1000                            # advance width


def glyph_x(t=150):
    """X: two diagonal bars of thickness t inside BOX (one closed contour, clockwise)."""
    x0, y0, x1, y1 = BOX
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    d = t / math.sqrt(2)
    pts = [(x0 + d, y1), (cx, cy + d), (x1 - d, y1), (x1, y1 - d), (cx + d, cy), (x1, y0 + d),
           (x1 - d, y0), (cx, cy - d), (x0 + d, y0), (x0, y0 + d), (cx - d, cy), (x0, y1 - d)]
    return [[(round(x), round(y), True) for x, y in pts]]


def glyph_tri_up():
    x0, y0, x1, y1 = BOX
    return [[(x0, y0, True), ((x0 + x1) // 2, y1, True), (x1, y0, True)]]   # clockwise


def glyph_tri_down():
    x0, y0, x1, y1 = BOX
    return [[(x0, y1, True), (x1, y1, True), ((x0 + x1) // 2, y0, True)]]   # clockwise


def glyph_circle():
    """Circle with 8 off-curve points (TrueType quadratic; on-curve points are implied midpoints)."""
    x0, y0, x1, y1 = BOX
    cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2
    k = r / math.cos(math.pi / 8)
    pts = []
    for i in range(8):                      # clockwise from the top: 90, 45, 0, -45 ...
        a = math.radians(90 - 45 * i)
        pts.append((round(cx + k * math.cos(a)), round(cy + k * math.sin(a)), False))
    return [pts]


GLYPHS = [None, glyph_x(), glyph_tri_up(), glyph_tri_down(), glyph_circle()]     # 0 = .notdef (empty)
CMAP = {0xF8F0: 1, 0xF8F1: 2, 0xF8F2: 3, 0xF8F3: 4}


def bbox(contours):
    xs = [p[0] for c in contours for p in c]
    ys = [p[1] for c in contours for p in c]
    return min(xs), min(ys), max(xs), max(ys)


def glyf_data(contours):
    if not contours:
        return b""
    xmin, ymin, xmax, ymax = bbox(contours)
    out = struct.pack(">hhhhh", len(contours), xmin, ymin, xmax, ymax)
    end, n = [], 0
    for c in contours:
        n += len(c)
        end.append(n - 1)
    out += struct.pack(">" + "H" * len(end), *end)
    out += struct.pack(">H", 0)                                     # no hinting instructions
    pts = [p for c in contours for p in c]
    out += bytes(1 if on else 0 for _, _, on in pts)                # flags: on-curve bit, 16-bit deltas
    px = py = 0
    xs, ys = b"", b""
    for x, y, _ in pts:
        xs += struct.pack(">h", x - px)
        ys += struct.pack(">h", y - py)
        px, py = x, y
    out += xs + ys
    while len(out) % 4:
        out += b"\0"
    return out


def checksum(data):
    data += b"\0" * ((4 - len(data) % 4) % 4)
    return sum(struct.unpack(">%dI" % (len(data) // 4), data)) & 0xFFFFFFFF


def build(path):
    n = len(GLYPHS)
    glyf, loca, off = b"", [], 0
    for g in GLYPHS:
        loca.append(off)
        d = glyf_data(g or [])
        glyf += d
        off += len(d)
    loca.append(off)
    allb = [bbox(g) for g in GLYPHS if g]
    gx0, gy0 = min(b[0] for b in allb), min(b[1] for b in allb)
    gx1, gy1 = max(b[2] for b in allb), max(b[3] for b in allb)
    maxpts = max(sum(len(c) for c in g) for g in GLYPHS if g)
    maxcont = max(len(g) for g in GLYPHS if g)

    t = {}
    t[b"head"] = struct.pack(">IIIIHHqqhhhhHHhhh", 0x00010000, 0x00010000, 0, 0x5F0F3CF5, 0x000B, UPEM,
                             0, 0, gx0, gy0, gx1, gy1, 0, 8, 2, 1, 0)
    t[b"hhea"] = struct.pack(">IhhhHhhhhhhhhhhhH", 0x00010000, ASC, DESC, 0, W, gx0, W - gx1, gx1,
                             1, 0, 0, 0, 0, 0, 0, 0, n)
    t[b"maxp"] = struct.pack(">IHHHHHHHHHHHHHH", 0x00010000, n, maxpts, maxcont, 0, 0, 2, 0, 0, 0, 0, 0, 0, 0, 0)
    hm = b""
    for g in GLYPHS:
        hm += struct.pack(">Hh", W, bbox(g)[0] if g else 0)
    t[b"hmtx"] = hm
    t[b"loca"] = struct.pack(">" + "I" * len(loca), *loca)
    t[b"glyf"] = glyf
    # cmap format 4 (one segment per codepoint + the 0xFFFF terminator)
    codes = sorted(CMAP)
    seg = len(codes) + 1
    ends = codes + [0xFFFF]
    starts = codes + [0xFFFF]
    deltas = [(CMAP[c] - c) & 0xFFFF for c in codes] + [1]
    sr = 2 * (2 ** int(math.log2(seg)))
    sub = struct.pack(">HHHHHHH", 4, 0, 0, seg * 2, sr, int(math.log2(sr // 2)), seg * 2 - sr)
    sub += struct.pack(">" + "H" * seg, *ends) + b"\0\0" + struct.pack(">" + "H" * seg, *starts)
    sub += struct.pack(">" + "H" * seg, *deltas) + struct.pack(">" + "H" * seg, *([0] * seg))
    sub = sub[:2] + struct.pack(">H", len(sub)) + sub[4:]
    t[b"cmap"] = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + sub
    # name (Windows platform, UTF-16BE)
    names = {1: "Aero Symbols", 2: "Regular", 3: "Aero Symbols Regular 1.0", 4: "Aero Symbols",
             5: "Version 1.0", 6: "AeroSymbols-Regular"}
    recs, strs = b"", b""
    for nid, s in names.items():
        e = s.encode("utf-16-be")
        recs += struct.pack(">HHHHHH", 3, 1, 0x409, nid, len(e), len(strs))
        strs += e
    t[b"name"] = struct.pack(">HHH", 0, len(names), 6 + 12 * len(names)) + recs + strs
    t[b"OS/2"] = struct.pack(">HhHHHhhhhhhhhhhh10sIIII4sHHHhhhHHIIhhHHH",
                             4, W, 400, 5, 0, 650, 600, 0, 75, 650, 600, 0, 350, 50, 250, 0,
                             bytes(10), 0, 0x10000000, 0, 0, b"AERO", 0x40, min(CMAP), max(CMAP),   # PUA = Unicode range bit 60
                             ASC, DESC, 0, ASC, -DESC, 1, 0, 500, 700, 0, 32, 0)
    t[b"post"] = struct.pack(">IIhhIIIII", 0x00030000, 0, -100, 50, 0, 0, 0, 0, 0)

    tags = sorted(t)
    nt = len(tags)
    es = int(math.log2(nt))
    sr = (2 ** es) * 16
    head = struct.pack(">IHHHH", 0x00010000, nt, sr, es, nt * 16 - sr)
    offset = 12 + 16 * nt
    dirs, body = b"", b""
    for tag in tags:
        data = t[tag]
        dirs += struct.pack(">4sIII", tag, checksum(data), offset + len(body), len(data))
        body += data + b"\0" * ((4 - len(data) % 4) % 4)
    font = bytearray(head + dirs + body)
    adj = (0xB1B0AFBA - checksum(bytes(font))) & 0xFFFFFFFF          # head.checkSumAdjustment
    hoff = offset + body.index(t[b"head"])                            # head table offset in the file
    font[hoff + 8:hoff + 12] = struct.pack(">I", adj)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(font)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/.local/share/fonts/AeroSymbols.ttf"))
