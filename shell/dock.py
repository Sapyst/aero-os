#!/usr/bin/env python3
"""
dock.py — o Dock: barra de apps na base da tela, centralizada (doc 24).

O que ele faz:
- Ícones redondos dos apps fixados (Claude, Firefox e Settings de começo). Os apps abertos que não
  estão fixados aparecem depois de um separador (dá pra desligar no Settings).
- Bolinha embaixo = app aberto; tracinho = o app que está em uso.
- Mouse por cima: "onda". Os ícones perto do mouse crescem e sobem, como um relevo que segue o mouse.
- Intellihide (o padrão):
  - Fica visível enquanto nenhuma janela encosta nele.
  - Uma janela encostou (ou foi maximizada): ele desce e vira uma LINHA "dormente" no vão entre as
    janelas e a borda da tela (o gaps_out do Hyprland, 12 px).
  - Encostou o mouse na linha: ele sobe POR CIMA das janelas, sem mexer nelas.
  - Tirou o mouse: volta ao que era antes.
  - Tela cheia (jogo, vídeo): some de vez, nem a linha fica.
- A linha e o Dock são o MESMO desenho. Ao subir, a linha alarga e engrossa até virar a barra, e os
  ícones nascem do centro pra fora, um depois do outro (a "onda"). Ao descer, o mesmo ao contrário.
- Clique: abre o app; se já está aberto, traz pra frente (e restaura se estava minimizado); se já
  está na frente, minimiza. Com várias janelas, cada clique vai pra próxima. Botão do meio = janela
  nova. Botão direito = menu (Add App com busca ali mesmo, fechar, remover, separador, espaço, Dock Settings…).

Peças técnicas (detalhes no doc 24):
- É uma CAMADA (layer-shell) presa na base, da largura da tela, com altura fixa. A camada nunca muda
  de tamanho nem se mexe: toda a animação é desenhada pelo próprio GTK, quadro a quadro
  (add_tick_callback). Mudar o tamanho de uma camada a cada quadro faria a tela "tremer".
- Quase toda a faixa é transparente. A "região de entrada" (input region) diz ao Hyprland onde o
  Dock recebe o mouse; no resto, o clique passa pra janela de trás. Ela muda com o estado: na linha
  dormente é só um pedacinho do vão; com o Dock visível, é a barra.
- Pra saber se uma janela encosta, ouvimos os eventos do Hyprland (socket2: janela abriu, fechou,
  mudou de área, tela cheia...) e pedimos a lista de janelas pelo socket de comandos (sem rodar o
  hyprctl). O Hyprland NÃO avisa quando você arrasta uma janela, então, só enquanto existe janela
  flutuante na tela, conferimos de novo 2x por segundo. É uma conversa direta pelo socket, sem abrir
  outro programa: custa quase nada.

Configurações (Settings → Dock, chaves DOCK_* no painel.conf): o Dock vigia o arquivo e aplica na hora.
Apps do Dock: ~/.config/aero-shell/dock.json (o "Add to Dock" do Super Menu também grava lá).
Precisa de LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so (o lançador ~/.local/bin/dock faz isso).
"""
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
import time

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import apps  # noqa: E402

PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")
CORES_BARRA = os.path.expanduser("~/.config/waybar/cores.css")      # a cor da barra de cima (doc 18)
HYPR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hypr",
                    os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""))

# Medidas (em pixels). Bate com a barra de cima: margem 8, cantos 14 (waybar/style.css e config.jsonc)
MARGEM = 8           # distância da barra até a borda de baixo da tela
FOLGA = 8            # espaço entre os ícones e a borda da barra
ESPACO = 8           # espaço entre um ícone e outro
RAIO = 22            # cantos: 22 px, o mesmo de TUDO no sistema (regra do Aero, doc 30)


def visual_atual():
    try:
        for linha in open(os.path.expanduser("~/.config/hypr/painel.conf")):
            if linha.startswith("VISUAL="):
                return linha.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "AeroTerm"


class BolhaIcone(Gtk.DrawingArea):
    """DotStyle: the app's real icon inside a round bubble (tile icons cropped round, like the start menu)."""

    def __init__(self, gicon, tamanho, nome=""):
        super().__init__(content_width=tamanho, content_height=tamanho, hexpand=True, vexpand=True)
        self.ic = None
        self.nome = nome
        try:
            import nuvem
            tema = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
            p = tema.lookup_by_gicon(gicon, 128, 1, Gtk.TextDirection.NONE, 0)
            f = p.get_file() if p is not None else None
            if f is not None and f.get_path() and "missing" not in f.get_path():
                from gi.repository import GdkPixbuf
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(f.get_path(), 128, 128, True)
                sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 128, 128)
                c = cairo.Context(sup)
                Gdk.cairo_set_source_pixbuf(c, pb, (128 - pb.get_width()) / 2, (128 - pb.get_height()) / 2)
                c.paint()
                self.ic = (sup, nuvem.quadrado(sup))
        except (GLib.Error, TypeError, ValueError, ImportError):
            self.ic = None
        self.set_draw_func(self._desenhar)

    def _desenhar(self, _a, cr, w, h):
        x, y, r = w / 2, h / 2, min(w, h) / 2 - 1
        cr.arc(x, y, r, 0, 2 * math.pi)
        cr.set_source_rgba(0.07, 0.07, 0.08, 0.92)
        cr.fill_preserve()
        cr.set_source_rgba(0.925, 0.92, 0.91, 0.14)
        cr.set_line_width(1)
        cr.stroke()
        if self.ic is None:                                  # no icon in the theme: the initial in dots
            import hermes_dots as hd
            letra = (self.nome[:1] or "?").upper()
            p = r * 0.11
            hd.desenhar_texto(cr, letra, x - 2.5 * p, y - 3.5 * p, p, (0.925, 0.92, 0.91), 0.9)
            return
        sup, placa = self.ic
        cr.save()
        if placa:
            px0, py0, px1, py1 = placa
            ri = r * 0.92
            cr.arc(x, y, ri, 0, 2 * math.pi)
            cr.clip()
            esc = ri * 2 * 1.1 / max(px1 - px0, py1 - py0)
            cr.translate(x, y)
            cr.scale(esc, esc)
            cr.translate(-(px0 + px1) / 2, -(py0 + py1) / 2)
        else:
            lado = r * 1.3
            cr.translate(x - lado / 2, y - lado / 2)
            cr.scale(lado / 128, lado / 128)
        cr.set_source_surface(sup, 0, 0)
        cr.paint()
        cr.restore()
LARG_SEPARADOR = 10
LINHA_ALT = 5        # espessura da linha dormente
LINHA_ALT_VAZIO = 7  # ...e da linha do Dock vazio ("meio grossa", pedido do Aero)

DEPURAR = bool(os.environ.get("DOCK_DEPURAR"))   # DOCK_DEPURAR=1 dock: mostra no log o que está desenhando

PADROES = {"DOCK_MODO": "inteligente", "DOCK_TAMANHO": "48", "DOCK_ONDA": "60",
           "DOCK_ALCANCE": "1.2", "DOCK_ABERTOS": "1", "DOCK_MOVIMENTO": "0"}


# ---------- utilidades ----------

def ler_opcoes():
    opcoes = dict(PADROES)
    try:
        with open(PAINEL) as f:
            for linha in f:
                chave, _, valor = linha.strip().partition("=")
                if chave in opcoes:
                    opcoes[chave] = valor.strip('"')
    except OSError:
        pass
    return opcoes


