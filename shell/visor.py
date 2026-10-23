"""
visor.py — the media viewer inside ORBIT: images (whole, any shape), GIFs, videos and sound.

The viewer is a liquid window (liquido.py draws its body and the necks); this widget paints only what is
inside it, clipped to the window's rounded shape:
- images: the whole picture fitted to the window, zoom (wheel, around the pointer) and pan (drag);
  pixel art stays crisp (nearest filter when zoomed in);
- GIFs: every frame, at its own timing;
- video and sound: Gtk.MediaFile (GStreamer, built into GTK). Needs the codec plugins installed
  (gst-plugins-good / -bad / gst-libav); without them the viewer says so instead of failing.

The owner (orbit.py) moves the window (Visor.janela) and forwards input; this module only loads and paints.
"""
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Graphene, Gsk, Gtk  # noqa: E402

IMAGEM = {"png", "jpg", "jpeg", "webp", "bmp", "svg", "avif", "heic", "tif", "tiff", "ico", "jxl"}
VIDEO = {"mp4", "mkv", "webm", "mov", "avi", "m4v", "ogv"}
SOM = {"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac"}


def tipo_midia(path):
    ext = path.rsplit(".", 1)[-1].casefold() if "." in path else ""
    if ext == "gif":
        return "gif"
    if ext in IMAGEM:
        return "img"
    if ext in VIDEO:
        return "vid"
    if ext in SOM:
        return "som"
    return None


def irmaos(path):
    """Every viewable file in the same folder, sorted by name (left/right walks through them)."""
    pasta = os.path.dirname(path)
    try:
        nomes = sorted((e.name for e in os.scandir(pasta) if e.is_file() and not e.name.startswith(".")),
                       key=str.casefold)
    except OSError:
        return [path]
    lista = [os.path.join(pasta, n) for n in nomes if tipo_midia(n)]
    return lista or [path]


