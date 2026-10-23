#!/usr/bin/env python3
"""
aerobar.py — AeroBar, the Aero OS top bar (DotStyle). Command: aerobar.

Same modules and the same clicks as the Waybar setup, drawn in dots: 7×7 dot icons, a dot volume meter,
workspaces as little dot squares, numbers in Doto. The clock sits at the exact middle (both sides get
the same width). One drawing area for the whole bar, redrawn only when something changes; the battery
pulse is the only animation, and only while charging.

Data comes from the system directly (no polling loop for what can be followed):
  workspaces, keyboard layout  Hyprland's event socket
  volume                       pactl subscribe
  power profile                net.hadess.PowerProfiles over D-Bus
  battery                      /sys/class/power_supply (every 5 s)
  Wi-Fi, Bluetooth             nmcli / bluetoothctl (every 10 s)
  minimized, UltraSu           their own scripts (every 2 s / 15 s), like before
  notifications                Hermes (com.aero.Hermes) every 2 s

Its layer is "aerobar"; the tray (bandeja.py) and Hermes read ~/.config/aero/aerobar.json to line up with it.

No visual Solid (solido.py) a mesma barra vira Material: três ilhas em pílula (surface container), ícones
Material (os "md" da Nerd Font), texto normal em Inter, áreas de trabalho como pontinhos com a ativa em pílula,
bateria deitada estilo Pixel e a dica de hover numa pílula sólida que nasce do ícone num metaball. As cores
vêm da paleta tonal e mudam ao vivo (solido.vigiar). No DotStyle continua tudo em pontos.
"""
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import solido  # noqa: E402

SOLIDO = solido.ativo()          # visual Solid: a barra Material (lido ao iniciar; trocar de visual reinicia a barra)

H = os.path.expanduser("~")
BIN = os.path.join(H, ".local/bin")
BANDEJA = os.path.join(H, ".cache/aero-shell/bandeja.json")
VISTO = os.path.join(H, ".cache/aero-shell/sino-visto")      # last notification id seen in the bell
CONFIG = os.path.join(H, ".config/aero/aerobar.json")
# Everything the person can change (Settings -> AeroBar, or the JSON by hand). Bad values fall back.
PADRAO = {
    "background": "none",       # none (invisible: only the icons float) | glass (see-through) | solid
    "opacity": 0.9,             # of the background, when there is one
    "outline": False,           # the thin line around the bar
    "radius": 14,               # curve of the corners (also the shape of the hover highlight area)
    "height": 34, "margin_top": 0, "margin_sides": 0,       # 0 = glued to the top of the screen
    "date": True,               # the date next to the clock
    "icon_size": 15, "clock_size": 17, "number_size": 15,
    "hover": True,              # the soft highlight under the pointer
    "hide": [],                 # modules to hide: workspaces min clock lang vol wifi bt bat ultrasu bell gear central
    "colors": {"text": "#ecebe8", "dim": "#8c8b88", "alert": "#ff3b30", "plugged": "#4cc2ff",
               "saver": "#ffd23f", "good": "#3dff9a", "background": "#0b0b0c"},
    "glow": True,               # coloured dots light up with a soft halo
    "auto_contrast": True,      # read the wallpaper under each icon: dark icons on light parts, light on dark
}
OCULTAVEIS = {"workspaces", "min", "clock", "lang", "vol", "wifi", "bt", "bat", "ultrasu", "bell", "gear", "central"}


