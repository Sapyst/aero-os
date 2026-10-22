#!/usr/bin/env python3
"""
slider.py — o controle deslizante do Aero (padrão de qualidade para todos os sliders do sistema).

Por que existe: o Gtk.Scale + "rodar pactl a cada pixel" travava a interface (cada mudança esperava um
processo) e a barra pulava sob o dedo quando o sistema avisava a mudança e o painel se remontava.
Aqui:
  - arrastar é 1:1: o botão fica EXATAMENTE sob o ponteiro (posição absoluta na trilha, valor contínuo);
  - clicar num ponto da trilha desliza até lá (mola), rolar = ±2 por passo (suave), setas ±1 (Shift ±5);
  - o botão cresce um pouco ao segurar; a trilha preenche na cor de destaque do tema;
  - o valor sai por ao_mudar(v) a cada quadro (para a interface: esfera, número) e por aplicar(v) no
    máximo ~30 vezes por segundo NUMA THREAD que só manda o ÚLTIMO valor (sem fila, sem travar a tela);
  - set_valor() vindo de fora é ignorado enquanto você segura, e anima quando não segura.
    s = SliderFluido(0, 100, valor=60, ao_mudar=lambda v: ..., aplicar=lambda v: ...)
"""
import math
import os
import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

import tinta  # noqa: E402


class Aplicador:
    """Manda o valor pro sistema numa thread: no máximo a cada `intervalo` s, sempre o mais recente.
    UMA thread só pra todos os sliders do programa (antes era uma por slider, e a central recria os sliders
    a cada redesenho: as threads se acumulavam pra sempre)."""
    _cond = threading.Condition()
    _pendentes = {}              # id do aplicador -> (função, valor mais recente)
    _thread = None

    def __init__(self, funcao, intervalo=0.033):
        self.funcao, self.intervalo = funcao, intervalo
        with Aplicador._cond:
            if Aplicador._thread is None:
                Aplicador._thread = threading.Thread(target=Aplicador._laco, args=(intervalo,), daemon=True)
                Aplicador._thread.start()

    def pedir(self, v):
        with Aplicador._cond:
            Aplicador._pendentes[id(self)] = (self.funcao, v)
            Aplicador._cond.notify()

    @staticmethod
    def _laco(intervalo):
        import time
        while True:
            with Aplicador._cond:
                while not Aplicador._pendentes:
                    Aplicador._cond.wait()
                lote, Aplicador._pendentes = list(Aplicador._pendentes.values()), {}
            for funcao, v in lote:
                try:
                    funcao(v)
                except Exception:  # noqa: BLE001 — um erro do comando não pode matar a thread
                    pass
            time.sleep(intervalo)


