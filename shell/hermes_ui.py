"""
hermes_ui.py — what Hermes puts on the screen: the cards and the dot wave.

- The WAVE lives on the highest layer (layer-shell OVERLAY, above fullscreen apps too). It ignores the
  mouse and the keyboard completely (empty input region, no keyboard), so it never steals a click or
  focus from a game. It exists only while it animates: the window is hidden the rest of the time.
  Over a fullscreen app it is faded (wave.fullscreen = faded): just an "oh, something arrived".
- The CARDS live on the TOP layer next to where the quick center opens ("follow-central"), or wherever
  the user put them. While a fullscreen app has focus they are held (fullscreen = hold) and show up
  when you leave fullscreen.

Everything visual comes from the settings (hermes.PADRAO): style, colours, fonts, sizes, timings.
"""
import math
import os
import re
import subprocess
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import hermes_dots as hd  # noqa: E402

PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")


def rgb(hexa):
    hexa = hexa.lstrip("#")
    return tuple(int(hexa[i:i + 2], 16) / 255 for i in (0, 2, 4))


def lado_da_central():
    """Where the quick center opens (painel.conf CENTRAL_POSICAO), default top-right."""
    try:
        with open(PAINEL, encoding="utf-8") as f:
            for linha in f:
                if linha.startswith("CENTRAL_POSICAO="):
                    v = linha.split("=", 1)[1].strip()
                    if v in ("top-left", "top-center", "top-right", "bottom-left", "bottom-center", "bottom-right"):
                        return v
    except OSError:
        pass
    return "top-right"


SINO = os.path.expanduser("~/.cache/aero-shell/sino.json")


def lugar_do_sino():
    """Where the bar's bell is, learned the last time it was clicked (som.py writes it):
    {"x", "y"} in the monitor's own coordinates plus the monitor size; None if never clicked."""
    try:
        import json
        with open(SINO, encoding="utf-8") as f:
            d = json.load(f)
        if all(isinstance(d.get(k), (int, float)) for k in ("x", "y", "w", "h")) and d["w"] > 0:
            return d
    except (OSError, ValueError):
        pass
    return None


def posicao(config):
    p = config["position"]
    if p == "follow-central":
        return lado_da_central()
    if p == "follow-bell":
        sino = lugar_do_sino()
        return "bottom-right" if sino and sino["y"] > sino["h"] / 2 else "top-right"   # bar at the bottom?
    return p


def tela_cheia():
    """True if the focused window is fullscreen (a game, a video...). Asked to Hyprland in ~5 ms."""
    try:
        r = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1)
        import json
        return int(json.loads(r.stdout or "{}").get("fullscreen", 0) or 0) > 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def movimento_reduzido(config):
    modo = config["wave"]["reduced_motion"]
    if modo != "auto":
        return modo == "on"
    try:
        return not Gtk.Settings.get_default().get_property("gtk-enable-animations")
    except Exception:  # noqa: BLE001
        return False


# ---------- the wave (pure drawing: testable on an image surface) ----------

def suave(a, b, x):
    """smoothstep: 0 before a, 1 after b, an S-curve in between."""
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