def _hex(v, padrao):
    if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v):
        return tuple(int(v[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return _hex(padrao, "#ffffff") if padrao != v else (1, 1, 1)


def ler_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            bruto = json.load(f)
        bruto = bruto if isinstance(bruto, dict) else {}
    except (OSError, ValueError):
        bruto = {}
    c = json.loads(json.dumps(PADRAO))
    if bruto.get("background") in ("solid", "glass", "none"):
        c["background"] = bruto["background"]
    for k, lo, hi in (("opacity", 0.0, 1.0),):
        if isinstance(bruto.get(k), (int, float)) and not isinstance(bruto.get(k), bool):
            c[k] = min(hi, max(lo, float(bruto[k])))
    for k, lo, hi in (("radius", 0, 30), ("height", 26, 60), ("margin_top", 0, 40), ("margin_sides", 0, 200),
                      ("icon_size", 10, 28), ("clock_size", 10, 32), ("number_size", 10, 28)):
        if isinstance(bruto.get(k), int) and not isinstance(bruto.get(k), bool):
            c[k] = min(hi, max(lo, bruto[k]))
    for k in ("outline", "date", "hover", "glow", "auto_contrast"):
        if isinstance(bruto.get(k), bool):
            c[k] = bruto[k]
    if isinstance(bruto.get("hide"), list):
        c["hide"] = [x for x in bruto["hide"] if x in OCULTAVEIS]
    if isinstance(bruto.get("colors"), dict):
        for k in c["colors"]:
            v = bruto["colors"].get(k)
            if isinstance(v, str) and re.match(r"^#[0-9a-fA-F]{6}$", v):
                c["colors"][k] = v
    return c


CFG = ler_config()
ALTURA, TOPO, LADO = CFG["height"], CFG["margin_top"], CFG["margin_sides"]
VAZAO = 22          # extra see-through room under the bar so glows fade out naturally instead of being cut
LARGURA_ICONE_BANDEJA = 30

FG = _hex(CFG["colors"]["text"], PADRAO["colors"]["text"])
DIM = _hex(CFG["colors"]["dim"], PADRAO["colors"]["dim"])
RED = _hex(CFG["colors"]["alert"], PADRAO["colors"]["alert"])
BLUE = _hex(CFG["colors"]["plugged"], PADRAO["colors"]["plugged"])
YELLOW = _hex(CFG["colors"]["saver"], PADRAO["colors"]["saver"])
FUNDO = _hex(CFG["colors"]["background"], PADRAO["colors"]["background"])
ICONE = CFG["icon_size"]

# two palettes: the user's colours for dark backgrounds, and strong versions for light ones
ESCURA = {"FG": FG, "DIM": DIM, "RED": RED, "BLUE": BLUE, "YELLOW": YELLOW,
          "GREEN": _hex(CFG["colors"]["good"], PADRAO["colors"]["good"])}
CLARA = {"FG": (0.07, 0.07, 0.08), "DIM": (0.28, 0.28, 0.27), "RED": (0.85, 0.09, 0.06),
         "BLUE": (0.0, 0.42, 0.95), "YELLOW": (0.72, 0.5, 0.0), "GREEN": (0.0, 0.6, 0.3)}


def usar_paleta(k):
    """Drawing colours for the next module: k = 0 dark-background palette, 1 light-background palette,
    anything between while the bar is changing (the colours blend)."""
    global FG, DIM, RED, BLUE, YELLOW, GREEN
    k = float(k)

    def mix(nome):
        a, b = ESCURA[nome], CLARA[nome]
        return tuple(a[i] + (b[i] - a[i]) * k for i in range(3))
    FG, DIM, RED, BLUE, YELLOW, GREEN = (mix(n) for n in ("FG", "DIM", "RED", "BLUE", "YELLOW", "GREEN"))


def luz_do_papel(caminho, largura, altura_tela, faixa, colunas=256):
    """Brightness (0..1) along the top strip of the wallpaper as the screen shows it (fit_mode cover):
    a list of `colunas` values left to right. None if the picture can't be read."""
    try:
        from gi.repository import GdkPixbuf
        info = GdkPixbuf.Pixbuf.get_file_info(caminho)
        iw, ih = info[1], info[2]
        if not iw or not ih:
            return None
        esc = max(largura / iw, altura_tela / ih)            # cover: fills the screen, crops the rest
        vis_w, vis_h = largura / esc, altura_tela / esc       # part of the picture that is visible
        x0, y0 = (iw - vis_w) / 2, (ih - vis_h) / 2
        alvo_w = colunas
        alvo_h = max(4, int(colunas * ih / iw))
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(caminho, int(alvo_w * iw / vis_w), -1, True)
        k = pb.get_width() / iw
        d, rs, n = pb.get_pixels(), pb.get_rowstride(), pb.get_n_channels()
        y_a, y_b = int(y0 * k), max(int(y0 * k) + 1, int((y0 + faixa / esc) * k))
        valores = []
        for c in range(colunas):
            x = int((x0 + (c + 0.5) / colunas * vis_w) * k)
            x = min(pb.get_width() - 1, max(0, x))
            soma = 0.0
            for y in range(y_a, min(y_b, pb.get_height())):
                i = y * rs + x * n
                soma += (d[i] * 0.2126 + d[i + 1] * 0.7152 + d[i + 2] * 0.0722) / 255
            valores.append(soma / max(1, min(y_b, pb.get_height()) - y_a))
        return valores
    except (GLib.Error, OSError, IndexError, ZeroDivisionError, TypeError):
        return None
GREEN = _hex(CFG["colors"]["good"], PADRAO["colors"]["good"])
F_RELOGIO = f"Doto Black {CFG['clock_size']}"
F_NUMERO = f"Doto Black {CFG['number_size']}"

# 7×7 dot icons: "#" lit, "+" half, "." off
ICONES = {
    "vol":   ["...#...", "..##...", "####.+.", "####..+", "####.+.", "..##...", "...#..."],
    "mute":  ["...#...", "..##...", "####...", "####.#.#", "####..#.", "..##.#.#", "...#..."],
    "bt":    ["..##...", "..#.#..", "#.#..#.", ".###...", "#.#..#.", "..#.#..", "..##..."],
    "shield": ["..###..", ".#####.", ".##.##.", ".##.##.", ".#####.", "..###..", "...#..."],
    "bell":  ["...#...", "..###..", ".#####.", ".#####.", ".#####.", "#######", "...#..."],
    "bellx": ["...#...", "..#.#..", ".#...#.", ".#...#.", ".#...#.", "#######", "...#..."],
    "gear":  ["...#...", ".#####.", ".##.##.", "###.###", ".##.##.", ".#####.", "...#..."],
    "grid":  ["##.##..", "##.##..", ".......", "##.##..", "##.##..", ".......", "......."],
    "min":   [".......", ".......", ".......", ".......", ".......", "#######", "#######"],
}


def wifi_icone(nivel):
    """Wi-Fi as stepped arcs of dots: nivel 0..4 lit steps (0 = disconnected, all faint)."""
    linhas = [".#####.", "#.....#", "..###..", ".#...#.", "...#...", "...#...", "......."]
    passos = {0: (0, 1), 1: (2, 3), 2: (4,), 3: (5,)}           # which rows belong to which step (outer->inner)
    acesas = set()
    for p in range(4):
        if nivel >= 4 - p:
            acesas.update(passos[p])
    acesas.update(passos[3] if nivel > 0 else ())
    return ["".join((c if (i in acesas or c == ".") else ("+" if c == "#" else c)) for c in linha)
            for i, linha in enumerate(linhas)]


def minutos_ultrasu(d):
    """Minutes of admin access left, from UltraSu's bar text ("22h 18m", "35m"), or None if not open."""
    if not isinstance(d, dict) or d.get("class") not in ("ativo", "acabando"):
        return None
    t = str(d.get("text", ""))
    m = re.search(r"(\d+)\s*h\s*(\d+)\s*m", t) or re.search(r"(\d+)\s*h", t)
    if m:
        return int(m.group(1)) * 60 + (int(m.group(2)) if m.lastindex and m.lastindex >= 2 else 0)
    m = re.search(r"(\d+)\s*m", t)
    return int(m.group(1)) if m else 0


def cor_ultrasu(minutos):
    """Time left as a colour: green > 15 h, blue 7–15 h, yellow 3–7 h, red < 3 h."""
    if minutos is None:
        return None
    h = minutos / 60
    return GREEN if h > 15 else BLUE if h > 7 else YELLOW if h > 3 else RED


def suave_(a, b, x):
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


def rodar(*cmd, timeout=4):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def lancar(cmd):
    """Runs a click command like Waybar did (through the shell, with ~ expanded), detached."""
    subprocess.Popen(["sh", "-c", cmd.replace("~/", H + "/")], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)


# ---------- drawing helpers ----------

def halo(cr, x, y, raio, cor, forca=0.55):
    """A soft glow behind a coloured dot: a gaussian fall-off in many steps, so it melts into whatever
    is behind with no visible edge."""
    import cairo
    raio *= 1.35
    g = cairo.RadialGradient(x, y, 0, x, y, raio)
    for k in range(13):
        t = k / 12
        g.add_color_stop_rgba(t, *cor, forca * 0.8 * math.exp(-4.2 * t * t) * (1 - t ** 6))
    cr.set_source(g)
    cr.arc(x, y, raio, 0, 2 * math.pi)
    cr.fill()


def colorida(cor):
    return CFG["glow"] and cor not in (FG, DIM) and not CLARO_AGORA[0]


CLARO_AGORA = [False]          # glows only on dark backgrounds (on light ones they look muddy)


def icone(cr, nome_ou_linhas, x, y, tamanho, cor, alfa=1.0):
    linhas = ICONES[nome_ou_linhas] if isinstance(nome_ou_linhas, str) else nome_ou_linhas
    p = tamanho / 7
    if colorida(cor) and alfa > 0.5:                      # one halo behind the whole icon, then the dots
        halo(cr, x + tamanho / 2, y + tamanho / 2, tamanho * 0.95, cor, 0.32 * alfa)
    for j, linha in enumerate(linhas):
        for i, ch in enumerate(linha[:7]):
            a = 1.0 if ch == "#" else 0.45 if ch == "+" else 0.1
            cr.set_source_rgba(*cor, a * alfa)
            cr.arc(x + p * (i + 0.5), y + p * (j + 0.5), p * 0.36, 0, 2 * math.pi)
            cr.fill()


def texto(cr, area, s, x, y_meio, fonte, cor, alfa=1.0, medir=False):
    """Pango text, vertically centred on y_meio. Returns the width."""
    layout = PangoCairo.create_layout(cr)
    layout.set_font_description(Pango.FontDescription.from_string(fonte))
    layout.set_text(s, -1)
    w, h = layout.get_pixel_size()
    if not medir:
        cr.set_source_rgba(*cor, alfa)
        cr.move_to(x, y_meio - h / 2)
        PangoCairo.show_layout(cr, layout)
    return w


# ---------- the bar ----------

class Modulo:
    """One clickable piece. largura(cr) measures it, desenhar(cr, x) draws it at x."""

    def __init__(self, barra, nome, cliques=None, dica=""):
        self.barra, self.nome = barra, nome
        self.cliques = cliques or {}
        self.dica = dica
        self.x, self.w = 0, 0
        self.visivel = True

    def largura(self, cr):
        return 0

    def desenhar(self, cr, x):
        pass

    def clique(self, botao, x):
        cmd = self.cliques.get(botao)
        if callable(cmd):
            cmd()
        elif cmd:
            lancar(cmd)

    def rolar(self, dy):
        pass


class Workspaces(Modulo):
    TAM, GAP = 15, 6

    def largura(self, cr):
        return 5 * self.TAM + 4 * self.GAP + 10

    def desenhar(self, cr, x):
        b = self.barra
        y = ALTURA / 2 - self.TAM / 2
        for k in range(5):
            n = k + 1
            ativo, usado = n == b.ws_ativo, n in b.ws_usados
            p = self.TAM / 5
            for i in range(5):
                for j in range(5):
                    borda = i in (0, 4) or j in (0, 4)
                    a = 1.0 if ativo else (0.5 if borda else 0.18) if usado else 0.1
                    cr.set_source_rgba(*FG, a)
                    cr.arc(x + 5 + k * (self.TAM + self.GAP) + p * (i + 0.5), y + p * (j + 0.5), p * 0.34, 0, 2 * math.pi)
                    cr.fill()

    def clique(self, botao, x):
        k = int((x - self.x - 5) // (self.TAM + self.GAP))
        if 0 <= k < 5 and botao == 1:
            lancar(f"hyprctl dispatch workspace {k + 1}")

    def rolar(self, dy):
        lancar(f"hyprctl dispatch workspace {'r-1' if dy < 0 else 'r+1'}")


class Minimizados(Modulo):
    def largura(self, cr):
        n = self.barra.minimizados
        self.visivel = n > 0
        return 0 if not n else ICONE + 6 + texto(cr, None, str(n), 0, 0, F_NUMERO, FG, medir=True) + 16

    def desenhar(self, cr, x):
        icone(cr, "min", x + 8, ALTURA / 2 - ICONE / 2, ICONE, FG)
        texto(cr, None, str(self.barra.minimizados), x + 8 + ICONE + 6, ALTURA / 2, F_NUMERO, FG)


class Relogio(Modulo):
    def _partes(self):
        agora = time.localtime()
        return time.strftime("%H:%M", agora), time.strftime("%a %b %d", agora).upper()

    def largura(self, cr):
        hora, data = self._partes()
        w = texto(cr, None, hora, 0, 0, F_RELOGIO, FG, medir=True) + 16
        if CFG["date"]:
            w += 10 + texto(cr, None, data, 0, 0, "Geist Mono 10", DIM, medir=True)
        return w

    def desenhar(self, cr, x):
        hora, data = self._partes()
        w = texto(cr, None, hora, x + 8, ALTURA / 2, F_RELOGIO, FG)
        if CFG["date"]:
            texto(cr, None, data, x + 8 + w + 10, ALTURA / 2 + 1, "Geist Mono 10", DIM)


class Idioma(Modulo):
    def largura(self, cr):
        return texto(cr, None, self.barra.idioma, 0, 0, F_NUMERO, FG, medir=True) + 16

    def desenhar(self, cr, x):
        texto(cr, None, self.barra.idioma, x + 8, ALTURA / 2, F_NUMERO, FG)


class Volume(Modulo):
    def largura(self, cr):
        return 8 + ICONE + 6 + 34 + 8

    def desenhar(self, cr, x):
        b = self.barra
        icone(cr, "mute" if b.mudo else "vol", x + 8, ALTURA / 2 - ICONE / 2, ICONE, FG)
        mx = x + 8 + ICONE + 6
        for i in range(10):
            for j in range(2):
                aceso = not b.mudo and (i + 1) / 10 <= b.volume / 100 + 0.001
                cr.set_source_rgba(*(RED if b.mudo else FG), 0.95 if aceso else (0.5 if b.mudo and j == 0 else 0.12))
                cr.arc(mx + 2 + i * 3.4, ALTURA / 2 - 2 + j * 4, 1.2, 0, 2 * math.pi)
                cr.fill()

    def rolar(self, dy):
        lancar(f"pactl set-sink-volume @DEFAULT_SINK@ {'+' if dy < 0 else '-'}5%")


class IconeSimples(Modulo):
    def __init__(self, barra, nome, desenho, cliques=None, cor=lambda: FG, alfa=lambda: 1.0, dica=""):
        super().__init__(barra, nome, cliques, dica)
        self.desenho, self.cor, self.alfa = desenho, cor, alfa

    def largura(self, cr):
        return 8 + ICONE + 8

    def desenhar(self, cr, x):
        d = self.desenho() if callable(self.desenho) else self.desenho
        icone(cr, d, x + 8, ALTURA / 2 - ICONE / 2, ICONE, self.cor(), self.alfa())


class Sino(IconeSimples):
    def desenhar(self, cr, x):
        super().desenhar(cr, x)
        if self.barra.nao_lidas and not self.barra.dnd:
            if CFG["glow"]:
                halo(cr, x + 8 + ICONE, ALTURA / 2 - ICONE / 2 + 0.5, 8, RED, 0.6)
            cr.set_source_rgba(*RED, 1)
            cr.arc(x + 8 + ICONE, ALTURA / 2 - ICONE / 2 + 0.5, 3, 0, 2 * math.pi)
            cr.fill()


class Bateria(Modulo):
    COLS, ROWS = 11, 5

    def largura(self, cr):
        return 8 + 26 + 6 + texto(cr, None, str(self.barra.bat_nivel), 0, 0, F_NUMERO, FG, medir=True) + 8

    def cor(self):
        b = self.barra
        if b.bat_nivel <= 12 and not b.na_tomada:
            return RED
        if b.perfil == "power-saver":
            return YELLOW
        return FG

    def desenhar(self, cr, x):
        b = self.barra
        cor = self.cor()
        W, Hh = 26, 15
        if colorida(cor):
            halo(cr, x + 8 + W / 2, ALTURA / 2, W * 0.75, cor, 0.3)
        p = W / (self.COLS + 1.2)
        oy = ALTURA / 2 - Hh / 2 + (Hh - (self.ROWS - 1) * p) / 2
        ox = x + 8
        cheio = round(b.bat_nivel / 100 * (self.COLS - 2))
        pulso = ((time.monotonic() / 1.4) % 1) * (cheio + 2) - 1 if b.carregando else -9
        for i in range(self.COLS):
            for j in range(self.ROWS):
                casca = i in (0, self.COLS - 1) or j in (0, self.ROWS - 1)
                dentro = not casca and i - 1 < cheio
                a = 0.95 if casca else 0.8 if dentro else 0.1
                if dentro and pulso > -9:
                    a = 0.45 + 0.55 * max(0.0, 1 - abs(i - pulso) / 1.6)
                cr.set_source_rgba(*cor, a)
                cr.arc(ox + p * (i + 0.6), oy + j * p, p * 0.34, 0, 2 * math.pi)
                cr.fill()
        for j in (1, 2, 3):
            cr.set_source_rgba(*cor, 0.95)
            cr.arc(ox + p * (self.COLS + 0.6), oy + j * p, p * 0.34, 0, 2 * math.pi)
            cr.fill()
        cor_num = BLUE if b.na_tomada else cor
        if colorida(cor_num):
            tw = texto(cr, None, str(b.bat_nivel), 0, 0, F_NUMERO, cor_num, medir=True)
            halo(cr, x + 8 + W + 6 + tw / 2, ALTURA / 2, max(tw, 14) * 0.9, cor_num, 0.3)
        texto(cr, None, str(b.bat_nivel), x + 8 + W + 6, ALTURA / 2, F_NUMERO, cor_num)


class EspacoBandeja(Modulo):
    def largura(self, cr):
        return self.barra.n_bandeja * LARGURA_ICONE_BANDEJA


class Barra(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, decorated=False)
        self.add_css_class("aerobar")
        prov = Gtk.CssProvider()            # the window itself paints nothing: the bar draws its own background
        prov.load_from_string("window.aerobar, .aerobar { background: none; background-color: transparent; "
                              "box-shadow: none; border: none; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.TOP)
        Camada.set_namespace(self, "aerobar")
        for borda in (Camada.Edge.TOP, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_margin(self, Camada.Edge.TOP, TOPO)
        Camada.set_margin(self, Camada.Edge.LEFT, LADO)
        Camada.set_margin(self, Camada.Edge.RIGHT, LADO)
        # windows start under the bar, not under the glow room (Solid: right at the islands' bottom edge, so a
        # panel opening under an island touches it and can grow out of it with a liquid neck)
        Camada.set_exclusive_zone(self, ALTURA - 3 if SOLIDO else ALTURA)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.set_default_size(-1, ALTURA + VAZAO)
        self.area = Gtk.DrawingArea(content_height=ALTURA + VAZAO, hexpand=True)
        self.area.connect("resize", lambda _a, w, _h: self._entrada(w))
        self.area.set_draw_func(self.desenhar)
        # no GTK tooltips: every icon explains itself with the dot drop (Gota) after a short hover
        self.set_child(self.area)

        # state
        self.ws_ativo, self.ws_usados = 1, set()
        self.minimizados, self.idioma = 0, "US"
        self.volume, self.mudo = 0, False
        self.wifi_nivel, self.wifi_nome = 0, ""
        self.bt_ligado, self.bt_conectados = False, 0
        self.bat_nivel, self.na_tomada, self.carregando, self.perfil = 0, False, False, "balanced"
        self.bat_dica = ""
        self.ultrasu = {"class": "", "tooltip": ""}
        self.dnd, self.nao_lidas, self.ultimo_id = False, False, 0
        self.n_bandeja = 0
        self.sobre = None
        self.pulso = None
        self.papel, self.luz = "", None
        self.mistura, self.alvos, self.trocas, self.tique_troca = {}, {}, {}, None

        cfg = os.path.join(BIN, "configuracoes")
        som = os.path.join(BIN, "som")
        self.esquerda = [Workspaces(self, "workspaces"),
                         Minimizados(self, "min", {1: f"{BIN}/hypr-minimizados"}, "Minimized windows")]
        self.centro = [Relogio(self, "clock", {1: lambda: self.calendario.alternar(self.centro[0])})]
        self.direita = [
            Idioma(self, "lang", {1: "hyprctl switchxkblayout all next"}, "Keyboard layout"),
            Volume(self, "vol", {1: f"{som} popup simples", 3: f"{som} popup completo",
                                 2: "pactl set-sink-mute @DEFAULT_SINK@ toggle"}),
            IconeSimples(self, "wifi", lambda: wifi_icone(self.wifi_nivel),
                         {1: f"{som} central abrir", 3: f"CONFIG_ABA=rede {cfg}"},
                         alfa=lambda: 1.0 if self.wifi_nivel else 0.5),
            IconeSimples(self, "bt", "bt", {1: f"{som} central abrir", 3: f"CONFIG_ABA=bluetooth {cfg}"},
                         alfa=lambda: 1.0 if self.bt_conectados else 0.6 if self.bt_ligado else 0.3),
            Bateria(self, "bat", {1: f"{BIN}/aero-energia --alternar", 2: f"{som} central abrir",
                                  3: f"CONFIG_ABA=energia {cfg}"}),
            IconeSimples(self, "ultrasu", "shield", {1: f"{BIN}/ultrasu"},
                         cor=lambda: cor_ultrasu(minutos_ultrasu(self.ultrasu)) or FG,
                         alfa=lambda: 1.0 if minutos_ultrasu(self.ultrasu) is not None else 0.45),
            Sino(self, "bell", lambda: "bellx" if self.dnd else "bell", {1: f"{som} notificacoes abrir"},
                 alfa=lambda: 0.55 if self.dnd else 1.0, dica="Hermes"),
            IconeSimples(self, "gear", "gear", {1: cfg}, dica="Settings"),
            EspacoBandeja(self, "tray"),
            IconeSimples(self, "central", "grid", {1: f"{som} central abrir", 3: cfg}, dica="Hestia"),
        ]

        clique = Gtk.GestureClick(button=0)
        clique.connect("released", self.ao_clicar)
        self.area.add_controller(clique)
        rolar = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        rolar.connect("scroll", self.ao_rolar)
        self.area.add_controller(rolar)
        mov = Gtk.EventControllerMotion()
        mov.connect("motion", lambda _c, x, _y: self.mudar_sobre(self.modulo_em(x)))
        mov.connect("leave", lambda *_: self.mudar_sobre(None))
        self.area.add_controller(mov)

        self.molas_s, self.gotas_s, self.tique_s, self.ultimo_s, self._anotado_s = {}, [], None, None, None
        self.ws_gotas, self.ws_antes = [], None   # Solid: gotas dos workspaces que nascem/somem
        self.gota = GotaSolida() if SOLIDO else Gota()
        if SOLIDO:                                # paleta nova (wallpaper, claro/escuro): só redesenha
            self._vigia_cores = solido.vigiar(lambda: (self.redesenhar(), self.gota.area.queue_draw()), peca="aerobar")
        self.calendario = CalendarioSolido() if SOLIDO else Calendario()
        # hypr-botoes (the click watcher) sends SIGUSR1 when a click lands outside an open popup:
        # in Hyprland an invisible layer can't catch clicks, so this is how "click outside closes" works
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                             lambda: (self.calendario.esconder(), True)[1])
        self._sino_escrito = None
        self.iniciar_fontes()
        self.present()

    def _entrada(self, largura):
        """Only the bar itself takes clicks; the glow room below lets every click through."""
        import cairo
        sup = self.get_surface()
        if sup is not None:
            sup.set_input_region(cairo.Region(cairo.RectangleInt(0, 0, int(largura), ALTURA)))

    # ---------- layout / drawing ----------

    def anotar_sino(self, w):
        """Tells Hermes exactly where the bell is (its wave and cards start there):
        ~/.cache/aero-shell/sino.json, rewritten only when the bell moves."""
        sino = next((m for m in self.direita if m.nome == "bell"), None)
        if sino is None or sino.w <= 0:
            return
        ponto = (round(LADO + sino.x + sino.w / 2), round(TOPO + ALTURA / 2))
        if ponto == self._sino_escrito:
            return
        self._sino_escrito = ponto
        tela_w, tela_h = w + 2 * LADO, 1440
        try:
            mon = Gdk.Display.get_default().get_monitor_at_surface(self.get_surface())
            g = mon.get_geometry()
            tela_w, tela_h = g.width, g.height
        except (AttributeError, TypeError):
            pass
        caminho = os.path.join(H, ".cache/aero-shell/sino.json")
        try:
            with open(caminho + ".tmp", "w") as f:
                json.dump({"x": ponto[0], "y": ponto[1], "w": tela_w, "h": tela_h, "fonte": "aerobar"}, f)
            os.replace(caminho + ".tmp", caminho)
        except OSError:
            pass

    def visiveis(self, lista):
        return [m for m in lista if m.nome not in CFG["hide"]]

    # ---------- Solid: molas, gotas e onde estão as ilhas ----------
    def animar_solido(self):
        if self.tique_s is None:
            self.ultimo_s = None
            self.tique_s = self.area.add_tick_callback(self._quadro_solido)

    def _quadro_solido(self, _w, relogio):
        agora = relogio.get_frame_time() / 1e6
        dt = 1 / 60 if self.ultimo_s is None else min(0.05, agora - self.ultimo_s)
        self.ultimo_s = agora
        mexe = False
        for mx, mw in self.molas_s.values():
            mexe = mx.passo(dt) | mw.passo(dt) | mexe
        self.area.queue_draw()
        if not mexe and not self.gotas_s and not self.ws_gotas:
            self.tique_s = None                      # parada: nenhum timer
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def anotar_solido(self, ilhas):
        """~/.cache/aero-shell/aerobar-solido.json: ilhas e ícones (alvo, em pixels da tela) pros painéis
        que nascem da barra. Reescrito só quando muda."""
        icones = {m.nome: round(LADO + m.x + m.w / 2) for m in self.esquerda + self.centro + self.direita if m.w > 0}
        alvo = [[round(LADO + a), round(LADO + b_)] for a, b_ in ilhas_solidas(self)]
        dados = {"ilhas": alvo, "icones": icones, "base": TOPO + ALTURA - 3}
        if dados == self._anotado_s:
            return
        self._anotado_s = dados
        try:
            with open(solido.ARQ_BARRA + ".tmp", "w") as f:
                json.dump(dados, f)
            os.replace(solido.ARQ_BARRA + ".tmp", solido.ARQ_BARRA)
        except OSError:
            pass

    def larg_de(self, m, cr):
        return largura_solida(self, m, cr) if SOLIDO else m.largura(cr)

    def medir(self, cr, w):
        def larg(lista):
            return sum(self.larg_de(m, cr) for m in lista)
        for m in self.esquerda + self.centro + self.direita:
            m.x, m.w = 0, 0
        esq, cen, dir_ = self.visiveis(self.esquerda), self.visiveis(self.centro), self.visiveis(self.direita)
        ce, de = larg(cen), larg(dir_)
        # Solid: a borda de fora da 1ª e da última ilha (m.x - 2 / fim + 2) na MESMA linha das janelas, que
        # ficam a general:gaps_out da borda da tela (ler_gaps); fora do Solid, o recuo de sempre
        ge, gd = (max(2, g - LADO) + 2 for g in self.gaps) if SOLIDO else (6, 6)
        x = ge
        for m in esq:
            m.x, m.w = x, self.larg_de(m, cr)
            x += m.w
        x = w / 2 - ce / 2                                    # the clock: exact middle, always
        for m in cen:
            m.x, m.w = x, self.larg_de(m, cr)
            x += m.w
        x = w - gd - de
        for m in dir_:
            m.x, m.w = x, self.larg_de(m, cr)
            x += m.w
        self.anotar_sino(w)

    def desenhar(self, _area, cr, w, h):
        if SOLIDO:
            return desenhar_barra_solida(self, cr, w)
        h = ALTURA                                            # below it is only room for the glow
        r = min(CFG["radius"], h / 2)
        if CFG["background"] != "none" or CFG["outline"]:
            cr.new_sub_path()
            for (ax, ay, a0) in ((w - r - 0.5, r + 0.5, -math.pi / 2), (w - r - 0.5, h - r - 0.5, 0),
                                 (r + 0.5, h - r - 0.5, math.pi / 2), (r + 0.5, r + 0.5, math.pi)):
                cr.arc(ax, ay, r, a0, a0 + math.pi / 2)
            cr.close_path()
            if CFG["background"] != "none":
                alfa = CFG["opacity"] * (0.55 if CFG["background"] == "glass" else 1.0)
                cr.set_source_rgba(*FUNDO, alfa)
                cr.fill_preserve()
            if CFG["outline"]:
                cr.set_source_rgba(*FG, 0.08)
                cr.set_line_width(1)
                cr.stroke()
            cr.new_path()
        self.medir(cr, w)
        for m in self.visiveis(self.esquerda + self.centro + self.direita):
            if m.w <= 0:
                continue
            usar_paleta(self.mistura_de(m, self.claro_em(m.x, m.x + m.w, w), w))
            CLARO_AGORA[0] = self.mistura.get(m.nome, 0) > 0.5
            if m is self.sobre and CFG["hover"] and not isinstance(m, EspacoBandeja):
                cr.set_source_rgba(*FG, 0.08)
                rr = 9
                x0, y0, ww, hh = m.x + 2, 4, m.w - 4, h - 8
                cr.new_sub_path()
                for (ax, ay, a0) in ((x0 + ww - rr, y0 + rr, -math.pi / 2), (x0 + ww - rr, y0 + hh - rr, 0),
                                     (x0 + rr, y0 + hh - rr, math.pi / 2), (x0 + rr, y0 + rr, math.pi)):
                    cr.arc(ax, ay, rr, a0, a0 + math.pi / 2)
                cr.close_path()
                cr.fill()
            m.desenhar(cr, m.x)
        usar_paleta(0)
        CLARO_AGORA[0] = False

    def claro_em(self, x0, x1, w):
        """Is the wallpaper light under [x0, x1]? Only with no background (or glass) and auto contrast on."""
        if not CFG["auto_contrast"] or CFG["background"] == "solid" or not self.luz:
            return False
        n = len(self.luz)
        a, b = int(max(0, x0) / w * n), int(min(w, x1) / w * n) + 1
        trecho = self.luz[a:b] or self.luz
        media = sum(trecho) / len(trecho)
        if CFG["background"] == "glass":                     # the dark glass darkens what is behind it
            media *= 1 - CFG["opacity"] * 0.55
        return media > 0.55

    TROCA, ONDA_TROCA = 0.45, 0.4        # each icon blends in 0.45 s; the change sweeps left->right in 0.4 s

    def mistura_de(self, m, claro, w):
        """Current blend for module m (0 dark .. 1 light). A change starts an animated sweep."""
        alvo = 1.0 if claro else 0.0
        agora = time.monotonic()
        if self.alvos.get(m.nome) != alvo:
            if m.nome not in self.alvos:                      # first draw: no animation
                self.mistura[m.nome] = alvo
            else:
                atraso = (m.x / max(w, 1)) * self.ONDA_TROCA
                self.trocas[m.nome] = (agora + atraso, self.mistura.get(m.nome, 1 - alvo))
                if self.tique_troca is None:
                    self.tique_troca = self.area.add_tick_callback(self._quadro_troca)
            self.alvos[m.nome] = alvo
        if m.nome in self.trocas:
            inicio, de = self.trocas[m.nome]
            t = min(1.0, max(0.0, (agora - inicio) / self.TROCA))
            t = t * t * (3 - 2 * t)
            self.mistura[m.nome] = de + (alvo - de) * t
            if t >= 1:
                del self.trocas[m.nome]
        return self.mistura.get(m.nome, alvo)

    def _quadro_troca(self, *_):
        self.area.queue_draw()
        if not self.trocas:
            self.tique_troca = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def ler_papel(self):
        """Current wallpaper (hyprpaper) -> brightness of the strip under the bar. Runs in a thread."""
        saida = rodar("hyprctl", "hyprpaper", "listactive")
        caminho = ""
        for linha in saida.splitlines():
            if ":" in linha:
                caminho = linha.split(":", 1)[1].strip()
                break
        if not caminho or caminho == self.papel:
            return False
        self.papel = caminho
        tela_w, tela_h = 2560, 1440
        try:
            for m in json.loads(rodar("hyprctl", "monitors", "-j") or "[]"):
                if m.get("focused"):
                    tela_w, tela_h = m["width"] / (m.get("scale") or 1), m["height"] / (m.get("scale") or 1)
        except (ValueError, KeyError, TypeError):
            pass
        self.luz = luz_do_papel(caminho, tela_w, tela_h, TOPO + ALTURA)
        return True

    def modulo_em(self, x):
        for m in self.esquerda + self.centro + self.direita:
            if m.w > 0 and m.x <= x < m.x + m.w:
                return m
        return None

    def mudar_sobre(self, m):
        if m is not self.sobre:
            self.sobre = m
            self._clicado = None                     # saiu do ícone clicado: a dica volta a valer
            self.area.queue_draw()
            if getattr(self, "_dica_id", None):
                GLib.source_remove(self._dica_id)
                self._dica_id = None
            if m is not None and SOLIDO and m.nome not in ("workspaces", "tray"):
                self.gota.esconder()
                self._dica_id = GLib.timeout_add(380, lambda mm=m: self._mostrar_dica(mm))
            elif m is not None and m.nome == "ultrasu":
                mins = minutos_ultrasu(self.ultrasu)
                texto_ = f"{mins // 60}H {mins % 60:02d}M" if mins is not None else "CLOSED"
                cor = cor_ultrasu(mins) or DIM
                self.gota.mostrar(texto_, cor, m.x + m.w / 2, self.largura_tela())
            elif m is not None and m.nome not in ("workspaces", "tray", "clock"):
                self.gota.esconder()
                self._dica_id = GLib.timeout_add(420, lambda mm=m: self._mostrar_dica(mm))   # a short, still hover
            else:
                self.gota.esconder()

    def _mostrar_dica(self, m):
        self._dica_id = None
        if m is not self.sobre or m is getattr(self, "_clicado", None):
            return False
        texto_ = texto_dica_solida(self, m) if SOLIDO else self.texto_dica(m)
        if texto_:
            self.gota.mostrar(texto_, ESCURA["FG"], m.x + m.w / 2, self.largura_tela())
        return False

    def largura_tela(self):
        """Largura da tela em pixels lógicos (a barra + as duas margens): a gota não passa da borda."""
        return self.area.get_width() + 2 * LADO

    def texto_dica(self, m):
        textos = {"vol": f"VOLUME {self.volume}%" + (" MUTED" if self.mudo else ""),
                  "wifi": self.wifi_nome or "OFFLINE",
                  "bt": (f"BLUETOOTH {self.bt_conectados} ON" if self.bt_conectados else
                         "BLUETOOTH ON" if self.bt_ligado else "BLUETOOTH OFF"),
                  "bat": f"{self.bat_nivel}%" + (" CHARGING" if self.carregando else " PLUGGED" if self.na_tomada else "")
                         + (" SAVER" if self.perfil == "power-saver" else ""),
                  "lang": f"KEYBOARD {self.idioma}", "min": f"{self.minimizados} MINIMIZED",
                  "bell": "HERMES" + (" (DND)" if self.dnd else ""), "gear": "SETTINGS",
                  "central": "HESTIA"}
        return textos.get(m.nome, m.dica.upper() if m.dica else "")

    def ao_clicar(self, g, _n, x, _y):
        m = self.modulo_em(x)
        # clicou: a dica (pílula) some e não volta até o ponteiro sair desse ícone e voltar
        self._clicado = m
        if getattr(self, "_dica_id", None):
            GLib.source_remove(self._dica_id)
            self._dica_id = None
        if SOLIDO and m is not None and m.nome == "clock" and g.get_current_button() == 1:
            pilula_ = self.gota.sumir_ja()             # a dica vira a gaveta do calendário (metaball)
            self.calendario.desde = pilula_
        else:
            self.gota.esconder()
        if m is not None:
            m.clique(g.get_current_button(), x)

    def ao_rolar(self, _c, _dx, dy):
        if self.sobre is not None:
            self.sobre.rolar(dy)
        return True

    def dica(self, _area, x, _y, _kb, tooltip):
        m = self.modulo_em(x)
        if m is None or _y > ALTURA:
            return False
        textos = {"vol": f"Volume {self.volume}%" + (" (muted)" if self.mudo else ""),
                  "wifi": self.wifi_nome or "Offline",
                  "bt": (f"Bluetooth: {self.bt_conectados} connected" if self.bt_conectados else
                         "Bluetooth on" if self.bt_ligado else "Bluetooth off"),
                  "bat": self.bat_dica, "ultrasu": "",            # UltraSu shows its own dot drop
                  "clock": time.strftime("%A, %d %B %Y")}
        t = textos.get(m.nome) or m.dica
        if not t:
            return False
        tooltip.set_text(t)
        return True

    def redesenhar(self):
        self.area.queue_draw()
        return False

    # ---------- data ----------

    def iniciar_fontes(self):
        self.ler_gaps()
        self.ler_workspaces()
        self.ler_idioma()
        self.ler_volume()
        self.ler_bateria()
        self.ler_bandeja()
        GLib.timeout_add_seconds(5, self.tique_bateria)
        GLib.timeout_add_seconds(30, lambda: (self.redesenhar(), True)[1])          # the clock's minute
        self.cada(2, self.ler_minimizados)
        self.cada(2, self.ler_notificacoes)
        self.cada(10, self.ler_rede)
        self.cada(15, self.ler_ultrasu)
        if not SOLIDO:                            # o contraste automático (wallpaper sob a barra) é só do DotStyle
            self.cada(3, self.ler_papel)          # per-workspace wallpapers change on switch: cheap check
        threading.Thread(target=self.ouvir_hyprland, daemon=True).start()
        threading.Thread(target=self.ouvir_pactl, daemon=True).start()
        self.ouvir_perfil()
        cfg_mon = Gio.File.new_for_path(CONFIG).monitor_file(Gio.FileMonitorFlags.NONE, None)
        cfg_mon.connect("changed", lambda _m, _f, _o, ev: ev == Gio.FileMonitorEvent.CHANGES_DONE_HINT
                        and GLib.timeout_add(150, self.recarregar_config))
        self._cfg_mon = cfg_mon
        mon = Gio.File.new_for_path(BANDEJA).monitor_file(Gio.FileMonitorFlags.NONE, None)
        mon.connect("changed", lambda *_: self.ler_bandeja())
        self._mon = mon

    def recarregar_config(self):
        """Settings changed. Sizes and colours are read at start, so the bar starts again (exit code 3 makes
        the launcher bring it back right away); a crash-safe and simple way to apply everything."""
        novo = ler_config()
        if novo != CFG:
            print("aerobar: settings changed, restarting", flush=True)
            os._exit(3)
        return False

    def cada(self, seg, f):
        """Runs f() in a thread now and every seg seconds; f returns True if something changed."""
        def laco():
            while True:
                try:
                    if f():
                        GLib.idle_add(self.redesenhar)
                except Exception as erro:  # noqa: BLE001
                    print(f"aerobar: {f.__name__}: {erro!r}", flush=True)
                time.sleep(seg)
        threading.Thread(target=laco, daemon=True).start()

    gaps = (12, 12)                                   # general:gaps_out (esquerda, direita), em px lógicos

    def ler_gaps(self):
        """general:gaps_out do Hyprland ("cima direita baixo esquerda", ou um número só): as ilhas da ponta
        se alinham com as janelas. Relido a cada configreloaded (mudou no Settings/conf: segue sozinho)."""
        try:
            d = json.loads(rodar("hyprctl", "getoption", "general:gaps_out", "-j") or "{}")
            v = [int(float(x)) for x in str(d.get("custom", d.get("int", ""))).split()]
        except (ValueError, TypeError):
            return
        if not v:
            return
        cima, dir_, baixo, esq = (v * 4)[:4] if len(v) == 1 else (v + v[:2])[:4] if len(v) == 2 else \
            (v[0], v[1], v[2], v[1]) if len(v) == 3 else v[:4]
        if (esq, dir_) != self.gaps:
            self.gaps = (esq, dir_)
            GLib.idle_add(self.redesenhar)

    def ler_workspaces(self):
        try:
            ws = json.loads(rodar("hyprctl", "workspaces", "-j") or "[]")
            ativo = json.loads(rodar("hyprctl", "activeworkspace", "-j") or "{}")
            self.ws_usados = {w["id"] for w in ws if w.get("windows", 0) > 0 and w["id"] > 0}
            self.ws_ativo = ativo.get("id", self.ws_ativo)
        except (ValueError, KeyError, TypeError):
            pass
        GLib.idle_add(self.redesenhar)
        return False

    def ler_idioma(self):
        try:
            for k in json.loads(rodar("hyprctl", "devices", "-j") or "{}").get("keyboards", []):
                if k.get("main"):
                    nome = k.get("active_keymap", "")
                    self.idioma = self._curto(nome)
        except (ValueError, TypeError):
            pass
        GLib.idle_add(self.redesenhar)
        return False

    @staticmethod
    def _curto(nome):
        tabela = {"English (US)": "US", "Portuguese (Brazil)": "BR", "English (intl., with AltGr dead keys)": "INT"}
        if nome in tabela:
            return tabela[nome]
        m = re.search(r"\(([A-Za-z]{2})", nome)
        return (m.group(1) if m else nome[:2]).upper() or "US"

    def ouvir_hyprland(self):
        caminho = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "hypr",
                               os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""), ".socket2.sock")
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(caminho)
                    for linha in s.makefile("r", encoding="utf-8", errors="replace"):
                        nome = linha.split(">>", 1)[0]
                        # lidos aqui, nesta thread: o hyprctl nunca trava a thread do GTK (hover, gota)
                        if nome in ("workspace", "workspacev2", "createworkspace", "destroyworkspace", "openwindow",
                                    "closewindow", "movewindow", "movewindowv2", "focusedmon"):
                            self.ler_workspaces()
                        elif nome == "activelayout":
                            self.ler_idioma()
                        elif nome == "configreloaded":
                            self.ler_gaps()
            except OSError:
                pass
            time.sleep(3)

    def ler_volume(self):
        saida = rodar("pactl", "get-sink-volume", "@DEFAULT_SINK@")
        m = re.search(r"(\d+)%", saida)
        self.volume = int(m.group(1)) if m else 0
        self.mudo = "yes" in rodar("pactl", "get-sink-mute", "@DEFAULT_SINK@")
        GLib.idle_add(self.redesenhar)
        return False

    def ouvir_pactl(self):
        while True:
            try:
                p = subprocess.Popen(["pactl", "subscribe"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True)
                for linha in p.stdout:
                    if "sink" in linha or "server" in linha:
                        self.ler_volume()
            except OSError:
                pass
            time.sleep(3)

    def ouvir_perfil(self):
        def pronto(_o, r):
            try:
                self.prox_perfil = Gio.DBusProxy.new_for_bus_finish(r)
            except GLib.Error:
                return
            self._perfil_do_proxy()
            self.prox_perfil.connect("g-properties-changed", lambda *_: self._perfil_do_proxy())
        Gio.DBusProxy.new_for_bus(Gio.BusType.SYSTEM, Gio.DBusProxyFlags.NONE, None, "net.hadess.PowerProfiles",
                                  "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles", None, pronto)

    def _perfil_do_proxy(self):
        v = self.prox_perfil.get_cached_property("ActiveProfile")
        if v is not None:
            self.perfil = v.unpack()
            self.redesenhar()

    def ler_bateria(self):
        base = "/sys/class/power_supply"
        try:
            bat = next(os.path.join(base, n) for n in sorted(os.listdir(base)) if n.startswith("BAT"))
        except (StopIteration, OSError):
            return
        def ler(nome, padrao=""):
            try:
                return open(os.path.join(bat, nome)).read().strip()
            except OSError:
                return padrao
        try:
            self.bat_nivel = int(ler("capacity", "0"))
        except ValueError:
            pass
        estado = ler("status")
        self.carregando = estado == "Charging"
        self.na_tomada = any(open(os.path.join(base, n, "online")).read().strip() == "1"
                             for n in os.listdir(base) if n.startswith(("AC", "ACAD", "ADP"))
                             and os.path.exists(os.path.join(base, n, "online")))
        try:
            watts = int(ler("power_now", "0")) / 1e6
            energia, cheia = int(ler("energy_now", "0")) / 1e6, int(ler("energy_full", "0")) / 1e6
            if watts > 0.5 and estado == "Discharging":
                h = energia / watts
                tempo = f"{int(h)}h {int(h * 60 % 60):02d}m left"
            elif watts > 0.5 and self.carregando and cheia > energia:
                h = (cheia - energia) / watts
                tempo = f"{int(h)}h {int(h * 60 % 60):02d}m to full"
            else:
                tempo = estado
            self.bat_dica = f"{self.bat_nivel}% · {tempo} · {watts:.1f} W"
            if self.perfil == "power-saver":
                self.bat_dica += " · power saver"
        except ValueError:
            self.bat_dica = f"{self.bat_nivel}%"
        if self.carregando and self.pulso is None and not SOLIDO:
            self.pulso = self.area.add_tick_callback(self._pulso)
        self.redesenhar()

    def _pulso(self, _w, relogio):
        """Only animation of the bar: the charging pulse. No máximo 30 quadros/s: a barra inteira a cada
        quadro do monitor (165 Hz) comia metade de um núcleo e deixava a gota do hover aos trancos."""
        if not self.carregando:
            self.pulso = None
            self.redesenhar()
            return GLib.SOURCE_REMOVE
        agora = relogio.get_frame_time()
        if agora - getattr(self, "_pulso_t", 0) >= 33000:          # µs
            self._pulso_t = agora
            self.area.queue_draw()
        return GLib.SOURCE_CONTINUE

    def tique_bateria(self):
        self.ler_bateria()
        return True

    def ler_minimizados(self):
        try:
            d = json.loads(rodar(os.path.join(BIN, "hypr-minimizados"), "--contar") or "{}")
            n = int(re.sub(r"\D", "", str(d.get("text", "0"))) or 0)
        except (ValueError, TypeError):
            n = 0
        mudou = n != self.minimizados
        self.minimizados = n
        return mudou

    def ler_rede(self):
        nivel, nome = 0, ""
        for linha in rodar("nmcli", "-t", "-f", "ACTIVE,SIGNAL,SSID", "dev", "wifi", "list", "--rescan", "no").splitlines():
            partes = re.split(r"(?<!\\):", linha)
            if len(partes) >= 3 and partes[0] == "yes":
                sinal = int(partes[1] or 0)
                nivel = 4 if sinal >= 75 else 3 if sinal >= 50 else 2 if sinal >= 25 else 1
                nome = partes[2].replace("\\:", ":")
                break
        if not nivel and "ethernet:connected" in rodar("nmcli", "-t", "-f", "TYPE,STATE", "dev"):
            nivel, nome = 4, "Wired"
        show = rodar("bluetoothctl", "show")
        ligado = "Powered: yes" in show
        conectados = len([x for x in rodar("bluetoothctl", "devices", "Connected").splitlines() if x.startswith("Device")])
        mudou = (nivel, nome, ligado, conectados) != (self.wifi_nivel, self.wifi_nome, self.bt_ligado, self.bt_conectados)
        self.wifi_nivel, self.wifi_nome, self.bt_ligado, self.bt_conectados = nivel, nome, ligado, conectados
        return mudou

    def ler_ultrasu(self):
        exe = os.path.join(BIN, "ultrasu")
        if not os.access(exe, os.X_OK):
            return False
        try:
            d = json.loads(rodar(exe, "--barra", timeout=6) or "{}")
        except ValueError:
            return False
        mudou = d != self.ultrasu
        self.ultrasu = d if isinstance(d, dict) else {}
        return mudou

    def ler_notificacoes(self):
        modos = rodar("busctl", "--user", "--json=short", "call", "com.aero.Hermes", "/org/freedesktop/Notifications",
                      "com.aero.Hermes", "Modes")
        try:
            dnd = "dnd" in json.loads(modos)["data"][0] if modos else False
        except (ValueError, KeyError, IndexError):
            dnd = False
        ultimo = 0
        r = rodar("busctl", "--user", "--json=short", "call", "com.aero.Hermes", "/org/freedesktop/Notifications",
                  "com.aero.Hermes", "LatestId")
        try:
            ultimo = int(json.loads(r)["data"][0]) if r else 0
        except (ValueError, KeyError, IndexError):
            pass
        try:
            visto = int(open(VISTO).read().strip() or 0)
        except (OSError, ValueError):
            visto = 0
        nao_lidas = ultimo > visto
        mudou = (dnd, nao_lidas) != (self.dnd, self.nao_lidas)
        self.dnd, self.nao_lidas = dnd, nao_lidas
        return mudou

    def ler_bandeja(self):
        try:
            classe = json.load(open(BANDEJA)).get("class", "")
            n = int(re.sub(r"\D", "", classe) or 0)
        except (OSError, ValueError, AttributeError):
            n = 0
        if n != self.n_bandeja:
            self.n_bandeja = n
            self.redesenhar()


class Pegador(Gtk.Window):
    """An invisible full-screen layer under a popup: a click anywhere outside the popup lands here and
    closes it. Exists only while the popup is open."""

    def __init__(self, ao_clicar):
        super().__init__(decorated=False)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "aerobar-pegador")
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.add_css_class("aerobar-gota")
        area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.set_child(area)
        c = Gtk.GestureClick(button=0)
        c.connect("pressed", lambda *_: ao_clicar())
        area.add_controller(c)


class Calendario(Gtk.Window):
    """A month in dots under the clock: today is a lit dot with a red ring, weekends dimmer, the other
    days small dots with their number in Doto. Scroll or the arrows change month; a click outside closes.
    It opens like the cards: the grid pops in from the top, day by day."""

    COL, LIN = 7, 6
    PASSO, TOPO_GRADE = 46, 74

    def __init__(self):
        super().__init__(decorated=False)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "aerobar-calendario")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.ON_DEMAND)
        self.add_css_class("aerobar-gota")
        self.largura = self.COL * self.PASSO + 36
        self.altura = self.TOPO_GRADE + self.LIN * self.PASSO + 20
        self.area = Gtk.DrawingArea(content_width=self.largura, content_height=self.altura)
        self.area.set_draw_func(self.desenhar)
        self.set_child(self.area)
        hoje = time.localtime()
        self.ano, self.mes = hoje.tm_year, hoje.tm_mon
        self.t0, self.saindo, self.tique = 0.0, False, None
        clique = Gtk.GestureClick()
        clique.connect("released", self._clique)
        self.area.add_controller(clique)
        rolar = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        rolar.connect("scroll", lambda _c, _dx, dy: (self.mudar_mes(1 if dy > 0 else -1), True)[1])
        self.area.add_controller(rolar)
        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self._tecla)
        self.add_controller(teclas)
        foco = Gtk.EventControllerFocus()
        foco.connect("leave", lambda *_: self.esconder())
        self.add_controller(foco)

    def alternar(self, modulo):
        if self.get_visible() and not self.saindo:
            self.esconder()
            return
        hoje = time.localtime()
        self.ano, self.mes = hoje.tm_year, hoje.tm_mon
        Camada.set_margin(self, Camada.Edge.TOP, TOPO + ALTURA + 6)
        self.t0, self.saindo = time.monotonic(), False
        self.set_visible(True)
        self.present()
        self.animar()

    SAIDA = 0.5

    def esconder(self):
        if self.get_visible() and not self.saindo:
            self.saindo, self.t0 = True, time.monotonic()
            self.animar()

    def mudar_mes(self, d):
        self.mes += d
        if self.mes > 12:
            self.mes, self.ano = 1, self.ano + 1
        elif self.mes < 1:
            self.mes, self.ano = 12, self.ano - 1
        self.t0 = time.monotonic() - 0.15                  # the new month pops in quickly
        self.animar()

    def animar(self):
        if self.tique is None:
            self.tique = self.area.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        e = time.monotonic() - self.t0
        self.area.queue_draw()
        if self.saindo and e > self.SAIDA:
            self.tique = None
            self.set_visible(False)
            return GLib.SOURCE_REMOVE
        if not self.saindo and e > 0.9:
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _tecla(self, _c, tecla, _cod, _mods):
        if tecla == Gdk.KEY_Escape:
            self.esconder()
        elif tecla in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_Page_Up):
            self.mudar_mes(-1)
        elif tecla in (Gdk.KEY_Right, Gdk.KEY_Down, Gdk.KEY_Page_Down):
            self.mudar_mes(1)
        return True

    def _clique(self, _g, _n, x, y):
        if y < 50:
            if x < 60:
                self.mudar_mes(-1)
            elif x > self.largura - 60:
                self.mudar_mes(1)

    def desenhar(self, _a, cr, w, h):
        import calendar
        e = time.monotonic() - self.t0
        if self.saindo:
            # leaving: the days go out last-to-first (rising and shrinking into dots), then the panel
            # folds up toward the clock and fades
            geral = 1 - suave_(0.25, self.SAIDA, e)
            dobra = 1 - suave_(0.22, self.SAIDA, e) * 0.85
        else:
            geral = min(1.0, e / 0.2)
            dobra = 0.75 + 0.25 * (1 - (1 - min(1.0, e / 0.3)) ** 3)   # unfolds down from the clock
        r = 22
        hh = h * dobra
        cr.new_sub_path()
        for (ax, ay, a0) in ((w - r, r, -math.pi / 2), (w - r, hh - r, 0), (r, hh - r, math.pi / 2), (r, r, math.pi)):
            cr.arc(ax, ay, r, a0, a0 + math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0.043, 0.043, 0.047, 0.93 * geral)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        # month title and arrows
        titulo = time.strftime("%B %Y", (self.ano, self.mes, 1, 0, 0, 0, 0, 1, -1)).upper()
        texto(cr, None, "<", 22, 30, "Doto Black 16", ESCURA["DIM"], geral)
        tw = texto(cr, None, titulo, 0, 0, "Doto Black 17", ESCURA["FG"], medir=True)
        texto(cr, None, titulo, w / 2 - tw / 2, 30, "Doto Black 17", ESCURA["FG"], geral)
        texto(cr, None, ">", w - 34, 30, "Doto Black 16", ESCURA["DIM"], geral)
        for i, nome in enumerate(("MO", "TU", "WE", "TH", "FR", "SA", "SU")):
            nw = texto(cr, None, nome, 0, 0, "Geist Mono 9", ESCURA["DIM"], medir=True)
            texto(cr, None, nome, 18 + i * self.PASSO + self.PASSO / 2 - nw / 2, 58, "Geist Mono 9",
                  ESCURA["DIM"], geral * (0.6 if i >= 5 else 1.0))
        hoje = time.localtime()
        semanas = calendar.Calendar(0).monthdayscalendar(self.ano, self.mes)
        for j, semana in enumerate(semanas):
            for i, dia in enumerate(semana):
                if not dia:
                    continue
                cx = 18 + i * self.PASSO + self.PASSO / 2
                cy = self.TOPO_GRADE + j * self.PASSO + self.PASSO / 2
                if self.saindo:
                    ordem = (len(semanas) * 7 - (j * 7 + i)) * 0.006       # the last day leaves first
                    k = 1 - suave_(ordem, ordem + 0.18, e)
                    sobe = (1 - k) * 10
                else:
                    atraso = (j * 7 + i) * 0.012
                    k = min(1.0, max(0.0, (e - atraso) / 0.22))
                    k = 1 - (1 - k) ** 3
                    sobe = -4 * (1 - k)
                if k <= 0.01:
                    continue
                cy -= sobe
                eh_hoje = (self.ano, self.mes, dia) == (hoje.tm_year, hoje.tm_mon, hoje.tm_mday)
                fim_semana = i >= 5
                if eh_hoje:
                    halo(cr, cx, cy, 30, ESCURA["RED"], 0.5 * geral)
                    cr.set_source_rgba(*ESCURA["FG"], geral)
                    cr.arc(cx, cy, 17 * k, 0, 2 * math.pi)
                    cr.fill()
                    cr.set_source_rgba(*ESCURA["RED"], geral)
                    cr.set_line_width(2)
                    cr.arc(cx, cy, 20 * k, 0, 2 * math.pi)
                    cr.stroke()
                    cor_num, a_num = (0.05, 0.05, 0.06), 1.0
                else:
                    cr.set_source_rgba(*ESCURA["FG"], 0.07 * geral * k)
                    cr.arc(cx, cy, 17 * k, 0, 2 * math.pi)
                    cr.fill()
                    cor_num, a_num = ESCURA["FG"], 0.55 if fim_semana else 0.9
                num = str(dia)
                nw = texto(cr, None, num, 0, 0, "Doto Black 15", cor_num, medir=True)
                texto(cr, None, num, cx - nw / 2, cy, "Doto Black 15", cor_num, a_num * k)
        cr.restore()


