#!/usr/bin/env python3
"""
sphere.py — The DotStyle Sphere Effect for Aero Assistant.

Drops from the AeroBar with a metaball liquid effect, visualizes listening, speaking,
and thinking states with DotStyle dots, and retracts back into the bar when closed.

States:
  - opening:  liquid drop detaches from taskbar with metaball neck and slides down to form sphere
  - listening: sphere opens in middle into a pulsing ring following default microphone level
  - thinking: slow rotation of DotStyle dots, no audio pulse
  - speaking: solid sphere of DotStyle dots pulsing with output audio level
  - closing:  sphere moves back up, merges with taskbar via liquid neck, and closes

Requirements:
  - Layer-shell surface (Gtk4LayerShell / OVERLAY), no keyboard focus
  - Respects AeroOS theme colors and reduced motion
  - Pauses rendering when hidden or during fullscreen game/app
  - Zero resident memory when call is closed
"""
import argparse
import math
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as Camada
    HAS_LAYER_SHELL = True
except (ValueError, ImportError):
    HAS_LAYER_SHELL = False

from gi.repository import Gdk, GLib, Gtk  # noqa: E402
import cairo  # noqa: E402

H = os.path.expanduser("~")
PAINEL = os.path.join(H, ".config/hypr/painel.conf")

# Default DotStyle palette
DOT_FG = (0.925, 0.92, 0.91)
DOT_DIM = (0.55, 0.545, 0.535)
DOT_ACCENT = (1.0, 0.231, 0.188)  # AeroOS Red Accent
BG_DARK = (0.043, 0.043, 0.047)

WIDTH = 320
HEIGHT = 280
SPHERE_RADIUS = 48.0
BAR_Y = 0.0
REST_Y = 130.0


def read_theme_accent():
    try:
        if os.path.exists(PAINEL):
            with open(PAINEL) as f:
                for line in f:
                    if line.startswith("COR_DESTAQUE=") or line.startswith("COR_DESTAQUE_WALL="):
                        val = line.strip().split("=", 1)[1].strip('"').strip("'")
                        if len(val) == 6:
                            r = int(val[0:2], 16) / 255.0
                            g = int(val[2:4], 16) / 255.0
                            b = int(val[4:6], 16) / 255.0
                            return (r, g, b)
    except OSError:
        pass
    return DOT_ACCENT


def is_reduced_motion():
    try:
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "enable-animations"],
                             capture_output=True, text=True, timeout=1).stdout
        if "false" in out.lower():
            return True
    except Exception:
        pass
    return False


def is_fullscreen_active():
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"], capture_output=True, text=True, timeout=1).stdout
        if '"fullscreen": 1' in out or '"fullscreen": true' in out:
            return True
    except Exception:
        pass
    return False


def pescoco(cr, cx, y_borda, y_alvo, larg_borda, larg_alvo, alcance, cor):
    """Liquid neck in Cairo between a horizontal border and a descending drop (from shell/solido.py)."""
    d = 1.0 if y_alvo >= y_borda else -1.0
    g = (y_alvo - y_borda) * d
    if g >= alcance:
        return False
    s_ = 1.0 - max(0.0, g) / alcance
    nb = larg_borda * (0.5 + 0.5 * s_)
    np_ = larg_alvo * (0.5 + 0.5 * s_)
    nm = max(0.0, min(nb, np_) * (s_ ** 1.1) - 0.8)
    if nm <= 0.2:
        return False
    y0, y1 = y_borda - 2 * d, y_alvo + 3 * d

    cr.save()
    cr.set_source_rgba(cor[0], cor[1], cor[2], 0.85 * s_)
    cr.move_to(cx - nb, y0)
    cr.curve_to(cx - nb * 0.7, (y0 + y1) * 0.35, cx - nm, (y0 + y1) * 0.5, cx - np_, y1)
    cr.line_to(cx + np_, y1)
    cr.curve_to(cx + nm, (y0 + y1) * 0.5, cx + nb * 0.7, (y0 + y1) * 0.35, cx + nb, y0)
    cr.close_path()
    cr.fill()
    cr.restore()
    return True


