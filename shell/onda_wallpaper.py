#!/usr/bin/env python3
"""
onda_wallpaper.py — a troca de papel de parede FLUIDA: o novo se desdobra por cima do velho numa onda
(a mesma família da onda de pontos do Hermes), com a borda líquida e um spray de pontinhos DotStyle na frente.

Quem chama: ~/.local/bin/papel-de-parede (Settings, Thunar, Super+Shift+W...). Não roda sozinho:
existe só durante a animação (~1 s) e some. Parado = zero custo.

Como funciona:
  1. decodifica as DUAS imagens já no tamanho da tela (do mesmo jeito que o hyprpaper faz "cover":
     cobre a tela e corta as sobras, centralizado). Tudo antes de aparecer qualquer coisa: nada engasga.
  2. abre uma superfície layer-shell na camada BACKGROUND (acima do hyprpaper, abaixo de TUDO: janelas,
     widgets da área de trabalho, barra). Sem mouse, sem teclado, zona exclusiva -1.
     O 1º quadro é o wallpaper velho, idêntico ao que já está na tela: abrir não muda nada.
  3. um shader (GLArea, OpenGL pelo libepoxy com ctypes, como o liquido.py) desdobra o novo a partir do
     ponteiro do mouse. Quando ele cobre a tela inteira, escreve "cobriu" na saída.
  4. o papel-de-parede troca o hyprpaper POR BAIXO (invisível: estamos por cima mostrando a mesma imagem)
     e escreve "soltar". A superfície some num fade curto e o processo termina.
  Se faltar algo (sem GL, imagem que o GdkPixbuf não abre...), escreve "falhou" e sai: o papel-de-parede
  faz a troca instantânea de sempre.

Uso: onda_wallpaper.py --velho A --novo B --monitor DP-2 [--ms 1200] [--x X --y Y]
     (X, Y = de onde a onda nasce, em pixels lógicos DO MONITOR; sem eles, o centro)

Modo --revelar (troca de TEMA/cores, quem chama é o aero-tema-onda): --velho é um print da tela de antes.
  A superfície vai pra camada OVERLAY (por cima de tudo, sem mouse nem teclado) mostrando o print parado,
  idêntico à tela: escreve "pronto". O tema novo é aplicado POR BAIXO; quando chega "ir" na entrada, a
  onda abre um buraco no print e o visual novo, ao vivo, aparece por ela. Quando o buraco cobre a tela,
  o processo termina sozinho (nada fica na tela).
"""
import argparse
import ctypes
import math
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

from liquido import (ARRAY_BUFFER, COLOR_BUFFER_BIT, FLOAT, STATIC_DRAW, TRIANGLE_STRIP,  # noqa: E402
                     GLenum, GLfloat, GLint, GLsizei, GLuint, _gl)

TEXTURE_2D, TEXTURE0, RGBA, RGBA8, UNSIGNED_BYTE = 0x0DE1, 0x84C0, 0x1908, 0x8058, 0x1401
MIN_FILTER, MAG_FILTER, WRAP_S, WRAP_T = 0x2801, 0x2800, 0x2802, 0x2803
LINEAR, CLAMP_TO_EDGE, UNPACK_ALIGNMENT, UNPACK_ROW_LENGTH = 0x2601, 0x812F, 0x0CF5, 0x0CF2

LIMITE_S = 12          # segurança: sem "soltar" até aqui, some sozinho
LIMITE_REVELAR_S = 20  # --revelar: sem "ir" até aqui, a onda começa sozinha (a troca de visual leva uns segundos)
FADE_S = 0.22          # o sumiço final (a troca por baixo já aconteceu: é só por garantia)
CELULA = 22            # espaço entre os pontinhos (o mesmo dot_spacing da onda do Hermes)
FAIXA = 130            # até onde o spray de pontos vai à frente da borda (px)