def desenhar_onda(cr, w, h, p, origem, cfg, cor, forca=1.0):
    """One frame of the wave. p = progress 0..1, origem = (x, y) in px, cfg = settings["wave"],
    forca = 1 normally, fullscreen_intensity/intensity over a fullscreen app."""
    if not 0 <= p < 1:
        return 0
    sp = cfg["dot_spacing"]
    alcance = math.hypot(w, h) * cfg["reach"]
    largura = cfg["width"]
    raio_base = sp * cfg["dot_size"]
    # envelope: a short fade-in, then a long, soft fade-out that reaches exactly 0 at the end
    # (smoothstep curves: no corner anywhere, so nothing "cuts" when the wave ends)
    brilho = cfg["intensity"] * forca * suave(0.0, 0.08, p) * (1 - suave(0.35, 1.0, p))
    ox, oy = origem
    r, g, b = cor
    desenhados = 0
    # only the dots near the ring: a band of the grid around the current radius
    raio = p * alcance
    faixa = largura * alcance
    x0 = max(0, int((ox - raio - faixa) // sp))
    x1 = min(int(w // sp) + 1, int((ox + raio + faixa) // sp) + 1)
    y0 = max(0, int((oy - raio - faixa) // sp))
    y1 = min(int(h // sp) + 1, int((oy + raio + faixa) // sp) + 1)
    for i in range(x0, x1):
        x = sp / 2 + i * sp
        for j in range(y0, y1):
            y = sp / 2 + j * sp
            d = math.hypot(x - ox, y - oy) / alcance
            banda = 1 - min(1.0, abs(d - p) / largura)
            if banda <= 0:
                continue
            banda = banda * banda * (3 - 2 * banda)          # soft edges on both sides of the ring
            cr.set_source_rgba(r, g, b, brilho * banda)
            cr.arc(x, y, raio_base * (0.4 + 0.6 * banda), 0, 2 * math.pi)
            cr.fill()
            desenhados += 1
    return desenhados


def origem_da_onda(config, w, h, cartao=None):
    """Where the wave starts: the card's centre, the card's corner of the screen, or the screen centre."""
    modo = config["wave"]["origin"]
    if modo == "screen-center":
        return w / 2, h / 2
    if config["position"] == "follow-bell":
        sino = lugar_do_sino()
        if sino:                                              # the wave leaves the bell itself
            return sino["x"] * w / sino["w"], sino["y"] * h / sino["h"]
        return w - 150, 18                                    # until the first click: near the bar's right end
    pos = posicao(config)
    m = config["margin"]
    x = {"left": m + 24, "center": w / 2, "right": w - m - 24}[pos.split("-")[1]]
    y = m + 24 if pos.startswith("top") else h - m - 24
    if modo == "notification" and cartao is not None:
        return cartao
    return x, y


class Onda(Gtk.Window):
    """Full-screen, click-through, overlay layer. Hidden whenever no wave is running."""

    def __init__(self, config):
        super().__init__(decorated=False)
        self.config = config
        self.add_css_class("hermes-onda")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "hermes-wave")
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_exclusive_zone(self, -1)                  # cover the bar too, ignore reserved space
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.area = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.area.set_draw_func(self._desenhar)
        self.set_child(self.area)
        self.connect("realize", self._sem_entrada)
        self.ondas = []                                       # [(t0, origem, cor, forca)]
        self.tique = None

    def _sem_entrada(self, *_):
        import cairo
        superficie = self.get_surface()
        if superficie is not None:
            superficie.set_input_region(cairo.Region())       # every click goes through
        return False

    def soltar(self, urgente=False, cartao=None, sobre_tela_cheia=False):
        cfg = self.config["wave"]
        if not cfg["enabled"] or (sobre_tela_cheia and cfg["fullscreen"] == "off"):
            return False
        cor = cfg["urgent_color"] if urgente else (cfg["color"] if cfg["color"] != "auto"
                                                    else self.config["colors"]["text"])
        forca = 1.0
        if sobre_tela_cheia and cfg["fullscreen"] == "faded":
            forca = cfg["fullscreen_intensity"] / max(cfg["intensity"], 0.01)
        self.ondas.append((time.monotonic(), cartao, rgb(cor), forca, movimento_reduzido(self.config)))
        if not self.get_visible():
            self.set_visible(True)
        if self.tique is None:
            self.tique = self.area.add_tick_callback(self._quadro)
        return True

    def _quadro(self, *_):
        agora = time.monotonic()
        dur = self.config["wave"]["duration_ms"] / 1000
        self.ondas = [o for o in self.ondas if agora - o[0] < (dur if not o[4] else 0.8)]
        self.area.queue_draw()
        if not self.ondas:
            self.tique = None
            self.set_visible(False)                           # gone: nothing on screen, nothing to draw
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _desenhar(self, _area, cr, w, h):
        agora = time.monotonic()
        dur = self.config["wave"]["duration_ms"] / 1000
        for t0, cartao, cor, forca, calmo in self.ondas:
            origem = origem_da_onda(self.config, w, h, cartao)
            if calmo:                                        # reduced motion: one soft dot, no ring
                k = 1 - suave(0.0, 1.0, (agora - t0) / 0.8)
                cr.set_source_rgba(*cor, max(0.0, k) * self.config["wave"]["intensity"] * forca)
                cr.arc(origem[0], origem[1], self.config["wave"]["dot_spacing"] * 0.4, 0, 2 * math.pi)
                cr.fill()
                continue
            desenhar_onda(cr, w, h, (agora - t0) / dur, origem, self.config["wave"], cor, forca)


# ---------- cards ----------

CSS = """
window.hermes-onda, .hermes-onda { background: none; background-color: transparent; box-shadow: none; }
window.hermes-cartoes, .hermes-cartoes { background: none; background-color: transparent; box-shadow: none; }
.hermes-titulo, .hermes-corpo { color: @hermes_fg; }
.hermes-app { font-family: "@hermes_mono"; font-size: 10.5px; letter-spacing: 0.08em; color: @hermes_dim; }
.hermes-titulo { font-family: "@hermes_body"; font-weight: 600; font-size: 13.5px; }
.hermes-corpo { font-family: "@hermes_body"; font-size: 12.5px; color: alpha(@hermes_fg, 0.82); }
.hermes-acao { font-family: "@hermes_mono"; font-size: 12px; border-radius: 999px; padding: 3px 12px;
  background: transparent; color: @hermes_fg; border: 1px solid alpha(@hermes_fg, 0.18); }
.hermes-acao:hover { border-color: alpha(@hermes_fg, 0.45); }
.hermes-fechar { min-width: 22px; min-height: 22px; padding: 0; border-radius: 99px; background: transparent;
  color: @hermes_dim; }
"""


def css_do_tema(config, claro=False):
    c, f = config["colors_light" if claro else "colors"], config["font"]
    trocas = {"@hermes_bg": c["background"], "@hermes_fg": c["text"], "@hermes_dim": c["dim"],
              "@hermes_accent": c["accent"], "@hermes_radius": f"{config['radius']}px",
              "@hermes_mono": f["mono"], "@hermes_body": f["body"]}
    texto = CSS
    for k in sorted(trocas, key=len, reverse=True):          # longer names first (@hermes_bg vs @hermes_body)
        texto = texto.replace(k, trocas[k])
    return texto


def pontos_progresso(valor, cor, n=24):
    """A dot progress bar (value hint 0..100)."""
    area = Gtk.DrawingArea(content_height=8, hexpand=True)

    def desenhar(_a, cr, w, h):
        passo = w / n
        for i in range(n):
            aceso = (i + 1) / n * 100 <= valor + 0.01
            cr.set_source_rgba(*cor, 0.95 if aceso else 0.15)
            cr.arc(passo * (i + 0.5), h / 2, min(passo * 0.32, h / 2), 0, 2 * math.pi)
            cr.fill()
    area.set_draw_func(desenhar)
    return area


def imagem_da_notificacao(n, tamanho=36):
    """image-data pixels, or an icon path, or an icon name. None if there is nothing to show."""
    try:
        if n.imagem:
            im = n.imagem
            pb = GdkPixbuf.Pixbuf.new_from_bytes(GLib.Bytes.new(im["data"]), GdkPixbuf.Colorspace.RGB,
                                                 im["alpha"], 8, im["width"], im["height"], im["rowstride"])
            pb = pb.scale_simple(tamanho, tamanho, GdkPixbuf.InterpType.BILINEAR)
            return Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pb))
        icone = n.icone or n.entrada
        if icone.startswith("file://"):
            icone = icone[7:]
        if icone.startswith("/") and os.path.isfile(icone) and os.path.getsize(icone) < 8 * 1024 * 1024:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(icone, tamanho, tamanho, True)
            return Gtk.Picture.new_for_paintable(Gdk.Texture.new_for_pixbuf(pb))
        if icone and "/" not in icone:
            return Gtk.Image(icon_name=icone, pixel_size=tamanho)
    except (GLib.Error, ValueError, TypeError):
        pass
    return None


def montar_cartao(n, config, ao_clicar, ao_acao, ao_fechar, claro=False):
    """One DotStyle card (hermes_dots.CartaoDots): dot outline that assembles, app name and time in dot
    letters, a red dot to dismiss, hover bounce. Text widgets never use markup except the body, which
    hermes.limpar_markup made safe."""
    cores_hex = config["colors_light" if claro else "colors"]
    cores = {k: rgb(v) for k, v in cores_hex.items()}
    pos = posicao(config)
    lado = "right" if pos.endswith("right") else "left" if pos.endswith("left") else "center"
    caixa = hd.CartaoDots(cores, config["radius"], critica=n.urgencia == 2, semente=n.id, origem=lado)
    caixa.set_size_request(config["width"], -1)
    dentro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7, margin_top=12, margin_bottom=12,
                     margin_start=14, margin_end=12)
    caixa.append(dentro)
    topo = Gtk.Box(spacing=8)
    nome = n.app if len(n.app) <= 28 else n.app[:27] + "."
    topo.append(hd.TextoDots(nome, cores["dim"], passo=1.55, alfa=0.95))
    topo.append(Gtk.Box(hexpand=True))
    topo.append(hd.TextoDots(time.strftime("%H:%M", time.localtime(n.criada)), cores["dim"], passo=1.55, alfa=0.7))
    topo.append(hd.PontoFechar(cores["accent"], lambda: ao_fechar(n.id)))
    dentro.append(topo)
    meio = Gtk.Box(spacing=10)
    if config["icons"]:
        img = imagem_da_notificacao(n)
        if img is not None:
            img.set_valign(Gtk.Align.START)
            meio.append(img)
    textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
    # a wrapping label asks for its whole text as one line unless told otherwise: cap it by the card width
    colunas = max(12, int((config["width"] - 30 - (46 if config["icons"] else 0)) / 8.6))   # ~px per char at 13 px
    if n.titulo:
        t = Gtk.Label(label=n.titulo, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=2,
                      ellipsize=Pango.EllipsizeMode.END, use_markup=False, max_width_chars=colunas,
                      natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        t.add_css_class("hermes-titulo")
        textos.append(t)
    if n.corpo:
        c = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, lines=4,
                      ellipsize=Pango.EllipsizeMode.END, max_width_chars=colunas,
                      natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
        c.set_markup(n.corpo)
        c.add_css_class("hermes-corpo")
        textos.append(c)
    meio.append(textos)
    dentro.append(meio)
    if n.valor is not None:
        dentro.append(pontos_progresso(n.valor, cores["text"]))
    botoes = [(k, t) for k, t in n.acoes if k != "default" and t]
    if botoes:
        linha = Gtk.Box(spacing=6)
        for k, t in botoes[:3]:
            b = Gtk.Button(label=t)
            b.add_css_class("hermes-acao")
            b.connect("clicked", lambda *_, k=k: ao_acao(n.id, k))
            linha.append(b)
        dentro.append(linha)
    clique = Gtk.GestureClick(button=0)

    def soltou(g, _n, _x, _y):
        if g.get_current_button() == 3:
            ao_fechar(n.id)
        else:
            ao_clicar(n.id)
    clique.connect("released", soltou)
    caixa.add_controller(clique)
    return caixa


class Cartoes(Gtk.Window):
    """The stack of cards, on the TOP layer at the chosen position. Its size = its cards, so the rest of
    the screen stays clickable."""

    def __init__(self, config, servico):
        super().__init__(decorated=False)
        self.config, self.servico = config, servico
        self.add_css_class("hermes-cartoes")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.TOP)
        Camada.set_namespace(self, "hermes")
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.pilha = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=config["gap"])
        self.set_child(self.pilha)
        self.widgets = {}
        self.ancorar()

    def ancorar(self):
        pos = posicao(self.config)
        v, h_ = pos.split("-")
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, False)
        Camada.set_anchor(self, Camada.Edge.TOP if v == "top" else Camada.Edge.BOTTOM, True)
        if h_ != "center":
            Camada.set_anchor(self, Camada.Edge.LEFT if h_ == "left" else Camada.Edge.RIGHT, True)
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_margin(self, borda, self.config["margin"])
        if self.config["position"] == "follow-bell":
            sino = lugar_do_sino()
            if sino:                       # the card's right edge sits just right of the bell
                Camada.set_margin(self, Camada.Edge.RIGHT,
                                  int(max(self.config["margin"], sino["w"] - sino["x"] - 30)))
        self.de_baixo = v == "bottom"

    def mostrar(self, n):
        velho = self.widgets.pop(n.id, None)
        novo = montar_cartao(n, self.config, lambda i: self.servico.acionar(i),
                             lambda i, k: self.servico.acionar(i, k), lambda i: self.servico.fechar(i),
                             claro="claro" in self.servico.modos)
        if velho is not None:
            self.pilha.insert_child_after(novo, velho)
            self.pilha.remove(velho)
        elif self.de_baixo:
            self.pilha.append(novo)
        else:
            self.pilha.prepend(novo)
        self.widgets[n.id] = novo
        # too many: the oldest leaves the screen (it stays in the history)
        while len(self.widgets) > self.config["max_visible"]:
            self._sumir(self.widgets.pop(min(self.widgets)))
        self.set_visible(True)

    def tirar(self, id_):
        w = self.widgets.pop(id_, None)
        if w is not None:
            self._sumir(w)

    def _sumir(self, w):
        """The card dissolves (dots go out), then leaves the stack; the window hides when empty."""
        def fim():
            if w.get_parent() is self.pilha:
                self.pilha.remove(w)
            if not self.widgets and self.pilha.get_first_child() is None:
                self.set_visible(False)
        if hasattr(w, "sair"):
            w.sair(fim)
        else:
            fim()

    def centro_do_mais_novo(self):
        """Approximate screen point of the newest card (for the wave's origin), or None."""
        return None


STEAM_TOAST = re.compile(r"notification|toast", re.I)


def ouvir_hyprland(ao_evento):
    """Thread: Hyprland's event socket (socket2). ao_evento(name, data) runs in the GTK thread."""
    import socket
    import threading
    caminho = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "hypr",
                           os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""), ".socket2.sock")

    def laco():
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sk:
                    sk.connect(caminho)
                    resto = b""
                    while True:
                        bloco = sk.recv(65536)
                        if not bloco:
                            break
                        resto += bloco
                        *linhas, resto = resto.split(b"\n")
                        for linha in linhas:
                            nome, _, dados = linha.decode("utf-8", "replace").partition(">>")
                            if nome in ("fullscreen", "openwindow"):
                                GLib.idle_add(lambda n=nome, d=dados: (ao_evento(n, d), False)[1])
            except OSError:
                pass
            time.sleep(3)                                     # Hyprland restarting: try again
    if os.path.exists(caminho):
        threading.Thread(target=laco, daemon=True, name="hermes-hyprland").start()
        return True
    return False