def hypr(comando):
    """Conversa direto com o Hyprland pelo socket de comandos (o mesmo que o hyprctl usa por baixo).
    Ex: hypr("j/clients") devolve o texto JSON das janelas; hypr("dispatch focuswindow ...")."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(os.path.join(HYPR, ".socket.sock"))
            s.sendall(comando.encode())
            partes = []
            while True:
                bloco = s.recv(65536)
                if not bloco:
                    break
                partes.append(bloco)
        return b"".join(partes).decode("utf-8", "replace")
    except OSError:
        return ""


def hypr_json(comando, padrao):
    try:
        return json.loads(hypr("j/" + comando) or "null") or padrao
    except ValueError:
        return padrao


def hyprland_vivo():
    """O Hyprland em que este Dock nasceu (o da HYPRLAND_INSTANCE_SIGNATURE) ainda está vivo?
    "Bate na porta" do socket de comandos. Pegadinha: quando o Hyprland CAI, o arquivo .socket.sock
    continua lá (ninguém apaga), só que ninguém atende: dá "conexão recusada". Por isso não basta
    ver se o arquivo existe."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(os.path.join(HYPR, ".socket.sock"))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False          # ninguém atende (ou nem existe o arquivo): essa sessão acabou
    except OSError:
        return True           # outro problema (ex: demorou): na dúvida, segue (não fecha o Dock à toa)


def ler_cores():
    """Pega a cor da barra de cima no cores.css da waybar (que o hypr-tema gera): assim o Dock
    acompanha sozinho o tema claro/escuro, a cor escolhida no Settings e a opacidade."""
    cores = {"fundo": (0.965, 0.965, 0.965, 0.80), "texto": (0.118, 0.118, 0.118, 1.0),
             "destaque": (0.208, 0.518, 0.894, 1.0)}
    try:
        with open(CORES_BARRA) as f:
            texto = f.read()
    except OSError:
        return cores
    for nome in cores:
        m = re.search(r"@define-color\s+" + nome + r"\s+([^;]+);", texto)
        if m:
            rgba = Gdk.RGBA()
            if rgba.parse(m.group(1).strip()):
                cores[nome] = (rgba.red, rgba.green, rgba.blue, rgba.alpha)
    return cores


def suave(t):          # "ease out cubic": começa rápido e freia no fim
    return 1 - (1 - t) ** 3


def com_mola(t, s=1.5):  # "ease out back": passa um pouquinho do ponto e volta (efeito mola)
    t -= 1
    return t * t * ((s + 1) * t + s) + 1


def limitar(x, a=0.0, b=1.0):
    return a if x < a else b if x > b else x


def rgba(c, alfa=None):
    cor = Gdk.RGBA()
    cor.red, cor.green, cor.blue = c[0], c[1], c[2]
    cor.alpha = c[3] if alfa is None else alfa
    return cor


def retangulo(x, y, w, h):
    return Graphene.Rect().init(x, y, max(0.0, w), max(0.0, h))


def cantos_redondos(ret, raio):
    """Retângulo de cantos redondos pro GSK. Atenção (pegadinha do PyGObject): NÃO dá pra escrever
    Gsk.RoundedRect().init_from_rect(...) numa linha só: o valor devolvido vem com lixo na memória.
    Tem que criar primeiro e iniciar depois."""
    contorno = Gsk.RoundedRect()
    contorno.init_from_rect(ret, raio)
    return contorno


# ---------- um item do Dock ----------

class Item:
    """Um app, um separador ou um espaço. Apps têm um widget (a bolha com o ícone)."""

    def __init__(self, tipo, app=None, fixado=True):
        self.tipo, self.app, self.fixado = tipo, app, fixado
        self.widget = None
        self.janelas = []            # janelas abertas deste app (dicionários do Hyprland)
        self.ativo = False           # é o app em uso agora?
        self.quique_desde = None     # pulando (abrindo)? desde quando
        self.entrada = 0.0           # 0→1: animação de entrar no Dock (ao adicionar)
        self.indice = None           # posição no dock.json
        # calculados a cada quadro:
        self.x = self.y = self.tam = 0.0
        self.escala = 1.0
        self.visivel = 1.0

    @property
    def chave(self):
        return self.app.get_id() if self.app else None


# ---------- o widget que desenha tudo ----------

class Faixa(Gtk.Widget):
    """Desenha a barra/linha e posiciona os ícones na mão (por isso a onda pode mexer em tudo)."""

    def __init__(self, dock):
        super().__init__()
        self.dock = dock

    def do_measure(self, orientacao, _para):
        if orientacao == Gtk.Orientation.VERTICAL:
            return self.dock.altura, self.dock.altura, -1, -1
        return 1, 1, -1, -1

    def do_size_allocate(self, largura, altura, _base):
        self.dock.calcular(largura, altura)
        # Menus (popovers) presos direto nesta faixa: um widget feito à mão tem que "apresentar"
        # os popovers dele a cada layout, senão o menu não acompanha mudanças de tamanho e o GTK
        # fecha ele (era o que fechava o "Add App" ao trocar de página).
        filho = self.get_first_child()
        while filho is not None:
            if isinstance(filho, Gtk.Popover):
                filho.present()
            filho = filho.get_next_sibling()
        for item in self.dock.itens:
            if item.widget is None:
                continue
            maximo = self.dock.tam_max
            escala = max(0.001, item.escala)
            transformacao = (Gsk.Transform.new()
                             .translate(Graphene.Point().init(item.x, item.y))
                             .scale(escala, escala))
            item.widget.set_child_visible(item.visivel > 0.01)
            item.widget.set_opacity(limitar(item.visivel))
            item.widget.allocate(maximo, maximo, -1, transformacao)

    def do_snapshot(self, snap):
        d = self.dock
        if getattr(d, "dots", False):
            return self.snapshot_pontos(snap)
        if DEPURAR:
            print("desenho: fase", round(d.fase, 2), "alvo", d.alvo, "barra", d.ret_barra, "alfa", round(d.alfa_barra, 2),
                  "encostado", d.encostado, "tela cheia", d.tela_cheia, flush=True)
        fundo = d.cores["fundo"]
        if d.ret_barra and d.alfa_barra > 0.005:
            x, y, w, h = d.ret_barra
            raio = min(RAIO, h / 2)
            contorno = cantos_redondos(retangulo(x, y, w, h), raio)
            # sombra suave embaixo da barra (some quando ela vira linha)
            snap.append_outset_shadow(contorno, rgba((0, 0, 0, 1), (0.10 + 0.08 * d.forma) * d.alfa_barra),
                                      0, 1 + d.forma, 0, 3 + 7 * d.forma)
            snap.push_rounded_clip(contorno)
            # A barra de cima tem 80% + desfoque do Hyprland. Camada GTK4 não ganha desfoque (doc 16),
            # então o Dock fica um pouco mais sólido (90%) pra o que está atrás não atrapalhar a leitura.
            alfa = max(fundo[3], 0.90)
            snap.append_color(rgba(fundo, alfa * d.alfa_barra), retangulo(x, y, w, h))
            snap.pop()
            # contorno fino: a barra (e principalmente a linha dormente) continua visível sobre um
            # papel de parede da mesma cor dela. Mais forte na linha, mais suave na barra.
            borda = rgba(d.cores["texto"], (0.10 + 0.25 * (1 - d.forma)) * d.alfa_barra)
            snap.append_border(contorno, [1, 1, 1, 1], [borda, borda, borda, borda])
        for item in d.itens:
            if item.tipo == "separador" and item.visivel > 0.01:
                altura = d.tam * 0.6 * item.visivel
                snap.append_color(rgba(d.cores["texto"], 0.25 * item.visivel),
                                  retangulo(item.x - 1, item.y - altura / 2, 2, altura))
            elif item.widget is not None:
                self.snapshot_child(item.widget, snap)
                # bolinha (aberto) ou tracinho (em uso)
                if item.janelas and item.visivel > 0.01:
                    largura = 12 if item.ativo else 5
                    cx = item.x + d.tam_max * item.escala / 2
                    y = d.base_barra - 4.5
                    ponto = cantos_redondos(retangulo(cx - largura / 2, y, largura, 4), 2)
                    snap.push_rounded_clip(ponto)
                    snap.append_color(rgba(d.cores["texto"], (0.75 if item.ativo else 0.5) * item.visivel),
                                      retangulo(cx - largura / 2, y, largura, 4))
                    snap.pop()


    def snapshot_pontos(self, snap):
        """DotStyle dock: no bar. A halftone of dots around the bubbles (big against them, fading out), which
        follows the magnifying wave on its own because it is measured from the bubbles' real size every frame.
        Sleeping (thin line state): just a row of dots. Open apps: glowing dots; separators: a dot column."""
        d = self.dock
        w, h = self.get_width(), self.get_height()
        cr = snap.append_cairo(Graphene.Rect().init(0, 0, w, h))
        tx = d.cores["texto"]
        fg = (tx[0], tx[1], tx[2])
        if d.ret_barra and d.alfa_barra > 0.005:
            x, y, bw, bh = d.ret_barra
            bolhas = [(it.x + d.tam_max * it.escala / 2, it.y + d.tam_max * it.escala / 2, d.tam_max * it.escala / 2 * 0.96)
                      for it in d.itens if it.widget is not None and it.visivel > 0.01]
            if d.forma < 0.35 or not bolhas:                       # sleeping line: a row of dots
                cy = y + bh / 2
                n = max(2, int(bw / 9))
                cr.new_path()
                for i in range(n + 1):
                    cr.new_sub_path()
                    cr.arc(x + bw * i / n, cy, 1.6, 0, 2 * math.pi)
                cr.set_source_rgba(*fg, 0.55 * d.alfa_barra)
                cr.fill()
            else:
                passo, alc = 11.0, 62.0
                grupos = [[] for _ in range(10)]
                gy = y - alc
                linha = 0
                while gy < min(h, y + bh + alc):
                    gx = x - alc + (passo / 2 if linha % 2 else 0)
                    while gx < x + bw + alc:
                        dmin = 999.0
                        for bx, by, br in bolhas:
                            dd = math.hypot(gx - bx, gy - by) - br
                            if dd < dmin:
                                dmin = dd
                        if 3 < dmin < alc:
                            t = dmin / alc
                            a = (0.05 + 0.4 * (1 - t) ** 1.3) * d.alfa_barra
                            grupos[min(9, int(a * 20))].append((gx, gy, 0.8 + 3.0 * (1 - t) ** 1.6))
                        gx += passo
                    gy += passo * 0.87
                    linha += 1
                for k, grupo in enumerate(grupos):
                    if not grupo:
                        continue
                    cr.new_path()
                    for gx, gy, r in grupo:
                        cr.new_sub_path()
                        cr.arc(gx, gy, r, 0, 2 * math.pi)
                    cr.set_source_rgba(*fg, (k + 0.5) / 20)
                    cr.fill()
        for item in d.itens:
            if item.tipo == "separador" and item.visivel > 0.01:
                for k in (-1, 0, 1):
                    cr.arc(item.x, item.y + k * 7, 1.5, 0, 2 * math.pi)
                    cr.set_source_rgba(*fg, 0.45 * item.visivel)
                    cr.fill()
        cr = None
        for item in d.itens:
            if item.tipo != "separador" and item.widget is not None:
                self.snapshot_child(item.widget, snap)
        cr = snap.append_cairo(Graphene.Rect().init(0, 0, w, h))
        for item in d.itens:
            if item.widget is not None and item.janelas and item.visivel > 0.01:
                cx = item.x + d.tam_max * item.escala / 2
                cy = d.base_barra - 2.5
                r = 3.0 if item.ativo else 2.0
                g = cairo.RadialGradient(cx, cy, 0, cx, cy, r * 3.5)
                for k in range(7):
                    t = k / 6
                    g.add_color_stop_rgba(t, *fg, (0.5 if item.ativo else 0.25) * math.exp(-4 * t * t) * item.visivel)
                cr.set_source(g)
                cr.arc(cx, cy, r * 3.5, 0, 2 * math.pi)
                cr.fill()
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.set_source_rgba(*fg, (1.0 if item.ativo else 0.7) * item.visivel)
                cr.fill()



