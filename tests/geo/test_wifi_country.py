"""aero-wifi-country: reading the routers' country, the time zone fallback and the config. No root, no iw."""
import importlib.machinery
import importlib.util
import os
import tempfile
import unittest

CAMINHO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "system", "geo", "aero-wifi-country")
loader = importlib.machinery.SourceFileLoader("aero_wifi_country", CAMINHO)
spec = importlib.util.spec_from_loader(loader.name, loader)
w = importlib.util.module_from_spec(spec)
loader.exec_module(w)

DUMP = """BSS 11:22:33:44:55:66(on wlan0) -- associated
\tfreq: 5785
\tSSID: Casa
\tCountry: US\tEnvironment: Indoor/Outdoor
BSS 22:22:33:44:55:66(on wlan0)
\tSSID: Vizinho
\tCountry: US\tEnvironment: Indoor/Outdoor
BSS 33:22:33:44:55:66(on wlan0)
\tSSID: Outro
\tCountry: CA\tEnvironment: Indoor/Outdoor
BSS 44:22:33:44:55:66(on wlan0)
\tSSID: SemPais
BSS 55:22:33:44:55:66(on wlan0)
\tCountry: XX\tEnvironment: Indoor/Outdoor
"""
FUSOS = {"America/New_York": "US", "America/Sao_Paulo": "BR"}


class TestVotos(unittest.TestCase):
    def test_conta_e_pesa_a_conectada(self):
        self.assertEqual(w.votos_da_varredura(DUMP), {"US": 4, "CA": 1})

    def test_vazio_e_lixo(self):
        self.assertEqual(w.votos_da_varredura(""), {})
        self.assertEqual(w.votos_da_varredura("garbage\nCountry: US\n"), {})


class TestDecidir(unittest.TestCase):
    auto = dict(w.PADRAO)

    def test_roteadores_ganham(self):
        self.assertEqual(w.decidir(self.auto, {"US": 4, "CA": 1}, "America/Sao_Paulo", FUSOS)[:2], ("US", "routers"))

    def test_um_voto_so_nao_basta(self):
        self.assertEqual(w.decidir(self.auto, {"CA": 1}, "America/New_York", FUSOS)[:2], ("US", "timezone"))

    def test_empate_cai_no_fuso(self):
        self.assertEqual(w.decidir(self.auto, {"US": 1, "CA": 1}, "America/Sao_Paulo", FUSOS)[:2], ("BR", "timezone"))

    def test_ip_por_ultimo(self):
        self.assertEqual(w.decidir(self.auto, {}, "Etc/UTC", FUSOS, "DE")[:2], ("DE", "ip"))
        self.assertEqual(w.decidir(self.auto, {}, "Etc/UTC", FUSOS)[0], "")

    def test_manual(self):
        conf = dict(self.auto, mode="manual", country="BR")
        self.assertEqual(w.decidir(conf, {"US": 9}, "America/New_York", FUSOS)[:2], ("BR", "manual"))


class TestArquivos(unittest.TestCase):
    def test_conf_ruim_vira_padrao(self):
        d = tempfile.mkdtemp()
        c = os.path.join(d, "c.conf")
        with open(c, "w") as f:
            f.write("mode=evil\ncountry=../x\nip_lookup=2\nother=1\n")
        self.assertEqual(w.ler_conf(c), w.PADRAO)
        with open(c, "w") as f:
            f.write("mode=manual\ncountry=BR\nip_lookup=1\n")
        self.assertEqual(w.ler_conf(c), {"mode": "manual", "country": "BR", "ip_lookup": "1"})
        self.assertEqual(w.ler_conf(os.path.join(d, "nada")), w.PADRAO)

    def test_tabelas_do_sistema(self):
        de_fuso, de_pais = w.tabela_fusos()
        if not de_fuso:
            self.skipTest("no zone.tab")
        self.assertEqual(de_fuso.get("America/Sao_Paulo"), "BR")
        self.assertIn("America/New_York", de_pais["US"])
        self.assertEqual(w.nomes_paises().get("BR"), "Brazil")


if __name__ == "__main__":
    unittest.main()
