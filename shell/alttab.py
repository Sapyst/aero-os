#!/usr/bin/env python3
"""
alttab.py — Alt+Tab igual ao do Windows.

Como funciona no Windows (e aqui):
- Segura Alt e aperta Tab: aparece a lista de janelas, da usada mais recentemente pra mais antiga,
  com a SEGUNDA já selecionada (a primeira é a que você está usando agora).
- Cada Tab avança; Shift+Tab (ou setas) volta. Soltou o Alt = vai pra janela selecionada.
- Alt+Tab rápido (aperta e solta): troca direto pra janela anterior, sem nem mostrar a lista.
  Repetindo, você alterna entre as duas últimas janelas.
- Esc cancela. Mouse: passar por cima seleciona, clicar troca, o X fecha a janela.
- Janelas minimizadas também aparecem; escolher uma traz ela de volta.
- Cada cartão mostra uma MINIATURA da janela (capturada com `grim -T`, ~50 ms cada).

Peças:
- Este programa fica rodando em segundo plano e escuta comandos num "socket" (arquivo especial
  de comunicação): $XDG_RUNTIME_DIR/aero-alttab.sock
- ~/.local/bin/alttab manda os comandos (next, prev, confirm, cancel) — é o que os atalhos chamam.
- É uma CAMADA (layer-shell), não uma janela: abrir a lista não mexe no histórico de foco.
- Enquanto está aberta, a camada pega o teclado só pra ela (modo EXCLUSIVE) e vigia o Alt:
  soltou = troca. (Um atalho "ao soltar o Alt" no hyprland.conf NÃO serve: o Hyprland cancela
  atalhos de soltar quando outra tecla — o Tab — foi apertada no meio.)
  O Tab continua sendo atalho do Hyprland (ele vem antes dos programas); setas, Enter e Esc a
  própria lista trata.
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import warnings

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Graphene", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)   # avisos do Gio.DesktopAppInfo

SOCKET = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aero-alttab.sock")
PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")
ATRASO_MS = 120          # Alt+Tab mais rápido que isso troca sem mostrar a lista (como no Windows)
MINIATURA = (232, 140)   # tamanho máximo da miniatura
POR_LINHA = 6            # cartões por linha (com mais janelas, quebra em outra linha)

CSS = """
/* backdrop-filter: none na janela: o gtk.css do sistema desfoca toda janela, e numa camada o Hyprland
   desfoca o RETÂNGULO inteiro (aparece um quadrado em volta dos cantos redondos). Pelo mesmo motivo,
   nada de sombra grande (ela é cortada na borda da janela): só um contorno fino. Doc 26. */
window.alttab { background: transparent; backdrop-filter: none; }
.painel {
    background-color: var(--popover-bg-color, @popover_bg_color);   /* cor do tema, quase sólida (doc 18) */
    border-radius: 22px;
    padding: 14px;
    margin: 2px;
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.10);
}
.cartao { border-radius: 14px; padding: 8px; border: 3px solid transparent;
          transition: border-color 150ms ease-out, background-color 150ms ease-out; }
