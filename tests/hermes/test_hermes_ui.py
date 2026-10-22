"""Hermes on screen, without showing anything: the wave is drawn on an image in memory, cards are built
but never presented, and the layer windows are replaced by fakes."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shell"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    raise unittest.SkipTest("no display")
try:
    import hermes_ui as ui  # noqa: E402
except (ValueError, ImportError) as erro:
    raise unittest.SkipTest(f"gtk4-layer-shell missing: {erro}")
import cairo  # noqa: E402

import hermes as h  # noqa: E402


class TestOnda(unittest.TestCase):
    def test_anel_so_perto_do_raio(self):
        cfg = h.validar_config({})["wave"]
        sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 800, 500)
        cr = cairo.Context(sup)
        n = ui.desenhar_onda(cr, 800, 500, 0.4, (780, 20), cfg, (1, 1, 1))
        self.assertGreater(n, 5)
        alcance = math.hypot(800, 500) * cfg["reach"]
        # a dot far from the ring stays empty
        self.assertEqual(sup.get_data()[(20 * 800 + 780) * 4 + 3], 0)
        self.assertEqual(ui.desenhar_onda(cr, 800, 500, 1.0, (0, 0), cfg, (1, 1, 1)), 0)
        self.assertEqual(ui.desenhar_onda(cr, 800, 500, -0.1, (0, 0), cfg, (1, 1, 1)), 0)
        self.assertLess(alcance, 1000)

    def test_esmaecida_e_mais_fraca(self):
        cfg = h.validar_config({})["wave"]

        def soma(forca):
            sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
            ui.desenhar_onda(cairo.Context(sup), 400, 300, 0.3, (390, 10), cfg, (1, 1, 1), forca)
            return sum(sup.get_data()[3::4])
        cheia = soma(1.0)
        fraca = soma(cfg["fullscreen_intensity"] / cfg["intensity"])
        self.assertGreater(cheia, fraca * 1.8)
        self.assertGreater(fraca, 0)

    def test_some_suave(self):
        """Brightness goes down smoothly to zero: no jump in the last frames."""
        cfg = h.validar_config({})["wave"]

        def soma(p):
            sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 400)
            ui.desenhar_onda(cairo.Context(sup), 600, 400, p, (300, 200), cfg, (1, 1, 1))
            return sum(sup.get_data()[3::4])
        valores = [soma(k / 50) for k in range(20, 50)]
        self.assertLess(valores[-1], valores[0] * 0.02)        # the last frame is almost nothing
        for a, b in zip(valores, valores[1:]):
            self.assertLess(b - a, max(valores) * 0.12)          # never a jump up
        self.assertEqual(ui.suave(0, 1, 1.0), 1.0)
        self.assertEqual(ui.suave(0, 1, 0.0), 0.0)

    def test_macia_sob_a_barra(self):
        cfg = h.validar_config({})["wave"]

        def borda_da_barra(modo):
            c = dict(cfg, under_bar=modo)
            sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 600, 300)
            ui.desenhar_onda(cairo.Context(sup), 600, 300, 0.25, (560, 20), c, (1, 1, 1), 1.0, (12, 8, 588, 42))
            dados = sup.get_data()
            faixa = sum(dados[(y * 600 + x) * 4 + 3] for y in range(8, 42) for x in range(12, 588))
            fora = sum(dados[(y * 600 + x) * 4 + 3] for y in range(60, 300) for x in range(0, 600))
            return faixa, fora
        dura, _ = borda_da_barra("normal")
        macia, fora_macia = borda_da_barra("soft")
        escondida, fora_esc = borda_da_barra("hide")
        self.assertEqual(escondida, 0)
        self.assertGreater(macia, 0)
        self.assertEqual(fora_macia, fora_esc)            # outside the bar nothing changes

    def test_origem(self):
        c = h.validar_config({"position": "bottom-left", "margin": 10})
        self.assertEqual(ui.origem_da_onda(c, 1000, 600), (34, 566))
        c = h.validar_config({"wave": {"origin": "screen-center"}})
        self.assertEqual(ui.origem_da_onda(c, 1000, 600), (500, 300))


class Falsa:
    def __init__(self, *a, **k):
        self.chamadas = []

    def __getattr__(self, nome):
        return lambda *a, **k: self.chamadas.append((nome, a, k))


class TestCartaoEFluxo(unittest.TestCase):
    def setUp(self):
        r = {}
        self.s = h.Hermes(h.validar_config({}), lambda ms, f: r.setdefault(len(r), f) and len(r) or len(r),
                          lambda t: None)

    def test_css_e_cartao(self):
        css = ui.css_do_tema(self.s.config)
        self.assertNotIn("@hermes", css)
        p = Gtk.CssProvider()
        p.load_from_string(css)
        i = self.s.notificar("Chat <x>", 0, "dialog-information", "Oi <b>", "<b>ok</b> & <i>x",
                             ["default", "Open", "reply", "Reply"], {"value": 40, "urgency": 2}, -1, ":1")
        n = self.s.ativas[i]
        cartao = ui.montar_cartao(n, self.s.config, lambda i: None, lambda i, k: None, lambda i: None)
        self.assertTrue(cartao.critica)

    def test_tela_cheia_segura_cartao_e_onda_fraca(self):
        cheia = [True]
        orig_onda, orig_cart = ui.Onda, ui.Cartoes
        ui.Onda, ui.Cartoes = Falsa, Falsa
        try:
            hu = ui.HermesUI(self.s, eh_tela_cheia=lambda: cheia[0], ouvir=False)
            i = self.s.notificar("Steam", 0, "", "Amigo online", "", [], {}, -1, ":1")
            self.assertEqual(hu.onda.chamadas[-1][2], {"urgente": False, "sobre_tela_cheia": True})
            self.assertEqual([c[0] for c in hu.cartoes.chamadas], [])       # held
            self.assertEqual([n.id for n in hu.guardadas], [i])
            c = self.s.notificar("Bateria", 0, "", "5%", "", [], {"urgency": 2}, -1, ":2")
            self.assertEqual(hu.cartoes.chamadas[-1][0], "mostrar")         # critical passes
            cheia[0] = False
            hu.saiu_da_tela_cheia()
            self.assertEqual(hu.cartoes.chamadas[-1][1][0].id, i)
            self.assertEqual(len(self.s.timers), 1)                          # timer starts only now
            self.s.fechar(c)
            self.assertEqual(hu.cartoes.chamadas[-1][0], "tirar")
        finally:
            ui.Onda, ui.Cartoes = orig_onda, orig_cart


if __name__ == "__main__":
    unittest.main()


class TestDots(unittest.TestCase):
    def test_fonte_e_texto(self):
        import hermes_dots as hd
        self.assertEqual(hd.normalizar("Notificação ç!"), "NOTIFICACAO C!")
        self.assertEqual(hd.normalizar("emoji 😀"), "EMOJI  ")
        self.assertAlmostEqual(hd.largura_texto("AB", 2), 22)
        for ch, g in hd.FONTE.items():
            self.assertEqual(len(g), 7, ch)
            self.assertTrue(all(len(linha) == 5 for linha in g), ch)
        sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, 100, 20)
        hd.desenhar_texto(cairo.Context(sup), "HERMES 02:51", 1, 1, 1.5, (1, 1, 1))
        self.assertGreater(sum(sup.get_data()[3::4]), 0)

    def test_perimetro(self):
        import hermes_dots as hd
        pts = hd.perimetro(0, 0, 340, 100, 22, 6)
        self.assertGreater(len(pts), 60)
        for x, y in pts:
            self.assertTrue(-0.01 <= x <= 340.01 and -0.01 <= y <= 100.01)
        self.assertEqual(len(hd.perimetro(0, 0, 10, 10, 50, 6)), 8)     # tiny box: never crashes

    def test_cartao_entra_e_sai(self):
        import time as t
        import hermes_dots as hd
        s = h.Hermes(h.validar_config({}), lambda ms, f: 1, lambda x: None)
        i = s.notificar("Discord", 0, "", "Nova mensagem", "oi", [], {}, -1, ":1")
        c = ui.montar_cartao(s.ativas[i], s.config, lambda i: None, lambda i, k: None, lambda i: None)
        self.assertIsInstance(c, hd.CartaoDots)
        forma, conteudo, contorno = c.fases()
        self.assertEqual((forma, conteudo), (0.0, 0.0))            # just born: only bubbles
        self.assertEqual(len(c.bolhas), c.N_BOLHAS)
        c.t_entrada -= 1
        self.assertEqual(c.fases(), (1.0, 1.0, 1.0))
        self.assertEqual(hd.bolha(0), 0.0)
        self.assertAlmostEqual(hd.bolha(1), 1.0)
        fim = []
        c.sair(lambda: fim.append(1))
        c.t_saida -= 1
        self.assertEqual(c.fases(), (0.0, 0.0, 0.0))
        c._quadro()
        ctx = __import__("gi.repository.GLib", fromlist=["GLib"]).MainContext.default()
        for _ in range(20):
            ctx.iteration(False)
        self.assertEqual(fim, [1])
        c.sobre(True)
        self.assertEqual(c.alvo, 1.0)                               # leaving: hover is ignored
        c2 = ui.montar_cartao(s.ativas[i], s.config, lambda i: None, lambda i, k: None, lambda i: None)
        c2.sobre(True)
        self.assertEqual(c2.alvo, 1.03)

    def test_sino(self):
        import json
        import tempfile
        d = tempfile.mkdtemp()
        velho = ui.SINO
        ui.SINO = os.path.join(d, "sino.json")
        try:
            c = h.validar_config({})
            self.assertEqual(c["position"], "follow-bell")
            self.assertEqual(ui.origem_da_onda(c, 1000, 600), (850, 18))       # estimate before a click
            with open(ui.SINO, "w") as f:
                json.dump({"x": 2300, "y": 20, "w": 2560, "h": 1440}, f)
            self.assertEqual(ui.origem_da_onda(c, 2560, 1440), (2300, 20))
            self.assertEqual(ui.posicao(c), "top-right")
        finally:
            ui.SINO = velho
