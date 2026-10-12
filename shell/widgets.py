#!/usr/bin/env python3
"""
widgets.py — os widgets da área de trabalho (doc 51): Clock, System, Resources, Weather e Pokémon battle.

Como funciona:
- Cada widget é uma CAMADA (layer-shell) própria no nível "bottom": fica entre o papel de parede e as
  janelas, como um widget de desktop clássico. A posição é a margem da camada (x, y em pixels da tela).
  O Hyprland desfoca o fundo dela (layerrule blur, namespace "aero-widget"): o mesmo vidro da barra.
- Uma camada transparente do tamanho da tela ("aero-desktop"), ABAIXO dos widgets, recebe o botão
  direito na área de trabalho (menu: adicionar widget, arrumar, Settings, wallpaper, terminal...) e, no
  modo "arrumar", desenha as molduras dos widgets e cuida do arrastar (a camada não se mexe, então as
  coordenadas do arraste são estáveis; os widgets de verdade acompanham ao vivo).
- Configuração: ~/.config/aero-shell/widgets.json (lista de widgets, posição, opções). O daemon VIGIA o
  arquivo e aplica na hora — é como o Settings → Widgets e o menu do desktop falam com ele.
- Consumo: nada roda "por rodar". Cada widget só acorda no seu próprio ritmo (relógio 1x por minuto,
  recursos a cada 2 s lendo /proc, clima a cada 30 min...) e as animações só existem enquanto acontecem.
- Tema: cores do libadwaita (var(--window-bg-color) etc., escritas pelo hypr-tema), claro/escuro ao vivo.
  Os desenhos (cairo) pegam a cor do texto do próprio widget na hora de desenhar.

Uso: widgets [--daemon] | --editar | --adicionar TIPO | --recarregar     (lançador: ~/.local/bin/widgets)
"""
import json
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
gi.require_version("Gdk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

CONFIG = os.path.expanduser("~/.config/aero-shell/widgets.json")
HYPR = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "hypr",
                    os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""))
RAIO = 14                 # cantos do cartão (= o arredondamento das janelas); molduras internas usam 10
SOMBRA = 14               # margem do cartão dentro da janela: é onde a sombra é desenhada
LARGURA_ESTREITA = 320    # largura padrão do conteúdo da coluna estreita (System, Resources, Processes)
LARGURA_LARGA = 518       # ...e da coluna larga (Clock, batalha, Weather)
CORES_BARRA = os.path.expanduser("~/.config/waybar/cores.css")   # cores do tema (o hypr-tema gera)
MARGEM_TELA = 12          # (reserva; o que vale é a moldura das janelas: gaps_out do Hyprland, lida em moldura())
GRADE = 8                 # o arraste "gruda" numa grade de 8 px
BARRA = 42                # altura reservada pela barra de cima (reserved do monitor)
ESCALAS = {"0.85": 85, "1": 100, "1.15": 115, "1.3": 130}
DEPURAR = bool(os.environ.get("WIDGETS_DEPURAR"))   # WIDGETS_DEPURAR=1: registra cliques e menus no log


def depurar(*a):
    if DEPURAR:
        print("[depurar]", *a, file=sys.stderr, flush=True)

MONO = '"Adwaita Mono", "JetBrainsMono Nerd Font", monospace'      # a mesma do kitty/TerMusic
MONO_FAMILIA = "Adwaita Mono"                                         # a mesma, pro Pango (desenhos cairo)

CSS = """
window.aero-desktop, window.aero-widget { background: transparent; }
/* Cartão: superfície SÓLIDA escura e chata (estilo dashboard eww), cantos 18, sombra leve — pintada uma vez
   numa textura (vidro_textura). Dentro, sub-cartões um pouco mais claros (.painel). Conteúdo num Palco fixo. */
.cartao { margin: 14px; padding: 14px; background: transparent; }
.cartao label { color: var(--window-fg-color, @window_fg_color); }
.brilho { margin: 14px; border-radius: 14px; background-color: alpha(currentColor, 0); transition: background-color 300ms ease; }
.moldura:hover .brilho { background-color: alpha(currentColor, 0.02); }
/* terminal simulado: molduras de linha fina (como as caixas do TerMusic), quase sem preenchimento */
.painel { background-color: alpha(currentColor, 0.025); border-radius: 0; padding: 10px 12px;
          box-shadow: inset 0 0 0 1px alpha(currentColor, 0.16); }
.prompt { font-family: MONO; font-size: 11px; }
.cursor { font-family: MONO; font-size: 11px; opacity: 0.8; }
.mono { font-family: MONO; font-variant-numeric: tabular-nums; }
.chave { font-family: MONO; font-size: 10px; font-weight: 700; opacity: 0.5; letter-spacing: 0.12em; }
.dim { opacity: 0.6; }
.apagado { opacity: 0.4; }
.pequeno { font-size: 11px; }
.negrito { font-weight: 700; }
.hora { font-family: MONO; font-size: 40px; font-weight: 700; letter-spacing: -0.02em; font-variant-numeric: tabular-nums; }
.hora-dim { opacity: 0.55; font-weight: 500; }
/* acentos pastel (os da referência): rosa, azul, pêssego, verde, lilás, amarelo */
.rosa { color: #e8938e; } .azul { color: #a6c8ec; } .pessego { color: #f2b68e; } .verde { color: #b9d6a0; }
.lilas { color: #c9a7eb; } .amarelo { color: #efd49a; }
.chip { font-family: MONO; font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 6px; letter-spacing: 0.08em; }
.escala-85 { font-size: 85%; }  .escala-115 { font-size: 115%; }  .escala-130 { font-size: 130%; }
.pilula { background-color: var(--popover-bg-color, @popover_bg_color); border-radius: 99px; padding: 6px 10px 6px 16px;
          box-shadow: 0 6px 24px rgba(0,0,0,0.25), inset 0 0 0 1px alpha(currentColor, 0.12); }
.pilula label { font-weight: 600; }
""".replace("MONO;", MONO + ";")

# a paleta pastel, pros desenhos cairo (as mesmas cores das classes CSS acima)
PASTEL = {"rosa": (0.91, 0.58, 0.56), "azul": (0.65, 0.78, 0.93), "pessego": (0.95, 0.71, 0.56),
          "verde": (0.73, 0.84, 0.63), "lilas": (0.79, 0.65, 0.92), "amarelo": (0.94, 0.83, 0.60)}

TIPOS = {}        # tipo -> classe do widget (os módulos widget_*.py se registram aqui)


def registrar(classe):
    TIPOS[classe.tipo] = classe
    return classe


# ---------- Hyprland (socket direto, sem abrir programa) ----------

def hypr(comando):
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(os.path.join(HYPR, ".socket.sock"))
            s.sendall(comando.encode())
            partes = []
            while (bloco := s.recv(65536)):
                partes.append(bloco)
        return b"".join(partes).decode("utf-8", "replace")
    except OSError:
        return ""


