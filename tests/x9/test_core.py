"""Tests for X9's core (system/x9/x9core.py) and its root helper x9-admin. No root, no journal:
journal entries are built by hand in the same shape journalctl -o json prints."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

AQUI = os.path.dirname(os.path.abspath(__file__))
X9 = os.path.join(AQUI, "..", "..", "system", "x9")
sys.path.insert(0, X9)
import x9core as x  # noqa: E402

N = [0]


def j(msg, fonte="sudo", pid="100", unidade="app-com.anthropic.Claude-2166.scope", ts=None, cmdline="sudo ls"):
    N[0] += 1
    ts = ts if ts is not None else 1792548082.0 + N[0]
    exe = {"sudo": "/usr/bin/sudo", "pkexec": "/usr/bin/pkexec", "su": "/usr/bin/su"}.get(fonte, "/usr/bin/x")
    return {"SYSLOG_IDENTIFIER": fonte, "MESSAGE": msg, "_EXE": exe, "_PID": pid, "__REALTIME_TIMESTAMP": str(int(ts * 1e6)),
            "__CURSOR": f"c{N[0]}", "_SYSTEMD_USER_UNIT": unidade, "_CMDLINE": cmdline}


def eventos(linhas):
    ju = x.Juntador()
    out = []
    for e in linhas:
        p = x.ler_linha(e)
        if p:
            out += ju.receber(p)
    return out + ju.todos()


class TestLeitura(unittest.TestCase):
    def test_sudo_ok_com_sessao(self):
        evs = eventos([j(" alice : TTY=pts/1 ; PWD=/home/alice ; USER=root ; COMMAND=/usr/bin/pacman -S htop"),
                       j("pam_unix(sudo:session): session opened for user root(uid=0) by alice(uid=1000)"),
                       j("pam_unix(sudo:session): session closed for user root")])
        self.assertEqual(len(evs), 1)
        e = evs[0]
        self.assertEqual((e["result"], e["category"], e["app"], e["user"], e["tty"], e["terminal"]),
                         ("allowed", "Packages", "Claude", "alice", "pts/1", True))
        self.assertEqual(e["purpose"], "Install packages: htop")
        self.assertRegex(e["id"], r"^[0-9a-f]{16}$")

    def test_comando_continuado(self):
        evs = eventos([j(" alice : PWD=/ ; USER=root ; COMMAND=/usr/bin/cp a"),
                       j(" alice : (command continued) b c /etc/x"),
                       j("pam_unix(sudo:session): session opened for user root")])
        self.assertEqual(evs[0]["command"], "/usr/bin/cp a b c /etc/x")
        self.assertEqual(evs[0]["tty"], "none")
        self.assertFalse(evs[0]["terminal"])

    def test_senha_cancelada_usa_cmdline(self):
        evs = eventos([j("pam_unix(sudo:auth): conversation failed", cmdline="sudo -A -u root pacman -Syu"),
                       j("pam_unix(sudo:auth): auth could not identify password for [alice]")])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["result"], "cancelled")
        self.assertEqual(evs[0]["command"], "pacman -Syu")
        self.assertEqual(evs[0]["purpose"], "Upgrade the whole system")

    def test_senha_errada(self):
        evs = eventos([j("pam_unix(sudo:auth): authentication failure; logname=alice uid=1000"),
                       j(" alice : 3 incorrect password attempts ; TTY=pts/2 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/id")])
        self.assertEqual(len(evs), 1)
        self.assertEqual((evs[0]["result"], evs[0]["category"]), ("wrong-password", "Authentication"))

    def test_app_tentou_sem_pedir_senha(self):
        evs = eventos([j(" alice : a password is required ; PWD=/ ; USER=root ; COMMAND=/usr/bin/cat /etc/shadow")])
        self.assertEqual(evs[0]["result"], "denied")
        self.assertEqual(evs[0]["risk"], "high")
        self.assertIn("without asking", evs[0]["reason"])

    def test_pkexec(self):
        ok = eventos([j("alice: Executing command [USER=root] [TTY=unknown] [CWD=/home/alice] "
                        "[COMMAND=/usr/local/bin/aero-memory-config set enabled 1]", fonte="pkexec",
                        unidade="session-3.scope")])[0]
        self.assertEqual((ok["result"], ok["category"], ok["app"]), ("allowed", "Aero system", "Desktop session"))
        nao = eventos([j("alice: Error executing command as another user: Not authorized [USER=root] [TTY=unknown] "
                         "[CWD=/] [COMMAND=/usr/bin/bash]", fonte="pkexec")])[0]
        self.assertEqual(nao["result"], "denied")
        self.assertEqual(nao["category"], "Root shell")

    def test_pkexec_que_ja_virou_o_programa(self):
        sessao = j("pam_unix(polkit-1:session): session opened for user root(uid=0) by alice(uid=1000)",
                   fonte="pkexec", pid="77")
        execucao = j("alice: Executing command [USER=root] [TTY=unknown] [CWD=/] "
                     "[COMMAND=/usr/local/bin/aero-memory-config set enabled 0]", fonte="pkexec", pid="77")
        execucao["_EXE"] = "/usr/bin/python3.14"            # journald saw it after the exec
        evs = eventos([sessao, execucao])
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["purpose"], "Change memory settings: set enabled 0")
        # without the real PAM line from the same pid it is ignored (could be forged)
        self.assertEqual(eventos([execucao]), [])
        outra = dict(sessao, _PID="78")
        self.assertEqual(eventos([outra, execucao]), [])
        falsa = dict(sessao, _EXE="/usr/bin/python3.14")
        self.assertEqual(eventos([falsa, execucao]), [])

    def test_pkexec_env_sem_aspas(self):
        e = eventos([j("alice: Executing command [USER=root] [TTY=unknown] [CWD=/] [COMMAND=/usr/bin/env "
                       "SHELL=/usr/bin/bash HYPRLAND_CMD=Hyprland --watchdog-fd 4 XDG=x /usr/bin/python3 "
                       "/usr/lib/pince/PINCE.py]", fonte="pkexec")])[0]
        self.assertEqual(e["purpose"], "Run python3 script PINCE.py")

    def test_su(self):
        ok = eventos([j("(to root) alice on pts/7", fonte="su", unidade="kitty-28697-0.scope")])[0]
        self.assertEqual((ok["result"], ok["category"], ok["app"]), ("allowed", "Root shell", "Terminal (kitty)"))
        ruim = eventos([j("FAILED SU (to root) alice on pts/7", fonte="su")])[0]
        self.assertEqual(ruim["result"], "wrong-password")

    def test_ruido_e_lixo(self):
        for e in (j("Started polkitd version 128", fonte="polkitd"), j(""), j("blah"),
                  {"SYSLOG_IDENTIFIER": "sudo", "MESSAGE": [255, 0, 1]}, {"SYSLOG_IDENTIFIER": "sudo"}):
            self.assertEqual(eventos([e]), [])

    def test_processos_diferentes_nao_misturam(self):
        evs = eventos([j(" a : PWD=/ ; USER=root ; COMMAND=/usr/bin/ls", pid="1"),
                       j(" b : PWD=/ ; USER=root ; COMMAND=/usr/bin/id", pid="2"),
                       j(" a : (command continued) -l", pid="1")])
        self.assertEqual(sorted(e["command"] for e in evs), ["/usr/bin/id", "/usr/bin/ls -l"])

    def test_vencidos(self):
        ju = x.Juntador()
        ju.receber(x.ler_linha(j(" a : PWD=/ ; USER=root ; COMMAND=/usr/bin/ls", ts=5.0)), agora=1000.0)
        self.assertEqual(ju.vencidos(1001.0), [])
        self.assertEqual(len(ju.vencidos(1004.0)), 1)
        self.assertEqual(ju.abertos, {})

    def test_linha_forjada_e_ignorada(self):
        falsa = j(" alice : TTY=pts/1 ; PWD=/ ; USER=root ; COMMAND=/usr/bin/true")
        falsa["_EXE"] = "/usr/bin/logger"
        self.assertEqual(eventos([falsa]), [])
        del falsa["_EXE"]
        self.assertEqual(eventos([falsa]), [])

    def test_historico_antigo_nao_corta_comando(self):
        ju = x.Juntador()
        ju.receber(x.ler_linha(j(" a : PWD=/ ; USER=root ; COMMAND=/usr/bin/cp a", pid="9", ts=10.0)), agora=500.0)
        self.assertEqual(ju.vencidos(501.0), [])           # 490 s "old" in the journal, but just arrived
        ju.receber(x.ler_linha(j(" a : (command continued) /etc/b", pid="9", ts=10.0)), agora=501.5)
        self.assertEqual(ju.todos()[0]["command"], "/usr/bin/cp a /etc/b")

    def test_app_da_unidade(self):
        for u, nome in (("app-com.anthropic.Claude-2166.scope", "Claude"), ("kitty-16031-0.scope", "Terminal (kitty)"),
                        ("session-3.scope", "Desktop session"), ("app-Hyprland-steam-55.scope", "Steam"),
                        ("", "Unknown"), ("app-antigravity-1234.scope", "antigravity")):
            self.assertEqual(x.app_da_unidade(u), nome, u)


class TestCategorias(unittest.TestCase):
    CASOS = [
        ("/usr/bin/pacman -Syu --noconfirm", "Packages", "Upgrade the whole system"),
        ("/usr/bin/pacman -R foo", "Packages", "Remove packages: foo"),
        ("/usr/bin/pacman -U /tmp/x.pkg.tar.zst", "Packages", "Install a package file: x.pkg.tar.zst"),
        ("/usr/bin/pacman -Qi foo", "Inspect (read-only)", None),
        ("/usr/bin/systemctl restart bluetooth.service", "Services", "Restart service: bluetooth.service"),
        ("/usr/bin/systemctl status x", "Inspect (read-only)", None),
        ("/usr/bin/systemctl reboot", "Power", None),
        ("/usr/bin/tee /etc/sudoers.d/10-x", "Users & security", None),
        ("/usr/bin/rm -rf /etc", "Files & config", None),
        ("/usr/bin/mkfs.ext4 /dev/sda1", "Disks & storage", None),
        ("/usr/bin/btrfs subvolume list /", "Inspect (read-only)", None),
        ("/usr/bin/bash", "Root shell", None),
        ("/usr/bin/bash -c 'echo hi'", "Scripts & code", None),
        ("/usr/bin/bash system/memory/install.sh --apply", "Scripts & code", "Run script install.sh"),
        ("/usr/bin/bash /home/alice/aero-os/install.sh", "Aero system", None),
        ("/usr/local/bin/painel-energia suspend lock ignore ignore ignore", "Aero system", None),
        ("/usr/bin/sysctl vm.swappiness", "Inspect (read-only)", None),
        ("/usr/bin/sysctl -w vm.swappiness=10", "Kernel & boot", None),
        ("/usr/bin/nmcli radio wifi off", "Network", None),
        ("/usr/bin/passwd alice", "Users & security", None),
        ("validate", "Authentication", None),
        ("/usr/bin/env FOO=1 /usr/bin/pacman -S vim", "Packages", "Install packages: vim"),
        ("/usr/bin/nice -n 5 /usr/bin/ls /root", "Inspect (read-only)", None),
        ("/usr/local/lib/x9/x9-admin delete", "X9", "Delete X9 log entries"),
        ("/usr/local/lib/x9/x9-setlock", "X9", "Set the X9 password"),
        ("/usr/local/bin/aero-snap 'X9 fix' bash system/x9/install.sh --apply", "Scripts & code",
         "Run script install.sh (with snapshots: X9 fix)"),
        ("/usr/local/bin/aero-snap undo 2 3", "Disks & storage", "Undo changes from a snapshot"),
        ("/usr/bin/cat /var/log/x9/2026-10-21.jsonl", "Inspect (read-only)", None),
        ("/opt/weird/thing --x", "Other", None),
        ("", "Other", None),
        ("'unbalanced", "Other", None),
    ]

    def test_casos(self):
        for cmd, cat, para in self.CASOS:
            c, p, r = x.classificar(cmd)
            self.assertEqual(c, cat, cmd)
            self.assertIn(r, ("low", "medium", "high"))
            if para:
                self.assertEqual(p, para, cmd)

    def test_riscos(self):
        self.assertEqual(x.classificar("/usr/bin/rm -rf /etc")[2], "high")
        self.assertEqual(x.classificar("/usr/bin/rm /tmp/a")[2], "medium")
        self.assertEqual(x.classificar("/usr/bin/bash")[2], "high")
        self.assertEqual(x.classificar("/usr/bin/ls /")[2], "low")
        self.assertEqual(x.classificar("su", "su")[0], "Root shell")


class TestArquivo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def ev(self, ts, **kw):
        base = {"id": f"{int(ts * 1000):016x}"[-16:], "ts": ts, "category": "Packages", "result": "allowed",
                "purpose": "p", "command": "c", "app": "Claude", "risk": "low", "tool": "sudo"}
        base.update(kw)
        return base

    def test_por_dia_e_regravar(self):
        hoje = time.time()
        a, b = self.ev(hoje), self.ev(hoje - 3 * 86400)
        with x.Trava(self.d):
            x.acrescentar([a, b], pasta=self.d)
        self.assertEqual(len(x.dias(self.d)), 2)
        self.assertEqual(x.ler_dia(x.dia_de(hoje), self.d), [a])
        modo = os.stat(x.arquivo_do_dia(x.dia_de(hoje), self.d)).st_mode & 0o777
        self.assertEqual(modo, 0o640)
        x.regravar_dia(x.dia_de(hoje), [], pasta=self.d)
        self.assertEqual(x.dias(self.d), [x.dia_de(b["ts"])])

    def test_linhas_estragadas_sao_ignoradas(self):
        caminho = x.arquivo_do_dia("2026-01-02", self.d)
        with open(caminho, "w") as f:
            f.write('{"id":"0123456789abcdef","ts":1}\nlixo\n{"id":"../x"}\n[1]\n')
        self.assertEqual([e["id"] for e in x.ler_dia("2026-01-02", self.d)], ["0123456789abcdef"])

    def test_dia_invalido(self):
        for ruim in ("../etc/passwd", "2026-1-1", "2026-01-01.jsonl", ""):
            with self.assertRaises(ValueError):
                x.arquivo_do_dia(ruim, self.d)

    def test_symlink_nao_e_seguido(self):
        alvo = os.path.join(self.d, "alvo")
        open(alvo, "w").close()
        dia = x.dia_de(time.time())
        os.symlink(alvo, x.arquivo_do_dia(dia, self.d))
        with self.assertRaises(OSError):
            x.acrescentar([self.ev(time.time())], pasta=self.d)
        with open(alvo) as f:
            self.assertEqual(f.read(), "")

    def test_retencao(self):
        agora = time.time()
        velho = self.ev(agora - 100 * 86400)
        leitura = self.ev(agora - 2 * 3600, category="Inspect (read-only)")
        novo = self.ev(agora - 60)
        x.acrescentar([velho, leitura, novo], pasta=self.d)
        regras = x.validar_retencao({"keep_days": 90, "rules": [
            {"category": "Inspect (read-only)", "result": "any", "after": 1, "unit": "hours"}]})
        self.assertEqual(x.aplicar_retencao(regras, agora, pasta=self.d), 2)
        self.assertEqual([e["id"] for d in x.dias(self.d) for e in x.ler_dia(d, self.d)], [novo["id"]])

    def test_retencao_para_sempre(self):
        r = x.validar_retencao({"keep_days": 0, "rules": []})
        self.assertFalse(x.vence(self.ev(0), r, time.time()))

    def test_validar_retencao_recusa(self):
        for ruim in ([], {"keep_days": -1}, {"keep_days": True}, {"keep_days": 99999}, {"x": 1},
                     {"rules": [{"category": "Nope", "after": 1}]}, {"rules": [{"after": 0}]},
                     {"rules": [{"after": 1, "unit": "weeks"}]}, {"rules": [{"after": 1, "evil": 1}]},
                     {"rules": [{"after": 1}] * 51}, {"rules": [{"after": "1"}]}):
            with self.assertRaises(ValueError, msg=ruim):
                x.validar_retencao(ruim)

    def test_ler_retencao_padrao(self):
        self.assertEqual(x.ler_retencao(os.path.join(self.d, "nada.json")), x.RETENCAO_PADRAO)


class TestFiltros(unittest.TestCase):
    def ev(self, quando, **kw):
        ts = time.mktime(time.strptime(quando, "%Y-%m-%d %H:%M"))
        base = {"id": "0" * 16, "ts": ts, "category": "Packages", "result": "allowed", "risk": "low",
                "app": "Claude", "purpose": "Install packages: htop", "command": "/usr/bin/pacman -S htop"}
        base.update(kw)
        return base

    def test_dias_e_horas_juntos_ou_separados(self):
        e = self.ev("2026-10-20 23:30")
        self.assertTrue(x.passa(e, {}))
        self.assertTrue(x.passa(e, {"day_from": "2026-10-20", "day_to": "2026-10-20"}))
        self.assertFalse(x.passa(e, {"day_from": "2026-10-21"}))
        self.assertFalse(x.passa(e, {"day_to": "2026-10-19"}))
        self.assertTrue(x.passa(e, {"time_from": "22:00", "time_to": "06:00"}))     # cruza a meia-noite
        self.assertFalse(x.passa(self.ev("2026-10-20 12:00"), {"time_from": "22:00", "time_to": "06:00"}))
        self.assertTrue(x.passa(e, {"time_from": "23:00"}))
        self.assertFalse(x.passa(e, {"time_to": "23:00"}))
        self.assertTrue(x.passa(e, {"day_from": "2026-10-20", "time_from": "23:00", "category": "Packages"}))
        self.assertFalse(x.passa(e, {"day_from": "2026-10-20", "time_from": "23:00", "category": "Network"}))

    def test_categoria_resultado_risco_app_texto(self):
        e = self.ev("2026-10-20 10:00")
        self.assertTrue(x.passa(e, {"result": "allowed", "risk": "low", "app": "Claude", "text": "PACMAN htop"}))
        self.assertFalse(x.passa(e, {"result": "denied"}))
        self.assertFalse(x.passa(e, {"app": "Steam"}))
        self.assertFalse(x.passa(e, {"text": "pacman vim"}))

    def test_hora_invalida(self):
        self.assertEqual(x.minutos("7:05"), 425)
        self.assertIsNone(x.minutos(""))
        for ruim in ("24:00", "7", "ab:cd", "12:60"):
            with self.assertRaises(ValueError):
                x.minutos(ruim)


class TestSenha(unittest.TestCase):
    def test_hash(self):
        r = x.hash_senha("abcd")
        self.assertTrue(x.confere_senha("abcd", r))
        self.assertFalse(x.confere_senha("abce", r))
        self.assertFalse(x.confere_senha("abcd", {"salt": "zz", "hash": "00"}))
        self.assertFalse(x.confere_senha("abcd", {}))
        self.assertNotIn("abcd", json.dumps(r))

    def test_regras(self):
        self.assertTrue(x.senha_boa("abc"))
        self.assertTrue(x.senha_boa("a" * 257))
        self.assertTrue(x.senha_boa("abcd\n"))
        self.assertEqual(x.senha_boa("abcd"), "")

    def test_gravar_e_ler(self):
        d = tempfile.mkdtemp()
        x.gravar_trava(1000, os.getgid(), x.hash_senha("segredo"), pasta=d)
        self.assertTrue(x.confere_senha("segredo", x.ler_trava(1000, pasta=d)))
        self.assertEqual(os.stat(x.arquivo_trava(1000, d)).st_mode & 0o777, 0o640)
        self.assertIsNone(x.ler_trava(1001, pasta=d))


def carregar(nome):
    caminho = os.path.join(X9, nome)
    loader = importlib.machinery.SourceFileLoader(nome.replace("-", "_"), caminho)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestAdmin(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.troca = mock.patch.object(x, "ARQUIVO", self.d)
        self.troca.start()
        self.admin = carregar("x9-admin")

    def tearDown(self):
        self.troca.stop()

    def test_apagar_deixa_nota(self):
        agora = time.time()
        evs = [{"id": f"{i:016x}", "ts": agora - i, "category": "Packages"} for i in range(1, 4)]
        x.acrescentar(evs)
        with mock.patch.object(self.admin, "gid_wheel", return_value=None):
            self.assertEqual(self.admin.apagar([evs[0]["id"], evs[1]["id"]], os.getuid()), 2)
        ficaram = [e for d in x.dias() for e in x.ler_dia(d)]
        self.assertEqual(len(ficaram), 2)
        nota = [e for e in ficaram if e["category"] == "X9"]
        self.assertEqual(len(nota), 1)
        self.assertEqual(nota[0]["purpose"], "Deleted 2 log entries")

    def test_apagar_so_notas_nao_cria_outra(self):
        x.acrescentar([{"id": "a" * 16, "ts": time.time(), "category": "X9"}])
        with mock.patch.object(self.admin, "gid_wheel", return_value=None):
            self.assertEqual(self.admin.apagar(["a" * 16], os.getuid()), 1)
        self.assertEqual(x.dias(), [])

    def test_sem_root_recusa(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        with self.assertRaises(SystemExit):
            self.admin.principal(["delete-all"])


if __name__ == "__main__":
    unittest.main()
