#!/usr/bin/env python3
"""make-goddesses.py: the goddesses of the System widget ("goddess of the day").

For each Greek goddess, one statue whose photo is free to reuse (CC0, public domain, or CC BY with
credit; never share-alike, so the art can live in this repo). Its ASCII rendering comes from
github.com/Sapyst/ascii-walls (walls/mythology), cropped to the statue and made small.
Writes shell/deusas/<name>.png and shell/deusas.json (name, Roman name, what she rules, the statue,
the photo's credit). The credits also go to the README.

  python3 tools/make-goddesses.py CATALOG.csv ASCII_WALLS_DIR
"""
# Cada deusa: a primeira estátua da coleção com licença livre de verdade (CC0 / domínio público,
# senão CC BY com crédito). CC BY-SA fica de fora: obrigaria a arte derivada a ter a mesma licença.
import csv
import json
import os
import re
import sys
import unicodedata

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf  # noqa: E402

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALTURA = 200          # px: nítida até a escala "Larger" do widget

# pasta da coleção -> (pasta no ascii-walls, nome, nome romano, o que ela rege)
DEUSAS = {
    "Atena": ("athena", "Athena", "Minerva", "wisdom & war"),
    "Afrodite": ("aphrodite", "Aphrodite", "Venus", "love & beauty"),
    "Ártemis": ("artemis", "Artemis", "Diana", "the hunt & moon"),
    "Hera": ("hera", "Hera", "Juno", "queen of the gods"),
    "Deméter": ("demeter", "Demeter", "Ceres", "the harvest"),
    "Héstia": ("hestia", "Hestia", "Vesta", "hearth & home"),
    "Perséfone": ("persephone", "Persephone", "Proserpina", "the underworld"),
    "Nike": ("nike", "Nike", "Victoria", "victory"),
    "Tique": ("tyche", "Tyche", "Fortuna", "fortune"),
    "Higeia": ("hygieia", "Hygieia", "Salus", "health"),
    "Têmis": ("themis", "Themis", "Justitia", "law & justice"),
    "Eos": ("eos", "Eos", "Aurora", "the dawn"),
    "Hebe": ("hebe", "Hebe", "Juventas", "youth"),
    "Selene": ("selene", "Selene", "Luna", "the moon"),
    "Hécate": ("hecate", "Hecate", "Trivia", "magic & crossroads"),
    "Nêmesis": ("nemesis", "Nemesis", "Invidia", "retribution"),
    "Éris": ("eris", "Eris", "Discordia", "strife & discord"),
}
LIVRE = ("CC0", "Public domain")
COM_CREDITO = ("CC BY 2.0", "CC BY 2.5", "CC BY 3.0", "CC BY 4.0")


def slug(texto):
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")


def recortar(caminho):
    """A estátua sem o preto em volta, pequena."""
    p = GdkPixbuf.Pixbuf.new_from_file(caminho)
    p = p.scale_simple(p.get_width() // 4, p.get_height() // 4, GdkPixbuf.InterpType.BILINEAR)
    w, h, n, st, d = p.get_width(), p.get_height(), p.get_n_channels(), p.get_rowstride(), p.get_pixels()
    xs, ys = [], []
    for y in range(0, h, 2):
        for x in range(0, w, 2):
            i = y * st + x * n
            if d[i] + d[i + 1] + d[i + 2] > 90:
                xs.append(x)
                ys.append(y)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    p = p.new_subpixbuf(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
    p = p.scale_simple(max(1, round(p.get_width() * ALTURA / p.get_height())), ALTURA, GdkPixbuf.InterpType.HYPER)
    # Pequena, a arte fica escura: clareia (curva suave, o preto continua preto).
    w, h, n, st = p.get_width(), p.get_height(), p.get_n_channels(), p.get_rowstride()
    d = bytearray(p.get_pixels())
    for y in range(h):
        for x in range(w):
            i = y * st + x * n
            for k in range(3):
                v = d[i + k] / 255
                d[i + k] = min(255, int(255 * (v ** 0.6) * 1.15))
    return GdkPixbuf.Pixbuf.new_from_bytes(__import__("gi.repository.GLib", fromlist=["Bytes"]).Bytes.new(bytes(d)),
                                           GdkPixbuf.Colorspace.RGB, p.get_has_alpha(), 8, w, h, st)


def main():
    catalogo, walls = sys.argv[1], sys.argv[2]
    os.makedirs(os.path.join(AQUI, "shell", "deusas"), exist_ok=True)
    linhas = list(csv.DictReader(open(catalogo, encoding="utf-8")))
    saida = []
    for pasta, (pasta_walls, nome, romano, rege) in DEUSAS.items():
        def wall(l):
            return os.path.join(walls, "walls", "mythology", pasta_walls, slug(l["nome"]) + "-mobile.png")
        obras = [l for l in linhas if l["deusa"] == pasta and os.path.exists(wall(l))]
        obra = next((l for l in obras if l["licenca"].strip() in LIVRE), None) or \
            next((l for l in obras if l["licenca"].strip() in COM_CREDITO), None)
        if not obra:
            print(f"{nome}: no free photo, skipped", file=sys.stderr)
            continue
        imagem = recortar(wall(obra))
        imagem.savev(os.path.join(AQUI, "shell", "deusas", nome.lower() + ".png"), "png", [], [])
        saida.append({
            "nome": nome, "romano": romano, "rege": rege,
            "obra": obra["nome"], "cidade": obra["cidade"], "imagem": nome.lower() + ".png",
            "credito": {"licenca": obra["licenca"].strip(), "fotografo": obra["fotografo"].strip(), "fonte": obra["fonte"].strip()},
        })
        print(f"{nome}: {obra['nome']} ({obra['licenca']}) {imagem.get_width()}x{imagem.get_height()}")
    with open(os.path.join(AQUI, "shell", "deusas.json"), "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
