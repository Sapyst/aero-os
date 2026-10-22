#!/usr/bin/env python3
"""
som.py — controle de som integrado à barra + avisos de volume/brilho na tela (OSD). Doc 23.

Na barra (waybar, módulo de volume):
  clique          → desce uma janelinha com o controle deslizante do volume
  botão direito   → a mesma janelinha, com as saídas, as entradas (microfones) e o volume do mic.
                    Escolher um dispositivo = ele vira o padrão do sistema.
                    "Sound settings" no fim abre o Settings (só se você clicar).
  Fecha clicando fora ou com Esc.

Teclas de volume/brilho/mudo: depois de mudar, mostram uma "pílula" no rodapé da tela (OSD).

Comandos (o ~/.local/bin/som manda pelo D-Bus pro programa já aberto):
  som popup simples|completo     som osd volume|mic|brilho     som notificacoes abrir     som --daemon

Também mora aqui a central de notificações (sininho da barra, doc 25): é o mesmo tipo de janelinha.

Peças do GTK:
- As janelinhas são CAMADAS (layer-shell), como a barra: ficam por cima e não viram "janela".
- Pra fechar ao clicar fora, existe uma camada transparente do tamanho da tela atrás da janelinha
  (o "pegador"): clicou nela = clicou fora.
- "pactl subscribe" avisa quando o volume ou os dispositivos mudam (por tecla, por outro app...):
  a janelinha se atualiza sozinha.
"""
import os
import re
import subprocess
import sys
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

# Reaproveita do Settings os nomes bonitos e a lista de dispositivos (sec_som.py)
sys.path.insert(0, os.path.expanduser("~/.local/share/aero-settings"))
from sec_som import listar, nome_bonito, volume_de  # noqa: E402


def app_instalado(app):
    """Nome de .desktop vindo de fora (ex.: notificação) é seguro e existe? Só letras, números . _ -"""
    if not app or not re.fullmatch(r"[A-Za-z0-9._-]+", app):
        return False
    try:
        return Gio.DesktopAppInfo.new(app.removesuffix(".desktop") + ".desktop") is not None
    except TypeError:          # PyGObject: construtor devolveu NULL = não instalado
        return False

LARGURA = 340
BACKLIGHT = "/sys/class/backlight/amdgpu_bl1"

CSS = """
/* O gtk.css do sistema põe "backdrop-filter: blur" em TODA janela (doc 16). Aqui as janelas são
   transparentes: sem o "none", o Hyprland borraria o retângulo inteiro (a tela toda, no caso do pegador).
   O desfoque fica só nos cartões. */
window.som-fundo, window.som, window.osd, window.central, window.central-rapida { background: transparent; backdrop-filter: none; }
.cartao-som {
    background-color: var(--popover-bg-color, @popover_bg_color);   /* cor do tema (doc 18) */
    border-radius: 22px; padding: 14px; margin: 2px;
    /* SEM desfoque e SEM sombra grande: em camadas, o Hyprland desfoca o RETÂNGULO da janela (não o
       cartão arredondado) e a sombra é cortada na borda da janela, os dois viram "quadrados" em volta
       dos cantos redondos. No lugar, um contorno fino que segue o arredondamento. */
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.10);
}
.cartao-som .titulo { font-weight: 700; font-size: 11px; opacity: 0.55; margin: 8px 4px 2px 4px; }
.cartao-som .aparelho { font-weight: 600; }
.cartao-som .por-cento { font-feature-settings: "tnum"; min-width: 38px; opacity: 0.8; }
.cartao-som row { border-radius: 10px; padding: 6px 8px; }
.cartao-som list { background: transparent; }
.cartao-som button.perigo { color: #e01b24; }
window.menu-janela .cartao-som button label { font-weight: normal; }   /* menu com letra normal, não negrito */
.cartao-som .marcado { color: var(--accent-bg-color, #3584e4); }
.cartao-som scale trough { min-height: 6px; }
.pilula {
    background-color: var(--popover-bg-color, @popover_bg_color);
    border-radius: 99px; padding: 12px 20px; margin: 2px;
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.10);     /* (mesmo motivo do cartão acima) */
}
.pilula levelbar trough { min-height: 6px; border-radius: 99px; background: alpha(currentColor, 0.12); }
.pilula levelbar block.filled { background: var(--accent-bg-color, #3584e4); border-radius: 99px; }
.pilula .por-cento { font-feature-settings: "tnum"; min-width: 40px; font-weight: 600; }
/* O aviso nunca tem foco, e o GTK "desbota" texto e ícones de janelas sem foco (estado :backdrop).
   Aqui a cor é forçada, com ou sem foco. */
.pilula label, .pilula image, .pilula label:backdrop, .pilula image:backdrop { color: @popover_fg_color; opacity: 1; }
/* ---- Central rápida (doc 30): estilo Material — fundo tonal, cantos bem redondos, contorno fino ---- */
.central > .player, .central > .controles {
    background-color: var(--popover-bg-color, @popover_bg_color);
    border-radius: 22px; margin: 0;   /* sem margem: a borda bate EXATAMENTE no limite das janelas (gap) */
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.10);
}
.central .veu { background-image: linear-gradient(to right, rgba(0,0,0,0.78), rgba(0,0,0,0.35)); }
.central .titulo-musica { color: white; font-weight: 700; font-size: 16px; }
.central .artista { color: rgba(255,255,255,0.85); }
.central .via { color: rgba(255,255,255,0.65); font-size: 11px; }
/* pílulas de fonte (várias tocando): a acesa é a que a central controla */
.central button.fonte { color: rgba(255,255,255,0.6); background: rgba(255,255,255,0.08); border-radius: 99px;
                        font-size: 10px; min-height: 20px; padding: 0 9px; box-shadow: none; }
.central button.fonte-atual { color: white; background: rgba(255,255,255,0.24); font-weight: 600; }
.central .botao-player { color: white; background: rgba(255,255,255,0.14); border-radius: 12px; min-width: 34px; min-height: 30px; }
.central .botao-player:hover { background: rgba(255,255,255,0.26); }
.central > .controles { padding: 16px; }
.central .chip { padding: 3px 10px; border-radius: 99px; background: alpha(currentColor, 0.08); font-weight: 600; font-size: 12px; }
.central button.bloco { border-radius: 14px; padding: 10px 10px 10px 14px; background: alpha(currentColor, 0.06);
                        box-shadow: inset 0 0 0 1px alpha(currentColor, 0.08); }
.central button.bloco:hover { background: alpha(currentColor, 0.10); }
.central button.bloco.ligado { background: var(--accent-bg-color, #3584e4); color: var(--accent-fg-color, white); box-shadow: none; }
.central button.bloco.ligado:hover { background: mix(var(--accent-bg-color, #3584e4), white, 0.1); }
.central .bloco-titulo { font-weight: 600; }
.central button.perfil { padding: 2px 12px 2px 2px; border-radius: 99px; background: transparent; }
.central button.perfil:hover { background: alpha(currentColor, 0.10); }
.central .redes { border-radius: 14px; padding: 8px; background: alpha(currentColor, 0.06); }
.central .redes-titulo { font-weight: 600; margin-left: 6px; }
.central button.rede { border-radius: 10px; padding: 6px 8px; background: transparent; }
.central button.rede:hover { background: alpha(currentColor, 0.10); }
.central button.desligar { min-width: 30px; min-height: 30px; padding: 0; border-radius: 99px; background: alpha(currentColor, 0.08); }
.central button.desligar.ligado { background: var(--accent-bg-color, #3584e4); color: var(--accent-fg-color, white); }
.central .bloco-estado { font-size: 11px; opacity: 0.75; }
.central button.seta { min-width: 26px; min-height: 26px; padding: 0; border-radius: 99px; background: transparent; }
.central button.seta:hover { background: alpha(currentColor, 0.12); }
.central scale trough { min-height: 8px; border-radius: 99px; }
.central scale.progresso { padding: 10px 0; }
.central .tempo { font-feature-settings: "tnum"; }
/* ⏪ ⏩: pequenos e discretos, só o ícone (sem fundo); acendem ao passar o mouse */
.central button.pulo { min-width: 24px; min-height: 24px; padding: 0; border-radius: 99px;
                       background: transparent; color: rgba(255,255,255,0.8); -gtk-icon-size: 14px; }
.central button.pulo:hover { background: rgba(255,255,255,0.18); color: white; }   /* números de largura igual: "1" ocupa o mesmo que "8" */   /* área de clique alta, barra fininha */
.central scale.progresso trough { min-height: 5px; background: rgba(255,255,255,0.25); }
.central scale.progresso highlight { background: white; }
.central scale.progresso slider { min-width: 14px; min-height: 14px; background: white; }
"""


def rodar(*cmd, timeout=4):
    # sempre com prazo: um player/D-Bus travado (Firefox, Steam...) não pode prender a central
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


_FOTO = {"chave": None, "textura": None}


def foto_perfil():
    """~/.face em MINIATURA (96 px), feita uma vez e guardada até a foto mudar.
    Antes a central decodificava a foto inteira a cada redesenho: com uma foto de 5000×6000 eram ~100 ms e
    uma textura de 120 MB por clique/mudança de volume — a central travava e ficava "uma imagem"."""
    caminho = os.path.expanduser("~/.face")
    try:
        chave = (os.path.getmtime(caminho), os.path.getsize(caminho))
    except OSError:
        return None
    if _FOTO["chave"] != chave:
        try:
            from gi.repository import GdkPixbuf
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(caminho, 96, 96, True)
            _FOTO["textura"] = Gdk.Texture.new_for_pixbuf(pb)
        except GLib.Error:
            _FOTO["textura"] = None
        _FOTO["chave"] = chave
    return _FOTO["textura"]


def hyprctl_json(*args):
    import json
    return json.loads(rodar("hyprctl", *args, "-j") or "null")


def icone_volume(v, mudo):
    if mudo or v == 0:
        return "audio-volume-muted-symbolic"
    return "audio-volume-low-symbolic" if v < 34 else "audio-volume-medium-symbolic" if v < 67 \
        else "audio-volume-high-symbolic"


def gap():
    """O espaço padrão entre as coisas e a borda/barra (general:gaps_out do Hyprland, 12 px).
    Regra do Aero: TUDO respeita esse espaço em volta; só tela cheia de verdade ocupa 100%."""
    try:
        return int(hyprctl_json("getoption", "general:gaps_out")["custom"].split()[0])
    except (TypeError, KeyError, ValueError, IndexError):
        return 12


class Pegador(Gtk.Window):
    """Camada transparente da tela toda: um clique nela = "clicou fora" = fecha a janelinha."""

    def __init__(self, ao_clicar):
        super().__init__()
        self.add_css_class("som-fundo")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.TOP)
        Camada.set_namespace(self, "som-fundo")
        Camada.set_exclusive_zone(self, -1)       # cobre até a barra (clicar no volume de novo fecha)
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        clique = Gtk.GestureClick(button=0)
        clique.connect("pressed", lambda *_: ao_clicar())
        self.add_controller(clique)
        self.set_child(Gtk.Box())


