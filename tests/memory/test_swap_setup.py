"""Testes do system/memory/aero-swap-setup (bash).

As funções puras e as decisões são testadas carregando o script com "source" num bash
separado (o main só roda quando o script é executado direto). A CLI é testada como usuário
comum: só leitura (dry-run, --status) e as recusas (--apply sem root, tamanho inválido).
Rodar: python3 -m unittest discover -s tests/memory -p 'test_swap_setup*.py'
"""
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "system" / "memory" / "aero-swap-setup"
GIB = 1024 ** 3
LINE = "/swap/swapfile none swap defaults,nofail,discard,pri=10 0 0"
TAG = "# aero-swap-setup: SSD swap tier (low priority; compressed RAM swap is used first)"


def sh(code, *args):
    """Roda CODE num bash com o script carregado; $1.. = args. Devolve (rc, stdout, stderr)."""
    p = subprocess.run(
        ["bash", "-c", 'source "$1"; shift; ' + code, "harness", str(SCRIPT), *args],
        capture_output=True, text=True, timeout=30,
    )
    return p.returncode, p.stdout, p.stderr


def ok(code, *args):
    return sh(code, *args)[0] == 0


PLAN_VARS = ("P_CONVERT P_MKSUBVOL P_MKDIR P_MARKER P_ADOPT P_SWAPOFF P_RMFILE P_CREATE P_FIXPERM "
             "P_REPRIO P_SWAPON P_FSTAB P_CHECK P_NOHIB P_FSTAB_RM P_FSTAB_RESTORE P_RMMARKER "
             "P_RMSUBVOL P_RMDIR P_NOHIB_RM").split()

# um btrfs de 1 TB com 500 GB livres, sem /swap, sem swap no SSD, 32 GB de RAM (16 disponíveis),
# initramfs que não volta de hibernação, sem o arquivo de bloqueio da hibernação
BASE = dict(
    F_FSTYPE="btrfs", F_DEVS="1", F_PROFILE="single", F_SWAPDIR="missing", F_SWAPDIR_SAFE="1",
    F_EXTRA="0", F_MARKER="none", F_FILE="missing", F_FILE_BYTES="0", F_FILE_GOOD="unknown",
    F_FILE_PERM="unknown", F_ACTIVE="0", F_USED_KIB="0", F_ACTIVE_PRIO="", F_FOREIGN="",
    F_FSTAB="none", F_AVAIL=str(500 * GIB), F_TOTAL=str(1000 * GIB),
    F_MEMAVAIL_KIB=str(16 * 1024 * 1024), F_MEMTOTAL_KIB=str(32 * 1024 * 1024), F_WARN_PCT="15",
    F_RESUME="none", F_SLEEP="missing", WANT_BYTES=str(16 * GIB), SIZE="16", SIZE_SET="0",
)
# o mesmo sistema com tudo já instalado
DONE = dict(BASE, F_SWAPDIR="subvol", F_MARKER="subvol", F_FILE="regular",
            F_FILE_BYTES=str(16 * GIB), F_FILE_GOOD="1", F_FILE_PERM="1", F_ACTIVE="1",
            F_ACTIVE_PRIO="10", F_FSTAB="exact", F_SLEEP="same")


def decide(kind, **facts):
    f = dict(BASE, **facts)
    assigns = "; ".join(f"{k}={shlex.quote(v)}" for k, v in f.items())
    code = (assigns + f"; decide_{kind}; for v in {' '.join(PLAN_VARS)}; do "
            'printf "%s=%s\\n" "$v" "${!v}"; done; echo "REFUSE=$REFUSE"; '
            'echo "NOTES=${#NOTES[@]}"; echo "REASON=$REASON"')
    rc, out, err = sh(code)
    assert rc == 0, (rc, out, err)
    vals = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    plan = {k for k in PLAN_VARS if vals[k] == "1"}
    return plan, int(vals["REFUSE"]), int(vals["NOTES"]), vals["REASON"]


