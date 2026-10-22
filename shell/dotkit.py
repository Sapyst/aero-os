"""
dotkit.py — DotStyle pieces shared by the whole shell (notification center, quick center, menus...).

  Painel(...)         a panel that is born from bubbles (hermes_dots.CartaoDots without the hover zoom)
  Rolagem(child)      smooth scrolling with inertia + a scrollbar made of dots that shows only while moving
  Interruptor(...)    an on/off switch: a big dot sliding over a track of small dots
  BotaoDots(...)      a flat text button written in Doto, with a soft glow on hover
  Bolha(...)          a colourful app icon inside a round bubble
  tempo_atras(ts)     "now", "5 min", "2 h", "yesterday"...

Everything animates only while something changes (tick callbacks removed when still).
"""
import math
import os
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gtk, Pango  # noqa: E402

import hermes_dots as hd  # noqa: E402

FG = (0.925, 0.92, 0.91)
DIM = (0.55, 0.545, 0.535)
RED = (1.0, 0.231, 0.188)
FUNDO = (0.043, 0.043, 0.047)
CORES = {"background": FUNDO, "text": FG, "dim": DIM, "accent": RED}

CSS = """
.dotkit-titulo { font-family: "Doto"; font-weight: 900; font-size: 17px; letter-spacing: 1px; color: #ecebe8; }
.dotkit-texto { font-family: "Geist", "Inter", sans-serif; font-size: 12.5px; color: alpha(#ecebe8, 0.82); }
.dotkit-forte { font-family: "Geist", "Inter", sans-serif; font-weight: 600; font-size: 13px; color: #ecebe8; }
.dotkit-mono { font-family: "Geist Mono", monospace; font-size: 10.5px; letter-spacing: 0.06em; color: #8c8b88; }
.dotkit-botao { font-family: "Doto"; font-weight: 900; font-size: 13px; letter-spacing: 1px; color: #ecebe8;
  background: none; border: none; box-shadow: none; padding: 4px 10px; border-radius: 99px; min-height: 0; }
.dotkit-botao:hover { background: alpha(#ecebe8, 0.08); }
.dotkit-botao:disabled { color: alpha(#ecebe8, 0.3); }
scrolledwindow.dotkit-rolagem, scrolledwindow.dotkit-rolagem > viewport { background: none; }
window.dots.notif-dots .cartao-som { background: none; border: none; box-shadow: none; padding: 0; margin: 0; animation: none; }
.dotkit-item { border-radius: 16px; padding: 8px 8px 8px 6px; transition: background 180ms ease; }
.dotkit-item:hover { background: alpha(#ecebe8, 0.06); }
"""
_css_ok = [False]


def aplicar_css():
    if _css_ok[0]:
        return
    p = Gtk.CssProvider()
    p.load_from_string(CSS)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)
    _css_ok[0] = True


def suave(a, b, x):
    return hd.suave(a, b, x)


# ---------- panel ----------

class Painel(hd.CartaoDots):
    """A panel that assembles from bubbles like a Hermes card, but stays still under the pointer."""

    def __init__(self, raio=22, origem="right", semente=7, critica=False):
        super().__init__(CORES, raio, critica=critica, semente=semente, origem=origem)

    def sobre(self, dentro):
        pass


# ---------- smooth scrolling + dot scrollbar ----------