class Janelinha(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.add_css_class("som")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "som")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_anchor(self, Camada.Edge.RIGHT, True)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.ON_DEMAND)   # pra receber o Esc
        self.pegador = Pegador(self.fechar)
        self.zona_icone = (0, 0)
        self.completo = False

        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", lambda _c, tecla, *_: self.fechar() if tecla == Gdk.KEY_Escape else None)
        self.add_controller(teclas)

        from esfera import EsferaLiquida
        self.esfera = EsferaLiquida(92)          # o volume como líquido (esfera.py); mantida entre os montar()
        self.cartao = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.cartao.add_css_class("cartao-som")
        self.cartao.set_size_request(LARGURA, -1)
        self.set_child(self.cartao)

    # ---------- montar ----------

    def linha_volume(self, eh_saida, dispositivo):
        """Botão de mudo + controle deslizante + porcentagem."""
        caixa = Gtk.Box(spacing=8)
        v, mudo = volume_de(dispositivo), dispositivo.get("mute", False)
        mudo_bt = Gtk.Button(icon_name=icone_volume(v, mudo) if eh_saida else
                             ("microphone-sensitivity-muted-symbolic" if mudo else
                              "audio-input-microphone-symbolic"),
                             tooltip_text="Unmute" if mudo else "Mute")
        mudo_bt.add_css_class("flat")
        mudo_bt.add_css_class("circular")
        alvo = "@DEFAULT_SINK@" if eh_saida else "@DEFAULT_SOURCE@"
        tipo = "sink" if eh_saida else "source"
        mudo_bt.connect("clicked", lambda *_: rodar("pactl", f"set-{tipo}-mute", alvo, "toggle"))
        por_cento = Gtk.Label(label=f"{v}%", xalign=1)
        por_cento.add_css_class("por-cento")
        por_cento.set_width_chars(4)

        def mudou(valor):                 # a cada quadro: interface (número, ícone, esfera)
            n = round(valor)
            por_cento.set_label(f"{n}%")
            if eh_saida:
                self.esfera.set_nivel(valor / 100)
            mudo_bt.set_icon_name(icone_volume(n, False) if eh_saida else "audio-input-microphone-symbolic")

        def aplicar(valor):               # numa thread, no máximo ~30x/s, sempre o último valor
            rodar("pactl", f"set-{tipo}-volume", alvo, f"{valor:.1f}%")

        # slider.py: arraste 1:1, clique desliza, rolar ±2 — e não trava a tela esperando o pactl
        from slider import SliderFluido
        escala = SliderFluido(0, 100, valor=min(v, 100), ao_mudar=mudou, aplicar=aplicar)
        escala.set_ativo(not mudo)
        self.sliders = [s for s in getattr(self, "sliders", []) if s.get_root() is not None] + [escala]
        for w in (mudo_bt, escala, por_cento):
            caixa.append(w)
        return caixa

    def lista(self, eh_saida, itens):
        """Lista de dispositivos; o marcado é o padrão. Clicar = vira o padrão do sistema."""
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        for d in itens:
            linha = Gtk.Box(spacing=10)
            marca = Gtk.Image(icon_name="object-select-symbolic" if d["padrao"] else "")
            marca.set_size_request(16, -1)
            marca.add_css_class("marcado")
            linha.append(marca)
            linha.append(Gtk.Label(label=nome_bonito(d.get("description", d["name"]), d["name"], not eh_saida),
                                   xalign=0, hexpand=True, ellipsize=3, use_markup=False))
            r = Gtk.ListBoxRow(child=linha)
            r.nome = d["name"]
            lista.append(r)
        comando = "set-default-sink" if eh_saida else "set-default-source"
        lista.connect("row-activated", lambda _l, r: (rodar("pactl", comando, r.nome), self.montar()))
        return lista

    def montar(self):
        # segurando um slider: o painel não se remonta por baixo (era isso que fazia a barra pular)
        if any(s.segurando for s in getattr(self, "sliders", [])):
            return
        while (filho := self.cartao.get_first_child()) is not None:
            self.cartao.remove(filho)
        saidas, entradas = listar("sinks"), listar("sources")
        saida = next((d for d in saidas if d["padrao"]), None)
        entrada = next((d for d in entradas if d["padrao"]), None)

        if saida:
            nome = Gtk.Label(label=nome_bonito(saida.get("description", saida["name"]), saida["name"], False),
                             xalign=0, ellipsize=3, use_markup=False, margin_start=4)
            nome.add_css_class("aparelho")
            topo = Gtk.Box(spacing=12, margin_bottom=4)
            if self.esfera.get_parent() is not None:
                self.esfera.get_parent().remove(self.esfera)
            primeira = not getattr(self, "_esfera_pronta", False)
            self.esfera.set_mudo(saida.get("mute", False))
            self.esfera.set_nivel(min(volume_de(saida), 100) / 100, imediato=primeira)
            self._esfera_pronta = True
            topo.append(self.esfera)
            lado = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, valign=Gtk.Align.CENTER, hexpand=True)
            lado.append(nome)
            lado.append(self.linha_volume(True, saida))
            topo.append(lado)
            self.cartao.append(topo)
        else:
            self.cartao.append(Gtk.Label(label="No output device"))

        if self.completo:
            titulo = Gtk.Label(label="OUTPUT", xalign=0)
            titulo.add_css_class("titulo")
            self.cartao.append(titulo)
            self.cartao.append(self.lista(True, saidas))
            titulo = Gtk.Label(label="INPUT", xalign=0)
            titulo.add_css_class("titulo")
            self.cartao.append(titulo)
            if entrada:
                self.cartao.append(self.linha_volume(False, entrada))
            self.cartao.append(self.lista(False, entradas))
            self.cartao.append(Gtk.Separator(margin_top=6, margin_bottom=2))
            ajustes = Gtk.Button()
            ajustes.set_child(Gtk.Label(label="Sound settings", xalign=0))
            ajustes.add_css_class("flat")
            ajustes.connect("clicked", self.abrir_ajustes)
            self.cartao.append(ajustes)

    def abrir_ajustes(self, *_):
        self.fechar()
        subprocess.Popen(["hyprctl", "dispatch", "exec",
                          f"CONFIG_ABA=som {os.path.expanduser('~/.local/bin/configuracoes')}"],
                         stdout=subprocess.DEVNULL)

    # ---------- abrir / fechar ----------

    def abrir(self, completo):
        if self.get_visible() and self.completo == completo:
            self.fechar()        # clicou de novo no mesmo jeito = fecha
            return
        self.completo = completo
        # Debaixo do mouse (que está em cima do ícone de volume na barra), sem sair da tela
        x = int(rodar("hyprctl", "cursorpos").split(",")[0])
        monitor = next((m for m in hyprctl_json("monitors") or [] if m.get("focused")), None)
        largura_tela = int(monitor["width"] / monitor["scale"]) if monitor else 2560
        margem = max(8, min(largura_tela - x - (LARGURA + 20) // 2, largura_tela - LARGURA - 28))
        Camada.set_margin(self, Camada.Edge.RIGHT, margem)
        Camada.set_margin(self, Camada.Edge.TOP, gap())  # o mesmo espaço das janelas (a camada já começa abaixo da barra)
        self.montar()
        self.mostrar((x - 40, x + 40))

    def fechar(self):
        self.set_visible(False)
        self.pegador.set_visible(False)
        Janelinha.abertas.discard(self)
        self._entrada = None              # (central) a região de entrada é refeita ao reabrir

    # ---------- "clicou fora = fecha" ----------
    # A camada invisível (pegador) não pegava cliques de verdade. Então, enquanto há uma janelinha
    # aberta, o Hyprland ganha um atalho TEMPORÁRIO: qualquer clique (esquerdo ou direito) roda
    # "som clique fora" — com "bindn", que NÃO engole o clique (o app embaixo recebe normal).
    # Se o clique foi fora da janelinha, ela fecha. Ao fechar, o atalho é removido (custo zero).
    abertas = set()

    def mostrar(self, zona_icone):
        """zona_icone = (x0, x1): faixa da barra de cima onde fica o ícone que abre esta janelinha
        (clicar nele de novo é "alternar", quem cuida é o próprio clique da barra)."""
        self.zona_icone = zona_icone
        self.pegador.present()
        self.present()
        Janelinha.abertas.add(self)     # o vigia de cliques (hypr-botoes) avisa a cada clique (doc 33)

    def clique_fora(self):
        """Chamado a cada clique enquanto aberta: fecha se o clique foi fora dela."""
        if not self.get_visible():
            return
        x, y = (int(v) for v in rodar("hyprctl", "cursorpos").split(","))
        alto_barra = 42 + 4
        if y <= alto_barra and self.zona_icone[0] <= x <= self.zona_icone[1]:
            return                                 # clique no próprio ícone: a barra alterna
        for l in (l for m in (hyprctl_json("layers") or {}).values()
                  for nivel in m["levels"].values() for l in nivel):
            altura = getattr(self, "altura_conteudo", 0) or l["h"]     # central: só os cartões, não o vazio
            if l["namespace"] == Camada.get_namespace(self) and \
               l["x"] <= x <= l["x"] + l["w"] and l["y"] <= y <= l["y"] + altura:
                return                             # clique DENTRO da janelinha
        self.fechar()


# ---------- notificações: Hermes (dentro deste processo) ou, se ele não estiver no comando, o mako ----------
# Nada de chamar o hermesctl daqui: o Hermes mora NESTE processo e a chamada esperaria por ela mesma.
HERMES = None          # o serviço (hermes.Hermes) quando é ele que recebe as notificações


def visual_atual():
    """O visual do sistema (painel.conf VISUAL): DotStyle, AeroTerm ou AerOS."""
    try:
        for linha in open(os.path.expanduser("~/.config/hypr/painel.conf")):
            if linha.startswith("VISUAL="):
                return linha.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "AeroTerm"


def notif_historico():
    import json
    if HERMES is not None:
        return HERMES.historico_mako()
    try:
        return json.loads(rodar("makoctl", "history", "-j") or "[]")
    except ValueError:
        return []


def notif_modos():
    return set(HERMES.modos) if HERMES is not None else set(rodar("makoctl", "mode").split())


def notif_modo(nome, ligado):
    """Pode ser chamada de uma thread: a mudança no Hermes acontece na thread da tela."""
    if HERMES is not None:
        GLib.idle_add(lambda: (HERMES.modo(nome, ligado), False)[1])
    else:
        rodar("makoctl", "mode", "-a" if ligado else "-r", nome)


def notif_dispensar():
    if HERMES is not None:
        GLib.idle_add(lambda: (HERMES.dispensar_tudo(), False)[1])
    else:
        notif_dispensar()


class CentralNotificacoes(Janelinha):
    """O sininho da barra (doc 25): últimas notificações (histórico do mako), "Não perturbe" e
    "Clear all". O mako não tem comando pra apagar o histórico, então "Clear all" anota o número da
    última notificação e a lista passa a mostrar só as mais novas que ela."""

    LIMPAS = os.path.expanduser("~/.cache/aero-shell/notificacoes-limpas")

    def __init__(self):
        super().__init__()
        Camada.set_namespace(self, "central-notificacoes")

    def limpas_ate(self):
        try:
            return int(open(self.LIMPAS).read())
        except (OSError, ValueError):
            return 0

    def montar(self):
        import json
        import re
        while (filho := self.cartao.get_first_child()) is not None:
            self.cartao.remove(filho)
        if visual_atual() == "DotStyle":
            return self.montar_dots()
        self.remove_css_class("dots")
        historico = notif_historico()
        corte = self.limpas_ate() if HERMES is None else 0      # o Hermes limpa de verdade (sem corte por id)
        itens = [n for n in historico if n.get("id", 0) > corte]

        topo = Gtk.Box(spacing=8, margin_start=4)
        titulo = Gtk.Label(label="Notifications", xalign=0, hexpand=True)
        titulo.add_css_class("aparelho")
        topo.append(titulo)
        limpar = Gtk.Button(label="Clear all", sensitive=bool(itens))
        limpar.add_css_class("flat")
        limpar.connect("clicked", lambda *_: self.limpar(historico))
        topo.append(limpar)
        self.cartao.append(topo)

        dnd = Gtk.Box(spacing=8, margin_start=4, margin_end=4, margin_bottom=4)
        dnd.append(Gtk.Image(icon_name="notifications-disabled-symbolic"))
        dnd.append(Gtk.Label(label="Do not disturb", xalign=0, hexpand=True))
        chave = Gtk.Switch(active="dnd" in notif_modos(), valign=Gtk.Align.CENTER)
        chave.connect("notify::active", lambda c, _p: self.nao_perturbe(c.get_active()))
        dnd.append(chave)
        self.cartao.append(dnd)
        self.cartao.append(Gtk.Separator())

        if not itens:
            vazio = Gtk.Label(label="No notifications", margin_top=18, margin_bottom=18)
            vazio.add_css_class("dim-label")
            self.cartao.append(vazio)
            return
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, activate_on_single_click=True)
        for n in itens:
            linha = Gtk.Box(spacing=10)
            icone = n.get("app_icon") or n.get("desktop_entry") or "dialog-information-symbolic"
            imagem = Gtk.Image(pixel_size=32, valign=Gtk.Align.START)
            if icone.startswith("/") and os.path.exists(icone):
                imagem.set_from_file(icone)
            else:
                imagem.set_from_icon_name(icone)
            linha.append(imagem)
            textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
            resumo = Gtk.Label(label=n.get("summary", ""), xalign=0, ellipsize=3, use_markup=False)
            resumo.add_css_class("aparelho")
            textos.append(resumo)
            corpo = re.sub(r"<[^>]+>", "", n.get("body", ""))     # tira marcações tipo <b>
            if corpo:
                textos.append(Gtk.Label(label=corpo, xalign=0, wrap=True, lines=2, ellipsize=3,
                                        max_width_chars=34, use_markup=False))
            origem = Gtk.Label(label=n.get("desktop_entry") or n.get("app_name", ""), xalign=0)
            origem.add_css_class("titulo")
            textos.append(origem)
            linha.append(textos)
            r = Gtk.ListBoxRow(child=linha)
            r.app = n.get("desktop_entry")
            lista.append(r)
        lista.connect("row-activated", lambda _l, r: self.abrir_app(r.app))
        rolagem = Gtk.ScrolledWindow(child=lista, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                     propagate_natural_height=True, max_content_height=460)
        self.cartao.append(rolagem)

    def montar_dots(self):
        """DotStyle: a panel born from bubbles, Doto title, a dot switch for Do not disturb, notifications as
        rows with the app's real icon in a bubble, smooth scrolling with a dot scrollbar."""
        import dotkit as dk
        import hermes_dots as hd
        dk.aplicar_css()
        self.add_css_class("dots")
        historico = notif_historico()
        corte = self.limpas_ate() if HERMES is None else 0
        itens = [n for n in historico if n.get("id", 0) > corte]
        painel = dk.Painel(origem="right", semente=len(itens))
        painel.set_size_request(LARGURA + 30, -1)
        dentro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_top=16, margin_bottom=14,
                         margin_start=16, margin_end=12)
        painel.append(dentro)
        topo = Gtk.Box(spacing=8)
        titulo = Gtk.Label(label="NOTIFICATIONS", xalign=0, hexpand=True)
        titulo.add_css_class("dotkit-titulo")
        topo.append(titulo)
        limpar = dk.BotaoDots("Clear all", lambda: self.limpar(historico))
        limpar.set_sensitive(bool(itens))
        topo.append(limpar)
        dentro.append(topo)
        dnd = Gtk.Box(spacing=10, margin_start=2)
        rotulo = Gtk.Label(label="Do not disturb", xalign=0, hexpand=True)
        rotulo.add_css_class("dotkit-forte")
        dnd.append(rotulo)
        dnd.append(dk.Interruptor("dnd" in notif_modos(), self.nao_perturbe))
        dentro.append(dnd)
        if not itens:
            vazio = Gtk.Label(label="NOTHING NEW", margin_top=26, margin_bottom=22)
            vazio.add_css_class("dotkit-titulo")
            vazio.set_opacity(0.35)
            dentro.append(vazio)
        else:
            lista = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for n in itens:
                lista.append(self.item_dots(n, dk, hd))
            dentro.append(dk.Rolagem(lista, altura_max=520))
        self.cartao.append(painel)

    def item_dots(self, n, dk, hd):
        import re
        linha = Gtk.Box(spacing=11)
        linha.add_css_class("dotkit-item")
        icone = n.get("app_icon") or n.get("desktop_entry") or ""
        linha.append(dk.Bolha(icone, 38, n.get("app_name", "")))
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        resumo = Gtk.Label(label=n.get("summary", ""), xalign=0, ellipsize=3, use_markup=False, max_width_chars=30)
        resumo.add_css_class("dotkit-forte")
        textos.append(resumo)
        corpo = re.sub(r"<[^>]+>", "", n.get("body", ""))
        if corpo:
            c = Gtk.Label(label=corpo, xalign=0, wrap=True, lines=2, ellipsize=3, max_width_chars=32,
                          use_markup=False, natural_wrap_mode=Gtk.NaturalWrapMode.WORD)
            c.add_css_class("dotkit-texto")
            textos.append(c)
        quando = dk.tempo_atras(n.get("time")) if n.get("time") else ""
        origem = (n.get("desktop_entry") or n.get("app_name") or "").split(".")[-1]
        meta = Gtk.Label(label=f"{origem.upper()}  ·  {quando.upper()}" if quando else origem.upper(), xalign=0,
                         ellipsize=3, use_markup=False)
        meta.add_css_class("dotkit-mono")
        textos.append(meta)
        linha.append(textos)
        fechar = hd.PontoFechar(dk.RED, lambda i=n.get("id"): self.remover(i))
        fechar.set_opacity(0)
        linha.append(fechar)
        mov = Gtk.EventControllerMotion()
        mov.connect("enter", lambda *_: fechar.set_opacity(1))
        mov.connect("leave", lambda *_: fechar.set_opacity(0))
        linha.add_controller(mov)
        clique = Gtk.GestureClick()
        clique.connect("released", lambda *_a, app=n.get("desktop_entry"): self.abrir_app(app))
        linha.add_controller(clique)
        return linha

    def remover(self, id_):
        if HERMES is not None and id_:
            HERMES.remover_do_historico(id_)
        self.montar()

    def abrir_app(self, app):
        """Clique numa notificação = traz o app dela pra frente (ou abre)."""
        self.fechar()
        # desktop_entry vem de QUEM mandou a notificação: só um nome de .desktop válido e instalado
        # chega ao "hyprctl dispatch exec" (que roda num shell). Nada de espaços, ; $ ` etc.
        if app_instalado(app):
            subprocess.Popen(["hyprctl", "dispatch", "exec", f"gtk-launch {app}"], stdout=subprocess.DEVNULL)

    def limpar(self, historico):
        if HERMES is not None:
            HERMES.limpar_historico()
            HERMES.dispensar_tudo()
            self.montar()
            return
        os.makedirs(os.path.dirname(self.LIMPAS), exist_ok=True)
        with open(self.LIMPAS, "w") as f:
            f.write(str(max((n.get("id", 0) for n in historico), default=0)))
        rodar("makoctl", "dismiss", "--all")
        self.montar()

    def nao_perturbe(self, ligar):
        notif_modo("dnd", ligar)
        rodar("pkill", "-RTMIN+10", "-x", "waybar")     # a barra troca o sininho na hora


