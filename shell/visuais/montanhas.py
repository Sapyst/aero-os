#!/usr/bin/env python3
"""montanhas.py — o wallpaper do AerOS: montanhas de vidro em tons pastel (gerado, sem imagem de fora).
python3 montanhas.py SAIDA.png [LARGURA ALTURA] [claro|escuro]"""
import math
import sys

import cairo

W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (2560, 1440)
ESCURO = len(sys.argv) > 4 and sys.argv[4] == "escuro"


def cor(h, a=1.0):
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)) + (a,)


s = cairo.ImageSurface(cairo.FORMAT_RGB24, W, H)
cr = cairo.Context(s)

# céu: degradê diagonal azul-claro → lilás → rosa, com um calor de pêssego embaixo
fundo = cairo.LinearGradient(0, 0, W, H)
paleta = (["1b1d3a", "2a2450", "3a2648", "3d2c3a"] if ESCURO else ["dce5fb", "e7e2f8", "f5e3f0", "f8e9df"])
for i, h in enumerate(paleta):
    fundo.add_color_stop_rgb(i / (len(paleta) - 1), *cor(h)[:3])
cr.set_source(fundo)
cr.paint()
for cx, cy, r, h, a in ((0.18, 0.12, 0.55, "c7d7fb", 0.55), (0.82, 0.18, 0.5, "f6cfe3", 0.5), (0.55, 0.95, 0.6, "f9dcc6", 0.45)):
    g = cairo.RadialGradient(cx * W, cy * H, 0, cx * W, cy * H, r * W)
    g.add_color_stop_rgba(0, *cor(h, a * (0.35 if ESCURO else 1)))
    g.add_color_stop_rgba(1, *cor(h, 0))
    cr.set_source(g)
    cr.paint()


def montanha(x0, x1, pico_x, pico_y, base_y, h_topo, h_base, a_topo, a_base, raio=0.06):
    """Um pico de vidro: lados levemente curvos, ponta arredondada, preenchimento em degradê + bordas de luz."""
    px, py = pico_x * W, pico_y * H
    l, r, b = x0 * W, x1 * W, base_y * H
    k = raio * W
    cr.new_path()
    cr.move_to(l, b)
    cr.curve_to(l + (px - l) * 0.62, b - (b - py) * 0.58, px - k * 0.75, py + k * 0.42, px - k * 0.32, py + k * 0.10)
    cr.curve_to(px - k * 0.12, py - k * 0.06, px + k * 0.12, py - k * 0.06, px + k * 0.35, py + k * 0.08)
    cr.curve_to(px + k * 0.75, py + k * 0.42, r - (r - px) * 0.62, b - (b - py) * 0.58, r, b)
    cr.close_path()
    caminho = cr.copy_path()
    g = cairo.LinearGradient(0, py, 0, b)
    g.add_color_stop_rgba(0, *cor(h_topo, min(1, a_topo * 1.18)))
    g.add_color_stop_rgba(1, *cor(h_base, a_base))
    cr.set_source(g)
    cr.fill()
    # brilho interno (vidro): um véu claro perto da crista
    cr.append_path(caminho)
    v = cairo.LinearGradient(px - k * 3, py, px + k * 2, py + (b - py) * 0.6)
    v.add_color_stop_rgba(0, 1, 1, 1, 0.0)
    v.add_color_stop_rgba(0.45, 1, 1, 1, 0.16 if not ESCURO else 0.06)
    v.add_color_stop_rgba(1, 1, 1, 1, 0.0)
    cr.set_source(v)
    cr.fill()
    # borda de luz: larga e fraca (halo) + fina e clara (aresta do vidro)
    for largura, alfa in ((14, 0.10), (5, 0.22), (2.2, 0.85 if not ESCURO else 0.45)):
        cr.append_path(caminho)
        cr.set_line_width(largura * W / 2560)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        cr.set_source_rgba(1, 1, 1, alfa)
        cr.stroke()


# do fundo para a frente: (x0, x1, pico_x, pico_y, base_y, cor topo, cor base, alfa topo, alfa base)
picos = [
    (0.30, 1.05, 0.62, 0.10, 1.10, "b7c9f4", "d9d4f6", 0.70, 0.35),   # azul, alto, ao fundo
    (-0.10, 0.62, 0.30, 0.22, 1.10, "cdc3f1", "e9d9f0", 0.62, 0.30),  # lilás
    (0.55, 1.25, 0.86, 0.30, 1.10, "f3c9dc", "f6dfe6", 0.66, 0.30),   # rosa, à direita
    (-0.20, 0.45, 0.13, 0.48, 1.10, "f2d3bd", "efdcd2", 0.70, 0.35),  # pêssego, à esquerda
    (0.18, 0.92, 0.50, 0.52, 1.10, "c5c8f2", "e6e0f4", 0.55, 0.28),   # lavanda, na frente
    (0.62, 1.30, 0.97, 0.58, 1.10, "d2c4ee", "efe2ef", 0.55, 0.28),   # lilás claro, frente direita
]
for p in picos:
    montanha(*p)

# névoa suave embaixo: as bases somem no claro
n = cairo.LinearGradient(0, H * 0.72, 0, H)
n.add_color_stop_rgba(0, *cor("f4eef8" if not ESCURO else "1b1d3a", 0))
n.add_color_stop_rgba(1, *cor("f4eef8" if not ESCURO else "1b1d3a", 0.55))
cr.set_source(n)
cr.paint()
s.write_to_png(sys.argv[1])
