#!/usr/bin/env python3
"""
main.py — janela principal do app Settings (Super+I).

Barra lateral em ÁRVORE (doc 57): grupos com filhos que abrem como sanfona (só o grupo escolhido mostra
os filhos); cada filho abre uma seção, às vezes numa sub-página dela (abrir_sub). Grupos:
  Theme (visual do sistema) · Workspaces (item único) · Devices · Connections · System

Layout (igual às Configurações do GNOME/Windows):
  ┌────────────┬──────────────────────────────┐
  │ Display    │  conteúdo da seção escolhida │
  │ Power      │                              │
  │ Sound      │  (clicar em algo pode abrir  │
  │ ...        │   uma sub-página, com "<")   │
  └────────────┴──────────────────────────────┘
  Adw.NavigationSplitView = a divisão lateral | conteúdo
  Adw.NavigationView      = pilha de páginas do lado direito (empilhar / voltar)

Cada seção mora no seu próprio arquivo sec_*.py. As seções só são criadas quando
abertas pela primeira vez (abre mais rápido), e ao_mostrar() atualiza os dados toda vez.
"""
import os

from comum import CSS_BOLINHAS, Adw, Gtk, aplicar_css, botoes_janela
from sec_aparencia import SecaoAparencia
from sec_areas import SecaoAreas
from sec_atualizacoes import SecaoAtualizacoes
from sec_bluetooth import SecaoBluetooth
from sec_dock import SecaoDock
from sec_energia import SecaoEnergia
from sec_icones import SecaoIcones
from sec_local import SecaoLocal
from sec_memoria import SecaoMemoria
from sec_mouse import SecaoMouse
from sec_notificacoes import SecaoNotificacoes
from sec_rede import SecaoRede
from sec_recording import SecaoGravacao
from sec_sobre import SecaoSobre
from sec_som import SecaoSom
from sec_teclado import SecaoTeclado
from sec_tela import SecaoTela
from sec_theme import SecaoTema
from sec_widgets import SecaoWidgets

# A árvore: (nome do grupo, ícone, [(nome do filho, classe da seção, sub-página ou None), ...])
ARVORE = [
    ("Theme", "preferences-desktop-appearance-symbolic", [
        ("Overview", SecaoTema, None), ("Colors and mode", SecaoAparencia, "colors"),
        ("Wallpaper", SecaoAparencia, "wallpaper"), ("Glass and windows", SecaoAparencia, "glass"),
        ("Icons", SecaoIcones, None), ("Dock", SecaoDock, None), ("Widgets", SecaoWidgets, None),
        ("Text and motion", SecaoAparencia, "text")]),
    # Workspaces: um item só (a página tem o seletor de área; Transition é sub-página dela)
    ("Workspaces", "view-paged-symbolic", SecaoAreas),
    ("Devices", "computer-symbolic", [
        ("Display", SecaoTela, None), ("Sound", SecaoSom, None), ("Keyboard", SecaoTeclado, None),
        ("Mouse & Touchpad", SecaoMouse, None)]),
    ("Screen recording", "media-record-symbolic", SecaoGravacao),
    ("Connections", "network-wireless-symbolic", [("Network", SecaoRede, None), ("Bluetooth", SecaoBluetooth, None)]),
    ("System", "emblem-system-symbolic", [
        ("Power", SecaoEnergia, None), ("Memory", SecaoMemoria, None), ("Notifications", SecaoNotificacoes, None), ("Location", SecaoLocal, None),
        ("Updates", SecaoAtualizacoes, None), ("About", SecaoSobre, None)]),
]
# Animações da árvore (doc 57): filhos deslizam (Gtk.Revealer), a seta gira, a linha clareia ao passar
CSS_ARVORE = """
.navigation-sidebar > row { transition: background-color 150ms ease-out; }
.navigation-sidebar > row.arvore-filho { min-height: 0; padding-top: 0; padding-bottom: 0; margin-bottom: 0; }
.arvore-seta { transition: -gtk-icon-transform 220ms ease-out; }
.arvore-seta.aberta { -gtk-icon-transform: rotate(90deg); }
"""
DURACAO = 220      # ms: abrir/fechar grupo e a página nova entrando

# nomes antigos de CONFIG_ABA (scripts e Super Menu) -> (seção, sub-página)
APELIDOS = {"wallpapers": ("aparencia", "wallpaper"), "tema": ("tema", None), "theme": ("tema", None),
            "aparencia": ("aparencia", "colors"), "areas": ("areas", None)}


