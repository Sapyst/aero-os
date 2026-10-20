#!/usr/bin/env python3
"""make-frames.py: the Venus de Milo in ASCII, animated (wave + marble shimmer + glitch), as PNG frames.

Used everywhere the Aero OS starts: the boot splash (Plymouth), the login screen and the desktop's
first second, so the three look like one continuous animation.

  python3 make-frames.py OUT_DIR [--frames 40] [--size 720]

Writes frame-00.png ... (a seamless loop) and still.png (the calm statue).
The ASCII comes from statue.py (public-domain photo of the Venus de Milo, Louvre, Wikimedia Commons).
"""
# Como funciona: a foto vira uma grade de caracteres com um tom de cinza cada (statue.py). Cada
# quadro desenha essa grade com três efeitos que fecham um ciclo perfeito (o último quadro emenda
# no primeiro): uma onda que desce pela estátua (cada linha desliza um pouco para o lado), um
# brilho que troca alguns caracteres por vizinhos mais claros/escuros, e de vez em quando um glitch
# (faixas deslocadas com as cores separadas, vermelho e ciano).
import argparse
import math
import os
import random
import sys

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
from statue import art  # noqa: E402

RAMP = " .,:;-~=+*o%#@"
COLS = 112
CROP = (0.22, 0.0, 0.78, 0.46)          # cabeça e ombros, como no cartão de perfil
FONT = "Adwaita Mono, JetBrains Mono, monospace"


def tone(g):
    """Gray of the marble (0..1) -> RGB, a hair warm, never pure white."""
    v = 0.16 + g * 0.80
    return v, v, v * 0.97


def draw(ctx, grid, w, h, t, n, seed, cell_w, cell_h, x0, y0, layout):
    rnd = random.Random(seed * 1000 + t)
    rows = len(grid)
    phase = t / n
    # Faixas de glitch: só em alguns quadros do ciclo (sempre os mesmos, então o ciclo fecha).
    glitch = []
    if t in (9, 10, 27):
        g = random.Random(seed + t)
        for _ in range(g.randint(2, 4)):
            r0 = g.randrange(rows)
            glitch.append((r0, r0 + g.randint(1, 4), g.choice((-1, 1)) * g.uniform(6, 22)))
    # Fundo transparente: quem mostra decide o preto (o boot, o login e o desktop).
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    for r, line in enumerate(grid):
        wave = math.sin(2 * math.pi * (r / rows * 1.4 - phase)) * cell_w * 0.55
        shift = next((d for a, b, d in glitch if a <= r < b), 0.0)
        for c, (ch, g) in enumerate(line):
            if ch == " ":
                continue
            # Brilho do mármore: uns poucos caracteres trocam por um vizinho da escala.
            if rnd.random() < 0.035:
                i = RAMP.find(ch)
                ch = RAMP[max(1, min(len(RAMP) - 1, i + rnd.choice((-1, 1))))]
                g = min(1.0, g + 0.12)
            x = x0 + c * cell_w + wave + shift
            y = y0 + r * cell_h
            layout.set_text(ch, -1)
            if shift:
                for (cr, cg, cb), dx in (((1.0, 0.2, 0.3), -2.0), ((0.2, 0.9, 1.0), 2.0)):
                    ctx.set_source_rgba(cr, cg, cb, 0.55)
                    ctx.move_to(x + dx, y)
                    PangoCairo.show_layout(ctx, layout)
            ctx.set_source_rgb(*tone(g))
            ctx.move_to(x, y)
            PangoCairo.show_layout(ctx, layout)
    # Linhas de varredura bem leves, como num monitor antigo (só sobre o que já está desenhado).
    ctx.set_operator(cairo.OPERATOR_ATOP)
    ctx.set_source_rgba(0, 0, 0, 0.18)
    for y in range(0, h, 3):
        ctx.rectangle(0, y, w, 1)
    ctx.fill()
    ctx.set_operator(cairo.OPERATOR_OVER)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--size", type=int, default=720)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    grid = art(os.path.join(AQUI, "venus-de-milo.jpg"), COLS, CROP)
    w = h = a.size
    cell_w = w * 0.78 / COLS
    cell_h = cell_w * 2.0
    x0 = (w - cell_w * COLS) / 2
    y0 = (h - cell_h * len(grid)) / 2
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    ctx = cairo.Context(surf)
    layout = PangoCairo.create_layout(ctx)
    fd = Pango.FontDescription.from_string(FONT)
    fd.set_absolute_size(cell_w / 0.6 * Pango.SCALE)
    layout.set_font_description(fd)
    for t in range(a.frames):
        draw(ctx, grid, w, h, t, a.frames, 7, cell_w, cell_h, x0, y0, layout)
        surf.write_to_png(os.path.join(a.out, f"frame-{t:02d}.png"))
    # O quadro parado: sem onda, sem brilho, sem glitch (fim do login, começo do desktop).
    rnd_free = [[(ch, g) for ch, g in line] for line in grid]
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    for r, line in enumerate(rnd_free):
        for c, (ch, g) in enumerate(line):
            if ch != " ":
                layout.set_text(ch, -1)
                ctx.set_source_rgb(*tone(g))
                ctx.move_to(x0 + c * cell_w, y0 + r * cell_h)
                PangoCairo.show_layout(ctx, layout)
    surf.write_to_png(os.path.join(a.out, "still.png"))
    print(f"{a.frames} frames + still.png in {a.out} ({w}x{h}, {len(grid)} rows)")


if __name__ == "__main__":
    main()