def carregar_dicas_dotstyle():
    """DotStyle tooltips (dotstyle-dicas.css), only when the visual is DotStyle."""
    caminho = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dotstyle-dicas.css")
    if visual_atual() == "DotStyle" and os.path.exists(caminho):
        prov = Gtk.CssProvider()
        prov.load_from_path(caminho)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_USER + 3)

# ---------- a janela-camada ----------

class Dock(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Dock")
        self.add_css_class("dock")
        self.app = app
        self.dots = visual_atual() == "DotStyle"      # DotStyle: halftone dock (Faixa.snapshot_pontos)
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.TOP)             # acima das janelas (abaixo de tela cheia)
        Camada.set_namespace(self, "dock")
        for borda in (Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)              # presa na base, de ponta a ponta
        Camada.set_exclusive_zone(self, -1)                   # não reserva espaço: as janelas não mudam
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)

        self.opcoes = ler_opcoes()
        self.cores = ler_cores()
        self.itens = []
        self.clientes = []
        self.fase = 0.0               # 0 = linha dormente, 1 = Dock visível (a animação anda por aqui)
        self.alvo = 0.0
        self.onda = 0.0               # quanto a onda do mouse está valendo (0 a 1)
        self.mouse_x = None           # posição do mouse (suavizada)
        self.mouse_x_real = None
        self.mouse_dentro = False
        self.menu_aberto = False
        self.encostado = False        # alguma janela encosta na área do Dock?
        self.tela_cheia = False       # alguma janela em tela cheia (jogo, vídeo)?
        self.linha_visivel = 1.0      # some em tela cheia
        self.relogio = None           # o tick (só existe enquanto anima)
        self.ultimo_quadro = None
        self.espera_sair = None
        self.vigia_flutuantes = None
        self.regiao_atual = None
        self.monitor = None
        self.ret_barra = None
        self.alfa_barra = 1.0
        self.forma = 0.0
        self.base_barra = 0.0
        self.medidas()

        self.faixa = Faixa(self)
        self.set_child(self.faixa)
        movimento = Gtk.EventControllerMotion()
        movimento.connect("enter", self.ao_entrar)
        movimento.connect("motion", self.ao_mover)
        movimento.connect("leave", self.ao_sair)
        self.faixa.add_controller(movimento)
        direito = Gtk.GestureClick(button=3)                 # botão direito na barra (fora dos ícones)
        direito.connect("pressed", self.ao_clicar_faixa)
        self.faixa.add_controller(direito)
        esquerdo = Gtk.GestureClick(button=1)                # clique na linha do Dock vazio = menu
        esquerdo.connect("released", lambda _g, _n, x, y: (not self.itens) and self.menu_dock(self.faixa, x=x, y=y))
        self.faixa.add_controller(esquerdo)

        self.mon_info = None
        self.abertos = {}             # app -> janelas abertas dele
        self.montar_itens()
        self.connect("map", lambda *_: GLib.timeout_add(300, self.ao_aparecer))

    # ---------- medidas que dependem das opções ----------

    def medidas(self):
        o = self.opcoes
        self.tam = max(24, min(96, int(o["DOCK_TAMANHO"])))
        self.movimento = o["DOCK_MOVIMENTO"] != "1"               # "Reduce motion" desliga as animações
        self.intensidade = limitar(int(o["DOCK_ONDA"]) / 100, 0, 1.2) if self.movimento else 0.0
        self.alcance = limitar(float(o["DOCK_ALCANCE"]), 0.5, 3.0)
        self.altura_barra = self.tam + 2 * FOLGA
        # Ícone desenhado no tamanho MÁXIMO da onda e reduzido pela transformação: fica nítido
        # quando cresce (sem refazer a imagem a cada quadro).
        self.tam_max = int(math.ceil(self.tam * (1 + max(self.intensidade, 0.0))))
        # altura fixa da camada: barra + o quanto o ícone cresce e sobe + respiro
        self.altura = int(MARGEM + self.altura_barra + self.tam * self.intensidade * 1.35 + 12)
        self.gap = self.ler_gap()

    def ler_gap(self):
        """O vão entre as janelas e a borda da tela (gaps_out do Hyprland); a linha dorme nele."""
        texto = hypr("j/getoption general:gaps_out")
        try:
            valor = json.loads(texto)
            numeros = [int(n) for n in re.findall(r"\d+", str(valor.get("custom", valor.get("int", 12))))]
            return max(6, numeros[2] if len(numeros) > 2 else numeros[0])    # o de baixo (cima, dir, baixo, esq)
        except (ValueError, IndexError, AttributeError):
            return 12

    # ---------- itens ----------

    def montar_itens(self):
        """Lê o dock.json e (se ligado) acrescenta os apps abertos que não estão fixados."""
        antigos = {(i.tipo, i.chave, i.fixado): i for i in self.itens if i.tipo == "app"}
        novos = []
        for n, registro in enumerate(apps.ler_lista("dock", apps.DOCK_PADRAO)):
            tipo = registro.get("tipo")
            item = None
            if tipo == "app":
                app = apps.por_id(registro.get("id", ""))
                if app is not None:             # desinstalado: some da tela (continua no arquivo)
                    item = antigos.pop(("app", app.get_id(), True), None) or Item("app", app)
            elif tipo in ("separador", "espaco"):
                item = Item(tipo)
            if item is not None:
                item.indice = n                 # posição no dock.json (pra remover/inserir no lugar certo)
                novos.append(item)
        if self.opcoes["DOCK_ABERTOS"] == "1":
            fixados = {i.chave for i in novos if i.tipo == "app"}
            soltos = [antigos.pop(("app", chave, False), None) or Item("app", apps.por_id(chave), fixado=False)
                      for chave in self.abertos if chave not in fixados and apps.por_id(chave)]
            if soltos:
                separador = Item("separador", fixado=False)
                separador.indice = None
                novos.append(separador)
                novos.extend(soltos)
        for velho in antigos.values():          # tira os widgets dos que saíram
            if velho.widget is not None:
                velho.widget.unparent()
        primeira_vez = not self.itens
        for item in novos:
            if item.tipo == "app" and item.widget is None:
                item.widget = self.criar_bolha(item)
                item.widget.set_parent(self.faixa)
                item.entrada = 1.0 if primeira_vez else 0.0     # quem chega depois entra animado
        self.itens = novos
        self.marcar_janelas()
        self.animar()

    def criar_bolha(self, item):
        bolha = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        bolha.add_css_class("bolha")
        bolha.set_size_request(self.tam_max, self.tam_max)
        gicon = item.app.get_icon() or Gio.ThemedIcon.new("application-x-executable")
        if getattr(self, "dots", False):
            icone = BolhaIcone(gicon, self.tam_max, item.app.get_display_name() or "")
        else:
            icone = Gtk.Image(gicon=gicon, pixel_size=int(self.tam_max * 0.72), hexpand=True, vexpand=True)
        bolha.append(icone)
        bolha.set_tooltip_text(item.app.get_display_name())
        clique = Gtk.GestureClick(button=0)
        clique.connect("released", lambda g, n, x, y: self.ao_clicar(g, item))
        bolha.add_controller(clique)
        return bolha

    # ---------- geometria (a cada quadro) ----------

    def larguras(self):
        return [self.tam if i.tipo == "app" else LARG_SEPARADOR if i.tipo == "separador" else self.tam / 2
                for i in self.itens]

    def largura_repouso(self):
        """Largura da barra parada (sem onda), pra saber onde o Dock fica na tela."""
        if not self.itens:
            return 150.0
        ls = self.larguras()
        return sum(ls) + ESPACO * (len(ls) - 1) + 2 * FOLGA

    def calcular(self, largura, altura):
        """Onde fica cada coisa neste quadro: linha/barra, ícones (com onda e animação de surgir)."""
        centro = largura / 2
        self.base_barra = altura - MARGEM                     # embaixo da barra
        topo_barra = self.base_barra - self.altura_barra
        larg_repouso = self.largura_repouso()
        # A linha dormente: no meio do vão (gap) entre as janelas e a borda da tela
        vazio = not self.itens
        esp_linha = LINHA_ALT_VAZIO if vazio else LINHA_ALT
        larg_linha = 150.0 if vazio else max(90.0, larg_repouso * 0.45)
        y_linha = altura - self.gap / 2 - esp_linha / 2
        if vazio and self.mouse_dentro:
            esp_linha += 2                                    # engrossa um pouco com o mouse em cima

        f = self.fase
        # ---- itens: larguras com a onda ----
        ls = self.larguras()
        n = len(ls)
        inicio_repouso = centro - (sum(ls) + ESPACO * max(0, n - 1)) / 2
        centros, pos = [], inicio_repouso
        for w in ls:
            centros.append(pos + w / 2)
            pos += w + ESPACO
        fatores = []
        for item, c in zip(self.itens, centros):
            fator = 0.0
            if self.mouse_x is not None and self.onda > 0.001 and self.intensidade > 0 and item.tipo != "separador":
                dist = (self.mouse_x - c) / self.tam
                fator = self.onda * self.intensidade * math.exp(-(dist * dist) / (2 * self.alcance ** 2))
            fatores.append(fator)
        novas = [w * (1 + fa) for w, fa in zip(ls, fatores)]
        total = sum(novas) + ESPACO * max(0, n - 1)
        # Onde a fileira começa, com a onda. Regra do Dock do macOS: o ponto embaixo do mouse NÃO SE
        # MEXE. Os ícones à esquerda do mouse crescem pra esquerda, os da direita pra direita.
        # Conta: quanto os itens cresceram À ESQUERDA do mouse (o item sob o mouse conta só a parte
        # que está à esquerda dele); a fileira começa esse tanto antes da posição parada.
        # (A versão anterior "ancorava" o item errado e o Dock andava junto com o mouse: o ícone fugia.)
        if self.mouse_x is not None and n:
            cresceu_esquerda, pos_r = 0.0, inicio_repouso
            for w, w_novo in zip(ls, novas):
                if self.mouse_x >= pos_r + w:
                    cresceu_esquerda += w_novo - w                       # item inteiro à esquerda
                elif self.mouse_x > pos_r:
                    cresceu_esquerda += (w_novo - w) * (self.mouse_x - pos_r) / w   # mouse em cima dele
                pos_r += w + ESPACO
            inicio = inicio_repouso - cresceu_esquerda
        else:
            inicio = centro - total / 2
        # ---- barra: da linha até o retângulo cheio ----
        larg_cheia = (total + 2 * FOLGA) if n else larg_linha
        x_cheio = inicio - FOLGA if n else centro - larg_linha / 2
        if vazio:
            self.forma = 0.0
            self.ret_barra = (centro - larg_linha / 2, y_linha, larg_linha, esp_linha)
            self.alfa_barra = self.linha_visivel
        else:
            lb = limitar(f / 0.55)                             # a barra anda na 1ª metade
            eb = com_mola(lb, 1.1) if self.movimento else lb
            ec = suave(lb)
            w = larg_linha + (larg_cheia - larg_linha) * eb
            h = esp_linha + (self.altura_barra - esp_linha) * ec
            y = y_linha + (topo_barra - y_linha) * ec
            x = (centro - larg_linha / 2) + (x_cheio - (centro - larg_linha / 2)) * eb
            self.forma = ec
            self.ret_barra = (x, y, w, h)
            self.alfa_barra = self.linha_visivel if f < 0.02 else 1.0
        # ---- cada item: escala, posição e o "surgir" em onda do centro pra fora ----
        pos = inicio
        meia = max(1.0, total / 2)
        agora = time.monotonic()
        for item, w_novo, fator in zip(self.itens, novas, fatores):
            meio = pos + w_novo / 2
            distancia = abs(meio - centro) / meia                 # 0 no centro, 1 na ponta
            local = limitar((f - 0.18 - distancia * 0.42) / 0.4)  # cada ícone tem a sua vez
            surgir = com_mola(local, 1.7) if self.movimento else local
            surgir *= suave(item.entrada) if self.movimento else 1.0
            item.visivel = limitar(local * 1.6) * min(1.0, item.entrada * 1.5 if self.movimento else 1.0)
            tam = self.tam * (1 + fator) * max(0.0, surgir)
            subida = fator * self.tam * 0.30                     # o "relevo" sobe além de crescer
            quique = 0.0
            if item.quique_desde is not None:
                t = agora - item.quique_desde
                if t > 8 or not self.movimento:
                    item.quique_desde = None
                else:
                    quique = abs(math.sin(t * math.pi * 2.2)) * self.tam * 0.32 * max(0.25, 1 - t / 8)
            fundo_icone = self.base_barra - FOLGA - (1 - max(0.0, surgir)) * self.altura_barra * 0.5
            item.escala = tam / self.tam_max
            item.x = meio - tam / 2
            item.y = fundo_icone - tam - subida - quique
            if item.tipo == "separador":
                item.x = meio
                item.y = self.base_barra - self.altura_barra / 2
            pos += w_novo + ESPACO
        self.atualizar_regiao(largura, altura, larg_repouso, y_linha, x_cheio, larg_cheia)

    # ---------- região de entrada (onde o Dock recebe o mouse) ----------

    def atualizar_regiao(self, largura, altura, larg_repouso, y_linha, x_cheio, larg_cheia):
        centro = largura / 2
        if self.tela_cheia and not self.menu_aberto:
            retangulos = []                                   # tela cheia: nada (o jogo recebe tudo)
        elif not self.itens:
            # Dock vazio: a linha "meio grossa" com uma área de clique folgada em volta
            retangulos = [(centro - 90, altura - self.gap - 10, 180, self.gap + 10)]
        elif self.alvo < 0.5 and not self.menu_aberto:
            # Dormente: só o vão embaixo do Dock (não rouba cliques das janelas)
            retangulos = [(centro - larg_repouso / 2, altura - self.gap, larg_repouso, self.gap)]
        else:
            topo = altura - MARGEM - self.altura_barra
            folga = 0
            if self.mouse_dentro:
                topo = 0                                      # com a onda, os ícones sobem: área maior
                folga = self.tam                              # e uma margem generosa dos lados:
                                                              # escapar uns pixels não derruba o Dock
            esq = max(0, min(x_cheio, centro - larg_repouso / 2) - folga)
            dir_ = min(largura, max(x_cheio + larg_cheia, centro + larg_repouso / 2) + folga)
            retangulos = [(esq, topo, dir_ - esq, altura - topo)]
        chave = tuple(tuple(int(round(v)) for v in r) for r in retangulos)
        if chave == self.regiao_atual or self.get_surface() is None:
            return
        self.regiao_atual = chave
        regiao = cairo.Region([cairo.RectangleInt(*r) for r in chave]) if chave else cairo.Region()
        # Aplica DEPOIS do quadro: mudar a região no meio do cálculo do layout fazia o GTK não
        # mostrar nada (descoberto no protótipo; doc 24).
        GLib.idle_add(self.aplicar_regiao, regiao)

    def aplicar_regiao(self, regiao):
        superficie = self.get_surface()
        if superficie is not None:
            superficie.set_input_region(regiao)
        return GLib.SOURCE_REMOVE

    # ---------- animação ----------

    def animar(self):
        """Liga o relógio de quadros (se não estiver ligado). Ele se desliga sozinho quando tudo para."""
        if self.relogio is None:
            self.ultimo_quadro = None
            self.relogio = self.faixa.add_tick_callback(self.quadro)
        self.faixa.queue_allocate()
        self.faixa.queue_draw()

    def quadro(self, _widget, relogio):
        agora = relogio.get_frame_time() / 1e6
        dt = 0.016 if self.ultimo_quadro is None else min(0.05, agora - self.ultimo_quadro)
        self.ultimo_quadro = agora
        parado = True
        # fase: anda em direção ao alvo (subir: ~0,55 s; descer: ~0,4 s)
        if self.fase != self.alvo:
            if not self.movimento:
                self.fase = self.alvo
            else:
                passo = dt / (0.55 if self.alvo > self.fase else 0.40)
                self.fase = min(self.alvo, self.fase + passo) if self.alvo > self.fase else max(self.alvo, self.fase - passo)
            parado = False
        # onda: segue o mouse com suavidade
        alvo_onda = 1.0 if (self.mouse_dentro and self.fase > 0.9) else 0.0
        if abs(self.onda - alvo_onda) > 0.002:
            self.onda += (alvo_onda - self.onda) * (1 - math.exp(-dt / 0.07))
            parado = False
        else:
            self.onda = alvo_onda
        if self.mouse_x_real is not None:
            if self.mouse_x is None:
                self.mouse_x = self.mouse_x_real
            elif abs(self.mouse_x - self.mouse_x_real) > 0.3:
                self.mouse_x += (self.mouse_x_real - self.mouse_x) * (1 - math.exp(-dt / 0.045))
                parado = False
        if self.onda == 0.0 and not self.mouse_dentro:
            self.mouse_x = self.mouse_x_real = None
        # linha some em tela cheia
        alvo_linha = 0.0 if self.tela_cheia else 1.0
        if abs(self.linha_visivel - alvo_linha) > 0.01:
            self.linha_visivel += (alvo_linha - self.linha_visivel) * (1 - math.exp(-dt / 0.08))
            parado = False
        else:
            self.linha_visivel = alvo_linha
        for item in self.itens:
            if item.entrada < 1.0:
                item.entrada = min(1.0, item.entrada + dt / 0.35)
                parado = False
            if item.quique_desde is not None:
                parado = False
        self.faixa.queue_allocate()
        self.faixa.queue_draw()
        if parado:
            self.relogio = None
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    # ---------- estado: visível ou dormente? ----------

    def decidir(self):
        modo = self.opcoes["DOCK_MODO"]
        if self.menu_aberto or (self.mouse_dentro and not self.tela_cheia):
            alvo = 1.0
        elif self.tela_cheia:
            alvo = 0.0
        elif modo == "sempre":
            alvo = 1.0
        elif modo == "auto":
            alvo = 0.0
        else:                                               # "inteligente" (intellihide)
            alvo = 0.0 if self.encostado else 1.0
        if alvo != self.alvo:
            self.alvo = alvo
            self.regiao_atual = None
        self.animar()

    def ao_entrar(self, _c, x, _y):
        if self.espera_sair:
            GLib.source_remove(self.espera_sair)
            self.espera_sair = None
        self.mouse_dentro = True
        self.mouse_x_real = x
        self.decidir()

    def ao_mover(self, _c, x, _y):
        self.mouse_x_real = x
        if not self.mouse_dentro:
            self.ao_entrar(_c, x, _y)
        self.animar()

    def ao_sair(self, *_):
        # espera um pouquinho: passar rápido pela borda não faz o Dock "piscar"
        if self.espera_sair:
            GLib.source_remove(self.espera_sair)
        self.espera_sair = GLib.timeout_add(300, self.saiu_de_vez)

    def saiu_de_vez(self):
        self.espera_sair = None
        self.mouse_dentro = False
        self.decidir()
        return GLib.SOURCE_REMOVE

    # ---------- janelas (Hyprland) ----------

    def ao_aparecer(self):
        superficie = self.get_surface()
        if superficie is not None:
            mon = Gdk.Display.get_default().get_monitor_at_surface(superficie)
            self.monitor = mon.get_connector() if mon else None
        self.atualizar_janelas()
        return GLib.SOURCE_REMOVE

    def clientes_visiveis(self, todos=False):
        """Janelas da área de trabalho que está na tela (todos=True: de todas as áreas, pros indicadores)."""
        if todos:
            return [c for c in self.clientes if c.get("mapped") and c.get("workspace", {}).get("id", 0) != 0]
        return [c for c in self.clientes if c.get("mapped") and not c.get("hidden") and self.na_tela(c)]

    def na_tela(self, c):
        mon = self.mon_info
        if not mon:
            return False
        ws = c.get("workspace", {})
        especial = (mon.get("specialWorkspace") or {}).get("name") or ""
        return ws.get("id") == mon["activeWorkspace"]["id"] or (especial and ws.get("name") == especial)

    def atualizar_janelas(self):
        self.clientes = hypr_json("clients", [])
        monitores = hypr_json("monitors", [])
        self.mon_info = next((m for m in monitores if m.get("name") == self.monitor), None) or \
            next((m for m in monitores if m.get("focused")), None)
        self.ativa = hypr_json("activewindow", {}).get("address")
        abertos = {}
        for c in self.clientes_visiveis(todos=True):
            nome_ws = c.get("workspace", {}).get("name", "")
            if nome_ws.startswith("special:") and nome_ws != "special:minimized":
                continue                                    # áreas escondidas (testes, scratchpad)
            app = apps.app_da_janela(c.get("class") or c.get("initialClass"))
            if app:
                abertos.setdefault(app.get_id(), []).append(c)
        mudou_abertos = set(abertos) != set(self.abertos)
        self.abertos = abertos
        if mudou_abertos and self.opcoes["DOCK_ABERTOS"] == "1":
            self.montar_itens()                             # entrou/saiu um app aberto não fixado
        else:
            self.marcar_janelas()
        # Encostando? (retângulo do Dock parado, em coordenadas da tela)
        self.encostado = self.tela_cheia = False
        flutuantes = False
        if self.mon_info:
            m = self.mon_info
            escala = m.get("scale", 1) or 1
            larg_mon, alt_mon = m["width"] / escala, m["height"] / escala
            if m.get("transform", 0) % 2 == 1:
                larg_mon, alt_mon = alt_mon, larg_mon
            larg = self.largura_repouso()
            dx = m["x"] + (larg_mon - larg) / 2
            dy = m["y"] + alt_mon - MARGEM - self.altura_barra
            for c in self.clientes_visiveis():
                if c.get("workspace", {}).get("name") == "special:minimized":
                    continue
                tela = c.get("fullscreen", 0)
                if tela == 2 or (tela and c.get("fullscreenClient", 0) == 2):
                    self.tela_cheia = True
                if tela == 1:
                    self.encostado = True                     # maximizada: o Dock dorme
                flutuantes = flutuantes or c.get("floating", False)
                (x, y), (w, h) = c["at"], c["size"]
                if x < dx + larg and x + w > dx and y < dy + self.altura_barra + MARGEM and y + h > dy:
                    self.encostado = True
        # O Hyprland não avisa quando uma janela é ARRASTADA: com janela flutuante na tela,
        # conferimos de novo a cada meio segundo (só nessa situação).
        if flutuantes and self.vigia_flutuantes is None:
            self.vigia_flutuantes = GLib.timeout_add(500, self.conferir_de_novo)
        elif not flutuantes and self.vigia_flutuantes is not None:
            GLib.source_remove(self.vigia_flutuantes)
            self.vigia_flutuantes = None
        self.publicar_posicoes()
        self.decidir()

    def publicar_posicoes(self):
        """Grava onde fica o ícone (em repouso, sem onda) de cada janela aberta, em coordenadas da tela:
        {endereço: [x, y]}. O hypr-minimizar usa isso pra animar a janela até o ícone certo."""
        m = getattr(self, "mon_info", None)
        if not m:
            return
        escala = m.get("scale", 1) or 1
        larg_mon, alt_mon = m["width"] / escala, m["height"] / escala
        if m.get("transform", 0) % 2 == 1:
            larg_mon, alt_mon = alt_mon, larg_mon
        ls = self.larguras()
        pos = larg_mon / 2 - (sum(ls) + ESPACO * max(0, len(ls) - 1)) / 2
        y = m["y"] + alt_mon - MARGEM - self.altura_barra / 2
        saida = {}
        for item, w in zip(self.itens, ls):
            x = m["x"] + pos + w / 2
            pos += w + ESPACO
            if item.tipo == "app":
                for c in item.janelas:
                    saida[c["address"]] = [round(x), round(y)]
        texto = json.dumps(saida)
        if texto != getattr(self, "_posicoes_publicadas", None):
            self._posicoes_publicadas = texto
            try:
                with open(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aero-dock-icones.json"), "w") as f:
                    f.write(texto)
            except OSError:
                pass

    def marcar_janelas(self):
        """Indicadores: quais itens têm janela aberta e qual está em uso."""
        for item in self.itens:
            if item.tipo == "app":
                item.janelas = sorted(self.abertos.get(item.chave, []), key=lambda c: c.get("focusHistoryID", 99))
                item.ativo = any(c.get("address") == getattr(self, "ativa", None) for c in item.janelas)
                if item.janelas and item.quique_desde is not None and time.monotonic() - item.quique_desde > 0.4:
                    item.quique_desde = None                  # a janela abriu: para de pular

    def conferir_de_novo(self):
        self.vigia_flutuantes = None
        self.atualizar_janelas()          # recria o temporizador se ainda houver flutuantes
        return GLib.SOURCE_REMOVE

    # ---------- cliques ----------

    def ao_clicar(self, gesto, item):
        botao = gesto.get_current_button()
        if botao == 1:
            self.clique_principal(item)
        elif botao == 2:
            self.abrir_app(item)                           # botão do meio = janela nova
        elif botao == 3:
            self.menu_app(item)
        gesto.set_state(Gtk.EventSequenceState.CLAIMED)

    def abrir_app(self, item):
        apps.abrir(item.app)
        item.quique_desde = time.monotonic()
        self.animar()

    def clique_principal(self, item):
        janelas = item.janelas
        if not janelas:
            self.abrir_app(item)
            return
        primeira = janelas[0]
        em_uso = item.ativo and primeira.get("workspace", {}).get("name") != "special:minimized"
        if em_uso and len(janelas) == 1:
            # minimiza com animação até este ícone (o hypr-minimizar lê a posição que publicamos abaixo)
            subprocess.Popen([os.path.expanduser("~/.local/bin/hypr-minimizar"), primeira["address"]])
        elif em_uso:
            self.trazer(janelas[-1])                       # várias janelas: vai pra próxima
        else:
            self.trazer(primeira)

    def trazer(self, c):
        """Mesma receita do Alt+Tab: minimizada volta pra área atual; depois foco e frente."""
        endereco = c["address"]
        if c.get("workspace", {}).get("name") == "special:minimized":
            atual = (self.mon_info or {}).get("activeWorkspace", {}).get("id", 1)
            hypr(f"dispatch movetoworkspacesilent {atual},address:{endereco}")
        hypr(f"dispatch focuswindow address:{endereco}")
        if c.get("floating"):
            hypr(f"dispatch alterzorder top,address:{endereco}")

    # ---------- menus ----------

    def popover(self, ancora, acoes, x=None, y=None, depois_de=None):
        """Menu do botão direito. `acoes` = lista de (texto, função); None = linha separadora.
        O menu tem DUAS páginas (Gtk.Stack): a lista de ações e o "Add App", que mostra os apps
        instalados ali mesmo, dentro do menu (nada de janela separada: pedido do Aero)."""
        pilha = Gtk.Stack(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT,
                          hhomogeneous=False, vhomogeneous=False, interpolate_size=True,
                          transition_duration=180 if self.movimento else 0)
        pop = Gtk.Popover(child=pilha, has_arrow=False, position=Gtk.PositionType.TOP)
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        caixa.add_css_class("menu-dock")
        # "Add App" é sempre o primeiro: é o que mais se usa
        b = Gtk.Button()
        conteudo = Gtk.Box(spacing=8)
        conteudo.append(Gtk.Image(icon_name="list-add-symbolic"))
        conteudo.append(Gtk.Label(label="Add App", xalign=0, hexpand=True))
        conteudo.append(Gtk.Image(icon_name="go-next-symbolic"))
        b.set_child(conteudo)
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self.mostrar_apps(pop, pilha, depois_de))
        caixa.append(b)
        caixa.append(Gtk.Separator(margin_top=4, margin_bottom=4))
        for acao in acoes:
            if acao is None:
                caixa.append(Gtk.Separator(margin_top=4, margin_bottom=4))
                continue
            texto, funcao = acao
            b = Gtk.Button(label=texto)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, fn=funcao: (pop.popdown(), fn()))
            caixa.append(b)
        pilha.add_named(caixa, "menu")
        pop.set_parent(ancora)
        if x is not None:
            ret = Gdk.Rectangle()
            ret.x, ret.y, ret.width, ret.height = int(x), int(y), 1, 1
            pop.set_pointing_to(ret)                    # o menu sai de onde você clicou
        self.menu_aberto = True
        self.decidir()

        janela_antes = hypr_json("activewindow", {}).get("address")

        def fechou(p):
            self.menu_aberto = False
            # devolve o teclado pras janelas (só pegamos pra busca do "Add App") e o foco pra janela
            # em que você estava
            Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
            if janela_antes:
                GLib.timeout_add(80, lambda: (hypr(f"dispatch focuswindow address:{janela_antes}"), False)[1])
            GLib.idle_add(p.unparent)
            self.decidir()
        pop.connect("closed", fechou)
        # A camada do Dock não recebe teclado (pra não roubar o foco das janelas). Com o menu aberto,
        # pedimos o teclado "sob demanda" (ON_DEMAND): a busca do "Add App" precisa dele.
        # Tem que ser ANTES de abrir: trocar com o menu já aberto faz o Hyprland fechar o menu.
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.ON_DEMAND)
        pop.popup()
        return pop

    def mostrar_apps(self, pop, pilha, depois_de):
        """Página "Add App": busca + categorias + lista rolável dos apps instalados.
        Clicou num app (ou Enter na busca) = ele entra no Dock na hora, e o menu fecha."""
        if pilha.get_child_by_name("apps") is None:
            pilha.add_named(self.pagina_apps(pop, pilha, depois_de), "apps")
        pilha.set_visible_child_name("apps")
        GLib.timeout_add(150, lambda: (pilha.get_child_by_name("apps").busca.grab_focus(), False)[1])

    def pagina_apps(self, pop, pilha, depois_de):
        pagina = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        pagina.add_css_class("apps-dock")
        topo = Gtk.Box(spacing=4)
        voltar = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Back")
        voltar.add_css_class("flat")
        voltar.connect("clicked", lambda *_: pilha.set_visible_child_name("menu"))
        topo.append(voltar)
        busca = Gtk.SearchEntry(placeholder_text="Search apps", hexpand=True)
        topo.append(busca)
        pagina.append(topo)
        pagina.busca = busca

        # Categorias como "chips" pequenos numa faixa que rola de lado
        estado = {"categoria": "todos", "adicionados": 0}
        chips = Gtk.Box(spacing=4)
        primeiro = None
        for chave, nome, _grupo in apps.CATEGORIAS:
            chip = Gtk.ToggleButton(label=nome, active=chave == "todos")
            chip.add_css_class("chip")
            if primeiro is None:
                primeiro = chip
            else:
                chip.set_group(primeiro)               # só um marcado por vez
            chip.connect("toggled", lambda c, k=chave: c.get_active() and (estado.update(categoria=k),
                                                                           lista.invalidate_filter()))
            chips.append(chip)
        faixa_chips = Gtk.ScrolledWindow(child=chips, vscrollbar_policy=Gtk.PolicyType.NEVER,
                                         hscrollbar_policy=Gtk.PolicyType.EXTERNAL)
        pagina.append(faixa_chips)

        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        lista.add_css_class("lista-apps")
        no_dock = {r.get("id") for r in self.registros() if r.get("tipo") == "app"}
        for app in apps.todos():
            linha_caixa = Gtk.Box(spacing=10)
            linha_caixa.append(Gtk.Image(gicon=app.get_icon() or Gio.ThemedIcon.new("application-x-executable"),
                                         pixel_size=24))
            linha_caixa.append(Gtk.Label(label=app.get_display_name(), xalign=0, hexpand=True,
                                         ellipsize=3, use_markup=False))
            marca = Gtk.Image(icon_name="object-select-symbolic", visible=app.get_id() in no_dock)
            linha_caixa.append(marca)
            linha = Gtk.ListBoxRow(child=linha_caixa)
            linha.app, linha.marca = app, marca
            linha.set_sensitive(app.get_id() not in no_dock)       # já está no Dock: aparece marcado
            lista.append(linha)

        def filtro(linha):
            return apps.combina(linha.app, busca.get_text()) and apps.na_categoria(linha.app, estado["categoria"])
        lista.set_filter_func(filtro)
        busca.connect("search-changed", lambda *_: lista.invalidate_filter())

        def adicionar(linha):
            if not linha.get_sensitive():
                return
            lista_dock = self.registros()
            if depois_de is not None:
                posicao = self.indice_de(depois_de, lista_dock) + 1 + estado["adicionados"]
            else:
                posicao = len(lista_dock)
            lista_dock.insert(min(posicao, len(lista_dock)), {"tipo": "app", "id": linha.app.get_id()})
            apps.gravar_lista("dock", lista_dock)                  # o vigia do dock.json remonta o Dock
            estado["adicionados"] += 1
            linha.marca.set_visible(True)
            linha.set_sensitive(False)
            pop.popdown()
        lista.connect("row-activated", lambda _l, linha: adicionar(linha))

        def primeiro_visivel(*_):                                  # Enter na busca = adiciona o 1º
            linha = lista.get_first_child()
            while linha is not None:
                if linha.get_child_visible() and linha.get_sensitive():
                    adicionar(linha)
                    return
                linha = linha.get_next_sibling()
        busca.connect("activate", primeiro_visivel)
        busca.connect("stop-search", lambda *_: pop.popdown())     # Esc fecha

        rolagem = Gtk.ScrolledWindow(child=lista, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     min_content_height=340, max_content_height=340, propagate_natural_height=True)
        rolagem.set_size_request(300, -1)
        pagina.append(rolagem)
        return pagina

    def menu_app(self, item):
        acoes = []
        for nome in item.app.list_actions():                # ações do próprio app ("New Private Window"...)
            acoes.append((item.app.get_action_name(nome), lambda n=nome: self.acao_do_app(item, n)))
        acoes.append(("New Window", lambda: self.abrir_app(item)))
        if item.janelas:
            acoes.append(None)
            texto = "Close Window" if len(item.janelas) == 1 else f"Close All Windows ({len(item.janelas)})"
            acoes.append((texto, lambda: [hypr(f"dispatch closewindow address:{c['address']}") for c in item.janelas]))
        acoes.append(None)
        if item.fixado:
            acoes.append(("Remove from Atlas", lambda: self.remover(item)))
        else:
            acoes.append(("Keep in Atlas", lambda: self.fixar(item)))
        acoes += self.acoes_gerais(item if item.fixado else None)
        self.popover(item.widget, acoes, depois_de=item if item.fixado else None)

    def ao_clicar_faixa(self, _gesto, _n, x, y):
        for item in self.itens:
            if item.tipo == "app" and item.x <= x <= item.x + self.tam_max * item.escala:
                return                                    # é um ícone: o menu dele cuida
        alvo = None
        for item in self.itens:
            if item.tipo != "app" and item.fixado and abs(x - (item.x if item.tipo == "separador" else item.x + self.tam * item.escala / 2)) < self.tam / 3:
                alvo = item
        self.menu_dock(self.faixa, alvo, x, y)

    def menu_dock(self, ancora, item=None, x=None, y=None):
        acoes = []
        if item is not None:
            acoes.append(("Remove", lambda: self.remover(item)))
        acoes += self.acoes_gerais(item)
        # Sem "Hide Dock": o Dock é parte do sistema e não se fecha (pedido do Aero).
        self.popover(ancora, acoes, x, y, depois_de=item)

    def acoes_gerais(self, depois_de=None):
        return [("Add Separator", lambda: self.inserir({"tipo": "separador"}, depois_de)),
                ("Add Space", lambda: self.inserir({"tipo": "espaco"}, depois_de)),
                None,
                ("Atlas Settings…", lambda: subprocess.Popen(
                    ["hyprctl", "dispatch", "exec", "env CONFIG_ABA=dock " + os.path.expanduser("~/.local/bin/configuracoes")],
                    stdout=subprocess.DEVNULL))]

    def acao_do_app(self, item, nome):
        """Ação do .desktop (ex: Firefox → "New Private Window"). Roda pelo Hyprland, como o abrir."""
        chave = GLib.KeyFile()
        try:
            chave.load_from_file(item.app.get_filename(), GLib.KeyFileFlags.NONE)
            comando = chave.get_string(f"Desktop Action {nome}", "Exec")
        except GLib.Error:
            return
        comando = re.sub(r"%[a-zA-Z]", "", comando).strip()     # tira os %u, %F... (não há arquivo)
        subprocess.Popen(["hyprctl", "dispatch", "exec", comando], stdout=subprocess.DEVNULL)

    # ---------- mudar a lista (grava no dock.json; o vigia remonta) ----------

    def registros(self):
        return apps.ler_lista("dock", apps.DOCK_PADRAO)

    def indice_de(self, item, lista):
        """Posição do item no dock.json (guardada quando o item foi lido)."""
        i = getattr(item, "indice", None)
        return i if i is not None and i < len(lista) else len(lista) - 1

    def remover(self, item):
        lista = self.registros()
        i = self.indice_de(item, lista)
        if 0 <= i < len(lista):
            del lista[i]
            apps.gravar_lista("dock", lista)

    def fixar(self, item):
        lista = self.registros()
        lista.append({"tipo": "app", "id": item.chave})
        apps.gravar_lista("dock", lista)

    def inserir(self, registro, depois_de=None):
        lista = self.registros()
        i = self.indice_de(depois_de, lista) + 1 if depois_de is not None else len(lista)
        lista.insert(i, registro)
        apps.gravar_lista("dock", lista)


