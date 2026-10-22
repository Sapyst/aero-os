"""
nuvem.py — the DotStyle start menu: a cloud of app bubbles (Super key, when the visual is DotStyle).

- Every app is a round bubble with its real, colourful icon. The ones you open most (and the pinned ones)
  are bigger and sit near the centre; rarely used apps are small, at the edge.
- Opening: bubbles pop out from the centre one after another, with a little bounce. Closing: they fall
  back to the centre and vanish.
- Pointer: a magnifying lens; the bubble under it grows, neighbours step aside, its name appears in dots.
- Typing searches right away: bubbles that don't match shrink to tiny dots; the matches grow and gather
  in the middle. Enter opens the first; arrows move between bubbles; Esc closes.
- Transparent full-screen layer: the desktop stays visible behind. It draws only while something moves.

Usage counts: ~/.cache/aero-shell/menu-uso.json ({app id: times opened}).
"""
import json
import math
import os
import random
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import apps  # noqa: E402

USO = os.path.expanduser("~/.cache/aero-shell/menu-uso.json")
FG = (0.925, 0.92, 0.91)
DIM = (0.55, 0.545, 0.535)
RED = (1.0, 0.231, 0.188)
FIXADOS_PADRAO = ["com.anthropic.Claude.desktop", "firefox.desktop", "com.aero.Settings.desktop",
                  "kitty.desktop", "thunar.desktop", "steam.desktop"]
RAIOS = (54, 40, 30, 23)           # big (top 6), medium (next 14), small (next 30), tiny (the rest)
ICONE_PX = 112                     # icon resolution kept in memory per app (enough for the zoomed bubble)


def suave(a, b, x):
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


def pop(t):
    """0 -> overshoot -> 1 in about 0.32 s (t in seconds)."""
    if t <= 0:
        return 0.0
    k = min(1.0, t / 0.32)
    return 1 - math.cos(k * math.pi * 1.5) * math.exp(-4.5 * k) * (1 - k * 0.0)


def ler_uso():
    try:
        with open(USO, encoding="utf-8") as f:
            d = json.load(f)
        return {k: int(v) for k, v in d.items() if isinstance(k, str) and isinstance(v, int)} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def contar_uso(app_id):
    d = ler_uso()
    d[app_id] = d.get(app_id, 0) + 1
    os.makedirs(os.path.dirname(USO), exist_ok=True)
    with open(USO + ".tmp", "w", encoding="utf-8") as f:
        json.dump(d, f)
    os.replace(USO + ".tmp", USO)