class Rolagem(Gtk.Overlay):
    """A ScrolledWindow whose wheel scrolling glides (eased, with inertia) and whose scrollbar is a
    column of dots on the right edge that appears while scrolling and fades away after."""

    def __init__(self, filho, altura_max=460):
        super().__init__()
        aplicar_css()
        self.sw = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     vscrollbar_policy=Gtk.PolicyType.EXTERNAL,   # scrollable, no GTK bar
                                     propagate_natural_height=True, max_content_height=altura_max)
        self.sw.add_css_class("dotkit-rolagem")
        self.sw.set_child(filho)
        self.set_child(self.sw)
        self.barra = Gtk.DrawingArea(content_width=10, halign=Gtk.Align.END, valign=Gtk.Align.FILL,
                                     can_target=False)
        self.barra.set_draw_func(self._desenhar_barra)
        self.add_overlay(self.barra)
        self.adj = self.sw.get_vadjustment()
        self.alvo = 0.0
        self.vel = 0.0
        self.visto = 0.0                      # when the bar last moved (for fading the dots)
        self.tique = None
        rolar = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL |
                                          Gtk.EventControllerScrollFlags.KINETIC)
        rolar.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        rolar.connect("scroll", self._rolar)
        rolar.connect("decelerate", self._desacelerar)
        self.sw.add_controller(rolar)
        self.adj.connect("value-changed", lambda *_: self._mexeu())

    def _limite(self, v):
        return max(self.adj.get_lower(), min(v, self.adj.get_upper() - self.adj.get_page_size()))

    def _rolar(self, ctl, _dx, dy):
        unidade = ctl.get_unit()
        if unidade == Gdk.ScrollUnit.SURFACE:                 # touchpad: follow the fingers 1:1
            self.alvo = self._limite(self.adj.get_value() + dy)
            self.adj.set_value(self.alvo)
            self.vel = 0
        else:                                                 # wheel: glide toward the new target
            base = self.alvo if self.tique is not None else self.adj.get_value()
            self.alvo = self._limite(base + dy * 64)
            self._animar()
        return True

    def _desacelerar(self, _ctl, _vx, vy):
        """Touchpad fling: keep gliding with the finger's speed, slowing down naturally."""
        self.vel = vy / 60.0
        self.alvo = None
        self._animar()

    def _animar(self):
        if self.tique is None:
            self.tique = self.sw.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        atual = self.adj.get_value()
        if self.alvo is None:                                 # inertia
            self.vel *= 0.92
            novo = self._limite(atual + self.vel)
            parado = abs(self.vel) < 0.2 or novo == atual
        else:                                                 # eased glide (critically damped feel)
            novo = atual + (self.alvo - atual) * 0.22
            parado = abs(self.alvo - novo) < 0.5
            if parado:
                novo = self.alvo
        self.adj.set_value(novo)
        if parado:
            self.alvo = self.adj.get_value()
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _mexeu(self):
        self.visto = time.monotonic()
        self.barra.queue_draw()
        if getattr(self, "_fade", None) is None:
            self._fade = self.barra.add_tick_callback(self._fade_quadro)

    def _fade_quadro(self, *_):
        self.barra.queue_draw()
        if time.monotonic() - self.visto > 1.4:
            self._fade = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _desenhar_barra(self, _a, cr, w, h):
        total = self.adj.get_upper() - self.adj.get_lower()
        pagina = self.adj.get_page_size()
        if total <= pagina + 1:
            return
        idade = time.monotonic() - self.visto
        alfa = 1 - suave(0.7, 1.3, idade)
        if alfa <= 0.01:
            return
        passo = 7.0
        n = max(3, int((h - 8) / passo))
        ini = (self.adj.get_value() - self.adj.get_lower()) / total
        fim = ini + pagina / total
        for i in range(n):
            t = (i + 0.5) / n
            dentro = ini <= t <= fim
            cr.set_source_rgba(*FG, alfa * (0.85 if dentro else 0.14))
            cr.arc(w / 2, 4 + i * passo + passo / 2, 1.9 if dentro else 1.2, 0, 2 * math.pi)
            cr.fill()


# ---------- switch ----------

class Interruptor(Gtk.DrawingArea):
    """On/off: a big dot slides over five small dots. ao_mudar(bool) is called on click."""

    def __init__(self, ligado=False, ao_mudar=None):
        super().__init__(content_width=46, content_height=22, valign=Gtk.Align.CENTER)
        self.ligado, self.ao_mudar = ligado, ao_mudar
        self.pos = 1.0 if ligado else 0.0
        self.tique = None
        self.set_cursor_from_name("pointer")
        self.set_draw_func(self._desenhar)
        c = Gtk.GestureClick()
        c.connect("released", lambda *_: self.alternar())
        self.add_controller(c)

    def definir(self, ligado):
        self.ligado = ligado
        self._animar()

    def alternar(self):
        self.definir(not self.ligado)
        if self.ao_mudar:
            self.ao_mudar(self.ligado)

    def _animar(self):
        if self.tique is None:
            self.tique = self.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        alvo = 1.0 if self.ligado else 0.0
        self.pos += (alvo - self.pos) * 0.25
        self.queue_draw()
        if abs(alvo - self.pos) < 0.01:
            self.pos = alvo
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _desenhar(self, _a, cr, w, h):
        cy = h / 2
        for i in range(5):
            x = 9 + i * 7
            cr.set_source_rgba(*FG, 0.18 + 0.5 * self.pos)
            cr.arc(x, cy, 1.4, 0, 2 * math.pi)
            cr.fill()
        x = 9 + 28 * self.pos
        if self.pos > 0.5:                                    # lit knob glows a little
            g = __import__("cairo").RadialGradient(x, cy, 0, x, cy, 14)
            for k in range(9):
                t = k / 8
                g.add_color_stop_rgba(t, *FG, 0.25 * math.exp(-4 * t * t) * (1 - t ** 6))
            cr.set_source(g)
            cr.arc(x, cy, 14, 0, 2 * math.pi)
            cr.fill()
        cr.set_source_rgba(*FG, 0.45 + 0.55 * self.pos)
        cr.arc(x, cy, 6.5, 0, 2 * math.pi)
        cr.fill()