class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Settings", default_width=1000, default_height=720)
        self.set_size_request(760, 480)
        self.secoes = {}

        # Barra lateral em árvore: linha de grupo (com seta) + linhas filhas recuadas
        self.lista = lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        lista.add_css_class("navigation-sidebar")
        self.entradas = {}            # linha -> ("grupo", índice) ou ("filho", (grupo, classe, sub))
        self.filhos = {}              # grupo -> [linhas filhas]
        self.setas = {}
        self.aberto = None
        self.ultimo = {}              # grupo -> último filho aberto (ao reabrir, volta nele)
        self.animacao = None
        for g, (nome, icone, filhos) in enumerate(ARVORE):
            caixa = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
            caixa.append(Gtk.Image.new_from_icon_name(icone))
            caixa.append(Gtk.Label(label=nome, xalign=0, hexpand=True))
            linha = Gtk.ListBoxRow(child=caixa)
            lista.append(linha)
            self.filhos[g] = []
            if not isinstance(filhos, list):          # item sem filhos: abre a seção direto
                self.entradas[linha] = ("filho", (g, filhos, None))
                self.setas[g] = None
                continue
            seta = Gtk.Image.new_from_icon_name("pan-end-symbolic")
            seta.add_css_class("arvore-seta")
            caixa.append(seta)
            self.entradas[linha] = ("grupo", g)
            self.setas[g] = seta
            for titulo, classe, sub in filhos:
                rotulo = Gtk.Label(label=titulo, xalign=0, margin_top=9, margin_bottom=9, margin_start=40)
                rotulo.add_css_class("dim-label")
                cortina = Gtk.Revealer(child=rotulo, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                       transition_duration=DURACAO)
                filho = Gtk.ListBoxRow(child=cortina, visible=False)
                filho.add_css_class("arvore-filho")
                # fechou de vez: a linha some (não fica foco de teclado em linha escondida)
                cortina.connect("notify::child-revealed",
                                lambda r, _p, l=filho: r.get_reveal_child() or l.set_visible(False))
                lista.append(filho)
                self.entradas[filho] = ("filho", (g, classe, sub))
                self.filhos[g].append(filho)
        lista.connect("row-selected", self.ao_escolher)
        lista.connect("row-activated", self.ao_ativar)      # clique no grupo: abre / fecha

        lateral = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=lista, vexpand=True))
        cabecalho = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cabecalho.pack_start(botoes_janela(self))       # fechar, minimizar, maximizar (que funcionam)
        lateral.add_top_bar(cabecalho)

        self.nav = Adw.NavigationView()
        divisao = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title="Settings", child=lateral),
            content=Adw.NavigationPage(title="Settings", child=self.nav),
            min_sidebar_width=220, max_sidebar_width=260)

        self.avisos = Adw.ToastOverlay(child=divisao)   # "toasts" = avisos que somem sozinhos
        self.set_content(self.avisos)

        # Abrir direto numa seção: CONFIG_ABA=som configuracoes (nomes antigos valem: APELIDOS)
        inicial = os.environ.get("CONFIG_ABA", "tema")
        sec, sub = APELIDOS.get(inicial, (inicial, None))
        if not self.ir(sec, sub):
            self.ir("tema")

    def abrir_grupo(self, g):
        """Sanfona: os filhos do grupo g deslizam pra baixo, os dos outros recolhem. g=None fecha tudo."""
        self.aberto = g
        for outro, linhas in self.filhos.items():
            for l in linhas:
                if outro == g:
                    l.set_visible(True)
                l.get_child().set_reveal_child(outro == g)
            if self.setas[outro]:
                (self.setas[outro].add_css_class if outro == g else self.setas[outro].remove_css_class)("aberta")

    def ao_ativar(self, _lista, linha):
        """Clique (ou Enter) num grupo: se está aberto, fecha; se fechado, abre no último filho visto."""
        tipo, g = self.entradas[linha]
        if tipo != "grupo":
            return
        if self.aberto == g:
            self.abrir_grupo(None)
            self.lista.select_row(linha)         # o grupo fica marcado: a página dele continua aberta
            return
        self.abrir_grupo(g)
        self.lista.select_row(self.ultimo.get(g) or self.filhos[g][0])

    def ao_escolher(self, _lista, linha):
        if linha is None:
            return
        tipo, valor = self.entradas[linha]
        if tipo == "grupo":                      # quem decide é o ao_ativar (abre / fecha)
            return
        g, classe, sub = valor
        self.ultimo[g] = linha
        if self.aberto != g:
            self.abrir_grupo(g)
        if classe.id not in self.secoes:
            self.secoes[classe.id] = classe(self)
        secao = self.secoes[classe.id]
        if hasattr(secao, "abrir_sub"):
            secao.abrir_sub(sub)
        self.nav.replace([secao.pagina])   # troca o lado direito (e descarta sub-páginas abertas)
        secao.ao_mostrar()
        self.entrar(secao.pagina)

    def entrar(self, pagina):
        """A página nova aparece subindo de leve e clareando (o replace do Adw não anima sozinho)."""
        if self.animacao:
            self.animacao.skip()

        def passo(v):
            pagina.set_opacity(v)
            pagina.set_margin_top(round((1 - v) * 14))
        alvo = Adw.CallbackAnimationTarget.new(passo)
        self.animacao = Adw.TimedAnimation.new(pagina, 0, 1, DURACAO, alvo)
        self.animacao.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self.animacao.play()

    def ir(self, sec_id, sub=None):
        """Seleciona na árvore o filho da seção sec_id (e sub-página sub). False se não existir.
        "transitions" não está na árvore: abre Workspaces e empilha a Transition."""
        if sec_id == "transitions":
            self.ir("areas")
            self.secoes["areas"].abrir_transicoes()
            return True
        for linha, (tipo, valor) in self.entradas.items():
            if tipo == "filho" and valor[1].id == sec_id and (sub is None or valor[2] == sub):
                self.lista.select_row(linha)
                return True
        return False

    def atualizar_secoes(self):
        """Depois de trocar o wallpaper: as páginas que mostram ele se atualizam."""
        from comum import ler_painel
        if "areas" in self.secoes:
            self.secoes["areas"].mostrar_area()
        if "aparencia" in self.secoes:
            self.secoes["aparencia"].mostrar_wallpaper(ler_painel())

    # Usados pelas seções:
    def empilhar(self, pagina):
        self.nav.push(pagina)       # abre uma sub-página (aparece o botão "<")

    def voltar(self):
        self.nav.pop()

    def avisar(self, texto):
        self.avisos.add_toast(Adw.Toast(title=texto, timeout=3, use_markup=False))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Settings")

    def do_startup(self):
        Adw.Application.do_startup(self)
        aplicar_css(CSS_BOLINHAS)
        aplicar_css(CSS_ARVORE)

    def do_activate(self):
        # Se já estiver aberto, só traz a janela pra frente
        (self.props.active_window or Janela(self)).present()


if __name__ == "__main__":
    App().run([])