class SphereEffect:
    def __init__(self, state="opening"):
        self.state = state
        self.start_time = time.time()
        self.accent = read_theme_accent()
        self.audio_level = 0.0
        self.reduced_motion = is_reduced_motion()
        self.cx = WIDTH / 2.0
        self.cy = REST_Y
        self.phase = 0.0

        # Precompute fibonacci dot sphere (120 points)
        self.dots = []
        n_dots = 120
        phi = math.pi * (3.0 - math.sqrt(5.0))  # golden angle
        for i in range(n_dots):
            y = 1.0 - (i / float(n_dots - 1)) * 2.0  # y goes from 1 to -1
            radius_at_y = math.sqrt(max(0.0, 1.0 - y * y))
            theta = phi * i
            x = math.cos(theta) * radius_at_y
            z = math.sin(theta) * radius_at_y
            self.dots.append((x, y, z))

    def set_state(self, new_state):
        self.state = new_state
        self.start_time = time.time()

    def set_audio_level(self, level):
        self.audio_level = max(0.0, min(1.0, level))

    def draw(self, cr, width, height):
        now = time.time()
        elapsed = now - self.start_time
        cx = width / 2.0

        # Background is transparent layer surface
        cr.save()
        cr.set_operator(cairo.OPERATOR_CLEAR)
        cr.paint()
        cr.restore()

        if self.state == "opening":
            dur = 0.6 if not self.reduced_motion else 0.05
            p = min(1.0, elapsed / dur)
            # Ease out quad
            t = 1.0 - (1.0 - p) * (1.0 - p)
            cur_y = BAR_Y + (REST_Y - BAR_Y) * t
            cur_r = SPHERE_RADIUS * (0.3 + 0.7 * t)

            # Draw metaball liquid neck if still in reach
            pescoco(cr, cx, BAR_Y, cur_y - cur_r, larg_borda=36.0, larg_alvo=cur_r * 0.9, alcance=110.0, cor=self.accent)

            # Draw descending drop
            cr.save()
            cr.set_source_rgba(self.accent[0], self.accent[1], self.accent[2], 0.9)
            cr.arc(cx, cur_y, cur_r, 0, 2 * math.pi)
            cr.fill()
            cr.restore()

            if p >= 1.0:
                self.state = "listening"

        elif self.state == "closing":
            dur = 0.5 if not self.reduced_motion else 0.05
            p = min(1.0, elapsed / dur)
            t = p * p  # ease in quad
            cur_y = REST_Y - (REST_Y - BAR_Y) * t
            cur_r = SPHERE_RADIUS * max(0.1, 1.0 - 0.7 * t)

            pescoco(cr, cx, BAR_Y, cur_y - cur_r, larg_borda=36.0, larg_alvo=cur_r * 0.9, alcance=110.0, cor=self.accent)

            cr.save()
            cr.set_source_rgba(self.accent[0], self.accent[1], self.accent[2], 0.9 * (1.0 - 0.5 * t))
            cr.arc(cx, cur_y, cur_r, 0, 2 * math.pi)
            cr.fill()
            cr.restore()

            if p >= 1.0:
                self.finished = True

        elif self.state == "listening":
            # Pulsing open ring following mic input level
            pulse = math.sin(elapsed * 4.0) * 0.08 + self.audio_level * 0.35
            base_r = SPHERE_RADIUS * (1.1 + pulse)
            inner_r = base_r * 0.55
            n_ring_dots = 48
            angle_step = (2.0 * math.pi) / n_ring_dots

            cr.save()
            for i in range(n_ring_dots):
                a = i * angle_step + elapsed * 0.5
                wave = math.sin(a * 3.0 + elapsed * 5.0) * (4.0 + self.audio_level * 10.0)
                r = base_r + wave
                dot_x = cx + math.cos(a) * r
                dot_y = REST_Y + math.sin(a) * r
                dot_size = 2.4 + math.sin(a * 2.0) * 0.8 + self.audio_level * 2.0

                cr.set_source_rgba(DOT_FG[0], DOT_FG[1], DOT_FG[2], 0.9)
                cr.arc(dot_x, dot_y, dot_size, 0, 2 * math.pi)
                cr.fill()

                # Inner accent core dots
                core_x = cx + math.cos(a) * inner_r
                core_y = REST_Y + math.sin(a) * inner_r
                cr.set_source_rgba(self.accent[0], self.accent[1], self.accent[2], 0.75)
                cr.arc(core_x, core_y, 1.6, 0, 2 * math.pi)
                cr.fill()
            cr.restore()

        elif self.state == "thinking":
            # Slow rotation of DotStyle dots, no audio pulse
            rot_y = elapsed * 0.9
            rot_x = math.sin(elapsed * 0.4) * 0.25

            cos_y, sin_y = math.cos(rot_y), math.sin(rot_y)
            cos_x, sin_x = math.cos(rot_x), math.sin(rot_x)

            cr.save()
            for x0, y0, z0 in self.dots:
                # 3D rotation
                x1 = x0 * cos_y + z0 * sin_y
                z1 = -x0 * sin_y + z0 * cos_y
                y1 = y0 * cos_x - z1 * sin_x
                z2 = y0 * sin_x + z1 * cos_x

                scale = 1.0 / (2.2 - z2 * 0.7)
                px = cx + x1 * SPHERE_RADIUS * 1.05 * scale
                py = REST_Y + y1 * SPHERE_RADIUS * 1.05 * scale
                alpha = 0.3 + 0.65 * ((z2 + 1.0) / 2.0)
                dot_r = 1.6 + 1.5 * scale

                # Soft amber/accent tint for thinking
                cr.set_source_rgba(DOT_FG[0], DOT_FG[1], DOT_FG[2], alpha)
                cr.arc(px, py, dot_r, 0, 2 * math.pi)
                cr.fill()
            cr.restore()

        elif self.state == "speaking":
            # Solid sphere of DotStyle dots pulsing with output audio
            audio_pulse = self.audio_level * 0.45 + math.sin(elapsed * 8.0) * 0.06
            rot_y = elapsed * 1.4
            cos_y, sin_y = math.cos(rot_y), math.sin(rot_y)

            cr.save()
            for x0, y0, z0 in self.dots:
                x1 = x0 * cos_y + z0 * sin_y
                z1 = -x0 * sin_y + z0 * cos_y
                y1 = y0

                disp = 1.0 + audio_pulse * (0.8 + 0.4 * math.sin(y1 * 4.0 + elapsed * 6.0))
                scale = 1.0 / (2.2 - z1 * 0.7)
                px = cx + x1 * SPHERE_RADIUS * disp * scale
                py = REST_Y + y1 * SPHERE_RADIUS * disp * scale
                alpha = 0.35 + 0.65 * ((z1 + 1.0) / 2.0)
                dot_r = (2.0 + 1.6 * scale) * (1.0 + self.audio_level * 0.5)

                if z1 > 0.3:
                    cr.set_source_rgba(self.accent[0], self.accent[1], self.accent[2], alpha)
                else:
                    cr.set_source_rgba(DOT_FG[0], DOT_FG[1], DOT_FG[2], alpha * 0.85)

                cr.arc(px, py, dot_r, 0, 2 * math.pi)
                cr.fill()
            cr.restore()


