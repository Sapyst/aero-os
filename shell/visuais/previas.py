#!/usr/bin/env python3
"""previas.py — miniaturas dos visuais para o Settings (desenhadas, não prints: nada pessoal aparece)."""
import math
import os
import subprocess

import cairo

AQUI = os.path.dirname(os.path.abspath(__file__))
W, H = 384, 216


def arredondado(cr, x, y, w, h, r):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def aeros():
    tmp = os.path.join(AQUI, ".fundo-previa.png")
    subprocess.run(["python3", "-I", os.path.join(AQUI, "montanhas.py"), tmp, str(W), str(H)], check=True)
    s = cairo.ImageSurface.create_from_png(tmp)
    os.unlink(tmp)
    cr = cairo.Context(s)
    cr.set_source_rgba(1, 1, 1, 0.55)                 # barra de vidro
    cr.rectangle(0, 0, W, 11)
    cr.fill()
    for i in range(5):                                # pontinhos
        cr.set_source_rgba(0.48, 0.5, 0.94, 1) if i == 0 else cr.set_source_rgba(0.2, 0.2, 0.3, 0.3)
        arredondado(cr, W / 2 - 22 + i * 7 + (4 if i else 0), 4, 9 if i == 0 else 3, 3, 1.5)
        cr.fill()
    arredondado(cr, 112, 42, 160, 92, 10)             # janela de vidro
    cr.set_source_rgba(1, 1, 1, 0.55)
    cr.fill_preserve()
    cr.set_source_rgba(1, 1, 1, 0.9)
    cr.set_line_width(1)
    cr.stroke()
    for j in range(4):
        arredondado(cr, 126, 62 + j * 14, 90 + (j % 2) * 30, 5, 2.5)
        cr.set_source_rgba(0.48, 0.5, 0.94, 0.35 if j else 0.6)
        cr.fill()
    arredondado(cr, W / 2 - 62, H - 26, 124, 18, 9)  # dock
    cr.set_source_rgba(1, 1, 1, 0.6)
    cr.fill()
    cores = [(0.36, 0.55, 0.95), (0.95, 0.6, 0.4), (0.55, 0.85, 0.6), (0.85, 0.5, 0.75), (0.2, 0.2, 0.3), (0.6, 0.6, 0.95)]
    for i, c in enumerate(cores):
        arredondado(cr, W / 2 - 55 + i * 19, H - 23, 12, 12, 3.5)
        cr.set_source_rgb(*c)
        cr.fill()
    s.write_to_png(os.path.join(AQUI, "previa-AerOS.png"))


def aeroterm():
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
    cr = cairo.Context(s)
    cr.set_source_rgb(0, 0, 0)
    cr.paint()
    arredondado(cr, 6, 4, W - 12, 11, 5.5)            # barra pílula escura
    cr.set_source_rgb(0.12, 0.12, 0.13)
    cr.fill()
    cr.select_font_face("JetBrains Mono")
    cr.set_font_size(6)
    import random
    rnd = random.Random(7)
    ramp = " .:-=+*#%@"
    for r in range(26):                               # um busto em "ASCII" (padrão procedural)
        for c in range(36):
            dx, dy = (c - 18) / 13, (r - 10) / 9
            d = 1 - (dx * dx + dy * dy * 0.8) if r < 16 else 0.75 - abs(dx) * 0.5
            if d > 0.05 and not (r > 15 and abs(dx) > 1.35):
                v = min(1, d + rnd.random() * 0.25)
                cr.set_source_rgb(v * 0.85, v * 0.85, v * 0.82)
                cr.move_to(W / 2 - 18 * 3.6 + c * 3.6, 34 + r * 6.6)
                cr.show_text(ramp[max(1, int(v * 9))])
    for x, y, w, h in ((10, 22, 70, 56), (10, 84, 70, 60), (W - 94, 22, 84, 34), (W - 94, 62, 84, 50)):  # widgets
        arredondado(cr, x, y, w, h, 5)
        cr.set_source_rgba(0.14, 0.14, 0.15, 0.85)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.set_line_width(1)
        cr.stroke()
    arredondado(cr, W / 2 - 50, H - 22, 100, 15, 7)   # dock
    cr.set_source_rgb(0.12, 0.12, 0.13)
    cr.fill()
    for i in range(5):
        arredondado(cr, W / 2 - 44 + i * 18, H - 20, 11, 11, 3)
        cr.set_source_rgb(0.25, 0.25, 0.27)
        cr.fill()
    s.write_to_png(os.path.join(AQUI, "previa-AeroTerm.png"))


def solid():
    """Solid: formas chapadas, superfícies tonais (a paleta do solido.py), barra e dock em pílula, sem contornos."""
    import sys
    sys.path.insert(0, os.path.dirname(AQUI))
    import solido
    e = solido.esquema(True, {"COR_DESTAQUE": "2f8f9a"})
    c = lambda k: solido.de_hexa(e[k])  # noqa: E731
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
    cr = cairo.Context(s)
    cr.set_source_rgb(*solido.tom(205, 30, 22))           # o wallpaper do Solid, em miniatura
    cr.paint()
    for fx, fy, fr, t, cc in ((0.82, 0.18, 0.55, 40, 34), (0.12, 0.92, 0.62, 30, 46), (0.62, 1.05, 0.42, 48, 30)):
        cr.set_source_rgb(*solido.tom(205 + (t - 50) * 0.4, cc, t))
        cr.arc(fx * W, fy * H, fr * H, 0, 2 * math.pi)
        cr.fill()
    arredondado(cr, 6, 4, W - 12, 12, 6)                  # barra: pílula sólida
    cr.set_source_rgb(*c("surface_container"))
    cr.fill()
    arredondado(cr, W / 2 - 14, 6, 28, 8, 4)              # relógio
    cr.set_source_rgb(*c("primary_container"))
    cr.fill()
    arredondado(cr, 104, 34, 176, 112, 14)                # janela: superfície tonal
    cr.set_source_rgb(*c("surface"))
    cr.fill()
    arredondado(cr, 112, 44, 160, 14, 7)
    cr.set_source_rgb(*c("surface_container_high"))
    cr.fill()
    for j in range(3):
        arredondado(cr, 112, 66 + j * 22, 160, 18, 9)
        cr.set_source_rgb(*c("surface_container"))
        cr.fill()
        arredondado(cr, 250, 70 + j * 22, 16, 10, 5)      # interruptores em pílula
        cr.set_source_rgb(*c("primary" if j != 1 else "surface_container_highest"))
        cr.fill()
    arredondado(cr, W - 96, 34, 84, 70, 14)               # quick settings: blocos em pílula
    cr.set_source_rgb(*c("surface_container"))
    cr.fill()
    for i in range(4):
        arredondado(cr, W - 90 + (i % 2) * 40, 40 + (i // 2) * 20, 36, 16, 8)
        cr.set_source_rgb(*c("primary" if i in (0, 3) else "surface_container_highest"))
        cr.fill()
    arredondado(cr, W / 2 - 58, H - 26, 116, 20, 10)       # dock
    cr.set_source_rgb(*c("surface_container"))
    cr.fill()
    for i, k in enumerate(("primary", "tertiary", "secondary", "primary_container", "tertiary_container")):
        cr.arc(W / 2 - 40 + i * 20, H - 16, 6.5, 0, 2 * math.pi)
        cr.set_source_rgb(*c(k))
        cr.fill()
    s.write_to_png(os.path.join(AQUI, "previa-Solid.png"))


if __name__ == "__main__":
    import sys as _sys
    if "solid" in _sys.argv[1:]:
        solid()
    else:
        aeros()
        aeroterm()
        solid()
