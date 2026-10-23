"""
liquido.py — the liquid dots of DotStyle, drawn on the GPU (a Gtk.GLArea with one fragment shader).

Every shape (a circle for now) is a drop. Drops close to each other join with a liquid neck, like two
drops of mercury touching (a "smooth minimum" of their distance fields), and the space around all of them
is the DotStyle halftone: big bright dots against the liquid, smaller and fainter farther out.
The same idea the future Daimon compositor would use for every window.

Usage: put a CampoLiquido under your cairo drawing (Gtk.Overlay), call .formas([(x, y, r), ...]) with the
shapes in logical pixels each frame something moves, and draw the content (icons, text) on top with cairo.
If OpenGL is not available, .ok stays False and the caller keeps its old drawing.

OpenGL is reached through libepoxy (the library GTK itself uses) with ctypes: no extra Python package.
"""
import ctypes
import math
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

MAX = 160                       # shapes per frame (bubbles + the drops that are merging into them)

_epoxy = None


def _gl(nome, res, *args):
    """A GL function from libepoxy (epoxy_glX is a global holding the function pointer)."""
    global _epoxy
    if _epoxy is None:
        _epoxy = ctypes.CDLL("libepoxy.so.0")
    ptr = ctypes.c_void_p.in_dll(_epoxy, "epoxy_" + nome).value
    return ctypes.CFUNCTYPE(res, *args)(ptr)


GLuint, GLint, GLenum, GLsizei, GLfloat = ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_int, ctypes.c_float
VERTEX_SHADER, FRAGMENT_SHADER = 0x8B31, 0x8B30
COMPILE_STATUS, LINK_STATUS = 0x8B81, 0x8B82
ARRAY_BUFFER, STATIC_DRAW, FLOAT, TRIANGLE_STRIP = 0x8892, 0x88E4, 0x1406, 0x0005
COLOR_BUFFER_BIT, BLEND, ONE, ONE_MINUS_SRC_ALPHA = 0x4000, 0x0BE2, 1, 0x0303

CORPO = """
uniform vec2 res;          // device pixels
uniform float esc;         // device pixels per logical pixel
uniform int n;
uniform vec3 b[%(MAX)d];   // x, y (logical, y down), radius
uniform int nr;
uniform vec4 rr[4];        // windows: centre x, y, half width, half height (logical)
uniform float rc[4];       // their corner radius
uniform float k;           // how eagerly drops join (the size of the neck), logical px
uniform float celula;      // halftone cell, logical px
uniform float alcance;     // how far the halftone reaches, logical px
uniform float geral;       // global fade (opening / closing)
uniform float t;           // seconds: the oil film drifts slowly
out vec4 cor;

float smin(float a, float c, float kk) {
  float h = max(kk - abs(a - c), 0.) / kk;
  return min(a, c) - h * h * kk * .25;
}
// the field, and "duro": the plain nearest shape without joins (the gap between both is the liquid neck)
float caixa(vec2 p, vec4 r, float c) {
  vec2 q = abs(p - r.xy) - (r.zw - c);
  return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - c;
}
float campo2(vec2 p, out float duro) {
  float d = 1e5; duro = 1e5;
  for (int i = 0; i < %(MAX)d; i++) {
    if (i >= n) break;
    vec3 s = b[i];
    if (s.z > .3) { float e = length(p - s.xy) - s.z; d = smin(d, e, k); duro = min(duro, e); }
  }
  for (int i = 0; i < 4; i++) {
    if (i >= nr) break;
    float e = caixa(p, rr[i], rc[i]); d = smin(d, e, k * 1.6); duro = min(duro, e);
  }
  return d;
}
float campo(vec2 p) {
  float d = 1e5;
  for (int i = 0; i < %(MAX)d; i++) {
    if (i >= n) break;
    vec3 s = b[i];
    if (s.z > .3) d = smin(d, length(p - s.xy) - s.z, k);
  }
  for (int i = 0; i < 4; i++) {
    if (i >= nr) break;
    d = smin(d, caixa(p, rr[i], rc[i]), k * 1.6);
  }
  return d;
}
void main() {
  vec2 p = vec2(gl_FragCoord.x, res.y - gl_FragCoord.y) / esc;
  vec3 fg = vec3(.925, .92, .91);
  float duro;
  float d = campo2(p, duro);
  vec4 saida = vec4(0.);
  // halftone around the liquid (one dot per cell, sized by the cell centre's distance)
  float cel = celula;
  float lin = floor(p.y / (cel * .87));
  vec2 cc = vec2((floor(p.x / cel - mod(lin, 2.) * .5) + .5 + mod(lin, 2.) * .5) * cel, (lin + .5) * cel * .87);
  float dc = campo(cc);
  if (dc > 0. && dc < alcance) {
    float q = 1. - dc / alcance;
    float rad = .55 + cel * .36 * pow(q, 1.5);
    float a = (.05 + .5 * pow(q, 1.3)) * (1. - smoothstep(rad - 1. / esc, rad, length(p - cc)));
    saida = vec4(fg * a, a);
  }
  // the liquid: dark glass, a lit rim, a glossy highlight from the top-left
  if (d < 1.5) {
    float dl = campo(p + vec2(4., 4.));
    float rim = smoothstep(-4., 0., d);
    float brilho = clamp((dl - d) * .22, 0., 1.) * smoothstep(-24., -2., d);
    vec3 liq = vec3(.075, .075, .085) + vec3(.9) * (.07 * rim + .32 * brilho * brilho);
    // liquid vs solid: necks and the rim get a faded thin film of oil (drifting iridescent colours)
    // and are see-through like glass, so the liquid reads apart from the solid body of a shape
    float ponte = clamp((duro - d) / 6., 0., 1.);
    float borda = 1. - smoothstep(0., 6., -d);           // a thin rim: small drops stay solid
    vec3 oleo = .5 + .5 * cos(6.2831 * (d * .05 + t * .12 + (p.x - p.y) * .0022 + vec3(0., .33, .67)));
    liq = mix(liq, liq * .45 + oleo * .5, clamp(ponte * .9 + borda * .3, 0., 1.) * .55);
    float a = mix(.96, .72, max(borda, ponte * .85)) * (1. - smoothstep(-.8 / esc, .8 / esc, d));
    saida = vec4(liq * a, a) + saida * (1. - a);
  }
  cor = saida * geral;
}
""" % {"MAX": MAX}