def wallpaper_padrao():
    """O wallpaper "normal" do sistema (o que o papel-de-parede gravou no hyprpaper.conf)."""
    try:
        m = re.search(r"^\s*path\s*=\s*(.+)$", open(os.path.expanduser("~/.config/hypr/hyprpaper.conf")).read(), re.M)
        return os.path.expanduser(m.group(1).strip()) if m else None
    except OSError:
        return None


def hypr_json(comando, padrao):
    try:
        return json.loads(hypr("j/" + comando) or "null") or padrao
    except ValueError:
        return padrao


def hyprland_vivo():
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1.0)
            s.connect(os.path.join(HYPR, ".socket.sock"))
        return True
    except (ConnectionRefusedError, FileNotFoundError):
        return False
    except OSError:
        return True


def gaps_hyprland():
    """(gaps_out, gaps_in) do Hyprland — os mesmos espaços das janelas. gaps_out como [cima, direita, baixo, esquerda]."""
    def ler(nome, padrao):
        txt = hypr(f"getoption general:{nome}")
        m = re.search(r"custom type:\s*([\d\s]+)", txt)
        if not m:
            return [padrao] * 4
        v = [int(x) for x in m.group(1).split()]
        return (v * 4)[:4] if len(v) in (1, 2, 4) else [padrao] * 4
    return ler("gaps_out", 12), ler("gaps_in", 6)


def moldura(mon=None):
    """A moldura em que as janelas vivem: tela menos a área reservada (barra) menos gaps_out.
    Devolve (x0, y0, x1, y1, espaco): os widgets ficam SEMPRE dentro dela, e `espaco` (2×gaps_in, como entre
    duas janelas) é a distância entre um widget e outro."""
    mon = mon or monitor_focado()
    fora, dentro = gaps_hyprland()
    res = mon["reservado"]                  # [esquerda, cima, direita, baixo]
    x0 = res[0] + fora[3]
    y0 = res[1] + fora[0]
    x1 = mon["w"] - res[2] - fora[1]
    y1 = mon["h"] - res[3] - fora[2]
    return x0, y0, x1, y1, 2 * dentro[0]


def monitor_focado():
    ms = hypr_json("monitors", [])
    m = next((m for m in ms if m.get("focused")), ms[0] if ms else None)
    if not m:
        return {"x": 0, "y": 0, "w": 2560, "h": 1440, "reservado": [0, BARRA, 0, 0]}
    esc = m.get("scale", 1) or 1
    w, h = m["width"] / esc, m["height"] / esc
    if m.get("transform", 0) % 2 == 1:
        w, h = h, w
    return {"x": m["x"], "y": m["y"], "w": int(w), "h": int(h), "reservado": m.get("reserved", [0, BARRA, 0, 0])}


# ---------- configuração ----------

def cartao_de(tipo):
    """Tamanho do cartão (conteúdo + padding) de um tipo, na escala 1."""
    w, h = TIPOS[tipo].tamanho if tipo in TIPOS else (240, 100)
    return w + 28, h + 28


def layout_padrao(mon):
    """Primeira vez: à esquerda System, Resources e Processes; à direita Clock, a batalha e Weather — colados na
    moldura das janelas (gaps_out da tela e da barra) e com o espaço de duas janelas entre eles."""
    x0, y0, x1, _, g = moldura(mon)
    lista, ye, yd = [], y0, y0
    for tipo in ("sistema", "recursos", "processos"):
        w, h = cartao_de(tipo)
        lista.append({"id": f"{tipo}-1", "tipo": tipo, "x": x0, "y": ye, "ativo": True, "opcoes": {}})
        ye += h + g
    for tipo in ("relogio", "batalha", "clima"):
        w, h = cartao_de(tipo)
        lista.append({"id": f"{tipo}-1", "tipo": tipo, "x": x1 - w, "y": yd, "ativo": True, "opcoes": {}})
        yd += h + g
    return lista


class Config:
    def __init__(self):
        self.d = {"versao": 1, "editar": False, "escala": "1", "menos_movimento": False, "vidro": "0.62", "widgets": None}
        self.ler()

    def ler(self):
        try:
            with open(CONFIG) as f:
                novo = json.load(f)
            if isinstance(novo, dict):
                self.d.update(novo)
        except (OSError, ValueError):
            pass
        if self.d.get("widgets") is None:
            self.d["widgets"] = layout_padrao(monitor_focado())
            self.gravar()

    def gravar(self):
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG + ".tmp", "w") as f:
            json.dump(self.d, f, indent=2)
        os.replace(CONFIG + ".tmp", CONFIG)

    @property
    def widgets(self):
        return self.d["widgets"]

    def por_id(self, wid):
        return next((w for w in self.widgets if w["id"] == wid), None)

    def novo_id(self, tipo):
        n = 1
        while self.por_id(f"{tipo}-{n}"):
            n += 1
        return f"{tipo}-{n}"


# ---------- desenho compartilhado (cairo) ----------

def cor_texto(widget, alfa=1.0):
    c = widget.get_color()
    return (c.red, c.green, c.blue, c.alpha * alfa)


def retangulo_redondo(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -1.5708, 0)
    cr.arc(x + w - r, y + h - r, r, 0, 1.5708)
    cr.arc(x + r, y + h - r, r, 1.5708, 3.1416)
    cr.arc(x + r, y + r, r, 3.1416, 4.7124)
    cr.close_path()


_layouts = {}        # cache de layouts Pango: o caro é "montar" o texto (shaping), não pintar; textos repetem muito


def texto(cr, widget, s, x, y, tamanho=11, peso="normal", alfa=1.0, alinhar="esquerda", familia=None):
    """Escreve `s` com a fonte do sistema (Pango). Devolve a largura."""
    chave = (id(widget), s, round(tamanho, 2), peso, familia)
    layout = _layouts.get(chave)
    if layout is None:
        layout = PangoCairo.create_layout(cr)
        fd = widget.get_pango_context().get_font_description().copy()
        if familia:
            fd.set_family(familia)
        fd.set_absolute_size(tamanho * Pango.SCALE)
        fd.set_weight({"normal": Pango.Weight.NORMAL, "bold": Pango.Weight.BOLD, "light": Pango.Weight.LIGHT,
                       "medium": Pango.Weight.MEDIUM}[peso])
        layout.set_font_description(fd)
        layout.set_text(s, -1)
        if len(_layouts) > 600:                  # textos que mudam o tempo todo (taxas, %): não deixa crescer
            for k in list(_layouts)[:200]:
                del _layouts[k]
        _layouts[chave] = layout
    else:
        PangoCairo.update_layout(cr, layout)
    _, log = layout.get_pixel_extents()
    dx = {"esquerda": 0, "centro": -log.width / 2, "direita": -log.width}[alinhar]
    cr.set_source_rgba(*cor_texto(widget, alfa))
    cr.move_to(x + dx, y)
    PangoCairo.show_layout(cr, layout)
    return log.width


# ---------- animação leve ----------

