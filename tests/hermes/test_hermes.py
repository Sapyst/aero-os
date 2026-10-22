"""Hermes core: settings, text cleaning, images, rules, the notification list, and the real D-Bus
interface on a PRIVATE test bus (Gio.TestDBus): nothing reaches the desktop's notifications."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "shell"))
from gi.repository import Gio, GLib  # noqa: E402

import hermes as h  # noqa: E402


class Relogio:
    """Fake timers: nothing waits for real time."""
    def __init__(self):
        self.timers, self.n = {}, 0

    def agendar(self, ms, f):
        self.n += 1
        self.timers[self.n] = (ms, f)
        return self.n

    def cancelar(self, t):
        self.timers.pop(t, None)

    def disparar(self, t):
        ms, f = self.timers.pop(t)
        f()


def novo(config=None):
    r = Relogio()
    s = h.Hermes(h.validar_config(config or {}), r.agendar, r.cancelar)
    eventos = []
    s.ouvir(lambda ev, n: eventos.append((ev, n.id)))
    return s, r, eventos


class TestConfig(unittest.TestCase):
    def test_padrao_e_lixo(self):
        self.assertEqual(h.validar_config({}), h.PADRAO)
        self.assertEqual(h.validar_config("x"), h.PADRAO)
        c = h.validar_config({"position": "middle-earth", "width": "big", "margin": -5, "max_visible": 99,
                              "wave": {"color": "red", "intensity": 7, "origin": "moon"},
                              "colors": {"text": "#12345"}, "font": {"title": "Evil;font"}, "rules": "no"})
        self.assertEqual(c["position"], "follow-central")
        self.assertEqual(c["width"], 340)
        self.assertEqual(c["margin"], 0)
        self.assertEqual(c["max_visible"], 12)
        self.assertEqual(c["wave"]["color"], "auto")
        self.assertEqual(c["wave"]["intensity"], 1.0)
        self.assertEqual(c["wave"]["origin"], "notification")
        self.assertEqual(c["colors"]["text"], "#ecebe8")
        self.assertEqual(c["font"]["title"], "Doto")
        self.assertEqual(c["rules"], [])

    def test_personalizacao_valida(self):
        c = h.validar_config({"style": "aeroterm", "position": "top-center", "wave": {"color": "#00ff88",
                              "origin": "screen-center", "enabled": False, "fullscreen": "off"}, "fullscreen": "show"})
        self.assertEqual((c["style"], c["position"], c["fullscreen"]), ("aeroterm", "top-center", "show"))
        self.assertEqual((c["wave"]["color"], c["wave"]["origin"], c["wave"]["enabled"], c["wave"]["fullscreen"]),
                         ("#00ff88", "screen-center", False, "off"))
        self.assertEqual(h.validar_config({"fullscreen": "wave-only"})["fullscreen"], "hold")

    def test_regras(self):
        self.assertIsNone(h.validar_regra({"match": {"app": "("}, "set": {"hide": True}}))
        self.assertIsNone(h.validar_regra({"match": {"evil": "x"}, "set": {}}))
        self.assertIsNone(h.validar_regra({"match": {"app": "x"}, "set": {"timeout": -1}}))
        self.assertIsNotNone(h.validar_regra({"match": {"app": "steam"}, "set": {"wave": False, "timeout": 2000}}))


class TestTexto(unittest.TestCase):
    def test_markup(self):
        self.assertEqual(h.limpar_markup("<b>oi</b> & <i>tchau"), "<b>oi</b> &amp; <i>tchau</i>")
        self.assertEqual(h.limpar_markup('<span foreground="red">x</span><a href="http://e">link</a>'), "xlink")
        self.assertEqual(h.limpar_markup("<b><i>x</b>"), "<b><i>x</i></b>")
        self.assertEqual(h.limpar_markup("a<br>b &lt; &copy;"), "a\nb &lt; &amp;copy;")
        self.assertEqual(h.limpar_markup("<img src=x onerror=1>"), "")
        GLib.markup_escape_text("")                          # sanity
        from gi.repository import Pango
        for t in ("<b>x", "</i>y", "<<b>>", "&#99999999;", "<u><b>z</u>", "&#x41;"):
            Pango.parse_markup(h.limpar_markup(t), -1, "\0")   # never invalid markup

    def test_texto_puro(self):
        self.assertEqual(h.texto_puro("a\x00b\x1bc\nd"), "abc\nd")
        self.assertEqual(h.texto_puro(None), "")


class TestImagem(unittest.TestCase):
    def test_valida_e_recusa(self):
        ok = h.ler_imagem((2, 2, 8, True, 8, 4, b"\x00" * 16))
        self.assertEqual((ok["width"], ok["height"]), (2, 2))
        self.assertIsNotNone(h.ler_imagem((2, 2, 12, True, 8, 4, b"\x00" * 20)))  # padded rows, last one without
        for ruim in ((2, 2, 8, True, 8, 4, b"\x00" * 10), (0, 2, 8, True, 8, 4, b""), (9999, 1, 39996, True, 8, 4, b""),
                     (2, 2, 5, False, 8, 3, b"\x00" * 12), (2, 2, 8, False, 8, 4, b"\x00" * 16), "x"):
            self.assertIsNone(h.ler_imagem(ruim), ruim)


class TestLista(unittest.TestCase):
    def test_mostrar_expirar_fechar(self):
        s, r, ev = novo()
        i = s.notificar("App", 0, "", "Oi", "corpo", [], {}, -1, ":1.5")
        self.assertEqual(ev, [("show", i)])
        self.assertEqual(r.timers[1][0], 5000)               # normal default
        r.disparar(1)
        self.assertEqual(ev[-1], ("close", i))
        self.assertNotIn(i, s.ativas)
        self.assertEqual(s.historico[-1]["summary"], "Oi")
        j = s.notificar("App", 0, "", "x", "<b>Aero's</b> &amp; co", [], {}, -1, ":1.5")
        self.assertEqual(s.historico_mako()[0]["body"], "Aero's & co")

    def test_critica_fica(self):
        s, r, _ = novo()
        s.notificar("App", 0, "", "Bateria", "", [], {"urgency": 2}, -1, ":1.5")
        self.assertEqual(r.timers, {})                       # critical: timeout 0 = stays

    def test_urgencia_tipos_tolerantes(self):
        s, _, _ = novo()
        for v in (0, 2, 7):
            i = s.notificar("A", 0, "", "x", "", [], {"urgency": v}, -1, ":1")
            self.assertEqual(s.ativas[i].urgencia, v if v in (0, 2) else 1)

    def test_substituir_por_id_e_etiqueta(self):
        s, _, ev = novo()
        i = s.notificar("Vol", 0, "", "50%", "", [], {"x-canonical-private-synchronous": "volume"}, -1, ":1.2")
        j = s.notificar("Vol", 0, "", "60%", "", [], {"x-dunst-stack-tag": "volume"}, -1, ":1.2")
        self.assertEqual(i, j)
        self.assertEqual(s.ativas[i].titulo, "60%")
        k = s.notificar("Vol", i, "", "70%", "", [], {}, -1, ":1.2")
        self.assertEqual(k, i)
        self.assertEqual([e for e, _ in ev], ["show", "update", "update"])
        # unknown replaces_id -> new id (never chosen by the app)
        n = s.notificar("Vol", 999, "", "x", "", [], {}, -1, ":1.2")
        self.assertNotEqual(n, 999)

    def test_outro_app_nao_substitui_nem_fecha(self):
        s, _, _ = novo()
        i = s.notificar("Banco", 0, "", "Código 1234", "", [], {}, -1, ":1.10")
        j = s.notificar("Malvado", i, "", "Código falso", "", [], {}, -1, ":1.66")
        self.assertNotEqual(i, j)
        self.assertEqual(s.ativas[i].titulo, "Código 1234")
        self.assertFalse(s.fechar(i, h.PEDIDO, remetente=":1.66"))
        self.assertTrue(s.fechar(i, h.PEDIDO, remetente=":1.10"))

    def test_acoes_e_residente(self):
        s, _, _ = novo()
        i = s.notificar("Chat", 0, "", "Oi", "", ["default", "Open", "reply", "Reply"], {"resident": True}, 0, ":1")
        self.assertFalse(s.acionar(i, "nao-existe"))
        self.assertTrue(s.acionar(i, "reply"))
        self.assertIn(i, s.ativas)                           # resident stays
        j = s.notificar("Chat", 0, "", "Oi", "", ["default", "Open"], {}, 0, ":1")
        self.assertTrue(s.acionar(j))
        self.assertNotIn(j, s.ativas)

    def test_transitoria_fora_do_historico(self):
        s, _, _ = novo()
        s.notificar("A", 0, "", "efêmera", "", [], {"transient": True}, -1, ":1")
        self.assertEqual(len(s.historico), 0)

    def test_dnd_e_regras(self):
        s, _, ev = novo({"rules": [{"match": {"app": "^steam$"}, "set": {"wave": False, "timeout": 2000}},
                                   {"match": {"summary": "promo"}, "set": {"hide": True}}]})
        i = s.notificar("Steam", 0, "", "Amigo online", "", [], {}, -1, ":1")
        self.assertFalse(s.ativas[i].onda)
        self.assertEqual(s.ativas[i].timeout, 2000)
        s.notificar("Loja", 0, "", "PROMO hoje", "", [], {}, -1, ":1")
        self.assertEqual(len(ev), 1)                         # hidden: never shown
        self.assertEqual(s.historico[-1]["summary"], "PROMO hoje")
        s.modo("dnd", True)
        s.notificar("A", 0, "", "quieta", "", [], {}, -1, ":1")
        c = s.notificar("A", 0, "", "crítica", "", [], {"urgency": 2}, -1, ":1")
        self.assertEqual(ev[-1], ("show", c))                # critical passes DND
        self.assertEqual(len(ev), 2)

    def test_spam(self):
        s, _, ev = novo()
        ids = [s.notificar("Spam", 0, "", str(k), "", [], {}, -1, ":1.9") for k in range(100)]
        self.assertEqual(sum(1 for x in ids if x), h.Hermes.LIMITE_POR_SEG)
        self.assertNotEqual(s.notificar("Outro", 0, "", "ok", "", [], {}, -1, ":1.3"), 0)


class TestDBus(unittest.TestCase):
    """Real D-Bus calls on a private bus."""

    @classmethod
    def setUpClass(cls):
        cls.bus = Gio.TestDBus.new(Gio.TestDBusFlags.NONE)
        cls.bus.up()
        endereco = cls.bus.get_bus_address()
        flags = Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION
        cls.srv = Gio.DBusConnection.new_for_address_sync(endereco, flags, None, None)
        cls.cli = Gio.DBusConnection.new_for_address_sync(endereco, flags, None, None)

    @classmethod
    def tearDownClass(cls):
        cls.srv.close_sync(None)
        cls.cli.close_sync(None)
        cls.bus.down()

    def girar(self, ms=200):
        fim = GLib.get_monotonic_time() + ms * 1000
        ctx = GLib.MainContext.default()
        while GLib.get_monotonic_time() < fim:
            ctx.iteration(False)

    def chamar(self, metodo, params=None, tipo=None):
        """Async call + spinning the loop: the service lives in this same thread."""
        caixa = []
        self.cli.call(h.NOME, h.CAMINHO, h.NOME, metodo, params, GLib.VariantType(tipo) if tipo else None,
                      Gio.DBusCallFlags.NONE, 3000, None, lambda c, r: caixa.append(c.call_finish(r)))
        fim = GLib.get_monotonic_time() + 3_000_000
        while not caixa and GLib.get_monotonic_time() < fim:
            GLib.MainContext.default().iteration(True)
        self.assertTrue(caixa, f"no answer to {metodo}")
        return caixa[0]

    def test_ciclo_completo(self):
        s = h.Hermes(h.validar_config({}))
        s.publicar(self.srv)
        self.girar()
        nome, vendor, _, spec = self.chamar("GetServerInformation", None, "(ssss)").unpack()
        self.assertEqual((nome, vendor, spec), ("Hermes", "Aero OS", "1.2"))
        caps = self.chamar("GetCapabilities", None, "(as)").unpack()[0]
        self.assertIn("actions", caps)
        fechadas = []
        self.cli.signal_subscribe(None, h.NOME, "NotificationClosed", h.CAMINHO, None,
                                  Gio.DBusSignalFlags.NONE, lambda *a: fechadas.append(a[5].unpack()))
        dicas = {"urgency": GLib.Variant("y", 2), "category": GLib.Variant("s", "im.received"),
                 "image-data": GLib.Variant("(iiibiiay)", (1, 1, 4, True, 8, 4, b"\x01\x02\x03\x04"))}
        params = GLib.Variant("(susssasa{sv}i)", ("Chat", 0, "", "Oi <b>", "<b>negrito</b>", ["default", "Open"],
                                                  dicas, -1))
        (i,) = self.chamar("Notify", params, "(u)").unpack()
        self.assertGreater(i, 0)
        n = s.ativas[i]
        self.assertEqual((n.urgencia, n.categoria, n.corpo), (2, "im.received", "<b>negrito</b>"))
        self.assertIsNotNone(n.imagem)
        self.chamar("CloseNotification", GLib.Variant("(u)", (i,)))
        self.girar()
        self.assertIn((i, h.PEDIDO), fechadas)
        # garbage hints never kill the service
        params = GLib.Variant("(susssasa{sv}i)", ("X", 0, "", "y", "", ["only-one"],
                                                  {"image-data": GLib.Variant("s", "lixo"),
                                                   "urgency": GLib.Variant("s", "high")}, -1))
        (j,) = self.chamar("Notify", params, "(u)").unpack()
        self.assertEqual(s.ativas[j].acoes, [])
        self.assertEqual(s.ativas[j].urgencia, 1)
        Gio.bus_unown_name(s.dono)
        self.srv.unregister_object(s.registro)


if __name__ == "__main__":
    unittest.main()
