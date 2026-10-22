#!/usr/bin/env python3
"""
energia.py — confirma antes de sair, reiniciar ou desligar (doc 23).

Antes, Super+Shift+E fechava a sessão NA HORA: um esbarrão e tudo aberto se perdia sem salvar.
Agora aparece uma janelinha "Log out?" com Cancel (o padrão: Enter/Esc cancelam) e a ação.
Também é usada pelos botões de energia do Super Menu.

Uso: energia logout | reboot | poweroff
"""
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: E402

ACOES = {
    "logout": ("Log out?", "All open apps will be closed. Unsaved work will be lost.", "Log out",
               ["hyprctl", "dispatch", "exit"]),
    "reboot": ("Restart now?", "All open apps will be closed. Unsaved work will be lost.", "Restart",
               ["systemctl", "reboot"]),
    "poweroff": ("Shut down now?", "All open apps will be closed. Unsaved work will be lost.", "Shut down",
                 ["systemctl", "poweroff"]),
}


class App(Adw.Application):
    def __init__(self, acao):
        super().__init__(application_id="com.aero.Power")
        self.acao = acao

    def do_activate(self):
        titulo, texto, botao, comando = ACOES[self.acao]
        # A própria janela é o "diálogo": título, texto e dois botões (Cancel é o padrão)
        janela = Adw.ApplicationWindow(application=self, title=titulo, resizable=False,
                                       default_width=400)
        janela.add_css_class("energia")
        import dotkit
        dotkit.tema_apps(janela)                # DotStyle (dotstyle-apps.css), only in that visual
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                        margin_top=24, margin_bottom=20, margin_start=24, margin_end=24)
        cabeca = Gtk.Label(label=titulo, xalign=0)
        cabeca.add_css_class("title-2")
        caixa.append(cabeca)
        corpo = Gtk.Label(label=texto, xalign=0, wrap=True)
        corpo.add_css_class("dim-label")
        caixa.append(corpo)
        botoes = Gtk.Box(spacing=10, halign=Gtk.Align.END, margin_top=12)
        cancelar = Gtk.Button(label="Cancel")
        cancelar.connect("clicked", lambda *_: self.quit())
        confirmar = Gtk.Button(label=botao)
        confirmar.add_css_class("destructive-action")
        confirmar.connect("clicked", lambda *_: (subprocess.Popen(comando), self.quit()))
        botoes.append(cancelar)
        botoes.append(confirmar)
        caixa.append(botoes)
        janela.set_content(caixa)
        janela.set_default_widget(cancelar)         # Enter = Cancel (seguro)
        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", lambda _c, tecla, *_: self.quit() if tecla == 0xff1b else None)  # Esc
        janela.add_controller(teclas)
        janela.present()
        cancelar.grab_focus()


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else ""
    if acao not in ACOES:
        sys.exit("uso: energia logout|reboot|poweroff")
    sys.exit(App(acao).run([sys.argv[0]]))
