#!/usr/bin/env python3
"""statue.py: photo of a marble statue -> detailed ASCII on pure black (OLED).
  1. separates the marble (light, low saturation) from the background, fills holes
  2. stretches contrast inside the statue and sharpens the details (unsharp mask)
  3. each cell becomes a character from a dense ramp + its own gray level
Usage: python3 statue.py photo.png [cols] [x0,y0,x1,y1]  -> prints the ASCII
As a module: art(path, cols, crop) -> list of rows, each a list of (char, gray 0..1)."""
import colorsys
import math
import sys

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

FINO = 0.5
RAMP = " .,:;-~=+*o%#@"


def load(path, largura=1000):
    p = GdkPixbuf.Pixbuf.new_from_file(path)
    if p.get_width() > largura:      # plenty for 120-160 columns, and keeps pure Python fast
        p = p.scale_simple(largura, round(p.get_height() * largura / p.get_width()),
                           GdkPixbuf.InterpType.HYPER)
    w, h, n, st = p.get_width(), p.get_height(), p.get_n_channels(), p.get_rowstride()
    d = p.get_pixels()
    lum, sat = [], []
    for y in range(h):
        for x in range(w):
            i = y * st + x * n
            r, g, b = d[i] / 255, d[i + 1] / 255, d[i + 2] / 255
            _, s, v = colorsys.rgb_to_hsv(r, g, b)
            lum.append(0.2126 * r + 0.7152 * g + 0.0722 * b)
            sat.append((s, v))
    return w, h, lum, sat


def mask_of(w, h, lum, sat):
    # the wall is saturated and dark (s > ~0.6, v < ~0.5); marble, even in shadow, is paler or brighter
    m = [0 if (sv[0] > 0.56 and sv[1] < 0.52) or sv[1] < 0.22 else 1 for sv in sat]
    # background = everything not-marble connected to the border; the rest (shadows inside) is statue
    out = [0] * (w * h)
    pilha = [i for i in range(w)] + [(h - 1) * w + i for i in range(w)] + \
            [y * w for y in range(h)] + [y * w + w - 1 for y in range(h)]
    vis = bytearray(w * h)
    while pilha:
        i = pilha.pop()
        if vis[i] or m[i]:
            continue
        vis[i] = 1
        x, y = i % w, i // w
        if x > 0: pilha.append(i - 1)
        if x < w - 1: pilha.append(i + 1)
        if y > 0: pilha.append(i - w)
        if y < h - 1: pilha.append(i + w)
    for i in range(w * h):
        out[i] = 0 if vis[i] else 1
    return out


def blur(w, h, a, r):
    """box blur (two passes) for the unsharp mask"""
    t = [0.0] * (w * h)
    for y in range(h):
        acc = 0.0; row = y * w
        for x in range(-r, w):
            if x + r < w: acc += a[row + x + r]
            if x - r - 1 >= 0: acc -= a[row + x - r - 1]
            if x >= 0: t[row + x] = acc / (2 * r + 1)
    o = [0.0] * (w * h)
    for x in range(w):
        acc = 0.0
        for y in range(-r, h):
            if y + r < h: acc += t[(y + r) * w + x]
            if y - r - 1 >= 0: acc -= t[(y - r - 1) * w + x]
            if y >= 0: o[y * w + x] = acc / (2 * r + 1)
    return o


def art(path, cols=76, box=(0, 0, 1, 1), gamma=1.0, amount=1.3):
    """box = (x0, y0, x1, y1) as fractions of the photo: the part that becomes ASCII."""
    w, h, lum, sat = load(path)
    m = mask_of(w, h, lum, sat)
    # adaptive local contrast (like CLAHE): each point is compared with its own neighbourhood
    # (mean and deviation over a large radius), so the face is not darkened by the bright shoulders
    r = max(4, w // 16)
    media = blur(w, h, lum, r)
    quad = blur(w, h, [v * v for v in lum], r)
    fino = blur(w, h, lum, max(1, w // 200))
    bx0, by0, bx1, by1 = int(box[0] * w), int(box[1] * h), int(box[2] * w), int(box[3] * h)
    dentro = [lum[y * w + x] for y in range(by0, by1, 3) for x in range(bx0, bx1, 3) if m[y * w + x]]
    dentro.sort()
    lo, hi = dentro[int(len(dentro) * 0.03)], dentro[int(len(dentro) * 0.99)]
    sharp = []
    for i in range(w * h):
        dp = math.sqrt(max(1e-5, quad[i] - media[i] * media[i]))
        local = 0.5 + 0.5 * math.tanh(amount * (lum[i] - media[i]) / (dp + 0.03))
        glob = min(1, max(0, (lum[i] - lo) / (hi - lo)))
        sharp.append(min(1, max(0, 0.45 * glob + 0.55 * local + FINO * (lum[i] - fino[i]))))
    cw = (bx1 - bx0) / cols
    ch = cw * 2.05                      # monospace cells are ~2x taller than wide
    rows = int((by1 - by0) / ch)
    out = []
    for r in range(rows):
        linha = []
        for c in range(cols):
            tot = cov = n = 0
            for y in range(by0 + int(r * ch), min(by1, by0 + int((r + 1) * ch))):
                for x in range(bx0 + int(c * cw), min(bx1, bx0 + int((c + 1) * cw))):
                    i = y * w + x
                    n += 1
                    if m[i]:
                        cov += 1; tot += sharp[i]
            if n == 0 or cov / n < 0.35:
                linha.append((" ", 0.0)); continue
            v = (tot / cov) ** gamma
            k = 1 + int(v * (len(RAMP) - 2))
            linha.append((RAMP[k], 0.07 + 0.93 * v))
        out.append(linha)
    while out and all(ch == " " for ch, _ in out[-1]):
        out.pop()
    while out and all(ch == " " for ch, _ in out[0]):
        out.pop(0)
    return out


if __name__ == "__main__":
    a = art(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 76,
            tuple(float(v) for v in sys.argv[3].split(",")) if len(sys.argv) > 3 else (0, 0, 1, 1))
    print("\n".join("".join(ch for ch, _ in l).rstrip() for l in a))