CORPO = """
uniform vec2 res;          // pixels do framebuffer
uniform vec2 tela;         // pixels lógicos (y pra baixo)
uniform sampler2D velho;
uniform sampler2D novo;
uniform vec2 o;            // de onde a onda nasce
uniform float R;           // raio da frente
uniform float amp;         // quanto a borda ondula
uniform float fase;        // segundos: a borda se mexe enquanto avança
uniform float zoom;        // o novo "desdobra": começa um tiquinho maior e assenta em 1
uniform float faixa, cel, pontos, geral, semente, final;
uniform float revela;      // 1 = modo --revelar: dentro da onda não é o "novo", é um buraco (o vivo aparece)
out vec4 cor;

float h21(vec2 c) { return fract(sin(dot(c, vec2(127.1, 311.7))) * 43758.5453); }
float ruido(vec2 x) {
  vec2 i = floor(x), f = fract(x);
  f = f * f * (3. - 2. * f);
  return mix(mix(h21(i), h21(i + vec2(1, 0)), f.x), mix(h21(i + vec2(0, 1)), h21(i + vec2(1, 1)), f.x), f.y);
}
// distância até a frente da onda (negativa = já é o wallpaper novo)
float frente(vec2 p) {
  vec2 v = p - o;
  float r = length(v), a = atan(v.y, v.x);
  float w = .5 * sin(3. * a + semente + fase * 1.3) + .3 * sin(5. * a - semente * 1.7 - fase * 2.1)
          + .2 * sin(8. * a + semente * 2.3 + fase * 3.);
  w += (ruido(p / 190. + vec2(fase * .3, semente)) - .5) * 1.2;
  return r - (R + amp * w * smoothstep(0., amp * 4., r));   // perto do centro não ondula (sem pontas)
}
vec3 tex(sampler2D s, vec2 p) { return texture(s, p / tela).rgb; }

void main() {
  vec2 p = vec2(gl_FragCoord.x, res.y - gl_FragCoord.y) * tela / res;
  if (final > .5) { cor = revela > .5 ? vec4(0.) : vec4(tex(novo, p) * geral, geral); return; }
  float aa = tela.x / res.x;
  float d = frente(p);
  vec3 col = tex(velho, p);
  if (revela > .5) {
    // o print de antes com um buraco que cresce; na borda, só a luz da lente (o vivo aparece por baixo)
    float al = 1.;
    if (d > 0.) col *= 1. - .16 * exp(-d / 6.);
    if (pontos > 0. && d > 0. && d < faixa * 1.6) {
      float lin = floor(p.y / (cel * .87));
      vec2 cc = vec2((floor(p.x / cel - mod(lin, 2.) * .5) + .5 + mod(lin, 2.) * .5) * cel, (lin + .5) * cel * .87);
      float dc = frente(cc) + (h21(cc + semente) - .5) * faixa * .5;
      if (dc > 0. && dc < faixa) {
        float q = 1. - dc / faixa;
        float rad = cel * .44 * pow(q, 1.35);
        al *= 1. - pontos * pow(q, .6) * (1. - smoothstep(rad - aa, rad + aa, length(p - cc)));
      }
    }
    float dentro = 1. - smoothstep(-aa, aa, d);
    al *= 1. - dentro;
    float luz = 0.;
    if (d < 1.5) {
      vec2 n = normalize(vec2(frente(p + vec2(1.5, 0.)) - d, frente(p + vec2(0., 1.5)) - d) + 1e-6);
      float b = smoothstep(-30., 0., d) * dentro;
      luz = .10 * b * b * b * max(0., dot(n, vec2(-.707, -.707))) + .04 * b * b;
    }
    cor = vec4(col * al + vec3(luz), al + luz) * geral;
    return;
  }
  // uma sombra bem leve do líquido sobre o velho, logo à frente da borda (dá profundidade)
  if (d > 0.) col *= 1. - .16 * exp(-d / 6.);
  // o spray DotStyle: pontinhos do wallpaper NOVO chegando antes da onda, maiores perto da borda
  if (pontos > 0. && d > 0. && d < faixa * 1.6) {
    float lin = floor(p.y / (cel * .87));
    vec2 cc = vec2((floor(p.x / cel - mod(lin, 2.) * .5) + .5 + mod(lin, 2.) * .5) * cel, (lin + .5) * cel * .87);
    float dc = frente(cc) + (h21(cc + semente) - .5) * faixa * .5;    // cada ponto chega num tempo seu
    if (dc > 0. && dc < faixa) {
      float q = 1. - dc / faixa;
      float rad = cel * .44 * pow(q, 1.35);
      float a = pontos * pow(q, .6) * (1. - smoothstep(rad - aa, rad + aa, length(p - cc)));
      col = mix(col, tex(novo, o + (cc - o) / zoom), a);
    }
  }
  // a onda: dentro, o novo; na borda, uma lente líquida que entorta um pouco e pega luz do alto à esquerda
  if (d < 1.5) {
    vec2 n = normalize(vec2(frente(p + vec2(1.5, 0.)) - d, frente(p + vec2(0., 1.5)) - d) + 1e-6);
    float b = smoothstep(-30., 0., d);
    vec3 cn = tex(novo, o + (p - o) / zoom - n * 10. * b * b);
    float luz = max(0., dot(n, vec2(-.707, -.707)));
    cn += vec3(1.) * (.10 * b * b * b * luz + .025 * b * b);
    col = mix(col, cn, 1. - smoothstep(-aa, aa, d));
  }
  cor = vec4(col * geral, geral);
}
"""


