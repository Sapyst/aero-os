"""Testes do guarda de memória (system/memory/aero-memory) e do ajudante aero-memory-config.

Rodar (como usuário comum, sem instalar nada):
    python3 -B -m unittest discover -s tests/memory -p 'test_aero_memory.py' -v

- Funções puras: /proc falso (fixtures/ e árvores montadas em pasta temporária).
- Kernel de verdade, só com processos FILHOS deste teste (comm único via prctl): pidfd, SIGSTOP/
  SIGCONT, SIGTERM/SIGKILL, oom_score_adj, gatilho do PSI sem privilégio.
- O daemon de verdade em --check e --dry-run (fds/threads estáveis, saída limpa com SIGTERM).
Nada aqui manda sinal pra processo que não seja filho do teste.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import random
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.dont_write_bytecode = True      # não sujar o repo com __pycache__ dos scripts sem .py

AQUI = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(AQUI))
DAEMON = os.path.join(REPO, "system", "memory", "aero-memory")
HELPER = os.path.join(REPO, "system", "memory", "aero-memory-config")
FIX = os.path.join(AQUI, "fixtures")
HOME = os.path.expanduser("~")


def carregar(nome, caminho):
    loader = importlib.machinery.SourceFileLoader(nome, caminho)
    spec = importlib.util.spec_from_loader(nome, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


am = carregar("aero_memory", DAEMON)
amc = carregar("aero_memory_config", HELPER)


def fix(nome, modo="r"):
    with open(os.path.join(FIX, nome), modo) as f:
        return f.read()


def leituras(total_gb=32, disp_gb=20, swap_gb=4, swap_livre_gb=4, some=0.0, full=0.0):
    g = 1024 ** 3
    return {"mem_total": int(total_gb * g), "mem_available": int(disp_gb * g),
            "swap_total": int(swap_gb * g), "swap_free": int(swap_livre_gb * g),
            "psi_some": some, "psi_full": full}


def leituras_swaps(disp_gb, zram=(15.6, 0.0), ssd=None, total_gb=31.0, some=0.0, full=0.0):
    """Leituras com zram (tamanho, usado em GB, prioridade 100) e, opcional, o arquivo no SSD
    (tamanho, usado, prioridade 10), com swap_headroom calculado como o daemon faz."""
    g = 1024 ** 3
    linhas = ["Filename\tType\tSize\tUsed\tPriority"]
    devs = [("/dev/zram0", "partition", zram, 100)] + ([("/swap/swapfile", "file", ssd, 10)] if ssd else [])
    for nome, tipo, (tam, usado), prio in devs:
        linhas.append(f"{nome}\t{tipo}\t{int(tam * g / 1024)}\t{int(usado * g / 1024)}\t{prio}")
    tabela = am.parse_swaps("\n".join(linhas) + "\n")
    total_swap = sum(d[1] for d in tabela)
    usado_swap = sum(d[2] for d in tabela)
    return {"mem_total": int(total_gb * g), "mem_available": int(disp_gb * g), "swap_total": total_swap,
            "swap_free": total_swap - usado_swap, "swap_headroom": am.swap_headroom(tabela),
            "psi_some": some, "psi_full": full}


def P(pid, comm, cfg=None, **kw):
    """Proc pronto pros testes de plano/vítima."""
    base = dict(pid=pid, start=pid * 10, comm=comm, state="S", ppid=1, session=1, tty=0,
                uids=(1000, 1000, 1000, 1000), mem=100 * am.MiB, adj=0, score=100,
                exe_ok=None, aero=False, comfy=False)
    base.update(kw)
    return am.classify(am.Proc(**base), cfg or am.DEFAULTS)


def silencio():
    return contextlib.redirect_stderr(io.StringIO())


# ---------- /proc falso ----------

def fake_proc(raiz, pid, comm, *, ppid=1, session=None, tty=0, state="S", uids=(1000,) * 4,
              rss_pages=1000, swap_kb=0, start=None, adj=0, score=100, argv=None, cwd=None,
              exe=None, flags=0x400100):
    d = os.path.join(raiz, str(pid))
    os.makedirs(d)
    start = pid * 10 if start is None else start
    session = pid if session is None else session
    with open(os.path.join(d, "stat"), "w") as f:
        f.write(f"{pid} ({comm}) {state} {ppid} {pid} {session} {tty} -1 {flags} 0 0 0 0 0 0 0 0 20 0 1 0 "
                f"{start} 1000 {rss_pages} 0 0 0 0 0 0 0 0 0 0 0 0 17 0 0 0 0 0 0\n")
    with open(os.path.join(d, "status"), "w") as f:
        f.write(f"Name:\t{comm}\nUid:\t" + "\t".join(map(str, uids)) + f"\nVmSwap:\t{swap_kb} kB\n")
    with open(os.path.join(d, "oom_score_adj"), "w") as f:
        f.write(f"{adj}\n")
    with open(os.path.join(d, "oom_score"), "w") as f:
        f.write(f"{score}\n")
    with open(os.path.join(d, "cmdline"), "wb") as f:
        f.write(b"\0".join(a.encode() for a in (argv or [comm])) + b"\0")
    if cwd:
        os.symlink(cwd, os.path.join(d, "cwd"))
    if exe:
        os.symlink(exe, os.path.join(d, "exe"))
    return d


def ler_adj(raiz, pid):
    with open(os.path.join(raiz, str(pid), "oom_score_adj")) as f:
        return int(f.read().strip())


# ---------- processos filhos de verdade ----------

def comm_unico():
    return "aerot" + "".join(random.choice("abcdefghij") for _ in range(8))


def filho(comm, ignorar_term=False):
    """Filho python com comm próprio (prctl PR_SET_NAME) dormindo; devolve o Popen."""
    codigo = ("import ctypes, signal, time\n"
              f"ctypes.CDLL(None).prctl(15, {comm!r}.encode(), 0, 0, 0)\n"
              + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignorar_term else "")
              + "print('ready', flush=True)\ntime.sleep(60)\n")
    p = subprocess.Popen([sys.executable, "-I", "-c", codigo], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    p.stdout.readline()
    return p


def estado(pid):
    with open(f"/proc/{pid}/stat", "rb") as f:
        return am.parse_stat(f.read())


def esperar_estado(pid, quer, prazo=2.0):
    fim = time.monotonic() + prazo
    while time.monotonic() < fim:
        try:
            if estado(pid).state in quer:
                return True
        except OSError:
            return False
        time.sleep(0.02)
    return False


def matar(p):
    try:
        os.kill(p.pid, signal.SIGCONT)
        p.kill()
    except ProcessLookupError:
        pass
    p.wait(timeout=5)
    p.stdout.close()


# =====================================================================

class TestParsers(unittest.TestCase):
    def test_meminfo_normal(self):
        r = am.parse_meminfo(fix("meminfo_normal.txt"))
        self.assertEqual(r["mem_total"], 32000000 * 1024)
        self.assertEqual(r["mem_available"], 20000000 * 1024)
        self.assertEqual(r["swap_total"], 4194300 * 1024)
        self.assertEqual(r["swap_free"], 4194300 * 1024)

    def test_meminfo_noswap_and_bytes(self):
        r = am.parse_meminfo(fix("meminfo_noswap.txt", "rb"))
        self.assertEqual(r["swap_total"], 0)
        self.assertEqual(r["swap_free"], 0)

    def test_meminfo_broken(self):
        with self.assertRaises(ValueError):
            am.parse_meminfo(fix("meminfo_broken.txt"))
        with self.assertRaises(ValueError):
            am.parse_meminfo("")

    def test_meminfo_clamps(self):
        r = am.parse_meminfo("MemTotal: 100 kB\nMemAvailable: 500 kB\nSwapTotal: 10 kB\nSwapFree: 99 kB\n")
        self.assertEqual(r["mem_available"], r["mem_total"])
        self.assertEqual(r["swap_free"], r["swap_total"])

    def test_psi(self):
        self.assertEqual(am.parse_psi(fix("pressure_memory.txt")), {"some": 23.5, "full": 11.25})
        self.assertEqual(am.parse_psi(fix("pressure_some_only.txt")), {"some": 1.5, "full": 0.0})
        self.assertEqual(am.parse_psi("some avg10=abc\nfull avg10=-3 avg60=9\njunk\n"), {"some": 0.0, "full": 0.0})

    def test_stat_spaces(self):
        s = am.parse_stat(fix("stat_spaces.txt"))
        self.assertEqual((s.pid, s.comm, s.state, s.ppid, s.session, s.tty), (4242, "Web Content", "S", 4200, 1177, 0))
        self.assertEqual(s.starttime, 987654)
        self.assertEqual(s.rss_pages, 51200)
        self.assertFalse(s.flags & am.PF_KTHREAD)

    def test_stat_parens(self):
        s = am.parse_stat(fix("stat_parens.txt", "rb"))
        self.assertEqual(s.comm, "a) b (c)")
        self.assertEqual((s.state, s.ppid, s.session, s.tty, s.starttime, s.rss_pages), ("R", 1, 777, 34816, 5555, 256))

    def test_stat_newline_in_comm(self):
        linha = fix("stat_plain.txt").replace("(Hyprland)", "(x\nFAKE) y)")
        s = am.parse_stat(linha)
        self.assertEqual(s.comm, "x\nFAKE) y")
        self.assertEqual(s.starttime, 2600)
        self.assertEqual(am.sanitize_name(s.comm), "x?FAKE) y")

    def test_stat_kthread(self):
        self.assertTrue(am.parse_stat(fix("stat_kthread.txt")).flags & am.PF_KTHREAD)

    def test_stat_bad(self):
        for ruim in ("", "123 no parens S 1", "12 (x) S 1 2 3", "(x) S " + "1 " * 30, "a (x) S " + "1 " * 30):
            with self.assertRaises(ValueError, msg=ruim):
                am.parse_stat(ruim)

    def test_status(self):
        self.assertEqual(am.parse_status(fix("status_user.txt")), ((1000,) * 4, 2048 * 1024))
        self.assertEqual(am.parse_status(fix("status_sudo.txt", "rb"))[0], (1000, 0, 0, 0))
        with self.assertRaises(ValueError):
            am.parse_status(fix("status_nouid.txt"))

    def test_oom_kills(self):
        # X3: o contador do OOM killer do kernel (/proc/vmstat)
        self.assertEqual(am.parse_oom_kills(fix("vmstat_oom.txt")), 3)
        self.assertEqual(am.parse_oom_kills(fix("vmstat_oom.txt", "rb")), 3)
        self.assertIsNone(am.parse_oom_kills("nr_free_pages 1\npswpin 2\n"))         # sem a linha
        self.assertIsNone(am.parse_oom_kills("oom_kill x\n"))
        self.assertIsNone(am.parse_oom_kills("oom_kill -1\n"))
        self.assertIsNone(am.parse_oom_kills("oom_kill 1 2\n"))
        self.assertIsNone(am.parse_oom_kills(""))
        self.assertEqual(am.parse_oom_kills("oom_kill_extra 9\noom_kill 0\n"), 0)
        n = am.read_oom_kills()                                     # o /proc/vmstat de verdade
        self.assertTrue(n is None or (type(n) is int and n >= 0))
        self.assertIsNone(am.read_oom_kills("/nonexistent"))

    def test_cmdline(self):
        self.assertEqual(am.parse_cmdline(b"python3\0/x/y.py\0--port\08188\0"), ["python3", "/x/y.py", "--port", "8188"])
        self.assertEqual(am.parse_cmdline(b""), [])
        self.assertEqual(len(am.parse_cmdline(b"a\0" * 100)), 64)


class TestClassify(unittest.TestCase):
    def test_is_user(self):
        self.assertTrue(am.is_user((1000, 1000, 1000, 1000)))
        self.assertFalse(am.is_user((1000, 0, 0, 0)))          # sudo / setuid
        self.assertFalse(am.is_user((0, 0, 0, 0)))
        self.assertFalse(am.is_user((65534,) * 4))             # nobody
        self.assertFalse(am.is_user((61184,) * 4))             # DynamicUser
        self.assertFalse(am.is_user(()))

    def test_full_name(self):
        # BC2: o comm cortado em 15 volta a ser o nome da lista (o que o agente e o Settings mostram)
        self.assertEqual(am.full_name("fossilize_repla", ("steam", "fossilize_replay")), "fossilize_replay")
        self.assertEqual(am.full_name("steam", ("steam", "fossilize_replay")), "steam")
        self.assertEqual(am.full_name("firefox", ("fossilize_replay",)), "firefox")
        self.assertEqual(am.full_name("fossilize", ("fossilize_replay",)), "fossilize")
        self.assertEqual(am.full_name("x", ()), "x")

    def test_comm_truncation(self):
        self.assertTrue(am.comm_matches("fossilize_repla", ("fossilize_replay",)))
        self.assertTrue(am.comm_matches("xdg-desktop-por", am.VITAL))
        self.assertTrue(am.comm_matches("dbus-broker-lau", am.VITAL))
        self.assertFalse(am.comm_matches("fossilize", ("fossilize_replay",)))
        self.assertFalse(am.comm_matches("systemd-journal", am.VITAL))

    def test_aero_python(self):
        def aero(argv, home="/h/u"):
            return am.is_aero_path(am.script_path(argv, "/h/u"), home)
        self.assertTrue(aero(["python3", "/h/u/.local/share/aero-shell/widget.py"]))
        self.assertTrue(aero(["/usr/bin/python3", "-u", "/h/u/.local/share/aero-settings/main.py"]))
        self.assertTrue(aero(["/usr/bin/python3", "-I", "/h/u/.local/bin/aero-memory-agent", "--dry-run"]))
        self.assertTrue(aero(["python3", "-X", "dev", "/h/u/.local/share/aero-shell/b"]))
        self.assertTrue(aero(["python3", ".local/share/aero-shell/b.py"]))          # relativo ao cwd
        self.assertFalse(aero(["/usr/bin/python3", "-u", "/x/aero-settings/main.py"]))
        self.assertFalse(aero(["python3", "-c", "'/aero-shell/'"]))
        self.assertFalse(aero(["python3", "-m", "aero-shell"]))
        self.assertFalse(aero(["bash", "/h/u/.local/share/aero-shell/y"]))
        self.assertFalse(aero(["python3", "/x/other.py", "/h/u/.local/share/aero-shell/"]))
        self.assertFalse(aero([]))

    def test_aero_paths(self):
        home = "/h/u"
        self.assertEqual(am.script_path(["python3", "-I", "/h/u/.local/share/aero-shell/dock.py"], None),
                         "/h/u/.local/share/aero-shell/dock.py")
        self.assertEqual(am.script_path(["python3", "dock.py"], "/h/u/.local/share/aero-shell"),
                         "/h/u/.local/share/aero-shell/dock.py")
        self.assertIsNone(am.script_path(["python3", "dock.py"], None))
        self.assertIsNone(am.script_path(["python3", "-c", "x"], "/"))
        ok = am.is_aero_path
        self.assertTrue(ok("/h/u/.local/share/aero-shell/dock.py", home))
        self.assertTrue(ok("/h/u/.local/share/aero-settings/main.py", home + "/"))
        self.assertTrue(ok("/h/u/.local/bin/aero-memory-agent", home))
        self.assertTrue(ok("/usr/local/bin/aero-memory-agent", home))
        self.assertFalse(ok("/tmp/x/aero-shell/hog.py", home))                       # fora da home
        self.assertFalse(ok("/h/other/.local/share/aero-shell/dock.py", home))       # home de outro
        self.assertFalse(ok("/h/u/.local/share/aero-shell", home))                   # a pasta não é script
        self.assertFalse(ok("/h/u/.local/share/aero-shellx/a.py", home))
        self.assertFalse(ok(am.script_path(["python3", "/h/u/.local/share/aero-shell/../../../t/x.py"], None), home))
        self.assertFalse(ok("/tmp/aero-memory-agent", home))
        self.assertFalse(ok("/h/u/.local/share/aero-shell/dock.py", None))
        self.assertFalse(ok("/h/u/.local/share/aero-shell/dock.py", "relative"))

    def test_comfy(self):
        argv = ["/d/venv/bin/python", "main.py", "--listen", "127.0.0.1", "--port", "8188"]
        self.assertTrue(am.is_comfy_backend(argv, "ComfyUI"))
        self.assertFalse(am.is_comfy_backend(argv, "other"))
        self.assertTrue(am.is_comfy_backend(["python", "/mnt/x/ComfyUI/main.py"], None))
        self.assertTrue(am.is_comfy_backend(["python3.12", "main.py", "--comfy-api"], None))
        self.assertFalse(am.is_comfy_backend(["python", "/mnt/x/ComfyUI/server.py"], "ComfyUI"))
        self.assertFalse(am.is_comfy_backend(["node", "main.py"], "ComfyUI"))

    def test_classify_spoof(self):
        self.assertTrue(P(5, "Hyprland", exe_ok=True).vital)
        self.assertTrue(P(5, "Hyprland", exe_ok=True).never_kill)
        falso = P(5, "Hyprland", exe_ok=False, exe_name=False)   # outro programa com nome de compositor
        self.assertFalse(falso.vital)
        self.assertFalse(falso.never_kill)
        self.assertTrue(P(6, "x", aero=True).vital)
        # "script do Aero" gastão: perde a proteção (o dono pode pôr qualquer coisa na própria home)
        self.assertFalse(P(6, "x", aero=True, mem=am.AERO_MAX_MEM + 1).vital)
        cfg = dict(am.DEFAULTS, protect=("obs",))
        self.assertTrue(P(7, "obs", cfg).user_protect)
        self.assertTrue(P(8, "fossilize_repla").background)


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg, prob = am.parse_config("")
        self.assertEqual(cfg, am.DEFAULTS)
        self.assertEqual(prob, [])

    def test_full(self):
        cfg, prob = am.parse_config(fix("memory_full.conf"))
        self.assertEqual(prob, [])
        self.assertEqual(cfg["pause_background"], True)
        self.assertEqual(cfg["kill_on_critical"], False)
        self.assertEqual((cfg["warn_available"], cfg["critical_available"]), (20, 5))
        self.assertEqual(cfg["protect"], ("steam", "obs"))
        self.assertEqual(cfg["background"], ("fossilize_replay", "shadercache"))

    def test_bad(self):
        cfg, prob = am.parse_config(fix("memory_bad.conf"))
        self.assertEqual(cfg["enabled"], True)
        self.assertEqual(cfg["warn_available"], 15)
        self.assertEqual(cfg["critical_available"], 4)
        self.assertEqual(cfg["protect"], ())
        self.assertEqual(cfg["background"], ("fossilize_replay",))
        self.assertGreaterEqual(len(prob), 6)
        self.assertFalse(any("mystery" in p for p in prob))   # texto desconhecido não vai pro log

    def test_pair(self):
        cfg, prob = am.parse_config("warn_available=5\ncritical_available=5\n")
        self.assertEqual((cfg["warn_available"], cfg["critical_available"]), (15, 4))
        self.assertTrue(prob)
        cfg, prob = am.parse_config("warn_available=30\ncritical_available=29\n")
        self.assertEqual((cfg["warn_available"], cfg["critical_available"]), (30, 29))

    def test_empty_list_and_bools(self):
        cfg, _ = am.parse_config("background=\nenabled=off\nkill_on_critical=TRUE\n")
        self.assertEqual(cfg["background"], ())
        self.assertFalse(cfg["enabled"])
        self.assertTrue(cfg["kill_on_critical"])

    def test_export(self):
        e = am.export_config(am.DEFAULTS)
        self.assertEqual(e["enabled"], 1)
        self.assertEqual(e["background"], ["fossilize_replay"])
        json.dumps(e)

    def test_load_missing(self):
        cfg, prob = am.load_config("/nonexistent/aero/memory.conf")
        self.assertEqual(cfg, am.DEFAULTS)
        self.assertEqual(prob, [])


class TestLevels(unittest.TestCase):
    cfg = am.DEFAULTS

    def lv(self, ant, r, calmo=None, agora=100.0, pico=False):
        return am.decide_level(ant, calmo, r, self.cfg, agora, pico)

    def test_normal(self):
        self.assertEqual(self.lv("normal", leituras()), ("normal", None))

    def test_warning_by_headroom(self):
        # (1 + 2) / 32 = 9.4% < 15%
        self.assertEqual(self.lv("normal", leituras(disp_gb=1, swap_livre_gb=2))[0], "warning")

    def test_warning_by_psi_and_spike(self):
        self.assertEqual(self.lv("normal", leituras(some=25))[0], "warning")
        # um soluço do gatilho com a memória folgada: continua normal (nada pausado nem descarregado)
        self.assertEqual(self.lv("normal", leituras(), pico=True), ("normal", None))
        # no aviso, o pico segura o aviso (não começa a contar a calma)
        self.assertEqual(self.lv("warning", leituras(), None, 100.0, pico=True), ("warning", None))

    def test_critical(self):
        r = leituras(disp_gb=1, swap_livre_gb=0.2)              # 3% e swap 5% livre
        self.assertEqual(self.lv("normal", r)[0], "critical")
        r = leituras(disp_gb=1, swap_livre_gb=3, full=12)      # swap folgado mas full alto
        self.assertEqual(self.lv("warning", r)[0], "critical")
        r = leituras(disp_gb=1, swap_gb=0, swap_livre_gb=0)    # sem swap nenhum
        self.assertEqual(self.lv("normal", r)[0], "critical")

    def test_low_but_swap_ok_is_warning(self):
        r = leituras(disp_gb=1, swap_livre_gb=3, full=1)
        self.assertEqual(self.lv("normal", r)[0], "warning")

    def test_hysteresis(self):
        calmo = leituras()                                      # folga enorme, PSI 0
        nivel, desde = self.lv("warning", calmo, None, 100.0)
        self.assertEqual((nivel, desde), ("warning", 100.0))
        nivel, desde = self.lv("warning", calmo, desde, 129.0)
        self.assertEqual((nivel, desde), ("warning", 100.0))
        nivel, desde = self.lv("warning", calmo, desde, 130.0)
        self.assertEqual((nivel, desde), ("normal", None))

    def test_hysteresis_interrupted(self):
        nivel, desde = self.lv("warning", leituras(), None, 100.0)
        nivel, desde = self.lv("warning", leituras(some=6), desde, 110.0)   # PSI ainda >= 5
        self.assertEqual((nivel, desde), ("warning", None))
        nivel, desde = self.lv("warning", leituras(), None, 111.0)
        self.assertEqual(desde, 111.0)

    def test_band_stays(self):
        # folga 20% (entre 15 e 25): no normal fica normal, no warning fica warning
        r = leituras(total_gb=10, disp_gb=1, swap_gb=1, swap_livre_gb=1)
        self.assertEqual(self.lv("normal", r)[0], "normal")
        self.assertEqual(self.lv("warning", r, None, 0)[0], "warning")
        self.assertEqual(self.lv("warning", r, 0.0, 1000.0)[0], "warning")

    def test_critical_to_warning(self):
        self.assertEqual(self.lv("critical", leituras(disp_gb=2, swap_livre_gb=0))[0], "warning")

    def test_ssd_swap_is_not_headroom(self):
        # zram quase cheio + arquivo no SSD vazio: o SSD livre não conta como folga (X1)
        r = leituras_swaps(0.8, zram=(15.5, 15.4), ssd=(16, 0))
        self.assertEqual(self.lv("normal", r)[0], "warning")
        sem = dict(r)
        del sem["swap_headroom"]                      # sem /proc/swaps: o SwapFree todo (o jeito antigo)
        self.assertEqual(self.lv("normal", sem)[0], "normal")

    def test_zram_used_does_not_calm_down(self):
        # 1,5 GB disponível com o zram 10/15,6 usado: a folga é ~17%, não volta ao normal (B-4)
        r = leituras_swaps(1.5, zram=(15.6, 10), ssd=(16, 0), some=2)
        folga, _, _ = am.metrics(r)
        self.assertLess(folga, self.cfg["warn_available"] + 10)
        self.assertEqual(self.lv("warning", r, 50.0, 1000.0), ("warning", None))

    def test_ssd_thrash_is_critical(self):
        # zram cheio, trocando páginas com o SSD, 4,8% disponível, PSI full 40: crítico (B-4)
        r = leituras_swaps(1.5, zram=(15.6, 15.6), ssd=(16, 5), full=40)
        self.assertEqual(self.lv("warning", r)[0], "critical")
        r["psi_full"] = 25                            # ainda não travando de verdade: só aviso
        self.assertEqual(self.lv("warning", r)[0], "warning")
        # sem swap nenhum, o cache "quente" segurando o MemAvailable em 6%: também
        r = leituras(disp_gb=2, swap_gb=0, swap_livre_gb=0, full=40)
        self.assertEqual(self.lv("warning", r)[0], "critical")

    def test_thrash_with_free_ram_is_not_critical(self):
        # PSI full alto com memória sobrando (um cgroup com limite apertado): ninguém é fechado
        self.assertNotEqual(self.lv("normal", leituras(disp_gb=20, swap_gb=0, swap_livre_gb=0, full=60))[0],
                            "critical")
        self.assertNotEqual(self.lv("warning", leituras_swaps(1.5, zram=(15.6, 2), full=60))[0], "critical")


class TestReadings(unittest.TestCase):
    def test_read_readings_real(self):
        r = am.Guard("/nonexistent.conf", None, dry_run=True).read_readings()      # sem mock
        for k in ("mem_total", "mem_available", "swap_total", "swap_free", "psi_some", "psi_full",
                  "swap_headroom"):
            self.assertIn(k, r)
        am.decide_level("normal", None, r, am.DEFAULTS, 0.0)

    def test_read_readings_fake(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "meminfo"), "w") as f:
                f.write("MemTotal: 1000 kB\nMemAvailable: 500 kB\nSwapTotal: 300 kB\nSwapFree: 300 kB\n")
            r = am.read_readings(d)
            self.assertEqual((r["psi_ok"], r["psi_some"], r["psi_full"]), (False, 0.0, 0.0))
            self.assertNotIn("swap_headroom", r)              # sem /proc/swaps: metrics usa o SwapFree
            self.assertAlmostEqual(am.metrics(r)[0], 80.0)
            os.mkdir(os.path.join(d, "pressure"))
            with open(os.path.join(d, "pressure", "memory"), "w") as f:
                f.write(fix("pressure_memory.txt"))
            with open(os.path.join(d, "swaps"), "w") as f:
                f.write("Filename Type Size Used Priority\n")
            r = am.read_readings(d)
            self.assertEqual((r["psi_ok"], r["psi_some"]), (True, 23.5))
            self.assertNotIn("swap_headroom", r)              # tabela vazia com SwapTotal > 0: estranho
            with open(os.path.join(d, "swaps"), "w") as f:
                f.write("Filename Type Size Used Priority\n/dev/zram0 partition 300 0 100\n")
            self.assertEqual(am.read_readings(d)["swap_headroom"], 200 * 1024)


class TestProtectionPlan(unittest.TestCase):
    def test_plan(self):
        procs = [
            P(100, "Hyprland", exe_ok=True, adj=0),
            P(101, "Hyprland", exe_ok=False, adj=0, ppid=1),                       # falso
            P(102, "greetd", exe_ok=True, adj=0, uids=(0,) * 4),                   # root vital
            P(103, "sshd", adj=0, uids=(0,) * 4),                                  # root comum
            P(104, "fossilize_repla", adj=0),
            P(105, "python", comfy=True, adj=100),
            P(120, "Hyprland", exe_ok=True, adj=-900),                             # já protegido
            P(106, "firefox", adj=-900, ppid=120),                                 # herdou
            P(107, "bash", adj=-900, ppid=106),                                    # neto
            P(108, "obs", adj=-900, ppid=103),                                     # pai não é -900 (herdou antes)
            P(109, "waybar", exe_ok=True, adj=-950),                               # já mais protegido
            P(110, "fossilize_repla", adj=800),                                    # já acima
            P(111, "steam", am.DEFAULTS, adj=0),
            P(112, "x", aero=True, adj=-900),
        ]
        orig = {}
        mudar = {p.pid: (v, m) for p, v, m in am.plan_protection(procs, orig, set())}
        self.assertEqual(mudar[100], (-900, "protect"))
        self.assertNotIn(101, mudar)
        self.assertEqual(mudar[102], (-900, "protect"))
        self.assertNotIn(103, mudar)
        self.assertEqual(mudar[104], (500, "prefer"))
        self.assertEqual(mudar[105], (500, "prefer"))
        self.assertEqual(mudar[106], (0, "inherited"))
        self.assertEqual(mudar[107], (0, "inherited"))
        self.assertEqual(mudar[108], (0, "inherited"))
        self.assertNotIn(109, mudar)
        self.assertNotIn(110, mudar)
        self.assertNotIn(111, mudar)
        self.assertNotIn(112, mudar)
        self.assertNotIn(120, mudar)
        self.assertEqual(orig[(100, 1000)], 0)
        self.assertEqual(orig[(120, 1200)], am.DEFAULT_RESTORE_ADJ)

    def test_inherited_uses_original_value(self):
        hypr = P(100, "Hyprland", exe_ok=True, adj=200)
        orig = {}
        am.plan_protection([hypr], orig, set())
        hypr.adj = -900
        app = P(200, "firefox", adj=-900, ppid=100)
        mudar = am.plan_protection([hypr, app], orig, {hypr.key})
        self.assertEqual([(p.pid, v) for p, v, m in mudar], [(200, 200)])
        # visto antes = não mexe de novo (alguém pode ter posto -900 de propósito depois)
        self.assertEqual(am.plan_protection([hypr, app], orig, {hypr.key, app.key}), [])

    def test_orphan_app_is_reset(self):
        # app aberto pelo dock (gio + fork duplo): filho do PID 1, na sessão do Hyprland (B-1)
        hypr = P(1177, "Hyprland", exe_ok=True, adj=-900, session=1177)
        app = P(5000, "antigravity", adj=-900, ppid=1, session=1177, mem=12 * 1024 * am.MiB)
        mudar = am.plan_protection([hypr, app], {}, {hypr.key})
        self.assertEqual([(p.pid, v, m) for p, v, m in mudar], [(5000, am.DEFAULT_RESTORE_ADJ, "inherited")])

    def test_child_seen_after_parent_reset(self):
        hypr = P(100, "Hyprland", exe_ok=True, adj=-900)
        kitty = P(200, "kitty", adj=-900, ppid=100)
        orig = {}
        am.plan_protection([hypr, kitty], orig, {hypr.key})
        kitty.adj = 0                                   # foi zerado na passada anterior
        bash = P(300, "bash", adj=-900, ppid=200)       # nasceu antes do zero, visto só agora
        mudar = am.plan_protection([hypr, kitty, bash], orig, {hypr.key, kitty.key})
        self.assertEqual([(p.pid, v) for p, v, m in mudar], [(300, 0)])

    def test_pending_python_waits(self):
        hypr = P(100, "Hyprland", exe_ok=True, adj=-900)
        py = P(300, "python3", adj=-900, ppid=100, pending=True)    # pode ser script do Aero
        self.assertEqual(am.plan_protection([hypr, py], {}, {hypr.key}), [])
        py.pending = False                              # lido: não é do Aero
        mudar = am.plan_protection([hypr, py], {}, {hypr.key})
        self.assertEqual([(p.pid, m) for p, v, m in mudar], [(300, "inherited")])

    def test_restore_plan(self):
        cfg = dict(am.DEFAULTS, protect=("obs",))
        obs = P(5, "obs", cfg, adj=-900)
        fundo = P(6, "fossilize_repla", adj=500)
        mexido = P(7, "steam", adj=100)                 # alguém mudou depois: o valor é dele
        pendente = P(8, "python3", adj=-900, pending=True)
        applied = {obs.key: (0, -900), fundo.key: (200, 500), mexido.key: (0, -900), pendente.key: (0, -900)}
        # ainda listados: nada a desfazer na passada normal
        self.assertEqual(am.plan_restore([obs, fundo, mexido, pendente], applied, tudo=False), [])
        # o nome saiu de protect= e de background=: volta ao original
        sem = dict(am.DEFAULTS, protect=(), background=())
        obs2, fundo2 = P(5, "obs", sem, adj=-900), P(6, "fossilize_repla", sem, adj=500)
        mudar = am.plan_restore([obs2, fundo2, mexido, pendente], applied, tudo=False)
        self.assertEqual(sorted((p.pid, v) for p, v, m in mudar), [(5, 0), (6, 200)])
        # parando/desligado: todos que ainda estão com o nosso valor
        mudar = am.plan_restore([obs, fundo, mexido, pendente], applied, tudo=True)
        self.assertEqual(sorted((p.pid, v) for p, v, m in mudar), [(5, 0), (6, 200), (8, 0)])

    def test_user_protect_list(self):
        cfg = dict(am.DEFAULTS, protect=("obs",))
        mudar = am.plan_protection([P(5, "obs", cfg, adj=0)], {}, set())
        self.assertEqual([(v, m) for _, v, m in mudar], [(-900, "protect")])

    def test_unknown_adj_skipped(self):
        self.assertEqual(am.plan_protection([P(5, "Hyprland", exe_ok=True, adj=None)], {}, set()), [])


class TestPausePlan(unittest.TestCase):
    def test_plan_pause(self):
        procs = [
            P(10, "fossilize_repla", state="S"),
            P(11, "fossilize_repla", state="R"),
            P(12, "fossilize_repla", state="T"),                      # já parado por alguém
            P(13, "fossilize_repla", uids=(0,) * 4),                  # root
            P(14, "fossilize_repla", state="Z"),
            P(15, "firefox"),
            P(16, "fossilize_repla"),
        ]
        self.assertEqual([p.pid for p in am.plan_pause(procs, {(16, 160)}, own_pid=999)], [10, 11])
        self.assertEqual([p.pid for p in am.plan_pause(procs, set(), own_pid=10)], [11, 16])


class TestVictim(unittest.TestCase):
    total = 32 * 1024 ** 3

    def test_memory_first(self):
        procs = [P(10, "firefox", mem=3000 * am.MiB, score=600, ppid=1),
                 P(11, "code", mem=4000 * am.MiB, score=500)]
        self.assertEqual(am.choose_victim(procs, self.total).pid, 11)

    def test_tie_highest_score(self):
        procs = [P(10, "a", mem=900 * am.MiB, score=400), P(11, "b", mem=900 * am.MiB, score=500)]
        self.assertEqual(am.choose_victim(procs, self.total).pid, 11)
        procs = [P(10, "a", mem=300 * am.MiB, score=500), P(11, "b", mem=900 * am.MiB, score=500)]
        self.assertEqual(am.choose_victim(procs, self.total).pid, 11)

    def test_electron_adj_does_not_win(self):
        # renderers do Electron com oom_score_adj 300 (oom_score alto) contra o gastão de verdade
        procs = [P(10, "claude-desktop", mem=885 * am.MiB, score=875),
                 P(11, "claude-desktop", mem=70 * am.MiB, score=868),
                 P(12, "antigravity", mem=243 * am.MiB, score=870),
                 P(13, "rustc", mem=14 * 1024 * am.MiB, score=813)]
        self.assertEqual(am.choose_victim(procs, self.total).pid, 13)

    def test_prefers_background_and_comfy(self):
        procs = [P(10, "firefox", mem=6000 * am.MiB, score=900),
                 P(11, "fossilize_repla", mem=700 * am.MiB, score=300),
                 P(12, "python", comfy=True, mem=5000 * am.MiB, score=800)]
        self.assertEqual(am.choose_victim(procs, self.total).pid, 12)
        procs[2].comfy = False
        self.assertEqual(am.choose_victim(procs, self.total).pid, 11)
        procs[1].mem = 100 * am.MiB                                   # pequeno: sem preferência
        self.assertEqual(am.choose_victim(procs, self.total).pid, 10)

    def test_never(self):
        hypr = P(50, "Hyprland", exe_ok=True, mem=9000 * am.MiB, score=900, ppid=40, session=30)
        ancestral = P(40, "start-hyprlan", mem=9000 * am.MiB, score=900, ppid=30, session=30)
        cfg = dict(am.DEFAULTS, protect=("obs",))
        procs = [
            hypr, ancestral,
            P(60, "obs", cfg, mem=9000 * am.MiB, score=999),                       # lista protect
            P(61, "root-thing", uids=(0,) * 4, mem=9000 * am.MiB, score=999),
            P(62, "bash", mem=9000 * am.MiB, score=999, session=62, tty=34816),     # shell de terminal
            P(30, "sessionld", mem=9000 * am.MiB, score=999, session=30),          # líder da sessão do compositor
            P(64, "zombie", state="Z", mem=9000 * am.MiB, score=999),
            P(65, "tiny", mem=10 * am.MiB, score=999),
            P(66, "waybar", exe_ok=True, mem=2000 * am.MiB, score=999),           # vital normal
            P(os.getpid(), "me", mem=9000 * am.MiB, score=999),
        ]
        self.assertIsNone(am.choose_victim(procs, self.total, os.getpid()))
        servico = P(70, "node-MainThrea", mem=500 * am.MiB, score=200, session=70, tty=0)
        self.assertEqual(am.choose_victim(procs + [servico], self.total, os.getpid()).pid, 70)

    def test_vital_hog(self):
        hog = P(66, "waybar", exe_ok=True, mem=14 * 1024 * am.MiB, score=400)
        outro = P(67, "firefox", mem=2000 * am.MiB, score=300)
        self.assertEqual(am.choose_victim([hog, outro], self.total).pid, 66)
        comp = P(68, "Hyprland", exe_ok=True, mem=20 * 1024 * am.MiB, score=999)
        self.assertEqual(am.choose_victim([comp, outro], self.total).pid, 67)   # compositor: nunca

    def test_empty(self):
        self.assertIsNone(am.choose_victim([], self.total))

    def test_kill_group(self):
        procs = [P(10 + i, "fossilize_repla", mem=700 * am.MiB, score=300) for i in range(7)]
        procs.append(P(30, "fossilize_repla", uids=(1001,) * 4, mem=700 * am.MiB))
        procs.append(P(31, "fossilize_repla", state="Z"))
        v = am.choose_victim(procs, self.total)
        g = am.kill_group(v, procs)
        self.assertEqual(g[0], v)
        self.assertEqual(sorted(p.pid for p in g), list(range(10, 17)))
        ff = P(5, "firefox")
        self.assertEqual(am.kill_group(ff, procs + [ff]), [ff])
        muitos = [P(100 + i, "fossilize_repla") for i in range(100)]
        self.assertEqual(len(am.kill_group(muitos[0], muitos)), am.MAX_KILL_GROUP)


class TestSmallPieces(unittest.TestCase):
    def test_tail_lines(self):
        dados = b"".join(b'{"n":%d}\n' % i for i in range(100))
        cauda = am.tail_lines(dados, 50)
        self.assertLessEqual(len(cauda), 50)
        self.assertTrue(cauda.endswith(b'{"n":99}\n'))
        for linha in cauda.splitlines():
            json.loads(linha)
        self.assertEqual(am.tail_lines(b"abc\n", 100), b"abc\n")
        self.assertEqual(am.tail_lines(b"x" * 100, 10), b"")

    def test_parse_paused(self):
        bom = json.dumps({"version": 1, "paused": [
            {"pid": 1234, "start": 99, "comm": "fossilize_repla", "uid": 1000},
            {"pid": True, "start": 1, "comm": "x", "uid": 1000},          # bool não é pid
            {"pid": 1, "start": 1, "comm": "init", "uid": 0},             # pid 1 nunca
            {"pid": 55, "start": -1, "comm": "x", "uid": 1000},
            {"pid": "7", "start": 1},
            "lixo",
            {"pid": 77, "start": 5, "comm": "a\nb" * 30, "uid": 1000},
        ]}).encode()
        r = am.parse_paused(bom)
        self.assertEqual([x["pid"] for x in r], [1234, 77])
        self.assertNotIn("\n", r[1]["comm"])
        self.assertLessEqual(len(r[1]["comm"]), 32)
        self.assertEqual(am.parse_paused(b"not json"), [])
        self.assertEqual(am.parse_paused(b"[1,2]"), [])
        muitos = json.dumps({"paused": [{"pid": 100 + i, "start": 1, "uid": 1000} for i in range(500)]}).encode()
        self.assertEqual(len(am.parse_paused(muitos)), am.MAX_PAUSED)

    def test_describe_error_has_no_data(self):
        try:
            raise FileNotFoundError(2, "No such file", HOME + "/secret/doc.txt")
        except OSError as e:
            texto = am.describe_error(e)
        self.assertIn("FileNotFoundError ENOENT", texto)
        self.assertNotIn("secret", texto)
        self.assertNotIn(HOME, texto)

    def test_log_one_line_and_bounded(self):
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            am.log(6, "a\nb\rc")
            for i in range(300):
                am.log_limited(f"k{i}", 60, 7, "x")
        self.assertEqual(buf.getvalue().count("\n"), 1)
        self.assertLessEqual(len(am._last_log), 257)

    def test_psi_trigger_strings(self):
        for gatilho, _ in am.PSI_TRIGGERS:
            self.assertTrue(gatilho.endswith(b"\0"))      # o kernel troca o último byte por \0
            tipo, stall, janela = gatilho[:-1].split()
            self.assertEqual(tipo, b"some")
            self.assertLess(int(stall), int(janela))
            self.assertTrue(500000 <= int(janela) <= 10000000)
        self.assertEqual(int(am.PSI_TRIGGERS[-1][0][:-1].split()[2]) % 2000000, 0)   # sem privilégio

    def test_psi_open_missing(self):
        fd, motivo = am.open_psi_trigger("/nonexistent/pressure/memory")
        self.assertIsNone(fd)
        self.assertIn("ENOENT", motivo)

    @unittest.skipUnless(os.path.exists("/proc/pressure/memory"), "kernel without PSI")
    def test_psi_open_real(self):
        fd, modo = am.open_psi_trigger(am.PSI_PATH)
        self.assertIsNotNone(fd, modo)
        try:
            if os.geteuid() != 0:
                self.assertIn("2 s", modo)
            p = select.poll()
            p.register(fd, select.POLLPRI)
            p.poll(0)
        finally:
            os.close(fd)

    def test_sd_notify(self):
        with tempfile.TemporaryDirectory() as d:
            caminho = os.path.join(d, "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
                s.bind(caminho)
                with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": caminho}):
                    am.sd_notify("READY=1")
                self.assertEqual(s.recv(100), b"READY=1")
        with mock.patch.dict(os.environ, {"NOTIFY_SOCKET": ""}):
            am.sd_notify("READY=1")         # sem socket: não faz nada


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "run")

    def tearDown(self):
        self.tmp.cleanup()

    def test_atomic_json_and_modes(self):
        st = am.Store(self.dir)
        self.assertTrue(st.ok)
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o755)
        self.assertTrue(st.write_json("state.json", {"a": 1}))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, "state.json")).st_mode), 0o644)
        self.assertEqual(sorted(os.listdir(self.dir)), ["state.json"])     # sem tmp sobrando
        st.close()

    def test_bad_dir_refused(self):
        os.mkdir(self.dir, 0o755)
        os.chmod(self.dir, 0o777)
        with silencio():
            st = am.Store(self.dir)
        self.assertFalse(st.ok)
        self.assertFalse(st.write_json("state.json", {}))
        os.chmod(self.dir, 0o755)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.dir, link)
        with silencio():
            self.assertFalse(am.Store(link).ok)

    def test_symlinks_not_followed(self):
        st = am.Store(self.dir)
        alvo = os.path.join(self.tmp.name, "victim")
        with open(alvo, "w") as f:
            f.write("keep")
        os.symlink(alvo, os.path.join(self.dir, "state.json"))
        os.symlink(alvo, os.path.join(self.dir, "events.jsonl"))
        os.symlink(alvo, os.path.join(self.dir, "paused.json"))
        with silencio():
            st.write_json("state.json", {"a": 1})
            st.append_event({"kind": "x"})
            self.assertEqual(st.load_paused(), [])
        with open(alvo) as f:
            self.assertEqual(f.read(), "keep")
        self.assertFalse(os.path.islink(os.path.join(self.dir, "state.json")))
        st.close()

    def test_events_rotation(self):
        st = am.Store(self.dir)
        for i in range(3000):
            st.append_event({"t": i, "seq": i + 1, "kind": "pressure", "level": "warning"})
        caminho = os.path.join(self.dir, "events.jsonl")
        tamanho = os.path.getsize(caminho)
        self.assertLessEqual(tamanho, am.EVENTS_MAX)
        with open(caminho, "rb") as f:
            linhas = f.read().splitlines()
        seqs = [json.loads(x)["seq"] for x in linhas]
        self.assertEqual(seqs[-1], 3000)
        self.assertEqual(seqs, list(range(seqs[0], 3001)))       # contínuo, só os mais novos
        self.assertEqual(st.last_seq(), 3000)
        st.close()

    def test_paused_roundtrip(self):
        st = am.Store(self.dir)
        regs = [{"pid": 4321, "start": 5, "comm": "fossilize_repla", "uid": 1000}]
        self.assertTrue(st.save_paused(regs))
        self.assertEqual(st.load_paused(), regs)
        st.save_paused([])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "paused.json")))
        st.close()


class TestScannerFakeProc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proc = os.path.join(self.tmp.name, "proc")
        os.mkdir(self.proc)
        # "sistema" falso: o dono dos programas é este usuário (trusted_uids) e a pasta é system_dirs
        self.sysdir = os.path.join(self.tmp.name, "usr")
        os.makedirs(os.path.join(self.sysdir, "bin"))
        self.exe = self.arquivo(os.path.join(self.sysdir, "bin", "Hyprland"), 0o755)
        self.py = self.arquivo(os.path.join(self.sysdir, "bin", "python3.14"), 0o755)
        # home falsa do uid 1000 com um script do shell do Aero, como o Aero instala
        self.home = os.path.join(self.tmp.name, "h", "u")
        self.shell = os.path.join(self.home, ".local", "share", "aero-shell")
        os.makedirs(self.shell)
        self.script = self.arquivo(os.path.join(self.shell, "dock.py"), 0o644)
        comfy_dir = os.path.join(self.tmp.name, "ComfyUI")
        os.mkdir(comfy_dir)
        fake_proc(self.proc, 2, "kthreadd", flags=0x208040, uids=(0,) * 4)
        fake_proc(self.proc, 100, "Hyprland", exe=self.exe, session=90, adj=-900)   # já protegido
        fake_proc(self.proc, 101, "python3", ppid=100, session=90, exe=self.py,
                  argv=["python3", "-I", self.script])
        fake_proc(self.proc, 102, "python", ppid=100, session=90, adj=0, rss_pages=500000, score=900,
                  argv=["/x/venv/bin/python", "main.py", "--port", "8188"], cwd=comfy_dir)
        fake_proc(self.proc, 103, "firefox", ppid=100, session=90, adj=-900, rss_pages=200000, score=700)
        fake_proc(self.proc, 104, "fossilize_repla", ppid=1, session=90, rss_pages=180000, score=650)
        os.makedirs(os.path.join(self.proc, "self"))          # nomes não numéricos são ignorados
        self.sc = self.scanner(am.Scanner(self.proc))

    def arquivo(self, caminho, modo):
        with open(caminho, "w") as f:
            f.write("#")
        os.chmod(caminho, modo)
        return caminho

    def scanner(self, sc):
        sc.trusted_uids = (os.getuid(),)
        sc.system_dirs = (self.sysdir + "/",)
        sc.homes = {1000: self.home}
        return sc

    def guard(self, run=None, dry=False):
        g = am.Guard("/nonexistent.conf", run, dry_run=dry, proc_root=self.proc)
        self.scanner(g.scanner)
        return g

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_classifies(self):
        procs = {p.pid: p for p in self.sc.scan(am.DEFAULTS, details=True, oom_score=True)}
        self.assertNotIn(2, procs)
        self.assertTrue(procs[100].vital and procs[100].never_kill)
        self.assertTrue(procs[101].aero and procs[101].vital)
        self.assertTrue(procs[102].comfy)
        self.assertFalse(procs[103].vital)
        self.assertTrue(procs[104].background)
        self.assertEqual(procs[102].mem, 500000 * os.sysconf("SC_PAGE_SIZE"))
        self.assertEqual(procs[103].score, 700)

    def test_untrusted_exe(self):
        os.chmod(self.exe, 0o777)
        procs = {p.pid: p for p in self.sc.scan(am.DEFAULTS)}
        self.assertFalse(procs[100].vital)
        self.assertIs(procs[100].exe_ok, False)

    def test_renamed_program_is_not_vital(self):
        # link "Hyprland" pra outro programa do sistema: o comm bate, o arquivo de verdade não (B2)
        sleep = self.arquivo(os.path.join(self.sysdir, "bin", "sleep"), 0o755)
        fake_proc(self.proc, 110, "Hyprland", exe=sleep)
        p = {p.pid: p for p in self.sc.scan(am.DEFAULTS)}[110]
        self.assertEqual((p.exe_ok, p.exe_name, p.vital, p.never_kill), (False, False, False, False))

    def aero(self, pid, argv, exe=None, cwd=None, **kw):
        fake_proc(self.proc, pid, "python3", exe=exe or self.py, argv=argv, cwd=cwd, **kw)
        return {p.pid: p for p in self.sc.scan(am.DEFAULTS, details=True)}[pid]

    def test_aero_script_checks(self):
        # o jeito real: /usr/bin/python3.14 com o caminho absoluto do script na home do dono
        self.assertTrue(self.aero(120, ["python3", self.script, "--daemon"]).vital)
        # caminho relativo, a partir do cwd do processo
        self.assertTrue(self.aero(121, ["python3", "dock.py"], cwd=self.shell).aero)
        # "/aero-shell/" em qualquer outro lugar não vale
        falso = os.path.join(self.tmp.name, "x", "aero-shell")
        os.makedirs(falso)
        hog = self.arquivo(os.path.join(falso, "hog.py"), 0o644)
        p = self.aero(122, ["/usr/bin/python3", hog])
        self.assertFalse(p.aero or p.vital)
        # python que não é o do sistema (cópia na home)
        meu_py = self.arquivo(os.path.join(self.home, "python3"), 0o755)
        self.assertFalse(self.aero(123, ["python3", self.script], exe=meu_py).aero)
        # script que outros podem alterar, script que não existe, home de outro usuário
        aberto = self.arquivo(os.path.join(self.shell, "open.py"), 0o666)
        self.assertFalse(self.aero(124, ["python3", aberto]).aero)
        self.assertFalse(self.aero(125, ["python3", os.path.join(self.shell, "gone.py")]).aero)
        self.assertFalse(self.aero(126, ["python3", self.script], uids=(1001,) * 4).aero)
        # "../" pra fora da pasta do Aero
        self.assertFalse(self.aero(127, ["python3", os.path.join(self.shell, "..", "..", "x.py")]).aero)
        # script do Aero gastão: é do Aero, mas não ganha a proteção
        big = self.aero(128, ["python3", self.script], rss_pages=am.AERO_MAX_MEM // os.sysconf("SC_PAGE_SIZE") + 10)
        self.assertTrue(big.aero)
        self.assertFalse(big.vital)

    def test_aero_agent_location(self):
        bindir = os.path.join(self.home, ".local", "bin")
        os.makedirs(bindir)
        agente = self.arquivo(os.path.join(bindir, "aero-memory-agent"), 0o755)
        self.assertTrue(self.aero(130, ["python3", agente]).aero)
        outro = self.arquivo(os.path.join(self.tmp.name, "aero-memory-agent"), 0o755)
        self.assertFalse(self.aero(131, ["python3", outro]).aero)

    def test_details_false_and_cache(self):
        procs = {p.pid: p for p in self.sc.scan(am.DEFAULTS, details=False)}
        self.assertFalse(procs[102].comfy)                     # sem ler cmdline no caminho crítico
        self.assertNotIn((102, 1020, "python"), self.sc.cache)
        self.sc.scan(am.DEFAULTS, details=True)
        self.assertIn((102, 1020, "python"), self.sc.cache)
        procs = {p.pid: p for p in self.sc.scan(am.DEFAULTS, details=False)}
        self.assertTrue(procs[102].comfy)                      # agora vem do cache
        import shutil
        shutil.rmtree(os.path.join(self.proc, "102"))
        self.sc.scan(am.DEFAULTS)
        self.assertNotIn((102, 1020, "python"), self.sc.cache)  # poda

    def test_only_filter(self):
        procs = self.sc.scan(am.DEFAULTS, only=lambda c: c == "firefox")
        self.assertEqual([p.pid for p in procs], [103])

    def test_victim_on_fake_tree(self):
        procs = self.sc.scan(am.DEFAULTS, details=True, oom_score=True)
        v = am.choose_victim(procs, 32 * 1024 ** 3)
        self.assertEqual(v.pid, 102)                           # ComfyUI grande vem primeiro

    def test_protect_pass_writes(self):
        g = self.guard()
        with silencio():
            g.protect_pass()
        self.assertEqual(ler_adj(self.proc, 100), -900)
        self.assertEqual(ler_adj(self.proc, 101), -900)
        self.assertEqual(ler_adj(self.proc, 102), 500)
        self.assertEqual(ler_adj(self.proc, 103), 0)          # herdou -900 do Hyprland: voltou
        self.assertEqual(ler_adj(self.proc, 104), 500)
        g.store.close()

    def test_protect_pass_dry(self):
        g = self.guard(dry=True)
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            g.protect_pass()
            g.protect_pass()
        self.assertEqual(ler_adj(self.proc, 101), 0)
        self.assertEqual(ler_adj(self.proc, 103), -900)
        self.assertEqual(buf.getvalue().count("would set oom_score_adj 0 -> -900 on python3 (101)"), 1)
        self.assertEqual(buf.getvalue().count("on firefox (103) [inherited]"), 1)

    def test_disabled_undoes_protection(self):
        # B-3: o guarda desligado devolve os valores originais e zera o -900 herdado de quem nasceu
        # antes; daí em diante um app novo nasce com o valor normal do Hyprland
        run = os.path.join(self.tmp.name, "run")
        g = self.guard(run)
        with silencio():
            g.protect_pass()
        self.assertEqual([ler_adj(self.proc, n) for n in (100, 101, 102, 103, 104)], [-900, -900, 500, 0, 500])
        self.assertTrue(os.path.exists(os.path.join(run, "adj.json")))
        fake_proc(self.proc, 105, "kitty", ppid=100, session=90, adj=-900)     # aberto com o guarda ligado
        g.cfg = dict(am.DEFAULTS, enabled=False)
        g.last_protect = -1e9
        with mock.patch.object(g, "read_readings", return_value=leituras()), silencio():
            g.tick(100.0)
        self.assertEqual([ler_adj(self.proc, n) for n in (100, 101, 102, 103, 104, 105)], [0] * 6)
        self.assertEqual(g.applied, {})
        self.assertFalse(os.path.exists(os.path.join(run, "adj.json")))
        g.store.close()

    def test_unlisted_name_is_reverted(self):
        g = self.guard()
        fake_proc(self.proc, 106, "obs", ppid=1, session=106, adj=0)
        g.cfg = dict(am.DEFAULTS, protect=("obs",))
        with silencio():
            g.protect_pass()
        self.assertEqual(ler_adj(self.proc, 106), -900)
        g.cfg = dict(am.DEFAULTS, protect=(), background=())
        with silencio():
            g.protect_pass()
        self.assertEqual(ler_adj(self.proc, 106), 0)              # saiu de protect=: volta ao original
        self.assertEqual(ler_adj(self.proc, 104), 0)              # saiu de background=: idem
        self.assertEqual(ler_adj(self.proc, 100), -900)
        g.store.close()

    def test_critical_scan_protects(self):
        # no crítico a passada normal espera, mas a varredura do crítico zera o -900 herdado e
        # protege o desktop (guarda que acabou de reiniciar no aperto)
        fake_proc(self.proc, 107, "Hyprland", exe=self.exe, adj=0)
        fake_proc(self.proc, 108, "electron", ppid=1, session=90, adj=-900, rss_pages=300000)
        g = self.guard()
        g.cfg = dict(am.DEFAULTS, kill_on_critical=False)
        g.critical_since = 0.0
        with silencio():
            g.critical_actions(100.0, leituras(disp_gb=0.5, swap_livre_gb=0))
        self.assertEqual(ler_adj(self.proc, 107), -900)
        self.assertEqual(ler_adj(self.proc, 108), 0)
        g.store.close()

    def test_stop_post_undoes_leftover_adj(self):
        # ExecStopPost depois de um SIGKILL: adj.json ficou, o -900 é desfeito por ele
        run = os.path.join(self.tmp.name, "run")
        st = am.Store(run)
        with open(os.path.join(self.proc, "101", "oom_score_adj"), "w") as f:
            f.write("-900\n")
        with open(os.path.join(self.proc, "104", "oom_score_adj"), "w") as f:
            f.write("200\n")                                   # outro mudou depois: fica
        st.save_adj({(100, 1000): (0, -900), (101, 1010): (0, -900), (104, 1040): (0, 500),
                     (999, 9990): (0, -900)})
        st.close()
        with silencio():
            self.assertEqual(am.run_resume_paused(run, proc_root=self.proc), 0)
        self.assertEqual([ler_adj(self.proc, n) for n in (100, 101, 103, 104)], [0, 0, 0, 200])
        self.assertFalse(os.path.exists(os.path.join(run, "adj.json")))
        # sem adj.json (parada limpa): não mexe em nada
        with open(os.path.join(self.proc, "103", "oom_score_adj"), "w") as f:
            f.write("-900\n")
        with silencio():
            am.run_resume_paused(run, proc_root=self.proc)
        self.assertEqual(ler_adj(self.proc, 103), -900)

    def test_write_adj_wrong_start(self):
        self.assertFalse(am.write_adj(101, 999, -900, self.proc))
        self.assertFalse(am.write_adj(555, 5550, -900, self.proc))
        self.assertEqual(ler_adj(self.proc, 101), 0)
        self.assertTrue(am.write_adj(101, 1010, -900, self.proc))
        self.assertTrue(am.write_adj(101, 1010, 5, self.proc))       # O_TRUNC: sem sobra do valor antigo
        self.assertEqual(ler_adj(self.proc, 101), 5)


@unittest.skipUnless(am.is_user((os.getuid(),) * 4), "needs a normal user account (uid 1000..60000)")
class TestRealProcesses(unittest.TestCase):
    """Só com filhos deste teste (comm único)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.tmp.name, "run")
        self.filhos = []

    def tearDown(self):
        for p in self.filhos:
            matar(p)
        self.tmp.cleanup()

    def novo(self, comm=None, **kw):
        p = filho(comm or comm_unico(), **kw)
        self.filhos.append(p)
        return p

    def test_open_verified(self):
        p = self.novo()
        ini = estado(p.pid).starttime
        fd = am.open_verified(p.pid, ini)
        self.assertIsNotNone(fd)
        os.close(fd)
        self.assertIsNone(am.open_verified(p.pid, ini + 1))     # reuso de pid: outro início
        matar(p)
        self.filhos.remove(p)
        self.assertIsNone(am.open_verified(p.pid, ini))

    def test_write_adj_real(self):
        p = self.novo()
        ini = estado(p.pid).starttime
        self.assertTrue(am.write_adj(p.pid, ini, 500))
        with open(f"/proc/{p.pid}/oom_score_adj") as f:
            self.assertEqual(f.read().strip(), "500")
        self.assertFalse(am.write_adj(p.pid, ini + 1, 600))
        if os.geteuid() != 0:
            with self.assertRaises(PermissionError):           # baixar exige CAP_SYS_RESOURCE
                am.write_adj(p.pid, ini, -900)

    def guard(self, comm, dry=False):
        g = am.Guard("/nonexistent.conf", self.run, dry_run=dry)
        g.cfg = dict(am.DEFAULTS, background=(comm,))
        g.level = am.WARNING
        return g

    def test_pause_resume(self):
        comm = comm_unico()
        p1, p2 = self.novo(comm), self.novo(comm)
        g = self.guard(comm)
        with silencio():
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
        self.assertTrue(esperar_estado(p1.pid, "T") and esperar_estado(p2.pid, "T"))
        with open(os.path.join(self.run, "paused.json"), "rb") as f:
            regs = am.parse_paused(f.read())
        self.assertEqual(sorted(r["pid"] for r in regs), sorted([p1.pid, p2.pid]))
        with silencio():
            g.resume_all("test")
        self.assertTrue(esperar_estado(p1.pid, "S") and esperar_estado(p2.pid, "S"))
        self.assertFalse(os.path.exists(os.path.join(self.run, "paused.json")))
        ev = self.eventos()
        self.assertEqual([e["kind"] for e in ev], ["pressure", "paused", "resumed"])
        # paused/resumed dizem de quem são os processos (cada agente só avisa os do seu usuário)
        self.assertEqual((ev[1]["uid"], ev[1]["count"], ev[1]["names"]), (os.getuid(), 2, [comm]))
        self.assertEqual((ev[2]["uid"], ev[2]["count"], ev[2]["reason"]), (os.getuid(), 2, "calm"))
        self.assertNotIn("uid", ev[0])                                 # pressure é do sistema todo
        self.assertEqual(g.last_action["uid"], os.getuid())
        g.store.close()

    def eventos(self):
        with open(os.path.join(self.run, "events.jsonl")) as f:
            return [json.loads(x) for x in f]

    def test_shutdown_resumes(self):
        comm = comm_unico()
        p = self.novo(comm)
        g = self.guard(comm)
        with silencio():
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
            self.assertTrue(esperar_estado(p.pid, "T"))
            g.shutdown()
        self.assertTrue(esperar_estado(p.pid, "S"))
        # parar o serviço não é "a memória voltou ao normal"
        self.assertEqual(self.eventos()[-1]["reason"], "stopping")

    def test_reload_keeps_wanted_pauses(self):
        comm, outro = comm_unico(), comm_unico()
        p1, p2 = self.novo(comm), self.novo(outro)
        conf = os.path.join(self.tmp.name, "memory.conf")
        with open(conf, "w") as f:
            f.write(f"background={comm},{outro}\n")
        g = self.guard(comm)
        g.conf_path = conf
        with silencio():
            g.load_config(primeira=True)
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
        self.assertTrue(esperar_estado(p1.pid, "T") and esperar_estado(p2.pid, "T"))
        with open(conf, "w") as f:
            f.write(f"background={comm}\n")                          # outro saiu da lista
        with silencio():
            g.load_config()
        self.assertTrue(esperar_estado(p2.pid, "S"))
        time.sleep(0.1)
        self.assertEqual(estado(p1.pid).state, "T")                    # ainda vale: continua pausado
        self.assertEqual([x["pid"] for x in g.paused], [p1.pid])
        ev = self.eventos()[-1]
        self.assertEqual((ev["kind"], ev["reason"], ev["names"], ev["count"]), ("resumed", "reload", [outro], 1))
        with silencio():
            g.shutdown()
        self.assertTrue(esperar_estado(p1.pid, "S"))

    def test_dead_paused_are_forgotten(self):
        comm = comm_unico()
        p1, p2 = self.novo(comm), self.novo(comm)
        g = self.guard(comm)
        with silencio():
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
        self.assertTrue(esperar_estado(p1.pid, "T") and esperar_estado(p2.pid, "T"))
        matar(p2)                                   # o Steam fecha um worker parado
        self.filhos.remove(p2)
        g.last_pause_scan = -1e9
        with silencio():
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
        self.assertEqual([x["pid"] for x in g.paused], [p1.pid])
        with open(os.path.join(self.run, "paused.json"), "rb") as f:
            self.assertEqual([r["pid"] for r in am.parse_paused(f.read())], [p1.pid])
        with silencio():
            g.write_state(leituras(disp_gb=1))
        with open(os.path.join(self.run, "state.json")) as f:
            self.assertEqual(json.load(f)["paused_count"], 1)
        with silencio():
            g.resume_all("test")
        self.assertTrue(esperar_estado(p1.pid, "S"))

    def test_pause_survives_unexpected_error(self):
        # B-6: um erro inesperado (MemoryError...) logo depois de um SIGSTOP não deixa o processo parado
        # fora da lista: o resume_all retoma ele
        comm = comm_unico()
        filhos = [self.novo(comm) for _ in range(3)]
        g = self.guard(comm)
        real, chamadas = am.send, []

        def send(fd, sig):
            chamadas.append(sig)
            ok = real(fd, sig)
            if len(chamadas) == 2:
                raise MemoryError()             # o sinal já foi: o processo está parado
            return ok
        with mock.patch.object(am, "send", side_effect=send), silencio():
            g.safe(g.pressure_actions, time.monotonic(), leituras(disp_gb=1))
        parados = sorted(p.pid for p in filhos if esperar_estado(p.pid, "T", 0.5))
        self.assertEqual(len(parados), 2)
        self.assertEqual(sorted(x["pid"] for x in g.paused), parados)
        with silencio():
            g.resume_all("test")
        for p in filhos:
            self.assertTrue(esperar_estado(p.pid, "S"))
        self.assertEqual(g.paused, [])
        g.store.close()

    def test_events_use_full_names(self):
        # BC2: o kernel corta o comm em 15; eventos e state.json levam o nome inteiro da lista
        # background= (o que a notificação e o Settings mostram) e o state.json diz de quem são
        comm = (comm_unico() + "xyz")[:15]
        longo = comm + "_replay"
        p = self.novo(comm)
        self.assertEqual(estado(p.pid).comm, comm)
        g = self.guard(longo)
        with silencio():
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
            self.assertTrue(esperar_estado(p.pid, "T"))
            g.write_state(leituras(disp_gb=1))
            g.resume_all("test")
        self.assertTrue(esperar_estado(p.pid, "S"))
        ev = self.eventos()
        self.assertEqual([(e["kind"], e.get("names")) for e in ev[1:]], [("paused", [longo]), ("resumed", [longo])])
        with open(os.path.join(self.run, "state.json")) as f:
            est = json.load(f)
        self.assertEqual((est["paused"], est["paused_uids"], est["paused_count"]), ([longo], [os.getuid()], 1))
        # o Settings só aceita a lista se for de int de verdade (nada de float, texto ou bool)
        self.assertEqual([type(u) for u in est["paused_uids"]], [int])
        self.assertFalse(os.path.exists(os.path.join(self.run, "paused.json")))
        g.store.close()

    def test_leftovers_and_pid_reuse(self):
        comm = comm_unico()
        bom, outro, solto = self.novo(comm), self.novo(comm_unico()), self.novo(comm)
        for p in (bom, outro, solto):
            os.kill(p.pid, signal.SIGSTOP)
            self.assertTrue(esperar_estado(p.pid, "T"))
        st = am.Store(self.run)
        st.save_paused([
            {"pid": bom.pid, "start": estado(bom.pid).starttime, "comm": comm, "uid": os.getuid()},
            {"pid": outro.pid, "start": estado(outro.pid).starttime + 7, "comm": "x", "uid": os.getuid()},
        ])
        st.close()
        g = self.guard(comm)
        with silencio():
            g.resume_leftovers()
        self.assertTrue(esperar_estado(bom.pid, "S"))
        self.assertTrue(esperar_estado(solto.pid, "S"))         # parado sem registro, comm de fundo
        time.sleep(0.2)
        self.assertEqual(estado(outro.pid).state, "T")          # início diferente: não mexe
        self.assertFalse(os.path.exists(os.path.join(self.run, "paused.json")))
        g.store.close()

    def test_resume_paused_cli(self):
        p = self.novo()
        os.kill(p.pid, signal.SIGSTOP)
        self.assertTrue(esperar_estado(p.pid, "T"))
        st = am.Store(self.run)
        st.save_paused([{"pid": p.pid, "start": estado(p.pid).starttime, "comm": "x", "uid": os.getuid()}])
        st.close()
        r = subprocess.run([sys.executable, "-I", "-B", DAEMON, "--resume-paused", "--run-dir", self.run],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(esperar_estado(p.pid, "S"))
        self.assertFalse(os.path.exists(os.path.join(self.run, "paused.json")))
        r = subprocess.run([sys.executable, "-I", "-B", DAEMON, "--resume-paused", "--run-dir",
                            os.path.join(self.tmp.name, "nope")], capture_output=True, timeout=20)
        self.assertEqual(r.returncode, 0)

    def test_kill_procs(self):
        educado, teimoso, parado = self.novo(), self.novo(ignorar_term=True), self.novo()
        os.kill(parado.pid, signal.SIGSTOP)
        self.assertTrue(esperar_estado(parado.pid, "T"))
        grupo = [P(x.pid, "x", start=estado(x.pid).starttime) for x in (educado, teimoso, parado)]
        grupo.append(P(educado.pid, "x", start=1))                  # registro velho: ignorado
        g = am.Guard("/nonexistent.conf", None)
        t0 = time.monotonic()
        fechados = g.kill_procs(grupo, 0.6)
        self.assertEqual(len(fechados), 3)
        self.assertEqual(educado.wait(timeout=5), -signal.SIGTERM)
        self.assertEqual(parado.wait(timeout=5), -signal.SIGTERM)  # parado: TERM + CONT
        self.assertEqual(teimoso.wait(timeout=5), -signal.SIGKILL)
        self.assertLess(time.monotonic() - t0, 5)

    def test_dry_run_never_signals(self):
        comm = comm_unico()
        p = self.novo(comm)
        g = self.guard(comm, dry=True)
        g.critical_since = time.monotonic() - 10
        proibido = mock.Mock(side_effect=AssertionError("signal in dry-run"))
        buf = io.StringIO()
        with mock.patch.object(am.signal, "pidfd_send_signal", proibido), \
                mock.patch.object(am.os, "kill", proibido), contextlib.redirect_stderr(buf):
            g.pressure_actions(time.monotonic(), leituras(disp_gb=1))
            g.critical_actions(time.monotonic(), leituras(disp_gb=0.5, swap_livre_gb=0))
            g.resume_all("x")
        proibido.assert_not_called()
        self.assertEqual(estado(p.pid).state, "S")
        self.assertIn("dry-run: would pause 1", buf.getvalue())
        self.assertNotIn(HOME, buf.getvalue())
        self.assertFalse(os.path.exists(os.path.join(self.run, "paused.json")))
        g.store.close()


class TestDaemonProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.tmp.name, "run")
        self.conf = os.path.join(self.tmp.name, "memory.conf")
        with open(self.conf, "w") as f:
            f.write("background=" + comm_unico() + "\n")

    def tearDown(self):
        self.tmp.cleanup()

    def test_check(self):
        r = subprocess.run([sys.executable, "-I", "-B", DAEMON, "--check", "--config", self.conf,
                            "--run-dir", self.run], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Level now", r.stdout)
        self.assertIn("If critical", r.stdout)
        if os.path.exists("/proc/vmstat"):
            self.assertIn("OOM kills", r.stdout)
        self.assertNotIn(HOME, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.run))              # --check não cria nada

    @staticmethod
    def fds_estavel(pid):
        """Menor contagem de fds em 0,5 s (no meio de uma varredura há 2 fds a mais, de passagem)."""
        menor = None
        for _ in range(10):
            n = len(os.listdir(f"/proc/{pid}/fd"))
            menor = n if menor is None else min(menor, n)
            time.sleep(0.05)
        return menor

    @staticmethod
    def campo(pid, nome):
        with open(f"/proc/{pid}/status") as f:
            for linha in f:
                if linha.startswith(nome + ":"):
                    return int(linha.split()[1])

    def test_dry_run_loop(self):
        err = open(os.path.join(self.tmp.name, "err.log"), "w+")
        p = subprocess.Popen([sys.executable, "-I", "-B", DAEMON, "--dry-run", "--run-dir", self.run,
                              "--config", self.conf], stderr=err, stdout=subprocess.DEVNULL,
                             stdin=subprocess.DEVNULL)
        try:
            time.sleep(1.5)
            fds1 = self.fds_estavel(p.pid)
            self.assertEqual(self.campo(p.pid, "Threads"), 1)
            os.kill(p.pid, signal.SIGHUP)
            time.sleep(3.0)
            fds2 = self.fds_estavel(p.pid)
            self.assertEqual(fds1, fds2)
            self.assertEqual(self.campo(p.pid, "Threads"), 1)
            self.assertLess(self.campo(p.pid, "VmRSS"), 64 * 1024)     # kB
            with open(os.path.join(self.run, "state.json")) as f:
                est = json.load(f)
            self.assertIn(est["level"], ("normal", "warning", "critical"))
            self.assertTrue(est["dry_run"])
            self.assertLessEqual(abs(est["t"] - time.time()), 5)
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.run, "state.json")).st_mode), 0o644)
            self.assertNotIn(HOME, json.dumps(est))
            t0 = time.monotonic()
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=5), 0)
            self.assertLess(time.monotonic() - t0, 1.0)              # o sinal acorda o poll na hora
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            err.seek(0)
            log = err.read()
            err.close()
        self.assertIn("started (dry-run", log)
        self.assertIn("config reloaded", log)
        self.assertIn("stopped", log)
        self.assertNotIn("internal error", log)
        self.assertNotIn("Traceback", log)
        self.assertNotIn(HOME, log)

    def test_dry_run_refuses_live_run_dir(self):
        # C-4: um ensaio nunca escreve na pasta que o agente e o Settings leem (nem por um link)
        link = os.path.join(self.tmp.name, "link")
        os.symlink(am.RUN_DIR, link)
        existia = os.path.exists(am.RUN_DIR)
        for pasta in (am.RUN_DIR, am.RUN_DIR + "/", link):
            r = subprocess.run([sys.executable, "-I", "-B", DAEMON, "--dry-run", "--run-dir", pasta,
                                "--config", self.conf], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("--dry-run can't write into the live", r.stderr)
        self.assertEqual(os.path.exists(am.RUN_DIR), existia)

    def test_dry_run_without_run_dir_writes_nothing(self):
        p = subprocess.Popen([sys.executable, "-I", "-B", DAEMON, "--dry-run", "--config", self.conf],
                             stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, cwd=self.tmp.name)
        time.sleep(1.0)
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=5)
        self.assertEqual(p.returncode, 0, err)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["memory.conf"])


