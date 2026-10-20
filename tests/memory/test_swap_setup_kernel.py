"""Testes dos arquivos de kernel da memória esperta: sysctl, tmpfiles (MGLRU) e zram-generator.

Nada aqui muda o sistema: o systemd-tmpfiles roda em --dry-run (ou em arquivos de uma pasta
temporária) e o zram-generator roda no modo de teste (ZRAM_GENERATOR_ROOT), que só escreve
units numa pasta temporária e não cria dispositivos.
Rodar: python3 -m unittest discover -s tests/memory -p 'test_swap_setup*.py'
"""
import configparser
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

MEM = Path(__file__).resolve().parents[2] / "system" / "memory"
SYSCTL = MEM / "90-aero-memory.conf"
TMPFILES = MEM / "90-aero-memory.tmpfiles"
ZRAM = MEM / "zram-generator.conf"
GENERATOR = "/usr/lib/systemd/system-generators/zram-generator"

EXPECTED = {
    "vm.swappiness": 150,
    "vm.page-cluster": 0,
    "vm.watermark_boost_factor": 0,
    "vm.watermark_scale_factor": 125,
    "vm.vfs_cache_pressure": 50,
    "vm.dirty_bytes": 268435456,
    "vm.dirty_background_bytes": 67108864,
}


def sysctl_settings(path):
    """[(chave, valor, linha_anterior)] como o sysctl.d lê (# e ; são comentários)."""
    out, prev = [], ""
    for line in path.read_text().splitlines():
        s = line.strip()
        if s and not s.startswith(("#", ";")):
            key, _, val = s.partition("=")
            out.append((key.strip(), val.strip(), prev))
        prev = s
    return out


class Sysctl(unittest.TestCase):
    def test_exact_values(self):
        got = {k: int(v) for k, v, _ in sysctl_settings(SYSCTL)}
        self.assertEqual(got, EXPECTED)

    def test_every_setting_is_explained(self):
        for key, _, prev in sysctl_settings(SYSCTL):
            self.assertTrue(prev.startswith("#"), f"{key} has no comment right above it")

    def test_ranges_and_semantics(self):
        v = EXPECTED
        self.assertTrue(0 <= v["vm.swappiness"] <= 200)
        self.assertTrue(1 <= v["vm.watermark_scale_factor"] <= 3000)
        self.assertGreater(v["vm.vfs_cache_pressure"], 0)   # 0 = nunca descarta → OOM
        # o kernel usa metade do limite se o de fundo não for menor
        self.assertLess(v["vm.dirty_background_bytes"], v["vm.dirty_bytes"])
        # abaixo de 2 páginas o kernel ignora o valor
        self.assertGreaterEqual(v["vm.dirty_bytes"], 2 * 4096)
        self.assertGreaterEqual(v["vm.dirty_background_bytes"], 2 * 4096)
        # usar *_bytes zera os *_ratio: o arquivo não pode setar os dois de cada par
        keys = {k for k, _, _ in sysctl_settings(SYSCTL)}
        self.assertNotIn("vm.dirty_ratio", keys)
        self.assertNotIn("vm.dirty_background_ratio", keys)
        text = SYSCTL.read_text()
        self.assertIn("zera vm.dirty_ratio", text)

    @unittest.skipUnless(Path("/proc/sys/vm").is_dir(), "needs Linux /proc/sys")
    def test_keys_exist_in_this_kernel(self):
        for key in EXPECTED:
            p = Path("/proc/sys", *key.split("."))
            self.assertTrue(p.exists(), key)

    def test_installed_name_sorts_late_but_not_last(self):
        # 90-: depois dos padrões do systemd/distro (10-, 50-), antes do 99-sysctl.conf do usuário
        self.assertTrue(SYSCTL.name.startswith("90-") and SYSCTL.name.endswith(".conf"))


