"""Testes do aero-memory-agent (componente C). Só biblioteca padrão.

Rodar:  python3 -m unittest discover -s tests/memory -p 'test_agent.py' -v
Dados falsos em tests/memory/fixtures/agent/ (endereços de documentação, caminhos inventados).
Os testes de integração montam um /proc falso, um Hyprland falso (os dois sockets), um servidor HTTP
em 127.0.0.1 e processos filhos descartáveis (sleep) pra testar o encerramento de verdade.
"""
import contextlib
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import pwd
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
FIX = os.path.join(HERE, "fixtures", "agent")
SCRIPT = os.path.join(REPO, "bin", "aero-memory-agent")
UNIT = os.path.join(REPO, "config", "systemd", "user", "aero-memory-agent.service")


def load_agent():
    loader = importlib.machinery.SourceFileLoader("aero_memory_agent", SCRIPT)
    spec = importlib.util.spec_from_loader("aero_memory_agent", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


A = load_agent()
sys.dont_write_bytecode = True


def fixture(name):
    with open(os.path.join(FIX, name)) as f:
        return f.read()


def tcp_text(rows, v6=False):
    """Linhas no formato do /proc/net/tcp(6). rows: (laddr, lport, raddr, rport, estado, uid, inode)."""
    import struct
    fam = socket.AF_INET6 if v6 else socket.AF_INET

    def h(ip):
        raw = socket.inet_pton(fam, ip)
        n = len(raw) // 4
        return "".join("%08X" % w for w in struct.unpack("=%dI" % n, raw))
    out = ["  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"]
    for i, (la, lp, ra, rp, st, uid, ino) in enumerate(rows):
        out.append("%4d: %s:%04X %s:%04X %02X 00000000:00000000 00:00000000 00000000 %5d        0 %d 1 0 100 0 0 10 0"
                   % (i, h(la), lp, h(ra), rp, st, uid, ino))
    return "\n".join(out) + "\n"


def real_start(pid):
    with open(f"/proc/{pid}/stat") as f:
        return int(f.read().rpartition(")")[2].split()[19])


def spawn_sleeper(ignore_term=False):
    code = "import signal,time\n"
    if ignore_term:
        code += "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    code += "print('ready', flush=True)\ntime.sleep(60)\n"
    p = subprocess.Popen([sys.executable, "-I", "-c", code], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    p.stdout.readline()                      # espera o handler de sinal estar no lugar
    p.stdout.close()
    return p


class FakeProc:
    """Um /proc falso numa pasta temporária (só o que o agente lê)."""

    def __init__(self, root):
        self.root = root
        self.procs = {}
        os.makedirs(os.path.join(root, "net"))
        self.write("uptime", "100000.00 200000.00\n")
        self.write("meminfo", "MemTotal: 32000000 kB\nCached: 4000000 kB\n")
        self.set_tcp([], [])

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def set_tcp(self, rows4, rows6=()):
        self.write("net/tcp", tcp_text(rows4))
        self.write("net/tcp6", tcp_text(list(rows6), v6=True))

    def add(self, pid, comm, ppid=1, start=1000, tty=0, uid=None, argv=(), cwd="/", fds=(), maps=""):
        uid = os.getuid() if uid is None else uid
        d = os.path.join(self.root, str(pid))
        os.makedirs(os.path.join(d, "fd"), exist_ok=True)
        self.procs[pid] = {"comm": comm, "ppid": ppid, "start": start, "tty": tty, "uid": uid, "cpu": 100,
                           "rss_mb": 4096, "anon_mb": 3072}
        self._write_stat(pid)
        with open(os.path.join(d, "cmdline"), "wb") as f:
            f.write(b"\0".join(a.encode() for a in argv) + (b"\0" if argv else b""))
        self.write(f"{pid}/maps", maps)
        os.symlink(cwd, os.path.join(d, "cwd"))
        for i, target in enumerate(fds):
            os.symlink(target, os.path.join(d, "fd", str(i)))

    def _write_stat(self, pid):
        p = self.procs[pid]
        self.write(f"{pid}/stat", f"{pid} ({p['comm']}) S {p['ppid']} {pid} {pid} {p['tty']} -1 0 0 0 0 0 "
                                  f"{p['cpu']} 50 0 0 20 0 1 0 {p['start']} 1000 100 0 0 0\n")
        u = p["uid"]
        self.write(f"{pid}/status", f"Name:\t{p['comm']}\nUid:\t{u}\t{u}\t{u}\t{u}\n"
                                    f"VmRSS:\t{p['rss_mb'] * 1024} kB\nRssAnon:\t{p['anon_mb'] * 1024} kB\n"
                                    f"RssShmem:\t1024 kB\n")

    def update(self, pid, **changes):
        """Muda tty / cpu (ticks de utime) / rss_mb / anon_mb de um processo falso."""
        self.procs[pid].update(changes)
        self._write_stat(pid)

    def add_fd(self, pid, target):
        d = os.path.join(self.root, str(pid), "fd")
        os.symlink(target, os.path.join(d, str(len(os.listdir(d)) + 100)))

    def remove(self, pid):
        import shutil
        shutil.rmtree(os.path.join(self.root, str(pid)), ignore_errors=True)


class FakeHypr:
    """Hyprland de mentira: .socket2.sock (eventos) e .socket.sock (responde j/clients)."""

    def __init__(self, inst_dir):
        self.dir = inst_dir
        os.makedirs(inst_dir, exist_ok=True)
        self.clients = []
        self.conns = []
        self.stop = False
        self.ev = self._listen(".socket2.sock")
        self.req = self._listen(".socket.sock")
        self.threads = [threading.Thread(target=self._accept_events, daemon=True),
                        threading.Thread(target=self._serve_requests, daemon=True)]
        for t in self.threads:
            t.start()

    def _listen(self, name):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.bind(os.path.join(self.dir, name))
        s.listen(8)
        s.settimeout(0.05)
        return s

    def _accept_events(self):
        while not self.stop:
            try:
                c, _ = self.ev.accept()
            except (socket.timeout, OSError):
                continue
            self.conns.append(c)

    def _serve_requests(self):
        while not self.stop:
            try:
                c, _ = self.req.accept()
            except (socket.timeout, OSError):
                continue
            with c:
                c.settimeout(1)
                try:
                    if c.recv(256) == b"j/clients":
                        c.sendall(json.dumps(self.clients).encode())
                except OSError:
                    pass

    def wait_conn(self, n=1):
        deadline = time.monotonic() + 3
        while len(self.conns) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        return len(self.conns) >= n

    def send(self, *lines):
        self.conns[-1].sendall("".join(l + "\n" for l in lines).encode())

    def drop(self):
        for c in self.conns:
            c.close()
        self.conns = []

    def close(self):
        self.stop = True
        for t in self.threads:
            t.join(2)
        self.drop()
        self.ev.close()
        self.req.close()


def quiet(agent):
    """Desliga os relógios: o teste chama cada passo na mão."""
    far = time.monotonic() + 1e9
    for k in agent.t:
        agent.t[k] = far
    agent.hypr_retry_at = far


@contextlib.contextmanager
def captured_stderr():
    old, buf = sys.stderr, io.StringIO()
    sys.stderr = buf
    try:
        yield buf
    finally:
        sys.stderr = old


# ======================================================================= funções puras

class TestConfig(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(A.parse_config(None), A.DEFAULTS)
        self.assertEqual(A.parse_config(""), A.DEFAULTS)

    def test_values_and_garbage(self):
        c = A.parse_config("# comment\ncomfy_free_on_close=0\ncomfy_idle_unload_min = 30  # half hour\n"
                           "comfy_free_for_games=off\ncomfy_port=8190\ncomfy_window_class=ComfyUI\nnotify=no\n"
                           "unknown=1\n=5\nnoequals\n")
        self.assertEqual(c["comfy_free_on_close"], 0)
        self.assertEqual(c["comfy_idle_unload_min"], 30)
        self.assertEqual(c["comfy_free_for_games"], 0)
        self.assertEqual(c["comfy_port"], 8190)
        self.assertEqual(c["comfy_window_class"], "comfyui")
        self.assertEqual(c["notify"], 0)
        self.assertNotIn("unknown", c)

    def test_invalid_values_keep_defaults(self):
        c = A.parse_config("comfy_port=0\ncomfy_port=70000\ncomfy_port=-1\ncomfy_idle_unload_min=99999\n"
                           "comfy_idle_unload_min=-3\ncomfy_free_on_close=2\ncomfy_window_class=a b\n"
                           "comfy_window_class=$(rm -rf)\nnotify=maybe\n")
        self.assertEqual(c, A.DEFAULTS)

    def test_odd_digits_never_crash(self):
        # "²".isdigit() é True mas int("²") quebra; número enorme passa do limite do int(): o agente
        # entraria num laço de reinícios. Fica o padrão.
        for line in ("comfy_port=²", "comfy_idle_unload_min=³", "comfy_port=٣٣", "comfy_port=８１８８",
                     "comfy_idle_unload_min=" + "9" * 5000, "comfy_port=" + "1" * 5000, "comfy_port= 8 1"):
            self.assertEqual(A.parse_config(line), A.DEFAULTS, line[:30])

    def test_reload_error_keeps_running_with_old_config(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "c.conf")
            with open(conf, "w") as f:
                f.write("notify=0\ncomfy_idle_unload_min=30\n")
            a = A.Agent(conf_path=conf, events_path=os.path.join(d, "none"), proc=d, sysfs=d)
            quiet(a)
            with open(conf, "w") as f:
                f.write("comfy_port=²\ncomfy_idle_unload_min=" + "9" * 5000 + "\n")
            a.reload = True
            with captured_stderr():
                a.step(timeout_ms=0)                         # arquivo estranho: padrões, sem cair
            self.assertEqual(a.conf, A.DEFAULTS)

            def boom():
                raise RuntimeError("x")
            a.conf = dict(A.DEFAULTS, comfy_idle_unload_min=30)
            a.load_config = boom
            a.reload = True
            with captured_stderr() as err:
                a.step(timeout_ms=0)                         # erro inesperado: fica a config anterior
            self.assertEqual(a.conf["comfy_idle_unload_min"], 30)
            self.assertIn("configuration not reloaded", err.getvalue())
            with captured_stderr():
                a.shutdown()


class TestHyprParsing(unittest.TestCase):
    def test_norm_addr(self):
        self.assertEqual(A.norm_addr("55800f619320"), "0x55800f619320")
        self.assertEqual(A.norm_addr("0x55800F619320"), "0x55800f619320")
        self.assertIsNone(A.norm_addr("zz"))
        self.assertIsNone(A.norm_addr(""))
        self.assertIsNone(A.norm_addr("0x" + "f" * 17))

    def test_event_lines(self):
        lines = fixture("hypr_events.txt").splitlines()
        parsed = [A.parse_hypr_line(l) for l in lines]
        self.assertEqual(parsed[0], ("workspace", "2"))
        self.assertIsNone(parsed[-1])
        opened = [A.parse_openwindow(d) for e, d in (p for p in parsed if p) if e == "openwindow"]
        self.assertEqual(opened[0], ("0x55800f619320", "comfyui"))
        self.assertEqual(opened[1], ("0x55800f5407b0", "firefox"))     # título com vírgulas e >>
        self.assertIsNone(opened[2])                                    # endereço inválido
        self.assertIsNone(opened[3])                                    # campos faltando

    def test_comfy_windows_from_clients(self):
        w = A.comfy_windows(json.loads(fixture("clients.json")), "comfyui")
        self.assertEqual(w, {"0x55800f619320": 4200, "0x55800f6193a0": 4200})
        self.assertEqual(A.comfy_windows(None, "comfyui"), {})
        self.assertEqual(A.comfy_windows({"a": 1}, "comfyui"), {})

    def test_line_buffer(self):
        b = A.LineBuffer(cap=16)
        self.assertEqual(b.feed(b"openwin"), [])
        self.assertEqual(b.feed(b"dow>>a\nclose"), ["openwindow>>a"])
        self.assertEqual(b.feed(b"window>>b\n"), ["closewindow>>b"])
        # linha gigante: sai só o começo, o resto é descartado até a próxima quebra
        self.assertEqual(b.feed(b"x" * 40), ["x" * 16])
        self.assertEqual(b.feed(b"y" * 40 + b"\nok\n"), ["ok"])
        self.assertLessEqual(len(b.buf), 16)
        self.assertEqual(b.feed("é>>ç\n".encode()), ["é>>ç"])

    def test_order_instances(self):
        found = [("old_1", 10.0), ("new_3", 30.0), ("mid_2", 20.0)]
        self.assertEqual(A.order_instances(None, found), ["new_3", "mid_2", "old_1"])
        self.assertEqual(A.order_instances("old_1", found), ["old_1", "new_3", "mid_2"])
        self.assertEqual(A.order_instances("gone_9", found), ["new_3", "mid_2", "old_1"])

    def test_valid_sig(self):
        self.assertTrue(A.valid_sig("efb5_1792522709_223715740"))
        for bad in ("", None, ".", "..", "a/b", "../x", "a b"):
            self.assertFalse(A.valid_sig(bad), bad)


class TestTcp(unittest.TestCase):
    def setUp(self):
        self.entries = A.parse_proc_net_tcp(fixture("proc_net_tcp.txt")) + \
            A.parse_proc_net_tcp(fixture("proc_net_tcp6.txt"))

    def test_hex_to_ip(self):
        self.assertEqual(A.hex_to_ip("0100007F"), "127.0.0.1")
        self.assertEqual(A.hex_to_ip("00000000"), "0.0.0.0")
        self.assertEqual(A.hex_to_ip("00000000000000000000000001000000"), "::1")
        self.assertEqual(A.hex_to_ip("0000000000000000FFFF00000100007F"), "127.0.0.1")
        self.assertEqual(A.hex_to_ip("0" * 32), "::")
        with self.assertRaises(ValueError):
            A.hex_to_ip("123")

    def test_parse(self):
        self.assertEqual(len(self.entries), 14)
        e = self.entries[0]
        self.assertEqual((e.laddr, e.lport, e.state, e.uid, e.inode), ("127.0.0.1", 8188, 0x0A, 1000, 5001))
        self.assertEqual(A.parse_proc_net_tcp("header\nbroken line\n  0: XYZ:1 00:0 0A\n"), [])

    def test_listen_inodes(self):
        # 5002 é num IP de rede, 5003 é de outro usuário (e em 127.0.0.2): os dois ficam de fora
        self.assertEqual(A.listen_inodes(self.entries, 8188, 1000), [5001])
        self.assertEqual(A.listen_inodes(self.entries, 8188, 1001), [])        # 127.0.0.2 não serve
        other = A.parse_proc_net_tcp(tcp_text([("127.0.0.1", 8188, "0.0.0.0", 0, 0x0A, 1001, 5003)]))
        self.assertEqual(A.listen_inodes(other, 8188, 1001), [5003])
        self.assertEqual(A.listen_inodes(other, 8188, 1000), [])               # de outro usuário
        self.assertEqual(A.listen_inodes(self.entries, 8189, 1000), [7001])     # ::
        self.assertEqual(A.listen_inodes(self.entries, 8190, 1000), [7005])     # ::1
        self.assertEqual(A.listen_inodes(self.entries, 9999, 1000), [])

    def test_external_clients(self):
        # conexões no fixture: app (6002), navegador (6004), cliente IPv4 de servidor :: (7003),
        # cliente de outra máquina (sem lado cliente aqui). TIME_WAIT não conta.
        self.assertEqual(A.count_external_clients(self.entries, 8188, set()), 4)
        self.assertEqual(A.count_external_clients(self.entries, 8188, {6002}), 3)
        self.assertEqual(A.count_external_clients(self.entries, 8188, {6002, 6004, 7003}), 1)
        self.assertEqual(A.count_external_clients(self.entries, 9999, set()), 0)

    def test_local_listen_ok(self):
        for ip in ("127.0.0.1", "::1", "0.0.0.0", "::"):
            self.assertTrue(A.local_listen_ok(ip))
        # outro 127.x: o pedido em 127.0.0.1 iria pra outro endereço que não o dele
        for ip in ("127.1.2.3", "192.0.2.1", "2001:db8::1", "::2"):
            self.assertFalse(A.local_listen_ok(ip))


class TestProcParsing(unittest.TestCase):
    def test_stat(self):
        s = A.parse_stat(fixture("stat_backend.txt"))
        self.assertEqual((s["pid"], s["comm"], s["state"], s["ppid"], s["tty"], s["cpu"], s["start"]),
                         (4242, "python3", "S", 4200, 0, 1750, 987654))
        o = A.parse_stat(fixture("stat_odd_comm.txt"))
        self.assertEqual((o["comm"], o["state"], o["tty"], o["start"]), ("Web Content (x) y", "R", 34817, 1000))
        self.assertIsNone(A.parse_stat(""))
        self.assertIsNone(A.parse_stat("12 (x) S 1 2"))

    def test_status(self):
        s = A.parse_status(fixture("status_backend.txt"))
        self.assertEqual(s["uids"], (1000, 1000, 1000, 1000))
        self.assertEqual(s["rss_mb"], 6144)
        self.assertEqual(s["anon_mb"], 5120)
        self.assertEqual(s["shmem_mb"], 64)
        self.assertTrue(A.same_user(s, 1000))
        self.assertFalse(A.same_user(s, 1001))
        self.assertFalse(A.same_user(A.parse_status("Uid:\t1000\t0\t0\t0\n"), 1000))
        self.assertFalse(A.same_user(None, 1000))

    def test_maps(self):
        paths = A.parse_maps_paths(fixture("maps.txt"))
        self.assertEqual(paths, {"/models/checkpoints/base model v1.safetensors",
                                 "/models/loras/STYLE.SafeTensors", "/models/vae/ae.sft"})

    def test_is_model_path(self):
        self.assertTrue(A.is_model_path("/m/x.GGUF"))
        self.assertFalse(A.is_model_path("/m/x.so"))
        self.assertFalse(A.is_model_path("x.ckpt"))
        self.assertFalse(A.is_model_path("socket:[12]"))

    def test_looks_like_comfy(self):
        # main.py só conta numa pasta com a cara do ComfyUI (layout); um "python main.py" qualquer não
        self.assertFalse(A.looks_like_comfy("python", ["/v/bin/python", "main.py", "--port", "8188"], "/d/x"))
        self.assertTrue(A.looks_like_comfy("python", ["/v/bin/python", "main.py", "--port", "8188"], "/d/x",
                                           layout=True))
        self.assertTrue(A.looks_like_comfy("python3", ["python3", "-m", "comfy_cli"], "/"))
        self.assertTrue(A.looks_like_comfy("python3.14", ["python3.14", "server.py"], "/data/ComfyUI"))
        self.assertFalse(A.looks_like_comfy("python3", ["python3", "-m", "http.server", "8188"], "/srv"))
        self.assertFalse(A.looks_like_comfy("node", ["node", "comfy-proxy.js"], "/srv/comfy"))
        self.assertFalse(A.looks_like_comfy("python3", [], "/"))

    def test_is_terminal_tty(self):
        # só pseudo-terminal (/dev/pts/N, major 136-143) é "aberto num terminal"; o tty1 do login
        # (que todo app da sessão herda quando o Hyprland sobe do console) não
        self.assertTrue(A.is_terminal_tty(os.makedev(136, 3)))
        self.assertTrue(A.is_terminal_tty(os.makedev(143, 0)))
        self.assertFalse(A.is_terminal_tty(os.makedev(4, 1)))
        self.assertFalse(A.is_terminal_tty(0))

    def test_comfy_layout(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(A.comfy_layout(d))
            os.mkdir(os.path.join(d, "comfy"))
            self.assertFalse(A.comfy_layout(d))
            open(os.path.join(d, "execution.py"), "w").close()
            self.assertTrue(A.comfy_layout(d))
            self.assertFalse(A.comfy_layout(os.path.join(d, "missing")))

    def test_is_app_comm(self):
        self.assertTrue(A.is_app_comm("electron", "comfyui"))
        self.assertTrue(A.is_app_comm("ComfyUI", "comfyui"))
        self.assertFalse(A.is_app_comm("bash", "comfyui"))
        self.assertFalse(A.is_app_comm("systemd", ""))

    def test_descendants(self):
        ppids = {10: 1, 11: 10, 12: 11, 13: 10, 20: 1, 21: 20}
        self.assertEqual(A.descendants(10, ppids), {11, 12, 13})
        self.assertEqual(A.descendants(99, ppids), set())
        self.assertEqual(A.descendants(1, {1: 1, 2: 1}), {2})       # ciclo não trava


class TestDecisions(unittest.TestCase):
    def test_close_verdict(self):
        cv = A.close_verdict
        self.assertEqual(cv(False, 0, False, True, False, 0), "off")
        self.assertEqual(cv(True, 1, False, True, False, 0), "window")
        self.assertEqual(cv(True, 0, True, True, False, 0), "app")
        self.assertEqual(cv(True, 0, False, False, False, 0), "gone")
        self.assertEqual(cv(True, 0, False, True, True, 0), "terminal")
        self.assertEqual(cv(True, 0, False, True, False, 2), "clients")
        self.assertEqual(cv(True, 0, False, True, False, 0), "stop")

    def test_orphan_due(self):
        self.assertEqual(A.orphan_due(100, None, False), (False, None))
        self.assertEqual(A.orphan_due(100, None, True), (False, 100))
        self.assertEqual(A.orphan_due(150, 100, True), (False, 100))
        self.assertEqual(A.orphan_due(220, 100, True), (True, 100))
        self.assertEqual(A.orphan_due(220, 100, False), (False, None))

    def test_update_busy(self):
        self.assertEqual(A.update_busy(50, 10, None, 0), (10, None))
        self.assertEqual(A.update_busy(50, 10, (1, 0), 0), (50, True))
        self.assertEqual(A.update_busy(50, 10, (0, 3), 0), (50, True))
        self.assertEqual(A.update_busy(50, 10, (0, 0), 0.5), (10, False))
        self.assertEqual(A.update_busy(50, 10, (0, 0), 5.0), (50, True))     # trabalho curto entre olhadas

    def test_idle_unload_due(self):
        d = A.idle_unload_due
        self.assertTrue(d(1000, 100, 15, True, None))
        self.assertTrue(d(1000, 100, 15, True, True))
        self.assertFalse(d(1000, 100, 15, True, False))
        self.assertFalse(d(1000, 100, 15, False, True))
        self.assertFalse(d(1000, 100, 0, True, True))       # 0 = nunca
        self.assertFalse(d(500, 100, 15, True, True))       # ainda não deu 15 min
        self.assertFalse(d(1000, None, 15, True, True))

    def test_unload_worth_it(self):
        # o POST /free mexe na GPU: com a NVIDIA dormindo, só com prova de modelo na RAM
        w = A.unload_worth_it
        self.assertFalse(w(False, False, 9000, 1000))       # já descarregado: nada a fazer
        self.assertTrue(w(None, False, 500, None))          # GPU acordada (ou sem NVIDIA): pode pedir
        self.assertFalse(w(None, True, 1500, 1000))         # dormindo, RAM do tamanho do vazio
        self.assertTrue(w(None, True, 2100, 1000))          # dormindo, +1 GB sobre o vazio
        self.assertFalse(w(None, True, 1900, None))         # dormindo, sem saber o vazio: < 2 GB
        self.assertTrue(w(True, True, 2500, None))
        self.assertFalse(w(True, True, None, None))

    def test_models_loaded_guess(self):
        g = A.models_loaded_guess
        self.assertTrue(g(5000, 2000, None))
        self.assertTrue(g(None, 6000, 1500))
        self.assertFalse(g(300, 1600, 1500))
        self.assertFalse(g(0, 1600, 1500))                  # GPU dormindo, RAM do tamanho do vazio
        self.assertIsNone(g(None, 1600, 1500))
        self.assertIsNone(g(300, 1600, None))
        self.assertIsNone(g(None, None, None))

    def test_games_step(self):
        # primeira amostra vira a base (o que o sistema sempre usa)
        want, armed, base = A.games_step(200, None, 5000, True, True)
        self.assertEqual((want, armed, base), (False, True, 200))
        # um jogo abre: +2 GB de outros, ComfyUI parado com modelo -> libera
        want, armed, base = A.games_step(2200, base, 5000, True, armed)
        self.assertTrue(want)
        # ComfyUI ocupado: não libera
        self.assertFalse(A.games_step(2200, 200, 5000, False, True)[0])
        # ComfyUI sem modelo na VRAM: nada a liberar
        self.assertFalse(A.games_step(2200, 200, 500, True, True)[0])
        # depois de liberar (armado = False), o mesmo jogo não dispara de novo
        self.assertFalse(A.games_step(2300, 200, 5000, True, False)[0])
        # o jogo fecha: rearma
        self.assertTrue(A.games_step(210, 200, 5000, True, False)[1])
        # a base só desce
        self.assertEqual(A.games_step(150, 200, 5000, True, True)[2], 150)


class TestMemoryEvents(unittest.TestCase):
    def test_parse(self):
        # o fixture tem os campos que o aero-memory grava de verdade (seq, uid, count, reason, level...)
        evs = [A.parse_memory_event(l) for l in fixture("events.jsonl").splitlines()]
        kinds = [e["kind"] if e else None for e in evs]
        self.assertEqual(kinds, ["paused", "killed", "resumed", "pressure", None, None, None, None, "killed",
                                 "resumed", "oom_kill"])
        self.assertEqual(evs[0]["names"], ["fossilize_replay", "fossilize_replay"])
        self.assertEqual((evs[0]["uid"], evs[0]["count"], evs[0]["mb"]), (1000, 7, 5200))
        self.assertEqual((evs[1]["name"], evs[1]["pid"], evs[1]["uid"]), ("Web Content", 999, 1000))
        self.assertEqual((evs[2]["reason"], evs[9]["reason"]), ("calm", "reload"))
        self.assertEqual(evs[10]["count"], 1)
        self.assertNotIn("uid", evs[10])                     # o kernel não diz de quem era
        self.assertNotIn("mb", evs[8])                       # negativo: ignorado
        self.assertNotIn("<", evs[8]["name"])
        self.assertNotIn("&", evs[8]["name"])
        self.assertIsNone(A.parse_memory_event(b"\xff\xfe"))

    def test_notifications(self):
        evs = [A.parse_memory_event(l) for l in fixture("events.jsonl").splitlines()]
        kind, title, body, icon, urgency = A.event_notification(evs[0])
        self.assertEqual(kind, "paused")
        self.assertEqual(body, "Paused Steam shader compilation to keep things smooth")
        body = A.event_notification(evs[1])[2]
        self.assertEqual(body, "Closed Web Content (it was using 3.0 GB) to stop the system from freezing")
        self.assertEqual(A.event_notification(evs[2])[0], "resumed")
        self.assertIsNone(A.event_notification(evs[3]))     # pressure: sem notificação
        self.assertIsNone(A.event_notification(evs[9]))     # resumed por recarga: não "voltou ao normal"
        self.assertEqual(A.event_notification(evs[10])[:3],
                         ("oom_kill", "Memory ran out", "The system closed an app to stop a freeze"))
        body = A.event_notification(evs[8])[2]
        for ch in "<>&'\"":
            self.assertNotIn(ch, body)
        many = {"kind": "paused", "t": 1, "names": ["a", "b", "c", "d", "e"]}
        self.assertEqual(A.event_notification(many)[2], "Paused a, b, c and 2 more to keep things smooth")
        self.assertEqual(A.event_notification({"kind": "paused", "t": 1})[2],
                         "Paused background work to keep things smooth")

    def test_dry_run_events_ignored(self):
        # aero-memory em --dry-run: nada aconteceu de verdade (sem aviso falso, sem POST /free)
        for kind in A.MEMORY_KINDS:
            self.assertIsNone(A.parse_memory_event(json.dumps({"t": 1, "kind": kind, "dry_run": True})))
            self.assertIsNotNone(A.parse_memory_event(json.dumps({"t": 1, "kind": kind, "dry_run": False})))

    def test_resumed_only_calm_says_back_to_normal(self):
        # o aero-memory retoma também ao recarregar a config, ao parar, com a pausa desligada e na partida
        def note(reason):
            ev = {"t": 1, "kind": "resumed", "names": ["x"], "uid": 1000}
            if reason is not ...:
                ev["reason"] = reason
            return A.event_notification(A.parse_memory_event(json.dumps(ev)))
        self.assertEqual(note("calm")[1], "Memory is back to normal")
        self.assertEqual(note(...)[1], "Memory is back to normal")             # aero-memory antigo, sem motivo
        for r in ("reload", "stopping", "disabled", "leftover", "something-new", 5, ["calm"]):
            self.assertIsNone(note(r), r)
        self.assertEqual(A.parse_memory_event('{"t":1,"kind":"resumed","reason":"<b>x</b>"}')["reason"], "other")

    def test_uid_parse(self):
        self.assertEqual(A.parse_memory_event('{"t": 1, "kind": "killed", "uid": 1000}')["uid"], 1000)
        for bad in ('true', '-1', '"1000"', '1.5', str(2 ** 32)):
            self.assertNotIn("uid", A.parse_memory_event('{"t": 1, "kind": "killed", "uid": %s}' % bad))

    def test_kernel_cut_names(self):
        # o kernel corta o comm em 15 letras: "fossilize_replay" chega como "fossilize_repla"
        ev = A.parse_memory_event('{"t": 1, "kind": "paused", "names": ["fossilize_repla"]}')
        self.assertEqual(A.event_notification(ev)[2], "Paused Steam shader compilation to keep things smooth")
        ev = A.parse_memory_event('{"t": 1, "kind": "killed", "name": "fossilize_repla", "pid": 77, "mb": 5200}')
        self.assertTrue(A.event_notification(ev)[2].startswith("Closed Steam shader compilation (it was"))
        # o servidor do ComfyUI fechado aparece como "python": vira "ComfyUI" pelo pid
        ev = A.parse_memory_event('{"t": 1, "kind": "killed", "name": "python", "pid": 4242, "mb": 6000}')
        self.assertTrue(A.event_notification(ev, (4242,))[2].startswith("Closed ComfyUI (it was"))
        self.assertTrue(A.event_notification(ev, (1,))[2].startswith("Closed python (it was"))

    def test_clean_name(self):
        self.assertEqual(A.clean_name("kworker/0:1"), "kworker/0:1")
        self.assertEqual(A.clean_name("a\nb\x1b[31m"), "a?b??31m")
        self.assertEqual(len(A.clean_name("x" * 100)), 32)

    def test_fmt_size(self):
        self.assertEqual(A.fmt_size(512), "512 MB")
        self.assertEqual(A.fmt_size(3100), "3.0 GB")
        self.assertEqual(A.fmt_size(None), "0 MB")
        self.assertEqual(A.fmt_size(-5), "0 MB")


class TestRateLimit(unittest.TestCase):
    def test_per_kind(self):
        r = A.RateLimit(30)
        self.assertTrue(r.allow("a", 0))
        self.assertFalse(r.allow("a", 10))
        self.assertTrue(r.allow("b", 10))
        self.assertTrue(r.allow("a", 31))

    def test_bounded(self):
        r = A.RateLimit(30, cap=4)
        for i in range(4):
            self.assertTrue(r.allow(i, 0))
        self.assertFalse(r.allow("x", 1))                    # cheio e ninguém venceu: recusa
        self.assertTrue(r.allow("x", 100))                   # os velhos saem
        self.assertLessEqual(len(r.last), 4)


class TestNvidia(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(A.parse_nvidia_apps(fixture("nvidia_apps.txt")), {4242: 5120, 4243: 300, 5555: 2048})
        self.assertEqual(A.parse_nvidia_apps(""), {})
        self.assertEqual(A.parse_nvidia_apps("No running processes found\n"), {})
        self.assertEqual(A.parse_nvidia_apps("1, 10\n1, 20\n"), {1: 30})
        self.assertEqual(A.parse_nvidia_used(fixture("nvidia_used.txt")), 7800)
        self.assertEqual(A.parse_nvidia_used("100\n200\n"), 300)
        self.assertIsNone(A.parse_nvidia_used("[N/A]\n"))
        self.assertIsNone(A.parse_nvidia_used(None))

    def test_states(self):
        self.assertTrue(A.nvidia_awake(["active"]))
        self.assertFalse(A.nvidia_awake(["active", "suspended"]))   # nvidia-smi acordaria a outra
        self.assertFalse(A.nvidia_awake(["resuming"]))
        self.assertFalse(A.nvidia_awake(["unsupported"]))
        self.assertFalse(A.nvidia_awake([]))
        self.assertTrue(A.nvidia_asleep(["suspended"]))
        self.assertFalse(A.nvidia_asleep(["suspended", "active"]))
        self.assertFalse(A.nvidia_asleep([]))

    def test_find_gpus_fake_sysfs(self):
        with tempfile.TemporaryDirectory() as d:
            def dev(name, vendor, cls, status):
                p = os.path.join(d, name)
                os.makedirs(os.path.join(p, "power"))
                for f, v in (("vendor", vendor), ("class", cls), ("power/runtime_status", status)):
                    with open(os.path.join(p, f), "w") as fh:
                        fh.write(v + "\n")
            dev("0000:00:02.0", "0x1002", "0x030000", "active")     # AMD
            dev("0000:01:00.0", "0x10de", "0x030000", "suspended")  # NVIDIA vídeo
            dev("0000:01:00.1", "0x10de", "0x040300", "active")     # NVIDIA áudio: não é GPU
            dev("0000:02:00.0", "0x10de", "0x030200", "active")     # NVIDIA 3D controller
            gpus = A.find_nvidia_gpus(d)
            self.assertEqual([os.path.basename(g) for g in gpus], ["0000:01:00.0", "0000:02:00.0"])
            self.assertEqual(A.gpu_states(gpus), ["suspended", "active"])
            self.assertFalse(A.nvidia_awake(A.gpu_states(gpus)))
            self.assertEqual(A.find_nvidia_gpus(os.path.join(d, "missing")), [])


class TestQueueParse(unittest.TestCase):
    def test_parse_queue(self):
        self.assertEqual(A.parse_queue({"queue_running": [], "queue_pending": []}), (0, 0))
        self.assertEqual(A.parse_queue({"queue_running": [[1, "id", {}]], "queue_pending": [1, 2]}), (1, 2))
        self.assertIsNone(A.parse_queue({"queue_running": "x", "queue_pending": []}))
        self.assertIsNone(A.parse_queue([]))


# ======================================================================= arquivos

class TestEventTail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "events.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def append(self, text):
        with open(self.path, "a") as f:
            f.write(text)

    def ev(self, kind, t):
        return json.dumps({"t": t, "kind": kind}) + "\n"

    def test_skips_history_then_follows(self):
        self.append(self.ev("paused", 100))
        tail = A.EventTail(self.path, now=50)
        self.assertEqual(tail.poll(), [])                   # o passado não repete
        self.append(self.ev("resumed", 101))
        self.assertEqual([e["kind"] for e in tail.poll()], ["resumed"])
        self.assertEqual(tail.poll(), [])

    def test_partial_line(self):
        tail = A.EventTail(self.path, now=50)
        tail.poll()
        line = self.ev("killed", 102)
        self.append(line[:10])
        self.assertEqual(tail.poll(), [])
        self.append(line[10:])
        self.assertEqual([e["kind"] for e in tail.poll()], ["killed"])

    def test_rotation_truncate_half(self):
        tail = A.EventTail(self.path, now=50)
        tail.poll()
        for i in range(10):
            self.append(self.ev("paused", 100 + i))
        self.assertEqual(len(tail.poll()), 10)
        with open(self.path) as f:                           # como o aero-memory faz: fica a metade nova
            keep = f.read().splitlines()[5:]
        with open(self.path, "w") as f:
            f.write("\n".join(keep) + "\n")
        self.append(self.ev("resumed", 200))
        got = tail.poll()
        self.assertEqual([e["kind"] for e in got], ["resumed"])

    def test_rewrite_longer_than_offset(self):
        tail = A.EventTail(self.path, now=50)
        tail.poll()
        self.append(self.ev("killed", 100))
        self.assertEqual(len(tail.poll()), 1)
        with open(self.path, "w") as f:                      # cortado e reescrito, maior que antes
            f.write(self.ev("paused", 150) + self.ev("resumed", 151))
        self.assertEqual([e["kind"] for e in tail.poll()], ["paused", "resumed"])

    def test_missing_then_created(self):
        tail = A.EventTail(self.path, now=50)
        self.assertEqual(tail.poll(), [])
        self.append(self.ev("old", 10) + self.ev("paused", 10) + self.ev("killed", 60))
        self.assertEqual([e["kind"] for e in tail.poll()], ["killed"])   # mais velho que a partida: pula

    def test_deleted_and_recreated(self):
        self.append(self.ev("paused", 100))
        tail = A.EventTail(self.path, now=50)
        tail.poll()
        os.unlink(self.path)
        self.assertEqual(tail.poll(), [])
        self.append(self.ev("resumed", 120))
        self.assertEqual([e["kind"] for e in tail.poll()], ["resumed"])

    def test_huge_line_does_not_grow(self):
        tail = A.EventTail(self.path, now=50)
        tail.poll()
        self.append("x" * (A.EVENTS_READ_CAP + 100))
        self.assertEqual(tail.poll(), [])
        self.append("\n" + self.ev("killed", 100))
        kinds = []
        for _ in range(3):
            kinds += [e["kind"] for e in tail.poll()]
        self.assertEqual(kinds, ["killed"])

    def test_not_a_regular_file(self):
        os.mkfifo(self.path)
        tail = A.EventTail(self.path, now=50)
        self.assertEqual(tail.poll(), [])                    # nunca abre (não trava num FIFO)


class TestModelFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, name, size=4096):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(b"\0" * size)
        return p

    def test_remember_dedupe_cap_regular_only(self):
        a = self.make("a.safetensors")
        link = os.path.join(self.d, "link.safetensors")
        os.symlink(a, link)
        hard = os.path.join(self.d, "hard.safetensors")
        os.link(a, hard)
        os.mkdir(os.path.join(self.d, "dir.ckpt"))
        b = self.make("b.gguf")
        known, seen = {}, set()
        A.remember_model_files({a, link, hard, b, os.path.join(self.d, "dir.ckpt"),
                                os.path.join(self.d, "missing.pt")}, known, seen)
        self.assertEqual(len(known), 2)                      # a (3 nomes, 1 arquivo) + b
        self.assertTrue(all(os.path.isabs(p) for p, _ in known.values()))
        known2 = {}
        A.remember_model_files({self.make(f"m{i}.pt") for i in range(10)}, known2, set(), cap=3)
        self.assertEqual(len(known2), 3)

    def test_purge(self):
        a = self.make("a.safetensors", 1 << 20)
        known = {}
        A.remember_model_files({a}, known, set())
        self.assertEqual(A.purge_files(known), (1, 1 << 20))

    def test_purge_refuses_swapped_or_odd_files(self):
        a = self.make("a.safetensors")
        known = {}
        A.remember_model_files({a}, known, set())
        os.unlink(a)
        self.make("a.safetensors")                           # outro arquivo no mesmo nome
        self.assertEqual(A.purge_files(known)[0], 0)
        fifo = os.path.join(self.d, "f.ckpt")
        os.mkfifo(fifo)
        st = os.stat(fifo)
        t0 = time.monotonic()
        self.assertEqual(A.purge_files({(st.st_dev, st.st_ino): (fifo, 0)})[0], 0)
        self.assertLess(time.monotonic() - t0, 1)            # nem abre um FIFO
        target = self.make("t.pt")
        ln = os.path.join(self.d, "ln.pt")
        os.symlink(target, ln)
        st = os.stat(target)
        self.assertEqual(A.purge_files({(st.st_dev, st.st_ino): (ln, 0)})[0], 0)    # nunca segue link

    def test_collect_from_fake_proc(self):
        a = self.make("x.safetensors")
        b = self.make("y.SFT")
        fp = FakeProc(os.path.join(self.d, "proc"))
        fp.add(4242, "python3", fds=["/dev/null", "socket:[5001]", b, b + " (deleted)"],
               maps=f"7f00-7f01 r--s 00000000 00:2a 9001 {a}\n7f01-7f02 r--p 0 00:00 0\n")
        self.assertEqual(A.fd_model_paths(4242, fp.root), {b})
        self.assertEqual(A.socket_inodes(4242, fp.root), {5001})
        paths = A.parse_maps_paths(A.read_text(f"{fp.root}/4242/maps")) | A.fd_model_paths(4242, fp.root)
        known = {}
        A.remember_model_files(paths, known, set())
        self.assertEqual(len(known), 2)


class TestStateFile(unittest.TestCase):
    def test_atomic_private(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.json")
            A.atomic_write_json(p, {"a": 1})
            A.atomic_write_json(p, {"a": 2})
            with open(p) as f:
                self.assertEqual(json.load(f), {"a": 2})
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            self.assertEqual(os.listdir(d), ["s.json"])       # nenhum temporário sobrando
            with self.assertRaises(TypeError):
                A.atomic_write_json(p, {"bad": object()})
            self.assertEqual(os.listdir(d), ["s.json"])


# ======================================================================= processos de verdade

class TestStopProcess(unittest.TestCase):
    def test_terminate(self):
        p = spawn_sleeper()
        self.assertEqual(A.stop_process(p.pid, real_start(p.pid), wait=5), "terminated")
        self.assertEqual(p.wait(2), -signal.SIGTERM)

    def test_kill_when_term_ignored(self):
        p = spawn_sleeper(ignore_term=True)
        self.assertEqual(A.stop_process(p.pid, real_start(p.pid), wait=0.5), "killed")
        self.assertEqual(p.wait(2), -signal.SIGKILL)

    def test_stopped_process_still_ends(self):
        p = spawn_sleeper()
        os.kill(p.pid, signal.SIGSTOP)
        self.assertEqual(A.stop_process(p.pid, real_start(p.pid), wait=5), "terminated")
        p.wait(2)

    def test_pid_reuse_guard(self):
        p = spawn_sleeper()
        try:
            self.assertEqual(A.stop_process(p.pid, real_start(p.pid) + 1, wait=0.5), "gone")
            self.assertIsNone(p.poll())                      # não mexeu no processo
        finally:
            p.kill()
            p.wait(2)

    def test_already_gone(self):
        p = spawn_sleeper()
        start = real_start(p.pid)
        p.kill()
        p.wait(2)
        self.assertEqual(A.stop_process(p.pid, start, wait=0.5), "gone")


# ======================================================================= HTTP (127.0.0.1)

class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        srv.hits.append(("GET", self.path, None))
        if srv.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{srv.other_port}/queue")
            self.end_headers()
            return
        body = srv.queue_body
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.server.hits.append(("POST", self.path, self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


class TestHttp(unittest.TestCase):
    def server(self):
        s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        s.hits, s.mode, s.other_port = [], "ok", 0
        s.queue_body = b'{"queue_running": [], "queue_pending": [[1]]}'
        t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.addCleanup(lambda: (s.shutdown(), s.server_close(), t.join(2)))
        return s

    def test_queue_and_free(self):
        s = self.server()
        port = s.server_address[1]
        self.assertEqual(A.comfy_queue(port), (0, 1))
        self.assertTrue(A.comfy_free(port))
        self.assertEqual(s.hits[-1], ("POST", "/free", A.FREE_BODY))
        self.assertEqual(json.loads(A.FREE_BODY), {"unload_models": True, "free_memory": True})

    def test_ignores_proxy_env(self):
        s = self.server()
        old = {k: os.environ.get(k) for k in ("http_proxy", "HTTP_PROXY", "no_proxy", "NO_PROXY")}
        os.environ["http_proxy"] = os.environ["HTTP_PROXY"] = "http://127.0.0.1:9"
        os.environ.pop("no_proxy", None)
        os.environ.pop("NO_PROXY", None)
        try:
            self.assertEqual(A.comfy_queue(s.server_address[1]), (0, 1))
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_never_follows_redirects(self):
        s, other = self.server(), self.server()
        s.mode, s.other_port = "redirect", other.server_address[1]
        self.assertIsNone(A.comfy_queue(s.server_address[1]))
        self.assertEqual(other.hits, [])

    def test_huge_queue_is_busy(self):
        s = self.server()
        s.queue_body = b'{"queue_running": [' + b'"x",' * (A.HTTP_MAX_BYTES // 4 + 10) + b'"x"], "queue_pending": []}'
        self.assertEqual(A.comfy_queue(s.server_address[1]), (1, 0))

    def test_bad_json_and_down(self):
        s = self.server()
        s.queue_body = b"<html>not json</html>"
        self.assertIsNone(A.comfy_queue(s.server_address[1]))
        with socket.socket() as probe:                       # porta livre: ninguém escuta
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        self.assertIsNone(A.comfy_queue(free_port))
        self.assertFalse(A.comfy_free(free_port))

    def test_deadline_holds_against_a_trickling_server(self):
        # um byte a cada 0,1 s: o prazo total (2x o timeout) vale, o agente nunca fica preso esperando
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(2)
        srv.settimeout(5)
        port = srv.getsockname()[1]
        done = threading.Event()

        def serve(head):
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                c.recv(4096)
                try:
                    c.sendall(head)
                    while not done.is_set():
                        c.sendall(b"x")
                        time.sleep(0.1)
                except OSError:
                    pass

        for head in (b"HTTP/1.0 200 OK\r\nContent-Length: 100000\r\n\r\n", b"HTTP/1.0 200 OK\r\nX-Slow: "):
            t = threading.Thread(target=serve, args=(head,), daemon=True)
            t.start()
            t0 = time.monotonic()
            with self.assertRaises(TimeoutError):
                A.comfy_http(port, "/queue", timeout=0.3)
            self.assertLess(time.monotonic() - t0, 1.5)
            done.set()
            t.join(3)
            done.clear()
        srv.close()

    def test_ipv6_only_server(self):
        try:
            s = start_http(self, "::1")
        except OSError:
            self.skipTest("no IPv6 loopback here")
        s.queue_body = b'{"queue_running": [1], "queue_pending": []}'
        port = s.server_address[1]
        self.assertEqual(A.comfy_queue(port, host="::1"), (1, 0))
        self.assertTrue(A.comfy_free(port, host="::1"))
        self.assertEqual(A.http_host(["::1"]), "::1")
        self.assertEqual(A.http_host(["::", "0.0.0.0"]), "127.0.0.1")
        with self.assertRaises(ValueError):
            A.comfy_http(port, "/queue", host="192.0.2.1")              # só 127.0.0.1 / ::1

    def test_only_fixed_requests(self):
        for port, path in ((8188, "/prompt"), (8188, "/queue?x=1"), (0, "/queue"), (70000, "/queue"),
                           (True, "/queue"), ("8188", "/queue")):
            with self.assertRaises(ValueError):
                A.comfy_http(port, path)


# ======================================================================= o agente inteiro

def start_http(test, host="127.0.0.1"):
    """Um "ComfyUI" HTTP de mentira (porta livre: os testes nunca falam com um ComfyUI de verdade)."""
    cls = http.server.ThreadingHTTPServer
    if ":" in host:
        cls = type("Http6", (http.server.ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    s = cls((host, 0), _Handler)
    s.hits, s.mode, s.other_port = [], "ok", 0
    s.queue_body = b'{"queue_running": [], "queue_pending": []}'
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    t.start()
    test.addCleanup(lambda: (s.shutdown(), s.server_close(), t.join(2)))
    return s


class AgentCase(unittest.TestCase):
    """Agente com /proc falso, sem GPU, sem notificações, um Hyprland falso (caminhos relativos:
    o limite de 108 bytes de um socket UNIX não deixa usar a pasta temporária longa) e um ComfyUI HTTP falso."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.mkdir("rt", 0o700)
        os.mkdir("sysfs")
        self.fp = FakeProc(os.path.join(self.tmp.name, "proc"))
        conf = os.path.join(self.tmp.name, "agent.conf")
        with open(conf, "w") as f:
            f.write("notify=0\n")
        self.state = os.path.join(self.tmp.name, "rt", "agent.json")
        self.agent = A.Agent(conf_path=conf, events_path=os.path.join(self.tmp.name, "none.jsonl"),
                             state_path=self.state, proc=self.fp.root, sysfs="sysfs")
        self.agent.runtime = "rt"
        self.agent.notify_bin = None
        self.agent.smi = None
        quiet(self.agent)
        self.hypr = None
        self.kids = []
        self.err = captured_stderr()
        self.logbuf = self.err.__enter__()
        self.srv = start_http(self)
        self.port = self.srv.server_address[1]
        self.agent.conf["comfy_port"] = self.port

    def tearDown(self):
        self.agent.shutdown()
        self.err.__exit__(None, None, None)
        if self.hypr:
            self.hypr.close()
        for p in self.kids:
            if p.poll() is None:
                p.kill()
            p.wait(2)
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def logs(self):
        return self.logbuf.getvalue()

    def start_hypr(self, clients=()):
        self.hypr = FakeHypr(os.path.join("rt", "hypr", "test_1_1"))
        self.hypr.clients = list(clients)
        self.assertTrue(self.agent.hypr_connect())
        self.assertTrue(self.hypr.wait_conn())

    def pump(self, until, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.agent.step(timeout_ms=50)
            if until():
                return True
        return False

    def comfy_setup(self, tty=0, cmd=("python3", "main.py", "--port", "8188"), cwd_name="ComfyUI",
                    extra_tcp=(), with_app=True):
        """Um servidor (processo real dormindo) e, se pedido, o app (outro processo real)."""
        uid = os.getuid()
        backend = spawn_sleeper()
        self.kids.append(backend)
        app_pid = None
        if with_app:
            app = spawn_sleeper()
            self.kids.append(app)
            app_pid = app.pid
            self.fp.add(app.pid, "electron", start=real_start(app.pid), fds=["socket:[6002]"])
        models = []
        for n in ("unet.safetensors", "clip.gguf"):
            p = os.path.join(self.tmp.name, n)
            with open(p, "wb") as f:
                f.write(b"\0" * 65536)
            models.append(p)
        cwd = os.path.join(self.tmp.name, cwd_name)
        os.makedirs(cwd, exist_ok=True)
        start = real_start(backend.pid)
        self.fp.write("uptime", f"{start / A.CLK_TCK + 10000:.2f} 0.00\n")
        self.fp.add(backend.pid, cmd[0], ppid=app_pid or 1, start=start, tty=tty,
                    argv=cmd, cwd=cwd, fds=["socket:[5001]", "socket:[6001]", models[1]],
                    maps=f"7f00-7f01 r--s 00000000 00:2a 9001 {models[0]}\n")
        rows = [("127.0.0.1", self.port, "0.0.0.0", 0, 0x0A, uid, 5001)] + list(extra_tcp)
        self.fp.set_tcp(rows)
        return backend, app_pid, models

    def simple_backend(self):
        """Servidor sem app, na pasta "ComfyUI", já achado pelo agente."""
        backend, _, _ = self.comfy_setup(with_app=False)
        self.backend_proc = backend
        return self.agent.scan_backend()

    def posts(self):
        return [h for h in self.srv.hits if h[0] == "POST"]

    def age_orphan(self, b, s=None):
        """Faz de conta que ele está órfão e parado há `s` segundos (padrão: o suficiente pra encerrar)."""
        s = A.ORPHAN_AFTER_S + 1 if s is None else s
        b.last_busy -= s + 10
        self.agent.orphan_since = time.monotonic() - s

    def fake_gpu(self, status="active"):
        """Uma NVIDIA falsa no sysfs falso e um nvidia-smi falso (responde o que está em apps.txt/used.txt
        e anota cada chamada em calls.txt)."""
        d = os.path.join("sysfs", "0000:01:00.0")
        os.makedirs(os.path.join(d, "power"), exist_ok=True)
        for f, v in (("vendor", "0x10de"), ("class", "0x030000"), ("power/runtime_status", status)):
            with open(os.path.join(d, f), "w") as fh:
                fh.write(v + "\n")
        self.agent.gpus = A.find_nvidia_gpus("sysfs")
        smi = os.path.join(self.tmp.name, "nvidia-smi")
        with open(smi, "w") as f:
            f.write('#!/bin/sh\nd=$(dirname "$0")\necho "$1" >> "$d/calls.txt"\ncase "$1" in\n'
                    '  --query-compute-apps=*) cat "$d/apps.txt" ;;\n  --query-gpu=*) cat "$d/used.txt" ;;\n'
                    '  *) exit 1 ;;\nesac\n')
        os.chmod(smi, 0o755)
        self.agent.smi = smi

    def smi_says(self, apps, used):
        for name, text in (("apps.txt", apps), ("used.txt", used)):
            with open(os.path.join(self.tmp.name, name), "w") as f:
                f.write(text)

    def smi_calls(self):
        try:
            with open(os.path.join(self.tmp.name, "calls.txt")) as f:
                return f.read().split()
        except OSError:
            return []


class TestAgentWindows(AgentCase):
    def test_events_from_socket2(self):
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": 1}])
        self.assertEqual(self.agent.windows, {"0x1a": None})            # pid 1 nunca vira "app"
        self.hypr.clients = [{"address": "0x1a", "class": "comfyui", "pid": 1},
                             {"address": "0x2b", "class": "comfyui", "pid": 1}]
        self.hypr.send("openwindow>>2b,1,comfyui,ComfyUI", "openwindow>>3c,1,kitty,shell")
        self.assertTrue(self.pump(lambda: len(self.agent.windows) == 2))
        self.hypr.send("closewindow>>1a")
        self.assertTrue(self.pump(lambda: len(self.agent.windows) == 1))
        self.assertIsNone(self.agent.close_at)                           # ainda tem uma janela
        self.hypr.send("closewindow>>3c", "closewindow>>2b")
        self.assertTrue(self.pump(lambda: self.agent.close_at is not None))
        self.hypr.send("openwindow>>4d,1,ComfyUI,again")                 # reabriu: cancela
        self.assertTrue(self.pump(lambda: self.agent.close_at is None))

    def test_reconnect_with_backoff(self):
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": 1}])
        self.hypr.drop()                                                 # Hyprland "caiu"
        self.assertTrue(self.pump(lambda: self.agent.hypr_sock is None))
        self.assertEqual(self.agent.windows, {})
        self.assertIsNotNone(self.agent.close_at)                        # sem compositor, sem janela
        self.assertTrue(self.agent.hypr_connect())                       # voltou
        self.assertTrue(self.hypr.wait_conn())
        self.hypr.close()
        self.hypr = None
        for name in (".socket2.sock", ".socket.sock"):
            os.unlink(os.path.join("rt", "hypr", "test_1_1", name))
        self.agent.hypr_disconnect()
        b0 = self.agent.hypr_backoff
        self.assertFalse(self.agent.hypr_connect())
        self.assertFalse(self.agent.hypr_connect())
        self.assertGreater(self.agent.hypr_backoff, b0)
        for _ in range(10):
            self.agent.hypr_connect()
        self.assertLessEqual(self.agent.hypr_backoff, 30)

    def test_instant_drop_backoff_grows(self):
        # aceita e fecha na hora (Hyprland saindo, socket velho): a espera cresce, o log não enche
        self.start_hypr([])
        waits = []
        for _ in range(6):
            self.hypr.drop()
            self.assertTrue(self.pump(lambda: self.agent.hypr_sock is None))
            waits.append(round(self.agent.hypr_retry_at - time.monotonic()))
            self.assertTrue(self.agent.hypr_connect())
            self.assertTrue(self.hypr.wait_conn())
        self.assertEqual(waits, [1, 2, 4, 8, 16, 30])
        self.assertLessEqual(self.logs().count("events closed"), 2)
        # uma conexão que durou: volta pra espera curta
        self.agent.hypr_since -= 60
        self.hypr.drop()
        self.assertTrue(self.pump(lambda: self.agent.hypr_sock is None))
        self.assertEqual(round(self.agent.hypr_retry_at - time.monotonic()), 1)

    def test_newest_instance_and_stale_env(self):
        old = FakeHypr(os.path.join("rt", "hypr", "old_1_1"))
        old.close()                                                      # sobrou o arquivo, ninguém escuta
        os.utime(os.path.join("rt", "hypr", "old_1_1", ".socket2.sock"), (1, 1))
        self.hypr = FakeHypr(os.path.join("rt", "hypr", "new_2_2"))
        os.environ["HYPRLAND_INSTANCE_SIGNATURE"] = "old_1_1"
        try:
            self.assertTrue(self.agent.hypr_connect())                   # a do ambiente falha: pega a viva
        finally:
            del os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        self.assertTrue(self.agent.hypr_dir.endswith("new_2_2"))


class TestAgentClose(AgentCase):
    def close_now(self):
        self.agent.close_at = time.monotonic() - 1
        self.agent.timers()

    def test_last_window_closes_server_stopped_and_purged(self):
        backend, app_pid, models = self.comfy_setup()
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": app_pid}])
        self.assertIn(app_pid, self.agent.apps)
        b = self.agent.scan_backend()
        self.assertEqual(b.pid, backend.pid)
        self.hypr.clients = []
        self.hypr.send("closewindow>>1a")
        self.assertTrue(self.pump(lambda: self.agent.close_at is not None))
        # o app (Electron) sai sozinho, como faz ao fechar a janela
        for p in self.kids:
            if p.pid == app_pid:
                p.terminate()
                p.wait(2)
        self.fp.remove(app_pid)
        self.assertTrue(self.pump(lambda: not self.agent.apps))
        self.close_now()
        self.assertEqual(backend.wait(3), -signal.SIGTERM)
        self.assertIsNone(self.agent.backend)
        la = self.agent.last_action
        self.assertEqual((la["kind"], la["files"]), ("closed", 2))
        logs = self.logs()
        self.assertIn("terminated", logs)
        self.assertNotIn(self.tmp.name, logs)                           # nada de caminho no log
        for m in models:
            self.assertNotIn(os.path.basename(m), logs)

    def test_browser_still_connected_keeps_server(self):
        uid = os.getuid()
        browser = [("127.0.0.1", self.port, "127.0.0.1", 41000, 0x01, uid, 6101),
                   ("127.0.0.1", 41000, "127.0.0.1", self.port, 0x01, uid, 6102)]
        backend, _, _ = self.comfy_setup(extra_tcp=browser, with_app=False)
        self.start_hypr([])
        self.agent.scan_backend()
        self.close_now()
        self.assertIsNone(backend.poll())
        self.assertIn("other connection", self.logs())

    def test_app_own_connection_does_not_count(self):
        uid = os.getuid()
        app_conn = [("127.0.0.1", self.port, "127.0.0.1", 40000, 0x01, uid, 6001),
                    ("127.0.0.1", 40000, "127.0.0.1", self.port, 0x01, uid, 6002)]
        backend, app_pid, _ = self.comfy_setup(extra_tcp=app_conn)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertIn(app_pid, self.agent.apps)                         # achado pelo pai do servidor
        self.assertEqual(self.agent.external_clients(b), 0)

    def test_app_alive_in_tray_keeps_server(self):
        backend, app_pid, _ = self.comfy_setup()
        self.start_hypr([])
        self.agent.scan_backend()
        self.close_now()
        self.assertIsNone(backend.poll())
        self.assertIn("still running", self.logs())

    def test_terminal_server_is_never_stopped(self):
        backend, _, _ = self.comfy_setup(tty=34817, with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertTrue(b.has_tty)
        self.close_now()
        self.agent.orphan_since = time.monotonic() - 1000
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())

    def test_other_program_on_the_port_is_ignored(self):
        backend, _, _ = self.comfy_setup(cmd=("python3", "-m", "http.server", "8188"), cwd_name="srv",
                                         with_app=False)
        self.start_hypr([])
        self.assertIsNone(self.agent.scan_backend())
        self.close_now()
        self.assertIsNone(backend.poll())

    def test_listener_of_other_user_is_ignored(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.fp.set_tcp([("127.0.0.1", 8188, "0.0.0.0", 0, 0x0A, os.getuid() + 1, 5001)])
        self.assertIsNone(self.agent.scan_backend())

    def test_switch_off(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.agent.conf["comfy_free_on_close"] = 0
        self.start_hypr([])
        self.agent.scan_backend()
        self.close_now()
        self.agent.orphan_since = time.monotonic() - 1000
        self.agent.orphan_check(self.agent.backend)
        self.assertIsNone(backend.poll())

    def test_dry_run_never_acts(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.agent.dry = True
        self.start_hypr([])
        self.agent.scan_backend()
        self.close_now()
        self.assertIsNone(backend.poll())
        self.assertIn("[dry-run] would stop", self.logs())

    def test_orphan(self):
        # o app abriu o servidor e caiu (sem fechar ele): o servidor fica órfão
        backend, app_pid, _ = self.comfy_setup()
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertTrue(b.app_seen)                                      # achado pelo pai (o app)
        for p in self.kids:
            if p.pid == app_pid:
                p.kill()
                p.wait(2)
        self.fp.remove(app_pid)
        self.agent.check_apps()
        self.agent.close_at = None                                       # (o caminho do "fechou" é outro teste)
        self.agent.orphan_check(b)                                       # começa a contar
        self.assertIsNone(backend.poll())
        self.age_orphan(b)
        self.agent.orphan_check(b)
        self.assertEqual(backend.wait(3), -signal.SIGTERM)
        self.assertEqual(self.agent.last_action["kind"], "orphan")

    def test_orphan_never_started_by_app_is_left_alone(self):
        # aberto por um script/serviço/agente (sem tty, sem app, usado só por HTTP): nunca é órfão
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertFalse(b.app_seen)
        self.agent.orphan_check(b)
        self.age_orphan(b, 10 ** 4)
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())
        self.assertIsNone(self.agent.orphan_since)
        self.assertIn("was not started by the ComfyUI app", self.logs())

    def test_busy_orphan_is_kept(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        b.app_seen = True
        self.srv.queue_body = b'{"queue_running": [[1]], "queue_pending": [[2]]}'
        self.agent.orphan_check(b)
        self.age_orphan(b)
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())                               # no meio de um trabalho: fica
        self.assertGreater(self.agent.orphan_since, time.monotonic() - 5)    # e o relógio recomeça

    def test_orphan_with_high_cpu_and_no_answer_is_kept(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        b.app_seen = True
        self.fp.set_tcp([])                                              # a fila não responde
        b.cpu_at -= 30
        self.fp.update(backend.pid, cpu=100 + 30 * A.CLK_TCK)           # 1 núcleo inteiro: trabalhando
        self.age_orphan(b)
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())

    def test_orphan_recent_work_or_client_restarts_the_clock(self):
        uid = os.getuid()
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        b.app_seen = True
        self.age_orphan(b)
        b.last_busy = time.monotonic()                                   # a fila andou há pouco
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())
        self.assertGreater(self.agent.orphan_since, time.monotonic() - 5)
        # um script que conecta de vez em quando: visto numa olhada qualquer, recomeça a contar
        self.age_orphan(b, 60)
        self.fp.set_tcp([("127.0.0.1", self.port, "0.0.0.0", 0, 0x0A, uid, 5001),
                         ("127.0.0.1", self.port, "127.0.0.1", 41000, 0x01, uid, 6101),
                         ("127.0.0.1", 41000, "127.0.0.1", self.port, 0x01, uid, 6102)])
        self.agent.orphan_check(b)
        self.assertGreater(self.agent.orphan_since, time.monotonic() - 5)
        self.assertIsNone(backend.poll())

    def test_terminal_seen_once_stays_a_terminal(self):
        # nohup num terminal que depois fechou: o tty vira 0, mas ele continua "seu"
        backend, _, _ = self.comfy_setup(tty=os.makedev(136, 3), with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertTrue(b.has_tty)
        self.fp.update(backend.pid, tty=0)
        self.assertIs(self.agent.scan_backend(), b)
        self.assertTrue(b.has_tty)

    def test_text_console_is_not_a_terminal(self):
        # Hyprland aberto do login no tty1: todo app herda o tty1 (major 4). Isso não é "aberto num terminal"
        backend, _, _ = self.comfy_setup(tty=os.makedev(4, 1), with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.assertFalse(b.has_tty)
        self.close_now()
        self.assertEqual(backend.wait(3), -signal.SIGTERM)

    def test_close_keeps_a_working_server_then_orphan_rule_ends_it(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.srv.queue_body = b'{"queue_running": [[1]], "queue_pending": []}'
        self.close_now()
        self.assertIsNone(backend.poll())
        self.assertIn("still working", self.logs())
        self.assertTrue(b.app_seen)
        self.srv.queue_body = b'{"queue_running": [], "queue_pending": []}'
        self.agent.orphan_check(b)
        self.age_orphan(b)
        self.agent.orphan_check(b)
        self.assertEqual(backend.wait(3), -signal.SIGTERM)

    def test_server_started_after_close_is_left_alone(self):
        # ComfyUI-Manager "Restart" (os.execv): mesmo pid, socket novo, logo depois do app fechar
        uid = os.getuid()
        backend, app_pid, _ = self.comfy_setup()
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": app_pid}])
        self.agent.scan_backend()
        self.hypr.clients = []
        self.hypr.send("closewindow>>1a")
        self.assertTrue(self.pump(lambda: self.agent.close_at is not None))
        for p in self.kids:
            if p.pid == app_pid:
                p.terminate()
                p.wait(2)
        self.fp.remove(app_pid)
        self.agent.check_apps()
        time.sleep(0.01)
        self.fp.add_fd(backend.pid, "socket:[5009]")
        self.fp.set_tcp([("127.0.0.1", self.port, "0.0.0.0", 0, 0x0A, uid, 5009)])
        self.close_now()
        self.assertIsNone(backend.poll())
        self.assertIsNotNone(self.agent.backend)
        self.assertIn("started after that is left alone", self.logs())

    def test_no_orphan_without_hyprland(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        b = self.agent.scan_backend()
        self.agent.orphan_since = time.monotonic() - 1000
        self.agent.orphan_check(b)
        self.assertIsNone(backend.poll())

    def test_server_ended_with_window_open_is_purged_on_close(self):
        # o servidor sai com a janela ainda aberta (o aero-memory fechou ele, ou caiu) e nenhum novo vem:
        # quando a janela fecha, os arquivos dele ainda saem do cache
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": 1}])
        b = self.agent.scan_backend()
        self.assertTrue(b.app_seen)
        backend.terminate()
        backend.wait(2)
        self.fp.set_tcp([])
        self.assertIsNone(self.agent.scan_backend())
        self.assertIsNotNone(self.agent.gone)
        self.assertIsNone(self.agent.close_at)
        self.hypr.clients = []
        self.hypr.send("closewindow>>1a")
        self.assertTrue(self.pump(lambda: self.agent.close_at is not None))
        self.close_now()
        self.assertEqual(self.agent.last_action["files"], 2)

    def test_new_server_forgets_the_old_files(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.start_hypr([{"address": "0x1a", "class": "comfyui", "pid": 1}])
        self.agent.scan_backend()
        self.fp.set_tcp([])
        self.agent.scan_backend()
        self.assertIsNotNone(self.agent.gone)
        self.fp.set_tcp([("127.0.0.1", self.port, "0.0.0.0", 0, 0x0A, os.getuid(), 5001)])
        self.assertIsNotNone(self.agent.scan_backend())                 # reinício: carrega os mesmos arquivos
        self.assertIsNone(self.agent.gone)

    def test_models_mapped_later_are_collected_by_the_scan(self):
        backend, _, models = self.comfy_setup(with_app=False)
        b = self.agent.scan_backend()
        self.assertEqual(len(b.models), 2)
        late = os.path.join(self.tmp.name, "late.safetensors")
        with open(late, "wb") as f:
            f.write(b"\0" * 4096)
        self.fp.add_fd(backend.pid, late)
        self.agent.scan_backend()                                        # a olhada de 10 s
        self.assertEqual(len(b.models), 3)

    def test_requests_only_while_the_same_server_listens(self):
        b = self.simple_backend()
        self.assertEqual(self.agent.ask_queue(b), (0, 0))
        self.fp.set_tcp([])                                              # ele saiu: a porta pode ser de outro
        self.srv.hits.clear()
        self.assertIsNone(self.agent.ask_queue(b))
        self.assertFalse(self.agent.ask_free(b))
        self.assertEqual(self.srv.hits, [])

    def test_server_ended_by_app_then_purge(self):
        backend, _, models = self.comfy_setup(with_app=False)
        self.start_hypr([])
        b = self.agent.scan_backend()
        self.agent.collect_models(b)
        backend.terminate()
        backend.wait(2)
        self.fp.set_tcp([])
        self.assertIsNone(self.agent.scan_backend())
        self.assertIsNotNone(self.agent.close_at)                       # "server ended" agendou a limpeza
        self.close_now()
        self.assertEqual(self.agent.last_action["files"], 2)

    def test_state_file(self):
        self.comfy_setup(with_app=False)
        self.agent.scan_backend()
        self.agent.write_state(force=True)
        with open(self.state) as f:
            text = f.read()
        doc = json.loads(text)
        self.assertTrue(doc["comfy"]["running"])
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.assertNotIn(self.tmp.name, text)
        self.assertNotIn(os.path.expanduser("~"), text)
        self.agent.shutdown()
        self.assertFalse(os.path.exists(self.state))                    # sai junto com o agente


class TestAgentUnload(AgentCase):
    def setUp(self):
        super().setUp()
        s = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        s.hits, s.mode, s.other_port = [], "ok", 0
        s.queue_body = b'{"queue_running": [], "queue_pending": []}'
        t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        t.start()
        self.srv = s
        self.addCleanup(lambda: (s.shutdown(), s.server_close(), t.join(2)))
        self.agent.conf["comfy_port"] = s.server_address[1]

    def setup_backend(self):
        uid = os.getuid()
        backend = spawn_sleeper()
        self.kids.append(backend)
        self.fp.add(backend.pid, "python3", start=real_start(backend.pid), argv=("python3", "main.py"),
                    cwd="/srv/ComfyUI", fds=["socket:[5001]"])
        self.fp.set_tcp([("127.0.0.1", self.agent.conf["comfy_port"], "0.0.0.0", 0, 0x0A, uid, 5001)])
        return self.agent.scan_backend()

    def posts(self):
        return [h for h in self.srv.hits if h[0] == "POST"]

    def test_idle_unload_once_per_idle_period(self):
        b = self.setup_backend()
        self.agent.queue_tick(b)
        self.assertEqual(self.posts(), [])                               # acabou de abrir: não está parado
        b.last_busy -= 16 * 60
        self.agent.queue_tick(b)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.agent.free_note["kind"], "idle_unload")
        self.assertIsNone(self.agent.last_action)                        # só depois de ver a RAM sair
        self.fp.update(b.pid, rss_mb=1024)
        self.agent.confirm_free()
        self.assertEqual(self.agent.last_action["kind"], "idle_unload")
        self.agent.free_rate = A.RateLimit(0)
        self.agent.queue_tick(b)
        self.assertEqual(len(self.posts()), 1)                           # o mesmo período: não repete
        self.srv.queue_body = b'{"queue_running": [[1]], "queue_pending": []}'
        self.agent.queue_tick(b)                                         # trabalhou: rearma
        self.srv.queue_body = b'{"queue_running": [], "queue_pending": []}'
        b.last_busy -= 16 * 60
        self.agent.queue_tick(b)
        self.assertEqual(len(self.posts()), 2)

    def test_never_unload_while_busy(self):
        b = self.setup_backend()
        self.srv.queue_body = b'{"queue_running": [], "queue_pending": [[1]]}'
        b.last_busy -= 16 * 60
        b.idle = True
        self.assertFalse(self.agent.free_models(b, "games", "x", "y"))
        self.assertEqual(self.posts(), [])

    def test_pressure_event(self):
        b = self.setup_backend()
        self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(len(self.posts()), 1)
        self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(len(self.posts()), 1)                           # 1 por minuto
        self.assertEqual(self.agent.free_note["kind"], "unload")
        self.assertFalse(b.models_loaded)
        self.agent.confirm_free()                                        # nada saiu: não vira "última ação"
        self.assertIsNone(self.agent.last_action)

    def test_zero_minutes_means_never(self):
        b = self.setup_backend()
        self.agent.conf["comfy_idle_unload_min"] = 0
        b.last_busy -= 10 ** 6
        self.agent.queue_tick(b)
        self.assertEqual(self.posts(), [])


class TestAgentGpu(AgentCase):
    def test_games_baseline_learned_before_models_load(self):
        # a base ("o que o sistema sempre usa") vem de antes do jogo: senão o próprio jogo virava a base
        b = self.simple_backend()
        self.fake_gpu("active")
        self.smi_says(f"{b.pid}, 300\n", "500\n")             # só o contexto CUDA; o resto usa 200
        self.agent.gpu_tick(b)
        self.assertEqual(b.other_base, 200)
        self.assertEqual(self.posts(), [])
        b.idle = True                                          # modelo carregado, parado; um jogo abre
        self.smi_says(f"{b.pid}, 5000\n", "7700\n")
        self.agent.gpu_tick(b)
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.agent.free_note["kind"], "games")
        self.agent.free_rate = A.RateLimit(0)
        self.agent.gpu_tick(b)                                 # o mesmo jogo: uma vez só
        self.assertEqual(len(self.posts()), 1)

    def test_sleeping_gpu_is_not_woken(self):
        # nem nvidia-smi nem POST /free (que chama CUDA) com a NVIDIA dormindo e nada grande na RAM
        b = self.simple_backend()
        self.fake_gpu("suspended")
        self.fp.update(b.pid, anon_mb=900)
        self.agent.sample(b)
        self.agent.gpu_tick(b)
        self.agent.queue_tick(b)
        self.assertTrue(b.idle)
        self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(self.posts(), [])
        self.assertEqual(b.vram_mb, 0)
        # 6 GB na RAM: aí tem modelo de verdade e vale o pedido
        self.fp.update(b.pid, anon_mb=6000)
        self.agent.sample(b)
        self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(self.smi_calls(), [])

    def test_pressure_unloads_once_until_comfy_works_again(self):
        # o aero-memory repete "pressure" a cada minuto: um POST /free só, até o ComfyUI trabalhar de novo
        b = self.simple_backend()
        self.agent.free_rate = A.RateLimit(0)
        for _ in range(5):
            self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(len(self.posts()), 1)
        self.assertFalse(b.pressure_armed)
        self.agent.confirm_free()                              # nada saiu: sem aviso, sem "última ação"
        self.assertIsNone(self.agent.last_action)
        self.srv.queue_body = b'{"queue_running": [[1]], "queue_pending": []}'
        self.agent.queue_tick(b)                               # trabalhou: pode ter carregado de novo
        self.srv.queue_body = b'{"queue_running": [], "queue_pending": []}'
        self.agent.queue_tick(b)
        self.agent.on_memory_event({"t": time.time(), "kind": "pressure"})
        self.assertEqual(len(self.posts()), 2)

    def test_stuck_nvidia_smi_never_blocks(self):
        # nvidia-smi preso no driver: mata, não espera por ele, e não chama mais por um tempo
        self.fake_gpu("active")
        with open(self.agent.smi, "w") as f:
            f.write('#!/bin/sh\nd=$(dirname "$0")\necho "$1" >> "$d/calls.txt"\nexec sleep 30\n')
        old = A.SMI_TIMEOUT_S
        A.SMI_TIMEOUT_S = 0.5
        try:
            t0 = time.monotonic()
            self.assertIsNone(self.agent.run_smi(["--query-gpu=memory.used"]))
            self.assertLess(time.monotonic() - t0, 2.5)
            parked = self.agent.smi_parked
            self.assertIsNotNone(parked)
            self.assertIsNone(self.agent.run_smi(["--query-gpu=memory.used"]))
            self.assertEqual(len(self.smi_calls()), 1)
            parked.wait(5)                     # aqui o SIGKILL vale na hora; com o driver preso, demora
            self.agent.reap_smi()
            self.assertIsNone(self.agent.smi_parked)
            self.assertIsNone(self.agent.run_smi(["--query-gpu=memory.used"]))     # ainda na pausa
            self.assertEqual(len(self.smi_calls()), 1)
            self.assertIn("GPU checks paused", self.logs())
        finally:
            A.SMI_TIMEOUT_S = old


class TestAgentMisc(AgentCase):
    def test_other_listener_not_rescanned(self):
        self.comfy_setup(cmd=("python3", "-m", "http.server", "8188"), cwd_name="srv", with_app=False)
        calls = []
        real = A.find_inode_owners

        def counting(*a, **k):
            calls.append(1)
            return real(*a, **k)
        A.find_inode_owners = counting
        try:
            for _ in range(5):
                self.assertIsNone(self.agent.scan_backend())
        finally:
            A.find_inode_owners = real
        self.assertEqual(len(calls), 1)                  # a varredura cara roda uma vez só
        self.assertEqual(self.logs().count("which is not ComfyUI"), 1)

    def test_reload_with_new_port_forgets_server(self):
        backend, _, _ = self.comfy_setup(with_app=False)
        self.agent.scan_backend()
        with open(self.agent.conf_path, "w") as f:
            f.write("notify=0\ncomfy_port=8199\n")
        self.agent.reload = True
        self.agent.step(timeout_ms=0)
        self.assertEqual(self.agent.conf["comfy_port"], 8199)
        self.assertIsNone(self.agent.backend)
        self.assertIsNone(self.agent.gone)
        self.assertIsNone(self.agent.close_at)           # não confundiu com "o servidor fechou"
        self.assertIsNone(backend.poll())

    def test_dry_run_unload_says_would(self):
        self.agent.dry = True
        b = A.Backend(123, 1, 1, "python3", False, False, time.monotonic())
        self.agent.ask_queue = lambda b: (0, 0)          # fila vazia (o servidor 123 é de mentira)
        self.agent.ask_free = lambda b: self.fail("dry-run sent POST /free")
        self.assertTrue(self.agent.free_models(b, "idle_unload", "x", "y"))
        logs = self.logs()
        self.assertIn("[dry-run] would unload", logs)
        self.assertNotIn("models unloaded (idle_unload)", logs)

    def test_memory_events_only_own_uid(self):
        # o aero-memory vê todos os usuários: cada agente só avisa dos processos do próprio usuário
        seen = []
        self.agent.notify = lambda *a: seen.append(a)
        uid = os.getuid()
        for kind in ("paused", "resumed", "killed"):
            self.agent.on_memory_event({"t": 1.0, "kind": kind, "name": "x", "names": ["x"], "uid": uid + 1})
            self.agent.on_memory_event({"t": 1.0, "kind": kind, "name": "x", "names": ["x"]})      # sem dono
        self.assertEqual(seen, [])
        for kind in ("paused", "resumed", "killed"):
            self.agent.on_memory_event({"t": 1.0, "kind": kind, "name": "x", "names": ["x"], "uid": uid})
        self.assertEqual([s[0] for s in seen], ["paused", "resumed", "killed"])
        # retomado porque o serviço parou ou recarregou: não é "voltou ao normal"
        for reason in ("stopping", "reload", "leftover", "disabled"):
            self.agent.on_memory_event({"t": 1.0, "kind": "resumed", "names": ["x"], "uid": uid, "reason": reason})
        self.assertEqual(len(seen), 3)
        # o kernel fechou algo: o evento não tem dono e o aviso não tem nome (vale pra todos)
        self.agent.on_memory_event({"t": 1.0, "kind": "oom_kill", "count": 1})
        self.assertEqual(seen[-1][:2], ("oom_kill", "Memory ran out"))

    def test_bounded_structures(self):
        self.start_hypr([])
        for i in range(A.MAX_WINDOWS + 50):
            self.agent.on_hypr_line(f"openwindow>>{i + 1:x},1,comfyui,t")
        self.assertLessEqual(len(self.agent.windows), A.MAX_WINDOWS)
        for i in range(100):
            self.agent.say_once(f"k{i}", "x")
        self.assertLessEqual(len(self.agent.said), 65)


def steady_fds(pid, samples=8):
    """Quantos fds o processo tem parado (o menor de várias olhadas: ignora um arquivo aberto no meio
    de uma leitura)."""
    counts = []
    for _ in range(samples):
        counts.append(len(os.listdir(f"/proc/{pid}/fd")))
        time.sleep(0.1)
    return min(counts)


def read_until(fd, buf, needle, timeout=5):
    """Lê o stderr (fd cru) de um processo até aparecer `needle`. -> texto lido até agora."""
    import select as sel
    deadline = time.monotonic() + timeout
    while needle not in buf[0] and time.monotonic() < deadline:
        r, _, _ = sel.select([fd], [], [], 0.1)
        if r:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            buf[0] += chunk.decode("utf-8", "replace")
    return needle in buf[0]


class TestCli(unittest.TestCase):
    """O script de verdade num subprocesso, contra o Hyprland falso (XDG_RUNTIME_DIR relativo)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)                      # caminho relativo: o do socket tem limite de tamanho
        self.addCleanup(os.chdir, self.old_cwd)
        os.mkdir("rt", 0o700)
        self.hypr = FakeHypr(os.path.join("rt", "hypr", "cli_1_1"))
        self.addCleanup(self.hypr.close)
        self.hypr.clients = [{"address": "0x1a", "class": "comfyui", "pid": 1},
                             {"address": "0x2b", "class": "kitty", "pid": 1},
                             {"address": "0x3c", "class": "firefox", "pid": 1}]
        self.env = dict(os.environ, XDG_RUNTIME_DIR="rt", PYTHONDONTWRITEBYTECODE="1")
        self.env.pop("HYPRLAND_INSTANCE_SIGNATURE", None)
        self.conf = os.path.join(self.tmp.name, "agent.conf")
        with open(self.conf, "w") as f:
            f.write("notify=1\n")
        self.events = os.path.join(self.tmp.name, "events.jsonl")
        open(self.events, "w").close()

    def test_check(self):
        r = subprocess.run([sys.executable, SCRIPT, "--check", "--config", self.conf, "--events", self.events],
                           cwd=self.tmp.name, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("hyprland: running, 3 windows, 1 of ComfyUI", r.stdout)
        self.assertIn("memory guard events: present", r.stdout)
        self.assertNotIn(self.tmp.name, r.stdout + r.stderr)

    def test_dry_run_lifecycle(self):
        p = subprocess.Popen([sys.executable, SCRIPT, "--dry-run", "--config", self.conf, "--events", self.events],
                             cwd=self.tmp.name, env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        fd, buf = p.stderr.fileno(), [""]
        try:
            self.assertTrue(read_until(fd, buf, "1 of ComfyUI"), buf[0])
            self.assertTrue(self.hypr.wait_conn())
            base_fds = steady_fds(p.pid)
            self.hypr.send("openwindow>>4d,1,firefox,a title, with commas")
            self.assertTrue(read_until(fd, buf, "window opened (ComfyUI: no)"), buf[0])
            # enxurrada de eventos (títulos mudando, linhas gigantes): nada cresce
            self.hypr.send(*(["windowtitlev2>>4d,t" + "x" * 50] * 2000 + ["activewindow>>" + "y" * 20000]))
            self.hypr.send("closewindow>>4d")
            self.assertTrue(read_until(fd, buf, "window closed (ComfyUI: no)"), buf[0])
            with open(self.events, "a") as f:
                # de outro usuário e de um aero-memory em --dry-run: nada; o deste usuário: avisa
                t = time.time() + 1
                for ev in ({"t": t, "kind": "paused", "names": ["someone_else"], "uid": os.getuid() + 1},
                           {"t": t, "kind": "killed", "name": "firefox", "uid": os.getuid(), "dry_run": True},
                           {"t": t, "kind": "resumed", "names": ["x"], "uid": os.getuid(), "reason": "stopping"},
                           {"t": t, "kind": "paused", "names": ["fossilize_replay"], "uid": os.getuid()}):
                    f.write(json.dumps(ev) + "\n")
            self.assertTrue(read_until(fd, buf, "would notify: Memory is low"), buf[0])
            self.assertNotIn("someone_else", buf[0])
            self.assertNotIn("Memory was almost full", buf[0])
            self.assertNotIn("back to normal", buf[0])
            p.send_signal(signal.SIGHUP)
            self.assertTrue(read_until(fd, buf, "configuration reloaded"), buf[0])
            self.assertEqual(steady_fds(p.pid), base_fds)
            with open(f"/proc/{p.pid}/status") as f:
                threads = [l for l in f if l.startswith("Threads:")][0].split()[1]
            self.assertEqual(threads, "1")
            self.assertEqual(subprocess.run(["ps", "--ppid", str(p.pid), "--no-headers"],
                                            capture_output=True, text=True).stdout.strip(), "")
            self.hypr.drop()                                   # Hyprland "reinicia"
            self.assertTrue(read_until(fd, buf, "reconnecting"), buf[0])
            self.assertTrue(self.hypr.wait_conn(), buf[0])     # voltou sozinho (espera curta no começo)
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(10), 0)
            read_until(fd, buf, "stopped", timeout=2)
            self.assertIn("stopped", buf[0])
            self.assertNotIn("error in", buf[0])
            self.assertNotIn(self.tmp.name, buf[0])
        finally:
            if p.poll() is None:
                p.kill()
                p.wait(5)
            p.stderr.close()


class TestUnit(unittest.TestCase):
    def test_unit(self):
        import configparser
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str
        with open(UNIT) as f:
            cp.read_file(f)
        u, s = cp["Unit"], cp["Service"]
        self.assertEqual(s["ExecStart"], "%h/.local/bin/aero-memory-agent")
        self.assertEqual(s["Restart"], "always")
        self.assertIn("-HUP $MAINPID", s["ExecReload"])        # o Settings usa reload: SIGHUP relê a config
        self.assertEqual(cp["Install"]["WantedBy"], "default.target")
        # caindo em seguida (binário sumiu, erro na partida), o limite tem que ser alcançável: para em
        # "failed" em vez de reiniciar pra sempre (com RestartSec=3, o padrão de 5 em 10 s nunca chega)
        burst, interval, every = int(u["StartLimitBurst"]), int(u["StartLimitIntervalSec"]), int(s["RestartSec"])
        self.assertLess(burst * every, interval)


class TestNoPersonalData(unittest.TestCase):
    """O repositório é público: nada de nome de usuário, pasta pessoal, máquina."""

    def files(self):
        out = [SCRIPT, UNIT, __file__]
        out += [os.path.join(FIX, f) for f in os.listdir(FIX)]
        return out

    def test_no_personal_paths(self):
        user = pwd.getpwuid(os.getuid()).pw_name
        host = socket.gethostname()
        home = os.path.expanduser("~")
        for path in self.files():
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            self.assertNotIn("/" + "home/", text, path)
            self.assertNotIn("/" + "mnt/", text, path)
            if home not in ("/", "/root"):
                self.assertNotIn(home, text, path)
            if len(user) >= 4 and user not in ("root", "user", "test", "admin"):
                self.assertNotIn(user, text, path)
            if len(host) >= 4 and host not in ("localhost", "archlinux"):
                self.assertNotIn(host, text, path)

    def test_no_shell_true(self):
        with open(SCRIPT) as f:
            text = f.read()
        self.assertNotIn("shell" + "=True", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("os.popen", text)


if __name__ == "__main__":
    unittest.main()