# Solid (visual Solid, Material): a mesma união líquida (smin) das formas, mas CHAPADA — uma cor só, sem
# meio-tom, sem brilho, sem óleo. É o metaball das animações do Solid: um bloco nascendo do botão com um
# "pescoço" líquido, um cartão se soltando do sino... Só existe durante a animação (quem usa tira do overlay).
CORPO_SOLIDO = """
uniform vec2 res;
uniform float esc;
uniform int n;
uniform vec3 b[%(MAX)d];
uniform int nr;
uniform vec4 rr[4];
uniform float rc[4];
uniform float k;
uniform float geral;
uniform vec4 cor_solida;   // rgb + alfa (não pré-multiplicado)
out vec4 cor;

float smin(float a, float c, float kk) {
  float h = max(kk - abs(a - c), 0.) / kk;
  return min(a, c) - h * h * kk * .25;
}
float caixa(vec2 p, vec4 r, float c) {
  vec2 q = abs(p - r.xy) - (r.zw - c);
  return length(max(q, 0.)) + min(max(q.x, q.y), 0.) - c;
}
void main() {
  vec2 p = vec2(gl_FragCoord.x, res.y - gl_FragCoord.y) / esc;
  float d = 1e5;
  for (int i = 0; i < %(MAX)d; i++) {
    if (i >= n) break;
    vec3 s = b[i];
    if (s.z > .3) d = smin(d, length(p - s.xy) - s.z, k);
  }
  for (int i = 0; i < 4; i++) {
    if (i >= nr) break;
    d = smin(d, caixa(p, rr[i], rc[i]), k);
  }
  float a = (1. - smoothstep(-.75 / esc, .75 / esc, d)) * cor_solida.a * geral;
  cor = vec4(cor_solida.rgb * a, a);
}
""" % {"MAX": MAX}