def empacotar(raios, folga=7.0):
    """Circle packing around (0, 0): biggest first, each new circle walks a sunflower spiral and takes the
    first spot that touches nobody (neighbours looked up in a coarse grid, so it stays fast).
    Returns [(x, y)] in the same order as raios."""
    pos, grade = [], {}
    tam = 2 * max(raios) + folga if raios else 1
    ouro = math.pi * (3 - math.sqrt(5))
    k_ini = 0

    def livre(x, y, r):
        gx, gy = int(x // tam), int(y // tam)
        for ix in (gx - 1, gx, gx + 1):
            for iy in (gy - 1, gy, gy + 1):
                for j in grade.get((ix, iy), ()):
                    px, py = pos[j]
                    if math.hypot(x - px, y - py) < r + raios[j] + folga:
                        return False
        return True

    for i, r in enumerate(raios):
        k = max(0, k_ini - 300)
        while True:
            d = 7.0 * math.sqrt(k)
            a = k * ouro
            x, y = d * math.cos(a) * 1.25, d * math.sin(a) * 0.82        # a wide cloud (screens are wide)
            if livre(x, y, r) or k > 200000:
                break
            k += 1
        k_ini = max(k_ini, k)
        pos.append((x, y))
        grade.setdefault((int(x // tam), int(y // tam)), []).append(i)
    return pos


class Bolha:
    __slots__ = ("app", "id", "nome", "r", "x", "y", "atraso", "sup", "alvo_x", "alvo_y", "cx", "cy",
                 "escala", "visivel")

    def __init__(self, app):
        self.app = app
        self.id = app.get_id() or ""
        self.nome = app.get_display_name() or self.id
        self.sup = None
        self.escala, self.visivel = 1.0, 1.0


class Nuvem(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, decorated=False)
        self.add_css_class("nuvem")
        prov = Gtk.CssProvider()
        prov.load_from_string(".nuvem { background: none; background-color: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 3)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "supermenu")
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self.desenhar)
        self.set_child(self.area)
        self.bolhas, self.cache = [], {}
        self.busca = ""
        self.mouse = None
        self.sel = 0
        self.t_abrir = 0.0
        self.t_fechar = None
        self.tique = None
        self.ultimo_mov = 0.0
        mov = Gtk.EventControllerMotion()
        mov.connect("motion", self._mover)
        mov.connect("leave", lambda *_: self._mover(None, None, None))
        self.area.add_controller(mov)
        clique = Gtk.GestureClick(button=0)
        clique.connect("released", self._clique)
        self.area.add_controller(clique)
        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self._tecla)
        self.add_controller(teclas)

    # ---------- data ----------

    def carregar(self):
        uso = ler_uso()
        fixados = apps.ler_lista("menu-fixados", FIXADOS_PADRAO)
        recentes = apps.ler_lista("menu-recentes", [])

        def peso(a):
            i = a.get_id() or ""
            return (uso.get(i, 0) * 3 + (40 if i in fixados else 0) +
                    (12 - recentes.index(i) if i in recentes else 0))
        lista = sorted(apps.todos(), key=lambda a: (-peso(a), a.get_display_name().casefold()))
        self.bolhas = [Bolha(a) for a in lista]
        for i, b in enumerate(self.bolhas):
            b.r = RAIOS[0] if i < 6 else RAIOS[1] if i < 20 else RAIOS[2] if i < 50 else RAIOS[3]
        for b, (x, y) in zip(self.bolhas, empacotar([b.r for b in self.bolhas])):
            b.x, b.y = x, y
            b.cx, b.cy = x, y
            b.atraso = min(0.42, math.hypot(x, y) / 1400) + random.uniform(0, 0.04)
        self.reorganizar()

    def icone(self, b):
        """The app's real icon as a cairo surface (kept for the next openings)."""
        if b.id in self.cache:
            return self.cache[b.id]
        sup = None
        try:
            gicon = b.app.get_icon()
            if gicon is not None:
                tema = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
                p = tema.lookup_by_gicon(gicon, ICONE_PX, 1, Gtk.TextDirection.NONE, 0)
                if p is not None and (p.get_icon_name() or "").startswith("image-missing"):
                    p = None                                  # no real icon: the bubble shows the initial
                f = p.get_file() if p is not None else None
                if f is not None and f.get_path():
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(f.get_path(), ICONE_PX, ICONE_PX, True)
                    import cairo
                    sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, ICONE_PX, ICONE_PX)
                    ctx = cairo.Context(sup)                  # centred (icons are not always square)
                    Gdk.cairo_set_source_pixbuf(ctx, pb, (ICONE_PX - pb.get_width()) / 2,
                                                (ICONE_PX - pb.get_height()) / 2)
                    ctx.paint()
        except (GLib.Error, TypeError, ValueError):
            sup = None
        self.cache[b.id] = sup
        return sup

    # ---------- search ----------

    def reorganizar(self):
        """Search changed: matching bubbles gather in the middle (re-packed), the others shrink to dots."""
        texto = self.busca.strip()
        if not texto:
            for b in self.bolhas:
                b.alvo_x, b.alvo_y, b.visivel = b.x, b.y, 1.0
            self.sel = 0
            self.animar()
            return
        pontos = [(apps.pontuar(b.app, texto), i) for i, b in enumerate(self.bolhas)]
        achados = [i for p, i in sorted(pontos, key=lambda t: -t[0]) if p > 0]
        raios = [RAIOS[0] if k == 0 else RAIOS[1] for k in range(len(achados))]
        lugares = empacotar(raios) if achados else []
        conjunto = set(achados)
        for k, i in enumerate(achados):
            b = self.bolhas[i]
            b.alvo_x, b.alvo_y, b.visivel = lugares[k][0], lugares[k][1], 1.0
        for i, b in enumerate(self.bolhas):
            if i not in conjunto:
                b.alvo_x, b.alvo_y, b.visivel = b.x * 1.35, b.y * 1.35, 0.0     # step back as tiny dots
        self.ordem_busca = achados
        self.sel = achados[0] if achados else -1
        self.animar()

    # ---------- open / close ----------

    def abrir(self):
        self.busca = ""
        self.carregar()
        self.t_abrir, self.t_fechar = time.monotonic(), None
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)
        self.present()
        self.animar()

    def fechar(self, depois=None):
        if not self.get_visible() or self.t_fechar is not None:
            return
        self.t_fechar = time.monotonic()
        self._depois = depois
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.animar()

    def alternar(self):
        if self.get_visible() and self.t_fechar is None:
            self.fechar()
        else:
            self.abrir()

    def abrir_app(self, b):
        def lancar():
            apps.abrir(b.app)
            recentes = [b.id] + [i for i in apps.ler_lista("menu-recentes", []) if i != b.id]
            apps.gravar_lista("menu-recentes", recentes[:12])
            contar_uso(b.id)
        self.fechar(lancar)

    # ---------- animation ----------

    def animar(self):
        if self.tique is None:
            self.tique = self.area.add_tick_callback(self._quadro)

    def _quadro(self, *_):
        agora = time.monotonic()
        mexendo = False
        for b in self.bolhas:
            dx, dy = b.alvo_x - b.cx, b.alvo_y - b.cy
            if abs(dx) > 0.3 or abs(dy) > 0.3:
                b.cx += dx * 0.18
                b.cy += dy * 0.18
                mexendo = True
            alvo_v = b.visivel
            if abs(b.escala - (0.16 if alvo_v == 0 else 1.0)) > 0.005:
                b.escala += ((0.16 if alvo_v == 0 else 1.0) - b.escala) * 0.2
                mexendo = True
        abrindo = agora - self.t_abrir < 0.9
        lente = self.mouse is not None and agora - self.ultimo_mov < 0.6
        self.area.queue_draw()
        if self.t_fechar is not None and agora - self.t_fechar > 0.32:
            self.tique = None
            self.set_visible(False)
            f, self._depois = getattr(self, "_depois", None), None
            if f:
                GLib.idle_add(lambda: (f(), False)[1])
            return GLib.SOURCE_REMOVE
        if not (mexendo or abrindo or lente or self.t_fechar is not None):
            self.tique = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # ---------- geometry ----------

    def _centro(self):
        return self.area.get_width() / 2, self.area.get_height() / 2 + 10

    def _onde(self, b, agora):
        """Screen position, radius and alpha of bubble b right now (opening/closing/lens included)."""
        cx, cy = self._centro()
        x, y, r = cx + b.cx, cy + b.cy, b.r * b.escala
        if self.t_fechar is None:
            k = pop(agora - self.t_abrir - b.atraso)
            x, y = cx + (x - cx) * min(1.0, 0.6 + 0.4 * k), cy + (y - cy) * min(1.0, 0.6 + 0.4 * k)
            r *= k
            a = min(1.0, max(0.0, (agora - self.t_abrir - b.atraso) / 0.12))
        else:
            p = suave(0, 0.3, agora - self.t_fechar)
            x, y = x + (cx - x) * p * 0.8, y + (cy - y) * p * 0.8
            r *= 1 - p * 0.85
            a = 1 - p
        if self.mouse is not None and b.escala > 0.5:
            mx, my = self.mouse
            d = math.hypot(x - mx, y - my)
            lente = math.exp(-(d / 150) ** 2)
            r *= 1 + 0.55 * lente
            if d > 1:                                         # neighbours step aside
                empurra = 26 * lente * (1 - lente) * 2
                x += (x - mx) / d * empurra
                y += (y - my) / d * empurra
        return x, y, r, a

    def bolha_em(self, px, py):
        agora = time.monotonic()
        melhor = None
        for i, b in enumerate(self.bolhas):
            x, y, r, a = self._onde(b, agora)
            if a > 0.3 and b.escala > 0.5 and math.hypot(px - x, py - y) <= r:
                melhor = i
        return melhor

    # ---------- drawing ----------

    def desenhar(self, _area, cr, w, h):
        agora = time.monotonic()
        # a whisper of a dot grid so the cloud has a floor (never hides the desktop)
        if self.t_fechar is None:
            fundo = min(1.0, (agora - self.t_abrir) / 0.25)
        else:
            fundo = 1 - suave(0, 0.3, agora - self.t_fechar)
        cr.set_source_rgba(0.02, 0.02, 0.025, 0.28 * fundo)
        cr.paint()
        sobre = self.bolha_em(*self.mouse) if self.mouse else None
        foco = sobre if sobre is not None else (self.sel if (self.busca or getattr(self, "navegou", False)) else None)
        # tiny dots first, then the bubbles, and the focused one last (on top of its neighbours)
        ordem = sorted(range(len(self.bolhas)), key=lambda i: (i == foco, self.bolhas[i].escala))
        for i in ordem:
            b = self.bolhas[i]
            x, y, r, a = self._onde(b, agora)
            if r < 0.6 or a <= 0.01:
                continue
            if b.escala < 0.5:                                # filtered out: a tiny dot
                cr.set_source_rgba(*FG, 0.25 * a)
                cr.arc(x, y, max(2.0, r * 0.5), 0, 2 * math.pi)
                cr.fill()
                continue
            selecionada = i == self.sel and (bool(self.busca) or getattr(self, "navegou", False))
            if i == sobre or selecionada:                     # a soft halo around the focused bubble
                import cairo
                g = cairo.RadialGradient(x, y, r * 0.8, x, y, r * 1.6)
                for k in range(9):
                    t = k / 8
                    g.add_color_stop_rgba(t, *FG, 0.22 * a * math.exp(-4 * t * t) * (1 - t ** 6))
                cr.set_source(g)
                cr.arc(x, y, r * 1.6, 0, 2 * math.pi)
                cr.fill()
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(0.08, 0.08, 0.09, 0.82 * a)
            cr.fill_preserve()
            cr.set_source_rgba(*FG, 0.16 * a)
            cr.set_line_width(1)
            cr.stroke()
            sup = self.icone(b)
            if sup is not None:
                lado = r * 1.25
                cr.save()
                cr.translate(x - lado / 2, y - lado / 2)
                cr.scale(lado / ICONE_PX, lado / ICONE_PX)
                cr.set_source_surface(sup, 0, 0)
                cr.paint_with_alpha(a)
                cr.restore()
            else:
                self._texto(cr, b.nome[:1].upper(), x, y, f"Doto Black {int(r * 0.7)}", a)
            if i == foco:
                self._etiqueta(cr, b.nome.upper(), x, y + r + 18, a)
        # the search line, written in dots at the top
        if self.busca or (self.t_fechar is None and agora - self.t_abrir < 2.5):
            texto = self.busca.upper() + "_" if self.busca else "TYPE TO SEARCH"
            self._texto(cr, texto, w / 2, 80, "Doto Black 26", fundo * (1.0 if self.busca else 0.5))

    def _etiqueta(self, cr, s, x, y, alfa):
        """The focused app's name in dots on a small dark pill, always readable."""
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Doto Black 14"))
        layout.set_text(s, -1)
        tw, th = layout.get_pixel_size()
        w, h = tw + 22, th + 8
        x0, y0, r = x - w / 2, y - h / 2, h / 2
        cr.new_sub_path()
        cr.arc(x0 + w - r, y0 + r, r, -math.pi / 2, math.pi / 2)
        cr.arc(x0 + r, y0 + r, r, math.pi / 2, 3 * math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0.04, 0.04, 0.045, 0.88 * alfa)
        cr.fill()
        cr.move_to(x - tw / 2, y - th / 2)
        cr.set_source_rgba(*FG, alfa)
        PangoCairo.show_layout(cr, layout)

    def _texto(self, cr, s, x, y, fonte, alfa):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string(fonte))
        layout.set_text(s, -1)
        tw, th = layout.get_pixel_size()
        cr.move_to(x - tw / 2 + 1, y - th / 2 + 1)
        cr.set_source_rgba(0, 0, 0, 0.45 * alfa)
        PangoCairo.show_layout(cr, layout)
        cr.move_to(x - tw / 2, y - th / 2)
        cr.set_source_rgba(*FG, alfa)
        PangoCairo.show_layout(cr, layout)

    # ---------- input ----------

    def _mover(self, _c, x, y):
        self.mouse = None if x is None else (x, y)
        self.ultimo_mov = time.monotonic()
        self.navegou = False
        self.animar()

    def _clique(self, g, _n, x, y):
        i = self.bolha_em(x, y)
        if i is None:
            self.fechar()                                     # click on the empty space closes
        elif g.get_current_button() == 1:
            self.abrir_app(self.bolhas[i])
        elif g.get_current_button() == 3:
            self.alternar_fixado(self.bolhas[i])

    def alternar_fixado(self, b):
        fixados = apps.ler_lista("menu-fixados", FIXADOS_PADRAO)
        fixados = [f for f in fixados if f != b.id] if b.id in fixados else fixados + [b.id]
        apps.gravar_lista("menu-fixados", fixados)
        texto = self.busca
        self.carregar()
        self.busca = texto
        self.reorganizar()

    def _tecla(self, _c, tecla, _cod, _mods):
        if tecla == Gdk.KEY_Escape:
            if self.busca:
                self.busca = ""
                self.reorganizar()
            else:
                self.fechar()
            return True
        if tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if 0 <= self.sel < len(self.bolhas):
                self.abrir_app(self.bolhas[self.sel])
            return True
        if tecla == Gdk.KEY_BackSpace:
            self.busca = self.busca[:-1]
            self.reorganizar()
            return True
        direcoes = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if tecla in direcoes:
            self.mover_selecao(*direcoes[tecla])
            return True
        u = Gdk.keyval_to_unicode(tecla)
        if u and chr(u).isprintable() and len(self.busca) < 40:
            self.busca += chr(u)
            self.reorganizar()
            return True
        return False

    def mover_selecao(self, dx, dy):
        """Arrow keys: jump to the nearest visible bubble in that direction."""
        if not self.bolhas:
            return
        atual = self.bolhas[self.sel] if 0 <= self.sel < len(self.bolhas) else self.bolhas[0]
        melhor, nota = None, None
        for i, b in enumerate(self.bolhas):
            if b is atual or b.visivel == 0:
                continue
            vx, vy = b.alvo_x - atual.alvo_x, b.alvo_y - atual.alvo_y
            frente = vx * dx + vy * dy
            if frente <= 1:
                continue
            lado = abs(vx * dy - vy * dx)
            n = frente + lado * 2.2
            if nota is None or n < nota:
                melhor, nota = i, n
        if melhor is not None:
            self.sel = melhor
            self.navegou = True
            self.mouse = None
            self.area.queue_draw()
