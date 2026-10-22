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

The layer is called "waybar" on purpose: the tray, the quick center and Hermes already line up with it.
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

H = os.path.expanduser("~")
BIN = os.path.join(H, ".local/bin")
BANDEJA = os.path.join(H, ".cache/aero-shell/bandeja.json")
VISTO = os.path.join(H, ".cache/aero-shell/sino-visto")      # last notification id seen in the bell
CONFIG = os.path.join(H, ".config/aero/aerobar.json")
# Everything the person can change (Settings -> AeroBar, or the JSON by hand). Bad values fall back.
PADRAO = {
    "background": "solid",      # solid | glass (more see-through) | none (only the icons float)
    "opacity": 0.9,             # of the background, when there is one
    "outline": True,            # the thin line around the bar
    "radius": 14,
    "height": 34, "margin_top": 8, "margin_sides": 12,
    "date": True,               # the date next to the clock
    "icon_size": 15, "clock_size": 17, "number_size": 15,
    "hover": True,              # the soft highlight under the pointer
    "hide": [],                 # modules to hide: workspaces min clock lang vol wifi bt bat ultrasu bell gear central
    "colors": {"text": "#ecebe8", "dim": "#8c8b88", "alert": "#e8312a", "plugged": "#5aa9ff",
               "saver": "#f2c84b", "background": "#0b0b0c"},
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
    for k in ("outline", "date", "hover"):
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
LARGURA_ICONE_BANDEJA = 30

FG = _hex(CFG["colors"]["text"], PADRAO["colors"]["text"])
DIM = _hex(CFG["colors"]["dim"], PADRAO["colors"]["dim"])
RED = _hex(CFG["colors"]["alert"], PADRAO["colors"]["alert"])
BLUE = _hex(CFG["colors"]["plugged"], PADRAO["colors"]["plugged"])
YELLOW = _hex(CFG["colors"]["saver"], PADRAO["colors"]["saver"])
FUNDO = _hex(CFG["colors"]["background"], PADRAO["colors"]["background"])
ICONE = CFG["icon_size"]
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

def icone(cr, nome_ou_linhas, x, y, tamanho, cor, alfa=1.0):
    linhas = ICONES[nome_ou_linhas] if isinstance(nome_ou_linhas, str) else nome_ou_linhas
    p = tamanho / 7
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
        texto(cr, None, str(b.bat_nivel), x + 8 + W + 6, ALTURA / 2, F_NUMERO,
              BLUE if b.na_tomada else cor)


class EspacoBandeja(Modulo):
    def largura(self, cr):
        return self.barra.n_bandeja * LARGURA_ICONE_BANDEJA


class Barra(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, decorated=False)
        self.add_css_class("aerobar")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.TOP)
        Camada.set_namespace(self, "waybar")
        for borda in (Camada.Edge.TOP, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_margin(self, Camada.Edge.TOP, TOPO)
        Camada.set_margin(self, Camada.Edge.LEFT, LADO)
        Camada.set_margin(self, Camada.Edge.RIGHT, LADO)
        Camada.auto_exclusive_zone_enable(self)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.set_default_size(-1, ALTURA)
        self.area = Gtk.DrawingArea(content_height=ALTURA, hexpand=True)
        self.area.set_draw_func(self.desenhar)
        self.area.set_has_tooltip(True)
        self.area.connect("query-tooltip", self.dica)
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

        cfg = os.path.join(BIN, "configuracoes")
        som = os.path.join(BIN, "som")
        self.esquerda = [Workspaces(self, "workspaces"),
                         Minimizados(self, "min", {1: f"{BIN}/hypr-minimizados"}, "Minimized windows")]
        self.centro = [Relogio(self, "clock", {1: f"{som} notificacoes abrir"})]
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
                         cor=lambda: RED if self.ultrasu.get("class") in ("ativo", "acabando") else FG,
                         alfa=lambda: 1.0 if self.ultrasu.get("class") in ("ativo", "acabando") else 0.45),
            Sino(self, "bell", lambda: "bellx" if self.dnd else "bell", {1: f"{som} notificacoes abrir"},
                 alfa=lambda: 0.55 if self.dnd else 1.0, dica="Notifications"),
            IconeSimples(self, "gear", "gear", {1: cfg}, dica="Settings"),
            EspacoBandeja(self, "tray"),
            IconeSimples(self, "central", "grid", {1: f"{som} central abrir", 3: cfg}, dica="Quick center"),
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

        self.iniciar_fontes()
        self.present()

    # ---------- layout / drawing ----------

    def visiveis(self, lista):
        return [m for m in lista if m.nome not in CFG["hide"]]

    def medir(self, cr, w):
        def larg(lista):
            return sum(m.largura(cr) for m in lista)
        for m in self.esquerda + self.centro + self.direita:
            m.x, m.w = 0, 0
        esq, cen, dir_ = self.visiveis(self.esquerda), self.visiveis(self.centro), self.visiveis(self.direita)
        ce, de = larg(cen), larg(dir_)
        x = 6
        for m in esq:
            m.x, m.w = x, m.largura(cr)
            x += m.w
        x = w / 2 - ce / 2                                    # the clock: exact middle, always
        for m in cen:
            m.x, m.w = x, m.largura(cr)
            x += m.w
        x = w - 6 - de
        for m in dir_:
            m.x, m.w = x, m.largura(cr)
            x += m.w

    def desenhar(self, _area, cr, w, h):
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

    def modulo_em(self, x):
        for m in self.esquerda + self.centro + self.direita:
            if m.w > 0 and m.x <= x < m.x + m.w:
                return m
        return None

    def mudar_sobre(self, m):
        if m is not self.sobre:
            self.sobre = m
            self.area.queue_draw()

    def ao_clicar(self, g, _n, x, _y):
        m = self.modulo_em(x)
        if m is not None:
            m.clique(g.get_current_button(), x)

    def ao_rolar(self, _c, _dx, dy):
        if self.sobre is not None:
            self.sobre.rolar(dy)
        return True

    def dica(self, _area, x, _y, _kb, tooltip):
        m = self.modulo_em(x)
        if m is None:
            return False
        textos = {"vol": f"Volume {self.volume}%" + (" (muted)" if self.mudo else ""),
                  "wifi": self.wifi_nome or "Offline",
                  "bt": (f"Bluetooth: {self.bt_conectados} connected" if self.bt_conectados else
                         "Bluetooth on" if self.bt_ligado else "Bluetooth off"),
                  "bat": self.bat_dica, "ultrasu": self.ultrasu.get("tooltip", ""),
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

    def ler_workspaces(self):
        try:
            ws = json.loads(rodar("hyprctl", "workspaces", "-j") or "[]")
            ativo = json.loads(rodar("hyprctl", "activeworkspace", "-j") or "{}")
            self.ws_usados = {w["id"] for w in ws if w.get("windows", 0) > 0 and w["id"] > 0}
            self.ws_ativo = ativo.get("id", self.ws_ativo)
        except (ValueError, KeyError, TypeError):
            pass
        self.redesenhar()
        return False

    def ler_idioma(self):
        try:
            for k in json.loads(rodar("hyprctl", "devices", "-j") or "{}").get("keyboards", []):
                if k.get("main"):
                    nome = k.get("active_keymap", "")
                    self.idioma = self._curto(nome)
        except (ValueError, TypeError):
            pass
        self.redesenhar()
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
                        if nome in ("workspace", "workspacev2", "createworkspace", "destroyworkspace", "openwindow",
                                    "closewindow", "movewindow", "movewindowv2", "focusedmon"):
                            GLib.idle_add(self.ler_workspaces)
                        elif nome == "activelayout":
                            GLib.idle_add(self.ler_idioma)
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
        if self.carregando and self.pulso is None:
            self.pulso = self.area.add_tick_callback(self._pulso)
        self.redesenhar()

    def _pulso(self, *_):
        """Only animation of the bar: the charging pulse (a redraw per frame while charging)."""
        if not self.carregando:
            self.pulso = None
            self.redesenhar()
            return GLib.SOURCE_REMOVE
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


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Bar", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        if not self.get_windows():
            Barra(self)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    sys.exit(App().run(sys.argv[:1]))
