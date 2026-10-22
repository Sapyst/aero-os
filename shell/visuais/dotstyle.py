#!/usr/bin/env python3
"""
dotstyle.py — the DotStyle visual's own pictures, drawn (nothing downloaded):

  python3 dotstyle.py wallpaper <out.png> [W H]   the Venus de Milo as a halftone of dots on OLED black
  python3 dotstyle.py preview <out.png>           the 384×216 card for Settings → Theme

The statue comes from boot/venus-de-milo.jpg (public-domain photo, also used by the boot animation).
"""
import math
import os
import sys

import cairo
import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))
ESTATUA = [os.path.join(AQUI, "venus-de-milo.jpg"), os.path.join(AQUI, "..", "..", "boot", "venus-de-milo.jpg"),
           os.path.expanduser("~/.local/share/aero-shell/visuais/venus-de-milo.jpg")]
FUNDO = (0.027, 0.027, 0.031)
TINTA = (0.925, 0.92, 0.91)
VERMELHO = (0.91, 0.19, 0.16)


def amostras(cols, rows):
    """Brightness grid (0..1) of the statue, background removed, or None if the photo is missing."""
    caminho = next((c for c in ESTATUA if os.path.isfile(c)), None)
    if caminho is None:
        return None
    pb = GdkPixbuf.Pixbuf.new_from_file(caminho).scale_simple(cols, rows, GdkPixbuf.InterpType.HYPER)
    d, rs, n = pb.get_pixels(), pb.get_rowstride(), pb.get_n_channels()
    grade = []
    for y in range(rows):
        linha = []
        for x in range(cols):
            i = y * rs + x * n
            lum = (d[i] * 0.299 + d[i + 1] * 0.587 + d[i + 2] * 0.114) / 255
            linha.append(max(0.0, min(1.0, (lum - 0.42) / 0.5)))   # the grey studio background drops out
        grade.append(linha)
    return grade


def desenhar(cr, w, h, passo, vermelho=True):
    cr.set_source_rgb(*FUNDO)
    cr.paint()
    # the faint grid everywhere
    for x in range(int(passo / 2), w, passo):
        for y in range(int(passo / 2), h, passo):
            cr.set_source_rgba(*TINTA, 0.05)
            cr.arc(x, y, passo * 0.09, 0, 2 * math.pi)
            cr.fill()
    # the statue: a column a bit right of centre, as tall as the screen
    alto = int(h * 0.94 / passo)
    largo = int(alto * 1920 / 3221)
    grade = amostras(largo, alto)
    if grade is None:
        return
    ox = int(w * 0.62 / passo) - largo // 2
    oy = int(h * 0.06 / passo)
    for j, linha in enumerate(grade):
        for i, v in enumerate(linha):
            if v < 0.04:
                continue
            x = (ox + i + 0.5) * passo
            y = (oy + j + 0.5) * passo
            cr.set_source_rgba(*TINTA, 0.25 + 0.75 * v)
            cr.arc(x, y, passo * 0.5 * (0.18 + 0.72 * math.sqrt(v)), 0, 2 * math.pi)
            cr.fill()
    if vermelho:                                            # the one red dot, as in the whole visual
        cr.set_source_rgb(*VERMELHO)
        cr.arc(w - passo * 3, h - passo * 3, passo * 0.42, 0, 2 * math.pi)
        cr.fill()


def wallpaper(saida, w=2560, h=1440):
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    desenhar(cairo.Context(s), w, h, max(10, w // 200))
    s.write_to_png(saida)


def previa(saida, w=384, h=216):
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(s)
    desenhar(cr, w, h, 5, vermelho=False)
    # the bar: a thin black strip with dot icons, the clock exactly in the middle
    cr.set_source_rgba(0.06, 0.06, 0.065, 0.95)
    cr.rectangle(0, 0, w, 11)
    cr.fill()
    for k in range(5):
        cr.set_source_rgba(*TINTA, 0.95 if k == 0 else 0.3)
        cr.arc(8 + k * 6, 5.5, 1.6, 0, 2 * math.pi)
        cr.fill()
    for k in range(4):
        cr.set_source_rgba(*TINTA, 0.85)
        cr.arc(w / 2 - 6 + k * 4, 5.5, 1.1, 0, 2 * math.pi)
        cr.fill()
    for k in range(6):
        cr.set_source_rgba(*(VERMELHO if k == 2 else TINTA), 0.9)
        cr.arc(w - 50 + k * 7, 5.5, 1.4, 0, 2 * math.pi)
        cr.fill()
    # a notification card, built of dots, under the bell
    x0, y0, cw, ch = w - 118, 18, 108, 34
    cr.set_source_rgba(0.04, 0.04, 0.045, 0.95)
    r = 8
    cr.new_sub_path()
    for (ax, ay, a0) in ((x0 + cw - r, y0 + r, -math.pi / 2), (x0 + cw - r, y0 + ch - r, 0),
                         (x0 + r, y0 + ch - r, math.pi / 2), (x0 + r, y0 + r, math.pi)):
        cr.arc(ax, ay, r, a0, a0 + math.pi / 2)
    cr.close_path()
    cr.fill()
    for k in range(int((cw + ch) * 2 / 4)):
        t = k / ((cw + ch) * 2 / 4)
        per = 2 * (cw + ch)
        d = t * per
        if d < cw:
            px, py = x0 + d, y0
        elif d < cw + ch:
            px, py = x0 + cw, y0 + d - cw
        elif d < 2 * cw + ch:
            px, py = x0 + cw - (d - cw - ch), y0 + ch
        else:
            px, py = x0, y0 + ch - (d - 2 * cw - ch)
        cr.set_source_rgba(*TINTA, 0.4)
        cr.arc(px, py, 0.7, 0, 2 * math.pi)
        cr.fill()
    cr.set_source_rgb(*VERMELHO)
    cr.arc(x0 + cw - 7, y0 + 7, 2.2, 0, 2 * math.pi)
    cr.fill()
    for k, largura in enumerate((60, 80)):
        cr.set_source_rgba(*TINTA, 0.75 - k * 0.3)
        cr.rectangle(x0 + 8, y0 + 14 + k * 8, largura, 3)
        cr.fill()
    s.write_to_png(saida)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "wallpaper":
        dims = [int(x) for x in sys.argv[3:5]] if len(sys.argv) >= 5 else []
        wallpaper(sys.argv[2], *dims)
    elif len(sys.argv) == 3 and sys.argv[1] == "preview":
        previa(sys.argv[2])
    else:
        sys.exit(__doc__)