class Visor(Gtk.Widget):
    def __init__(self):
        super().__init__(can_target=False, hexpand=True, vexpand=True)
        self.janela = None               # (cx, cy, half_w, half_h, corner) on screen, set by the owner
        self.alfa = 0.0                  # content opacity (shows once the liquid window has formed)
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        self.path, self.tipo = None, None
        self.textura = None              # images and GIF frames
        self.midia = None                # Gtk.MediaFile (video / sound)
        self.tamanho = None              # (w, h) of the picture / video, once known
        self.erro = None
        self._gif, self._gif_fonte, self._geracao = None, None, 0
        self.ao_mudar = None             # owner callback: the size became known, or the video advanced

    # ---------- loading ----------

    def carregar(self, path):
        self.parar()
        self._geracao += 1
        g = self._geracao
        self.path, self.tipo = path, tipo_midia(path)
        self.textura, self.tamanho, self.erro = None, None, None
        self.zoom, self.pan = 1.0, [0.0, 0.0]
        if self.tipo == "img":
            def trabalho():
                try:
                    tex = Gdk.Texture.new_from_filename(path)
                except GLib.Error:
                    try:                                    # formats GTK can't read itself: gdk-pixbuf loaders
                        tex = Gdk.Texture.new_for_pixbuf(GdkPixbuf.Pixbuf.new_from_file(path))
                    except GLib.Error as e:
                        GLib.idle_add(self._falhou, g, e.message)
                        return
                GLib.idle_add(self._pronta, g, tex)
            threading.Thread(target=trabalho, daemon=True).start()
        elif self.tipo == "gif":
            def trabalho():
                try:
                    anim = GdkPixbuf.PixbufAnimation.new_from_file(path)
                except GLib.Error as e:
                    GLib.idle_add(self._falhou, g, e.message)
                    return
                GLib.idle_add(self._gif_pronto, g, anim)
            threading.Thread(target=trabalho, daemon=True).start()
        elif self.tipo in ("vid", "som"):
            m = Gtk.MediaFile.new_for_filename(path)
            m.set_loop(self.tipo == "vid")
            m.connect("invalidate-contents", lambda *_: self.queue_draw())
            m.connect("invalidate-size", lambda *_: self._tamanho_video())
            m.connect("notify::error", lambda *_: self._falhou(g, m.get_error().message if m.get_error() else "error"))
            m.connect("notify::timestamp", lambda *_: self.ao_mudar and self.ao_mudar("tempo"))
            self.midia = m
            m.play()
            if self.tipo == "som":
                self.tamanho = (420, 420)
                GLib.idle_add(lambda: (self.ao_mudar and self.ao_mudar("tamanho"), False)[1])
        self.queue_draw()

    def _pronta(self, g, tex):
        if g == self._geracao:
            self.textura, self.tamanho = tex, (tex.get_width(), tex.get_height())
            if self.ao_mudar:
                self.ao_mudar("tamanho")
            self.queue_draw()
        return False

    def _falhou(self, g, msg):
        if g == self._geracao:
            self.erro = msg
            if self.tamanho is None:
                self.tamanho = (520, 320)
            if self.ao_mudar:
                self.ao_mudar("tamanho")
            self.queue_draw()
        return False

    def _tamanho_video(self):
        m = self.midia
        if m is not None and m.get_intrinsic_width() > 0:
            self.tamanho = (m.get_intrinsic_width(), m.get_intrinsic_height())
            if self.ao_mudar:
                self.ao_mudar("tamanho")

    def _gif_pronto(self, g, anim):
        if g != self._geracao:
            return False
        if anim.is_static_image():
            return self._pronta(g, Gdk.Texture.new_for_pixbuf(anim.get_static_image()))
        self._gif = anim.get_iter(None)
        self.tamanho = (anim.get_width(), anim.get_height())
        if self.ao_mudar:
            self.ao_mudar("tamanho")
        self._gif_quadro(g)
        return False

    def _gif_quadro(self, g):
        self._gif_fonte = None
        if g != self._geracao or self._gif is None:
            return False
        self._gif.advance(None)
        self.textura = Gdk.Texture.new_for_pixbuf(self._gif.get_pixbuf())
        self.queue_draw()
        atraso = self._gif.get_delay_time()
        self._gif_fonte = GLib.timeout_add(max(20, atraso if atraso > 0 else 100), self._gif_quadro, g)
        return False

    def parar(self):
        if self._gif_fonte:
            GLib.source_remove(self._gif_fonte)
            self._gif_fonte = None
        self._gif = None
        if self.midia is not None:
            self.midia.pause()
            self.midia = None

    # ---------- controls ----------

    def tocar_pausar(self):
        if self.midia is not None:
            self.midia.pause() if self.midia.get_playing() else self.midia.play()
        elif self._gif_fonte:
            GLib.source_remove(self._gif_fonte)
            self._gif_fonte = None
        elif self._gif is not None:
            self._gif_quadro(self._geracao)

    def tocando(self):
        return (self.midia is not None and self.midia.get_playing()) or bool(self._gif_fonte)

    def progresso(self):
        """(fraction, seconds, total seconds) of a video / sound, or None."""
        m = self.midia
        if m is None or m.get_duration() <= 0:
            return None
        return m.get_timestamp() / m.get_duration(), m.get_timestamp() / 1e6, m.get_duration() / 1e6

    def buscar(self, fracao):
        m = self.midia
        if m is not None and m.get_duration() > 0 and m.is_seekable():
            m.seek(int(max(0.0, min(1.0, fracao)) * m.get_duration()))

    def ajuste(self):
        """Scale that fits the whole picture in the window (zoom 1)."""
        if self.janela is None or not self.tamanho:
            return 1.0
        _cx, _cy, hw, hh, _c = self.janela
        return min(2 * hw / self.tamanho[0], 2 * hh / self.tamanho[1])

    def ampliar(self, fator, px=None, py=None):
        """Zoom by `fator` keeping the point under the pointer still."""
        if self.janela is None:
            return
        cx, cy = self.janela[0], self.janela[1]
        novo = max(1.0, min(40.0, self.zoom * fator))
        k = novo / self.zoom
        if px is not None:
            ox, oy = px - cx - self.pan[0], py - cy - self.pan[1]
            self.pan[0] -= ox * (k - 1)
            self.pan[1] -= oy * (k - 1)
        self.zoom = novo
        if self.zoom <= 1.0001:
            self.pan = [0.0, 0.0]
        self.queue_draw()

    # ---------- painting ----------

    def do_snapshot(self, snap):
        if self.janela is None or self.alfa <= 0.01:
            return
        cx, cy, hw, hh, canto = self.janela
        borda = Graphene.Rect().init(cx - hw, cy - hh, 2 * hw, 2 * hh)
        clip = Gsk.RoundedRect()
        clip.init_from_rect(borda, max(0.0, canto - 1))
        snap.push_rounded_clip(clip)
        snap.push_opacity(self.alfa)
        if self.tamanho and (self.textura is not None or (self.midia is not None and self.tipo == "vid")):
            s = self.ajuste() * self.zoom
            w, h = self.tamanho[0] * s, self.tamanho[1] * s
            x, y = cx - w / 2 + self.pan[0], cy - h / 2 + self.pan[1]
            if self.textura is not None:
                filtro = Gsk.ScalingFilter.NEAREST if s >= 2 else Gsk.ScalingFilter.TRILINEAR
                snap.append_scaled_texture(self.textura, filtro, Graphene.Rect().init(x, y, w, h))
            else:
                snap.save()
                snap.translate(Graphene.Point().init(x, y))
                self.midia.snapshot(snap, w, h)
                snap.restore()
        snap.pop()
        snap.pop()
