#!/usr/bin/env python3
"""aero-intro: the first second of the desktop. Covers the screen with the still Venus (the same
picture the login screen ended on), waits for the bar to be up, then glitches out and reveals the
desktop. Started first thing by Hyprland (exec-once); exits by itself."""
# Fica por cima de tudo (camada OVERLAY) enquanto barra, dock e widgets nascem por baixo. Espera a
# barra aparecer (no máximo 2,5 s) e então sai com o glitch, mostrando o desktop pronto.
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Gtk4LayerShell as Camada  # noqa: E402

sys.path.insert(0, "/usr/share/aero/venus")
from venus import Venus  # noqa: E402

ESPERA_MAX = 2.5


class Intro(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Intro")

    def do_activate(self):
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)
        j = Gtk.ApplicationWindow(application=self)
        Camada.init_for_window(j)
        Camada.set_layer(j, Camada.Layer.OVERLAY)
        Camada.set_namespace(j, "aero-intro")
        Camada.set_exclusive_zone(j, -1)
        Camada.set_keyboard_mode(j, Camada.KeyboardMode.NONE)
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(j, borda, True)
        self.venus = Venus()
        self.venus.hold()
        j.set_child(self.venus)
        j.present()
        self.inicio = GLib.get_monotonic_time() / 1e6
        GLib.timeout_add(150, self.esperar)

    def esperar(self):
        pronto = subprocess.run(["pgrep", "-x", "waybar"], capture_output=True).returncode == 0 or \
            subprocess.run(["pgrep", "-f", "aero-shell/aerobar.py"], capture_output=True).returncode == 0
        passou = GLib.get_monotonic_time() / 1e6 - self.inicio
        if (pronto and passou > 0.6) or passou > ESPERA_MAX:
            self.venus.leave(1.0, self.quit)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE


if __name__ == "__main__":
    sys.exit(Intro().run([sys.argv[0]]))
