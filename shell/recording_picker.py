#!/usr/bin/env python3
"""
recording_picker.py — Ctrl+Shift+Print: escolhe o que gravar antes de começar.

Aparece no canto da tela, no mesmo lugar e no mesmo visual das notificações, com dois botões:
  1  Full screen  (o monitor onde está o mouse)
  2  Area         (você arrasta a área com o mouse)
Teclas 1 e 2 escolhem, Esc (ou clicar fora... não: Esc) cancela. Depois de escolher, a janela some e o
~/.local/bin/screen-record começa. Ctrl+Shift+Print de novo, gravando, para a gravação (quem cuida
disso é o screen-record, antes de abrir esta janela).
Rodar com LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so (o screen-record já faz).
"""
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

SCRIPT = os.path.expanduser("~/.local/bin/screen-record")
OPCOES = [(["view-fullscreen-symbolic"], "Full screen", "1", "screen"),
          (["image-crop-symbolic", "transform-crop-symbolic", "edit-select-all-symbolic"], "Area", "2", "area")]

CSS = """
window.rec-picker { background: transparent; backdrop-filter: none; }
.cartao {
    background-color: var(--popover-bg-color, @popover_bg_color);
    border-radius: 22px;
    padding: 14px 16px 16px 16px;
    box-shadow: inset 0 0 0 1px alpha(currentColor, 0.10);
}
.cartao .titulo { font-weight: 600; }
.cartao .sub { font-size: 12px; opacity: 0.65; }
.opcao { border-radius: 16px; padding: 12px 8px 10px 8px; min-width: 120px;
         background: alpha(currentColor, 0.06); transition: background-color 150ms ease-out; }
.opcao:hover, .opcao:focus-visible { background: alpha(var(--accent-bg-color, #3584e4), 0.22); }
.opcao .rotulo { font-size: 12px; }
.tecla { font-family: monospace; font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 6px;
         background: alpha(currentColor, 0.12); }
"""


class Picker(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.RecordingPicker")
        self.escolha = None

    def do_activate(self):
        prov = Gtk.CssProvider()
        prov.load_from_string(CSS)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        j = self.janela = Gtk.ApplicationWindow(application=self, title="Screen recording")
        j.add_css_class("rec-picker")
        Camada.init_for_window(j)
        Camada.set_layer(j, Camada.Layer.OVERLAY)
        Camada.set_namespace(j, "screen-record")
        Camada.set_anchor(j, Camada.Edge.TOP, True)
        Camada.set_anchor(j, Camada.Edge.RIGHT, True)
        Camada.set_margin(j, Camada.Edge.TOP, 12)          # o mesmo canto e margem das notificações (mako)
        Camada.set_margin(j, Camada.Edge.RIGHT, 12)
        Camada.set_keyboard_mode(j, Camada.KeyboardMode.EXCLUSIVE)   # 1, 2 e Esc chegam aqui

        cartao = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cartao.add_css_class("cartao")
        topo = Gtk.Box(spacing=10)
        topo.append(Gtk.Image(icon_name="media-record-symbolic", pixel_size=20))
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        t = Gtk.Label(label="Screen recording", xalign=0)
        t.add_css_class("titulo")
        s = Gtk.Label(label="What do you want to record?  Esc cancels", xalign=0)
        s.add_css_class("sub")
        textos.append(t)
        textos.append(s)
        topo.append(textos)
        cartao.append(topo)

        botoes = Gtk.Box(spacing=8, homogeneous=True)
        for icone, nome, tecla, modo in OPCOES:
            b = Gtk.Button()
            b.add_css_class("flat")
            b.add_css_class("opcao")
            dentro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            dentro.append(Gtk.Image(gicon=Gio.ThemedIcon.new_from_names(icone), pixel_size=32))   # 1º que o tema tiver
            linha = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
            k = Gtk.Label(label=tecla)
            k.add_css_class("tecla")
            r = Gtk.Label(label=nome)
            r.add_css_class("rotulo")
            linha.append(k)
            linha.append(r)
            dentro.append(linha)
            b.set_child(dentro)
            b.connect("clicked", lambda _b, m=modo: self.escolher(m))
            botoes.append(b)
        cartao.append(botoes)
        j.set_child(cartao)

        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self.tecla)
        j.add_controller(teclas)
        j.present()
        GLib.timeout_add_seconds(15, lambda: (self.quit(), False)[1])     # esquecida aberta: some sozinha

    def tecla(self, _c, tecla, _cod, _mod):
        nome = Gdk.keyval_name(tecla) or ""
        if nome in ("1", "KP_1", "KP_End", "exclam"):
            self.escolher("screen")
        elif nome in ("2", "KP_2", "KP_Down", "at"):
            self.escolher("area")
        elif nome == "Escape":
            self.quit()
        else:
            return False
        return True

    def escolher(self, modo):
        self.escolha = modo
        self.janela.set_visible(False)       # some antes: não aparece na gravação nem atrapalha a seleção
        self.quit()


if __name__ == "__main__":
    app = Picker()
    app.run([sys.argv[0]])
    if app.escolha:
        cmd = [SCRIPT, "start-screen"] if app.escolha == "screen" else [SCRIPT, "start-area"]
        env = {k: v for k, v in os.environ.items() if k != "LD_PRELOAD"}
        subprocess.Popen(cmd, env=env, start_new_session=True)
