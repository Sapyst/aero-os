#!/usr/bin/env python3
"""
emojis.py — seletor de emojis (Super+.), como o Win+. do Windows (doc 23).

Usa o Gtk.EmojiChooser, que já vem pronto no GTK4 (busca, categorias, recentes, tons de pele).
Ele é um "popover" (balão), então precisa de uma janela de apoio: uma janelinha transparente no
centro da tela, de onde o balão sai.

Escolheu um emoji: ele vai pra área de transferência e é colado no app em que você estava
(o mesmo truque do histórico do clipboard, doc 22: focuswindow + sendshortcut).
"""
import json
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

TERMINAIS = {"kitty", "foot", "alacritty", "Alacritty", "org.wezfurlong.wezterm", "com.mitchellh.ghostty"}
CSS = "window.emojis, window.emojis > * { background: transparent; box-shadow: none; }"


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True).stdout


class App(Adw.Application):    # Adw = acompanha o claro/escuro do sistema (doc 35)
    def __init__(self):
        super().__init__(application_id="com.aero.Emojis")
        self.anterior = None

    def do_activate(self):
        ativa = json.loads(hyprctl("activewindow", "-j") or "{}")
        self.anterior = ativa if ativa.get("address") else None

        provedor = Gtk.CssProvider()
        provedor.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        janela = Gtk.ApplicationWindow(application=self, title="Emoji", decorated=False,
                                       default_width=8, default_height=8)
        janela.add_css_class("emojis")
        import dotkit
        dotkit.tema_apps(janela)                # DotStyle (dotstyle-apps.css), only in that visual
        apoio = Gtk.Box()
        janela.set_child(apoio)
        escolhedor = Gtk.EmojiChooser()
        escolhedor.set_parent(apoio)
        escolhedor.connect("emoji-picked", self.escolheu)
        escolhedor.connect("closed", lambda *_: GLib.timeout_add(50, self.sair))
        janela.present()
        GLib.timeout_add(80, lambda: escolhedor.popup() and False)   # espera a janela aparecer

    def escolheu(self, _escolhedor, emoji):
        subprocess.run(["wl-copy", "--", emoji])
        self.colar = True

    def sair(self):
        if getattr(self, "colar", False) and self.anterior:
            endereco = self.anterior["address"]
            hyprctl("dispatch", "focuswindow", f"address:{endereco}")
            atalho = "CTRL SHIFT" if self.anterior.get("class") in TERMINAIS else "CTRL"
            subprocess.Popen(["sh", "-c", f'sleep 0.15; hyprctl dispatch sendshortcut "{atalho}, V, address:{endereco}"'])
        self.quit()
        return False


if __name__ == "__main__":
    sys.exit(App().run([sys.argv[0]]))