class TestGuardLogic(unittest.TestCase):
    """O laço e o tick com leituras falsas (dry-run, sem /run de verdade)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run = os.path.join(self.tmp.name, "run")

    def tearDown(self):
        self.tmp.cleanup()

    def guard(self, **cfg):
        g = am.Guard("/nonexistent.conf", self.run, dry_run=True)
        g.cfg = dict(am.DEFAULTS, background=(comm_unico(),), **cfg)
        g.last_protect = time.monotonic() + 1e6          # sem passada de proteção aqui
        return g

    def test_tick_flow(self):
        g = self.guard()
        r = leituras(disp_gb=1, swap_livre_gb=2)
        with mock.patch.object(g, "read_readings", return_value=r), silencio():
            g.tick(100.0)
        self.assertEqual(g.level, "warning")
        with open(os.path.join(self.run, "events.jsonl")) as f:
            ev = [json.loads(x) for x in f]
        self.assertEqual([e["kind"] for e in ev], ["pressure"])
        self.assertTrue(ev[0]["dry_run"])
        with open(os.path.join(self.run, "state.json")) as f:
            est = json.load(f)
        self.assertEqual(est["level"], "warning")
        self.assertEqual(est["config"]["background"], list(g.cfg["background"]))
        # calmo de novo: só volta ao normal depois de 30 s
        with mock.patch.object(g, "read_readings", return_value=leituras()), silencio():
            g.tick(101.0)
            self.assertEqual(g.level, "warning")
            g.tick(131.5)
        self.assertEqual(g.level, "normal")
        g.store.close()

    def test_disabled_does_nothing(self):
        g = self.guard(enabled=False)
        acoes = mock.Mock()
        with mock.patch.object(g, "read_readings", return_value=leituras(disp_gb=0.5, swap_livre_gb=0)), \
                mock.patch.object(g, "pressure_actions", acoes), mock.patch.object(g, "critical_actions", acoes), \
                mock.patch.object(g, "protect_pass", acoes), silencio():
            g.tick(100.0)
        self.assertEqual(g.level, "critical")
        acoes.assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.run, "events.jsonl")))
        g.store.close()

    def test_critical_skips_protect_pass(self):
        g = self.guard()
        g.last_protect = -1e9
        with mock.patch.object(g, "read_readings", return_value=leituras(disp_gb=0.5, swap_livre_gb=0)), \
                mock.patch.object(g, "protect_pass") as prot, mock.patch.object(g, "critical_actions") as crit, \
                silencio():
            g.tick(100.0)
        prot.assert_not_called()
        crit.assert_called_once()
        g.store.close()

    def test_errors_never_kill_the_loop(self):
        g = self.guard()
        g._signals()
        try:
            buf = io.StringIO()
            with mock.patch.object(g, "read_readings", side_effect=RuntimeError("boom " + HOME)), \
                    contextlib.redirect_stderr(buf):
                g.next_tick = 0
                g.iteration()                               # o tick quebra; a volta não
                chamadas = []

                def volta():
                    chamadas.append(1)
                    if len(chamadas) == 1:
                        raise MemoryError()
                    if len(chamadas) == 2:
                        raise KeyError("x")
                    g.stop = True                           # terceira volta: para

                g.stop = False
                with mock.patch.object(g, "iteration", side_effect=volta), \
                        mock.patch.object(am.time, "sleep", return_value=None):
                    g.loop()
                self.assertEqual(len(chamadas), 3)
            self.assertIn("internal error", buf.getvalue())
            self.assertNotIn(HOME, buf.getvalue())
            self.assertNotIn("boom", buf.getvalue())
        finally:
            with silencio():
                g.shutdown()
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGHUP, signal.SIG_DFL)

    def test_killed_event_has_owner(self):
        g = am.Guard("/nonexistent.conf", self.run, dry_run=False)
        vitima = P(4242, "rustc", mem=14 * 1024 * am.MiB, uids=(1001,) * 4)
        g.critical_since = 0.0
        with mock.patch.object(g.scanner, "scan", return_value=[vitima]), \
                mock.patch.object(g, "protect_procs") as prot, \
                mock.patch.object(g, "kill_procs", return_value=[vitima]) as kill, silencio():
            g.critical_actions(100.0, leituras(disp_gb=0.5, swap_livre_gb=0))
        prot.assert_called_once_with([vitima])
        kill.assert_called_once()
        with open(os.path.join(self.run, "events.jsonl")) as f:
            ev = [json.loads(x) for x in f]
        self.assertEqual([(e["kind"], e["uid"], e["name"], e["pid"]) for e in ev], [("killed", 1001, "rustc", 4242)])
        self.assertEqual(g.last_action["uid"], 1001)
        g.store.close()

    def test_killed_event_names(self):
        # BC2: trabalho de fundo com o nome inteiro da lista; o backend do ComfyUI como "ComfyUI"
        casos = [(P(4242, "fossilize_repla", mem=6 * 1024 * am.MiB), "fossilize_replay"),
                 (P(4243, "python3", mem=9 * 1024 * am.MiB, comfy=True), "ComfyUI"),
                 (P(4244, "rustc", mem=9 * 1024 * am.MiB), "rustc")]
        for i, (vitima, nome) in enumerate(casos):
            run = os.path.join(self.tmp.name, f"run{i}")
            g = am.Guard("/nonexistent.conf", run, dry_run=False)
            g.critical_since = 0.0
            with mock.patch.object(g.scanner, "scan", return_value=[vitima]), \
                    mock.patch.object(g, "protect_procs"), \
                    mock.patch.object(g, "kill_procs", return_value=[vitima]), silencio():
                g.critical_actions(100.0, leituras(disp_gb=0.5, swap_livre_gb=0))
            with open(os.path.join(run, "events.jsonl")) as f:
                ev = [json.loads(x) for x in f]
            self.assertEqual([(e["kind"], e["name"]) for e in ev], [("killed", nome)])
            g.store.close()

    def test_oom_kill_event(self):
        # X3: o OOM killer do kernel fechou algo -> evento oom_kill {count} e last_action, mesmo com
        # o guarda desligado; a primeira leitura só marca a base
        proc = os.path.join(self.tmp.name, "proc")
        os.mkdir(proc)
        vmstat = os.path.join(proc, "vmstat")

        def contador(n):
            with open(vmstat, "w") as f:
                f.write(f"nr_free_pages 1\noom_kill {n}\npswpin 0\n")
        contador(7)
        g = am.Guard("/nonexistent.conf", self.run, dry_run=False, proc_root=proc)
        g.cfg = dict(am.DEFAULTS, enabled=False)
        buf = io.StringIO()
        with mock.patch.object(g, "read_readings", return_value=leituras()), contextlib.redirect_stderr(buf):
            g.tick(100.0)
            self.assertFalse(os.path.exists(os.path.join(self.run, "events.jsonl")))     # base, sem aviso
            contador(9)
            g.tick(102.0)
            g.tick(104.0)                                                              # sem mudança
            os.unlink(vmstat)
            g.tick(106.0)                                                              # sumiu: nada
            contador(10)
            g.tick(108.0)
        with open(os.path.join(self.run, "events.jsonl")) as f:
            ev = [json.loads(x) for x in f]
        self.assertEqual([(e["kind"], e["count"]) for e in ev], [("oom_kill", 2), ("oom_kill", 1)])
        self.assertTrue(all("uid" not in e and "name" not in e for e in ev))
        with open(os.path.join(self.run, "state.json")) as f:
            self.assertEqual(json.load(f)["last_action"]["kind"], "oom_kill")
        self.assertIn("out-of-memory killer closed 2", buf.getvalue())
        self.assertNotIn("internal error", buf.getvalue())
        g.store.close()

    def test_by_uid(self):
        regs = [{"uid": 1001, "n": 1}, {"uid": 1000, "n": 2}, {"uid": 1001, "n": 3}]
        self.assertEqual([(u, [r["n"] for r in rs]) for u, rs in am.by_uid(regs)], [(1000, [2]), (1001, [1, 3])])

    def test_paused_file_is_private(self):
        st = am.Store(self.run)
        st.save_paused([{"pid": 4321, "start": 5, "comm": "x", "uid": 1000}])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.run, "paused.json")).st_mode), 0o600)
        st.close()

    def test_journal_log_never_blocks(self):
        # stderr = socket que ninguém lê (journal engasgado): o log perde linhas mas não trava
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        st = os.fstat(b.fileno())
        codigo = ("import importlib.machinery, importlib.util, sys, time\n"
                  "sys.dont_write_bytecode = True\n"
                  f"l = importlib.machinery.SourceFileLoader('am', {DAEMON!r})\n"
                  "m = importlib.util.module_from_spec(importlib.util.spec_from_loader('am', l))\n"
                  "l.exec_module(m)\n"
                  "assert m._JOURNAL\n"
                  "m.nonblocking_journal()\n"
                  "for i in range(100000):\n"
                  "    m.log(6, 'x' * 200)\n"
                  "print('done')\n")
        try:
            r = subprocess.run([sys.executable, "-I", "-c", codigo], stderr=b.fileno(), stdout=subprocess.PIPE,
                               text=True, timeout=30, env=dict(os.environ, JOURNAL_STREAM=f"{st.st_dev}:{st.st_ino}"))
            self.assertEqual((r.returncode, r.stdout.strip()), (0, "done"))
            self.assertTrue(a.recv(16).startswith(b"<6>xxx"))
        finally:
            a.close()
            b.close()
        # JOURNAL_STREAM de outro arquivo: não é o journal (nada de prefixo nem não-bloqueante)
        with mock.patch.dict(os.environ, {"JOURNAL_STREAM": "1:2"}):
            self.assertFalse(am._stderr_is_journal())

    def test_lock_memory_errno(self):
        # num processo à parte: se o mlockall der certo, ele trava a memória do processo inteiro
        codigo = ("import importlib.machinery, importlib.util, sys\n"
                  "sys.dont_write_bytecode = True\n"
                  f"l = importlib.machinery.SourceFileLoader('am', {DAEMON!r})\n"
                  "m = importlib.util.module_from_spec(importlib.util.spec_from_loader('am', l))\n"
                  "l.exec_module(m)\n"
                  "libc = m.lock_memory()\n"
                  "print('libc' if libc is not None else 'none')\n")
        r = subprocess.run([sys.executable, "-I", "-c", codigo], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "libc")
        self.assertTrue("memory locked" in r.stderr or "mlockall failed: ENOMEM" in r.stderr
                        or "mlockall failed: EPERM" in r.stderr, r.stderr)


class TestConfigHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.chmod(self.tmp.name, 0o755)
        self.conf = os.path.join(self.tmp.name, "memory.conf")

    def tearDown(self):
        self.tmp.cleanup()

    def run_helper(self, *args):
        return subprocess.run([sys.executable, "-I", "-B", HELPER, "--config-path", self.conf] + list(args),
                              capture_output=True, text=True, timeout=30)

    def test_validate(self):
        v = amc.validate
        self.assertEqual(v("enabled", "1"), "1")
        self.assertEqual(v("warn_available", "07"), "7")
        self.assertEqual(v("background", " fossilize_replay , steam,steam,, "), "fossilize_replay,steam")
        self.assertEqual(v("protect", ""), "")
        for k, val in [("enabled", "yes"), ("enabled", "1\n"), ("enabled", " 1"), ("warn_available", "0"),
                       ("warn_available", "51"), ("warn_available", "-1"), ("warn_available", "1e1"),
                       ("warn_available", "15\n"), ("protect", "a b"), ("protect", "a\nenabled=0"),
                       ("protect", "x=y"), ("protect", "naïve"), ("protect", "a#b"), ("protect", "x" * 33),
                       ("protect", ",".join(f"n{i}" for i in range(21))), ("nope", "1"), ("", "1"),
                       ("enabled ", "1"), ("background", "a;rm -rf /")]:
            with self.assertRaises(amc.Refused, msg=(k, val)):
                v(k, val)

    def test_parse_render_roundtrip_with_daemon(self):
        vals = amc.parse_file(fix("memory_full.conf"))
        self.assertEqual(vals["pause_background"], "1")
        texto = amc.render(vals)
        cfg, prob = am.parse_config(texto)
        self.assertEqual(prob, [])
        self.assertEqual(cfg, am.parse_config(fix("memory_full.conf"))[0])
        # o que o ajudante descarta, o daemon também não aceitaria
        self.assertEqual(amc.parse_file(fix("memory_bad.conf")), {})

    def test_check_pair(self):
        amc.check_pair({"warn_available": "20", "critical_available": "19"})
        with self.assertRaises(amc.Refused):
            amc.check_pair({"warn_available": "4"})
        with self.assertRaises(amc.Refused):
            amc.check_pair({"critical_available": "15"})

    def test_bad_stored_pair_does_not_block_switches(self):
        # par inválido gravado à mão (o guarda já usa 15/4): os switches do Settings continuam valendo
        for par in ("warn_available=5\ncritical_available=10\n", "warn_available=10\ncritical_available=12\n",
                    "warn_available=4\n"):
            with open(self.conf, "w") as f:
                f.write(par + "pause_background=1\n")
            r = self.run_helper("set", "enabled", "0")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("reset to the defaults", r.stderr)
            with open(self.conf) as f:
                texto = f.read()
            self.assertIn("enabled=0\n", texto)
            self.assertIn("pause_background=1\n", texto)
            self.assertNotIn("warn_available", texto)
            self.assertNotIn("critical_available", texto)
            self.assertEqual(am.load_config(self.conf)[1], [])
        # mudar um dos percentuais pra um par inválido continua recusado
        with open(self.conf, "w") as f:
            f.write("warn_available=10\n")
        r = self.run_helper("set", "critical_available", "12")
        self.assertEqual(r.returncode, 2)
        self.assertIn("must be lower than warn_available (10)", r.stderr)
        with open(self.conf) as f:
            self.assertEqual(f.read(), "warn_available=10\n")

    def test_swap_size_never_killed(self):
        # o aero-swap-setup roda sem prazo: um timeout mandaria SIGKILL no meio de um swapoff
        feito = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(amc.os.path, "isfile", return_value=True), \
                mock.patch.object(amc.os, "access", return_value=True), \
                mock.patch.object(amc.subprocess, "run", return_value=feito) as run:
            amc.swap_size("16")
        args, kw = run.call_args
        self.assertEqual(args[0], [amc.SWAP_SETUP, "--apply", "--size", "16"])
        self.assertNotIn("timeout", kw)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)

    def test_config_path_root_guard(self):
        self.assertEqual(amc.config_path(None, 0), (amc.CONF_PATH, False))
        with self.assertRaises(amc.Refused):
            amc.config_path("/etc/shadow", 0)
        self.assertEqual(amc.config_path("/tmp/x", 1000), ("/tmp/x", True))

    def test_swap_result(self):
        self.assertIsNone(amc.swap_setup_result(0, ""))
        self.assertIsInstance(amc.swap_setup_result(6, "a\nnot enough RAM\n"), amc.Unsafe)
        self.assertIsInstance(amc.swap_setup_result(5, ""), amc.Unsafe)
        self.assertIsInstance(amc.swap_setup_result(2, "bad"), amc.Refused)
        self.assertIsInstance(amc.swap_setup_result(1, "x" * 1000), amc.Failed)
        self.assertLess(len(str(amc.swap_setup_result(1, "x" * 1000))), 400)

    def test_set_get_cli(self):
        r = self.run_helper("set", "warn_available", "20")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "ok"), r.stderr)
        self.assertEqual(stat.S_IMODE(os.stat(self.conf).st_mode), 0o644)
        r = self.run_helper("set", "critical_available", "25")
        self.assertEqual(r.returncode, 2)
        self.assertIn("lower than", r.stderr)
        r = self.run_helper("set", "background", "fossilize_replay,steam")
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.conf) as f:
            texto = f.read()
        self.assertIn("warn_available=20\n", texto)
        self.assertIn("background=fossilize_replay,steam\n", texto)
        self.assertNotIn("critical_available", texto)
        r = self.run_helper("get")
        self.assertIn("warn_available=20", r.stdout)
        self.assertIn("critical_available=4", r.stdout)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), ["memory.conf"])     # sem tmp sobrando
        cfg, prob = am.load_config(self.conf)
        self.assertEqual(prob, [])
        self.assertEqual(cfg["background"], ("fossilize_replay", "steam"))

    def test_refusals_cli(self):
        for args in (["set", "enabled"], ["set", "enabled", "2"], ["set", "evil", "1"], ["frobnicate"],
                     ["set", "enabled", "1", "extra"], ["swap-size", "16"], []):
            r = self.run_helper(*args)
            self.assertEqual(r.returncode, 2, (args, r.stderr))
        self.assertFalse(os.path.exists(self.conf))
        r = subprocess.run([sys.executable, "-I", "-B", HELPER, "set", "enabled", "1"],
                           capture_output=True, text=True, timeout=30)
        if os.geteuid() != 0:
            self.assertEqual(r.returncode, 2)       # sem root e sem modo teste: recusa

    def test_concurrent_sets(self):
        chaves = [("enabled", "0"), ("pause_background", "0"), ("kill_on_critical", "0"),
                  ("warn_available", "30"), ("critical_available", "9"), ("protect", "obs"),
                  ("background", "abc")]
        procs = [subprocess.Popen([sys.executable, "-I", "-B", HELPER, "--config-path", self.conf, "set", k, v],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for k, v in chaves]
        for p in procs:
            _, err = p.communicate(timeout=30)
            self.assertEqual(p.returncode, 0, err)
        with open(self.conf) as f:
            vals = amc.parse_file(f.read())
        self.assertEqual(vals, dict(chaves))

    def test_symlink_and_bad_dir(self):
        alvo = os.path.join(self.tmp.name, "victim")
        with open(alvo, "w") as f:
            f.write("keep\n")
        os.symlink(alvo, self.conf)
        r = self.run_helper("set", "enabled", "0")
        self.assertEqual(r.returncode, 4)
        with open(alvo) as f:
            self.assertEqual(f.read(), "keep\n")
        os.unlink(self.conf)
        os.chmod(self.tmp.name, 0o777)
        r = self.run_helper("set", "enabled", "0")
        self.assertEqual(r.returncode, 4)
        os.chmod(self.tmp.name, 0o755)

    def test_no_personal_data_in_files(self):
        usuario = pwd.getpwuid(os.getuid()).pw_name
        maquina = socket.gethostname()
        arquivos = [DAEMON, HELPER, os.path.join(REPO, "system", "memory", "aero-memory.service"), __file__]
        arquivos += [os.path.join(FIX, n) for n in os.listdir(FIX) if os.path.isfile(os.path.join(FIX, n))]
        for caminho in arquivos:
            with open(caminho) as f:
                texto = f.read()
            self.assertNotIn(HOME, texto, caminho)
            self.assertNotIn("/" + "home" + "/", texto, caminho)
            if len(usuario) >= 4:
                self.assertNotIn(usuario, texto, caminho)
            if len(maquina) >= 4:
                self.assertNotIn(maquina, texto, caminho)


if __name__ == "__main__":
    unittest.main()