class CalendarioSolido(Calendario):
    """Solid: o mês em Material — superfície tonal, números em Inter, hoje num círculo da primária. Abre
    como uma gaveta que ESCORRE da ilha do relógio: o painel desce até GAP abaixo da ilha, preso a ela por
    um pescoço sólido contínuo (mesma cor da ilha, cantos côncavos de raio FILETE nas duas pontas);
    fecha se recolhendo pra dentro da ilha. Se a dica do relógio estava na tela, a gaveta NASCE dela:
    a pílula vira o começo da gaveta (desde_pilula) e cresce até o mês inteiro.

    A camada começa Y0 px DENTRO da ilha (o pescoço entra um pouco nela: sem fresta de anti-serrilhado)
    e ignora a zona exclusiva da barra (-1): sem isso o Hyprland a punha ABAIXO da área reservada da barra
    e sobrava um vão de ~30 px entre a ilha e o pescoço."""

    GAP = 26                                   # da borda de baixo da ilha até o topo do painel (aberto)
    FILETE = 13                                # raio dos cantos côncavos do pescoço (união suave)
    PESCOCO = 17                               # meia largura da cintura do pescoço
    Y0 = 2
    ENTRA, SAI = 0.42, 0.28

    def __init__(self):
        super().__init__()
        self.altura += self.GAP + self.Y0
        self.area.set_content_height(self.altura)
        Camada.set_exclusive_zone(self, -1)
        self.desde = None                          # (largura, altura, topo) da pílula da dica, se nasce dela
        prov = Gtk.CssProvider()                   # a janela não pinta nada: o painel é todo desenhado aqui
        prov.load_from_string(".aerobar-gota { background: none; background-color: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def alternar(self, modulo):
        if self.get_visible() and not self.saindo:
            self.esconder()
            return
        hoje = time.localtime()
        self.ano, self.mes = hoje.tm_year, hoje.tm_mon
        Camada.set_margin(self, Camada.Edge.TOP, TOPO + ALTURA - 3 - self.Y0)   # a borda de baixo da ilha
        self.t0, self.saindo = time.monotonic(), False
        self.set_visible(True)
        self.present()
        self.animar()

    def _quadro(self, *_):
        e = time.monotonic() - self.t0
        self.area.queue_draw()
        if self.saindo and e > self.SAI:
            self.tique = None
            self.set_visible(False)
            return GLib.SOURCE_REMOVE
        if not self.saindo and e > self.ENTRA:
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def esconder(self):
        self.desde = None
        super().esconder()

    def _clique(self, _g, _n, x, y):
        if self.GAP + self.Y0 <= y < 50 + self.GAP + self.Y0:
            if x < 60:
                self.mudar_mes(-1)
            elif x > self.largura - 60:
                self.mudar_mes(1)

    def desenhar(self, _a, cr, w, h):
        import calendar
        t = time.monotonic() - self.t0
        if self.saindo:
            k = min(1.0, t / self.SAI)
            e, alfa = 1 - k * k, 1 - solido.suave(0.0, 0.4, k)
        else:
            k = min(1.0, t / self.ENTRA)
            e, alfa = 1 - (1 - k) ** 3, solido.suave(0.5, 1.0, k)
        sup = cor_s("surface_container")
        y0 = self.Y0                               # a borda de baixo da ilha, nesta camada
        ph = h - self.GAP - y0
        w0, h0, t0_ = self.desde or (64, 18, 0)    # começo: a pílula da dica (se estava aberta) ou uma gota
        pw, phh = w0 + (w - w0) * e, h0 + (ph - h0) * e
        topo = y0 + t0_ + (self.GAP - t0_) * e
        x0 = (w - pw) / 2
        cr.set_source_rgb(*sup)
        pilula(cr, x0, topo, pw, phh, min(solido.raio(), phh / 2, pw / 2))
        cr.fill()
        self._pescoco(cr, w / 2, y0, topo, pw)     # a gaveta continua presa à ilha
        cr.fill()
        if alfa <= 0.01:
            return
        cr.push_group()
        cr.translate(0, self.GAP + y0)
        on, dim, prim = cor_s("on_surface"), cor_s("on_surface_variant"), cor_s("primary")
        titulo = time.strftime("%B %Y", (self.ano, self.mes, 1, 0, 0, 0, 0, 1, -1))
        tw = texto_s(cr, titulo, 0, 0, 16, on, Pango.Weight.SEMIBOLD, medir=True)
        texto_s(cr, titulo, w / 2 - tw / 2, 30, 16, on, Pango.Weight.SEMIBOLD)
        for gx, cp in ((30, 0xF0141), (w - 30, 0xF0142)):                     # ‹ › (Material chevrons)
            cr.set_source_rgb(*cor_s("surface_container_highest"))
            cr.arc(gx, 30, 15, 0, 2 * math.pi)
            cr.fill()
            glifo_s(cr, cp, gx, 30, on, px=18)
        for i, nome in enumerate(("M", "T", "W", "T", "F", "S", "S")):
            nw = texto_s(cr, nome, 0, 0, 12, dim, medir=True)
            texto_s(cr, nome, 18 + i * self.PASSO + self.PASSO / 2 - nw / 2, 60, 12, dim)
        hoje = time.localtime()
        for j, semana in enumerate(calendar.Calendar(0).monthdayscalendar(self.ano, self.mes)):
            for i, dia in enumerate(semana):
                if not dia:
                    continue
                cx = 18 + i * self.PASSO + self.PASSO / 2
                cy = self.TOPO_GRADE + j * self.PASSO + self.PASSO / 2
                eh_hoje = (self.ano, self.mes, dia) == (hoje.tm_year, hoje.tm_mon, hoje.tm_mday)
                if eh_hoje:
                    cr.set_source_rgb(*prim)
                    cr.arc(cx, cy, 19, 0, 2 * math.pi)
                    cr.fill()
                cor = cor_s("on_primary") if eh_hoje else (dim if i >= 5 else on)
                num = str(dia)
                nw = texto_s(cr, num, 0, 0, 14, cor, Pango.Weight.SEMIBOLD if eh_hoje else Pango.Weight.NORMAL,
                             medir=True)
                texto_s(cr, num, cx - nw / 2, cy, 14, cor, Pango.Weight.SEMIBOLD if eh_hoje else Pango.Weight.NORMAL)
        cr.pop_group_to_source()
        cr.paint_with_alpha(alfa)

    def _pescoco(self, cr, cx, y_ilha, y_painel, pw):
        """Um pescoço SÓLIDO só (sem fresta): cintura de meia largura PESCOCO entre a borda de baixo da
        ilha (y_ilha) e o topo do painel (y_painel), com cantos côncavos de raio FILETE nas duas pontas
        (o canto vira um quarto de círculo "pra fora": a união suave das duas formas). Vão curto (abrindo):
        o raio encolhe pra caber. Também entra 2 px na ilha e no painel (sem costura no anti-serrilhado)."""
        g = y_painel - y_ilha
        if g <= 0.5:
            return
        r = max(0.0, min(self.FILETE, g / 2))
        nw = max(2.0, min(self.PESCOCO, pw / 2 - r - 2))
        a, b = y_ilha, y_painel
        cr.move_to(cx - nw - r, a - 2)
        cr.line_to(cx - nw - r, a)
        cr.arc(cx - nw - r, a + r, r, -math.pi / 2, 0)                 # canto côncavo de cima, esquerda
        cr.line_to(cx - nw, b - r)
        cr.arc(cx - nw - r, b - r, r, 0, math.pi / 2)                  # de baixo, esquerda
        cr.line_to(cx - nw - r, b + 2)
        cr.line_to(cx + nw + r, b + 2)
        cr.line_to(cx + nw + r, b)
        cr.arc(cx + nw + r, b - r, r, math.pi / 2, math.pi)            # de baixo, direita
        cr.line_to(cx + nw, a + r)
        cr.arc(cx + nw + r, a + r, r, math.pi, 3 * math.pi / 2)        # de cima, direita
        cr.line_to(cx + nw + r, a - 2)
        cr.close_path()


class Gota(Gtk.Window):
    """A small dot label that drops down under a bar icon on hover: its dots fall from the bar and
    settle into the letters; on leave they rise back and fade. Click-through, own layer.

    Sem trancos: a camada tem tamanho FIXO (cabe o maior texto, MAX_CHARS letras) e só muda a margem;
    redimensionar uma camada já aberta cortava a última letra até o compositor confirmar o tamanho novo.
    Depois de sumir ela fica aberta (transparente) por um tempinho, pra passar de ícone em ícone sem
    reabrir a camada (cada reabertura roda a animação de entrada do Hyprland). Os pontos de cada texto
    ficam guardados e o halo colorido é um carimbo pronto, então o quadro só pinta."""

    PASSO = 2.4
    MAX_CHARS = 30
    PAD_X, PAD_Y = 12, 9
    QUEDA, SAIDA, ESPERA = 0.28, 0.3, 1.5      # s: queda de cada ponto, subida/fade, aberta e vazia
    ONDA = 0.4                                 # s: a onda da esquerda à direita, qualquer que seja o texto

    @classmethod
    def medidas(cls, n):
        """Caixa para n letras 5×7 (6 colunas por letra, a última sem o espaço) + respiro dos dois lados."""
        return (max(1, n * 6 - 1)) * cls.PASSO + 2 * cls.PAD_X, 7 * cls.PASSO + 2 * cls.PAD_Y

    def __init__(self):
        super().__init__(decorated=False)
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from hermes_dots import normalizar, FONTE         # uma vez só (antes era a cada hover)
        self._normalizar, self._fonte = normalizar, FONTE
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "aerobar-gota")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_anchor(self, Camada.Edge.LEFT, True)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_margin(self, Camada.Edge.TOP, TOPO + ALTURA + 2)
        self.add_css_class("aerobar-gota")
        prov = Gtk.CssProvider()
        prov.load_from_string(".aerobar-gota { background: none; background-color: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        lw, lh = self.medidas(self.MAX_CHARS)
        self.W, self.H = int(math.ceil(lw)) + 2, int(math.ceil(lh))
        self.area = Gtk.DrawingArea(content_width=self.W, content_height=self.H)
        self.area.set_draw_func(self.desenhar)
        self.set_child(self.area)
        self.set_default_size(self.W, self.H)
        self.set_size_request(self.W, self.H)
        self.connect("realize", self._sem_entrada)
        self.texto, self.cor, self.saindo, self.tique = "", FG, False, None
        self.e, self.t0 = 0.0, None             # tempo da animação (pelo relógio de quadros) / início
        self.caixa = (0.0, 0.0)                 # x da caixa dentro da camada, largura da caixa
        self.pontos, self.fim, self._cache, self._carimbos = [], 0.0, {}, {}
        self._fechar_id = None

    def _sem_entrada(self, *_):
        import cairo
        if self.get_surface() is not None:
            self.get_surface().set_input_region(cairo.Region())

    def _pontos_de(self, t):
        """(x, y, atraso, queda) de cada ponto aceso, relativos à caixa. Guardado por texto.
        O atraso por coluna encolhe nos textos longos: a onda dura no máximo ONDA, e a animação só
        termina depois do último ponto pousar (antes ela parava em 0.7 s fixos e as últimas letras
        de um texto comprido ficavam congeladas no meio da queda, meio apagadas)."""
        if t not in self._cache:
            import random
            p, rnd, pts = self.PASSO, random.Random(len(t)), []
            passo_t = min(0.012, self.ONDA / max(1, len(t) * 6))
            for i, ch in enumerate(t):
                g = self._fonte[ch]
                for c in range(5):
                    for linha in range(7):
                        if g[linha][c] == "1":
                            pts.append((self.PAD_X + (i * 6 + c) * p + p / 2, self.PAD_Y + linha * p + p / 2,
                                        (i * 6 + c) * passo_t + rnd.uniform(0, 0.05), rnd.uniform(14, 26)))
            if len(self._cache) > 64:
                self._cache.clear()
            fim = max((pt[2] for pt in pts), default=0) + self.QUEDA + 0.02
            self._cache[t] = (pts, fim)
        return self._cache[t]

    def mostrar(self, texto_, cor, centro_x, tela_w=None):
        t = self._normalizar(texto_).strip()
        if len(t) > self.MAX_CHARS:                                # nome de rede comprido: corta com ".."
            t = t[:self.MAX_CHARS - 2].rstrip() + ".."
        if not t.strip():
            return
        largura, _ = self.medidas(len(t))
        self.pontos, self.fim = self._pontos_de(t)
        self.texto, self.cor = t, cor
        meio = LADO + centro_x                                    # centro do ícone, em pixels da tela
        tela_w = tela_w or (meio * 2)
        esq = int(round(min(max(0, meio - self.W / 2), max(0, tela_w - self.W))))
        Camada.set_margin(self, Camada.Edge.LEFT, esq)           # só a margem muda; o tamanho é sempre o mesmo
        x0 = min(max(0.0, meio - esq - largura / 2), self.W - largura)
        self.caixa = (math.floor(x0), largura)
        if self._fechar_id:
            GLib.source_remove(self._fechar_id)
            self._fechar_id = None
        self.e, self.t0, self.saindo = 0.0, None, False
        if not self.get_visible():
            self.set_visible(True)
        self._animar()

    def esconder(self):
        if self.get_visible() and self.texto and not self.saindo:
            self.e, self.t0, self.saindo = 0.0, None, True
            self._animar()

    def _animar(self):
        self.area.queue_draw()
        if self.tique is None:
            self.tique = self.area.add_tick_callback(self._quadro)

    def _quadro(self, _w, relogio):
        agora = relogio.get_frame_time() / 1e6
        if self.t0 is None:
            self.t0 = agora
        self.e = agora - self.t0
        self.area.queue_draw()
        if self.saindo and self.e > self.SAIDA:
            self.tique, self.texto = None, ""
            self._fechar_id = GLib.timeout_add(int(self.ESPERA * 1000), self._fechar)
            return GLib.SOURCE_REMOVE
        if not self.saindo and self.e > self.fim:
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _fechar(self):
        self._fechar_id = None
        if not self.texto:
            self.set_visible(False)
        return GLib.SOURCE_REMOVE

    def _carimbo(self, cor):
        """O halo de um ponto colorido, pintado uma vez numa superfície pequena (antes: um gradiente
        de 13 paradas por ponto, por quadro)."""
        chave = tuple(round(c, 3) for c in cor)
        if chave not in self._carimbos:
            import cairo
            r = self.PASSO * 1.6 * 1.35
            lado = int(math.ceil(2 * r)) + 2
            s = cairo.ImageSurface(cairo.FORMAT_ARGB32, lado, lado)
            halo(cairo.Context(s), lado / 2, lado / 2, self.PASSO * 1.6, cor, 0.22)
            self._carimbos[chave] = (s, lado / 2)
        return self._carimbos[chave]

    def desenhar(self, _a, cr, w, h):
        if not self.texto:
            return                                                # aberta e vazia: nada a pintar
        e = self.e
        fundo = min(1.0, e / 0.18) if not self.saindo else max(0.0, 1 - e / self.SAIDA)
        x0, bw = self.caixa
        bh, r = h, 9
        cr.translate(x0, 0)
        cr.new_sub_path()
        for (ax, ay, a0) in ((bw - r, r, -math.pi / 2), (bw - r, bh - r, 0), (r, bh - r, math.pi / 2), (r, r, math.pi)):
            cr.arc(ax, ay, r, a0, a0 + math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(*FUNDO, 0.9 * fundo)
        cr.fill()
        carimbo = self._carimbo(self.cor) if colorida(self.cor) else None
        raio = self.PASSO * 0.38
        for x, y, atraso, queda in self.pontos:
            if not self.saindo:
                k = min(1.0, max(0.0, (e - atraso) / self.QUEDA))
                k = 1 - (1 - k) ** 3                              # falls and lands softly
                yy, a = y - queda * (1 - k), k
            else:
                k = min(1.0, e / self.SAIDA)
                yy, a = y - queda * 0.6 * k, 1 - k
            if a <= 0.01:
                continue
            if carimbo is not None:
                s, meio = carimbo
                cr.set_source_surface(s, x - meio, yy - meio)
                cr.paint_with_alpha(a)
            cr.set_source_rgba(*self.cor, a)
            cr.arc(x, yy, raio, 0, 2 * math.pi)
            cr.fill()


# ---------- visual Solid (Material) ----------
# Ícones Material: os "md" (Material Design Icons) da Nerd Font, desenhados como texto (sem arquivos).
F_ICONES_S = "FiraCode Nerd Font Propo"
GLIFOS_S = {"vol_alto": 0xF057E, "vol_medio": 0xF0580, "vol_baixo": 0xF057F, "mudo": 0xF0581,
            "wifi": (0xF092F, 0xF091F, 0xF0922, 0xF0925, 0xF0928), "wifi_off": 0xF05AA,
            "bt": 0xF00AF, "bt_off": 0xF00B2, "bt_con": 0xF00B1, "sino": 0xF009A, "sino_off": 0xF009B,
            "gear": 0xF0493, "shield": 0xF0498, "central": 0xF062E, "min": 0xF05B0}
GL_S, PAD_S = 16, 7                  # tamanho do ícone (px) e o respiro de cada lado
WS_PILULA, WS_PONTO, WS_GAP = 20, 8, 6


def _layout_s(cr, s, fonte, px, peso=None):
    lay = PangoCairo.create_layout(cr)
    fd = Pango.FontDescription.from_string(fonte)
    fd.set_absolute_size(px * Pango.SCALE)
    if peso is not None:
        fd.set_weight(peso)
    lay.set_font_description(fd)
    lay.set_text(s, -1)
    return lay


def texto_s(cr, s, x, y_meio, px, cor, peso=Pango.Weight.MEDIUM, alfa=1.0, medir=False):
    lay = _layout_s(cr, s, "Inter", px, peso)
    w, h = lay.get_pixel_size()
    if not medir:
        cr.set_source_rgba(*cor, alfa)
        cr.move_to(x, y_meio - h / 2)
        PangoCairo.show_layout(cr, lay)
    return w


def glifo_s(cr, cp, cx, cy, cor, alfa=1.0, px=GL_S):
    """Um ícone Material centrado (pela tinta, não pela linha do texto) em cx, cy."""
    lay = _layout_s(cr, chr(cp), F_ICONES_S, px)
    tinta, _ = lay.get_pixel_extents()
    cr.set_source_rgba(*cor, alfa)
    cr.move_to(cx - tinta.x - tinta.width / 2, cy - tinta.y - tinta.height / 2)
    PangoCairo.show_layout(cr, lay)


def pilula(cr, x, y, w, h, r=None):
    r = min(h / 2, w / 2) if r is None else min(r, h / 2, w / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def cor_s(nome):
    return solido.rgb(nome)


def glifo_do_modulo(b, m):
    """(código do ícone, papel da cor, alfa) dos módulos que são só um ícone."""
    n = m.nome
    if n == "vol":
        g = "mudo" if b.mudo else "vol_alto" if b.volume >= 60 else "vol_medio" if b.volume >= 25 else "vol_baixo"
        return GLIFOS_S[g], "on_surface", 0.6 if b.mudo else 1.0
    if n == "wifi":
        return (GLIFOS_S["wifi"][max(0, min(4, b.wifi_nivel))], "on_surface", 1.0) if b.wifi_nivel else \
            (GLIFOS_S["wifi_off"], "on_surface_variant", 0.7)
    if n == "bt":
        if b.bt_conectados:
            return GLIFOS_S["bt_con"], "on_surface", 1.0
        return (GLIFOS_S["bt"], "on_surface", 1.0) if b.bt_ligado else (GLIFOS_S["bt_off"], "on_surface_variant", 0.7)
    if n == "ultrasu":
        mins = minutos_ultrasu(b.ultrasu)
        if mins is None:
            return GLIFOS_S["shield"], "on_surface_variant", 0.6
        return GLIFOS_S["shield"], "primary" if mins > 7 * 60 else "tertiary" if mins > 3 * 60 else "error", 1.0
    if n == "bell":
        return (GLIFOS_S["sino_off"], "on_surface_variant", 0.8) if b.dnd else (GLIFOS_S["sino"], "on_surface", 1.0)
    if n in ("gear", "central", "min"):
        return GLIFOS_S[n], "on_surface", 1.0
    return None


def largura_solida(b, m, cr):
    if isinstance(m, Workspaces):
        return 2 * 8 + WS_PILULA + 4 * WS_PONTO + 4 * WS_GAP
    if isinstance(m, Minimizados):
        m.visivel = b.minimizados > 0
        return 0 if not b.minimizados else \
            PAD_S + GL_S + 4 + texto_s(cr, str(b.minimizados), 0, 0, 12, (1, 1, 1), medir=True) + PAD_S
    if isinstance(m, Relogio):
        hora = time.strftime("%H:%M")
        w = PAD_S + 2 + texto_s(cr, hora, 0, 0, 14, (1, 1, 1), Pango.Weight.SEMIBOLD, medir=True) + PAD_S + 2
        if CFG["date"]:
            w += 8 + texto_s(cr, time.strftime("%a, %b %d"), 0, 0, 12, (1, 1, 1), Pango.Weight.NORMAL, medir=True)
        return w
    if isinstance(m, Idioma):
        return PAD_S + texto_s(cr, b.idioma, 0, 0, 12, (1, 1, 1), Pango.Weight.SEMIBOLD, medir=True) + PAD_S
    if isinstance(m, Bateria):
        return PAD_S + 22 + 6 + texto_s(cr, str(b.bat_nivel), 0, 0, 12, (1, 1, 1), medir=True) + PAD_S
    if isinstance(m, EspacoBandeja):
        return m.largura(cr)
    return PAD_S + GL_S + PAD_S


WS_GOTA_S = 0.42          # s: a gota de um workspace que nasce (cai no ponto) / some (escorre e solta)


def _ws_mola(b, chave, valor, omega):
    """Molas dos workspaces moram junto das da barra (b.molas_s, em pares) pra andarem no mesmo tique."""
    if chave not in b.molas_s:
        b.molas_s[chave] = (solido.Mola(valor, omega), solido.Mola(0.0))
    return b.molas_s[chave][0]


def desenhar_workspaces_solido(b, m, cr, x, cy, dim):
    """Os 5 workspaces: pontos + a pílula primária do ativo. Trocar de workspace é um METABALL em molas
    criticamente amortecidas: a frente da pílula (mola rápida) dispara pro ponto novo e a traseira (mola
    lenta) fica pra trás — primeiro ela ESTICA, depois um pescoço líquido (solido.ponte) liga as duas e
    afina, e a traseira chega e se funde na pílula nova. Trocas seguidas só mudam o alvo das molas (a
    velocidade continua: retoma suave do meio). Workspace que nasce cai como gota no ponto; o que some
    escorre pra baixo e solta. Tudo cairo, formas sólidas; parado não há tique (as molas param sozinhas)."""
    agora = time.monotonic()
    primeira = b.ws_antes is None
    # larguras ALVO de cada casa (o ativo é largo) -> centros do layout final (alvo das molas e dos cliques)
    alvos, xx, m.casas = [], 8.0, []
    for k in range(5):
        lw = WS_PILULA if k + 1 == b.ws_ativo else WS_PONTO
        alvos.append(xx + lw / 2)
        m.casas.append((x + xx, x + xx + lw))
        xx += lw + WS_GAP
    # larguras ATUAIS (molas) -> centros de agora (os pontos abrem espaço pra pílula, suave)
    centros, xx = [], 8.0
    for k in range(5):
        lw_alvo = WS_PILULA if k + 1 == b.ws_ativo else WS_PONTO
        mola = _ws_mola(b, f"ws:larg{k}", lw_alvo, 20.0)
        mola.alvo = lw_alvo
        lw = mola.x
        centros.append(xx + lw / 2)
        xx += lw + WS_GAP
    # nível de "usado" de cada ponto (raio 3->4, alfa 0.4->1) + as gotas de quem nasce/some
    usados = {n for n in b.ws_usados if 1 <= n <= 5}
    if not primeira:
        for n in usados - b.ws_antes:
            b.ws_gotas = [g for g in b.ws_gotas if g[1] != n - 1] + [("entra", n - 1, agora)]
        for n in b.ws_antes - usados:
            b.ws_gotas = [g for g in b.ws_gotas if g[1] != n - 1] + [("sai", n - 1, agora)]
    b.ws_antes = set(usados)
    b.ws_gotas = [g for g in b.ws_gotas if agora - g[2] < WS_GOTA_S]
    for k in range(5):
        u = _ws_mola(b, f"ws:uso{k}", 1.0 if k + 1 in usados else 0.0, 12.0)
        u.alvo = 1.0 if k + 1 in usados else 0.0
        gota = next((g for g in b.ws_gotas if g[1] == k), None)
        if gota and gota[0] == "entra":                     # o ponto só "enche" quando a gota chega
            u_vis = u.x * solido.suave(0.55, 1.0, (agora - gota[2]) / WS_GOTA_S)
        else:
            u_vis = u.x
        r = 3 + u_vis
        px = x + centros[k]
        cr.set_source_rgba(*dim, 0.4 + 0.6 * u_vis)
        cr.arc(px, cy, r, 0, 2 * math.pi)
        cr.fill()
        if gota:                                            # a gota: cai de cima / escorre pra baixo
            t = (agora - gota[2]) / WS_GOTA_S
            if gota[0] == "entra":
                e = 1 - (1 - min(1.0, t)) ** 3
                rg = 2.2 + 0.8 * e
                gy = (cy - 10) + 10 * e
                cr.set_source_rgba(*dim, 0.5 + 0.5 * e)
                cr.arc(px, gy, rg, 0, 2 * math.pi)
                cr.fill()
                if gy + rg < cy - r:                         # ainda acima: o pescoço puxando pro ponto
                    solido.pescoco(cr, px, cy - r + 0.5, gy + rg, 2.6, rg * 0.8, 7)
            else:
                e = min(1.0, t) ** 2
                rg = 3.2 - 1.4 * e
                gy = cy + 11 * e
                cr.set_source_rgba(*dim, 0.9 * (1 - solido.suave(0.5, 1.0, t)))
                cr.arc(px, gy, rg, 0, 2 * math.pi)
                cr.fill()
                if gy - rg > cy + r:                         # já saiu do ponto: o pescoço afina e solta
                    solido.pescoco(cr, px, cy + r - 0.5, gy - rg, 2.6, rg * 0.8, 7)
    # a pílula: frente rápida, traseira lenta (as duas criticamente amortecidas, sem passar do alvo)
    k_at = min(4, max(0, b.ws_ativo - 1))
    frente = _ws_mola(b, "ws:frente", alvos[k_at], 26.0)
    tras = _ws_mola(b, "ws:tras", alvos[k_at], 13.0)
    frente.alvo = tras.alvo = alvos[k_at]
    if not (1 <= b.ws_ativo <= 5):
        mexe = any(not b.molas_s[c][0].parada for c in b.molas_s if c.startswith("ws:")) or b.ws_gotas
        if mexe:
            b.animar_solido()
        return
    a, t_ = x + frente.x, x + tras.x
    meia, hp = WS_PILULA / 2, 8.0
    dist = abs(a - t_)
    cr.set_source_rgb(*cor_s("primary"))
    if dist < 12:                                           # perto: uma pílula só, ESTICADA entre as duas
        x0, x1 = min(a, t_) - meia, max(a, t_) + meia
        h = hp * max(0.72, WS_PILULA / (x1 - x0)) ** 0.5    # estica = afina (o volume se conserva)
        pilula(cr, x0, cy - h / 2, x1 - x0, h)
        cr.fill()
    else:                                                   # longe: cabeça + gota traseira + pescoço
        rt = 4.0 * max(0.55, 1 - (dist - 12) / 60)          # a traseira encolhe enquanto o pescoço estica
        pilula(cr, a - meia, cy - hp / 2, WS_PILULA, hp)
        cr.fill()
        pilula(cr, t_ - rt * 1.4, cy - rt, rt * 2.8, rt * 2)
        cr.fill()
        if a > t_:                                          # ponte entre a traseira (esq.) e a frente (dir.)
            solido.ponte(cr, t_ + rt * 1.4, a - meia, cy - rt, rt * 2, 34)
        else:
            solido.ponte(cr, a + meia, t_ - rt * 1.4, cy - rt, rt * 2, 34)
    if b.ws_gotas or any(not b.molas_s[c][0].parada for c in b.molas_s if c.startswith("ws:")):
        b.animar_solido()


def desenhar_modulo_solido(b, m, cr, x):
    cy = ALTURA / 2
    fg, dim = cor_s("on_surface"), cor_s("on_surface_variant")
    if isinstance(m, Workspaces):
        desenhar_workspaces_solido(b, m, cr, x, cy, dim)
    elif isinstance(m, Minimizados):
        glifo_s(cr, GLIFOS_S["min"], x + PAD_S + GL_S / 2, cy, fg)
        texto_s(cr, str(b.minimizados), x + PAD_S + GL_S + 4, cy, 12, fg)
    elif isinstance(m, Relogio):
        w = texto_s(cr, time.strftime("%H:%M"), x + PAD_S + 2, cy, 14, fg, Pango.Weight.SEMIBOLD)
        if CFG["date"]:
            texto_s(cr, time.strftime("%a, %b %d"), x + PAD_S + 2 + w + 8, cy, 12, dim, Pango.Weight.NORMAL)
    elif isinstance(m, Idioma):
        texto_s(cr, b.idioma, x + PAD_S, cy, 12, fg, Pango.Weight.SEMIBOLD)
    elif isinstance(m, Bateria):
        bx, by, bw, bh = x + PAD_S, cy - 5.5, 20, 11            # a bateria deitada do Pixel: corpo + nível
        pilula(cr, bx, by, bw, bh, 3.5)
        cr.set_source_rgba(*fg, 0.28)
        cr.fill()
        cr.rectangle(bx + bw + 0.5, cy - 2.5, 1.8, 5)
        cr.fill()
        if b.bat_nivel <= 12 and not b.na_tomada:
            papel = "error"
        elif b.na_tomada:
            papel = "primary"
        elif b.perfil == "power-saver":
            papel = "tertiary"
        else:
            papel = "on_surface"
        cr.save()
        pilula(cr, bx, by, bw, bh, 3.5)
        cr.clip()
        cr.rectangle(bx, by, bw * max(0.04, min(1.0, b.bat_nivel / 100)), bh)
        cr.set_source_rgb(*cor_s(papel))
        cr.fill()
        cr.restore()
        texto_s(cr, str(b.bat_nivel), bx + 22 + 6, cy, 12, cor_s("primary") if b.na_tomada else fg)
    elif isinstance(m, EspacoBandeja):
        pass
    else:
        g = glifo_do_modulo(b, m)
        if g:
            cp, papel, alfa = g
            gx = x + PAD_S + GL_S / 2
            glifo_s(cr, cp, gx, cy, cor_s(papel), alfa)
            if m.nome == "bell" and b.nao_lidas and not b.dnd:       # não lidas: um ponto do erro, como no Pixel
                cr.arc(gx + 6, cy - 6, 3.5, 0, 2 * math.pi)
                cr.set_source_rgb(*cor_s("error"))
                cr.fill()


def ilhas_solidas(b):
    """As três ilhas (esquerda, centro, direita) como (x0, x1) no LAYOUT FINAL (alvo das molas)."""
    out = []
    for lista in (b.esquerda, b.centro, b.direita):
        vis = [m for m in b.visiveis(lista) if m.w > 0]
        if vis:
            out.append((min(m.x for m in vis) - 2, max(m.x + m.w for m in vis) + 2))
    return out


ENTRA_S, SAI_S = 0.5, 0.55            # s: a gota de um ícone novo subindo pra ilha / a de um que saiu caindo
PONTE_S = 46                           # px: ilhas mais perto que isso se puxam com uma ponte líquida


def _molas(b, m):
    """As molas (x, largura) de um módulo; criadas paradas no lugar certo (sem animar no 1º quadro)."""
    if m.nome not in b.molas_s:
        b.molas_s[m.nome] = (solido.Mola(m.x), solido.Mola(m.w))
    return b.molas_s[m.nome]


def desenhar_barra_solida(b, cr, w):
    """As ilhas Material. Tudo que muda de tamanho anda em MOLAS (criticamente amortecidas): um ícone
    chegando abre espaço e entra como uma gota que sobe e se funde na ilha; um que sai cai como gota, com
    o pescoço afinando até romper. Ilhas próximas se puxam com uma ponte líquida. Parado, nenhum timer."""
    b.medir(cr, w)
    y0, ph = 3, ALTURA - 6
    agora = time.monotonic()
    primeira = not b.molas_s
    mexe = False
    grupos = []
    for lista in (b.esquerda, b.centro, b.direita):
        partes = []
        for m in b.visiveis(lista):
            mx, mw = _molas(b, m)
            alvo_w = m.w if m.w > 0 else 0.0
            if mw.alvo <= 0.5 < alvo_w:                                   # chegou: nasce no lugar (largura 0)
                mx.x = mx.alvo = m.x + m.w / 2
                mx.v = 0.0
                if not primeira:
                    b.gotas_s.append(("entra", m.nome, agora))             # ...e entra como gota
            elif not primeira and alvo_w <= 0.5 < mw.alvo:               # saiu: cai como gota
                b.gotas_s.append(("sai", (mx.x + mw.x / 2), agora))
            if m.w > 0:
                mx.alvo = m.x
            mw.alvo = alvo_w
            if mx.x != mx.alvo or mw.x != mw.alvo:
                mexe = True
            if mw.x > 0.5:
                partes.append((m, mx.x, mw.x))
        if partes:
            grupos.append(partes)
    b.gotas_s = [g for g in b.gotas_s if agora - g[2] < (ENTRA_S if g[0] == "entra" else SAI_S)]
    if mexe or b.gotas_s:
        b.animar_solido()
    sup = cor_s("surface_container")
    cr.set_source_rgb(*sup)
    ilhas = []
    for partes in grupos:
        x0 = min(x for _m, x, _w in partes) - 2
        x1 = max(x + ww for _m, x, ww in partes) + 2
        ilhas.append((x0, x1))
        pilula(cr, x0, y0, x1 - x0, ph)
        cr.fill()
    for (_a, xa), (xb, _b) in zip(ilhas, ilhas[1:]):                    # ilhas perto: a ponte líquida
        solido.ponte(cr, xa, xb, y0, ph, PONTE_S)
    b.gota.ilhas = ilhas
    borda = y0 + ph
    for tipo, quem, t0 in b.gotas_s:                                    # as gotas (mesma cor da ilha)
        if tipo == "entra":
            par = b.molas_s.get(quem)
            if not par:
                continue
            cx = par[0].x + par[1].x / 2
            k = solido.suave(0.0, 1.0, (agora - t0) / ENTRA_S)
            e = 1 - (1 - k) ** 3
            r = 4 + 5 * e
            cy = (ALTURA + VAZAO - 6) + (ALTURA / 2 - (ALTURA + VAZAO - 6)) * e
        else:
            cx = quem
            k = (agora - t0) / SAI_S
            e = k * k
            r = 9 - 5 * e
            cy = ALTURA / 2 + ((ALTURA + VAZAO - 5) - ALTURA / 2) * e
        alfa = 1.0 if tipo == "entra" else 1 - solido.suave(0.55, 1.0, k)
        cr.set_source_rgba(*sup, alfa)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.fill()
        if cy - r > borda - 1:                                            # fora da ilha: o pescoço
            solido.pescoco(cr, cx, borda, cy - r, 9, r * 0.8, 15)
    b.anotar_solido(ilhas)
    on = cor_s("on_surface")
    for partes in grupos:
        for m, x, ww in partes:
            if m is b.sobre and CFG["hover"] and not isinstance(m, EspacoBandeja):   # a "state layer" do Material
                pilula(cr, x + 1, y0 + 3, ww - 2, ph - 6)
                cr.set_source_rgba(*on, 0.09)
                cr.fill()
            entrada = next((agora - g[2] for g in b.gotas_s if g[0] == "entra" and g[1] == m.nome), None)
            alfa_ = 1.0 if entrada is None else solido.suave(0.45, 1.0, entrada / ENTRA_S)
            if m.w == 0:                                                  # saindo: some enquanto encolhe
                alfa_ = min(alfa_, max(0.0, ww / 30 - 0.2))
            if alfa_ <= 0.01:
                continue
            cr.save()
            cr.rectangle(x, 0, ww, ALTURA)                                # encolhendo: o conteúdo não vaza
            cr.clip()
            if alfa_ < 0.99:
                cr.push_group()
            desenhar_modulo_solido(b, m, cr, x)
            if alfa_ < 0.99:
                cr.pop_group_to_source()
                cr.paint_with_alpha(alfa_)
            cr.restore()


def texto_dica_solida(b, m):
    n = m.nome
    if n == "ultrasu":
        mins = minutos_ultrasu(b.ultrasu)
        return f"Admin mode · {mins // 60}h {mins % 60:02d}m left" if mins is not None else "Admin mode off"
    textos = {"vol": f"Volume {b.volume}%" + (" · muted" if b.mudo else ""),
              "wifi": b.wifi_nome or "Offline",
              "bt": (f"Bluetooth · {b.bt_conectados} connected" if b.bt_conectados else
                     "Bluetooth on" if b.bt_ligado else "Bluetooth off"),
              "bat": b.bat_dica or f"{b.bat_nivel}%", "lang": f"Keyboard · {b.idioma}",
              "min": f"{b.minimizados} minimized", "bell": "Hermes" + (" · Do not disturb" if b.dnd else ""),
              "gear": "Settings", "central": "Hestia", "clock": time.strftime("%A, %B %d")}
    return textos.get(n, m.dica)


def _clique_workspaces_solido(self, botao, x):
    for k, (a, b_) in enumerate(getattr(self, "casas", [])):
        if a - WS_GAP / 2 <= x < b_ + WS_GAP / 2 and botao == 1:
            lancar(f"hyprctl dispatch workspace {k + 1}")
            return


if SOLIDO:
    Workspaces.clique = _clique_workspaces_solido


class GotaSolida(Gtk.Window):
    """A dica do Solid: uma pílula sólida (a cor da ilha da barra) que nasce do ícone num METABALL — uma gota
    escorre da borda de baixo da ilha, estica num pescoço líquido que afina e se solta, virando a pílula com
    o texto. Desenhada em cairo (a barra roda no renderizador cairo: abrir OpenGL aqui custaria ~100 MB de
    RAM pra sempre); o pescoço é o "conector gosmento" clássico: duas curvas que afinam com a distância.
    Só anima na entrada/saída; parada, é uma pílula. Mesma interface da Gota: mostrar(...) e esconder()."""

    MAX_W, ALT = 320, 30
    SOBE = 8                    # a camada começa 8 px DENTRO da barra: a gota nasce na borda da ilha
    GAP = 12                    # distância final entre a ilha e a pílula
    PESCOCO = 11                # até essa distância o pescoço existe (depois se solta: GAP é maior)
    ENTRA, SAI, ESPERA = 0.38, 0.2, 1.5

    def __init__(self):
        super().__init__(decorated=False)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "aerobar-gota")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_anchor(self, Camada.Edge.LEFT, True)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_margin(self, Camada.Edge.TOP, TOPO + ALTURA - self.SOBE)
        self.add_css_class("aerobar-gota")
        prov = Gtk.CssProvider()
        prov.load_from_string(".aerobar-gota { background: none; background-color: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.W, self.H = self.MAX_W, self.SOBE - 3 + self.GAP + self.ALT + 4
        self.area = Gtk.DrawingArea(content_width=self.W, content_height=self.H)
        self.area.set_draw_func(self.desenhar)
        self.set_child(self.area)
        self.set_default_size(self.W, self.H)
        self.set_size_request(self.W, self.H)
        self.connect("realize", self._sem_entrada)
        self.ilhas = []
        self.texto, self.entrando, self.t, self.t0, self.tique = "", True, 1.0, None, None
        self.cx, self.bw, self.limites = 0.0, 0.0, (-1e9, 1e9)
        self._fechar_id = None

    def _sem_entrada(self, *_):
        import cairo
        if self.get_surface() is not None:
            self.get_surface().set_input_region(cairo.Region())

    def mostrar(self, texto_, _cor, centro_x, tela_w=None):
        if not texto_:
            return
        lay = self.area.create_pango_layout(texto_)
        lay.set_font_description(Pango.FontDescription.from_string("Inter Medium 9.5"))
        tw = lay.get_pixel_size()[0]
        self.bw = min(self.MAX_W - 8, tw + 28)
        meio = LADO + centro_x
        tela_w = tela_w or (meio * 2)
        esq = int(round(min(max(0, meio - self.W / 2), max(0, tela_w - self.W))))
        Camada.set_margin(self, Camada.Edge.LEFT, esq)
        self.cx = min(max(self.bw / 2 + 2, meio - esq), self.W - self.bw / 2 - 2)
        lim = next(((a, b) for a, b in self.ilhas if a <= centro_x <= b), None)
        r = (ALTURA - 6) / 2                                    # só a parte reta da borda de baixo da ilha
        self.limites = (lim[0] + r + LADO - esq, lim[1] - r + LADO - esq) if lim else (-1e9, 1e9)
        if self._fechar_id:
            GLib.source_remove(self._fechar_id)
            self._fechar_id = None
        self.texto = texto_
        if not self.get_visible():
            self.set_visible(True)
        self._animar(True)

    def esconder(self):
        if self.get_visible() and self.texto and (self.entrando or self.tique):
            if not self.entrando:
                return
            self._animar(False)

    def sumir_ja(self):
        """Some NA HORA (sem animar) e devolve (largura, altura, topo abaixo da ilha) da pílula, se ela
        estava inteira na tela — quem abre no lugar dela (a gaveta do calendário) nasce desse formato."""
        forma = None
        if self.get_visible() and self.texto and self.entrando and self.t >= 1.0:
            forma = (self.bw, float(self.ALT), float(self.GAP))
        else:                            # ainda entrando/saindo: só recolhe pra ilha, como sempre
            self.esconder()
            return None
        if self.tique is not None:
            self.area.remove_tick_callback(self.tique)
            self.tique = None

        def apagar():                    # 60 ms depois: a gaveta já desenhou o 1º quadro por cima (sem vão)
            self.texto, self.entrando = "", False
            self.area.queue_draw()
            return GLib.SOURCE_REMOVE
        GLib.timeout_add(60, apagar)
        return forma

    def _animar(self, entrando):
        self.entrando, self.t0 = entrando, None
        self.t = 0.0
        self.area.queue_draw()
        if self.tique is None:
            self.tique = self.area.add_tick_callback(self._quadro)

    def _quadro(self, _w, relogio):
        agora = relogio.get_frame_time() / 1e6
        if self.t0 is None:
            self.t0 = agora
        self.t = min(1.0, (agora - self.t0) / (self.ENTRA if self.entrando else self.SAI))
        self.area.queue_draw()
        if self.t < 1.0:
            return GLib.SOURCE_CONTINUE
        self.tique = None
        if not self.entrando:
            self.texto = ""
            self._fechar_id = GLib.timeout_add(int(self.ESPERA * 1000), self._fechar)
        return GLib.SOURCE_REMOVE

    def _fechar(self):
        self._fechar_id = None
        if not self.texto:
            self.set_visible(False)
        return GLib.SOURCE_REMOVE

    def desenhar(self, _a, cr, w, h):
        if not self.texto:
            return
        if self.entrando:
            e, alfa_txt = solido.enfatizado(self.t), solido.suave(0.55, 1.0, self.t)
        else:
            e, alfa_txt = 1 - self.t * self.t, 1 - solido.suave(0.0, 0.35, self.t)
        borda = self.SOBE - 3                                   # a borda de baixo da ilha (y, dentro da camada)
        topo_fim = borda + self.GAP
        hh = 5 + (self.ALT / 2 - 5) * e
        hw = 7 + (self.bw / 2 - 7) * e
        topo = borda - 4 + (topo_fim + 4 - borda) * e           # a gota começa grudada na borda
        cr.set_source_rgb(*cor_s("surface_container"))
        pilula(cr, self.cx - hw, topo, 2 * hw, 2 * hh)
        cr.fill()
        a, b = self.limites                                     # o pescoço líquido (solido.pescoco)
        solido.pescoco(cr, self.cx, borda, topo, max(2.0, min(14.0, self.cx - a, b - self.cx)),
                       min(hw - 1, 12.0), self.PESCOCO)
        if alfa_txt > 0.01:
            lay = PangoCairo.create_layout(cr)
            lay.set_font_description(Pango.FontDescription.from_string("Inter Medium 9.5"))
            lay.set_text(self.texto, -1)
            tw, th = lay.get_pixel_size()
            cr.set_source_rgba(*cor_s("on_surface"), alfa_txt)
            cr.move_to(self.cx - tw / 2, topo + hh - th / 2)
            PangoCairo.show_layout(cr, lay)


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Bar", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        if not self.get_windows():
            Barra(self)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(App().run(sys.argv[:1]))