# ---------- text button ----------

def BotaoDots(texto, ao_clicar):
    aplicar_css()
    b = Gtk.Button(label=texto.upper())
    b.add_css_class("dotkit-botao")
    b.connect("clicked", lambda *_: ao_clicar())
    return b


# ---------- app icon in a bubble ----------

def _pixbuf_icone(icone, tamanho):
    if not icone:
        return None
    try:
        if icone.startswith("file://"):
            icone = icone[7:]
        if icone.startswith("/") and os.path.isfile(icone) and os.path.getsize(icone) < 16 * 1024 * 1024:
            return GdkPixbuf.Pixbuf.new_from_file_at_scale(icone, tamanho * 2, tamanho * 2, True)
        tema = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if tema.has_icon(icone):
            p = tema.lookup_icon(icone, None, tamanho * 2, 1, Gtk.TextDirection.NONE, 0)
            f = p.get_file()
            if f and f.get_path():
                return GdkPixbuf.Pixbuf.new_from_file_at_scale(f.get_path(), tamanho * 2, tamanho * 2, True)
    except (GLib.Error, ValueError, TypeError):
        pass
    return None


class Bolha(Gtk.DrawingArea):
    """A round bubble with the app's real, colourful icon inside (or its initial if there is none)."""

    def __init__(self, icone, tamanho=36, nome=""):
        super().__init__(content_width=tamanho, content_height=tamanho, valign=Gtk.Align.START)
        self.pb = _pixbuf_icone(icone, tamanho)
        self.nome = nome
        self.set_draw_func(self._desenhar)

    def _desenhar(self, _a, cr, w, h):
        r = min(w, h) / 2
        cr.arc(w / 2, h / 2, r, 0, 2 * math.pi)
        cr.set_source_rgba(*FG, 0.08)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        if self.pb is not None:
            pw, ph = self.pb.get_width(), self.pb.get_height()
            foto = pw > 96 and abs(pw - ph) > 4 or pw >= 200        # screenshots/photos fill the bubble
            esc = (max(w / pw, h / ph) if foto else min(w / pw, h / ph) * 0.68)
            cr.translate(w / 2 - pw * esc / 2, h / 2 - ph * esc / 2)
            cr.scale(esc, esc)
            Gdk.cairo_set_source_pixbuf(cr, self.pb, 0, 0)
            cr.paint()
        else:
            letra = (self.nome[:1] or "?").upper()
            hd.desenhar_texto(cr, letra, w / 2 - 4.5 * 1.8 / 2 * 1.6, h / 2 - 7 * 1.8 / 2, 1.8, FG, 0.9)
        cr.restore()
        cr.arc(w / 2, h / 2, r - 0.5, 0, 2 * math.pi)
        cr.set_source_rgba(*FG, 0.14)
        cr.set_line_width(1)
        cr.stroke()


# ---------- time ----------

def tempo_atras(ts, agora=None):
    agora = agora or time.time()
    d = max(0, agora - (ts or agora))
    if d < 60:
        return "now"
    if d < 3600:
        return f"{int(d // 60)} min"
    if d < 86400:
        return f"{int(d // 3600)} h"
    if d < 172800:
        return "yesterday"
    return time.strftime("%b %d", time.localtime(ts))


def visual_atual():
    try:
        for linha in open(os.path.expanduser("~/.config/hypr/painel.conf")):
            if linha.startswith("VISUAL="):
                return linha.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "AeroTerm"


def tema_apps(janela):
    """DotStyle for the small apps: loads dotstyle-apps.css (+ the tooltip style) and marks the window.
    Returns True if DotStyle is on."""
    if visual_atual() != "DotStyle":
        return False
    pasta = os.path.dirname(os.path.abspath(__file__))
    for nome in ("dotstyle-apps.css", "dotstyle-dicas.css"):
        caminho = os.path.join(pasta, nome)
        if os.path.exists(caminho):
            p = Gtk.CssProvider()
            p.load_from_path(caminho)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_USER + 3)
    janela.add_css_class("dots")
    return True