class Tmpfiles(unittest.TestCase):
    def lines(self):
        return [l for l in TMPFILES.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]

    def test_single_line(self):
        self.assertEqual(self.lines(), ["w- /sys/kernel/mm/lru_gen/min_ttl_ms - - - - 1000"])

    @unittest.skipUnless(shutil.which("systemd-tmpfiles"), "needs systemd-tmpfiles")
    def test_dry_run_parses(self):
        p = subprocess.run(["systemd-tmpfiles", "--create", "--dry-run", str(TMPFILES)],
                           capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("Failed", p.stderr)
        if Path("/sys/kernel/mm/lru_gen/min_ttl_ms").exists():
            self.assertIn("/sys/kernel/mm/lru_gen/min_ttl_ms", p.stdout + p.stderr)

    @unittest.skipUnless(shutil.which("systemd-tmpfiles"), "needs systemd-tmpfiles")
    def test_missing_path_is_skipped_quietly(self):
        # kernel sem MGLRU: o "w" não cria o arquivo nem falha
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "lru_gen", "min_ttl_ms")
            conf = Path(d, "t.conf")
            conf.write_text(self.lines()[0].replace("/sys/kernel/mm/lru_gen/min_ttl_ms", str(target)) + "\n")
            p = subprocess.run(["systemd-tmpfiles", "--create", str(conf)], capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertFalse(target.exists())
            self.assertFalse(target.parent.exists())

    @unittest.skipUnless(shutil.which("systemd-tmpfiles"), "needs systemd-tmpfiles")
    def test_writes_value_when_present(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "min_ttl_ms")
            target.write_text("0\n")
            conf = Path(d, "t.conf")
            conf.write_text(self.lines()[0].replace("/sys/kernel/mm/lru_gen/min_ttl_ms", str(target)) + "\n")
            p = subprocess.run(["systemd-tmpfiles", "--create", str(conf)], capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(target.read_text(), "1000")

    @unittest.skipUnless(shutil.which("systemd-tmpfiles"), "needs systemd-tmpfiles")
    @unittest.skipIf(os.geteuid() == 0, "root ignores file modes")
    def test_write_failure_does_not_fail_boot(self):
        # o "-" de "w-": uma escrita recusada não deixa o systemd-tmpfiles-setup.service falho
        with tempfile.TemporaryDirectory() as d:
            target = Path(d, "min_ttl_ms")
            target.write_text("0\n")
            target.chmod(0o444)
            conf = Path(d, "t.conf")
            conf.write_text(self.lines()[0].replace("/sys/kernel/mm/lru_gen/min_ttl_ms", str(target)) + "\n")
            p = subprocess.run(["systemd-tmpfiles", "--create", str(conf)], capture_output=True, text=True, timeout=20)
            self.assertEqual(p.returncode, 0, p.stderr)
            target.chmod(0o644)


class Zram(unittest.TestCase):
    def test_keys(self):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read(ZRAM)
        self.assertEqual(cp.sections(), ["zram0"])
        self.assertEqual(dict(cp["zram0"]), {
            "zram-size": "min(ram / 2, 16384)",
            "compression-algorithm": "zstd",
            "swap-priority": "100",
        })

    @unittest.skipUnless(os.access(GENERATOR, os.X_OK), "needs zram-generator")
    def test_generator_test_mode(self):
        # MemTotal (kB) → tamanho esperado (MB): metade da RAM, no máximo 16384
        cases = {8 * 1024 * 1024: 4096, 32000000: 15625, 64 * 1024 * 1024: 16384}
        for kib, want_mb in cases.items():
            with tempfile.TemporaryDirectory() as root:
                r = Path(root)
                (r / "etc/systemd").mkdir(parents=True)
                (r / "proc").mkdir()
                shutil.copy(ZRAM, r / "etc/systemd/zram-generator.conf")
                (r / "proc/meminfo").write_text(f"MemTotal: {kib} kB\nMemFree: 1000 kB\n")
                (r / "proc/cmdline").write_text("quiet\n")
                outs = [r / n for n in ("out", "early", "late")]
                for o in outs:
                    o.mkdir()
                env = dict(os.environ, ZRAM_GENERATOR_ROOT=root, RUST_LOG="info")
                p = subprocess.run([GENERATOR, *map(str, outs)], capture_output=True, text=True,
                                   timeout=20, env=env)
                self.assertEqual(p.returncode, 0, p.stderr)
                m = re.search(r"/dev/zram0 with (\d+)MB", p.stdout + p.stderr)
                self.assertIsNotNone(m, p.stderr)
                self.assertEqual(int(m.group(1)), want_mb)
                unit = (r / "out/dev-zram0.swap").read_text()
                self.assertIn("Priority=100", unit)


if __name__ == "__main__":
    unittest.main()