class CampoLiquido(Gtk.GLArea):
    corpo = CORPO

    def __init__(self, k=16.0, celula=13.0, alcance=64.0):
        super().__init__(hexpand=True, vexpand=True, can_target=False, has_depth_buffer=False)
        self.set_auto_render(False)
        self.k, self.celula, self.alcance = k, celula, alcance
        self._formas, self.geral, self._janelas = [], 1.0, []
        self.ok = False
        self.prog = None
        self.connect("realize", self._realizar)
        self.connect("render", self._render)

    def formas(self, lista, geral=1.0, janelas=()):
        """lista: drops [(x, y, r)]; janelas: up to 4 windows [(cx, cy, half_w, half_h, corner)], all in
        logical pixels. Windows join the drops with the same liquid necks. Redraws on the next frame."""
        self._formas, self.geral, self._janelas = lista[:MAX], geral, list(janelas)[:4]
        if self.ok:
            self.queue_render()

    # ---------- GL ----------

    def _realizar(self, _a):
        self.make_current()
        if self.get_error() is not None:
            return
        try:
            es = self.get_context().get_use_es()
            topo = "#version 300 es\nprecision highp float;\n" if es else "#version 330 core\n"
            vs = topo + "in vec2 pos; void main() { gl_Position = vec4(pos, 0., 1.); }"
            self.prog = self._programa(vs, topo + self.corpo)
            gen = _gl("glGenVertexArrays", None, GLsizei, ctypes.POINTER(GLuint))
            bind_vao = _gl("glBindVertexArray", None, GLuint)
            vao, vbo = GLuint(), GLuint()
            gen(1, ctypes.byref(vao))
            bind_vao(vao)
            _gl("glGenBuffers", None, GLsizei, ctypes.POINTER(GLuint))(1, ctypes.byref(vbo))
            _gl("glBindBuffer", None, GLenum, GLuint)(ARRAY_BUFFER, vbo)
            quad = (GLfloat * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
            _gl("glBufferData", None, GLenum, ctypes.c_ssize_t, ctypes.c_void_p, GLenum)(
                ARRAY_BUFFER, ctypes.sizeof(quad), ctypes.cast(quad, ctypes.c_void_p), STATIC_DRAW)
            loc = _gl("glGetAttribLocation", GLint, GLuint, ctypes.c_char_p)(self.prog, b"pos")
            _gl("glEnableVertexAttribArray", None, GLuint)(loc)
            _gl("glVertexAttribPointer", None, GLuint, GLint, GLenum, ctypes.c_ubyte, GLsizei, ctypes.c_void_p)(
                loc, 2, FLOAT, 0, 0, None)
            self.vao = vao
            uni = _gl("glGetUniformLocation", GLint, GLuint, ctypes.c_char_p)
            self.u = {nome: uni(self.prog, nome.encode()) for nome in
                      ("res", "esc", "n", "b", "k", "celula", "alcance", "geral", "t", "nr", "rr", "rc",
                       "cor_solida")}
            self.f_4f = _gl("glUniform4f", None, GLint, GLfloat, GLfloat, GLfloat, GLfloat)
            self.f_use = _gl("glUseProgram", None, GLuint)
            self.f_vao = bind_vao
            self.f_2f = _gl("glUniform2f", None, GLint, GLfloat, GLfloat)
            self.f_1f = _gl("glUniform1f", None, GLint, GLfloat)
            self.f_1i = _gl("glUniform1i", None, GLint, GLint)
            self.f_3fv = _gl("glUniform3fv", None, GLint, GLsizei, ctypes.POINTER(GLfloat))
            self.f_4fv = _gl("glUniform4fv", None, GLint, GLsizei, ctypes.POINTER(GLfloat))
            self.f_1fv = _gl("glUniform1fv", None, GLint, GLsizei, ctypes.POINTER(GLfloat))
            self.f_clearc = _gl("glClearColor", None, GLfloat, GLfloat, GLfloat, GLfloat)
            self.f_clear = _gl("glClear", None, ctypes.c_uint)
            self.f_draw = _gl("glDrawArrays", None, GLenum, GLint, GLsizei)
            self.ok = True
        except (OSError, ValueError, RuntimeError) as erro:
            print("liquido: no GL,", erro, flush=True)
            self.ok = False

    def _programa(self, vs, fs):
        criar = _gl("glCreateShader", GLuint, GLenum)
        fonte = _gl("glShaderSource", None, GLuint, GLsizei, ctypes.POINTER(ctypes.c_char_p), ctypes.c_void_p)
        compilar = _gl("glCompileShader", None, GLuint)
        iv = _gl("glGetShaderiv", None, GLuint, GLenum, ctypes.POINTER(GLint))
        log = _gl("glGetShaderInfoLog", None, GLuint, GLsizei, ctypes.c_void_p, ctypes.c_char_p)
        prog = _gl("glCreateProgram", GLuint)()
        for tipo, src in ((VERTEX_SHADER, vs), (FRAGMENT_SHADER, fs)):
            s = criar(tipo)
            arr = (ctypes.c_char_p * 1)(src.encode())
            fonte(s, 1, arr, None)
            compilar(s)
            ok = GLint()
            iv(s, COMPILE_STATUS, ctypes.byref(ok))
            if not ok.value:
                buf = ctypes.create_string_buffer(4096)
                log(s, 4096, None, buf)
                raise RuntimeError(buf.value.decode(errors="replace"))
            _gl("glAttachShader", None, GLuint, GLuint)(prog, s)
        _gl("glLinkProgram", None, GLuint)(prog)
        ok = GLint()
        _gl("glGetProgramiv", None, GLuint, GLenum, ctypes.POINTER(GLint))(prog, LINK_STATUS, ctypes.byref(ok))
        if not ok.value:
            raise RuntimeError("link failed")
        return prog

    def _render(self, _a, _ctx):
        self.f_clearc(0, 0, 0, 0)
        self.f_clear(COLOR_BUFFER_BIT)
        if not self.ok or not (self._formas or self._janelas) or self.geral <= 0.004:
            return True
        esc = self.get_scale_factor()
        w, h = self.get_width() * esc, self.get_height() * esc
        self.f_use(self.prog)
        self.f_vao(self.vao)
        dados = (GLfloat * max(3, 3 * len(self._formas)))(*[v for f in self._formas for v in f])
        self.f_2f(self.u["res"], w, h)
        self.f_1f(self.u["esc"], esc)
        self.f_1i(self.u["n"], len(self._formas))
        self.f_3fv(self.u["b"], max(1, len(self._formas)), dados)
        self.f_1i(self.u["nr"], len(self._janelas))
        if self._janelas:
            rs = (GLfloat * (4 * len(self._janelas)))(*[v for j in self._janelas for v in j[:4]])
            cs = (GLfloat * len(self._janelas))(*[j[4] for j in self._janelas])
            self.f_4fv(self.u["rr"], len(self._janelas), rs)
            self.f_1fv(self.u["rc"], len(self._janelas), cs)
        self.f_1f(self.u["k"], self.k)
        self.f_1f(self.u["celula"], self.celula)
        self.f_1f(self.u["alcance"], self.alcance)
        self.f_1f(self.u["geral"], self.geral)
        self.f_1f(self.u["t"], (time.monotonic() % 1000.0))
        if self.u["cor_solida"] >= 0:
            self.f_4f(self.u["cor_solida"], *self.cor)
        self.f_draw(TRIANGLE_STRIP, 0, 4)
        return True


class CampoSolido(CampoLiquido):
    """O metaball chapado do Solid: CampoSolido(cor=(r, g, b), k=22). .formas(gotas, geral, janelas) como no
    CampoLiquido. Feito pra viver só durante uma animação: quem cria tira do overlay quando ela acaba."""
    corpo = CORPO_SOLIDO

    def __init__(self, cor=(0.2, 0.2, 0.2), alfa=1.0, k=22.0):
        super().__init__(k=k)
        self.cor = (*cor[:3], alfa)


def gotas_de_nascimento(x, y, r, idade, semente):
    """A bubble being born: 3 drops appear around its place and flow into it while it grows, so it
    looks poured rather than popped. idade = seconds since birth. Returns [(x, y, r)] (empty when done)."""
    if idade >= 0.55 or idade < 0:
        return []
    t = idade / 0.55
    e = 1 - (1 - t) ** 3
    out = []
    for i in range(3):
        a = semente * 2.399 + i * 2.094
        dist = r * (1.9 + 0.35 * math.sin(semente + i)) * (1 - e)
        out.append((x + math.cos(a) * dist, y + math.sin(a) * dist, r * (0.42 + 0.1 * i) * (1 - e * 0.85)))
    return out