class SliderFluido(Gtk.DrawingArea):
    ALTURA = 28
    TRILHA = 8

    def __init__(self, minimo=0.0, maximo=100.0, valor=0.0, passo_rolar=2.0, ao_mudar=None, aplicar=None):
        super().__init__(content_height=self.ALTURA, hexpand=True, focusable=True, can_focus=True)
        self.min, self.max = float(minimo), float(maximo)
        self.valor = self.mostrado = float(valor)
        self.passo_rolar = passo_rolar
        self.ao_mudar = ao_mudar
        self.aplicador = Aplicador(aplicar) if aplicar else None
        self.segurando = False
        self.crescer = 0.0              # 0..1: o botão cresce ao segurar/passar por cima
        self.alvo_crescer = 0.0
        self.vel = 0.0
        self.tick = None
        self.ativo = True
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        self.set_draw_func(self.desenhar)

        arrastar = Gtk.GestureDrag()
        arrastar.connect("drag-begin", self.comecou)
        arrastar.connect("drag-update", self.arrastou)
        arrastar.connect("drag-end", self.soltou)
        self.add_controller(arrastar)
        self.arraste = arrastar
        rolar = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        rolar.connect("scroll", self.rolou)
        self.add_controller(rolar)
        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self.tecla)
        self.add_controller(teclas)
        mov = Gtk.EventControllerMotion()
        mov.connect("enter", lambda *_: self.mirar_crescer(0.5))
        mov.connect("leave", lambda *_: self.mirar_crescer(1.0 if self.segurando else 0.0))
        self.add_controller(mov)

    # ---------- valor ----------
    def x_para_valor(self, x):
        r = self.ALTURA / 2
        w = self.get_width() - 2 * r
        f = min(1.0, max(0.0, (x - r) / max(1, w)))
        return self.min + f * (self.max - self.min)

    def definir(self, v, animar=False, avisar=True):
        v = min(self.max, max(self.min, v))
        self.valor = v
        if not animar:
            self.mostrado, self.vel = v, 0.0
        self.ligar()
        if avisar:
            if self.ao_mudar:
                self.ao_mudar(v)
            if self.aplicador:
                self.aplicador.pedir(v)
        self.queue_draw()

    def set_valor(self, v):
        """Valor vindo de fora (o sistema mudou): ignorado enquanto você segura; senão desliza até ele."""
        if not self.segurando and abs(v - self.valor) > 0.25:
            self.definir(v, animar=True, avisar=False)

    def set_ativo(self, sim):
        self.ativo = sim
        self.set_sensitive(sim)
        self.queue_draw()

    # ---------- entrada ----------
    def comecou(self, _g, x, _y):
        self.segurando = True
        self.grab_focus()
        self.mirar_crescer(1.0)
        self.definir(self.x_para_valor(x), animar=True)      # clique: desliza até o ponto

    def arrastou(self, g, dx, dy):
        ok, x0, _y0 = g.get_start_point()
        if ok:
            self.definir(self.x_para_valor(x0 + dx))          # arraste: 1:1 sob o ponteiro
            self.mostrado = self.valor

    def soltou(self, g, dx, _dy):
        ok, x0, _y0 = g.get_start_point()
        self.segurando = False
        self.mirar_crescer(0.5)
        if ok and self.aplicador:
            self.aplicador.pedir(self.valor)                  # o valor final sempre chega

    def rolou(self, _c, _dx, dy):
        self.definir(self.valor - dy * self.passo_rolar, animar=True)
        return True

    def tecla(self, _c, tecla, _cod, mods):
        passo = 5 if mods & Gdk.ModifierType.SHIFT_MASK else 1
        if tecla in (Gdk.KEY_Right, Gdk.KEY_Up):
            self.definir(self.valor + passo, animar=True)
            return True
        if tecla in (Gdk.KEY_Left, Gdk.KEY_Down):
            self.definir(self.valor - passo, animar=True)
            return True
        return False

    def mirar_crescer(self, alvo):
        self.alvo_crescer = alvo
        self.ligar()

    # ---------- animação ----------
    def ligar(self):
        if self.tick is None and self.get_mapped():
            self.ultimo = None
            self.tick = self.add_tick_callback(self.passo)

    def passo(self, _w, relogio):
        t = relogio.get_frame_time() / 1e6
        dt = min(0.05, t - self.ultimo) if self.ultimo else 0.016
        self.ultimo = t
        k, c = 260.0, 30.0                                     # mola rápida, quase sem passar do ponto
        self.vel += (k * (self.valor - self.mostrado) - c * self.vel) * dt
        self.mostrado += self.vel * dt
        self.crescer += (self.alvo_crescer - self.crescer) * min(1, dt * 14)
        self.queue_draw()
        parado = abs(self.valor - self.mostrado) < 0.02 and abs(self.vel) < 0.05 \
            and abs(self.alvo_crescer - self.crescer) < 0.01
        if parado:
            self.mostrado, self.vel, self.crescer = self.valor, 0.0, self.alvo_crescer
            self.tick = None
            return False
        return True

    # ---------- desenho ----------
    def _em_pontos(self):
        raiz = self.get_root()
        return raiz is not None and raiz.has_css_class("dots")

    def desenhar_pontos(self, cr, w, h):
        """DotStyle: a row of dots; the filled part is bright dots that grow slightly toward the knob,
        the knob is a glowing dot that grows while held."""
        r = self.ALTURA / 2
        cy = h / 2
        x0, x1 = r, w - r
        f = (self.mostrado - self.min) / max(1e-6, self.max - self.min)
        xb = x0 + f * (x1 - x0)
        passo = 8.0
        n = max(2, int((x1 - x0) / passo))
        dois_pi = 2 * math.pi
        cheios, vazios = [], []
        for i in range(n + 1):
            x = x0 + (x1 - x0) * i / n
            if x <= xb:
                perto = max(0.0, 1 - (xb - x) / 60)                 # dots near the knob are a bit bigger
                cheios.append((x, 1.9 + 1.0 * perto))
            else:
                vazios.append((x, 1.3))
        cr.new_path()
        for x, rr in vazios:
            cr.new_sub_path()
            cr.arc(x, cy, rr, 0, dois_pi)
        cr.set_source_rgba(0.925, 0.92, 0.91, 0.22)
        cr.fill()
        cr.new_path()
        for x, rr in cheios:
            cr.new_sub_path()
            cr.arc(x, cy, rr, 0, dois_pi)
        cr.set_source_rgba(0.925, 0.92, 0.91, 0.95 if self.ativo else 0.5)
        cr.fill()
        rb = 6.5 + 3.0 * self.crescer
        import cairo
        g = cairo.RadialGradient(xb, cy, 0, xb, cy, rb * 2.6)          # soft glow behind the knob
        for k in range(9):
            t = k / 8
            g.add_color_stop_rgba(t, 0.925, 0.92, 0.91, 0.35 * math.exp(-4 * t * t) * (1 - t ** 6))
        cr.set_source(g)
        cr.arc(xb, cy, rb * 2.6, 0, dois_pi)
        cr.fill()
        cr.arc(xb, cy, rb, 0, dois_pi)
        cr.set_source_rgb(0.95, 0.945, 0.935)
        cr.fill()

    def desenhar(self, _a, cr, w, h):
        if self._em_pontos():
            return self.desenhar_pontos(cr, w, h)
        cor_texto = self.get_color()
        acc = tinta.cor(self, "destaque")[:3] if self.ativo else (0.6, 0.6, 0.62)   # = destaque do menu
        r = self.ALTURA / 2
        cy = h / 2
        x0, x1 = r, w - r
        f = (self.mostrado - self.min) / max(1e-6, self.max - self.min)
        xb = x0 + f * (x1 - x0)
        t = self.TRILHA / 2

        def pilula(a, b, alt):
            cr.new_sub_path()
            cr.arc(a, cy, alt, math.pi / 2, 3 * math.pi / 2)
            cr.arc(b, cy, alt, -math.pi / 2, math.pi / 2)
            cr.close_path()
        pilula(x0, x1, t)                                       # trilha vazia
        cr.set_source_rgba(cor_texto.red, cor_texto.green, cor_texto.blue, 0.14)
        cr.fill()
        if xb > x0:                                             # parte cheia
            pilula(x0, xb, t)
            cr.set_source_rgb(*acc)
            cr.fill()
        rb = 7.5 + 3.0 * self.crescer                           # o botão (cresce ao segurar)
        cr.new_path()
        cr.arc(xb, cy + 1.2, rb + 0.8, 0, math.tau)             # sombra
        cr.set_source_rgba(0, 0, 0, 0.18)
        cr.fill()
        cr.arc(xb, cy, rb, 0, math.tau)
        cr.set_source_rgb(1, 1, 1)
        cr.fill_preserve()
        cr.set_line_width(1.2)
        cr.set_source_rgba(*acc, 0.55)
        cr.stroke()
        if self.has_focus() and not self.segurando:
            cr.arc(xb, cy, rb + 3.5, 0, math.tau)
            cr.set_line_width(2)
            cr.set_source_rgba(*acc, 0.35)
            cr.stroke()

    def do_dispose(self):
        tinta.soltar(self)
        Gtk.DrawingArea.do_dispose(self)
