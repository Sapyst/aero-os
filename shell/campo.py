"""
campo.py — the live halftone field of DotStyle, for any window (the same idea as the start menu's filler).

A CampoDots is a DrawingArea placed BEHIND a window's content (Gtk.Overlay). It asks for the shapes of the
content (cards: rounded rectangles, in its own coordinates) and fills the space around them with a grid
of dots: big and bright right against the cards, smaller and fainter farther away, until they vanish.
So a card's edge "dissolves" into dots instead of ending in a line.

Every dot is alive: pulled back to its place, pushed away by the pointer and by the cards, condensing in
when the window opens (from scattered positions) and dissolving when it closes. When everything rests and
the pointer is still, it stops drawing (0 CPU).
"""
import math
import random
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

FG = (0.925, 0.92, 0.91)


def ease(t):
    return 0.0 if t <= 0 else 1.0 if t >= 1 else 1 - (1 - t) ** 3


def dist_ret(px, py, x, y, w, h, r):
    """Signed distance from a point to a rounded rectangle (negative inside)."""
    cx, cy = x + w / 2, y + h / 2
    qx, qy = abs(px - cx) - (w / 2 - r), abs(py - cy) - (h / 2 - r)
    fora = math.hypot(max(qx, 0.0), max(qy, 0.0))
    dentro = min(max(qx, qy), 0.0)
    return fora + dentro - r