.cartao:hover { background: alpha(currentColor, 0.06); }
/* Selecionado: borda na cor de destaque e um "bump" — cresce um pouco além e assenta (keyframes) */
.cartao.selecionado { border-color: var(--accent-bg-color, #3584e4); background: alpha(var(--accent-bg-color, #3584e4), 0.14);
                      transform: scale(1.03); animation: bump 220ms ease-out; }
@keyframes bump {
    0%   { transform: scale(1.00); }
    55%  { transform: scale(1.07); }
    100% { transform: scale(1.03); }
}
.cartao .titulo { font-weight: 600; font-size: 12px; }
.cartao .miniatura { border-radius: 8px; background: alpha(currentColor, 0.06); }
.cartao .minimizada { font-size: 10px; opacity: 0.6; }
.cartao .fechar { min-width: 22px; min-height: 22px; padding: 0; border-radius: 99px;
                  background: alpha(currentColor, 0.14); color: currentColor; }   /* neutro, sem vermelho (doc 42) */
"""


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True).stdout


def ler_opcoes():
    """Opções do Settings (Keyboard → Alt+Tab), no painel.conf."""
    opcoes = {"ALTTAB_TODAS": "1", "ALTTAB_MINIATURAS": "1"}
    try:
        with open(PAINEL) as f:
            for linha in f:
                chave, _, valor = linha.strip().partition("=")
                if chave in opcoes:
                    opcoes[chave] = valor.strip('"')
    except OSError:
        pass
    return opcoes


def icone_da_janela(classe):
    """Acha o ícone do app pela "classe" da janela (ex: firefox -> firefox.desktop)."""
    for nome in (classe, classe.lower()):
        try:
            return Gio.DesktopAppInfo.new(f"{nome}.desktop").get_icon()
        except TypeError:
            pass
    for app in Gio.AppInfo.get_all():        # apps cuja janela tem outro nome (StartupWMClass)
        if isinstance(app, Gio.DesktopAppInfo) and (app.get_startup_wm_class() or "").lower() == classe.lower():
            return app.get_icon()
    for grupo in Gio.DesktopAppInfo.search(classe):   # último recurso: busca pelo nome
        return Gio.DesktopAppInfo.new(grupo[0]).get_icon()
    return Gio.ThemedIcon.new("application-x-executable")


class AltTab(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Alt+Tab")
        self.add_css_class("alttab")
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)                       # por cima de tudo
        Camada.set_namespace(self, "alttab")
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)           # fechada: não pega o teclado
        # sem âncoras = centralizado na tela

        # Grade de cartões: até POR_LINHA por linha; o painel tem o tamanho exato do conteúdo
        self.painel = Gtk.Grid(row_spacing=6, column_spacing=6, row_homogeneous=True,
                               column_homogeneous=True)
        self.painel.add_css_class("painel")
        self.set_child(self.painel)
        # DotStyle: the live halftone field wraps every card (campo.py)
        import dotkit
        self.campo = None
        if dotkit.tema_apps(self):
            import campo
            self.campo = campo.envolver(self, self.painel, formas=self.formas_cartoes, halo=70)

        self.janelas = []        # lista de janelas (dicionários do hyprctl clients -j)
        self.cartoes = []
        self.selecionada = 0
        self.aberta = False      # Alt+Tab em andamento (com ou sem a lista na tela)
        self.temporizador = None
        self.soltou_alt_em = 0.0      # "confirm" que chegou com a lista fechada (Alt+Tab rapidíssimo)
        self.mouse_inicio = None  # onde o mouse estava quando a lista abriu
        self.so_minimizadas = False   # True = aberta pelo Super+M (só minimizadas, sem vigiar o Alt)
        self.pasta = tempfile.mkdtemp(prefix="alttab-")

        # Se a lista sumir sem passar pelo terminar() (ex: o Hyprland fecha a camada quando um
        # monitor é desligado — a tampa do notebook), zera o estado; senão o próximo Alt+Tab
        # achava que ela ainda estava aberta e não mostrava nada.
        self.connect("notify::visible", self.ao_mudar_visibilidade)

        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self.ao_apertar)
        teclas.connect("key-released", self.ao_soltar)
        teclas.connect("modifiers", self.ao_mudar_modificadores)
        self.add_controller(teclas)
        foco = Gtk.EventControllerFocus()
        foco.connect("enter", lambda *_: GLib.idle_add(self.ao_receber_foco))
        self.add_controller(foco)
        self.teclado_chegou = False

    # ---------- comandos (vêm do ~/.local/bin/alttab) ----------

    def ao_mudar_visibilidade(self, *_):
        if not self.get_visible() and self.aberta and self.temporizador is None and self.get_opacity() > 0:
            print("a lista sumiu sozinha (camada fechada?); zerando", flush=True)
            self.aberta = False
            Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)

    def comando(self, cmd):
        print("comando:", cmd, "aberta:", self.aberta, "visível:", self.get_visible(), flush=True)
        try:
            self._comando(cmd)
        except Exception:
            # Qualquer erro: registra e volta ao estado "fechada", pra nunca ficar travado
            traceback.print_exc()
            self.aberta = False
            self.set_visible(False)
            Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        return GLib.SOURCE_REMOVE

    def _comando(self, cmd):
        if cmd in ("next", "prev") and self.aberta and not self.get_visible() and self.temporizador is None:
            print("estado travado (aberta mas invisível); recomeçando", flush=True)
            self.aberta = False
        if cmd in ("next", "prev"):
            passo = 1 if cmd == "next" else -1
            if not self.aberta:
                self.comecar(passo)
            elif self.janelas:
                self.selecionar((self.selecionada + passo) % len(self.janelas))
                self.mostrar_agora()          # segundo Tab: mostra a lista na hora
        elif cmd == "overview":
            # Super+Tab (doc 25): TODAS as janelas, de todas as áreas, fica aberta até você escolher
            if self.aberta:
                self.terminar(trocar=False)
            else:
                self.comecar(1, so_minimizadas=True, todas=True)
        elif cmd == "minimized":
            # Super+M (doc 25): a mesma lista, só com as minimizadas; fica aberta até você escolher
            if self.aberta:
                self.terminar(trocar=False)
            else:
                self.comecar(1, so_minimizadas=True)
        elif cmd == "confirm" and self.aberta:
            self.terminar(trocar=True)
        elif cmd == "confirm":
            # O Hyprland avisou que o Alt foi solto ANTES do "next" chegar (os dois comandos correm
            # em paralelo). Guarda a hora: se o "next" vier logo em seguida, troca na hora.
            self.soltou_alt_em = time.monotonic()
        elif cmd == "cancel" and self.aberta:
            self.terminar(trocar=False)

    def comecar(self, passo, so_minimizadas=False, todas=False):
        # so_minimizadas = modo "fica aberta" (sem vigiar o Alt); todas = visão geral (Super+Tab)
        self.so_minimizadas = so_minimizadas
        opcoes = ler_opcoes()
        clientes = json.loads(hyprctl("clients", "-j") or "[]")
        ativo = json.loads(hyprctl("activeworkspace", "-j") or "{}").get("id")
        janelas = [c for c in clientes if c["mapped"] and not c["hidden"] and
                   (c["workspace"]["id"] > 0 or c["workspace"]["name"] == "special:minimized")]
        if opcoes["ALTTAB_TODAS"] != "1":     # só a área de trabalho atual (e as minimizadas)
            janelas = [c for c in janelas if c["workspace"]["id"] == ativo or
                       c["workspace"]["name"] == "special:minimized"]
        if so_minimizadas and not todas:
            janelas = [c for c in clientes if c["workspace"]["name"] == "special:minimized"]
        elif todas:
            janelas = [c for c in clientes if c["mapped"] and not c["hidden"] and
                       (c["workspace"]["id"] > 0 or c["workspace"]["name"] == "special:minimized")]
        # focusHistoryID: 0 = a janela em uso, 1 = a anterior... (a ordem "mais recente primeiro")
        self.janelas = sorted(janelas, key=lambda c: c["focusHistoryID"])
        if not self.janelas:
            if so_minimizadas and not todas:
                subprocess.Popen(["notify-send", "-a", "Window", "No minimized windows",
                                  "Use the yellow button (or Super+Down) to minimize a window."])
            return
        self.aberta = True
        self.mouse_inicio = None
        self.teclado_chegou = False
        self.montar(opcoes["ALTTAB_MINIATURAS"] == "1")
        inicio = 0 if so_minimizadas else (1 if passo == 1 else len(self.janelas) - 1)
        self.selecionar(inicio % len(self.janelas))
        if so_minimizadas:
            # sem Alt pra vigiar: aparece na hora e espera Enter/clique/Esc
            Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)
            self.set_opacity(1)
            self.present()
            return
        if time.monotonic() - self.soltou_alt_em < 0.4:     # o Alt já foi solto: Alt+Tab rápido
            self.soltou_alt_em = 0.0
            self.terminar(trocar=True)
            return
        # Já entra na tela (invisível, opacidade 0) pra pegar o teclado e vigiar o Alt;
        # só aparece de verdade depois do atraso: Alt+Tab rápido troca sem piscar nada
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)
        self.set_opacity(0)
        self.present()
        self.temporizador = GLib.timeout_add(ATRASO_MS, self.ao_passar_atraso)

    def ao_passar_atraso(self):
        # Passou o atraso e o Alt continua apertado: mostra a lista.
        # (Se o Alt tivesse sido solto, o ao_mudar_modificadores já teria trocado de janela.)
        # Atenção: NÃO dá pra perguntar "o Alt está apertado?" aqui: a camada demora mais que o
        # atraso pra receber o teclado, e até lá a resposta é sempre "não" (doc 20).
        self.temporizador = None         # este temporizador acabou (não remover de novo)
        print("atraso: teclado chegou?", self.teclado_chegou, flush=True)
        return self.mostrar_agora()

    def formas_cartoes(self):
        formas = []
        for c in self.cartoes:
            ok, r = c.compute_bounds(self.campo)
            if ok and r.get_width() > 4:
                formas.append((r.get_x(), r.get_y(), r.get_width(), r.get_height(), 22))
        return formas

    def mostrar_agora(self):
        if self.temporizador:
            GLib.source_remove(self.temporizador)
            self.temporizador = None
        if self.aberta:
            self.set_opacity(1)
            if self.campo is not None:
                self.campo.abrir()
        return GLib.SOURCE_REMOVE

    # ---------- teclado (enquanto aberta) ----------

    def alt_apertado(self):
        teclado = Gdk.Display.get_default().get_default_seat().get_keyboard()
        return bool(teclado.get_modifier_state() & Gdk.ModifierType.ALT_MASK)

    def ao_mudar_modificadores(self, _ctrl, estado):
        print("modificadores:", int(estado), flush=True)
        self.teclado_chegou = True
        # NÃO troca por aqui: ao receber o teclado, o GTK às vezes diz "Alt solto" com o Alt ainda
        # apertado, e a lista trocava logo no 1º Tab. Quem avisa o soltar do Alt é o Hyprland
        # ("bindrt = ALT, Alt_L" -> alttab confirm), que vê o teclado de verdade.
        return False

    def ao_receber_foco(self, *_):
        """O teclado chegou na lista. Se o Alt já foi solto antes disso (Alt+Tab bem rápido),
        não vai ter evento de "soltou": confere o estado agora."""
        print("teclado chegou; alt =", self.alt_apertado(), flush=True)
        self.teclado_chegou = True
        # (não troca aqui; ver ao_mudar_modificadores)

    def ao_soltar(self, _ctrl, tecla, _codigo, _estado):
        if self.aberta and not self.so_minimizadas and tecla in (Gdk.KEY_Alt_L, Gdk.KEY_Alt_R, Gdk.KEY_Meta_L, Gdk.KEY_Meta_R):
            self.terminar(trocar=True)

    def ao_apertar(self, _ctrl, tecla, _codigo, estado):
        if not self.aberta or not self.janelas:
            return False
        n = len(self.janelas)
        if tecla == Gdk.KEY_Escape:
            self.terminar(trocar=False)
        elif tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.terminar(trocar=True)
        elif tecla in (Gdk.KEY_Right, Gdk.KEY_Down) or (tecla == Gdk.KEY_Tab):
            self.selecionar((self.selecionada + 1) % n)
            self.mostrar_agora()
        elif tecla in (Gdk.KEY_Left, Gdk.KEY_Up, Gdk.KEY_ISO_Left_Tab):
            self.selecionar((self.selecionada - 1) % n)
            self.mostrar_agora()
        return True

    def terminar(self, trocar):
        if self.temporizador:
            GLib.source_remove(self.temporizador)
            self.temporizador = None
        self.aberta = False
        self.set_visible(False)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)    # devolve o teclado
        if trocar and self.janelas:
            alvo = self.janelas[self.selecionada]
            self.trocar_para(alvo)
            # Quando a lista (camada) some de verdade, o Hyprland devolve o foco pra janela que
            # estava antes — por cima da troca acima. Por isso confere de novo um pouco depois.
            GLib.timeout_add(120, self.conferir_troca, alvo)

    def conferir_troca(self, alvo):
        ativa = json.loads(hyprctl("activewindow", "-j") or "{}").get("address")
        if ativa != alvo["address"]:
            print("foco voltou pra outra janela; trocando de novo", flush=True)
            alvo = dict(alvo, workspace={"name": ""})    # já saiu das minimizadas na 1ª tentativa
            self.trocar_para(alvo)
        return GLib.SOURCE_REMOVE

    def trocar_para(self, c):
        endereco = c["address"]
        print("trocando para:", c["class"], "selecionada:", self.selecionada, flush=True)
        if c["workspace"]["name"] == "special:minimized":
            # minimizada: volta pra área de trabalho atual antes de focar
            atual = json.loads(hyprctl("activeworkspace", "-j"))["id"]
            hyprctl("dispatch", "movetoworkspacesilent", f"{atual},address:{endereco}")
        hyprctl("dispatch", "focuswindow", f"address:{endereco}")
        if c["floating"]:
            hyprctl("dispatch", "alterzorder", f"top,address:{endereco}")   # flutuante: traz pra frente

    # ---------- cartões ----------

    def montar(self, com_miniaturas):
        while (filho := self.painel.get_first_child()) is not None:
            self.painel.remove(filho)
        self.cartoes = []
        for i, c in enumerate(self.janelas):
            self.painel.attach(self.cartao(i, c, com_miniaturas), i % POR_LINHA, i // POR_LINHA, 1, 1)
        self.set_default_size(1, 1)      # encolhe a camada pro tamanho do novo conteúdo
        if com_miniaturas:
            threading.Thread(target=self.capturar, args=(list(self.janelas), list(self.cartoes)),
                             daemon=True).start()

    def cartao(self, i, c, com_miniaturas):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        caixa.add_css_class("cartao")

        topo = Gtk.Box(spacing=6)
        topo.append(Gtk.Image(gicon=icone_da_janela(c["class"]), pixel_size=18))
        titulo = Gtk.Label(label=c["title"] or c["class"], xalign=0, hexpand=True, ellipsize=3,
                           max_width_chars=22, use_markup=False)
        titulo.add_css_class("titulo")
        topo.append(titulo)
        fechar = Gtk.Button(child=Gtk.Label(label="✖"), valign=Gtk.Align.CENTER, opacity=0)   # mesmo ✖ das barras (doc 42)
        fechar.add_css_class("fechar")
        fechar.connect("clicked", lambda _b: self.fechar_janela(c))
        topo.append(fechar)
        caixa.append(topo)

        largura, altura = MINIATURA
        miniatura = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN, can_shrink=True)
        miniatura.set_size_request(largura, altura if com_miniaturas else 0)
        miniatura.add_css_class("miniatura")
        icone_grande = Gtk.Image(gicon=icone_da_janela(c["class"]), pixel_size=64 if com_miniaturas else 48)
        fundo = Gtk.Overlay(child=miniatura)
        fundo.add_overlay(icone_grande)        # o ícone grande aparece até a miniatura chegar
        caixa.append(fundo)
        caixa.miniatura, caixa.icone_grande = miniatura, icone_grande
        if c["workspace"]["name"] == "special:minimized":
            aviso = Gtk.Label(label="Minimized")
            aviso.add_css_class("minimizada")
            caixa.append(aviso)

        # mouse: MEXER por cima seleciona (e mostra o X); clicar troca pra janela.
        # Só "estar" por cima não conta: o mouse parado onde a lista aparece não rouba a seleção.
        movimento = Gtk.EventControllerMotion()
        movimento.connect("motion", lambda _m, x, y: self.mouse_mexeu(caixa, x, y) and
                          (self.selecionar(i), fechar.set_opacity(1)))
        movimento.connect("leave", lambda *_: fechar.set_opacity(0))
        caixa.add_controller(movimento)
        clique = Gtk.GestureClick(button=1)
        clique.connect("released", lambda *_: (self.selecionar(i), self.terminar(trocar=True)))
        caixa.add_controller(clique)
        meio = Gtk.GestureClick(button=2)       # botão do meio fecha, como nas abas do navegador
        meio.connect("released", lambda *_: self.fechar_janela(c))
        caixa.add_controller(meio)
        self.cartoes.append(caixa)
        return caixa

    def mouse_mexeu(self, caixa, x, y):
        """True se o mouse andou de verdade desde que a lista abriu (não só "apareceu embaixo")."""
        ponto = caixa.compute_point(self, Graphene.Point().init(x, y))[1]
        if self.mouse_inicio is None:
            self.mouse_inicio = (ponto.x, ponto.y)
            return False
        return abs(ponto.x - self.mouse_inicio[0]) + abs(ponto.y - self.mouse_inicio[1]) > 6

    def selecionar(self, i):
        if not self.cartoes:
            return
        self.cartoes[self.selecionada % len(self.cartoes)].remove_css_class("selecionado")
        self.selecionada = i
        self.cartoes[i].add_css_class("selecionado")

    def fechar_janela(self, c):
        hyprctl("dispatch", "closewindow", f"address:{c['address']}")
        i = self.janelas.index(c)
        del self.janelas[i]
        if not self.janelas:
            self.terminar(trocar=False)
            return
        # remonta os cartões (cada um guarda o próprio número na lista)
        self.montar(ler_opcoes()["ALTTAB_MINIATURAS"] == "1")
        self.selecionada = 0
        self.selecionar(min(i, len(self.janelas) - 1))

    def capturar(self, janelas, cartoes):
        """Tira a "foto" de cada janela, todas ao mesmo tempo (em segundo plano)."""
        processos = []
        for c in janelas:
            if c["workspace"]["name"] == "special:minimized":
                processos.append((None, None))   # minimizada não é desenhada: a foto sairia em branco
                continue
            arquivo = os.path.join(self.pasta, c["address"] + ".png")
            escala = min(1.0, max(0.1, MINIATURA[0] * 1.5 / max(1, c["size"][0])))   # 1.5x: nítido
            processos.append((arquivo, subprocess.Popen(
                ["grim", "-T", c["stableId"], "-s", f"{escala:.3f}", "-l", "1", arquivo],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)))
        for (arquivo, proc), caixa in zip(processos, cartoes):
            if proc is not None and proc.wait() == 0:
                GLib.idle_add(self.por_miniatura, caixa, arquivo)

    def por_miniatura(self, caixa, arquivo):
        try:
            caixa.miniatura.set_paintable(Gdk.Texture.new_from_filename(arquivo))
            caixa.icone_grande.set_visible(False)
        except GLib.Error:
            pass
        return GLib.SOURCE_REMOVE


class App(Adw.Application):    # Adw = acompanha o claro/escuro do sistema (doc 35)
    def __init__(self):
        super().__init__(application_id="com.aero.AltTab", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        provedor = Gtk.CssProvider()
        provedor.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.janela = AltTab(self)
        self.hold()
        # Escuta os comandos no socket (numa thread; cada comando vai pra thread do GTK)
        if os.path.exists(SOCKET):
            teste = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                teste.sendto(b"ping", SOCKET)   # alguém atendeu = já tem um Alt+Tab rodando
                print("já existe um Alt+Tab rodando; saindo", flush=True)
                self.quit()
                return
            except OSError:
                os.unlink(SOCKET)               # socket "morto" (sobrou de um programa que fechou)
        servidor = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)   # DGRAM: cada mensagem é um comando
        servidor.bind(SOCKET)

        def ouvir():
            while True:
                cmd = servidor.recv(64).decode().strip()
                GLib.idle_add(self.janela.comando, cmd)

        threading.Thread(target=ouvir, daemon=True).start()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv[:1]))
