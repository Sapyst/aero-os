"""
hermes_dots.py — the DotStyle pieces of a Hermes card: a 5×7 dot font (so the app name and the time
are written in dots without any font download), the dot outline around the card, the red close dot,
and the card container that assembles itself, bounces on hover and dissolves when it leaves.

Animation runs only while something changes (tick callback removed when still).
"""
import math
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gtk  # noqa: E402

# ---------- 5×7 dot font ----------
_F = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11110 10001 10001 10001 10001 10001 11110",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 01010 00100 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11110 00001 00001 01110 00001 00001 11110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    ":": "00000 00100 00100 00000 00100 00100 00000", ".": "00000 00000 00000 00000 00000 01100 01100",
    "-": "00000 00000 00000 11111 00000 00000 00000", "_": "00000 00000 00000 00000 00000 00000 11111",
    "/": "00001 00010 00010 00100 01000 01000 10000", "'": "00100 00100 01000 00000 00000 00000 00000",
    "!": "00100 00100 00100 00100 00100 00000 00100", "?": "01110 10001 00001 00010 00100 00000 00100",
    "&": "01100 10010 10100 01000 10101 10010 01101", "+": "00000 00100 00100 11111 00100 00100 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010", ")": "01000 00100 00010 00010 00010 00100 01000",
    " ": "00000 00000 00000 00000 00000 00000 00000",
}
FONTE = {k: [linha for linha in v.split()] for k, v in _F.items()}
ACENTOS = str.maketrans("ÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇÑ", "AAAAAEEEEIIIIOOOOOUUUUCN")


def normalizar(texto):
    """Uppercase, accents removed, unknown characters become a space-like gap."""
    t = texto.upper().translate(ACENTOS)
    return "".join(c if c in FONTE else " " for c in t)


def largura_texto(texto, passo):
    t = normalizar(texto)
    return max(0, (len(t) * 6 - 1)) * passo


def desenhar_texto(cr, texto, x, y, passo, cor, alfa=1.0, apagado=0.0):
    """Dot text. passo = distance between dots; apagado = alpha of the unlit dots (0 = invisible)."""
    t = normalizar(texto)
    r = passo * 0.36
    for i, ch in enumerate(t):
        g = FONTE[ch]
        for c in range(5):
            for linha in range(7):
                aceso = g[linha][c] == "1"
                a = alfa if aceso else apagado * alfa
                if a <= 0.01:
                    continue
                cr.set_source_rgba(*cor, a)
                cr.arc(x + (i * 6 + c) * passo + passo / 2, y + linha * passo + passo / 2, r, 0, 2 * math.pi)
                cr.fill()


class TextoDots(Gtk.DrawingArea):
    """A label written in dots."""

    def __init__(self, texto, cor, passo=1.6, alfa=0.55):
        super().__init__()
        self.texto, self.cor, self.passo, self.alfa = texto, cor, passo, alfa
        self.set_content_width(math.ceil(largura_texto(texto, passo)) + 1)
        self.set_content_height(math.ceil(7 * passo) + 1)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(lambda _a, cr, _w, _h: desenhar_texto(cr, self.texto, 0, 0, self.passo, self.cor,
                                                                  self.alfa))


# ---------- the red close dot ----------

class PontoFechar(Gtk.DrawingArea):
    """Red dot that closes the card. Grows a little and shows a ring on hover."""

    def __init__(self, cor, ao_clicar):
        super().__init__(content_width=18, content_height=18, valign=Gtk.Align.CENTER)
        self.cor, self.sobre = cor, False
        self.set_tooltip_text("Dismiss")
        self.set_cursor_from_name("pointer")
        self.set_draw_func(self._desenhar)
        mov = Gtk.EventControllerMotion()
        mov.connect("enter", lambda *_: self._sobre(True))
        mov.connect("leave", lambda *_: self._sobre(False))
        self.add_controller(mov)
        clique = Gtk.GestureClick()
        clique.connect("released", lambda g, *_: (g.set_state(Gtk.EventSequenceState.CLAIMED), ao_clicar()))
        self.add_controller(clique)

    def _sobre(self, v):
        self.sobre = v
        self.queue_draw()

    def _desenhar(self, _a, cr, w, h):
        cx, cy = w / 2, h / 2
        cr.set_source_rgba(*self.cor, 1.0)
        cr.arc(cx, cy, 5.2 if self.sobre else 4.0, 0, 2 * math.pi)
        cr.fill()
        if self.sobre:                                    # a ring of tiny dots around it
            for k in range(10):
                a = k / 10 * 2 * math.pi
                cr.set_source_rgba(*self.cor, 0.55)
                cr.arc(cx + math.cos(a) * 8, cy + math.sin(a) * 8, 0.9, 0, 2 * math.pi)
                cr.fill()