class Tween:
    """Chama passo(v), v de 0 a 1 ao longo de `ms`, a ~60 fps por GLib (não pelo relógio de 144 Hz da tela:
    metade dos quadros, mesma suavidade). Só existe enquanto dura — custo zero em repouso."""
    EASINGS = {"out": lambda t: 1 - (1 - t) ** 3, "in": lambda t: t ** 3, "linear": lambda t: t,
               "inout": lambda t: 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2}

    def __init__(self, ms, passo, easing="out", ao_terminar=None, fps=60):
        self.ms, self.passo, self.ao_terminar = max(1, ms), passo, ao_terminar
        self.f = self.EASINGS[easing]
        self.inicio = time.monotonic()
        self.fonte = GLib.timeout_add(int(1000 / fps), self._tick)

    def _tick(self):
        t = min(1.0, (time.monotonic() - self.inicio) * 1000 / self.ms)
        self.passo(self.f(t))
        if t >= 1.0:
            self.fonte = None
            if self.ao_terminar:
                self.ao_terminar()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def cancelar(self):
        if self.fonte:
            GLib.source_remove(self.fonte)
            self.fonte = None


_cores = {"quando": 0, "valores": None}


def cores_tema():
    """Cores do tema lidas do cores.css da waybar (o hypr-tema gera; o Dock faz igual): fundo, fundo_solido,
    texto, destaque — como (r, g, b, a). Relidas quando o arquivo muda."""
    try:
        mtime = os.path.getmtime(CORES_BARRA)
    except OSError:
        mtime = 0
    if _cores["valores"] is None or mtime != _cores["quando"]:
        escuro = Adw.StyleManager.get_default().get_dark()
        v = {"fundo_solido": (0.118, 0.118, 0.118, 1.0) if escuro else (0.965, 0.965, 0.965, 1.0),
             "texto": (0.94, 0.94, 0.94, 1.0) if escuro else (0.118, 0.118, 0.118, 1.0),
             "destaque": (0.94, 0.94, 0.94, 1.0) if escuro else (0.118, 0.118, 0.118, 1.0)}
        try:
            with open(CORES_BARRA) as f:
                css = f.read()
            for nome in ("fundo_solido", "texto", "destaque"):
                m = re.search(r"@define-color\s+" + nome.replace("_", "-") + r"\s+([^;]+);", css)
                if m:
                    rgba = Gdk.RGBA()
                    if rgba.parse(m.group(1).strip()):
                        v[nome] = (rgba.red, rgba.green, rgba.blue, rgba.alpha)
        except OSError:
            pass
        _cores["valores"], _cores["quando"] = v, mtime
    return _cores["valores"]


def vidro_textura(w, h, cor_texto_rgba, opacidade=0.62):
    """A superfície do cartão, pintada com cairo numa textura w×h (já contando SOMBRA px em volta).
    O padrão é o vidro das JANELAS do sistema (TerMusic inativo, ez-fm): a cor do tema a ~62 % + o desfoque do
    Hyprland atrás, chato, com um fio de borda claro (como o ez-fm) e sombra leve. Settings → Widgets →
    Surface muda a opacidade; abaixo de 0,5 ganha brilho e borda refrativa (modo "liquid")."""
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    cr = cairo.Context(surf)
    x0, y0, cw, ch = SOMBRA, SOMBRA, w - 2 * SOMBRA, h - 2 * SOMBRA
    r = RAIO
    escuro = Adw.StyleManager.get_default().get_dark()
    chato = opacidade >= 0.5
    for i in range(10):                              # sombra: soma ≤ 0,16 (abaixo do ignore_alpha do Hyprland)
        t = i / 10
        exp = SOMBRA * (1 - t)
        retangulo_redondo(cr, x0 - exp, y0 - exp + 4, cw + 2 * exp, ch + 2 * exp, r + exp)
        cr.set_source_rgba(0, 0, 0, 0.012 + 0.008 * t)
        cr.fill()
    fundo = cores_tema()["fundo_solido"]
    retangulo_redondo(cr, x0, y0, cw, ch, r)
    cr.set_source_rgba(*fundo[:3], opacidade if escuro else min(0.96, opacidade + 0.12))
    cr.fill()
    if not chato:
        cr.save()
        retangulo_redondo(cr, x0, y0, cw, ch, r)
        cr.clip()
        esp = cairo.RadialGradient(x0 + cw * 0.18, y0 - ch * 0.1, 0, x0 + cw * 0.18, y0 - ch * 0.1, cw * 0.75)
        esp.add_color_stop_rgba(0, 1, 1, 1, 0.16)
        esp.add_color_stop_rgba(1, 1, 1, 1, 0.0)
        cr.set_source(esp)
        cr.paint()
        base = cairo.LinearGradient(0, y0 + ch * 0.55, 0, y0 + ch)
        base.add_color_stop_rgba(0, 0, 0, 0, 0.0)
        base.add_color_stop_rgba(1, 0, 0, 0, 0.16 if escuro else 0.08)
        cr.set_source(base)
        cr.paint()
        retangulo_redondo(cr, x0 + 1.25, y0 + 1.25, cw - 2.5, ch - 2.5, r - 1.25)
        rim = cairo.LinearGradient(x0, y0, x0 + cw, y0 + ch)
        rim.add_color_stop_rgba(0, 1, 1, 1, 0.42)
        rim.add_color_stop_rgba(0.5, 1, 1, 1, 0.10)
        rim.add_color_stop_rgba(1, 1, 1, 1, 0.22)
        cr.set_source(rim)
        cr.set_line_width(1.5)
        cr.stroke()
        cr.restore()
    retangulo_redondo(cr, x0 + 0.5, y0 + 0.5, cw - 1, ch - 1, r)      # contorno visível, como a borda das caixas do TerMusic
    cr.set_source_rgba(cor_texto_rgba.red, cor_texto_rgba.green, cor_texto_rgba.blue, 0.16 if chato else 0.0)
    cr.set_line_width(1)
    cr.stroke()
    surf.flush()
    return Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(bytes(surf.get_data())), surf.get_stride())


def painel_cairo(cr, x, y, w, h, r=10, titulo=None, area=None, e=1.0):
    """Moldura de terminal nos desenhos cairo: linha fina de contorno (quase sem preenchimento) e, se houver
    `titulo`, ele fica EMBUTIDO na linha de cima, como numa caixa de TUI (─ cpu ──────). `area` = o widget
    (pra fonte e cor do título)."""
    c = cores_tema()["texto"]
    r = 0                                        # molduras internas RETAS (só o cartão de fora é arredondado)
    cr.rectangle(x, y, w, h)
    cr.set_source_rgba(*c[:3], 0.025)
    cr.fill()
    cr.rectangle(x + 0.5, y + 0.5, w - 1, h - 1)
    cr.set_source_rgba(*c[:3], 0.16)
    cr.set_line_width(1)
    cr.stroke()
    if titulo and area is not None:
        tx, ty, tam = x + 14 * e, y - 6.5 * e, 9.5 * e
        larg = texto(cr, area, titulo, tx, ty, tam, "bold", 0.0, familia=MONO_FAMILIA)   # mede
        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)                 # abre a "janela" na linha pro título
        cr.rectangle(tx - 5 * e, y - 2, larg + 10 * e, 4)
        cr.fill()
        cr.restore()
        texto(cr, area, titulo, tx, ty, tam, "bold", 0.6, familia=MONO_FAMILIA)


