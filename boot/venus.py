"""venus.py: the Aero OS start-up animation, as a GTK widget (login screen and desktop intro).

The same ASCII Venus de Milo as the boot splash (frames from make-frames.py, installed in
/usr/share/aero/venus). Three things it can do:
  loop()                 the statue breathing: wave, marble shimmer, now and then a glitch
  enter(secs, done)      glitches IN from nothing and settles on the still statue
  leave(secs, done)      glitches OUT: bands slide, colours split, everything fades to nothing
The black behind the statue fades with it (`backdrop`), so what is underneath shows through.
"""
# Por que um widget só: a tela de login termina com a estátua parada, e o desktop começa com a
# MESMA estátua parada no mesmo lugar e tamanho. O instante preto entre os dois (quando o sistema
# troca a tela do usuário "greeter" para o seu) vira só uma piscada do glitch.
import math
import os
import random

import cairo
from gi.repository import GLib, Gtk

PASTA = "/usr/share/aero/venus"
FPS = 25


class Venus(Gtk.DrawingArea):
    def __init__(self, pasta=PASTA, backdrop=True):
        super().__init__(hexpand=True, vexpand=True)
        self.frames = []
        i = 0
        while os.path.exists(os.path.join(pasta, f"frame-{i:02d}.png")):
            self.frames.append(cairo.ImageSurface.create_from_png(os.path.join(pasta, f"frame-{i:02d}.png")))
            i += 1
        self.still = cairo.ImageSurface.create_from_png(os.path.join(pasta, "still.png"))
        self.backdrop = 1.0 if backdrop else 0.0   # opacidade do preto de fundo
        self.mode, self.t0, self.dur, self.done = "still", 0.0, 1.0, None
        self.set_draw_func(self.draw)
        self.add_tick_callback(self.tick)

    # ---------- comandos ----------
    def _start(self, mode, secs, done):
        self.mode, self.dur, self.done = mode, secs, done
        self.t0 = GLib.get_monotonic_time() / 1e6

    def loop(self):
        self._start("loop", 1.0, None)

    def enter(self, secs=0.6, done=None, fade_backdrop=False):
        self.fade_backdrop = fade_backdrop      # o preto também vem aparecendo (sobre a tela de login)
        self._start("enter", secs, done)

    def leave(self, secs=0.9, done=None, keep_backdrop=False):
        self.keep_backdrop = keep_backdrop
        self._start("leave", secs, done)

    def hold(self):
        self._start("still", 1.0, None)

    # ---------- tempo ----------
    def tick(self, *_):
        if self.mode in ("enter", "leave"):
            if self.progress() >= 1.0:
                done, self.done = self.done, None
                self.mode = "still" if self.mode == "enter" else "gone"
                if done:
                    GLib.idle_add(done)
        self.queue_draw()
        return GLib.SOURCE_CONTINUE

    def progress(self):
        return min(1.0, (GLib.get_monotonic_time() / 1e6 - self.t0) / self.dur)

    # ---------- desenho ----------
    def draw(self, _area, ctx, w, h):
        now = GLib.get_monotonic_time() / 1e6
        p = self.progress()
        if self.mode == "gone":
            if not getattr(self, "keep_backdrop", False):
                return
            ctx.set_source_rgb(0, 0, 0)
            ctx.paint()
            return
        # Quanto do quadro aparece, quanto glitch, quanto preto atrás.
        if self.mode == "enter":
            alpha, glitch = p, (1 - p) * 1.4
            back = self.backdrop * (min(1.0, p * 1.6) if getattr(self, "fade_backdrop", False) else 1.0)
        elif self.mode == "leave":
            alpha, glitch = 1 - p, p * 1.6
            back = self.backdrop if getattr(self, "keep_backdrop", False) else self.backdrop * (1 - p) ** 1.5
        else:
            alpha, glitch, back = 1.0, 0.0, self.backdrop
        if back > 0:
            ctx.set_source_rgba(0, 0, 0, back)
            ctx.paint()
        if self.mode == "loop" and self.frames:
            img = self.frames[int(now * FPS) % len(self.frames)]
        elif self.mode in ("enter", "leave") and self.frames:
            img = self.frames[int(now * FPS * 2) % len(self.frames)]
        else:
            img = self.still
        # Tamanho: a estátua ocupa ~60% da altura, centrada (igual no login e no desktop).
        iw, ih = img.get_width(), img.get_height()
        s = h * 0.62 / ih
        x0, y0 = (w - iw * s) / 2, (h - ih * s) / 2
        if glitch <= 0.01:
            ctx.save()
            ctx.translate(x0, y0)
            ctx.scale(s, s)
            ctx.set_source_surface(img, 0, 0)
            ctx.paint_with_alpha(alpha)
            ctx.restore()
            return
        # Glitch: a imagem em faixas horizontais; cada faixa escorrega para o lado, e uma cópia
        # vermelha e outra ciano saem do lugar (as cores "separadas" de um sinal ruim).
        rnd = random.Random(int(now * 30))
        band = max(4, int(ih / 28))
        for y in range(0, ih, band):
            jump = (rnd.random() - 0.5) * 2 * glitch * 60 if rnd.random() < 0.35 + glitch * 0.3 else 0
            for (r, g, b, dx, a) in ((1, 0.25, 0.35, -4 * glitch, 0.5), (0.2, 0.9, 1, 4 * glitch, 0.5), (None, None, None, 0, 1)):
                ctx.save()
                ctx.rectangle(x0, y0 + y * s, iw * s + 200, band * s + 1)
                ctx.clip()
                ctx.translate(x0 + jump + dx * 3, y0)
                ctx.scale(s, s)
                if r is None:
                    ctx.set_source_surface(img, 0, 0)
                    ctx.paint_with_alpha(alpha)
                else:
                    ctx.set_source_rgba(r, g, b, alpha * a * min(1, glitch))
                    ctx.mask_surface(img, 0, 0)
                ctx.restore()