class MenuJanela(Janelinha):
    """Botão direito na BARRA DE TÍTULO de uma janela (doc 26): menu tipo Windows.

    O Hyprland avisa TODO clique direito (atalho "bindn" = não engole o clique, o app recebe normal);
    aqui a gente confere se o mouse está em cima de uma barra de título (hyprbars, 32 px acima da
    janela). Se não estiver, não faz nada."""

    BARRA = 32
    BIN = os.path.expanduser("~/.local/bin")

    def __init__(self):
        super().__init__()
        Camada.set_namespace(self, "menu-janela")
        self.add_css_class("menu-janela")
        Camada.set_anchor(self, Camada.Edge.RIGHT, False)
        Camada.set_anchor(self, Camada.Edge.LEFT, True)       # posição = onde o mouse está
        self.cartao.set_size_request(230, -1)
        self.janela = None

    def janela_na_barra(self, x, y):
        import json
        visiveis = {m["activeWorkspace"]["id"] for m in json.loads(rodar("hyprctl", "monitors", "-j") or "[]")}
        achadas = []
        for c in json.loads(rodar("hyprctl", "clients", "-j") or "[]"):
            if c["workspace"]["id"] not in visiveis or c["hidden"] or c["fullscreen"]:
                continue
            (cx, cy), (w, _h) = c["at"], c["size"]
            if cx <= x <= cx + w and cy - self.BARRA - 2 <= y < cy:      # só a faixa da barra
                achadas.append(c)
        achadas.sort(key=lambda c: c["focusHistoryID"])              # a de cima (mais recente) ganha
        return achadas[0] if achadas else None

    def abrir_no_mouse(self):
        if self.get_visible():          # menu aberto + outro clique direito = fecha (como no Windows)
            self.fechar()
            return
        x, y = (int(v) for v in rodar("hyprctl", "cursorpos").split(","))
        c = self.janela_na_barra(x, y)
        print("menu-janela:", x, y, "→", c["class"] if c else "nenhuma barra aqui", flush=True)
        if c is None:
            return                                                    # clique direito fora de barra
        # Steam, nossos apps etc. não têm a barra do sistema (hyprbars:no_bar): nada de menu
        if c["class"].startswith("com.aero.") or c["class"] in ("steam", "discord"):
            return
        self.janela = c
        monitor = next((m for m in hyprctl_json("monitors") or [] if m["id"] == c["monitor"]), None)
        topo = monitor["reserved"][1] if monitor else 0                  # a camada começa abaixo da barra de cima
        Camada.set_margin(self, Camada.Edge.LEFT, max(0, x - 14))
        Camada.set_margin(self, Camada.Edge.TOP, max(0, y - topo - 8))
        self.montar()
        self.mostrar((x - 40, x + 40))

    def acao(self, *comandos):
        self.fechar()
        for cmd in comandos:
            rodar(*cmd)

    def montar(self):
        while (filho := self.cartao.get_first_child()) is not None:
            self.cartao.remove(filho)
        c = self.janela
        alvo = f"address:{c['address']}"
        focar = ("hyprctl", "dispatch", "focuswindow", alvo)
        maximizada = c["fullscreen"] != 0 or c.get("fullscreenClient", 0) != 0
        visivel = "sempre-visivel" in c.get("tags", [])
        itens = [
            ("window-minimize-symbolic", "Minimize", [(f"{self.BIN}/hypr-minimizar", c["address"])]),
            ("window-restore-symbolic" if maximizada else "window-maximize-symbolic",
             "Restore" if maximizada else "Maximize", [(f"{self.BIN}/hypr-maximizar", c["address"])]),
            ("object-select-symbolic" if visivel else "view-reveal-symbolic",
             "Always visible", [(f"{self.BIN}/hypr-sempre-visivel", c["address"])]),
            ("view-dual-symbolic", "Tile" if c["floating"] else "Float",
             [("hyprctl", "dispatch", "togglefloating", alvo)]),
            None,
            ("go-previous-symbolic", "Snap left", [focar, (f"{self.BIN}/hypr-encaixar", "left")]),
            ("go-next-symbolic", "Snap right", [focar, (f"{self.BIN}/hypr-encaixar", "right")]),
            None,
        ]
        for item in itens:
            if item is None:
                self.cartao.append(Gtk.Separator(margin_top=3, margin_bottom=3))
                continue
            self.cartao.append(self.botao(*item))
        # Mover pra outra área de trabalho: uma fileira de números (a atual fica marcada)
        areas = Gtk.Box(spacing=4, margin_start=8, margin_top=2, margin_bottom=2)
        rotulo = Gtk.Label(label="Move to", xalign=0, hexpand=True)
        rotulo.add_css_class("dim-label")
        areas.append(rotulo)
        for n in range(1, 6):
            b = Gtk.Button(label=str(n), sensitive=c["workspace"]["id"] != n)
            b.add_css_class("flat")
            b.add_css_class("circular")
            b.connect("clicked", lambda _b, n=n: self.acao(
                ("hyprctl", "dispatch", "movetoworkspacesilent", f"{n},{alvo}")))
            areas.append(b)
        self.cartao.append(areas)
        self.cartao.append(Gtk.Separator(margin_top=3, margin_bottom=3))
        fechar = self.botao("window-close-symbolic", "Close", [("hyprctl", "dispatch", "closewindow", alvo)])
        fechar.add_css_class("perigo")
        self.cartao.append(fechar)

    def botao(self, icone, texto, comandos):
        b = Gtk.Button()
        caixa = Gtk.Box(spacing=10)
        caixa.append(Gtk.Image(icon_name=icone))
        caixa.append(Gtk.Label(label=texto, xalign=0, hexpand=True, use_markup=False))
        b.set_child(caixa)
        b.add_css_class("flat")
        b.connect("clicked", lambda *_: self.acao(*comandos))
        return b