def tracejado(cr, x, y, w, e=1.0, alfa=0.22):
    """Linha tracejada (- - - -), como os separadores do TerMusic."""
    c = cores_tema()["texto"]
    cr.save()
    cr.set_source_rgba(*c[:3], alfa)
    cr.set_line_width(1)
    cr.set_dash([5 * e, 4 * e])
    cr.move_to(x, y + 0.5)
    cr.line_to(x + w, y + 0.5)
    cr.stroke()
    cr.restore()


class Quadro(Gtk.Overlay):
    """Moldura de terminal com widgets GTK dentro: um DrawingArea desenha a linha (com o título embutido na
    borda de cima) e o conteúdo fica por cima, com margem. append() adiciona ao conteúdo."""

    def __init__(self, titulo=None, e=1.0, raio=10, espaco=4):
        super().__init__()
        self.titulo, self.e, self.raio = titulo, e, raio
        self.fundo = Gtk.DrawingArea()
        self.fundo.set_draw_func(self._desenhar)
        self.set_child(self.fundo)
        self.conteudo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=espaco * e,
                                margin_top=int((15 if titulo else 10) * e), margin_bottom=int(10 * e),
                                margin_start=int(12 * e), margin_end=int(12 * e))
        self.add_overlay(self.conteudo)
        self.set_measure_overlay(self.conteudo, True)

    def append(self, w):
        self.conteudo.append(w)

    def _desenhar(self, area, cr, w, h):
        topo = 7 * self.e if self.titulo else 0
        painel_cairo(cr, 0, topo, w, h - topo, self.raio * self.e, self.titulo, area, self.e)


def anel(cr, cx, cy, r, fracao, cor, espessura=7.0):
    """Anel de progresso (gauge): trilho apagado + arco na cor, começando em cima, no sentido horário."""
    import math
    cr.set_line_width(espessura)
    cr.set_line_cap(1)
    cr.new_sub_path()                 # senão o cairo liga o fim do arco anterior ao começo deste
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.set_source_rgba(*cor[:3], 0.12)
    cr.stroke()
    f = max(0.0, min(1.0, fracao))
    if f > 0.005:
        cr.new_sub_path()
        cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * f)
        cr.set_source_rgba(*cor[:3], 1.0)
        cr.stroke()


def barra_vertical(cr, x, y, w, h, fracao, cor):
    """Barra vertical de pontas redondas: trilho apagado + parte cheia (de baixo pra cima)."""
    retangulo_redondo(cr, x, y, w, h, w / 2)
    cr.set_source_rgba(*cor[:3], 0.12)
    cr.fill()
    f = max(0.0, min(1.0, fracao))
    if f > 0.005:
        alt = max(w, h * f)
        retangulo_redondo(cr, x, y + h - alt, w, alt, w / 2)
        cr.set_source_rgba(*cor[:3], 1.0)
        cr.fill()