def bezier(x1, y1, x2, y2):
    """Curva de tempo estilo CSS cubic-bezier: devolve f(t) -> 0..1."""
    def f(t):
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(40):                          # acha o s em que x(s) = t (bisseção: sempre converge)
            s = (lo + hi) / 2
            x = 3 * (1 - s) ** 2 * s * x1 + 3 * (1 - s) * s * s * x2 + s ** 3
            lo, hi = (s, hi) if x < t else (lo, s)
        s = (lo + hi) / 2
        return 3 * (1 - s) ** 2 * s * y1 + 3 * (1 - s) * s * s * y2 + s ** 3
    return f


AVANCO = bezier(0.42, 0.0, 0.22, 1.0)       # sai devagar, ganha corpo e pousa macio nos cantos
ASSENTA = bezier(0.25, 1.0, 0.5, 1.0)        # a "suave" do Hyprland do Aero (o zoom assenta)


def suave(a, b, x):
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


def decodificar(caminho, w, h):
    """A imagem já no tamanho da tela, cortada como o "cover" do hyprpaper (cobre e centraliza)."""
    img = GdkPixbuf.Pixbuf.new_from_file(caminho)
    if not img.get_has_alpha():
        img = img.add_alpha(False, 0, 0, 0)
    iw, ih = img.get_width(), img.get_height()
    s = max(w / iw, h / ih)
    destino = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, w, h)
    img.scale(destino, 0, 0, w, h, -(iw * s - w) / 2, -(ih * s - h) / 2, s, s, GdkPixbuf.InterpType.BILINEAR)
    return destino.read_pixel_bytes().get_data(), destino.get_rowstride()


