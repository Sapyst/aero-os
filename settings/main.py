#!/usr/bin/env python3
"""
main.py — janela principal do app Settings (Super+I).

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
from sec_atualizacoes import SecaoAtualizacoes
from sec_bluetooth import SecaoBluetooth
from sec_dock import SecaoDock
from sec_energia import SecaoEnergia
from sec_local import SecaoLocal
from sec_mouse import SecaoMouse
from sec_rede import SecaoRede
from sec_sobre import SecaoSobre
from sec_som import SecaoSom
from sec_teclado import SecaoTeclado
from sec_tela import SecaoTela
from sec_widgets import SecaoWidgets

# Theme (sec_aparencia) junta aparência + wallpaper; o navegador de wallpapers virou sub-página dela.
SECOES = [SecaoTela, SecaoEnergia, SecaoSom, SecaoBluetooth, SecaoRede, SecaoLocal, SecaoAparencia,
          SecaoDock, SecaoWidgets, SecaoTeclado, SecaoMouse, SecaoAtualizacoes, SecaoSobre]


class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Settings", default_width=1000, default_height=720)
        self.set_size_request(760, 480)
        self.secoes = {}

        # Barra lateral: uma lista com ícone + nome de cada seção
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        lista.add_css_class("navigation-sidebar")
        for classe in SECOES:
            caixa = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
            caixa.append(Gtk.Image.new_from_icon_name(classe.icone))
            caixa.append(Gtk.Label(label=classe.titulo, xalign=0))
            lista.append(Gtk.ListBoxRow(child=caixa))
        lista.connect("row-selected", self.ao_escolher)

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

        # Abrir direto numa seção: CONFIG_ABA=atualizacoes configuracoes
        ids = [c.id for c in SECOES]
        inicial = os.environ.get("CONFIG_ABA", "tela")
        abrir_wall = inicial == "wallpapers"          # links antigos (Super Menu): Theme → Wallpaper
        if inicial in ("wallpapers", "tema", "theme"):
            inicial = "aparencia"
        lista.select_row(lista.get_row_at_index(ids.index(inicial) if inicial in ids else 0))
        if abrir_wall and "aparencia" in self.secoes:
            # depois da janela aparecer: antes disso o "push" da sub-página se perde
            from gi.repository import GLib
            GLib.timeout_add(300, lambda: self.secoes["aparencia"].abrir_wallpapers() and False)

    def ao_escolher(self, _lista, linha):
        if linha is None:
            return
        classe = SECOES[linha.get_index()]
        if classe.id not in self.secoes:
            self.secoes[classe.id] = classe(self)
        secao = self.secoes[classe.id]
        self.nav.replace([secao.pagina])   # troca o lado direito (e descarta sub-páginas abertas)
        secao.ao_mostrar()

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

    def do_activate(self):
        # Se já estiver aberto, só traz a janela pra frente
        (self.props.active_window or Janela(self)).present()


if __name__ == "__main__":
    App().run([])
