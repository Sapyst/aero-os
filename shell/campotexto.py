"""
campotexto.py — the DotStyle text field logic, shared by ORBIT's search and the start menu (nuvem.py).

It behaves like a normal text field: typing inserts at the cursor, Ctrl+A selects everything (typing then
replaces it), Ctrl+C / X / V, Ctrl+Backspace deletes a word, Delete, left/right (Ctrl: a word), Home/End.
The selection is shown as a red outline around the letters (desenhar), not a filled block.
"""
import subprocess

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Pango, PangoCairo  # noqa: E402

import cairo  # noqa: E402

RED = (1.0, 0.231, 0.188)
FG = (0.925, 0.92, 0.91)


class CampoTexto:
    def __init__(self, ao_mudar, limite=60):
        self.texto, self.cursor, self.tudo = "", 0, False
        self.ao_mudar = ao_mudar          # called with no arguments when the text changes
        self.limite = limite

    def definir(self, texto, cursor=None):
        self.texto = texto[:self.limite]
        self.cursor = len(self.texto) if cursor is None else max(0, min(len(self.texto), cursor))
        self.tudo = False
        self.ao_mudar()

    def limpar(self):
        self.definir("", 0)

    def tecla(self, tecla, ctrl, display):
        """Handles one key. Returns True if it was a text-field key (the caller stops there)."""
        q, c = self.texto, min(self.cursor, len(self.texto))
        if ctrl and tecla in (Gdk.KEY_a, Gdk.KEY_A):
            self.tudo = bool(q)
            self.ao_mudar_visual()
            return True
        if ctrl and tecla in (Gdk.KEY_c, Gdk.KEY_C, Gdk.KEY_x, Gdk.KEY_X) and self.tudo and q:
            subprocess.Popen(["wl-copy", q], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if tecla in (Gdk.KEY_x, Gdk.KEY_X):
                self.limpar()
            return True
        if ctrl and tecla in (Gdk.KEY_v, Gdk.KEY_V):
            def colado(cb, res):
                try:
                    t = cb.read_text_finish(res)
                except GLib.Error:
                    t = None
                if t and t.strip():
                    t = t.strip().splitlines()[0]
                    if self.tudo:
                        self.definir(t)
                    else:
                        cc = min(self.cursor, len(self.texto))
                        self.definir(self.texto[:cc] + t + self.texto[cc:], cc + len(t))
            display.get_clipboard().read_text_async(None, colado)
            return True
        if tecla == Gdk.KEY_BackSpace and q:
            if self.tudo:
                self.limpar()
            elif ctrl:
                k = c
                while k > 0 and q[k - 1] == " ":
                    k -= 1
                while k > 0 and q[k - 1] != " ":
                    k -= 1
                self.definir(q[:k] + q[c:], k)
            elif c > 0:
                self.definir(q[:c - 1] + q[c:], c - 1)
            return True
        if tecla == Gdk.KEY_Delete and q:
            self.definir("" if self.tudo else q[:c] + q[c + 1:], 0 if self.tudo else c)
            return True
        if q and tecla in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Home, Gdk.KEY_End):
            if tecla == Gdk.KEY_Home:
                c = 0
            elif tecla == Gdk.KEY_End:
                c = len(q)
            elif ctrl:
                passo = -1 if tecla == Gdk.KEY_Left else 1
                c += passo
                while 0 < c < len(q) and q[c - (1 if passo < 0 else 0)] != " ":
                    c += passo
            else:
                c += -1 if tecla == Gdk.KEY_Left else 1
            self.cursor, self.tudo = max(0, min(len(q), c)), False
            self.ao_mudar_visual()
            return True
        u = Gdk.keyval_to_unicode(tecla)
        if u and chr(u).isprintable() and not ctrl:
            if self.tudo:
                self.definir(chr(u), 1)
            else:
                self.definir(q[:c] + chr(u) + q[c:], c + 1)
            return True
        return False

    def ao_mudar_visual(self):
        """Selection / cursor moved without changing the text: the owner only needs to redraw."""
        f = getattr(self, "redesenhar", None)
        if f:
            f()


def desenhar(cr, campo, x, y, fonte, alfa=1.0, maiusculas=True, piscar=1.0):
    """Draws the field's text centred at (x, y): the letters, the red outline when everything is selected
    and the red cursor bar. Returns the text width."""
    texto = campo.texto.upper() if maiusculas else campo.texto
    lay = PangoCairo.create_layout(cr)
    lay.set_font_description(Pango.FontDescription.from_string(fonte))
    lay.set_text(texto, -1)
    tw, th = lay.get_pixel_size()
    x0, y0 = x - tw / 2, y - th / 2
    cr.new_path()
    if campo.tudo and texto:                          # Ctrl+A: a red outline with a soft glow
        cr.move_to(x0, y0)
        PangoCairo.layout_path(cr, lay)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        for largura, a in ((7, 0.12), (4.5, 0.25), (2.2, 0.95)):
            cr.set_source_rgba(*RED, a * alfa)
            cr.set_line_width(largura)
            cr.stroke_preserve()
        cr.new_path()
    cr.move_to(x0, y0)
    cr.set_source_rgba(*FG, alfa)
    PangoCairo.show_layout(cr, lay)
    cr.new_path()
    pre = lay.copy()
    pre.set_text(texto[:campo.cursor], -1)
    pw = pre.get_pixel_size()[0] if campo.cursor else 0
    alt = th * 0.8
    cr.rectangle(x0 + pw + 1, y - alt / 2, 3, alt)
    cr.set_source_rgba(*RED, alfa * piscar)
    cr.fill()
    return tw