# ---------- geometry ----------

def perimetro(x, y, w, h, r, passo):
    """Points along a rounded rectangle, clockwise from the top-left corner's end, ~passo apart."""
    r = max(0.0, min(r, w / 2, h / 2))
    trechos = []                    # (length, function t->point)
    lados = [((x + r, y), (x + w - r, y)), ((x + w, y + r), (x + w, y + h - r)),
             ((x + w - r, y + h), (x + r, y + h)), ((x, y + h - r), (x, y + r))]
    cantos = [(x + w - r, y + r, -math.pi / 2), (x + w - r, y + h - r, 0.0), (x + r, y + h - r, math.pi / 2),
              (x + r, y + r, math.pi)]
    for (a, b), (cx, cy, ang0) in zip(lados, cantos):
        comp = math.hypot(b[0] - a[0], b[1] - a[1])
        trechos.append((comp, lambda t, a=a, b=b: (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)))
        trechos.append((r * math.pi / 2, lambda t, cx=cx, cy=cy, ang0=ang0: (
            cx + r * math.cos(ang0 + t * math.pi / 2), cy + r * math.sin(ang0 + t * math.pi / 2))))
    total = sum(c for c, _ in trechos)
    n = max(8, int(total / passo))
    pontos = []
    for k in range(n):
        d = k / n * total
        for comp, f in trechos:
            if d <= comp or comp == 0:
                pontos.append(f(d / comp if comp else 0))
                break
            d -= comp
    return pontos


def suave(a, b, x):
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


# ---------- the card ----------

