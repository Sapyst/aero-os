#!/usr/bin/env python3
"""
askpass.py — janelinha de senha para o sudo.

Quando o app roda algo com "sudo -A" e o sudo precisa da senha, ele executa este programa
(o caminho vem da variável SUDO_ASKPASS). Nós mostramos uma janela, e o que a pessoa digitar
é impresso na saída padrão: o sudo lê de lá. Saída com código 1 = cancelado.
A senha nunca é salva em lugar nenhum: só passa por um "cano" (pipe) direto pro sudo.
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk  # noqa: E402


class AskPass(Adw.Application):
    def __init__(self):
        # NON_UNIQUE: cada pedido de senha é uma janela independente
        super().__init__(application_id="com.aero.Settings.AskPass",
                         flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.codigo = 1  # cancelado, até que confirmem

    def do_activate(self):
        janela = Adw.ApplicationWindow(application=self, title="Authentication required",
                                       default_width=420, resizable=False)
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                        margin_top=24, margin_bottom=24, margin_start=24, margin_end=24)
        caixa.append(Gtk.Image(icon_name="dialog-password-symbolic", pixel_size=48))
        titulo = Gtk.Label(label="Authentication required")
        titulo.add_css_class("title-2")
        caixa.append(titulo)
        texto = Gtk.Label(label="Settings needs your password to make system changes.",
                          wrap=True, justify=Gtk.Justification.CENTER)
        texto.add_css_class("dim-label")
        caixa.append(texto)

        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lista.add_css_class("boxed-list")
        self.senha = Adw.PasswordEntryRow(title="Password")
        self.senha.connect("entry-activated", lambda *_: self.confirmar())  # Enter confirma
        lista.append(self.senha)
        caixa.append(lista)

        botoes = Gtk.Box(spacing=12, homogeneous=True, margin_top=6)
        cancelar = Gtk.Button(label="Cancel")
        cancelar.connect("clicked", lambda *_: self.quit())
        ok = Gtk.Button(label="Authenticate")
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *_: self.confirmar())
        botoes.append(cancelar)
        botoes.append(ok)
        caixa.append(botoes)

        janela.set_content(caixa)
        janela.present()
        self.senha.grab_focus()

    def confirmar(self):
        print(self.senha.get_text(), flush=True)
        self.codigo = 0
        self.quit()


if __name__ == "__main__":
    app = AskPass()
    app.run([])
    sys.exit(app.codigo)