class CentralRapida(Janelinha):
    """Central rápida (doc 30): o que está tocando + brilho/volume + botões grandes (Wi-Fi, Bluetooth...).

    O PLAYER: quase todo app de música/vídeo do Linux publica o que está tocando num padrão chamado
    MPRIS (no D-Bus). O Firefox também: YouTube Music, YouTube, Spotify web, SoundCloud... aparecem
    aqui sem instalar nada. O "playerctl" lê e controla esse padrão.
    Enquanto a central está aberta, ela se atualiza a cada segundo (fechada, não gasta nada)."""

    LARG = 380
    BIN = os.path.expanduser("~/.local/bin")

    def __init__(self):
        super().__init__()
        Camada.set_namespace(self, "central-rapida")
        self.cartao.remove_css_class("cartao-som")       # aqui são DOIS cartões separados
        self.cartao.add_css_class("central")
        self.cartao.set_spacing(10)
        self.cartao.set_size_request(self.LARG, -1)
        self.cartao.set_valign(Gtk.Align.START)
        self.relogio = None
        self.capa_atual = None
        # A janela da central tem ALTURA FIXA (quase a tela toda): mudar o tamanho de uma camada faz o
        # Hyprland dar um "pulinho" nela. O espaço abaixo dos cartões é transparente e NÃO pega cliques
        # (região de entrada = só os cartões, atualizada a cada quadro em que a altura muda).
        self.altura_conteudo = 0
        self.add_tick_callback(self.ajustar_entrada)
        # Toda vez que a central reabre, o Wayland cria a superfície de novo e ZERA a região de entrada.
        # Sem esquecer o tamanho anterior, ela não era reaplicada (mesmo tamanho = "nada mudou") e a central
        # aparecia sem receber cliques: "parece só uma imagem". Ao mapear, força reaplicar.
        self.connect("map", lambda *_: setattr(self, "_entrada", None))
        vazio = Gtk.GestureClick(button=0)
        vazio.connect("pressed", self.clique_no_vazio)
        self.add_controller(vazio)

    def ajustar_entrada(self, *_):
        import cairo
        # NENHUM clique pode varar a central para a janela de trás. Antes, a região que pega cliques
        # era só a altura dos cartões; quando a central se redesenhava (lista abrindo, aparelho
        # mudando), essa altura encolhia por um instante e o clique caía no app de trás.
        # Agora a janela INTEIRA pega cliques, sempre; clicar no vazio abaixo dos cartões só fecha a
        # central (ver clique_no_vazio), igual a clicar fora dela.
        ok, caixa = self.cartao.compute_bounds(self)
        fim = int(caixa.get_y() + caixa.get_height()) if ok else 0
        if fim > 16 and fim != self.altura_conteudo:
            self.altura_conteudo = fim
            try:      # o hypr-botoes também precisa saber até onde vai a central (clique "dentro" ou "fora")
                with open(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "central-altura"), "w") as f:
                    f.write(str(fim))
            except OSError:
                pass
        w, h = self.get_width(), self.get_height()
        superficie = self.get_surface()
        if superficie is not None and h > 0 and (h, w) != getattr(self, "_entrada", None):
            self._entrada = (h, w)
            superficie.set_input_region(cairo.Region(cairo.RectangleInt(0, 0, w, h)))
        return GLib.SOURCE_CONTINUE

    def clique_no_vazio(self, _gesto, _n, _x, y):
        """Clique na parte transparente abaixo dos cartões = clicou fora: fecha (sem passar o clique)."""
        if self.altura_conteudo and y > self.altura_conteudo:
            self.fechar()

    # ---------- estado do sistema (tudo por comandos simples) ----------

    # Duração e posição: o Firefox às vezes PARA de informar no meio do vídeo (YouTube). Então a central
    # lembra a duração de cada vídeo, pergunta ao YouTube quando não sabe, e conta o tempo sozinha.
    CACHE_DURACOES = os.path.expanduser("~/.cache/aero-shell/duracoes.json")
    try:
        import json as _json
        duracoes = _json.load(open(CACHE_DURACOES))     # url do vídeo -> segundos (salvo entre reinícios)
    except (OSError, ValueError):
        duracoes = {}
    buscando = set()
    ref_pos = None           # (url, posição em segundos, hora em que valia, tocando?)

    def buscar_duracao_youtube(self, url):
        """A página do vídeo traz "lengthSeconds":"1295". Roda numa thread, uma vez por vídeo."""
        import re
        import threading
        import urllib.request
        if url in self.buscando or "youtube.com/watch" not in url:
            return
        self.buscando.add(url)

        def trabalho():
            try:
                pedido = urllib.request.Request(url.split("&")[0], headers={"User-Agent": "Mozilla/5.0"})
                html = urllib.request.urlopen(pedido, timeout=8).read().decode("utf-8", "replace")
                m = re.search(r'"lengthSeconds":"(\d+)"', html)
                if m:
                    self.duracoes[url] = float(m.group(1))
                    self.salvar_duracoes()
            except Exception:
                pass
        threading.Thread(target=trabalho, daemon=True).start()

    def salvar_duracoes(self):
        import json
        os.makedirs(os.path.dirname(self.CACHE_DURACOES), exist_ok=True)
        itens = list(self.duracoes.items())[-300:]          # guarda só os 300 mais recentes
        with open(self.CACHE_DURACOES + ".tmp", "w") as f:
            json.dump(dict(itens), f)
        os.replace(self.CACHE_DURACOES + ".tmp", self.CACHE_DURACOES)

    # Fonte escolhida (várias tocando: YTMusic, TerMusic, Firefox, Spotify...). Vazio/fechada = a que o
    # playerctl escolhe (a que tocou por último). Clicar numa pílula do cartão do player troca.
    ESCOLHA = os.path.expanduser("~/.cache/aero-shell/player-escolhido")

    @staticmethod
    def fontes():
        return [p for p in rodar("playerctl", "-l").splitlines() if p.strip()]

    def fonte(self):
        try:
            f = open(self.ESCOLHA).read().strip()
        except OSError:
            return None
        return f if f in self.fontes() else None

    def pc(self, *args):
        """playerctl na fonte escolhida."""
        f = self.fonte()
        return rodar("playerctl", *(["-p", f] if f else []), *args)

    def pc_fundo(self, *args):
        """playerctl a partir de um clique: numa thread (a tela nunca espera o player responder)."""
        import threading
        threading.Thread(target=lambda: self.pc(*args), daemon=True).start()

    @staticmethod
    def nome_fonte(player, url=""):
        base = player.split(".")[0]
        if base == "mpv" and "youtube.com" in (url or ""):
            return "YTMusic"                     # o motor do YTMusic/TerMusic toca pelo mpv
        return {"firefox": "Firefox", "spotify": "Spotify", "chromium": "Chromium"}.get(base, base.capitalize())

    def escolher_fonte(self, player):
        os.makedirs(os.path.dirname(self.ESCOLHA), exist_ok=True)
        with open(self.ESCOLHA, "w") as f:
            f.write(player)
        GLib.timeout_add(100, lambda: (self.atualizar_player(sozinho=True), False)[1])

    @staticmethod
    def capa_do_motor(url):
        """A capa da música que o motor do YTMusic/TerMusic está tocando (pergunta pro motor, rápido)."""
        try:
            sys.path.insert(0, os.path.expanduser("~/.local/share/ytmotor"))
            import socket
            import json as _j
            s = socket.socket(socket.AF_UNIX)
            s.settimeout(1)
            s.connect(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ytmotor.sock"))
            s.sendall(b'{"cmd": "estado"}\n')
            buf = b""
            while not buf.endswith(b"\n"):
                d = s.recv(1 << 20)
                if not d:
                    break
                buf += d
            s.close()
            atual = (_j.loads(buf).get("r") or {}).get("atual") or {}
            return atual.get("capa") or ""
        except Exception:
            return ""

    def capa_local(self, url):
        """Capa da internet (http) -> arquivo no cache; baixa numa thread e redesenha quando chega."""
        import hashlib
        import threading
        import urllib.request
        pasta = os.path.expanduser("~/.cache/aero-shell/capas")
        arq = os.path.join(pasta, hashlib.sha1(url.encode()).hexdigest() + ".img")
        if os.path.exists(arq):
            return arq
        if url not in self.buscando:
            self.buscando.add(url)

            def baixar():
                try:
                    os.makedirs(pasta, exist_ok=True)
                    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}),
                                                timeout=8) as r, open(arq + ".tmp", "wb") as f:
                        f.write(r.read(5_000_000))
                    os.replace(arq + ".tmp", arq)
                    GLib.idle_add(lambda: (self.trocar_player(self.capa_atual), False)[1])
                except Exception:
                    pass
            threading.Thread(target=baixar, daemon=True).start()
        return None

    def player(self):
        """O player da fonte escolhida (ou o "principal" do playerctl: o que tocou por último), ou None."""
        import time
        fmt = "{{playerName}}\t{{status}}\t{{artist}}\t{{title}}\t{{mpris:artUrl}}\t{{mpris:length}}\t{{xesam:url}}"
        f = self.fonte()
        try:
            r = subprocess.run(["playerctl", *(["-p", f] if f else []), "metadata", "--format", fmt],
                               capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return None
        partes = r.stdout.rstrip("\n").split("\t")
        if r.returncode != 0 or len(partes) < 7 or not (partes[3] or partes[2]):
            return None
        nome, estado, artista, titulo, capa, duracao, url = partes[:7]
        url = url or titulo
        if not capa and nome.startswith("mpv") and "youtube.com" in url:
            capa = self.capa_do_motor(url)           # YTMusic/TerMusic: o mpv não informa a capa
        tocando = estado == "Playing"
        if duracao.isdigit() and int(duracao) > 0:
            if self.duracoes.get(url) != int(duracao) / 1e6:
                self.duracoes[url] = int(duracao) / 1e6
                self.salvar_duracoes()
        elif url not in self.duracoes:
            self.buscar_duracao_youtube(url)
        dur = self.duracoes.get(url, 0.0)
        try:
            pos = float(self.pc("position").strip() or 0)
        except ValueError:
            pos = 0.0
        agora = time.monotonic()
        ref = self.ref_pos
        if pos > 0.5 or ref is None or ref[0] != url:
            self.ref_pos = (url, pos, agora, tocando)          # posição informada: vira a referência
        else:
            # o player disse ~0: estima a partir da última posição conhecida + o tempo que passou tocando
            _, p0, t0, tocava = ref
            pos = p0 + (agora - t0 if tocava else 0)
            self.ref_pos = (url, pos, agora, tocando)
        return {"nome": self.nome_fonte(nome, url), "player": nome, "fontes": tuple(self.fontes()), "tocando": tocando,
                "artista": artista, "titulo": titulo, "capa": capa, "url": url,
                "duracao": dur, "posicao": min(pos, dur) if dur else pos}

    @staticmethod
    def wifi():
        ligado = rodar("nmcli", "-t", "-f", "WIFI", "g").strip() == "enabled"
        # "--rescan no": só lê a lista que o NetworkManager já tem. Sem isso, se a lista tem mais de 30 s o nmcli
        # faz uma varredura nova ANTES de responder (~4,6 s aqui) e a Central travava: o clique parecia não abrir,
        # e o 2º clique (feito na espera) fechava ela logo que abria.
        # "con show --active" só diz a conexão ligada (~15 ms). Listar redes Wi-Fi ("dev wifi") era lento
        # (até 1,8 s) e deixava a central demorando pra aparecer.
        rede = next((l.rsplit(":", 1)[0] for l in rodar("nmcli", "-t", "-f", "NAME,TYPE", "con", "show",
                                                         "--active").splitlines()
                     if l.endswith(":802-11-wireless")), "")
        return ligado, rede

    @staticmethod
    def bluetooth():
        info = rodar("bluetoothctl", "show")
        ligado = "Powered: yes" in info
        conectados = [l.split(" ", 2)[2] for l in rodar("bluetoothctl", "devices", "Connected").splitlines()
                      if l.startswith("Device ")]
        return ligado, conectados

    @staticmethod
    def filtro_ligado():
        try:
            return open(os.path.expanduser("~/.cache/hypr-tema/filtro")).read().strip() not in ("", "0")
        except OSError:
            return False

    # ---------- montar ----------

    def cartao_player(self, pl):
        """Capa no fundo (escurecida), título/artista e os botões ⏮ ⏯ ⏭."""
        fundo = Gtk.Overlay(overflow=Gtk.Overflow.HIDDEN)   # HIDDEN = corta a capa nos cantos redondos
        fundo.add_css_class("player")
        imagem = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        imagem.set_size_request(-1, 150)
        capa = pl["capa"]
        if capa.startswith("file://"):
            caminho = GLib.filename_from_uri(capa)[0]
            if os.path.exists(caminho):
                imagem.set_filename(caminho)
        elif capa.startswith("http"):
            caminho = self.capa_local(capa)
            if caminho:
                imagem.set_filename(caminho)
        fundo.set_child(imagem)
        veu = Gtk.Box()
        veu.add_css_class("veu")                      # gradiente escuro por cima da capa (texto legível)
        fundo.add_overlay(veu)
        caixa = Gtk.Box(spacing=8, margin_start=16, margin_end=12, margin_top=14, margin_bottom=12)
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, spacing=2)
        titulo = Gtk.Label(label=pl["titulo"] or "Unknown", xalign=0, ellipsize=3, use_markup=False)
        titulo.add_css_class("titulo-musica")
        artista = Gtk.Label(label=pl["artista"] or "", xalign=0, ellipsize=3, use_markup=False)
        artista.add_css_class("artista")
        if len(pl["fontes"]) > 1:
            # várias fontes: uma pílula pra cada; a acesa é a que a central controla
            via = Gtk.Box(spacing=4, valign=Gtk.Align.END, vexpand=True)
            for fonte in pl["fontes"]:
                b = Gtk.Button(label=self.nome_fonte(fonte, pl["url"] if fonte == pl["player"] else ""),
                               tooltip_text="Control this source")
                b.add_css_class("fonte")
                if fonte == pl["player"]:
                    b.add_css_class("fonte-atual")
                b.connect("clicked", lambda _b, f=fonte: self.escolher_fonte(f))
                via.append(b)
        else:
            via = Gtk.Label(label=f"Playing via {pl['nome']}", xalign=0, valign=Gtk.Align.END, vexpand=True)
            via.add_css_class("via")
        for w in (titulo, artista, via):
            textos.append(w)
        # Barra de progresso (onde está o vídeo/música): arrastar ou clicar = pular pra aquele ponto
        self.barra_pos = self.tempo = None
        # Faixa da base: SEMPRE tem ⏪ 10s e 10s ⏩ (funcionam mesmo quando o player não informa a duração,
        # o que o Firefox faz em vários vídeos do YouTube). A barra aparece junto quando a duração existe.
        linha = Gtk.Box(spacing=6, valign=Gtk.Align.END, margin_start=10, margin_end=12, margin_bottom=8)
        linha.add_css_class("faixa-progresso")
        voltar = Gtk.Button(icon_name="media-seek-backward-symbolic", tooltip_text="Back 10 seconds")
        voltar.add_css_class("pulo")
        voltar.connect("clicked", lambda *_: self.pular_relativo(-10))
        linha.append(voltar)
        if True:   # a barra fica SEMPRE (sem duração ainda: travada, com "--:--", até a duração chegar)
            self.barra_pos = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, max(pl["duracao"], 1), 1)
            self.barra_pos.set_sensitive(pl["duracao"] > 0)
            self.barra_pos.set_hexpand(True)
            self.barra_pos.set_draw_value(False)
            self.barra_pos.set_value(min(pl["posicao"], pl["duracao"]))
            self.barra_pos.add_css_class("progresso")
            self.tempo = Gtk.Label(label=self.texto_tempo(pl["posicao"], pl["duracao"]) if pl["duracao"] > 0
                                   else self.texto_tempo(pl["posicao"], 0).split(" / ")[0] + " / --:--")
            self.tempo.add_css_class("via")
            self.tempo.add_css_class("tempo")
            # largura FIXA pro tempo: a barra não muda de tamanho a cada segundo.
            # "00:00 / 00:00" = 13 caracteres; vídeo de mais de 1 hora = "0:00:00 / 0:00:00" (17)
            self.tempo.set_width_chars(17 if pl["duracao"] >= 3600 else 13)
            self.tempo.set_xalign(1)
            # "change-value" = só quando VOCÊ mexe (não quando o programa atualiza a posição)
            self.barra_pos.connect("change-value", self.ao_pular)
            linha.append(self.barra_pos)
            linha.append(self.tempo)
        avancar = Gtk.Button(icon_name="media-seek-forward-symbolic", tooltip_text="Forward 10 seconds")
        avancar.add_css_class("pulo")
        avancar.connect("clicked", lambda *_: self.pular_relativo(10))
        linha.append(avancar)
        caixa.append(textos)
        botoes = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, valign=Gtk.Align.CENTER)
        for icone, cmd in (("media-skip-backward-symbolic", "previous"),
                           ("media-playback-pause-symbolic" if pl["tocando"] else "media-playback-start-symbolic",
                            "play-pause"),
                           ("media-skip-forward-symbolic", "next")):
            b = Gtk.Button(icon_name=icone)
            b.add_css_class("botao-player")
            b.connect("clicked", self.ao_botao_player, cmd)
            botoes.append(b)
            if cmd == "play-pause":
                self.bt_play = b
        caixa.append(botoes)
        fundo.add_overlay(caixa)
        fundo.add_overlay(linha)            # por último = por cima de tudo (recebe o mouse primeiro)
        caixa.set_margin_bottom(44)         # o texto/botões não ocupam a faixa da base
        return fundo

    def deslizante(self, icone, valor, aplicar, minimo=1, maximo=100):
        """Slider da central. O comando (pactl, DDC...) roda NUMA THREAD, só o valor mais recente (slider.py).
        Antes: Gtk.Scale rodando o comando a cada pixel na tela + uma trava "mexendo" que às vezes nunca
        soltava (o Gtk.Scale engole o "soltou") — e a central parava de responder de vez."""
        from slider import SliderFluido
        linha = Gtk.Box(spacing=10)
        linha.append(Gtk.Image(icon_name=icone))
        escala = SliderFluido(minimo, maximo, valor=valor, aplicar=lambda v: aplicar(round(v)))
        self.sliders = [s for s in getattr(self, "sliders", []) if s.get_root() is not None] + [escala]
        linha.append(escala)
        return linha

    @property
    def mexendo(self):
        """Você está segurando algum slider? (então nada se redesenha por baixo)"""
        return any(s.segurando for s in getattr(self, "sliders", []) if s.get_root() is not None)

    def botao_grande(self, icone, titulo, estado, ligado, ao_clicar, aba=None):
        """Botão "pílula" Material: ligado = cor de destaque; a setinha abre a página do Settings."""
        b = Gtk.Button()
        b.add_css_class("bloco")
        if ligado:
            b.add_css_class("ligado")
        caixa = Gtk.Box(spacing=10)
        caixa.append(Gtk.Image(icon_name=icone))
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        t = Gtk.Label(label=titulo, xalign=0, ellipsize=3, use_markup=False)
        t.add_css_class("bloco-titulo")
        e = Gtk.Label(label=estado, xalign=0, ellipsize=3, use_markup=False)
        e.add_css_class("bloco-estado")
        textos.append(t)
        textos.append(e)
        caixa.append(textos)
        if aba:
            # A setinha fica DENTRO do botão grande. Um botão dentro de outro deixava o clique valer
            # para os dois (a setinha abria a lista E o botão desligava o Bluetooth). Aqui ela pega o
            # clique antes de todo mundo (fase de captura) e não deixa ele chegar ao botão de fora.
            seta = Gtk.Button(icon_name="go-next-symbolic", valign=Gtk.Align.CENTER)
            seta.add_css_class("seta")
            pega = Gtk.GestureClick(button=0, propagation_phase=Gtk.PropagationPhase.CAPTURE)

            def apertou(g, *_):
                g.set_state(Gtk.EventSequenceState.CLAIMED)

            def soltou(g, *_):
                g.set_state(Gtk.EventSequenceState.CLAIMED)
                if aba.startswith("sub:"):
                    self.alternar_sub(aba[4:])
                else:
                    self.abrir_settings(aba)
            pega.connect("pressed", apertou)
            pega.connect("released", soltou)
            seta.add_controller(pega)
            caixa.append(seta)
        b.set_child(caixa)
        if isinstance(ao_clicar, str):   # Wi-Fi/Bluetooth/Sound: o clique só abre/fecha a subjanelinha
            b.connect("clicked", lambda *_, q=ao_clicar: self.alternar_sub(q))   # (desligar fica lá dentro)
        else:
            b.connect("clicked", lambda *_: self.ao_bloco(b, e, ao_clicar))
        return b

    # ---------- lista de redes Wi-Fi (abre ao clicar no botão Wi-Fi) ----------
    def alternar_sub(self, qual):
        self.sub = None if getattr(self, "sub", None) == qual else qual
        if self.sub == "wifi":
            self.procurar_redes()
        elif self.sub == "bt":
            self.procurar_bt()
            self.buscar_bt()
        self.atualizar_sub()

    @staticmethod
    def cabecalho(titulo, ligado, dica, ao_clicar, icone="system-shutdown-symbolic"):
        """Topo de uma subjanelinha: título + botão redondo de desligar (o 2º passo, de propósito)."""
        topo = Gtk.Box(spacing=6)
        t = Gtk.Label(label=titulo, xalign=0, hexpand=True)
        t.add_css_class("redes-titulo")
        topo.append(t)
        b = Gtk.Button(icon_name=icone, tooltip_text=dica)
        b.add_css_class("desligar")
        if ligado:
            b.add_css_class("ligado")
        b.connect("clicked", lambda *_: ao_clicar())
        topo.append(b)
        return topo

    @staticmethod
    def item(icone, nome, ativo, ao_clicar, extra=None):
        linha = Gtk.Box(spacing=8)
        linha.append(Gtk.Image(icon_name=icone))
        rot = Gtk.Label(label=nome, xalign=0, hexpand=True, ellipsize=3, use_markup=False)
        if ativo:
            rot.add_css_class("bloco-titulo")
        linha.append(rot)
        if extra:
            linha.append(Gtk.Image(icon_name=extra, opacity=0.6))
        if ativo:
            linha.append(Gtk.Image(icon_name="object-select-symbolic"))
        b = Gtk.Button(child=linha)
        b.add_css_class("rede")
        b.connect("clicked", lambda *_: ao_clicar())
        return b

    def em_thread(self, *comandos, depois=None, espera=600, limite=4):
        """Roda comandos em segundo plano e redesenha logo depois (sem travar a central)."""
        import threading
        threading.Thread(target=lambda: [rodar(*c, timeout=limite) for c in comandos] and depois and depois(), daemon=True).start()
        GLib.timeout_add(espera, self.montar_seguro)

    # Bluetooth: aparelhos pareados (clique conecta/desconecta) e os que estão por perto (clique pareia)
    def procurar_bt(self):
        import threading

        def aparelhos(*filtro):
            out = []
            for l in rodar("bluetoothctl", "devices", *filtro).splitlines():
                partes = l.split(" ", 2)
                # sem nome de verdade (só o endereço) = não mostra
                if len(partes) == 3 and partes[0] == "Device" and partes[2].replace("-", ":") != partes[1]:
                    out.append((partes[1], partes[2]))
            return out

        def trabalho():
            conectados = {m for m, _ in aparelhos("Connected")}
            pareados = aparelhos("Paired")
            self.aparelhos = [{"mac": m, "nome": n, "uso": m in conectados} for m, n in pareados]
            self.aparelhos.sort(key=lambda a: not a["uso"])
            ja = {m for m, _ in pareados}
            self.por_perto = [{"mac": m, "nome": n} for m, n in aparelhos() if m not in ja]
            GLib.idle_add(self.atualizar_sub)
        threading.Thread(target=trabalho, daemon=True).start()

    def buscar_bt(self):
        """Abrir a lista já procura aparelhos por perto (20 s), e a lista vai se atualizando sozinha."""
        import threading
        if getattr(self, "bt_buscando", False) or not self.estado["bluetooth"][0]:
            return
        self.bt_buscando = True

        def busca():
            rodar("bluetoothctl", "pairable", "on")
            rodar("bluetoothctl", "--timeout", "20", "scan", "on", timeout=25)
            self.bt_buscando = False
            self.procurar_bt()
        threading.Thread(target=busca, daemon=True).start()

        def atualizar():
            if not self.bt_buscando or getattr(self, "sub", None) != "bt" or not self.get_visible():
                return GLib.SOURCE_REMOVE
            self.procurar_bt()
            return GLib.SOURCE_CONTINUE
        GLib.timeout_add(2000, atualizar)

    def lista_bt(self, bt_on):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        caixa.add_css_class("redes")
        caixa.append(self.cabecalho("Bluetooth devices", bt_on, "Turn Bluetooth off" if bt_on else "Turn Bluetooth on",
                                    lambda: self.em_thread(("bluetoothctl", "power", "off" if bt_on else "on"),
                                                           depois=lambda: (self.procurar_bt(), self.buscar_bt()))))
        aparelhos = getattr(self, "aparelhos", None)
        if not bt_on or not aparelhos:
            caixa.append(Gtk.Label(label="Bluetooth is off" if not bt_on else
                                   ("Searching…" if aparelhos is None else "No paired devices"),
                                   xalign=0, margin_start=6, opacity=0.7))
        else:
            for a in aparelhos:
                caixa.append(self.item("bluetooth-active-symbolic", a["nome"], a["uso"],
                                       lambda a=a: self.em_thread(("bluetoothctl", "disconnect", a["mac"]) if a["uso"] else
                                                                  # aero-bluetooth: liga o Bluetooth, marca como confiável e tenta de novo se falhar
                                                                  (os.path.expanduser("~/.local/bin/aero-bluetooth"), "connect", a["mac"]),
                                                                  depois=self.procurar_bt, espera=3000, limite=45)))
        if bt_on:
            perto = getattr(self, "por_perto", [])
            titulo = Gtk.Label(label="Nearby" + ("  ·  searching…" if getattr(self, "bt_buscando", False) else ""),
                               xalign=0, margin_start=6, margin_top=6, opacity=0.7)
            caixa.append(titulo)
            if not perto and not getattr(self, "bt_buscando", False):
                caixa.append(Gtk.Label(label="Nothing nearby. Put the device in pairing mode.", xalign=0,
                                       margin_start=6, opacity=0.6, wrap=True))
            for a in perto:
                # clique pareia: o aero-bluetooth pareia, confia e conecta (o agente dele responde ao aparelho)
                caixa.append(self.item("bluetooth-symbolic", a["nome"], False,
                                       lambda a=a: self.em_thread((os.path.expanduser("~/.local/bin/aero-bluetooth"), "pair", a["mac"]),
                                                                  depois=self.procurar_bt, espera=4000, limite=60),
                                       extra="list-add-symbolic"))
        mais = Gtk.Button(child=Gtk.Label(label="Bluetooth settings", xalign=0))
        mais.add_css_class("rede")
        mais.connect("clicked", lambda *_: self.abrir_settings("bluetooth"))
        caixa.append(mais)
        return caixa

    # Som: saída e entrada juntas (clique escolhe o aparelho; botões redondos = mudo)
    def lista_som(self, e):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        caixa.add_css_class("redes")
        for chave, titulo, tipo, icone in (("sinks", "Output", "sink", "audio-speakers-symbolic"),
                                           ("sources", "Input", "source", "audio-input-microphone-symbolic")):
            atual = next((d for d in e[chave] if d["padrao"]), None)
            mudo = bool(atual and atual.get("mute"))
            caixa.append(self.cabecalho(titulo, not mudo, ("Unmute" if mudo else "Mute"),
                                        lambda t=tipo: self.em_thread(("pactl", f"set-{t}-mute", f"@DEFAULT_{t.upper()}@",
                                                                       "toggle"), espera=300),
                                        icone if not mudo else ("audio-volume-muted-symbolic" if tipo == "sink"
                                                                else "microphone-sensitivity-muted-symbolic")))
            for d in e[chave]:
                if d["name"].endswith(".monitor"):
                    continue
                caixa.append(self.item(icone, nome_bonito(d.get("description", d["name"]), d["name"], tipo == "source"),
                                       d["padrao"], lambda d=d, t=tipo: self.em_thread(("pactl", f"set-default-{t}",
                                                                                       d["name"]), espera=300)))
        mais = Gtk.Button(child=Gtk.Label(label="Sound settings", xalign=0))
        mais.add_css_class("rede")
        mais.connect("clicked", lambda *_: self.abrir_settings("som"))
        caixa.append(mais)
        return caixa

    def procurar_redes(self):
        """Lista as redes em volta numa thread (o nmcli pode levar 1–2 s procurando)."""
        import re
        import threading

        def trabalho():
            saida = rodar("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "auto")
            redes = {}
            for linha in saida.splitlines():
                # o -t separa por ":" e escapa ":" dentro do nome como "\:"
                campos = [c.replace("\\:", ":") for c in re.split(r"(?<!\\):", linha)]
                if len(campos) < 4 or not campos[1]:
                    continue
                uso, ssid, sinal, seg = campos[0] == "*", campos[1], int(campos[2] or 0), campos[3]
                if ssid not in redes or uso or sinal > redes[ssid]["sinal"]:
                    redes[ssid] = {"ssid": ssid, "uso": uso or redes.get(ssid, {}).get("uso", False),
                                   "sinal": sinal, "seguranca": seg not in ("", "--"), "tipo": seg}
            self.redes = sorted(redes.values(), key=lambda r: (not r["uso"], -r["sinal"]))
            GLib.idle_add(self.atualizar_sub)
        threading.Thread(target=trabalho, daemon=True).start()

    def lista_wifi(self, wifi_on):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        caixa.add_css_class("redes")
        # o "desligar" de verdade: 2º passo, de propósito, pra ninguém derrubar a internet sem querer
        caixa.append(self.cabecalho("Wi-Fi networks", wifi_on, "Turn Wi-Fi off" if wifi_on else "Turn Wi-Fi on",
                                    lambda: self.ligar_wifi(not wifi_on)))
        if not wifi_on:
            caixa.append(Gtk.Label(label="Wi-Fi is off", xalign=0, margin_start=6, opacity=0.7))
            return caixa
        aviso = getattr(self, "aviso_wifi", "")
        if aviso:
            rotulo = Gtk.Label(label=aviso, xalign=0, margin_start=6, opacity=0.8, wrap=True, use_markup=False)
            caixa.append(rotulo)
        redes = getattr(self, "redes", None)
        if not redes:
            caixa.append(Gtk.Label(label="Searching…" if redes is None else "No networks found",
                                   xalign=0, margin_start=6, opacity=0.7))
            return caixa
        rolar = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, propagate_natural_height=True,
                                   max_content_height=220)
        lista = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for r in redes:
            nivel = ("excellent" if r["sinal"] >= 75 else "good" if r["sinal"] >= 50
                     else "ok" if r["sinal"] >= 25 else "weak")
            linha = Gtk.Box(spacing=8)
            linha.append(Gtk.Image(icon_name=f"network-wireless-signal-{nivel}-symbolic"))
            nome = Gtk.Label(label=r["ssid"], xalign=0, hexpand=True, ellipsize=3)
            if r["uso"]:
                nome.add_css_class("bloco-titulo")
            linha.append(nome)
            if r["seguranca"]:
                linha.append(Gtk.Image(icon_name="channel-secure-symbolic", opacity=0.6))
            if r["uso"]:
                linha.append(Gtk.Image(icon_name="object-select-symbolic"))
            b = Gtk.Button(child=linha)
            b.add_css_class("rede")
            if not r["uso"]:
                b.connect("clicked", lambda *_, rede=r: self.conectar_rede(rede))
            lista.append(b)
            if getattr(self, "senha_para", None) == r["ssid"]:
                lista.append(self.campo_senha(r))
        rolar.set_child(lista)
        caixa.append(rolar)
        return caixa

    def ligar_wifi(self, ligar):
        import threading
        if ligar:
            self.redes = None                     # "Searching…" até a lista nova chegar
        threading.Thread(target=lambda: (rodar("nmcli", "radio", "wifi", "on" if ligar else "off"),
                                         ligar and __import__("time").sleep(2), ligar and self.procurar_redes()),
                         daemon=True).start()
        GLib.timeout_add(800, self.montar_seguro)

    def campo_senha(self, rede):
        """Senha de uma rede nova, ali mesmo na central. Vai pro NetworkManager pela entrada padrão
        (passwd-file /dev/stdin), nunca na linha de comando (que qualquer programa consegue ler)."""
        caixa = Gtk.Box(spacing=6, margin_start=28, margin_end=4, margin_top=2, margin_bottom=4)
        campo = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, placeholder_text="Password")
        ok = Gtk.Button(label="Connect")
        ok.add_css_class("suggested-action")

        def enviar(*_):
            senha = campo.get_text()
            if len(senha) >= 8 or (rede["tipo"].startswith("WEP") and senha):
                self.conectar_com_senha(rede, senha)
            else:
                self.aviso_wifi = "Wi-Fi passwords have at least 8 characters"
                self.atualizar_sub()
        campo.connect("activate", enviar)
        ok.connect("clicked", enviar)
        caixa.append(campo)
        caixa.append(ok)
        GLib.idle_add(lambda: (campo.grab_focus(), GLib.SOURCE_REMOVE)[1])
        return caixa

    def conectar_rede(self, rede):
        """Rede salva: conecta direto. Aberta: conecta. Nova com senha: abre o campo de senha aqui.
        Empresarial (802.1X, login e certificado): isso precisa da tela completa do Settings."""
        import threading
        ssid = rede["ssid"]
        salvas = rodar("nmcli", "-t", "-f", "NAME", "con", "show").splitlines()
        if ssid not in salvas and "802.1X" in rede.get("tipo", ""):
            self.abrir_settings("rede")
            return
        if ssid not in salvas and rede["seguranca"]:
            self.senha_para = None if getattr(self, "senha_para", None) == ssid else ssid
            self.aviso_wifi = ""
            self.atualizar_sub()
            return
        self.senha_para = None
        self.aviso_wifi = f"Connecting to {ssid}…"
        self.atualizar_sub()

        def trabalho():
            if ssid in salvas:
                r = subprocess.run(["nmcli", "--wait", "45", "con", "up", "id", ssid], capture_output=True, text=True)
            else:
                r = subprocess.run(["nmcli", "--wait", "45", "dev", "wifi", "connect", ssid],
                                   capture_output=True, text=True)
            self.depois_conectar(ssid, r.returncode == 0)
        threading.Thread(target=trabalho, daemon=True).start()

    def conectar_com_senha(self, rede, senha):
        import threading
        ssid = rede["ssid"]
        self.senha_para = None
        self.aviso_wifi = f"Connecting to {ssid}…"
        self.atualizar_sub()
        # WPA3 puro usa "sae"; WPA2 e o modo misto WPA2/WPA3 usam "wpa-psk"; WEP é "none" + chave WEP
        tipo = rede.get("tipo", "")
        if tipo.startswith("WEP"):
            seguranca, campo = ["wifi-sec.key-mgmt", "none"], "802-11-wireless-security.wep-key0"
        elif "WPA3" in tipo and "WPA2" not in tipo and "WPA1" not in tipo:
            seguranca, campo = ["wifi-sec.key-mgmt", "sae"], "802-11-wireless-security.psk"
        else:
            seguranca, campo = ["wifi-sec.key-mgmt", "wpa-psk"], "802-11-wireless-security.psk"

        def trabalho():
            criou = subprocess.run(["nmcli", "con", "add", "type", "wifi", "con-name", ssid, "ssid", ssid]
                                   + seguranca, capture_output=True, text=True)
            ok = False
            if criou.returncode == 0:
                r = subprocess.run(["nmcli", "--wait", "45", "con", "up", "id", ssid, "passwd-file", "/dev/stdin"],
                                   input=f"{campo}:{senha}\n", capture_output=True, text=True)
                ok = r.returncode == 0
                if not ok:
                    subprocess.run(["nmcli", "con", "delete", "id", ssid], capture_output=True)
            self.depois_conectar(ssid, ok, senha_errada=criou.returncode == 0)
        threading.Thread(target=trabalho, daemon=True).start()

    def depois_conectar(self, ssid, ok, senha_errada=False):
        def na_tela():
            if ok:
                self.aviso_wifi = ""
            else:
                self.aviso_wifi = (f"Couldn't connect to {ssid}. Wrong password?" if senha_errada
                                   else f"Couldn't connect to {ssid}")
                if senha_errada:
                    self.senha_para = ssid               # campo aberto de novo pra tentar outra vez
            self.procurar_redes()
            self.montar_seguro()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(na_tela)

    def ao_bloco(self, botao, rotulo, acao):
        """Clique num botão grande: muda NA HORA (cor e texto) e roda o comando em segundo plano.
        Uns 2 s depois, confere o estado de verdade (se o comando falhou, o botão volta)."""
        import threading
        ligando = not botao.has_css_class("ligado")
        (botao.add_css_class if ligando else botao.remove_css_class)("ligado")
        if rotulo.get_label() in ("On", "Off", "Muted"):
            rotulo.set_label("On" if ligando else ("Muted" if rotulo.get_label() == "On" and
                                                   botao.get_child().get_first_child().get_icon_name()
                                                   .startswith("audio-input") else "Off"))
        threading.Thread(target=acao, daemon=True).start()
        GLib.timeout_add(2000, self.montar_seguro)

    def coletar(self):
        """Busca TODAS as informações ao mesmo tempo (cada uma numa thread): ~0,1 s no total, em vez de
        ~0,7 s uma atrás da outra. Roda em segundo plano; a tela não espera por ela."""
        from concurrent.futures import ThreadPoolExecutor
        tarefas = {"player": self.player, "wifi": self.wifi, "bluetooth": self.bluetooth,
                   "modos": lambda: sorted(notif_modos()), "filtro": self.filtro_ligado,
                   "tema": lambda: rodar("cat", os.path.expanduser("~/.cache/hypr-tema/tema")).strip(),
                   "sinks": lambda: listar("sinks"), "sources": lambda: listar("sources"),
                   "brilho": self.ler_brilho}
        with ThreadPoolExecutor(len(tarefas)) as ex:
            futuros = {k: ex.submit(f) for k, f in tarefas.items()}
            return {k: f.result() for k, f in futuros.items()}

    @staticmethod
    def ler_brilho():
        """(telas que o controle muda, valor) — DDC pode levar segundos: só roda fora da tela."""
        from servicos import brilho
        telas = brilho.alvos()
        return telas, (brilho.ler(telas[0]) if telas else None)

    def atualizar_tudo(self):
        """Coleta em segundo plano e, quando chegar, redesenha (na thread do GTK)."""
        import threading

        def trabalho():
            estado = self.coletar()
            GLib.idle_add(self.aplicar_estado, estado)
        threading.Thread(target=trabalho, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def aplicar_estado(self, estado):
        antigo, self.estado = getattr(self, "estado", None), estado
        if not self.get_visible():
            return GLib.SOURCE_REMOVE
        # nada mudou (além do player, que tem o relógio dele)? não redesenha: evita a central "piscar"
        sem_player = lambda d: {k: v for k, v in (d or {}).items() if k != "player"}   # noqa: E731
        mesmo_player = (antigo or {}).get("player", {}) and estado.get("player") and \
            antigo["player"].get("titulo") == estado["player"].get("titulo")
        if antigo and sem_player(antigo) == sem_player(estado) and (mesmo_player or antigo.get("player") == estado.get("player")):
            self.atualizar_sub()
        else:
            self.montar()
        return GLib.SOURCE_REMOVE

    def montar(self):
        if self.mexendo or getattr(self, "estado", None) is None:
            return
        e = self.estado
        while (filho := self.cartao.get_first_child()) is not None:
            self.cartao.remove(filho)
        pl = e["player"]
        self.capa_atual = pl
        self.widget_player = None
        if pl:
            self.widget_player = self.cartao_player(pl)
            self.cartao.append(self.widget_player)

        controles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        controles.add_css_class("controles")
        # topo: usuário + bateria
        topo = Gtk.Box(spacing=8)
        # você (o usuário): foto do perfil (~/.face) ou a inicial; clicar escolhe outra foto (doc 34)
        import pwd
        eu = pwd.getpwuid(os.getuid())
        nome_eu = eu.pw_gecos.split(",")[0] or eu.pw_name
        avatar = Adw.Avatar(size=26, text=nome_eu, show_initials=True)
        foto = foto_perfil()
        if foto is not None:
            avatar.set_custom_image(foto)
        perfil = Gtk.Button(tooltip_text="Change profile picture")
        perfil.add_css_class("perfil")
        caixa_eu = Gtk.Box(spacing=8)
        caixa_eu.append(avatar)
        rot = Gtk.Label(label=nome_eu)
        rot.add_css_class("bloco-titulo")
        caixa_eu.append(rot)
        perfil.set_child(caixa_eu)
        perfil.connect("clicked", lambda *_: (self.fechar(), subprocess.Popen(
            ["hyprctl", "dispatch", "exec", f"{self.BIN}/escolher-foto"], stdout=subprocess.DEVNULL)))
        topo.append(perfil)
        try:
            # a bateria pode se chamar BAT0, BAT1, BATT... (depende do notebook): pega a primeira
            pasta = next(os.path.join("/sys/class/power_supply", n) for n in sorted(os.listdir("/sys/class/power_supply"))
                         if n.startswith("BAT"))
            bat = int(open(f"{pasta}/capacity").read())
            carregando = open(f"{pasta}/status").read().strip() in ("Charging", "Full")
            chip = Gtk.Label(label=f"{'⚡ ' if carregando else ''}{bat}%")
            chip.add_css_class("chip")
            topo.append(chip)
        except (OSError, ValueError, StopIteration):
            pass
        topo.append(Gtk.Box(hexpand=True))
        ajustes = Gtk.Button(icon_name="emblem-system-symbolic", tooltip_text="Settings")
        ajustes.add_css_class("seta")
        ajustes.connect("clicked", lambda *_: self.abrir_settings(""))
        topo.append(ajustes)
        controles.append(topo)

        # brilho (a tela focada: notebook ou monitor externo via DDC; servicos/brilho.py) e volume
        from servicos import brilho
        telas_brilho, valor_brilho = e["brilho"]
        if valor_brilho is not None:
            controles.append(self.deslizante("display-brightness-symbolic", valor_brilho,
                                             lambda v: [brilho.definir(t, v) for t in telas_brilho]))
        saida = next((d for d in e["sinks"] if d["padrao"]), None)
        if saida:
            controles.append(self.deslizante("audio-volume-high-symbolic", min(volume_de(saida), 100),
                                             lambda v: rodar("pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{v}%")))

        # os 6 botões, em 2 colunas
        wifi_on, rede = e["wifi"]
        bt_on, bt_dev = e["bluetooth"]
        dnd = "dnd" in e["modos"]
        mic = next((d for d in e["sources"] if d["padrao"]), None)
        mic_mudo = bool(mic and mic.get("mute"))
        painel = self.ler_painel()
        try:
            escuro = open(os.path.expanduser("~/.cache/hypr-tema/tema")).read().strip() == "escuro"
        except OSError:
            escuro = painel.get("TEMA_MODO") == "escuro"
        filtro = e["filtro"]
        grade = Gtk.Grid(row_spacing=8, column_spacing=8, column_homogeneous=True)
        blocos = [
            self.botao_grande("network-wireless-symbolic" if wifi_on else "network-wireless-offline-symbolic",
                              "Wi-Fi", rede or ("On" if wifi_on else "Off"), wifi_on,
                              "wifi", "sub:wifi"),
            self.botao_grande("bluetooth-active-symbolic" if bt_on else "bluetooth-disabled-symbolic",
                              "Bluetooth", (bt_dev[0] if bt_dev else "On") if bt_on else "Off", bt_on,
                              # clique liga/desliga; a setinha ">" abre a lista (e já procura aparelhos)
                              lambda: rodar("bluetoothctl", "power", "off" if bt_on else "on"), "sub:bt"),
            self.botao_grande("notifications-disabled-symbolic", "Do not disturb", "On" if dnd else "Off", dnd,
                              lambda: (notif_modo("dnd", not dnd),
                                       rodar("pkill", "-RTMIN+10", "-x", "waybar"))),
            self.botao_grande("night-light-symbolic", "Night light", "On" if filtro else "Off", filtro,
                              lambda: self.alternar_filtro(not filtro), "sub:noite"),
            self.botao_grande("audio-volume-muted-symbolic" if saida and saida.get("mute") else "audio-speakers-symbolic",
                              "Sound", nome_bonito(saida.get("description", saida["name"]), saida["name"], False)
                              if saida else "No output", bool(saida and not saida.get("mute")), "som", "sub:som"),
            self.botao_grande("weather-clear-night-symbolic" if escuro else "weather-clear-symbolic",
                              "Dark mode", "On" if escuro else "Off", escuro,
                              lambda: self.alternar_tema(not escuro)),     # um clique troca, outro desfaz
        ]
        for i, b in enumerate(blocos):
            grade.attach(b, i % 2, i // 2, 1, 1)
        controles.append(grade)
        # subjanelinha (Wi-Fi, Bluetooth, Sound, agendas): uma área que só ESTENDE, deslizando,
        # sem redesenhar o resto da central
        self.area_sub = Gtk.Revealer(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                     transition_duration=180)
        self.area_sub.set_child(self.conteudo_sub())
        self.area_sub.set_reveal_child(self.area_sub.get_child() is not None)
        controles.append(self.area_sub)
        self.cartao.append(controles)

    def conteudo_sub(self):
        """O que vai dentro da subjanelinha aberta (ou None)."""
        e, sub = self.estado, getattr(self, "sub", None)
        wifi_on, bt_on, filtro = e["wifi"][0], e["bluetooth"][0], e["filtro"]
        painel = self.ler_painel()
        try:
            escuro = open(os.path.expanduser("~/.cache/hypr-tema/tema")).read().strip() == "escuro"
        except OSError:
            escuro = painel.get("TEMA_MODO") == "escuro"
        if sub == "wifi":
            return self.lista_wifi(wifi_on)
        if sub == "bt":
            return self.lista_bt(bt_on)
        if sub == "som":
            return self.lista_som(e)
        if sub == "noite":
            return self.agenda("Night light schedule", "FILTRO_MODO", "ligado" if filtro else "desligado",
                                         (("From", "FILTRO_INICIO"), ("To", "FILTRO_FIM")), painel, temperatura=True)
        return None

    def atualizar_sub(self):
        """Troca só o miolo da subjanelinha (lista nova chegou, aparelho mudou...)."""
        area = getattr(self, "area_sub", None)
        if area is None or not self.get_visible() or self.mexendo:
            return GLib.SOURCE_REMOVE
        novo = self.conteudo_sub()
        # A janela não muda de tamanho (ver __init__): só o miolo desliza, abrindo ou recolhendo.
        if novo is None:
            area.set_reveal_child(False)

            def encolher():
                if not area.get_reveal_child():
                    area.set_child(None)
                return GLib.SOURCE_REMOVE
            GLib.timeout_add(area.get_transition_duration() + 30, encolher)
        else:
            area.set_child(novo)
            area.set_reveal_child(True)
        return GLib.SOURCE_REMOVE

    def montar_seguro(self):
        if self.get_visible():
            self.atualizar_tudo()        # depois de um clique num botão: busca o estado novo e redesenha
        return GLib.SOURCE_REMOVE

    @staticmethod
    def texto_tempo(pos, dur):
        f = lambda t: (f"{int(t) // 3600}:{int(t) % 3600 // 60:02d}:{int(t) % 60:02d}" if t >= 3600
                       else f"{int(t) // 60}:{int(t) % 60:02d}")      # noqa: E731  (segundos -> "1:05")
        return f"{f(pos)} / {f(dur)}"

    def pular_relativo(self, segundos):
        import time
        self.pc_fundo("position", f"{abs(segundos)}{'+' if segundos > 0 else '-'}")
        if self.ref_pos:
            url, p, t, toc = self.ref_pos
            self.ref_pos = (url, max(0.0, p + (time.monotonic() - t if toc else 0) + segundos), time.monotonic(), toc)

    def ao_pular(self, _escala, _tipo, valor):
        import time
        self.pc_fundo("position", f"{max(0.0, valor):.1f}")      # pula pra esse ponto (segundos)
        if self.capa_atual:
            self.ref_pos = (self.capa_atual["url"], max(0.0, valor), time.monotonic(), self.capa_atual["tocando"])
        if self.tempo and self.capa_atual:
            self.tempo.set_label(self.texto_tempo(valor, self.capa_atual["duracao"]))
        return False

    def ao_botao_player(self, botao, cmd):
        self.pc_fundo(cmd)
        if cmd == "play-pause":                       # troca o ícone NA HORA (sem esperar o player)
            tocando = botao.get_icon_name() == "media-playback-start-symbolic"
            botao.set_icon_name("media-playback-pause-symbolic" if tocando else "media-playback-start-symbolic")
            if self.capa_atual:
                self.capa_atual["tocando"] = tocando
        GLib.timeout_add(600, lambda: (self.atualizar_player(sozinho=True), False)[1])

    def atualizar_player(self, sozinho=False):
        """A cada segundo (só aberta). Música/estado/capa mudou: troca o cartão do player.
        Só a posição andou: mexe só na barra e no tempo (sem redesenhar nada)."""
        if not self.get_visible():
            if not sozinho:
                self.relogio = None
            return GLib.SOURCE_REMOVE
        if not getattr(self, "perguntando_player", False):     # a pergunta anterior ainda não voltou: pula
            import threading
            self.perguntando_player = True

            def trabalho():
                try:
                    pl = self.player()
                finally:
                    self.perguntando_player = False
                GLib.idle_add(self.aplicar_player, pl)
            threading.Thread(target=trabalho, daemon=True).start()
        return GLib.SOURCE_REMOVE if sozinho else GLib.SOURCE_CONTINUE

    def aplicar_player(self, pl):
        if not self.get_visible():
            return GLib.SOURCE_REMOVE
        chave = lambda p: p and {k: v for k, v in p.items() if k != "posicao"}   # noqa: E731
        if chave(pl) != chave(self.capa_atual):
            self.trocar_player(pl)
        elif pl and self.barra_pos is not None and pl["duracao"] > 0:
            self.barra_pos.set_value(min(pl["posicao"], pl["duracao"]))
            self.tempo.set_label(self.texto_tempo(pl["posicao"], pl["duracao"]))
            self.capa_atual = pl
        return GLib.SOURCE_REMOVE

    def trocar_player(self, pl):
        """Troca SÓ o cartão do player (o de baixo, com os controles, fica como está)."""
        antigo = getattr(self, "widget_player", None)
        if antigo is not None and antigo.get_parent() is self.cartao:
            self.cartao.remove(antigo)
        self.widget_player = None
        self.capa_atual = pl
        if pl:
            self.widget_player = self.cartao_player(pl)
            self.cartao.prepend(self.widget_player)

    def alternar_filtro(self, ligar):
        """Filtro de luz azul: grava a escolha (o hypr-tema aplica) — mesmo efeito do Settings."""
        sys.path.insert(0, os.path.expanduser("~/.local/share/aero-settings"))
        import comum
        comum.atualizar_painel(FILTRO_MODO="ligado" if ligar else "desligado")
        # aplica o filtro DIRETO (rápido), em vez de regerar o tema inteiro; e anota pro hypr-tema saber
        temp = comum.ler_painel().get("FILTRO_TEMP", "4500") if ligar else "0"
        if temp == "0":         # hyprsunset 0.4: desligar = "identity true" (sem valor só pergunta) + 6500 K neutro
            rodar("hyprctl", "hyprsunset", "identity", "true")
            rodar("hyprctl", "hyprsunset", "temperature", "6500")
        else:
            rodar("hyprctl", "hyprsunset", "identity", "false")
            rodar("hyprctl", "hyprsunset", "temperature", temp)
        os.makedirs(os.path.expanduser("~/.cache/hypr-tema"), exist_ok=True)
        with open(os.path.expanduser("~/.cache/hypr-tema/filtro"), "w") as f:
            f.write(temp + "\n")

    @staticmethod
    def ler_painel():
        sys.path.insert(0, os.path.expanduser("~/.local/share/aero-settings"))
        import comum
        return comum.ler_painel()

    @staticmethod
    def gravar_painel(**kv):
        sys.path.insert(0, os.path.expanduser("~/.local/share/aero-settings"))
        import comum
        comum.atualizar_painel(**kv)

    def alternar_tema(self, escuro):
        """Dark mode na mão (desliga o automático). Trocar o tema precisa regerar as cores: roda em thread."""
        self.gravar_painel(TEMA_MODO="escuro" if escuro else "claro")
        rodar(f"{self.BIN}/hypr-tema", timeout=90)

    def agenda(self, titulo, chave_modo, modo_manual, horarios, painel, temperatura=False):
        """Subjanelinha de agenda: liga/desliga sozinho nos horários escolhidos (mesmo que o Settings)."""
        import threading
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        caixa.add_css_class("redes")
        auto = painel.get(chave_modo) == "auto"
        topo = Gtk.Box(spacing=6)
        t = Gtk.Label(label=titulo, xalign=0, hexpand=True)
        t.add_css_class("redes-titulo")
        topo.append(t)
        chave = Gtk.Switch(active=auto, valign=Gtk.Align.CENTER, tooltip_text="Turn on and off automatically")

        def ao_auto(sw, _p):
            self.gravar_painel(**{chave_modo: "auto" if sw.get_active() else modo_manual})
            threading.Thread(target=lambda: rodar(f"{self.BIN}/hypr-tema", timeout=90), daemon=True).start()
            GLib.timeout_add(600, self.montar_seguro)
        chave.connect("notify::active", ao_auto)
        topo.append(Gtk.Label(label="Automatic", opacity=0.75))
        topo.append(chave)
        caixa.append(topo)

        def hora(chave_hora, passo):
            h, m = (int(x) for x in painel.get(chave_hora, "00:00").split(":"))
            total = (h * 60 + m + passo) % 1440
            return f"{total // 60:02d}:{total % 60:02d}"

        for rotulo, chave_hora in horarios:
            linha = Gtk.Box(spacing=6, sensitive=auto, margin_start=6)
            linha.append(Gtk.Label(label=rotulo, xalign=0, hexpand=True))
            valor = Gtk.Label(label=painel.get(chave_hora, "00:00"), width_chars=5)
            valor.add_css_class("tempo")
            for icone, passo in (("list-remove-symbolic", -30), (None, 0), ("list-add-symbolic", 30)):
                if icone is None:
                    linha.append(valor)
                    continue
                b = Gtk.Button(icon_name=icone)
                b.add_css_class("desligar")

                def ao_passo(_b, ch=chave_hora, ps=passo, v=valor):
                    novo = hora(ch, ps)
                    painel[ch] = novo
                    v.set_label(novo)
                    self.gravar_painel(**{ch: novo})
                    threading.Thread(target=lambda: rodar(f"{self.BIN}/hypr-tema", timeout=90), daemon=True).start()
                b.connect("clicked", ao_passo)
                linha.append(b)
            caixa.append(linha)
        if temperatura:
            linha = Gtk.Box(spacing=6, margin_start=6)
            linha.append(Gtk.Label(label="Warmth", xalign=0))
            # pra direita = mais quente (temperatura menor): o slider vai de 2500 a 6000 e a cor é 8500 - valor

            def aplicar(valor):                  # numa thread (slider.py), só o valor mais recente
                v = str(int(8500 - valor) // 100 * 100)
                self.gravar_painel(FILTRO_TEMP=v)
                if self.filtro_ligado():           # já ligado: mostra a cor nova na hora
                    rodar("hyprctl", "hyprsunset", "temperature", v)
                    with open(os.path.expanduser("~/.cache/hypr-tema/filtro"), "w") as f:
                        f.write(v + "\n")
            from slider import SliderFluido
            escala = SliderFluido(2500, 6000, valor=8500 - int(painel.get("FILTRO_TEMP", "4500")), aplicar=aplicar)
            self.sliders = [s for s in getattr(self, "sliders", []) if s.get_root() is not None] + [escala]
            linha.append(escala)
            caixa.append(linha)
        return caixa

    @staticmethod
    def alternar_aviao(ligar):
        estado = "off" if ligar else "on"
        rodar("nmcli", "radio", "all", estado)
        rodar("bluetoothctl", "power", estado)

    def abrir_settings(self, aba):
        self.fechar()
        cmd = f"CONFIG_ABA={aba} {self.BIN}/configuracoes" if aba else f"{self.BIN}/configuracoes"
        subprocess.Popen(["hyprctl", "dispatch", "exec", cmd], stdout=subprocess.DEVNULL)

    def abrir_central(self):
        if self.get_visible():
            self.fechar()
            return
        # SEMPRE no canto superior direito, embaixo do ícone dela, com o mesmo espaço das janelas
        g = gap()
        Camada.set_margin(self, Camada.Edge.RIGHT, g)
        Camada.set_margin(self, Camada.Edge.TOP, g)
        monitor = next((m for m in hyprctl_json("monitors") or [] if m.get("focused")), None)
        largura_tela = int(monitor["width"] / monitor["scale"]) if monitor else 2560
        self.sub = None
        if monitor:      # altura fixa (ver __init__)
            reservado = monitor.get("reserved", [0, 0, 0, 0])
            self.set_size_request(-1, int(monitor["height"] / monitor["scale"]) - reservado[1] - reservado[3] - 2 * g)
        # Aparece NA HORA com o que mostrou da última vez; as informações novas chegam em ~0,1 s
        if getattr(self, "estado", None) is None:
            self.estado = self.coletar()      # primeira vez: ainda não há nada pra mostrar
        self.montar()
        self.mostrar((largura_tela - 60, largura_tela))      # o ícone fica nos últimos 52 px da barra
        self.atualizar_tudo()
        if self.relogio is None:
            self.relogio = GLib.timeout_add(1000, self.atualizar_player)


class Osd(Gtk.Window):
    """A "pílula" que aparece no rodapé quando você muda volume ou brilho pelas teclas."""

    def __init__(self):
        super().__init__()
        self.add_css_class("osd")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "osd")
        Camada.set_anchor(self, Camada.Edge.BOTTOM, True)
        Camada.set_margin(self, Camada.Edge.BOTTOM, 150)   # acima do Dock
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        caixa = Gtk.Box(spacing=14)
        caixa.add_css_class("pilula")
        caixa.set_size_request(300, -1)
        self.icone = Gtk.Image(pixel_size=20)
        from esfera import EsferaLiquida
        self.esfera = EsferaLiquida(46, texto=False)   # o volume enchendo/esvaziando (esfera.py)
        self.barra = Gtk.LevelBar(min_value=0, max_value=100, hexpand=True, valign=Gtk.Align.CENTER)
        self.por_cento = Gtk.Label(xalign=1)
        self.por_cento.add_css_class("por-cento")
        for w in (self.esfera, self.icone, self.barra, self.por_cento):
            caixa.append(w)
        self.set_child(caixa)
        self.relogio = None

    def mostrar(self, tipo):
        if tipo == "brilho":                 # a tela focada (notebook ou monitor externo): servicos/brilho.py
            from servicos import brilho
            tela = next(iter(brilho.alvos()), None)
            v = brilho.ler(tela) if tela else None
            if v is None:
                return
            icone = "display-brightness-symbolic"
        else:
            eh_saida = tipo != "mic"
            d = next((d for d in listar("sinks" if eh_saida else "sources") if d["padrao"]), None)
            if d is None:
                return
            v, mudo = volume_de(d), d.get("mute", False)
            if eh_saida:
                icone = icone_volume(v, mudo)
            else:
                icone = "microphone-sensitivity-muted-symbolic" if mudo else "audio-input-microphone-symbolic"
            if mudo:
                v = 0
        self.icone.set_from_icon_name(icone)
        self.esfera.set_mudo(tipo != "brilho" and v == 0)
        self.esfera.set_nivel(min(v, 100) / 100)
        self.barra.set_value(min(v, 100))
        self.por_cento.set_label(f"{v}%")
        self.present()
        if self.relogio:
            GLib.source_remove(self.relogio)
        self.relogio = GLib.timeout_add(1300, self.esconder)

    def esconder(self):
        self.relogio = None
        self.set_visible(False)
        return GLib.SOURCE_REMOVE


class App(Adw.Application):     # Adw = usa o estilo do libadwaita (cores certas no claro/escuro)
    def __init__(self):
        super().__init__(application_id="com.aero.Sound", flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.janelinha = None

    def preparar(self):
        provedor = Gtk.CssProvider()
        provedor.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.janelinha = Janelinha()
        self.central = CentralNotificacoes()
        self.menu_janela = MenuJanela()
        self.central_rapida = CentralRapida()
        self.osd = Osd()
        self.iniciar_hermes()
        self.hold()
        # Ações no D-Bus: o lançador chama com gdbus, sem iniciar outro Python
        for nome, funcao in (("popup", lambda p: self.janelinha.abrir(p == "completo")),
                             ("notificacoes", lambda _p: (self.lembrar_sino(), self.central.abrir(False))),
                             ("menu", lambda _p: self.menu_janela.abrir_no_mouse()),
                             ("central", lambda _p: self.central_rapida.abrir_central()),
                             ("sub", lambda p: self.central_rapida.alternar_sub(p)),   # abre uma subjanelinha
                             ("clique", lambda _p: [j.clique_fora() for j in list(Janelinha.abertas)]),
                             ("osd", lambda p: self.osd.mostrar(p))):
            acao = Gio.SimpleAction.new(nome, GLib.VariantType.new("s"))
            acao.connect("activate", lambda _a, valor, f=funcao: f(valor.get_string()))
            self.add_action(acao)
        threading.Thread(target=self.vigiar, daemon=True).start()

    def lembrar_sino(self):
        # a barra (AeroBar) tira a bolinha vermelha do sino: tudo até a última notificação foi visto
        if HERMES is not None:
            try:
                ultimo = max((x["id"] for x in HERMES.historico), default=0)
                with open(os.path.expanduser("~/.cache/aero-shell/sino-visto"), "w") as f:
                    f.write(str(ultimo))
            except OSError:
                pass
        self._lembrar_posicao_do_sino()

    def _lembrar_posicao_do_sino(self):
        # Com a AeroBar, ela mesma anota a posição exata do sino (sino.json): nada de adivinhar pelo mouse
        # (clicar no relógio abria as notificações e "mudava" o sino de lugar).
        if os.path.exists(os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aerobar.pid")):
            return
        """O sino da barra acabou de ser clicado: o mouse está em cima dele. Anota onde (em coordenadas
        do monitor), pra o Hermes soltar a onda e os cartões dali (~/.cache/aero-shell/sino.json)."""
        def trabalho():
            import json
            try:
                cursor = json.loads(rodar("hyprctl", "cursorpos", "-j") or "{}")
                for m in json.loads(rodar("hyprctl", "monitors", "-j") or "[]"):
                    escala = m.get("scale", 1) or 1
                    w, h = m["width"] / escala, m["height"] / escala
                    x, y = cursor["x"] - m["x"], cursor["y"] - m["y"]
                    if 0 <= x <= w and 0 <= y <= h:
                        caminho = os.path.expanduser("~/.cache/aero-shell/sino.json")
                        with open(caminho + ".tmp", "w") as f:
                            json.dump({"x": round(x), "y": round(y), "w": round(w), "h": round(h),
                                       "monitor": m.get("name", "")}, f)
                        os.replace(caminho + ".tmp", caminho)
                        if getattr(self, "hermes_ui", None) is not None:
                            GLib.idle_add(lambda: (self.hermes_ui.cartoes.ancorar(), False)[1])
                        return
            except (ValueError, KeyError, TypeError, OSError):
                pass
        threading.Thread(target=trabalho, daemon=True).start()

    def iniciar_hermes(self):
        """Hermes (doc: shell/hermes.py) recebe as notificações se o nome estiver livre. Com o mako
        rodando, o nome é dele e o Hermes fica quieto (sem janelas, sem custo)."""
        try:
            import hermes
            import hermes_ui
        except Exception as erro:  # noqa: BLE001 — sem Hermes a central continua igual
            print(f"hermes: not loaded: {erro!r}", flush=True)
            return
        servico = hermes.Hermes()
        servico.carregar_historico()

        def ganhou():
            global HERMES
            HERMES = servico
            self.hermes_ui = hermes_ui.HermesUI(servico)
            servico.ouvir(lambda ev, _n: ev == "modes" and rodar("pkill", "-RTMIN+10", "-x", "waybar"))
            print("hermes: in charge of notifications", flush=True)

        def perdeu():
            print("hermes: another notification daemon has the name (mako?); staying idle", flush=True)
        servico.publicar(Gio.bus_get_sync(Gio.BusType.SESSION), ganhou, perdeu)

    def vigiar(self):
        """pactl subscribe: uma linha por mudança ("Event 'change' on sink #52")."""
        # "setpriv --pdeathsig TERM": se este programa morrer (o Hyprland caiu, ou o som foi reiniciado),
        # o próprio Linux manda um SIGTERM pro pactl e ele fecha junto. Sem isso ele ficava rodando pra
        # sempre, órfão: o pactl ignora o SIGPIPE e nem percebe que ninguém mais lê o que ele escreve.
        proc = subprocess.Popen(["setpriv", "--pdeathsig", "TERM", "pactl", "subscribe"],
                                stdout=subprocess.PIPE, text=True)
        for linha in proc.stdout:
            if any(t in linha for t in ("sink", "source", "server")) and "monitor" not in linha:
                GLib.idle_add(self.atualizar)

    def atualizar(self):
        if self.janelinha.get_visible():
            self.janelinha.montar()
        return GLib.SOURCE_REMOVE

    def do_command_line(self, linha):
        if self.janelinha is None:
            self.preparar()
        args = linha.get_arguments()[1:]
        if len(args) == 2 and args[0] == "popup":
            self.janelinha.abrir(args[1] == "completo")
        elif len(args) == 2 and args[0] == "central":
            self.central_rapida.abrir_central()
        elif len(args) == 2 and args[0] == "notificacoes":
            self.lembrar_sino()
            self.central.abrir(False)
        elif len(args) == 2 and args[0] == "osd":
            self.osd.mostrar(args[1])
        return 0


def registrar_erros():
    """Diário de erros (~/.cache/aero-shell/som.log): erro em qualquer clique fica anotado com hora, e
    `kill -USR1 <pid>` grava onde cada thread está (o vigia do lançador `som` faz isso antes de reiniciar
    uma central travada). Antes, o daemon jogava tudo em /dev/null e um travamento não deixava pista."""
    import faulthandler
    import signal
    import time
    import traceback
    caminho = os.path.expanduser("~/.cache/aero-shell/som.log")
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    if os.path.exists(caminho) and os.path.getsize(caminho) > 512 * 1024:
        os.replace(caminho, caminho + ".1")
    diario = open(caminho, "a", buffering=1)
    diario.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} som.py iniciou (pid {os.getpid()})\n")
    faulthandler.enable(file=diario)
    faulthandler.register(signal.SIGUSR1, file=diario, all_threads=True)

    def anotar(tipo, valor, tb):
        diario.write(f"--- {time.strftime('%H:%M:%S')} erro:\n" + "".join(traceback.format_exception(tipo, valor, tb)))
    sys.excepthook = anotar
    threading_excepthook = lambda a: anotar(a.exc_type, a.exc_value, a.exc_traceback)  # noqa: E731
    import threading
    threading.excepthook = threading_excepthook


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        registrar_erros()
    sys.exit(App().run(sys.argv))