class Onda(Gtk.Window):
    def __init__(self, args, monitor, imagens, laco):
        super().__init__(decorated=False)
        self.args, self.imagens, self.laco = args, imagens, laco
        self.add_css_class("onda-wallpaper")
        Camada.init_for_window(self)
        self.revelar = args.revelar
        Camada.set_layer(self, Camada.Layer.OVERLAY if self.revelar else Camada.Layer.BACKGROUND)
        Camada.set_namespace(self, "aero-theme-wave" if self.revelar else "aero-wallpaper-wave")
        Camada.set_monitor(self, monitor)
        for borda in (Camada.Edge.TOP, Camada.Edge.BOTTOM, Camada.Edge.LEFT, Camada.Edge.RIGHT):
            Camada.set_anchor(self, borda, True)
        Camada.set_exclusive_zone(self, -1)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.area = Gtk.GLArea(hexpand=True, vexpand=True, can_target=False, has_depth_buffer=False)
        self.area.set_auto_render(False)
        self.area.connect("realize", self._realizar)
        self.area.connect("render", self._render)
        self.set_child(self.area)
        self.connect("realize", self._sem_entrada)
        self.ok = False
        self.tique = None
        self.t0 = None                     # o relógio começa no 1º quadro de verdade
        self.estado = "parado" if self.revelar else "onda"   # (parado ->) onda -> cobriu -> sumindo
        self.t_sumir = None
        self.semente = (time.time() * 7.31) % 6.283
        geo = monitor.get_geometry()
        self.tela = (geo.width, geo.height)
        self.o = (args.x if args.x is not None else geo.width / 2, args.y if args.y is not None else geo.height / 2)
        self.amp = min(self.tela) * 0.045
        cantos = [(0, 0), (geo.width, 0), (0, geo.height), (geo.width, geo.height)]
        self.rmax = max(math.hypot(cx - self.o[0], cy - self.o[1]) for cx, cy in cantos) + self.amp * 1.8 + FAIXA
        self.dur = max(0.3, args.ms / 1000)

    def _sem_entrada(self, *_):
        import cairo
        s = self.get_surface()
        if s is not None:
            s.set_input_region(cairo.Region())       # todo clique passa direto
        return False

    # ---------- GL ----------
    def _realizar(self, area):
        area.make_current()
        if area.get_error() is not None:
            return falhar(self.laco, "GLArea: %s" % area.get_error())
        try:
            es = area.get_context().get_use_es()
            topo = "#version 300 es\nprecision highp float;\n" if es else "#version 330 core\n"
            from liquido import CampoLiquido
            self.prog = CampoLiquido._programa(None, topo + "in vec2 pos; void main() { gl_Position = vec4(pos, 0., 1.); }",
                                               topo + CORPO)
            vao, vbo = GLuint(), GLuint()
            _gl("glGenVertexArrays", None, GLsizei, ctypes.POINTER(GLuint))(1, ctypes.byref(vao))
            self.f_vao = _gl("glBindVertexArray", None, GLuint)
            self.f_vao(vao)
            self.vao = vao
            _gl("glGenBuffers", None, GLsizei, ctypes.POINTER(GLuint))(1, ctypes.byref(vbo))
            _gl("glBindBuffer", None, GLenum, GLuint)(ARRAY_BUFFER, vbo)
            quad = (GLfloat * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
            _gl("glBufferData", None, GLenum, ctypes.c_ssize_t, ctypes.c_void_p, GLenum)(
                ARRAY_BUFFER, ctypes.sizeof(quad), ctypes.cast(quad, ctypes.c_void_p), STATIC_DRAW)
            loc = _gl("glGetAttribLocation", GLint, GLuint, ctypes.c_char_p)(self.prog, b"pos")
            _gl("glEnableVertexAttribArray", None, GLuint)(loc)
            _gl("glVertexAttribPointer", None, GLuint, GLint, GLenum, ctypes.c_ubyte, GLsizei, ctypes.c_void_p)(
                loc, 2, FLOAT, 0, 0, None)
            uni = _gl("glGetUniformLocation", GLint, GLuint, ctypes.c_char_p)
            self.u = {n: uni(self.prog, n.encode()) for n in
                      ("res", "tela", "velho", "novo", "o", "R", "amp", "fase", "zoom", "faixa", "cel", "pontos",
                       "geral", "semente", "final", "revela")}
            self.f_use = _gl("glUseProgram", None, GLuint)
            self.f_2f = _gl("glUniform2f", None, GLint, GLfloat, GLfloat)
            self.f_1f = _gl("glUniform1f", None, GLint, GLfloat)
            self.f_1i = _gl("glUniform1i", None, GLint, GLint)
            self.f_clearc = _gl("glClearColor", None, GLfloat, GLfloat, GLfloat, GLfloat)
            self.f_clear = _gl("glClear", None, ctypes.c_uint)
            self.f_draw = _gl("glDrawArrays", None, GLenum, GLint, GLsizei)
            self.f_ativa = _gl("glActiveTexture", None, GLenum)
            self.f_liga = _gl("glBindTexture", None, GLenum, GLuint)
            # as duas imagens viram texturas (já decodificadas: aqui é só copiar pra GPU)
            gerar = _gl("glGenTextures", None, GLsizei, ctypes.POINTER(GLuint))
            param = _gl("glTexParameteri", None, GLenum, GLenum, GLint)
            guardar = _gl("glPixelStorei", None, GLenum, GLint)
            subir = _gl("glTexImage2D", None, GLenum, GLint, GLint, GLsizei, GLsizei, GLint, GLenum, GLenum,
                        ctypes.c_char_p)
            self.tex = []
            for i, (dados, passo, w, h) in enumerate(self.imagens):
                t = GLuint()
                gerar(1, ctypes.byref(t))
                self.f_ativa(TEXTURE0 + i)
                self.f_liga(TEXTURE_2D, t)
                for k, v in ((MIN_FILTER, LINEAR), (MAG_FILTER, LINEAR), (WRAP_S, CLAMP_TO_EDGE), (WRAP_T, CLAMP_TO_EDGE)):
                    param(TEXTURE_2D, k, v)
                guardar(UNPACK_ALIGNMENT, 1)
                guardar(UNPACK_ROW_LENGTH, passo // 4)
                subir(TEXTURE_2D, 0, RGBA8, w, h, 0, RGBA, UNSIGNED_BYTE, dados)
                self.tex.append(t)
            guardar(UNPACK_ROW_LENGTH, 0)
            self.imagens = None                      # a cópia na RAM já não serve: a GPU tem a sua
            self.ok = True
        except (OSError, ValueError, RuntimeError, TypeError) as erro:
            falhar(self.laco, "GL: %s" % erro)
            return
        self.tique = area.add_tick_callback(self._quadro)

    def ir(self):
        """--revelar: o tema novo já está por baixo; a onda começa (o relógio zera no próximo quadro)."""
        if self.estado == "parado":
            self.estado, self.reiniciar = "onda", True      # o relógio zera no próximo quadro
            if self.ok and self.tique is None:
                self.tique = self.area.add_tick_callback(self._quadro)

    def _quadro(self, area, relogio):
        agora = relogio.get_frame_time() / 1e6
        if self.t0 is None or getattr(self, "reiniciar", False):
            self.t0, self.reiniciar = agora, False
        if self.estado == "parado":                  # o print parado: um quadro e pronto
            self.agora = agora
            area.queue_render()
            self.tique = None
            return GLib.SOURCE_REMOVE
        if self.estado == "sumindo" and self.t_sumir is None:
            self.t_sumir = agora
        self.agora = agora
        area.queue_render()
        if self.estado == "cobriu":                  # parado mostrando o novo: nada de redesenhar à toa
            self.tique = None
            return GLib.SOURCE_REMOVE
        if self.estado == "sumindo" and agora - self.t_sumir >= FADE_S:
            GLib.idle_add(self.laco.quit)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def _render(self, area, _ctx):
        self.f_clearc(0, 0, 0, 0)
        self.f_clear(COLOR_BUFFER_BIT)
        if not self.ok or self.t0 is None:
            return True
        el = self.agora - self.t0
        t = 0.0 if self.estado == "parado" else min(1.0, el / self.dur)
        geral = 1.0
        if self.estado == "sumindo":
            geral = 1 - suave(0, FADE_S, self.agora - self.t_sumir)
        esc = area.get_scale_factor()
        self.f_use(self.prog)
        self.f_vao(self.vao)
        for i, nome in enumerate(("velho", "novo")):
            self.f_ativa(TEXTURE0 + i)
            self.f_liga(TEXTURE_2D, self.tex[i])
            self.f_1i(self.u[nome], i)
        self.f_2f(self.u["res"], area.get_width() * esc, area.get_height() * esc)
        self.f_2f(self.u["tela"], area.get_width(), area.get_height())
        self.f_2f(self.u["o"], *self.o)
        self.f_1f(self.u["R"], AVANCO(t) * self.rmax)
        self.f_1f(self.u["amp"], self.amp)
        self.f_1f(self.u["fase"], el)
        self.f_1f(self.u["zoom"], 1 + 0.045 * (1 - ASSENTA(t)))
        self.f_1f(self.u["faixa"], FAIXA)
        self.f_1f(self.u["cel"], CELULA)
        # os pontos acendem rápido e apagam devagar, como o envelope da onda do Hermes
        self.f_1f(self.u["pontos"], 0.85 * suave(0.0, 0.1, t) * (1 - suave(0.55, 0.95, t)))
        self.f_1f(self.u["geral"], geral)
        self.f_1f(self.u["semente"], self.semente)
        self.f_1f(self.u["final"], 1.0 if t >= 1 else 0.0)
        self.f_1f(self.u["revela"], 1.0 if self.revelar else 0.0)
        self.f_draw(TRIANGLE_STRIP, 0, 4)
        if self.estado == "parado" and not getattr(self, "avisou_pronto", False):
            # o print foi DESENHADO, mas ainda não está na tela: só avisa "pronto" depois de 2 quadros
            # apresentados (o compositor já mostrou a camada). Antes, o tema trocava por baixo de uma
            # camada que ainda não tinha aparecido, e dava pra ver as peças trocando de cor.
            self.avisou_pronto = True
            quadros = [0]

            def apresentado(_w, _relogio):
                quadros[0] += 1
                if quadros[0] < 3:
                    return GLib.SOURCE_CONTINUE
                avisar("pronto")
                return GLib.SOURCE_REMOVE
            area.add_tick_callback(apresentado)
            return True
        if self.revelar and t >= 1 and self.estado == "onda":
            self.estado = "fim"                          # o buraco já cobre tudo: só sair
            GLib.idle_add(self.laco.quit)
            return True
        if t >= 1 and self.estado == "onda":
            self.estado = "cobriu"
            GLib.idle_add(avisar, "cobriu")          # o quadro final (só o novo) já foi desenhado
        return True

    def soltar(self):
        if self.estado != "sumindo":
            self.estado = "sumindo"
            if self.ok and self.tique is None:
                self.tique = self.area.add_tick_callback(self._quadro)
            elif not self.ok:
                GLib.idle_add(self.laco.quit)


def avisar(texto):
    try:
        print(texto, flush=True)
    except (BrokenPipeError, OSError):
        pass
    return False


def falhar(laco, motivo):
    print("onda_wallpaper: " + motivo, file=sys.stderr, flush=True)
    avisar("falhou")
    GLib.idle_add(laco.quit)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--velho", required=True)
    ap.add_argument("--novo", required=True)
    ap.add_argument("--monitor", default="")
    ap.add_argument("--ms", type=int, default=1200)
    ap.add_argument("--x", type=float)
    ap.add_argument("--y", type=float)
    ap.add_argument("--revelar", action="store_true")
    args = ap.parse_args()
    laco = GLib.MainLoop()

    Gtk.init()
    if not Camada.is_supported():
        return falhar(laco, "sem layer-shell") or 1
    monitores = Gdk.Display.get_default().get_monitors()
    monitor = next((monitores.get_item(i) for i in range(monitores.get_n_items())
                    if monitores.get_item(i).get_connector() == args.monitor), None) or monitores.get_item(0)
    geo = monitor.get_geometry()
    esc = monitor.get_scale()                        # fracionário também (1.25, 1.6...)
    w, h = round(geo.width * esc), round(geo.height * esc)

    # 1. as duas imagens, decodificadas em paralelo, ANTES de qualquer coisa aparecer
    imagens, erros = [None, None], []

    def carregar(i, caminho):
        try:
            dados, passo = decodificar(caminho, w, h)
            imagens[i] = (dados, passo, w, h)
        except GLib.Error as erro:
            erros.append(f"{caminho}: {erro.message}")
    fios = [threading.Thread(target=carregar, args=(i, c))
            for i, c in enumerate((args.velho,) if args.revelar else (args.velho, args.novo))]
    for f in fios:
        f.start()
    for f in fios:
        f.join()
    if args.revelar and imagens[0]:
        imagens[1] = imagens[0]                       # o "novo" nem é lido: dentro da onda é buraco
    if erros:
        falhar(laco, "; ".join(erros))
        return 1

    css = Gtk.CssProvider()
    css.load_from_string("window.onda-wallpaper { background: none; background-color: transparent; box-shadow: none; }")
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    janela = Onda(args, monitor, imagens, laco)

    # "soltar" (ou o papel-de-parede fechou a conversa) = pode sumir
    def ler(fonte, condicao):
        linha = sys.stdin.readline() if condicao & GLib.IOCondition.IN else ""
        if args.revelar and linha.strip() == "ir":
            janela.ir()
            return GLib.SOURCE_CONTINUE
        if not linha or linha.strip() == "soltar":
            janela.ir() if args.revelar else janela.soltar()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
    GLib.io_add_watch(GLib.IOChannel.unix_new(sys.stdin.fileno()), GLib.PRIORITY_DEFAULT,
                      GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR, ler)
    if args.revelar:                                  # segurança: o print nunca fica congelado na tela
        GLib.timeout_add_seconds(LIMITE_REVELAR_S, lambda: janela.ir() or False)
        GLib.timeout_add_seconds(LIMITE_REVELAR_S + 4, lambda: laco.quit() or False)
    else:
        GLib.timeout_add_seconds(LIMITE_S, lambda: janela.soltar() or False)

    janela.present()
    laco.run()
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
