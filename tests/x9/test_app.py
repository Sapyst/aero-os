"""X9 app built without showing a window: lock screen, unlocking, filters, selection.
pkexec is never called (rodar_admin is replaced)."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(AQUI, "..", "..", "system", "x9"))
sys.path.insert(0, os.path.join(AQUI, "..", "..", "x9"))
import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk  # noqa: E402

if not Gtk.init_check():
    raise unittest.SkipTest("no display")
import x9  # noqa: E402
import x9core as x  # noqa: E402


class TestApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Gio
        cls.app = x9.App()
        cls.app.set_flags(Gio.ApplicationFlags.NON_UNIQUE)   # no D-Bus name: never touches a running X9
        cls.app.register(None)

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.travas = tempfile.mkdtemp()
        agora = time.time()
        evs = [{"id": f"{i:016x}", "ts": agora - i * 3600, "tool": "sudo", "user": "l", "as": "root",
                "result": "allowed" if i % 2 else "denied", "reason": "", "command": f"/usr/bin/ls /tmp/{i} <b>",
                "cwd": "/", "tty": "none", "terminal": False, "app": "Claude" if i < 5 else "Terminal (kitty)",
                "category": "Inspect (read-only)" if i < 3 else "Packages", "purpose": f"Look at <i>{i}</i>",
                "risk": "low"} for i in range(10)]
        x.acrescentar(evs, pasta=self.d)
        self.chamadas = []

        def admin_falso(programa, acao, entrada):
            self.chamadas.append((programa, acao, entrada))
            if programa == x9.SETLOCK:
                dados = json.loads(entrada)
                x.gravar_trava(os.getuid(), os.getgid(), x.hash_senha(dados["new"]), pasta=self.travas)
            return subprocess.CompletedProcess([], 0, "", "")
        setlock = os.path.join(self.d, "x9-setlock")
        open(setlock, "w").close()
        self.trocas = [mock.patch.object(x, "ARQUIVO", self.d), mock.patch.object(x, "TRAVAS", self.travas),
                       mock.patch.object(x9, "SETLOCK", setlock), mock.patch.object(x9, "rodar_admin", admin_falso),
                       mock.patch.object(x9, "em_thread", lambda f, depois: depois(f()))]
        for t in self.trocas:
            t.start()
        self.janela = x9.Janela(self.app)

    def tearDown(self):
        for t in self.trocas:
            t.stop()
        self.janela.destroy()

    def test_primeira_vez_cria_senha_e_destrava(self):
        tr = self.janela.trava
        self.assertEqual(self.janela.pilha.get_visible_child_name(), "trava")
        self.assertTrue(tr.confirma.get_visible())
        tr.senha.set_text("abc")
        tr.entrar()
        self.assertTrue(tr.erro.get_visible())                 # too short
        tr.senha.set_text("abcd")
        tr.confirma.set_text("abce")
        tr.entrar()
        self.assertFalse(self.janela.destravada)
        tr.senha.set_text("abcd")
        tr.confirma.set_text("abcd")
        tr.entrar()
        self.assertTrue(self.janela.destravada)
        self.assertEqual(self.chamadas[0][1], None)
        self.assertEqual(json.loads(self.chamadas[0][2]), {"old": None, "new": "abcd"})
        self.janela.travar()
        self.assertFalse(self.janela.destravada)
        self.assertEqual(self.janela.registro.eventos, [])
        self.assertFalse(tr.confirma.get_visible())
        tr.senha.set_text("errada")
        tr.entrar()
        self.assertFalse(self.janela.destravada)
        tr.senha.set_text("abcd")
        tr.entrar()
        self.assertTrue(self.janela.destravada)

    def test_filtros_e_selecao(self):
        self.janela.destravar()
        r = self.janela.registro
        self.assertEqual(len(r.eventos), 10)
        self.assertEqual(r.eventos[0]["id"], f"{0:016x}")     # newest first
        r.categoria.set_selected([v for _, v in r.categoria.opcoes].index("Packages"))
        self.assertEqual(len(r.visiveis), 7)
        r.resultado.set_selected([v for _, v in r.resultado.opcoes].index("denied"))
        self.assertEqual({e["result"] for e in r.visiveis}, {"denied"})
        r.app.set_selected([v for _, v in r.app.opcoes].index("Terminal (kitty)"))
        self.assertTrue(all(e["app"] == "Terminal (kitty)" for e in r.visiveis))
        r.hora_de.set_text("99:99")
        self.assertTrue(r.hora_de.has_css_class("error"))      # bad time is ignored, not a crash
        r.limpar_filtros()
        self.assertEqual(len(r.visiveis), 10)
        r.busca.set_text("tmp/3")
        r.filtrar()
        self.assertEqual([e["id"] for e in r.visiveis], [f"{3:016x}"])
        r.limpar_filtros()
        self.janela.selecionar.set_active(True)
        r.marcar_todos(True)
        self.assertEqual(len(r.marcados), 10)
        self.janela.apagar(sorted(r.marcados))                 # opens a confirmation, deletes nothing yet
        self.assertEqual(self.chamadas, [])
        self.janela.depois_apagar(subprocess.CompletedProcess([], 126, "", ""))
        self.assertEqual(len(r.marcados), 10)                  # cancelled: selection stays

    def test_texto_com_markup_nao_quebra(self):
        self.janela.destravar()
        r = self.janela.registro
        linha = r.linha(r.eventos[0])
        self.assertFalse(linha.get_use_markup())
        x9.DialogoDetalhe(self.janela, r.eventos[0])

    def test_falha(self):
        cp = subprocess.CompletedProcess
        self.assertEqual(x9.falha(cp([], 0, "", "")), "")
        self.assertEqual(x9.falha(cp([], 126, "", "")), "Password cancelled")
        self.assertEqual(x9.falha(cp([], 127, "", "")), "Not authorized")
        self.assertEqual(x9.falha(cp([], 1, "", "x9-admin: bad ids\n")), "bad ids")


if __name__ == "__main__":
    unittest.main()