class SphereWindow(Gtk.Window):
    def __init__(self, initial_state="opening"):
        super().__init__(decorated=False)
        self.set_title("Aero Assistant Sphere")
        self.set_default_size(WIDTH, HEIGHT)
        self.effect = SphereEffect(initial_state)

        if HAS_LAYER_SHELL:
            Camada.init_for_window(self)
            Camada.set_layer(self, Camada.Layer.OVERLAY)
            Camada.set_namespace(self, "aero-sphere")
            Camada.set_anchor(self, Camada.Edge.TOP, True)
            Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
            Camada.set_exclusive_zone(self, -1)

        self.area = Gtk.DrawingArea(content_width=WIDTH, content_height=HEIGHT)
        self.area.set_draw_func(self.on_draw)
        self.set_child(self.area)

        # 60 FPS tick callback
        self.add_tick_callback(self.on_tick)
        start_ipc_server(self)

    def on_draw(self, area, cr, width, height):
        self.effect.draw(cr, width, height)

    def on_tick(self, widget, frame_clock):
        if getattr(self.effect, "finished", False):
            self.close()
            return False
        # Pause drawing during fullscreen game or app to save GPU/CPU
        if is_fullscreen_active():
            return True
        self.area.queue_draw()
        return True


def start_ipc_server(win):
    import socket
    import threading
    sock_path = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aero-sphere.sock")
    if os.path.exists(sock_path):
        try:
            os.remove(sock_path)
        except OSError:
            pass

    def socket_loop():
        try:
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            srv.bind(sock_path)
            while True:
                data, _ = srv.recvfrom(1024)
                if not data:
                    break
                text = data.decode(errors="replace").strip()
                handle_command(win, text)
        except Exception:
            pass

    def stdin_loop():
        try:
            for line in sys.stdin:
                handle_command(win, line.strip())
        except Exception:
            pass

    threading.Thread(target=socket_loop, daemon=True).start()
    threading.Thread(target=stdin_loop, daemon=True).start()


def handle_command(win, text):
    parts = text.split()
    if not parts:
        return
    cmd = parts[0].lower()
    if cmd == "state" and len(parts) > 1:
        st = parts[1].lower()
        if st in ("opening", "listening", "thinking", "speaking", "closing"):
            GLib.idle_add(win.effect.set_state, st)
    elif cmd in ("opening", "listening", "thinking", "speaking", "closing"):
        GLib.idle_add(win.effect.set_state, cmd)
    elif cmd == "audio" and len(parts) > 1:
        try:
            lvl = float(parts[1])
            GLib.idle_add(win.effect.set_audio_level, lvl)
        except ValueError:
            pass
    elif cmd in ("close", "exit", "quit"):
        GLib.idle_add(win.effect.set_state, "closing")


def render_to_png(state, output_path, audio_level=0.5):
    """Headless Cairo render to PNG for test verification and documentation."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
    cr = cairo.Context(surface)
    eff = SphereEffect(state)
    eff.set_audio_level(audio_level)
    # Simulate mid-animation
    eff.start_time = time.time() - 0.35
    eff.draw(cr, WIDTH, HEIGHT)
    surface.write_to_png(output_path)


def main():
    parser = argparse.ArgumentParser(description="Aero Assistant Sphere Effect")
    parser.add_argument("--state", default="opening", choices=["opening", "listening", "thinking", "speaking", "closing"])
    parser.add_argument("--screenshot", help="Render single frame of state to PNG and exit")
    parser.add_argument("--audio", type=float, default=0.5, help="Audio level 0.0 - 1.0")
    args = parser.parse_args()

    if args.screenshot:
        os.makedirs(os.path.dirname(os.path.abspath(args.screenshot)), exist_ok=True)
        render_to_png(args.state, args.screenshot, args.audio)
        print(f"Rendered {args.state} state to {args.screenshot}")
        return

    app = Gtk.Application(application_id="io.aero.AssistantSphere")

    def on_activate(application):
        win = SphereWindow(args.state)
        win.set_application(application)
        win.present()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