# ---------- o programa ----------

CSS_MODELO = """
window.dock { background: transparent; backdrop-filter: none; box-shadow: none; }
/* Sem círculo atrás dos ícones (pedido do Aero): a "bolha" é só a área clicável, invisível.
   Ela continua do tamanho do ícone ampliado, então a onda e o clique acertam igual. */
.bolha { background: none; box-shadow: none; }
.menu-dock button { padding: 6px 14px; border-radius: 8px; font-weight: normal; }
.menu-dock button label { min-width: 170px; }
.apps-dock { margin: 4px; }
.apps-dock .chip { padding: 1px 9px; min-height: 24px; border-radius: 99px; font-size: 12px;
                   background: alpha(%(texto)s, 0.06); }
.apps-dock .chip:checked { background: var(--accent-bg-color, #3584e4); color: var(--accent-fg-color, white); }
.apps-dock .lista-apps { background: transparent; }
.apps-dock .lista-apps row { border-radius: 8px; padding: 5px 8px; }
"""


class App(Adw.Application):    # Adw = menus acompanham o claro/escuro (doc 35)
    def __init__(self):
        super().__init__(application_id="com.aero.Dock", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.dock = None
        self.provedor = None

    def do_command_line(self, linha):
        """Roda na instância que já está aberta. "dock" inicia (se já estiver rodando, nada muda);
        "--reiniciar" fecha, e o lançador (~/.local/bin/dock) abre de novo na hora.
        Não existe "desligar o Dock": ele é parte do sistema (pedido do Aero)."""
        argumentos = linha.get_arguments()[1:]
        if "--reiniciar" in argumentos:
            self.quit()
            return 0
        if self.dock is None:
            try:
                self.iniciar()
            except Exception:
                # Deu erro no meio da montagem? Fecha DE VEZ em vez de ficar vivo "pela metade" (sem
                # Dock na tela, mas segurando o nome com.aero.Dock e barrando o próximo Dock).
                # Erro aqui dentro NÃO fecha o programa sozinho: o GTK só mostra o erro e segue.
                self.quit()
                raise                 # o erro continua indo pro log (dock.log)
        return 0

    def iniciar(self):
        self.hold()
        carregar_dicas_dotstyle()
        self.provedor = Gtk.CssProvider()
        # USER+1: vence o gtk.css do usuário, que liga o vidro em toda janela (doc 16)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), self.provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.dock = Dock(self)
        self.aplicar_cores()
        self.dock.present()
        # Vigias: lista do Dock, opções e cores mudaram -> aplica na hora
        self.vigias = []
        self.vigiar(os.path.dirname(os.path.join(apps.CONFIG, "dock.json")), "dock.json", self.ao_mudar_lista)
        self.vigiar(os.path.dirname(PAINEL), "painel.conf", self.ao_mudar_opcoes)
        self.vigiar(os.path.dirname(CORES_BARRA), None, self.ao_mudar_cores)
        threading.Thread(target=self.ouvir_hyprland, daemon=True).start()

    def vigiar(self, pasta, nome, funcao):
        os.makedirs(pasta, exist_ok=True)
        vigia = Gio.File.new_for_path(pasta).monitor_directory(Gio.FileMonitorFlags.WATCH_MOVES, None)
        espera = {"id": None}

        def mudou(_v, arquivo, outro, _evento):
            nomes = {arquivo.get_basename() if arquivo else "", outro.get_basename() if outro else ""}
            if nome is None or nome in nomes:
                if espera["id"]:
                    GLib.source_remove(espera["id"])
                # junta vários avisos seguidos numa coisa só
                espera["id"] = GLib.timeout_add(120, lambda: (espera.update(id=None), funcao(), False)[2])
        vigia.connect("changed", mudou)
        self.vigias.append(vigia)

    def ao_mudar_lista(self):
        self.dock.montar_itens()

    def ao_mudar_opcoes(self):
        novas = ler_opcoes()
        if novas == self.dock.opcoes:
            return
        tamanho_mudou = any(novas[k] != self.dock.opcoes[k] for k in ("DOCK_TAMANHO", "DOCK_ONDA", "DOCK_MOVIMENTO"))
        self.dock.opcoes = novas
        self.dock.medidas()
        if tamanho_mudou:
            for item in self.dock.itens:                    # ícones refeitos no tamanho novo
                if item.widget is not None:
                    item.widget.unparent()
                    item.widget = None
            self.dock.faixa.queue_resize()
        self.dock.montar_itens()
        self.dock.atualizar_janelas()

    def ao_mudar_cores(self):
        self.aplicar_cores()

    def aplicar_cores(self):
        self.dock.cores = ler_cores()
        r, g, b, _a = self.dock.cores["texto"]
        self.provedor.load_from_string(CSS_MODELO % {"texto": f"rgb({r * 255:.0f},{g * 255:.0f},{b * 255:.0f})"})
        self.dock.animar()

    def ouvir_hyprland(self):
        """Thread que escuta os eventos do Hyprland (socket2) e pede pro Dock reconferir as janelas."""
        interessa = {"openwindow", "closewindow", "movewindow", "movewindowv2", "activewindowv2",
                     "fullscreen", "changefloatingmode", "workspace", "workspacev2", "focusedmon",
                     "focusedmonv2", "activespecial", "activespecialv2", "minimized", "configreloaded",
                     "monitoradded", "monitorremoved", "togglegroup"}
        pendente = {"sim": False}

        def no_gtk():
            pendente["sim"] = False
            if self.dock is not None:
                self.dock.atualizar_janelas()
            return GLib.SOURCE_REMOVE

        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(os.path.join(HYPR, ".socket2.sock"))
                    for linha in s.makefile("r", encoding="utf-8", errors="replace"):
                        evento = linha.split(">>", 1)[0]
                        if evento in interessa and not pendente["sim"]:
                            pendente["sim"] = True
                            # 60 ms: vários eventos de uma vez (abrir uma janela gera uns 4) viram 1 conferida
                            GLib.timeout_add(60, no_gtk)
            except OSError as erro:
                print("socket2:", erro, flush=True)
            time.sleep(2)                                   # caiu (reload?): tenta de novo


if __name__ == "__main__":
    # Antes de abrir: tem tela? E o Hyprland em que este Dock nasceu ainda está vivo? Se não, sai COM
    # ERRO (código 1) antes de pegar o nome com.aero.Dock no D-Bus. Foi o que faltou quando o Hyprland
    # caiu: o lançador abriu o Dock de novo sem tela, ele ficou vivo "pela metade" segurando esse
    # nome, e o Dock do login novo achou que já tinha um Dock aberto (e não abriu).
    if Gdk.Display.get_default() is None:
        print(time.strftime("%F %T"), "sem tela (o Hyprland caiu?): o Dock não abre", file=sys.stderr, flush=True)
        sys.exit(1)
    if not hyprland_vivo():
        print(time.strftime("%F %T"), "o Hyprland desta sessão acabou: o Dock não abre", file=sys.stderr, flush=True)
        sys.exit(1)
    sys.exit(App().run(sys.argv))