class PureFunctions(unittest.TestCase):
    def test_valid_size(self):
        for good in ("4", "16", "32", "64"):
            self.assertTrue(ok('valid_size "$1"', good), good)
        for bad in ("3", "65", "0", "08", "016", "abc", "", "16 ", " 16", "1e1", "-4", "4.5", "99", "100"):
            self.assertFalse(ok('valid_size "$1"', bad), repr(bad))

    def test_parse_swaps_and_zram(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as t:
            t.write("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"
                    "/dev/zram0                              partition\t4194300\t\t1024\t\t100\n"
                    "/swap/swapfile                          file\t\t16777212\t0\t\t10\n")
        try:
            rc, out, _ = sh('parse_swaps "$1"', t.name)
            self.assertEqual(rc, 0)
            self.assertEqual(out.splitlines(), ["/dev/zram0 partition 4194300 1024 100",
                                                "/swap/swapfile file 16777212 0 10"])
        finally:
            os.unlink(t.name)
        self.assertTrue(ok('is_zram /dev/zram0'))
        self.assertTrue(ok('is_zram /dev/zram12'))
        self.assertFalse(ok('is_zram /dev/zram'))
        self.assertFalse(ok('is_zram /swap/swapfile'))
        self.assertFalse(ok('is_zram /dev/zram0x'))

    def test_meminfo(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as t:
            t.write("MemTotal:       32000000 kB\nMemFree:  100 kB\nMemAvailable:   20000000 kB\n")
        try:
            self.assertEqual(sh('meminfo_kib "$1" MemAvailable', t.name)[1].strip(), "20000000")
            self.assertEqual(sh('meminfo_kib "$1" Nope', t.name)[1].strip(), "")
        finally:
            os.unlink(t.name)

    def test_space_ok(self):
        # 1000 total, 20%: precisa sobrar 200
        self.assertTrue(ok("space_ok 400 1000 200 20"))
        self.assertFalse(ok("space_ok 399 1000 200 20"))
        self.assertFalse(ok("space_ok 100 1000 200 20"))
        self.assertTrue(ok(f"space_ok {500 * GIB} {1000 * GIB} {64 * GIB} 20"))

    def test_swapoff_safe(self):
        self.assertTrue(ok("swapoff_safe 0 1048576 1048576"))
        self.assertFalse(ok("swapoff_safe 1 1048576 1048576"))
        self.assertTrue(ok(f"swapoff_safe {4 * 1048576} {6 * 1048576} 1048576"))
        self.assertFalse(ok(f"swapoff_safe {6 * 1048576} {6 * 1048576} 1048576"))
        self.assertFalse(ok(f"swapoff_safe {4 * 1048576} {6 * 1048576} {3 * 1048576}"))

    def test_swapoff_reserve(self):
        # a % de aviso do vigia sobre a RAM total; nunca menos de 1 GB
        self.assertEqual(sh("swapoff_reserve_kib 33554432 15")[1].strip(), str(33554432 * 15 // 100))
        self.assertEqual(sh("swapoff_reserve_kib 4194304 15")[1].strip(), "1048576")
        self.assertEqual(sh("swapoff_reserve_kib 0 15")[1].strip(), "1048576")
        # a folga fica acima do "critical" do aero-memory (critical_available=4% da RAM)
        for total in (8 * 1048576, 32 * 1048576, 64 * 1048576):
            r = int(sh(f"swapoff_reserve_kib {total} 15")[1])
            self.assertGreater(r, total * 4 // 100)

    def test_guard_warn_pct(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d, "memory.conf")
            pct = lambda: sh('guard_warn_pct "$1"', str(conf))[1].strip()
            self.assertEqual(pct(), "15")                                  # sem arquivo
            conf.write_text("# x\nwarn_available = 20 # comment\n")
            self.assertEqual(pct(), "20")
            conf.write_text("warn_available=20\nwarn_available=30\n")    # vale a última
            self.assertEqual(pct(), "30")
            for bad in ("x", "", "051", "60", "0", "-5", "1e1"):
                conf.write_text(f"warn_available={bad}\n")
                self.assertEqual(pct(), "15", bad)
            conf.write_text("warn_available=3\ncritical_available=2\n")   # no mínimo 5
            self.assertEqual(pct(), "5")
            conf.write_text("warn_available=10\ncritical_available=10\n")  # como o vigia: padrões
            self.assertEqual(pct(), "15")
            conf.write_text("enabled=1\n")
            self.assertEqual(pct(), "15")
            # como o aero-memory, só as primeiras 500 linhas contam
            conf.write_text("# x\n" * 500 + "warn_available=30\n")
            self.assertEqual(pct(), "15")

    def test_guard_warn_pct_matches_the_guard(self):
        # o mesmo memory.conf tem que dar o mesmo warn_available aqui e no aero-memory (componente B)
        guard = REPO / "system" / "memory" / "aero-memory"
        if not guard.is_file():
            self.skipTest("aero-memory is not in the repo")
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("aero_memory_for_swap_test", str(guard))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        cases = ["", "warn_available=20\n", "warn_available = 25 # c\ncritical_available=3\n",
                 "warn_available=20\nwarn_available=x\n", "warn_available=x\nwarn_available=20\n",
                 "warn_available=10\ncritical_available=10\n", "warn_available=051\n",
                 "warn_available=50\n", "warn_available=\n", "critical_available=20\n",
                 "warn_available=8\ncritical_available=2\n", "warn_available=2 0\n"]
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d, "memory.conf")
            for text in cases:
                conf.write_text(text)
                want = max(5, mod.parse_config(text)[0]["warn_available"])
                self.assertEqual(sh('guard_warn_pct "$1"', str(conf))[1].strip(), str(want), repr(text))

    def test_size_matches(self):
        self.assertTrue(ok(f"size_matches {16 * GIB} {16 * GIB}"))
        # /proc/swaps conta sem a página do cabeçalho
        self.assertTrue(ok(f"size_matches {16777212 * 1024 + 4096} {16 * GIB}"))
        self.assertTrue(ok(f"size_matches {16 * GIB - 1048576} {16 * GIB}"))
        self.assertFalse(ok(f"size_matches {8 * GIB} {16 * GIB}"))

    def test_btrfs_profile(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "data")
            self.assertEqual(sh('btrfs_data_profile "$1"', str(p))[1].strip(), "unknown")
            p.mkdir()
            (p / "flags").write_text("1\n")
            self.assertEqual(sh('btrfs_data_profile "$1"', str(p))[1].strip(), "unknown")
            (p / "single").mkdir()
            self.assertEqual(sh('btrfs_data_profile "$1"', str(p))[1].strip(), "single")
            (p / "raid1").mkdir()
            self.assertEqual(sh('btrfs_data_profile "$1"', str(p))[1].strip(), "multi")
            (p / "single").rmdir()
            self.assertEqual(sh('btrfs_data_profile "$1"', str(p))[1].strip(), "multi")


class RunHelper(unittest.TestCase):
    def test_failure_is_exit_1(self):
        # o código do próprio comando (ex.: 4 do swapon) não pode se confundir com os nossos
        for code in (2, 4, 5, 6, 32):
            rc, out, err = sh(f'run bash -c "exit {code}"; echo not-reached')
            self.assertEqual(rc, 1, code)
            self.assertNotIn("not-reached", out)
            self.assertIn(f"exit {code}", err)
        rc, out, _ = sh('run true; echo after')
        self.assertEqual((rc, out.splitlines()[-1]), (0, "after"))


class Fstab(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name, "fstab")

    def tearDown(self):
        self.dir.cleanup()

    def state(self, text):
        self.path.write_text(text)
        rc, out, _ = sh('fstab_state "$1"', str(self.path))
        self.assertEqual(rc, 0)
        return out.strip()

    def test_state(self):
        base = "# comment\nUUID=x / btrfs defaults 0 0\n"
        self.assertEqual(self.state(base), "none")
        self.assertEqual(self.state(base + LINE + "\n"), "exact")
        self.assertEqual(self.state(base + "/swap/swapfile\tnone  swap\tdefaults,nofail,discard,pri=10\n"), "exact")
        # a linha de antes (sem discard) é reescrita pelo setup
        self.assertEqual(self.state(base + "/swap/swapfile none swap defaults,nofail,pri=10 0 0\n"), "other")
        self.assertEqual(self.state(base + "/swap/swapfile none swap defaults 0 0\n"), "other")
        self.assertEqual(self.state(base + LINE + "\n" + LINE + "\n"), "other")
        self.assertEqual(self.state(base + "#" + LINE + "\n"), "none")
        self.assertEqual(self.state(""), "none")

    def test_add_is_idempotent_and_keeps_the_rest(self):
        original = "# /etc/fstab\nUUID=a / btrfs rw 0 0\n\n# keep me\nUUID=b /home btrfs rw 0 0"  # sem \n no fim
        self.path.write_text(original)
        once = sh('fstab_with_ours "$1"', str(self.path))[1]
        self.path.write_text(once)
        twice = sh('fstab_with_ours "$1"', str(self.path))[1]
        self.assertEqual(once, twice)
        self.assertEqual(once.count(LINE), 1)
        self.assertEqual(once.count(TAG), 1)
        self.assertTrue(once.startswith(original + "\n"))
        self.assertTrue(once.endswith(TAG + "\n" + LINE + "\n"))
        self.assertEqual(self.state(once), "exact")

    def test_add_replaces_a_different_line(self):
        self.path.write_text("UUID=a / btrfs rw 0 0\n/swap/swapfile none swap defaults 0 0\nUUID=b /x ext4 rw 0 2\n")
        out = sh('fstab_with_ours "$1"', str(self.path))[1]
        self.assertNotIn("/swap/swapfile none swap defaults 0 0", out)
        self.assertIn("UUID=b /x ext4 rw 0 2\n", out)
        self.assertEqual(self.state(out), "exact")

    def test_remove(self):
        original = "# /etc/fstab\nUUID=a / btrfs rw 0 0\n# my old note: /swap/swapfile\n"
        self.path.write_text(original + TAG + "\n" + LINE + "\n")
        out = sh('fstab_without_ours "$1"', str(self.path))[1]
        self.assertEqual(out, original)
        self.path.write_text(out)
        self.assertEqual(sh('fstab_without_ours "$1"', str(self.path))[1], original)


@unittest.skipIf(os.geteuid() == 0, "as root this would run systemctl daemon-reload")
class WriteFstab(unittest.TestCase):
    """write_fstab de verdade num fstab de uma pasta temporária (FSTAB trocado)."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.fstab = Path(self.dir.name, "fstab")
        self.original = "# test fstab\ntmpfs /tmp tmpfs defaults 0 0\n"
        self.fstab.write_text(self.original)
        self.fstab.chmod(0o644)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, gen, extra=""):
        code = (f'FSTAB={shlex.quote(str(self.fstab))}; trap cleanup EXIT; {extra} '
                f'write_fstab {gen}; echo finished')
        return sh(code)

    def others(self):
        return sorted(p.name for p in Path(self.dir.name).iterdir() if p.name != "fstab")

    def test_add_then_nothing_then_remove(self):
        rc, out, err = self.write("fstab_with_ours")
        self.assertEqual(rc, 0, err)
        self.assertIn("finished", out)
        text = self.fstab.read_text()
        self.assertTrue(text.startswith(self.original))
        self.assertEqual(text.count(LINE), 1)
        self.assertEqual(stat_mode(self.fstab), 0o644)
        backups = self.others()
        self.assertEqual(len(backups), 1)
        self.assertRegex(backups[0], r"^fstab\.bak-aero-\d{8}-\d{6}")
        self.assertEqual(Path(self.dir.name, backups[0]).read_text(), self.original)

        rc, out, _ = self.write("fstab_with_ours")
        self.assertEqual(rc, 0)
        self.assertIn("already right", out)
        self.assertEqual(self.others(), backups)          # sem cópia nova

        rc, _, err = self.write("fstab_without_ours")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertEqual(len(self.others()), 2)
        self.assertFalse([n for n in self.others() if n.startswith(".fstab.aero-swap.")])

    def test_refuses_an_fstab_that_fails_verify(self):
        bad = 'bad_gen() { cat "$1"; echo "/nonexistent-aero-test /mnt/nonexistent-aero-test ext4 defaults 0 0"; };'
        rc, out, err = self.write("bad_gen", extra=bad)
        self.assertEqual(rc, 1)
        self.assertNotIn("finished", out)
        self.assertIn("NOT changed", err)
        self.assertEqual(self.fstab.read_text(), self.original)
        self.assertEqual(self.others(), [])                # nem backup nem temporário

    def test_refuses_a_symlinked_fstab(self):
        real = Path(self.dir.name, "real")
        real.write_text(self.original)
        self.fstab.unlink()
        self.fstab.symlink_to(real)
        rc, out, err = self.write("fstab_with_ours")
        self.assertEqual(rc, 1)
        self.assertIn("is a link", err)
        self.assertEqual(real.read_text(), self.original)


def stat_mode(p):
    return os.stat(p).st_mode & 0o7777


def wait_for_child(pid, comm, timeout=10.0):
    """True quando o processo PID tem um filho com esse comm (lido de /proc, sem sleep fixo)."""
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        try:
            filhos = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
        except OSError:
            return False
        for c in filhos:
            try:
                if Path(f"/proc/{c}/comm").read_text().strip() == comm:
                    return True
            except OSError:
                pass
        time.sleep(0.02)
    return False


class DecideSetup(unittest.TestCase):
    def test_fresh_btrfs(self):
        plan, refuse, _, _ = decide("setup")
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, {"P_NOHIB", "P_MKSUBVOL", "P_MARKER", "P_CREATE", "P_SWAPON", "P_FSTAB"})

    def test_fresh_ext4_and_xfs(self):
        for fs in ("ext4", "xfs"):
            plan, refuse, _, _ = decide("setup", F_FSTYPE=fs, F_DEVS="unknown", F_PROFILE="unknown")
            self.assertEqual(refuse, 0)
            self.assertEqual(plan, {"P_NOHIB", "P_MKDIR", "P_MARKER", "P_CREATE", "P_SWAPON", "P_FSTAB"})

    def test_unsupported_fs(self):
        for fs in ("tmpfs", "vfat", "zfs", "ntfs3", "f2fs", ""):
            plan, refuse, _, _ = decide("setup", F_FSTYPE=fs)
            self.assertEqual(refuse, 4, fs)

    def test_btrfs_limits(self):
        self.assertEqual(decide("setup", F_DEVS="2")[1], 4)
        self.assertEqual(decide("setup", F_PROFILE="multi")[1], 4)
        # sem dados do sysfs não recusa (o kernel recusaria no swapon e o script desfaz)
        self.assertEqual(decide("setup", F_DEVS="unknown", F_PROFILE="unknown")[1], 0)

    def test_foreign_swap_refused(self):
        self.assertEqual(decide("setup", F_FOREIGN="/dev/nvme0n1p3")[1], 4)

    def test_already_done(self):
        plan, refuse, notes, _ = decide("setup", **DONE)
        self.assertEqual((plan, refuse, notes), (set(), 0, 0))

    def test_fstab_fix_only(self):
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FSTAB="other"))
        self.assertEqual((plan, refuse), ({"P_FSTAB"}, 0))

    def test_resize_when_ram_allows(self):
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FILE_BYTES=str(8 * GIB), F_USED_KIB="1048576"))
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, {"P_SWAPOFF", "P_RMFILE", "P_CREATE", "P_SWAPON"})

    def test_resize_refused_when_busy(self):
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FILE_BYTES=str(8 * GIB),
                                                    F_USED_KIB=str(6 * 1048576),
                                                    F_MEMAVAIL_KIB=str(6 * 1048576)))
        self.assertEqual(refuse, 6)

    def test_not_enough_space(self):
        # 1000 GB, 210 GB livres: 16 GB deixaria 194 GB (< 20%)
        self.assertEqual(decide("setup", F_AVAIL=str(210 * GIB))[1], 5)
        self.assertEqual(decide("setup", F_AVAIL=str(216 * GIB))[1], 0)
        # num resize conta o espaço do arquivo velho: 195 + 16 (velho) - 8 (novo) = 203 GB ≥ 20%
        # (sem contar o velho seriam 187 GB e recusaria)
        r = decide("setup", **dict(DONE, F_AVAIL=str(195 * GIB), F_FILE_BYTES=str(16 * GIB),
                                   WANT_BYTES=str(8 * GIB), SIZE="8"))
        self.assertEqual(r[:2], ({"P_SWAPOFF", "P_RMFILE", "P_CREATE", "P_SWAPON"}, 0))
        r = decide("setup", **dict(DONE, F_AVAIL=str(180 * GIB), F_FILE_BYTES=str(16 * GIB),
                                   WANT_BYTES=str(8 * GIB), SIZE="8"))
        self.assertEqual(r[1], 5)

    def test_df_failed(self):
        self.assertEqual(decide("setup", F_TOTAL="0", F_AVAIL="0")[1], 1)

    def test_bad_swapdir(self):
        self.assertEqual(decide("setup", F_SWAPDIR="symlink")[1], 4)
        self.assertEqual(decide("setup", F_SWAPDIR="other")[1], 4)
        self.assertEqual(decide("setup", F_SWAPDIR="subvol", F_EXTRA="1")[1], 4)

    def test_plain_dir_on_btrfs(self):
        plan, refuse, _, _ = decide("setup", F_SWAPDIR="dir")
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, {"P_NOHIB", "P_CONVERT", "P_MKSUBVOL", "P_MARKER", "P_CREATE", "P_SWAPON", "P_FSTAB"})
        self.assertEqual(decide("setup", F_SWAPDIR="dir", F_FILE="regular", F_FILE_GOOD="1",
                                F_FILE_BYTES=str(16 * GIB), F_FILE_PERM="1")[1], 4)

    def test_plain_dir_on_ext4_is_fine(self):
        plan, refuse, _, _ = decide("setup", F_FSTYPE="ext4", F_SWAPDIR="dir")
        self.assertEqual((refuse, plan), (0, {"P_NOHIB", "P_MARKER", "P_CREATE", "P_SWAPON", "P_FSTAB"}))

    def test_new_file_in_an_existing_folder_is_marked(self):
        # um @swap vazio feito à mão (montado ou subvolume): o arquivo novo é do Aero e ganha
        # marcador, senão o --remove (e o install.sh --uninstall) deixaria 16 GB para trás
        for swapdir in ("subvol", "mount"):
            plan, refuse, _, _ = decide("setup", F_SWAPDIR=swapdir)
            self.assertEqual((refuse, plan), (0, {"P_NOHIB", "P_MARKER", "P_CREATE", "P_SWAPON", "P_FSTAB"}))
        # já marcado (ou adotado): não escreve de novo
        for marker in ("file", "subvol", "adopted"):
            self.assertNotIn("P_MARKER", decide("setup", F_SWAPDIR="subvol", F_MARKER=marker)[0])
        # e o --remove apaga o arquivo, a linha e o marcador, mas não a pasta
        plan, refuse, notes, _ = decide("remove", **dict(DONE, F_SWAPDIR="mount", F_MARKER="file"))
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, {"P_SWAPOFF", "P_FSTAB_RM", "P_RMFILE", "P_RMMARKER", "P_NOHIB_RM"})
        self.assertEqual(notes, 1)
        plan, _, _, _ = decide("remove", **dict(DONE, F_SWAPDIR="subvol", F_MARKER="file"))
        self.assertNotIn("P_RMSUBVOL", plan)

    def test_adopts_an_existing_manual_swapfile(self):
        # /swap e /swap/swapfile feitos à mão (como na ArchWiki), ligados com outra prioridade:
        # o marcador "adopted" guarda a linha antiga do fstab para o --remove
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_MARKER="none", F_ACTIVE_PRIO="-2", F_FSTAB="other"))
        self.assertEqual((refuse, plan), (0, {"P_ADOPT", "P_REPRIO", "P_FSTAB"}))
        # já adotado antes: não escreve o marcador de novo
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_MARKER="adopted"))
        self.assertEqual((refuse, plan), (0, set()))

    def test_adopted_file_is_not_resized_without_size(self):
        # um swapfile do usuário de tamanho "torto" (sem --size): fica como está (exit 4)
        for marker in ("none", "adopted"):
            facts = dict(DONE, F_MARKER=marker, F_FILE_BYTES=str(10 * GIB + 300 * 1048576))
            plan, refuse, _, reason = decide("setup", **facts)
            self.assertEqual(refuse, 4, marker)
            self.assertIn("--size", reason)
            self.assertIn("resume_offset", reason)
            # com --size explícito (o Settings sempre manda), troca
            plan, refuse, notes, _ = decide("setup", **dict(facts, SIZE_SET="1"))
            self.assertEqual(refuse, 0, marker)
            self.assertTrue({"P_SWAPOFF", "P_RMFILE", "P_CREATE", "P_SWAPON"} <= plan)
            self.assertGreaterEqual(notes, 1)
        # o swapfile feito pelo Aero troca de tamanho normalmente
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FILE_BYTES=str(8 * GIB)))
        self.assertEqual(refuse, 0)

    def test_never_deletes_a_foreign_non_swap_file(self):
        for size_set in ("0", "1"):
            r = decide("setup", **dict(DONE, F_MARKER="none", F_FILE_GOOD="0", F_ACTIVE="0", SIZE_SET=size_set))
            self.assertEqual(r[1], 4)
            self.assertNotIn("P_RMFILE", r[0])

    def test_unsafe_swap_dir_refused(self):
        # /swap de outro dono ou com escrita para outros: root seguiria links plantados lá dentro
        for swapdir in ("subvol", "dir", "mount"):
            r = decide("setup", **dict(DONE, F_SWAPDIR=swapdir, F_SWAPDIR_SAFE="0"))
            self.assertEqual(r[1], 4, swapdir)
            self.assertIn("owned by root", r[3])
            self.assertEqual(decide("remove", **dict(DONE, F_SWAPDIR=swapdir, F_SWAPDIR_SAFE="0"))[1], 4)

    def test_swapoff_reserve_follows_the_guard(self):
        # 32 GB de RAM, 6 GB no SSD, 7,2 GB disponíveis: depois do swapoff sobraria ~1,2 GB (3,8%),
        # abaixo do "critical" do aero-memory (4%) — recusa (antes, com 1 GB fixo, passava)
        busy = dict(DONE, F_MEMTOTAL_KIB=str(32 * 1048576), F_USED_KIB=str(6 * 1048576),
                    F_MEMAVAIL_KIB=str(int(7.2 * 1048576)))
        r = decide("setup", **dict(busy, F_FILE_BYTES=str(8 * GIB)))        # trocar o tamanho
        self.assertEqual(r[1], 6)
        self.assertIn("memory guard", r[3])
        self.assertEqual(decide("remove", **busy)[1], 6)
        plan, refuse, notes, _ = decide("setup", **dict(busy, F_ACTIVE_PRIO="-2"))   # reprio adiado
        self.assertEqual((refuse, "P_REPRIO" in plan, notes), (0, False, 1))
        # com folga (6 + 4,8 <= 12): pode
        self.assertEqual(decide("remove", **dict(busy, F_MEMAVAIL_KIB=str(12 * 1048576)))[1], 0)
        # um warn_available maior no memory.conf pede mais folga
        self.assertEqual(decide("remove", **dict(busy, F_MEMAVAIL_KIB=str(12 * 1048576), F_WARN_PCT="25"))[1], 6)

    def test_reprio_postponed_when_busy(self):
        plan, refuse, notes, _ = decide("setup", **dict(DONE, F_ACTIVE_PRIO="-2",
                                                        F_USED_KIB=str(8 * 1048576),
                                                        F_MEMAVAIL_KIB=str(4 * 1048576)))
        self.assertEqual((refuse, plan, notes), (0, set(), 1))

    def test_bad_file(self):
        self.assertEqual(decide("setup", **dict(DONE, F_FILE="other"))[1], 4)
        # sem assinatura de swap ou com copy-on-write: refaz (desligado → sem swapoff)
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FILE_GOOD="0", F_ACTIVE="0"))
        self.assertEqual((refuse, plan), (0, {"P_RMFILE", "P_CREATE", "P_SWAPON"}))

    def test_fix_permissions(self):
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_FILE_PERM="0"))
        self.assertEqual((refuse, plan), (0, {"P_FIXPERM"}))

    def test_inactive_file_is_turned_on(self):
        plan, refuse, _, _ = decide("setup", **dict(DONE, F_ACTIVE="0", F_ACTIVE_PRIO=""))
        self.assertEqual((refuse, plan), (0, {"P_SWAPON"}))

    def test_normal_user_view(self):
        # sem root: não dá para ver dentro do /swap
        plan, refuse, _, _ = decide("setup", F_SWAPDIR="subvol", F_EXTRA="unknown",
                                    F_MARKER="unknown", F_FILE="unknown")
        self.assertEqual(refuse, 0)
        self.assertIn("P_CHECK", plan)
        # ligado e do tamanho certo: nada a fazer, só um aviso de que root confere as permissões
        plan, refuse, notes, _ = decide("setup", **dict(DONE, F_EXTRA="unknown", F_MARKER="unknown",
                                                        F_FILE_PERM="unknown"))
        self.assertEqual((plan, refuse, notes), (set(), 0, 1))


class AdoptSize(unittest.TestCase):
    def size_after(self, **facts):
        f = dict(DONE, SIZE_SET="0")
        f.update(facts)
        assigns = "; ".join(f"{k}={shlex.quote(v)}" for k, v in f.items())
        rc, out, err = sh(assigns + '; adopt_current_size; echo "$SIZE $WANT_BYTES"')
        self.assertEqual(rc, 0, err)
        size, want = out.split()
        self.assertEqual(int(want), int(size) * GIB)
        return int(size)

    def test_keeps_the_current_size(self):
        self.assertEqual(self.size_after(F_FILE_BYTES=str(32 * GIB)), 32)
        # o que um usuário comum vê pelo /proc/swaps (sem a página do cabeçalho)
        self.assertEqual(self.size_after(F_FILE_BYTES=str((32 * 1048576 - 4) * 1024 + 4096)), 32)
        self.assertEqual(self.size_after(F_FILE_BYTES=str(10 * GIB)), 10)

    def test_explicit_size_wins(self):
        self.assertEqual(self.size_after(F_FILE_BYTES=str(32 * GIB), SIZE_SET="1", SIZE="8",
                                         WANT_BYTES=str(8 * GIB)), 8)

    def test_odd_files_get_the_default(self):
        self.assertEqual(self.size_after(F_FILE_BYTES=str(2 * GIB)), 16)            # pequeno demais
        self.assertEqual(self.size_after(F_FILE_BYTES=str(100 * GIB)), 16)          # grande demais
        self.assertEqual(self.size_after(F_FILE_BYTES=str(10 * GIB + 300 * 1048576)), 16)   # não redondo
        self.assertEqual(self.size_after(F_FILE="missing", F_FILE_BYTES="0"), 16)
        self.assertEqual(self.size_after(F_FILE_GOOD="0", F_FILE_BYTES=str(32 * GIB)), 16)

    def test_then_nothing_to_do(self):
        # 32 GB adotado + tudo certo = plano vazio (não encolhe para 16)
        f = dict(DONE, SIZE_SET="0", F_FILE_BYTES=str(32 * GIB))
        assigns = "; ".join(f"{k}={shlex.quote(v)}" for k, v in f.items())
        rc, out, err = sh(assigns + '; adopt_current_size; decide_setup; plan_is_empty && echo empty; echo "R=$REFUSE"')
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.split(), ["empty", "R=0"])


class DecideRemove(unittest.TestCase):
    def test_full_remove(self):
        plan, refuse, _, _ = decide("remove", **DONE)
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, {"P_SWAPOFF", "P_FSTAB_RM", "P_RMFILE", "P_RMMARKER", "P_RMSUBVOL", "P_NOHIB_RM"})

    def test_remove_plain_dir(self):
        plan, _, _, _ = decide("remove", **dict(DONE, F_FSTYPE="ext4", F_SWAPDIR="dir", F_MARKER="dir"))
        self.assertEqual(plan, {"P_SWAPOFF", "P_FSTAB_RM", "P_RMFILE", "P_RMMARKER", "P_RMDIR", "P_NOHIB_RM"})

    def test_busy(self):
        r = decide("remove", **dict(DONE, F_USED_KIB=str(10 * 1048576), F_MEMAVAIL_KIB=str(4 * 1048576)))
        self.assertEqual(r[1], 6)

    def test_keeps_files_and_folders_it_did_not_make(self):
        # um /swap/swapfile sem marcador não é do Aero: nem swapoff, nem apagar, nem mexer no fstab
        # (e, como ele continua ligado, o bloqueio da hibernação fica)
        plan, refuse, notes, _ = decide("remove", **dict(DONE, F_MARKER="none"))
        self.assertEqual(refuse, 0)
        self.assertEqual(plan, set())
        self.assertEqual(notes, 2)
        # o caso da ArchWiki: @swap montado em /swap, swapfile do usuário ligado
        plan, refuse, _, _ = decide("remove", **dict(DONE, F_SWAPDIR="mount", F_MARKER="none", F_SLEEP="missing"))
        self.assertEqual((plan, refuse), (set(), 0))
        plan, _, _, _ = decide("remove", **dict(DONE, F_EXTRA="1"))
        self.assertNotIn("P_RMSUBVOL", plan)
        # mesmo marcador, mas /swap montado à parte: nunca apaga
        plan, _, _, _ = decide("remove", **dict(DONE, F_SWAPDIR="mount"))
        self.assertNotIn("P_RMSUBVOL", plan)

    def test_nothing_to_remove(self):
        plan, refuse, _, _ = decide("remove")
        self.assertEqual((plan, refuse), (set(), 0))

    def test_symlink_dir_is_not_followed(self):
        plan, refuse, _, _ = decide("remove", F_SWAPDIR="symlink", F_FSTAB="exact")
        self.assertEqual((plan, refuse), ({"P_FSTAB_RM"}, 0))

    def test_never_deletes_non_regular(self):
        self.assertEqual(decide("remove", **dict(DONE, F_FILE="other", F_ACTIVE="0"))[1], 4)

    def test_adopted_file_stays_and_gets_its_line_back(self):
        plan, refuse, notes, _ = decide("remove", **dict(DONE, F_SWAPDIR="mount", F_MARKER="adopted"))
        self.assertEqual(refuse, 0)
        # o swapfile continua ligado e o boot não volta dele: o bloqueio da hibernação fica
        self.assertEqual(plan, {"P_FSTAB_RESTORE", "P_RMMARKER"})
        self.assertEqual(notes, 2)
        # com o resume configurado, sai
        plan, _, notes, _ = decide("remove", **dict(DONE, F_SWAPDIR="mount", F_MARKER="adopted", F_RESUME="systemd"))
        self.assertEqual((plan, notes), ({"P_FSTAB_RESTORE", "P_RMMARKER", "P_NOHIB_RM"}, 1))
        # mesmo cheio de páginas: não precisa de swapoff, então nunca recusa por RAM
        r = decide("remove", **dict(DONE, F_MARKER="adopted", F_USED_KIB=str(10 * 1048576),
                                    F_MEMAVAIL_KIB=str(1048576)))
        self.assertEqual(r[1], 0)

    def test_hibernation_block_goes_away(self):
        for state in ("same", "ours"):
            plan, _, _, _ = decide("remove", F_SLEEP=state)
            self.assertEqual(plan, {"P_NOHIB_RM"}, state)
        # um arquivo com o mesmo nome que não é do Aero fica
        self.assertEqual(decide("remove", F_SLEEP="foreign")[0], set())
        # outro swap de disco continua ligado sem resume: fica (com um aviso); com resume: sai
        plan, _, notes, _ = decide("remove", **dict(DONE, F_FOREIGN="/dev/nvme0n1p3"))
        self.assertNotIn("P_NOHIB_RM", plan)
        self.assertIn("P_RMFILE", plan)
        self.assertGreaterEqual(notes, 1)
        self.assertIn("P_NOHIB_RM", decide("remove", **dict(DONE, F_FOREIGN="/dev/nvme0n1p3", F_RESUME="cmdline"))[0])
        # /swap virou link com o swapfile ligado por ele: nada de swapoff, então o bloqueio fica
        self.assertEqual(decide("remove", F_SWAPDIR="symlink", F_ACTIVE="1", F_SLEEP="same")[0], set())
        self.assertEqual(decide("remove", F_SWAPDIR="symlink", F_SLEEP="same")[0], {"P_NOHIB_RM"})
        # sem conseguir desligar a swap, o bloqueio fica
        r = decide("remove", **dict(DONE, F_USED_KIB=str(10 * 1048576), F_MEMAVAIL_KIB=str(4 * 1048576)))
        self.assertEqual(r[1], 6)


class Hibernation(unittest.TestCase):
    """Bloqueio da hibernação: o swapfile deixaria o systemd hibernar sem o boot saber voltar."""

    def hooks(self, *texts):
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, t in enumerate(texts):
                p = Path(d, f"{i}.conf")
                p.write_text(t)
                paths.append(str(p))
            return sh('mkinitcpio_hooks "$@"', *paths)[1].strip()

    def test_mkinitcpio_hooks(self):
        arch = "HOOKS=(base udev autodetect microcode modconf keyboard keymap consolefont block filesystems fsck)\n"
        self.assertEqual(self.hooks(arch), "base udev autodetect microcode modconf keyboard keymap "
                                            "consolefont block filesystems fsck")
        # várias linhas, comentários e aspas
        self.assertEqual(self.hooks("#HOOKS=(systemd)\nHOOKS=(base\n  systemd # comment\n  'sd-vconsole' \"block\")\n"),
                         "base systemd sd-vconsole block")
        # o mkinitcpio.conf.d vem depois: HOOKS= troca tudo, HOOKS+= soma
        self.assertEqual(self.hooks(arch, "HOOKS+=(resume)\n").split()[-1], "resume")
        self.assertEqual(self.hooks(arch, "HOOKS=(base systemd autodetect)\n"), "base systemd autodetect")
        self.assertEqual(self.hooks("# nothing\n"), "")
        self.assertEqual(sh("mkinitcpio_hooks")[1].strip(), "")

    def test_resume_mode(self):
        mode = lambda gen, hooks, cmdline, efi: sh('resume_mode "$@"', gen, hooks, cmdline, efi)[1].strip()
        plain = "base udev autodetect block filesystems fsck"
        sd = "base systemd autodetect block filesystems fsck"
        both = "resume=UUID=abc resume_offset=533760"
        # a máquina comum do Arch: hook sem systemd/resume, sem resume= -> não volta
        self.assertEqual(mode("mkinitcpio", plain, "quiet", "1"), "none")
        # hook systemd num boot EFI: a variável EFI aponta o arquivo
        self.assertEqual(mode("mkinitcpio", sd, "quiet", "1"), "systemd")
        self.assertEqual(mode("mkinitcpio", sd, "quiet", "0"), "none")
        self.assertEqual(mode("mkinitcpio", sd, both, "0"), "cmdline")
        # resume= sem offset ganha da variável EFI e aponta o começo da partição: não volta
        self.assertEqual(mode("mkinitcpio", sd, "resume=UUID=abc", "1"), "none")
        self.assertEqual(mode("mkinitcpio", plain.replace("block", "block resume"), both, "0"), "cmdline")
        self.assertEqual(mode("mkinitcpio", plain, both, "1"), "none")         # ninguém lê o resume=
        self.assertEqual(mode("dracut", "", both, "0"), "cmdline")
        self.assertEqual(mode("booster", "", both, "0"), "cmdline")
        self.assertEqual(mode("unknown", "", both, "1"), "none")
        self.assertEqual(mode("dracut", "", "resume=UUID=abc resume_offset=x", "0"), "none")
        for gen, hooks in (("mkinitcpio", sd), ("dracut", "")):
            self.assertEqual(mode(gen, hooks, both + " noresume", "1"), "none")

    def test_sleep_conf_state_and_write(self):
        with tempfile.TemporaryDirectory() as d:
            sdir = Path(d, "sleep.conf.d")
            conf = sdir / "90-aero-swap.conf"
            pre = f"SLEEP_DIR={shlex.quote(str(sdir))}; SLEEP_CONF={shlex.quote(str(conf))}; "
            state = lambda: sh(pre + 'sleep_conf_state "$SLEEP_CONF"')[1].strip()
            self.assertEqual(state(), "missing")
            rc, out, err = sh(pre + "trap cleanup EXIT; write_sleep_conf; echo finished")
            self.assertEqual(rc, 0, err)
            self.assertIn("finished", out)
            text = conf.read_text()
            self.assertEqual(stat_mode(conf), 0o644)
            self.assertEqual([p.name for p in sdir.iterdir()], ["90-aero-swap.conf"])     # sem temporário
            for line in ("[Sleep]", "AllowHibernation=no", "AllowSuspendThenHibernate=no", "AllowHybridSleep=no"):
                self.assertIn(line + "\n", text)
            self.assertEqual(state(), "same")
            conf.write_text(text + "# changed by an older Aero\n")
            self.assertEqual(state(), "ours")
            conf.write_text("[Sleep]\nAllowHibernation=yes\n")
            self.assertEqual(state(), "foreign")
            conf.unlink()
            conf.symlink_to(Path(d, "elsewhere"))
            self.assertEqual(state(), "foreign")

    def test_decide(self):
        # sem como voltar (o caso desta máquina): o bloqueio entra antes de tudo
        self.assertIn("P_NOHIB", decide("setup")[0])
        self.assertEqual(decide("setup", **dict(DONE, F_SLEEP="missing"))[0], {"P_NOHIB"})
        self.assertEqual(decide("setup", **dict(DONE, F_SLEEP="ours"))[0], {"P_NOHIB"})
        # initramfs com systemd: hibernar continua possível (só um aviso)
        plan, _, notes, _ = decide("setup", F_RESUME="systemd")
        self.assertNotIn("P_NOHIB", plan)
        self.assertEqual(notes, 1)
        # resume_offset= vale para o arquivo que já existe, não para um novo
        plan, _, notes, _ = decide("setup", F_RESUME="cmdline")
        self.assertIn("P_NOHIB", plan)
        self.assertEqual(notes, 1)
        self.assertEqual(decide("setup", **dict(DONE, F_RESUME="cmdline", F_SLEEP="missing"))[0], set())
        # um arquivo com o mesmo nome que não é do Aero fica (com um aviso)
        plan, _, notes, _ = decide("setup", F_SLEEP="foreign")
        self.assertNotIn("P_NOHIB", plan)
        self.assertEqual(notes, 1)
        self.assertEqual(decide("setup", **dict(DONE, F_RESUME="systemd"))[2], 1)


class SafetyChecks(unittest.TestCase):
    def test_dir_is_safe(self):
        for good in ("0 700", "0 755", "0 750", "0 2755"):
            self.assertTrue(ok('dir_is_safe "$1"', good), good)
        for bad in ("0 775", "0 757", "0 777", "0 1777", "1000 700", "", "0", "0 abc", "0 7777", "x 700"):
            self.assertFalse(ok('dir_is_safe "$1"', bad), repr(bad))

    def test_adopt_marker_round_trip(self):
        # setup adota o swapfile feito à mão; --remove devolve a linha dele, igual estava
        with tempfile.TemporaryDirectory() as d:
            fstab, marker = Path(d, "fstab"), Path(d, "marker")
            user = "/swap/swapfile none swap defaults 0 0"
            original = f"UUID=a / btrfs rw 0 0\n{user}\ntmpfs /tmp tmpfs defaults 0 0\n"
            fstab.write_text(original)
            pre = f"FSTAB={shlex.quote(str(fstab))}; MARKER={shlex.quote(str(marker))}; "
            rc, out, err = sh(pre + "write_adopt_marker; marker_kind \"$MARKER\"")
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.splitlines()[-1], "adopted")
            self.assertEqual(stat_mode(marker), 0o600)
            self.assertEqual(sh(pre + 'saved_fstab_lines "$MARKER"')[1], user + "\n")
            fstab.write_text(sh(pre + 'fstab_with_ours "$FSTAB"')[1])           # o que o setup grava
            self.assertNotIn(user, fstab.read_text())
            back = sh(pre + 'fstab_restore_original "$FSTAB"')[1]
            self.assertIn(user + "\n", back)
            self.assertNotIn(LINE, back)
            self.assertNotIn(TAG, back)
            self.assertEqual(sorted(back.splitlines()), sorted(original.splitlines()))
            # um link no lugar do marcador: para sem escrever nada
            marker.unlink()
            target = Path(d, "target")
            target.write_text("keep\n")
            marker.symlink_to(target)
            rc, _, err = sh(pre + "write_adopt_marker")
            self.assertEqual(rc, 1)
            self.assertIn("is a link", err)
            self.assertEqual(target.read_text(), "keep\n")
            self.assertEqual(sh(pre + 'marker_kind "$MARKER"')[1].strip(), "none")

    def test_apply_writes_the_right_marker(self):
        # apply_setup com os comandos de verdade trocados por um "run" que só imprime
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d, "marker")
            for flags, want in (("P_MKSUBVOL=1", "subvol"), ("P_MKDIR=1", "dir"), (":", "file")):
                code = (f'MARKER={shlex.quote(str(marker))}; run() {{ printf "RUN %s\\n" "$*"; }}; '
                        'swap_is_on() { return 0; }; reset_plan; P_MARKER=1; ' + flags + '; apply_setup')
                rc, out, err = sh(code)
                self.assertEqual(rc, 0, err)
                self.assertIn(f"created={want}\n", marker.read_text())
                self.assertEqual(sh('marker_kind "$1"', str(marker))[1].strip(), want)
                self.assertEqual(stat_mode(marker), 0o600)
                marker.unlink()

    def test_marker_kind(self):
        with tempfile.TemporaryDirectory() as d:
            m = Path(d, "m")
            kind = lambda: sh('marker_kind "$1"', str(m))[1].strip()
            self.assertEqual(kind(), "none")
            for text, want in (("# x\ncreated=subvol\n", "subvol"), ("created=dir\n", "dir"),
                               ("created=file\n", "file"),
                               ("adopted=1\nfstab=/swap/swapfile none swap defaults 0 0\n", "adopted"),
                               ("created=subvolume\n", "none"), ("fstab=created=dir\n", "none")):
                m.write_text(text)
                self.assertEqual(kind(), want, text)

    def test_check_new_file(self):
        # o btrfs-progs 7.1 sai com 0 sem conseguir fazer o arquivo: o resultado é conferido
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "swapfile")
            pre = f"F_FSTYPE=ext4; WANT_BYTES={4 * 1048576}; "
            self.assertEqual(sh(pre + 'check_new_file "$1"', str(f))[0], 1)            # não existe
            f.write_bytes(b"")
            rc, _, err = sh(pre + 'check_new_file "$1"', str(f))
            self.assertEqual(rc, 1)
            self.assertIn("not a usable swap file", err)
            page = int(sh("echo $PAGE")[1])
            data = bytearray(4 * 1048576)
            data[page - 10:page] = b"SWAPSPACE2"
            f.write_bytes(bytes(data))
            self.assertEqual(sh(pre + 'check_new_file "$1"; echo good', str(f))[1].strip(), "good")
            self.assertEqual(sh(f"F_FSTYPE=ext4; WANT_BYTES={16 * GIB}; " + 'check_new_file "$1"', str(f))[0], 1)

    def test_file_is_good_swap(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "swapfile")
            page = int(sh("echo $PAGE")[1])
            data = bytearray(page * 4)
            f.write_bytes(bytes(data))
            good = lambda fs, path=None: sh(("" if path is None else f"PATH={shlex.quote(path)}; ")
                                            + f'F_FSTYPE={fs}; file_is_good_swap "$1"', str(f))[1].strip()
            self.assertEqual(good("ext4"), "0")                       # sem assinatura
            data[page - 10:page] = b"SWAPSPACE2"
            f.write_bytes(bytes(data))
            self.assertEqual(good("ext4"), "1")
            # btrfs: o +C (sem copy-on-write) vem do lsattr; aqui um de mentira numa pasta só nossa
            bindir = Path(d, "bin")
            bindir.mkdir()
            for tool in ("head", "tail", "awk"):
                (bindir / tool).symlink_to(shutil.which(tool))
            self.assertEqual(good("btrfs", str(bindir)), "unknown")   # sem lsattr: o swapon decide
            fake = bindir / "lsattr"
            fake.write_text('#!/bin/sh\necho "---------------C------ $2"\n')
            fake.chmod(0o755)
            self.assertEqual(good("btrfs", str(bindir)), "1")
            fake.write_text('#!/bin/sh\necho "---------------------- $2"\n')
            self.assertEqual(good("btrfs", str(bindir)), "0")

    def test_signals_still_clean_up(self):
        # Ctrl+C ou SIGTERM no meio da criação: o arquivo pela metade não fica no disco
        for sig, code in ((signal.SIGTERM, 143), (signal.SIGINT, 130)):
            with tempfile.TemporaryDirectory() as d:
                f = Path(d, "swapfile")
                code_sh = ('source "$1"; SWAP_FILE="$2"; CREATED_FILE=1; set_traps; '
                           ': > "$SWAP_FILE"; echo ready; sleep 30; echo not-reached')
                p = subprocess.Popen(["bash", "-c", code_sh, "harness", str(SCRIPT), str(f)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     start_new_session=True)
                try:
                    self.assertEqual(p.stdout.readline().strip(), "ready")
                    # espera o "sleep" (o comando da vez, como um swapoff) já estar rodando: um sinal
                    # entre o fork e o exec se perderia no filho, e o teste esperaria os 30 s
                    self.assertTrue(wait_for_child(p.pid, "sleep"))
                    os.killpg(p.pid, sig)
                    out, err = p.communicate(timeout=20)
                finally:
                    if p.poll() is None:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.communicate()
                self.assertEqual(p.returncode, code, sig)
                self.assertNotIn("not-reached", out)
                self.assertFalse(f.exists(), sig)
                self.assertIn("removed the half-made", err)
        # saída normal (o swapfile ligou e CREATED_FILE voltou a 0): o arquivo fica
        with tempfile.TemporaryDirectory() as d:
            f = Path(d, "swapfile")
            rc, _, _ = sh('SWAP_FILE="$1"; CREATED_FILE=0; set_traps; : > "$SWAP_FILE"; exit 0', str(f))
            self.assertEqual(rc, 0)
            self.assertTrue(f.exists())


@unittest.skipIf(os.geteuid() == 0, "these checks are for a normal user")
class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        p = subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_apply_needs_root(self):
        for args in (["--apply"], ["--apply", "--size", "8"], ["--remove", "--apply"]):
            rc, out, err = self.run_cli(*args)
            self.assertEqual(rc, 3, args)
            self.assertIn("needs root", err)
            self.assertIn("sudo aero-swap-setup", err)

    def test_bad_usage(self):
        for args in (["--size", "3"], ["--size", "65"], ["--size", "x"], ["--size"], ["--frobnicate"],
                     ["--status", "--size", "8"], ["--remove", "--size", "8"], ["--status", "--apply"]):
            self.assertEqual(self.run_cli(*args)[0], 2, args)

    def test_dry_run_changes_nothing(self):
        before = (os.path.lexists("/swap"), os.stat("/etc/fstab").st_mtime_ns)
        rc, out, err = self.run_cli()
        self.assertIn(rc, (0, 4, 5, 6), err)
        self.assertIn("dry run", out)
        self.assertIn("Swap tiers", out)
        rc2, out2, _ = self.run_cli("--remove")
        self.assertIn(rc2, (0, 4, 6))
        self.assertEqual(before, (os.path.lexists("/swap"), os.stat("/etc/fstab").st_mtime_ns))

    def test_status(self):
        rc, out, _ = self.run_cli("--status")
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("Swap tiers"))

    def test_help(self):
        rc, out, _ = self.run_cli("--help")
        self.assertEqual(rc, 0)
        self.assertIn("Exit codes", out)


class ScriptHygiene(unittest.TestCase):
    """Regras da casa para um script que roda como root."""

    def test_rules(self):
        text = SCRIPT.read_text()
        self.assertIn("set -euo pipefail", text)
        self.assertIn("\nPATH=/usr/sbin:/usr/bin:/sbin:/bin\n", text)
        for bad in ("eval ", "source /", ". /etc", "curl ", "wget "):
            self.assertNotIn(bad, text)
        self.assertTrue(os.access(SCRIPT, os.X_OK))
        rc = subprocess.run(["bash", "-n", str(SCRIPT)]).returncode
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