class CartaoDots(Gtk.Box):
    """Card container in DotStyle.
    - entrance: the outline's dots light up around the card (a running head), then the content fades in;
    - hover: a springy micro zoom (≈3%) and a shimmer running along the outline;
    - exit: content fades, the outline's dots go out, then ao_sumir() is called."""

    ENTRADA, SAIDA = 0.42, 0.34          # seconds
    MARGEM = 7                           # room around the card for the zoom (px)

    def __init__(self, cores, raio, critica=False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.fundo, self.texto, self.acento = cores["background"], cores["text"], cores["accent"]
        self.raio, self.critica = raio, critica
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(self.MARGEM)
        self.t_entrada = time.monotonic()
        self.t_saida = None
        self.ao_sumir = None
        self.escala, self.vel, self.alvo = 1.0, 0.0, 1.0
        self.brilho_t0 = None
        self.tique = None
        self._ultimo = time.monotonic()
        mov = Gtk.EventControllerMotion()
        mov.connect("enter", lambda *_: self.sobre(True))
        mov.connect("leave", lambda *_: self.sobre(False))
        self.add_controller(mov)
        self.animar()

    # --- state ---

    def sobre(self, dentro):
        self.alvo = 1.03 if dentro else 1.0
        if dentro:
            self.vel += 0.6                                   # the little "tap" kick of the bounce
            self.brilho_t0 = time.monotonic()
        self.animar()

    def sair(self, ao_sumir):
        if self.t_saida is None:
            self.t_saida = time.monotonic()
            self.ao_sumir = ao_sumir
            self.animar()

    def animar(self):
        if self.tique is None:
            self._ultimo = time.monotonic()
            self.tique = self.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        agora = time.monotonic()
        dt = min(0.05, agora - self._ultimo)
        self._ultimo = agora
        # damped spring toward the target scale
        k, amort = 260.0, 15.0
        acel = k * (self.alvo - self.escala) - amort * self.vel
        self.vel += acel * dt
        self.escala += self.vel * dt
        self.queue_draw()
        entrando = agora - self.t_entrada < self.ENTRADA + 0.05
        brilhando = self.brilho_t0 is not None and agora - self.brilho_t0 < 0.9
        mola = abs(self.alvo - self.escala) > 0.0005 or abs(self.vel) > 0.002
        if self.t_saida is not None and agora - self.t_saida >= self.SAIDA:
            self.tique = None
            if self.ao_sumir:
                f, self.ao_sumir = self.ao_sumir, None
                GLib.idle_add(lambda: (f(), False)[1])
            return GLib.SOURCE_REMOVE
        if not (entrando or brilhando or mola or self.t_saida is not None):
            self.escala, self.vel = self.alvo, 0.0
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def fases(self):
        """(outline 0..1, content alpha 0..1) for this instant."""
        agora = time.monotonic()
        contorno = suave(0, self.ENTRADA * 0.8, agora - self.t_entrada)
        conteudo = suave(self.ENTRADA * 0.45, self.ENTRADA, agora - self.t_entrada)
        if self.t_saida is not None:
            p = (agora - self.t_saida) / self.SAIDA
            conteudo *= 1 - suave(0.0, 0.5, p)
            contorno *= 1 - suave(0.25, 1.0, p)
        return contorno, conteudo

    # --- drawing ---

    def do_snapshot(self, snap):
        w, h = self.get_width(), self.get_height()
        contorno, conteudo = self.fases()
        snap.save()
        cx, cy = w / 2, h / 2
        snap.translate(Graphene.Point().init(cx, cy))
        snap.scale(self.escala, self.escala)
        snap.translate(Graphene.Point().init(-cx, -cy))
        ret = Graphene.Rect().init(-self.MARGEM, -self.MARGEM, w + 2 * self.MARGEM, h + 2 * self.MARGEM)
        cr = snap.append_cairo(ret)
        self._fundo_e_contorno(cr, w, h, contorno, conteudo)
        snap.push_opacity(conteudo)
        Gtk.Box.do_snapshot(self, snap)
        snap.pop()
        snap.restore()

    def _fundo_e_contorno(self, cr, w, h, contorno, conteudo):
        r = self.raio
        # glass background (appears with the content)
        if conteudo > 0:
            cr.new_sub_path()
            for (ax, ay, a0) in ((w - r, r, -math.pi / 2), (w - r, h - r, 0), (r, h - r, math.pi / 2), (r, r, math.pi)):
                cr.arc(ax, ay, r, a0, a0 + math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(*self.fundo, 0.93 * conteudo)
            cr.fill()
            # faint dot texture inside
            for x in range(10, int(w) - 6, 12):
                for y in range(10, int(h) - 6, 12):
                    cr.set_source_rgba(*self.texto, 0.035 * conteudo)
                    cr.arc(x, y, 0.8, 0, 2 * math.pi)
                    cr.fill()
        if contorno <= 0:
            return
        cor = self.acento if self.critica else self.texto
        pontos = perimetro(0.5, 0.5, w - 1, h - 1, r, 6.0)
        n = len(pontos)
        agora = time.monotonic()
        acesos = contorno * n
        brilho = None
        if self.brilho_t0 is not None and agora - self.brilho_t0 < 0.9:
            brilho = (agora - self.brilho_t0) / 0.9 * n            # shimmer position along the outline
        for i, (x, y) in enumerate(pontos):
            if i > acesos:
                break
            cabeca = max(0.0, 1 - (acesos - i) / 6) if contorno < 1 else 0.0   # bright running head
            a = (0.32 if not self.critica else 0.75) + 0.6 * cabeca
            raio = 1.15 + 0.6 * cabeca
            if brilho is not None:
                d = min(abs(i - brilho), n - abs(i - brilho))
                onda = max(0.0, 1 - d / 5)
                a += 0.55 * onda
                raio += 0.7 * onda
            cr.set_source_rgba(*cor, min(1.0, a))
            cr.arc(x, y, raio, 0, 2 * math.pi)
            cr.fill()