def janela_hyprland(endereco):
    """Position/size of a window by address (hyprctl clients), or None."""
    try:
        import json
        r = subprocess.run(["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2)
        for c in json.loads(r.stdout or "[]"):
            if c.get("address", "").lstrip("0x") == endereco.lstrip("0x"):
                return c
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return None


class HermesUI:
    """Connects the service to the screen: show/update/close -> cards; every new one -> a wave.
    Also: settings/modes reload, fullscreen hold (from Hyprland events) and Steam's own popups."""

    def __init__(self, servico, eh_tela_cheia=tela_cheia, ouvir=True):
        self.servico = servico
        self.config = servico.config
        self.eh_tela_cheia = eh_tela_cheia
        self.provedor = Gtk.CssProvider()
        self.aplicar_css()
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.onda = Onda(self.config)
        self.cartoes = Cartoes(self.config, servico)
        self.guardadas = []                                   # held while a fullscreen app has focus
        self._cheia = False
        servico.segurar = self._segurar
        servico.ouvir(self.evento)
        if ouvir:
            ouvir_hyprland(self.hyprland)

    def aplicar_css(self):
        self.provedor.load_from_string(css_do_tema(self.config, "claro" in self.servico.modos))

    def _segurar(self, n):
        """Called by the service before "show": is this card held right now?"""
        self._cheia = self.eh_tela_cheia()
        return self._cheia and self.config["fullscreen"] == "hold" and n.urgencia < 2

    def evento(self, ev, n):
        if ev in ("config", "modes"):
            self.config = self.servico.config
            self.onda.config = self.cartoes.config = self.config
            self.aplicar_css()
            if ev == "config":
                self.cartoes.ancorar()
            return
        if ev == "close":
            self.guardadas = [x for x in self.guardadas if x.id != n.id]
            self.cartoes.tirar(n.id)
            return
        cheia = self._cheia
        self._cheia = False
        if ev == "show" and n.onda:
            self.onda.soltar(urgente=n.urgencia == 2, sobre_tela_cheia=cheia)
        if cheia and self.config["fullscreen"] == "hold" and n.urgencia < 2:
            self.guardadas = [x for x in self.guardadas if x.id != n.id] + [n]
            return
        self.cartoes.mostrar(n)

    def saiu_da_tela_cheia(self):
        """Fullscreen ended: the held cards come in now (only those still open), timers start now."""
        guardadas, self.guardadas = self.guardadas, []
        for n in guardadas:
            if n.id in self.servico.ativas:
                self.cartoes.mostrar(n)
                self.servico.armar(n.id)

    def hyprland(self, nome, dados):
        if nome == "fullscreen":
            if dados.strip() == "0" and self.guardadas and not self.eh_tela_cheia():
                self.saiu_da_tela_cheia()
            return
        # openwindow>>ADDRESS,WORKSPACE,CLASS,TITLE : Steam's own notification popups
        partes = dados.split(",", 3)
        if len(partes) < 4 or self.config["steam"] == "off":
            return
        endereco, _ws, classe, titulo = partes
        if classe.lower() != "steam" or not STEAM_TOAST.search(titulo):
            return
        GLib.timeout_add(120, lambda: (self.popup_da_steam(endereco), False)[1])   # let it get its size

    def popup_da_steam(self, endereco):
        c = janela_hyprland(endereco)
        if c is None:
            return
        cheia = self.eh_tela_cheia()
        x, y = c.get("at", [0, 0])
        w, h = c.get("size", [0, 0])
        if self.config["steam"] == "move" and not cheia:
            m = self.config["margin"]
            pos = posicao(self.config)
            mon = self.monitor_tamanho()
            nx = {"left": m, "center": (mon[0] - w) // 2, "right": mon[0] - w - m}[pos.split("-")[1]]
            ny = m + 40 if pos.startswith("top") else mon[1] - h - m
            subprocess.run(["hyprctl", "dispatch", "movewindowpixel", f"exact {nx} {ny},address:0x{endereco.lstrip('0x')}"],
                           capture_output=True, timeout=2)
            x, y = nx, ny
        # the wave comes out of the popup (local coordinates of the focused monitor are close enough)
        self.onda.soltar(urgente=False, cartao=(x + w / 2, y + h / 2), sobre_tela_cheia=cheia)

    def monitor_tamanho(self):
        try:
            import json
            r = subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True, text=True, timeout=2)
            for m in json.loads(r.stdout or "[]"):
                if m.get("focused"):
                    return int(m["width"] / m.get("scale", 1)), int(m["height"] / m.get("scale", 1))
        except (OSError, ValueError, subprocess.TimeoutExpired, KeyError):
            pass
        return 1920, 1080