class CampoDots(Gtk.DrawingArea):
    """formas() -> [(x, y, w, h, radius)] in this widget's coordinates (the cards)."""

    PASSO = 15
    ALCANCE = 120

    def __init__(self, formas, fundo=(0.043, 0.043, 0.047), alfa_fundo=0.94):
        super().__init__(hexpand=True, vexpand=True, can_target=False)
        self.formas = formas
        self.fundo, self.alfa_fundo = fundo, alfa_fundo
        self.pontos = None
        self.mouse = None
        self.t_abrir = time.monotonic()
        self.t_fechar = None
        self.ao_sumir = None
        self.tique = None
        self._t = time.monotonic()
        self._ativos = 1
        self._assinatura = None
        self.set_draw_func(self._desenhar)

    # --- life cycle ---

    def abrir(self):
        self.t_abrir, self.t_fechar, self.pontos = time.monotonic(), None, None
        self._assinatura = None
        self.animar()

    def fechar(self, ao_sumir=None):
        self.t_fechar = time.monotonic()
        self.ao_sumir = ao_sumir
        self.animar()

    def ponteiro(self, x, y):
        """The window forwards pointer motion here (x, y in this widget's coordinates; None = left)."""
        self.mouse = None if x is None else (x, y)
        self.animar()

    def animar(self):
        if self.tique is None:
            self._t = time.monotonic()
            self.tique = self.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        self.queue_draw()
        agora = time.monotonic()
        if self.t_fechar is not None and agora - self.t_fechar > 0.36:
            self.tique = None
            f, self.ao_sumir = self.ao_sumir, None
            if f:
                GLib.idle_add(lambda: (f(), False)[1])
            return GLib.SOURCE_REMOVE
        if self._ativos == 0 and agora - self.t_abrir > 1.0 and self.t_fechar is None:
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # --- the field ---

    def _semear(self, w, h, formas):
        rnd = random.Random(len(formas) * 7919 + int(w) + int(h))
        p, alc = self.PASSO, self.ALCANCE
        pontos = []
        y = p / 2
        linha = 0
        while y < h:
            x = p / 2 + (p / 2 if linha % 2 else 0)
            while x < w:
                d = min((dist_ret(x, y, *f) for f in formas), default=999)
                if 4 < d < alc:
                    t = d / alc
                    if t < 0.3 or rnd.random() > (t - 0.3) * 1.25:
                        ang = rnd.uniform(0, 2 * math.pi)
                        longe = rnd.uniform(60, 200)
                        # [hx, hy, x, y, vx, vy, r, a, delay]
                        pontos.append([x, y, x + math.cos(ang) * longe, y + math.sin(ang) * longe, 0.0, 0.0,
                                       0.9 + 3.4 * (1 - t) ** 1.6, 0.05 + 0.34 * (1 - t) ** 1.3,
                                       t * 0.28 + rnd.uniform(0, 0.06)])
                x += p
            y += p * 0.87
            linha += 1
        return pontos

    def _desenhar(self, _a, cr, w, h):
        agora = time.monotonic()
        formas = self.formas() or []
        assinatura = (int(w), int(h), tuple((round(x), round(y), round(fw), round(fh)) for x, y, fw, fh, _ in formas))
        if self.pontos is None or assinatura != self._assinatura:
            novo = self.pontos is None
            self.pontos = self._semear(w, h, formas)
            self._assinatura = assinatura
            if not novo:                                       # layout changed while open: no fly-in
                for e in self.pontos:
                    e[2], e[3] = e[0], e[1]
        dt = min(2.0, max(0.25, (agora - self._t) * 60))
        self._t = agora
        fechando = self.t_fechar is not None
        k_abrir = agora - self.t_abrir
        fade = 1 - ease((agora - self.t_fechar) / 0.32) if fechando else 1.0

        # the cards' glass (their background), growing in with the field
        crescer = ease(k_abrir / 0.35) if not fechando else fade
        for x, y, fw, fh, r in formas:
            ix, iy = fw * (1 - crescer) / 2 * 0.08, fh * (1 - crescer) / 2 * 0.08
            xx, yy, ww, hh = x + ix, y + iy, fw - 2 * ix, fh - 2 * iy
            rr = min(r, ww / 2, hh / 2)
            cr.new_sub_path()
            for (ax, ay, a0) in ((xx + ww - rr, yy + rr, -math.pi / 2), (xx + ww - rr, yy + hh - rr, 0),
                                 (xx + rr, yy + hh - rr, math.pi / 2), (xx + rr, yy + rr, math.pi)):
                cr.arc(ax, ay, rr, a0, a0 + math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(*self.fundo, self.alfa_fundo * crescer)
            cr.fill()

        mx = my = None
        if self.mouse is not None and not fechando:
            mx, my = self.mouse
        amort = 0.8 ** dt
        grupos = [[] for _ in range(12)]
        ativos = 0
        cx, cy = w / 2, h / 3
        for e in self.pontos:
            parado = (abs(e[2] - e[0]) < 0.3 and abs(e[3] - e[1]) < 0.3 and abs(e[4]) < 0.03 and abs(e[5]) < 0.03
                      and (mx is None or abs(e[2] - mx) > 150 or abs(e[3] - my) > 150) and not fechando)
            if not parado:
                ativos += 1
                if fechando:                                   # dissolve: drift outward and fade
                    ax, ay = (e[2] - cx) * 0.012, (e[3] - cy) * 0.012
                else:
                    ax, ay = (e[0] - e[2]) * 0.08, (e[1] - e[3]) * 0.08
                if mx is not None:
                    dx, dy = e[2] - mx, e[3] - my
                    if -150 < dx < 150 and -150 < dy < 150:
                        d = math.hypot(dx, dy)
                        if 1 < d < 150:
                            f = (1 - d / 150) ** 2 * 6
                            ax += dx / d * f
                            ay += dy / d * f
                e[4] = (e[4] + ax * dt) * amort
                e[5] = (e[5] + ay * dt) * amort
                e[2] += e[4] * dt
                e[3] += e[5] * dt
            a = e[7] * (fade if fechando else min(1.0, max(0.0, (k_abrir - e[8]) / 0.22)))
            if a <= 0.01:
                continue
            if not parado:
                a = min(1.0, a * (1 + (abs(e[4]) + abs(e[5])) * 0.08))
            grupos[min(11, int(a * 22))].append((e[2], e[3], e[6]))
        self._ativos = ativos + (1 if k_abrir < 1.0 else 0)
        dois_pi = 2 * math.pi
        for k, grupo in enumerate(grupos):
            if not grupo:
                continue
            cr.new_path()
            for x, y, r in grupo:
                cr.new_sub_path()
                cr.arc(x, y, r, 0, dois_pi)
            cr.set_source_rgba(*FG, (k + 0.5) / 22)
            cr.fill()


# ---------- wrapping any window ----------

CSS_ENVOLVER = """
@keyframes campo-pop { from { opacity: 0; transform: scale(0.86); } 65% { opacity: 1; transform: scale(1.03); }
                       to { opacity: 1; transform: none; } }
.campo-conteudo { background: none; border: none; box-shadow: none;
                  animation: campo-pop 420ms cubic-bezier(0.34, 1.36, 0.64, 1) both; }
"""
_css_ok = [False]


def envolver(janela, conteudo, formas=None, halo=90, lados=("start", "end", "top", "bottom")):
    """Puts `conteudo` (already the window's child) inside an Overlay with a live field behind it.
    formas: function returning the cards [(x, y, w, h, r)] in the field's coordinates; default = the
    whole content as one card. Returns the CampoDots (call .abrir() when the window shows)."""
    from gi.repository import Gdk
    if not _css_ok[0]:
        p = Gtk.CssProvider()
        p.load_from_string(CSS_ENVOLVER)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_USER + 3)
        _css_ok[0] = True
    janela.set_child(None)
    campo = None

    def padrao():
        ok, r = conteudo.compute_bounds(campo)
        return [(r.get_x(), r.get_y(), r.get_width(), r.get_height(), 26)] if ok else []
    campo = CampoDots(formas or padrao)
    raiz = Gtk.Overlay()
    raiz.set_child(campo)
    suporte = Gtk.Box()
    for lado in lados:
        getattr(suporte, f"set_margin_{lado}")(halo)
    suporte.append(conteudo)
    raiz.add_overlay(suporte)
    raiz.set_measure_overlay(suporte, True)
    conteudo.add_css_class("campo-conteudo")
    mov = Gtk.EventControllerMotion()
    mov.connect("motion", lambda _c, x, y: campo.ponteiro(x, y))
    mov.connect("leave", lambda *_: campo.ponteiro(None, None))
    janela.add_controller(mov)
    janela.set_child(raiz)
    return campo