class PixelArt(Gtk.Widget):
    """Um sprite do pokefetch em escala INTEIRA (filtro NEAREST: pixel art sem borrão). Usado pelo System
    (o Pokémon do dia) e pela batalha. espelhar=True: olha pra direita."""

    SPRITES = os.path.expanduser("~/.local/share/pokefetch/sprites")

    def __init__(self, caixa, espelhar=False):
        super().__init__()
        self.caixa = int(caixa)
        self.espelhar = espelhar
        self.textura = None
        self.dx = self.dy = 0.0
        self.alfa = 1.0
        self.brilho = 0.0
        self.set_size_request(self.caixa, self.caixa)

    def carregar(self, pid):
        try:
            self.textura = Gdk.Texture.new_from_filename(os.path.join(self.SPRITES, f"{int(pid):04d}.png"))
        except GLib.Error:
            self.textura = None
        self.queue_draw()

    def do_measure(self, orientacao, _para):
        return self.caixa, self.caixa, -1, -1

    def do_snapshot(self, snap):
        if self.textura is None:
            return
        w, h = self.textura.get_width(), self.textura.get_height()
        f = next((c for c in range(8, 1, -1) if w % c == 0 and h % c == 0 and 240 // max(w // c, h // c) == c), 1)
        ow, oh = w / f, h / f
        k = max(1, int(self.caixa // max(ow, oh)))
        dw, dh = ow * k, oh * k
        x = round((self.caixa - dw) / 2 + self.dx)
        y = round(self.caixa - dh + self.dy)
        snap.push_opacity(self.alfa)
        if self.brilho > 0.01:
            b = self.brilho * 0.9
            snap.push_color_matrix(Graphene.Matrix().init_identity(), Graphene.Vec4().init(b, b, b, 0))
        if self.espelhar:
            snap.translate(Graphene.Point().init(x + dw, y))
            snap.scale(-1, 1)
            snap.append_scaled_texture(self.textura, Gsk.ScalingFilter.NEAREST, Graphene.Rect().init(0, 0, dw, dh))
        else:
            snap.append_scaled_texture(self.textura, Gsk.ScalingFilter.NEAREST, Graphene.Rect().init(x, y, dw, dh))
        if self.brilho > 0.01:
            snap.pop()
        snap.pop()


class Palco(Gtk.Widget):
    """Um container de tamanho FIXO. Os filhos ficam em (x, y) com o tamanho que eu disser (ou o natural) e o
    que passar da borda é recortado. É o que impede o cartão de "respirar": um rótulo que cresce, um número de
    dano que sobe, uma legenda de 1 ou 2 linhas — nada disso muda o tamanho do widget."""

    def __init__(self, w, h):
        super().__init__()
        self.w, self.h = int(w), int(h)
        self.filhos = []                 # [widget, x, y, w|None, h|None]
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def put(self, filho, x=0, y=0, w=None, h=None):
        filho.set_parent(self)
        self.filhos.append([filho, x, y, w, h])
        self.queue_resize()

    def move(self, filho, x, y):
        for f in self.filhos:
            if f[0] is filho:
                f[1], f[2] = x, y
        self.queue_allocate()

    def remove(self, filho):
        self.filhos = [f for f in self.filhos if f[0] is not filho]
        filho.unparent()

    def limpar(self):
        for f in self.filhos:
            f[0].unparent()
        self.filhos = []

    def redimensionar(self, w, h):
        self.w, self.h = int(w), int(h)
        self.queue_resize()

    def do_measure(self, orientacao, _para):
        return (self.w, self.w, -1, -1) if orientacao == Gtk.Orientation.HORIZONTAL else (self.h, self.h, -1, -1)

    def do_size_allocate(self, _w, _h, _base):
        for filho, x, y, cw, ch in self.filhos:
            if not filho.get_visible():
                continue
            lw = cw if cw is not None else filho.measure(Gtk.Orientation.HORIZONTAL, -1)[1]
            lh = ch if ch is not None else filho.measure(Gtk.Orientation.VERTICAL, int(lw))[1]
            filho.allocate(int(lw), int(lh), -1, Gsk.Transform.new().translate(Graphene.Point().init(int(x), int(y))))

    def do_snapshot(self, snap):
        for f in self.filhos:
            self.snapshot_child(f[0], snap)


def tile_cairo(cr, x, y, w, h, r=16, destaque=False):
    """Um bloco sólido (como a classe CSS .tile) desenhado no cairo: fundo sólido do tema + contorno fino."""
    c = cores_tema()
    retangulo_redondo(cr, x, y, w, h, r)
    cr.set_source_rgba(*c["fundo_solido"][:3], 1.0)
    cr.fill()
    retangulo_redondo(cr, x + 0.5, y + 0.5, w - 1, h - 1, r)
    if destaque:
        cr.set_source_rgba(*c["destaque"][:3], 0.6)
    else:
        cr.set_source_rgba(*c["texto"][:3], 0.08)
    cr.set_line_width(1)
    cr.stroke()


# ---------- a base de todo widget ----------

class Widget(Gtk.Window):
    tipo = ""                 # nome na configuração ("relogio")
    nome = ""                 # nome mostrado ("Clock")
    icone = ""                # ícone simbólico (menus e Settings)
    opcoes_padrao = {}        # opções e seus valores iniciais
    fps_animacao = 60         # quadros por segundo das animações deste widget (menos = mais leve)
    tamanho = (240, 100)      # tamanho FIXO do conteúdo (antes da escala); o cartão nunca muda com o conteúdo

    def __init__(self, app, registro):
        super().__init__(application=app, decorated=False, resizable=False)
        self.app = app
        self.registro = registro
        self.opcoes = dict(self.opcoes_padrao) | dict(registro.get("opcoes") or {})
        self.e = float(app.config.d.get("escala", "1"))      # escala dos desenhos
        self.areas = []                                     # DrawingAreas: redesenhar ao trocar o tema
        self.temporizadores = []
        self.tweens = []
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.BOTTOM)
        Camada.set_namespace(self, "aero-widget")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_anchor(self, Camada.Edge.LEFT, True)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.add_css_class("aero-widget")
        self.add_css_class(f"escala-{ESCALAS.get(app.config.d.get('escala', '1'), 100)}")
        # moldura = Overlay: embaixo a textura do vidro (Picture), no meio o realce do hover, em cima o conteúdo
        # O Overlay ocupa a janela inteira (cartão + margem da sombra). Filho principal: uma caixa vazia (não
        # pode ser a textura: o tamanho dela realimentaria o da janela e o cartão cresceria sem parar).
        # Camadas, de baixo pra cima: a textura do vidro (NÃO é medida: só preenche), o realce do hover e
        # o conteúdo (é quem dá o tamanho; a margem de 14 px dele é o espaço da sombra).
        self.moldura = Gtk.Overlay(child=Gtk.Box())
        self.moldura.add_css_class("moldura")
        self.fundo_pic = Gtk.Picture(can_shrink=True, content_fit=Gtk.ContentFit.FILL)
        self.moldura.add_overlay(self.fundo_pic)
        brilho = Gtk.Box()
        brilho.add_css_class("brilho")
        self.moldura.add_overlay(brilho)
        self.quadro = Gtk.Box()                     # margem (sombra) + padding; dentro, o palco fixo
        self.quadro.add_css_class("cartao")
        w, h = self.tamanho
        self.palco = Palco(w * self.e, h * self.e)
        self.quadro.append(self.palco)
        self.cartao = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6 * self.e)   # o conteúdo (montar())
        self.palco.put(self.cartao, 0, 0, w * self.e, h * self.e)
        self.moldura.add_overlay(self.quadro)
        self.moldura.set_measure_overlay(self.quadro, True)
        self._fundo_tam = None
        self.set_child(self.moldura)
        self.mover(registro["x"], registro["y"])
        direito = Gtk.GestureClick(button=3)
        direito.connect("pressed", lambda g, n, x, y: (depurar("botão direito no widget", self.tipo, x, y),
                                                       self.app.menu_widget(self, x, y)))
        self.quadro.add_controller(direito)
        # região de entrada: definida quando a janela ganha tamanho de verdade (no "map" ainda mede 0)
        self.connect("notify::default-width", lambda *_: self.set_entrada(not self.app.config.d.get("editar")))
        self.cartao.connect("realize", lambda *_: GLib.idle_add(self._entrada_inicial))
        self.montar()
        self.present()
        GLib.timeout_add(80, self._pintar_fundo)
        GLib.timeout_add(350, self._caber_na_tela)

    def _pintar_fundo(self):
        """Pinta o vidro do cartão numa textura do tamanho atual (uma vez; de novo só se o tamanho ou o tema mudar)."""
        w, h = self.moldura.get_width(), self.moldura.get_height()      # = a janela: cartão + 2×SOMBRA
        if w < 4 or h < 4 or (w, h) == self._fundo_tam:
            return GLib.SOURCE_REMOVE
        try:
            opacidade = float(self.app.config.d.get("vidro", "0.62"))
        except ValueError:
            opacidade = 0.62
        self.fundo_pic.set_paintable(vidro_textura(w, h, self.get_color(), opacidade))
        self._fundo_tam = (w, h)
        return GLib.SOURCE_REMOVE

    def _caber_na_tela(self):
        """Depois de medido: se passou da borda (outro monitor, escala maior...), puxa pra dentro."""
        x, y, w, h = self.retangulo()
        x0, y0, x1, y1, _ = moldura(self.app.monitor)
        nx = max(x0, min(x, x1 - w))
        ny = max(y0, min(y, y1 - h))
        if (nx, ny) != (x, y):
            self.mover(nx, ny)
            self.app.config.gravar()
        self._pintar_fundo()
        return GLib.SOURCE_REMOVE

    # a implementar em cada widget:
    def montar(self):
        pass

    def atualizar_opcoes(self, opcoes):
        """Opções mudaram no Settings. Padrão: remonta o conteúdo."""
        self.opcoes = dict(self.opcoes_padrao) | dict(opcoes or {})
        self.parar()
        while (f := self.cartao.get_first_child()) is not None:
            self.cartao.remove(f)
        self.areas.clear()
        for f in list(self.palco.filhos):             # o palco pode ter filhos além do cartao (batalha)
            if f[0] is not self.cartao:
                self.palco.remove(f[0])
        self.fundo_pic.set_paintable(None)            # solta a textura velha (senão a janela não encolhe)
        self._fundo_tam = None
        self.montar()
        GLib.timeout_add(80, self._pintar_fundo)

    def parar(self):
        for t in self.temporizadores:
            GLib.source_remove(t)
        self.temporizadores.clear()
        for tw in self.tweens:
            tw.cancelar()
        self.tweens.clear()

    def animar(self, ms, passo, easing="out", ao_terminar=None, fps=None):
        """Animação de `ms` chamando passo(v) com v de 0 a 1. Com "Reduce motion": pula direto pro fim.
        fps menor (ex: 25) pra animações que redesenham áreas grandes a toda hora (Resources)."""
        if self.menos_movimento:
            passo(1.0)
            if ao_terminar:
                ao_terminar()
            return None
        self.tweens = [t for t in self.tweens if t.fonte]
        tw = Tween(ms, passo, easing, ao_terminar, fps or self.fps_animacao)
        self.tweens.append(tw)
        return tw

    # utilidades:
    def a_cada(self, segundos, funcao):
        """Chama funcao() a cada N segundos (guardado pra parar junto com o widget)."""
        def tick():
            funcao()
            return GLib.SOURCE_CONTINUE
        self.temporizadores.append(GLib.timeout_add_seconds(segundos, tick))

    def area(self, w, h, desenhar, **kw):
        a = Gtk.DrawingArea(content_width=int(w * self.e), content_height=int(h * self.e), **kw)
        a.set_draw_func(lambda ar, cr, aw, ah: desenhar(cr, aw, ah))
        self.areas.append(a)
        return a

    def redimensionar(self, w, h):
        """Widgets cujo tamanho depende das opções (Resources) chamam isto no montar()."""
        self.palco.redimensionar(w * self.e, h * self.e)
        for f in self.palco.filhos:
            if f[0] is self.cartao:
                f[3], f[4] = w * self.e, h * self.e

    def rotulo(self, s, classe="", **kw):
        kw.setdefault("xalign", 0)
        kw.setdefault("ellipsize", Pango.EllipsizeMode.END)   # nunca pede mais que o palco dá
        lb = Gtk.Label(label=s, **kw)
        for c in classe.split():
            lb.add_css_class(c)
        return lb

    def mover(self, x, y):
        """(x, y) é a posição do CARTÃO; a janela começa SOMBRA px antes (a margem da sombra)."""
        self.registro["x"], self.registro["y"] = int(x), int(y)
        Camada.set_margin(self, Camada.Edge.LEFT, int(x) - SOMBRA)
        Camada.set_margin(self, Camada.Edge.TOP, int(y) - SOMBRA)

    def retangulo(self):
        """O retângulo do cartão na tela (sem a margem da sombra)."""
        w, h = self.get_width(), self.get_height()
        if not w or not h:
            nat = self.get_preferred_size()[1]
            w, h = nat.width, nat.height
        return self.registro["x"], self.registro["y"], w - 2 * SOMBRA, h - 2 * SOMBRA

    def _entrada_inicial(self):
        self.set_entrada(not self.app.config.d.get("editar"))
        return GLib.SOURCE_REMOVE

    def set_entrada(self, ativa):
        """Modo arrumar: o widget vira "fantasma" pro mouse (os cliques passam pra camada do desktop).
        Fora dele, a região é "enorme" (o compositor corta no tamanho da superfície): assim não depende
        de a janela já ter sido medida."""
        self.entrada_ativa = ativa
        surf = self.get_surface()
        if surf is None:
            return
        if ativa:
            surf.set_input_region(cairo.Region(cairo.RectangleInt(0, 0, 32767, 32767)))
        else:
            surf.set_input_region(cairo.Region())
        self.queue_draw()        # a região nova só vale no próximo "commit" da superfície: força um quadro

    def ao_tema(self):
        self._fundo_tam = None
        self._pintar_fundo()
        for a in self.areas:
            a.queue_draw()

    def fechar(self):
        self.parar()
        self.palco.limpar()
        self.destroy()

    @property
    def escuro(self):
        return Adw.StyleManager.get_default().get_dark()

    @property
    def menos_movimento(self):
        return bool(self.app.config.d.get("menos_movimento"))


# ---------- a camada do desktop: botão direito e modo arrumar ----------

class Desktop(Gtk.Window):
    def __init__(self, app):
        super().__init__(application=app, decorated=False)
        self.app = app
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.BOTTOM)
        Camada.set_namespace(self, "aero-desktop")
        for b in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, b, True)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.add_css_class("aero-desktop")
        self.editando = False
        self.arrastando = None          # (widget, dx, dy) durante um arraste

        self.fundo = Gtk.DrawingArea(hexpand=True, vexpand=True)
        self.fundo.set_draw_func(self.desenhar)
        sobre = Gtk.Overlay(child=self.fundo)
        # pílula "Done" + dica, só no modo arrumar
        self.pilula = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, valign=Gtk.Align.START, margin_top=BARRA + 22)
        self.pilula.add_css_class("pilula")
        self.pilula.append(Gtk.Label(label="Drag widgets to move them  ·  right-click one for options"))
        pronto = Gtk.Button(label="Done")
        pronto.add_css_class("suggested-action")
        pronto.add_css_class("pill")
        pronto.connect("clicked", lambda *_: self.app.editar(False))
        self.pilula.append(pronto)
        self.pilula.set_visible(False)
        sobre.add_overlay(self.pilula)
        self.set_child(sobre)

        direito = Gtk.GestureClick(button=3)
        direito.connect("pressed", self.ao_direito)
        self.fundo.add_controller(direito)
        esquerdo = Gtk.GestureClick(button=1)
        esquerdo.connect("pressed", lambda g, n, x, y: depurar("botão esquerdo no desktop em", x, y))
        self.fundo.add_controller(esquerdo)
        arraste = Gtk.GestureDrag(button=1)
        arraste.connect("drag-begin", self.ao_comecar)
        arraste.connect("drag-update", self.ao_arrastar)
        arraste.connect("drag-end", self.ao_soltar)
        self.fundo.add_controller(arraste)
        self.present()

    def modo(self, editando):
        self.editando = editando
        self.pilula.set_visible(editando)
        self.fundo.queue_draw()

    def widget_em(self, x, y):
        for w in reversed(self.app.widgets):
            if not w.get_visible():
                continue                         # widget de outra área de trabalho
            wx, wy, ww, wh = w.retangulo()
            if wx <= x <= wx + ww and wy <= y <= wy + wh:
                return w
        return None

    def ao_direito(self, _g, _n, x, y):
        depurar("botão direito no desktop em", x, y, "editando:", self.editando)
        w = self.widget_em(x, y) if self.editando else None
        if w:
            self.app.menu_widget(w, x, y, ancora=self.fundo)
        else:
            self.app.menu_desktop(self.fundo, x, y)

    def ao_comecar(self, _g, x, y):
        if not self.editando:
            return
        w = self.widget_em(x, y)
        if w:
            wx, wy, _, _ = w.retangulo()
            self.arrastando = (w, x - wx, y - wy)
            # traz pra "frente" na lista (fica por cima dos outros ao sobrepor)
            self.app.widgets.remove(w)
            self.app.widgets.append(w)

    def ao_arrastar(self, g, dx, dy):
        if not self.arrastando:
            return
        w, ox, oy = self.arrastando
        ok, x0, y0 = g.get_start_point()
        _, _, ww, wh = w.retangulo()
        mx0, my0, mx1, my1, _ = moldura(self.app.monitor)     # nunca sai da moldura das janelas
        nx = x0 + dx - ox
        ny = y0 + dy - oy
        nx = round(nx / GRADE) * GRADE
        ny = round(ny / GRADE) * GRADE
        nx = max(mx0, min(mx1 - ww, nx))
        ny = max(my0, min(my1 - wh, ny))
        if (nx, ny) != (w.registro["x"], w.registro["y"]):
            w.mover(nx, ny)
            self.fundo.queue_draw()

    def ao_soltar(self, *_):
        if self.arrastando:
            self.arrastando = None
            self.app.config.gravar()
            self.fundo.queue_draw()

    def desenhar(self, area, cr, w, h):
        if not self.editando:
            return                                   # fora do modo arrumar: totalmente transparente
        cr.set_source_rgba(0, 0, 0, 0.10)            # escurece de leve o desktop
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cor = cor_texto(area)
        for wd in self.app.widgets:
            if not wd.get_visible():
                continue
            x, y, ww, wh = wd.retangulo()
            retangulo_redondo(cr, x + 0.5, y + 0.5, ww - 1, wh - 1, RAIO)
            cr.set_source_rgba(*cor[:3], 0.9)
            cr.set_line_width(2)
            cr.set_dash([8, 6])
            cr.stroke()
            cr.set_dash([])
            # etiqueta com o nome, FORA da moldura (o cartão do widget fica por cima desta camada e
            # cobriria qualquer coisa desenhada dentro): acima dele, ou abaixo se estiver colado na barra
            layout = PangoCairo.create_layout(cr)
            fd = area.get_pango_context().get_font_description().copy()
            fd.set_absolute_size(12 * Pango.SCALE)
            fd.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(fd)
            layout.set_text(wd.nome, -1)
            _, log = layout.get_pixel_extents()
            alt = log.height + 8
            ey = y - alt - 6 if y - alt - 6 >= self.app.monitor["reservado"][1] + 4 else y + wh + 6
            retangulo_redondo(cr, x, ey, log.width + 16, alt, 8)
            cr.set_source_rgba(*cor[:3], 0.92)
            cr.fill()
            cr.set_source_rgba(1 - cor[0], 1 - cor[1], 1 - cor[2], 1)
            cr.move_to(x + 8, ey + 4)
            PangoCairo.show_layout(cr, layout)


# ---------- o aplicativo ----------

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Widgets", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.pronto = False
        self.widgets = []
        self.config = None
        self.desktop = None
        self.monitor = None
        self.area = None            # área de trabalho na tela (Settings → Workspaces: widgets e wallpaper por área)
        self.wall_area = None       # wallpaper que ESTE daemon pôs por causa da área

    def do_command_line(self, linha):
        args = linha.get_arguments()[1:]
        if not self.pronto:
            self.iniciar()
        if "--editar" in args:
            self.editar(not self.config.d.get("editar"))
        if "--adicionar" in args:
            i = args.index("--adicionar")
            if i + 1 < len(args):
                self.adicionar(args[i + 1])
        if "--recarregar" in args:
            self.config.ler()
            self.aplicar_config(tudo=True)
        return 0

    def iniciar(self):
        self.pronto = True
        self.hold()
        provedor = Gtk.CssProvider()
        provedor.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.monitor = monitor_focado()
        self.area = hypr_json("activeworkspace", {}).get("id")
        self.config = Config()
        self.acoes()
        self.desktop = Desktop(self)
        self.aplicar_config(tudo=True)
        # vigia a configuração (Settings e menus gravam o arquivo)
        self.vigia = Gio.File.new_for_path(CONFIG).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.vigia.connect("changed", self.ao_mudar_arquivo)
        self._debounce = None
        Adw.StyleManager.get_default().connect("notify::dark", lambda *_: [w.ao_tema() for w in self.widgets])
        self.vigia_cores = Gio.File.new_for_path(CORES_BARRA).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self.vigia_cores.connect("changed", lambda *_: GLib.timeout_add(300, lambda: ([w.ao_tema() for w in self.widgets], False)[1]))
        GLib.timeout_add_seconds(20, self.vigiar_hyprland)
        threading.Thread(target=self.ouvir_areas, daemon=True).start()
        self.aplicar_wallpaper()

    # ---- por área de trabalho: cada widget pode ter "areas": [1, 3]; sem a chave = todas ----
    def visivel_aqui(self, reg):
        areas = reg.get("areas")
        return not areas or self.area in areas

    def ouvir_areas(self):
        """Eventos do Hyprland (socket2), numa thread: só repassa a troca de área pro GTK."""
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(os.path.join(HYPR, ".socket2.sock"))
        except OSError:
            return
        buf = b""
        while (dados := s.recv(4096)):
            buf += dados
            *linhas, buf = buf.split(b"\n")
            for linha in linhas:
                evento, _, valor = linha.decode(errors="replace").partition(">>")
                if evento == "workspacev2":
                    try:
                        area = int(valor.split(",", 1)[0])
                    except ValueError:
                        continue
                    if area > 0:
                        GLib.idle_add(self.mudar_area, area)

    def mudar_area(self, area):
        if area != self.area:
            self.area = area
            for w in self.widgets:
                w.set_visible(self.visivel_aqui(w.registro))
            self.aplicar_wallpaper()
            self.desktop.fundo.queue_draw()
        return GLib.SOURCE_REMOVE

    def aplicar_wallpaper(self):
        """Wallpaper da área (por_area no widgets.json). Áreas sem um usam o wallpaper normal (hyprpaper.conf).
        Se nenhuma área tem wallpaper próprio, não mexe em nada."""
        por_area = self.config.d.get("por_area") or {}
        if not any(v.get("wallpaper") for v in por_area.values()):
            self.wall_area = None
            return
        alvo = (por_area.get(str(self.area)) or {}).get("wallpaper")
        if not alvo or not os.path.isfile(alvo) or "," in alvo:
            alvo = wallpaper_padrao()
        if alvo and alvo != self.wall_area:
            self.wall_area = alvo
            threading.Thread(target=lambda: subprocess.run(["hyprctl", "hyprpaper", "wallpaper", f",{alvo},cover"],
                                                           capture_output=True), daemon=True).start()

    def vigiar_hyprland(self):
        if not hyprland_vivo():
            self.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def ao_mudar_arquivo(self, *_):
        if self._debounce:
            GLib.source_remove(self._debounce)
        def aplicar():
            self._debounce = None
            self.config.ler()
            self.aplicar_config()
            return GLib.SOURCE_REMOVE
        self._debounce = GLib.timeout_add(200, aplicar)

    def aplicar_config(self, tudo=False):
        """Compara a configuração com o que está na tela: cria, fecha, move, atualiza opções."""
        if self.widgets and (tudo or any(w.e != float(self.config.d.get("escala", "1")) for w in self.widgets)):
            for w in self.widgets:
                w.fechar()
            self.widgets = []
        atuais = {w.registro["id"]: w for w in self.widgets}
        vistos = set()
        for reg in self.config.widgets:
            if not reg.get("ativo", True) or reg["tipo"] not in TIPOS:
                continue
            vistos.add(reg["id"])
            w = atuais.get(reg["id"])
            if w is None:
                try:
                    w = TIPOS[reg["tipo"]](self, reg)
                except Exception as erro:  # noqa: BLE001 — um widget com problema não derruba os outros
                    print(f"widget {reg['id']}: {erro!r}", file=sys.stderr)
                    continue
                self.widgets.append(w)
            else:
                w.registro = reg
                if (reg["x"] - SOMBRA, reg["y"] - SOMBRA) != (Camada.get_margin(w, Camada.Edge.LEFT), Camada.get_margin(w, Camada.Edge.TOP)):
                    w.mover(reg["x"], reg["y"])
                if (dict(w.opcoes_padrao) | dict(reg.get("opcoes") or {})) != w.opcoes:
                    w.atualizar_opcoes(reg.get("opcoes"))
            if w.get_visible() != self.visivel_aqui(reg):
                w.set_visible(self.visivel_aqui(reg))
        for wid, w in atuais.items():
            if wid not in vistos:
                w.fechar()
                self.widgets.remove(w)
        if self.area is not None:
            self.aplicar_wallpaper()
        if getattr(self, "_vidro", None) != self.config.d.get("vidro"):     # opacidade do vidro mudou
            self._vidro = self.config.d.get("vidro")
            for w in self.widgets:
                w.ao_tema()
        editar = bool(self.config.d.get("editar"))
        if editar != self.desktop.editando:
            self.desktop.modo(editar)
            for w in self.widgets:
                w.set_entrada(not editar)
        self.desktop.fundo.queue_draw()

    # ---- ações (menus e linha de comando) ----
    def acoes(self):
        def acao(nome, funcao, tipo=None):
            a = Gio.SimpleAction.new(nome, GLib.VariantType.new(tipo) if tipo else None)
            a.connect("activate", lambda _a, p: funcao(p.get_string() if p else None))
            self.add_action(a)
        acao("adicionar", self.adicionar, "s")
        acao("remover", self.remover, "s")
        acao("desativar", self.desativar, "s")
        acao("editar", lambda _p: self.editar(not self.config.d.get("editar")))
        acao("configuracoes", lambda _p: self.abrir("CONFIG_ABA=widgets ~/.local/bin/configuracoes"))
        acao("wallpaper", lambda _p: self.abrir("CONFIG_ABA=wallpapers ~/.local/bin/configuracoes"))
        acao("tela", lambda _p: self.abrir("CONFIG_ABA=tela ~/.local/bin/configuracoes"))
        acao("terminal", lambda _p: self.abrir("kitty"))
        acao("arquivos", lambda _p: self.abrir("xdg-open ~"))

    def abrir(self, cmd):
        hypr(f"dispatch exec {cmd}")

    def editar(self, ligar):
        self.config.d["editar"] = bool(ligar)
        self.config.gravar()
        self.aplicar_config()

    def adicionar(self, tipo):
        if tipo not in TIPOS:
            return
        n = sum(1 for w in self.config.widgets if w["tipo"] == tipo)
        x0, y0, _, _, _ = moldura(self.monitor)
        reg = {"id": self.config.novo_id(tipo), "tipo": tipo, "ativo": True, "opcoes": {},
               "x": x0 + 40 * n, "y": y0 + 40 * n}
        self.config.widgets.append(reg)
        self.config.gravar()
        self.aplicar_config()

    def remover(self, wid):
        self.config.d["widgets"] = [w for w in self.config.widgets if w["id"] != wid]
        self.config.gravar()
        self.aplicar_config()

    def desativar(self, wid):
        reg = self.config.por_id(wid)
        if reg:
            reg["ativo"] = False
            self.config.gravar()
            self.aplicar_config()

    # ---- menus ----
    def menu_desktop(self, ancora, x, y):
        menu = Gio.Menu()
        adicionar = Gio.Menu()
        for tipo, classe in TIPOS.items():
            item = Gio.MenuItem.new(classe.nome, None)
            item.set_action_and_target_value("app.adicionar", GLib.Variant.new_string(tipo))
            adicionar.append_item(item)
        menu.append_submenu("Add widget", adicionar)
        menu.append("Done arranging" if self.config.d.get("editar") else "Arrange widgets", "app.editar")
        menu.append("Widget settings…", "app.configuracoes")
        s2 = Gio.Menu()
        s2.append("Change wallpaper…", "app.wallpaper")
        s2.append("Display settings…", "app.tela")
        menu.append_section(None, s2)
        s3 = Gio.Menu()
        s3.append("Open terminal", "app.terminal")
        s3.append("Open files", "app.arquivos")
        menu.append_section(None, s3)
        self.popup(menu, ancora, x, y)

    def menu_widget(self, w, x, y, ancora=None):
        menu = Gio.Menu()
        menu.append("Done arranging" if self.config.d.get("editar") else "Arrange widgets", "app.editar")
        menu.append("Widget settings…", "app.configuracoes")
        s2 = Gio.Menu()
        for rotulo, acao in (("Hide this widget", "app.desativar"), ("Remove this widget", "app.remover")):
            item = Gio.MenuItem.new(rotulo, None)
            item.set_action_and_target_value(acao, GLib.Variant.new_string(w.registro["id"]))
            s2.append_item(item)
        menu.append_section(None, s2)
        self.popup(menu, ancora or w.cartao, x, y)

    def popup(self, modelo, ancora, x, y):
        """Menu de contexto numa camada. Receita do Dock: a camada não recebe teclado (pra não roubar o foco
        das janelas), mas o popup precisa de um "grab": pedimos o teclado sob demanda ANTES de abrir e
        devolvemos ao fechar (trocar com o menu aberto faz o Hyprland fechá-lo)."""
        depurar("popup em", x, y, "âncora:", type(ancora).__name__)
        janela = ancora.get_root()
        pop = Gtk.PopoverMenu.new_from_model(modelo)
        pop.set_parent(ancora)
        pop.set_has_arrow(False)
        ret = Gdk.Rectangle()
        ret.x, ret.y, ret.width, ret.height = int(x), int(y), 1, 1
        pop.set_pointing_to(ret)
        pop.set_position(Gtk.PositionType.BOTTOM)
        pop.set_halign(Gtk.Align.START)             # alinha a borda ESQUERDA do menu ao cursor (o padrão centraliza)
        janela_antes = hypr_json("activewindow", {}).get("address")

        def fechou(p):
            Camada.set_keyboard_mode(janela, Camada.KeyboardMode.NONE)
            if janela_antes:          # devolve o foco pra janela em que você estava
                GLib.timeout_add(80, lambda: (hypr(f"dispatch focuswindow address:{janela_antes}"), False)[1])
            GLib.idle_add(lambda: (p.unparent(), False)[1])
        pop.connect("closed", fechou)
        Camada.set_keyboard_mode(janela, Camada.KeyboardMode.ON_DEMAND)
        pop.popup()


if __name__ == "__main__":
    # Os widget_*.py fazem "from widgets import ...": isso importa este arquivo como MÓDULO (uma segunda
    # cópia, diferente do __main__). Então usamos essa cópia também aqui, senão o TIPOS de lá fica cheio
    # e o daqui vazio (foi o primeiro bug).
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import widgets as modulo_widgets
    for modulo in ("widget_relogio", "widget_sistema", "widget_recursos", "widget_processos", "widget_clima", "widget_batalha"):
        try:
            __import__(modulo)              # cada um se registra em modulo_widgets.TIPOS
        except Exception as erro:  # noqa: BLE001
            print(f"{modulo}: {erro!r}", file=sys.stderr)
    sys.exit(modulo_widgets.App().run(sys.argv))
