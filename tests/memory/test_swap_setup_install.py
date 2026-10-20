"""Testes do system/memory/install.sh.

Funções puras: carregando o script com "source". Instalação de verdade: com --destdir numa pasta
temporária (copia os arquivos, cópias de segurança, idempotência, desinstalar), como usuário comum,
sem tocar no sistema. Os arquivos do vigia (componente B) são trocados por versões de mentira.
Também confere que nenhum arquivo da parte A tem dados pessoais (usuário, máquina, /home).
Rodar: python3 -m unittest discover -s tests/memory -p 'test_swap_setup*.py'
"""
import getpass
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MEM = REPO / "system" / "memory"
INSTALL = MEM / "install.sh"
A_FILES = ["90-aero-memory.conf", "90-aero-memory.tmpfiles", "zram-generator.conf",
           "aero-swap-setup", "install.sh"]
B_STUBS = {
    "aero-memory": "#!/usr/bin/env python3\n# stub\nprint('stub')\n",
    "aero-memory-config": "#!/usr/bin/env bash\n# stub\necho stub\n",
    "aero-memory.service": "[Unit]\nDescription=stub\n",
}
DESTS = {
    "/etc/sysctl.d/90-aero-memory.conf": 0o644,
    "/etc/tmpfiles.d/90-aero-memory.conf": 0o644,
    "/etc/systemd/zram-generator.conf": 0o644,
    "/usr/local/bin/aero-swap-setup": 0o755,
    "/usr/local/bin/aero-memory": 0o755,
    "/usr/local/bin/aero-memory-config": 0o755,
    "/etc/systemd/system/aero-memory.service": 0o644,
}
BAK = re.compile(r"\.bak-aero-\d{8}-\d{6}(-\d+)*$")


def sh(code, *args):
    p = subprocess.run(["bash", "-c", 'source "$1"; shift; ' + code, "harness", str(INSTALL), *args],
                       capture_output=True, text=True, timeout=30)
    return p.returncode, p.stdout, p.stderr


def out(code, *args):
    rc, o, e = sh(code, *args)
    assert rc == 0, (code, rc, o, e)
    return o.strip()


class PureFunctions(unittest.TestCase):
    def test_valid_size(self):
        for v in ("4", "8", "16", "64"):
            self.assertEqual(sh('valid_size "$1"', v)[0], 0)
        for v in ("3", "65", "08", "x", "", "16g"):
            self.assertNotEqual(sh('valid_size "$1"', v)[0], 0, v)

    def test_backup_name(self):
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "f.conf")
            first = out('backup_name "$1" 20260101-120000', dst)
            self.assertEqual(first, dst + ".bak-aero-20260101-120000")
            Path(first).write_text("x")
            second = out('backup_name "$1" 20260101-120000', dst)
            self.assertEqual(second, dst + ".bak-aero-20260101-120000-1")
            # a cópia nunca termina em .conf: o sysctl.d / tmpfiles.d não a leem
            self.assertFalse(first.endswith(".conf") or second.endswith(".conf"))

    def test_file_state(self):
        uid = str(os.getuid())
        with tempfile.TemporaryDirectory() as d:
            src, dst = Path(d, "src"), Path(d, "dst")
            src.write_text("a\n")
            self.assertEqual(out('file_state "$@"', str(src), str(dst), "644", uid), "new")
            shutil.copy(src, dst)
            dst.chmod(0o644)
            self.assertEqual(out('file_state "$@"', str(src), str(dst), "644", uid), "same")
            dst.chmod(0o600)
            self.assertEqual(out('file_state "$@"', str(src), str(dst), "644", uid), "changed")
            dst.chmod(0o644)
            dst.write_text("b\n")
            self.assertEqual(out('file_state "$@"', str(src), str(dst), "644", uid), "changed")
            dst.unlink()
            dst.symlink_to(src)
            self.assertEqual(out('file_state "$@"', str(src), str(dst), "644", uid), "changed")

    def test_sysctl_settings(self):
        got = out('sysctl_settings "$1"', str(MEM / "90-aero-memory.conf")).splitlines()
        self.assertEqual(got, ["vm.swappiness 150", "vm.page-cluster 0", "vm.watermark_boost_factor 0",
                               "vm.watermark_scale_factor 125", "vm.vfs_cache_pressure 50",
                               "vm.dirty_bytes 268435456", "vm.dirty_background_bytes 67108864"])
        with tempfile.NamedTemporaryFile("w", delete=False) as t:
            t.write("; c\n# c\n  a.b =  1 \nx.y=2\n\nnot a setting\n")
        try:
            self.assertEqual(out('sysctl_settings "$1"', t.name).splitlines(), ["a.b 1", "x.y 2"])
        finally:
            os.unlink(t.name)

    def test_zram_wanted_bytes(self):
        self.assertEqual(int(out("zram_wanted_bytes 8388608")), 4 * 1024 ** 3)
        self.assertEqual(int(out("zram_wanted_bytes 32000000")), 32000000 * 512)
        self.assertEqual(int(out("zram_wanted_bytes 67108864")), 16 * 1024 ** 3)
        self.assertEqual(int(out("zram_wanted_bytes 134217728")), 16 * 1024 ** 3)

    def test_zram_action(self):
        self.assertEqual(out("zram_action 0 0 0"), "start")
        self.assertEqual(out("zram_action 0 0 1"), "start")
        self.assertEqual(out("zram_action 1 0 1"), "none")
        self.assertEqual(out("zram_action 1 500 1"), "none")
        self.assertEqual(out("zram_action 1 0 0"), "restart")
        self.assertEqual(out("zram_action 1 1 0"), "reboot")

    def test_zram_selected_algo(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as t:
            t.write("lzo-rle lzo lz4 lz4hc [zstd] deflate 842 \n")
        try:
            self.assertEqual(out('zram_selected_algo "$1"', t.name), "zstd")
            self.assertEqual(out('zram_selected_algo /nonexistent'), "")
        finally:
            os.unlink(t.name)

    def test_original_backup(self):
        with tempfile.TemporaryDirectory() as d:
            dst = os.path.join(d, "zram-generator.conf")
            self.assertEqual(out('original_backup "$1"', dst), "")
            Path(dst + ".bak-aero-20260101-100000").write_text("# Aero OS old version\n")
            Path(dst + ".bak-aero-20260102-100000").write_text("[zram0]\n# the user's own\n")
            Path(dst + ".bak-aero-20260103-100000").write_text("[zram0]\n# a later one\n")
            self.assertEqual(out('original_backup "$1"', dst), dst + ".bak-aero-20260102-100000")


    def test_sysctl_without_zram_keys(self):
        # --no-zram: sem swappiness 150 e page-cluster 0 (pensados para a swap em RAM)
        text = out('sysctl_without_zram_keys "$1"', str(MEM / "90-aero-memory.conf")) + "\n"
        with tempfile.NamedTemporaryFile("w", delete=False) as t:
            t.write(text)
        try:
            got = out('sysctl_settings "$1"', t.name).splitlines()
        finally:
            os.unlink(t.name)
        self.assertEqual(got, ["vm.watermark_boost_factor 0", "vm.watermark_scale_factor 125",
                               "vm.vfs_cache_pressure 50", "vm.dirty_bytes 268435456",
                               "vm.dirty_background_bytes 67108864"])
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line and not line.startswith("#"):
                self.assertTrue(lines[i - 1].startswith("#"), f"{line} lost its comment")
        # o comentário de cada chave tirada sai junto; o cabeçalho fica
        self.assertNotIn("swappiness (0..200)", text)
        self.assertNotIn("page-cluster = quantas", text)
        self.assertTrue(text.startswith("# Aero OS"))
        self.assertIn("--no-zram", lines[-1])

    def test_undo_install(self):
        with tempfile.TemporaryDirectory() as d:
            dst, bak = Path(d, "zram.conf"), Path(d, "zram.conf.bak-aero-x")
            dst.write_text("new\n")
            # não existia antes: some
            self.assertEqual(sh('declare -A BACKUP_OF=(["$1"]=new); undo_install "$1"', str(dst))[0], 0)
            self.assertFalse(dst.exists())
            # trocado agora: a cópia volta
            dst.write_text("new\n")
            bak.write_text("old\n")
            self.assertEqual(sh('declare -A BACKUP_OF=(["$1"]="$2"); undo_install "$1"', str(dst), str(bak))[0], 0)
            self.assertEqual(dst.read_text(), "old\n")
            self.assertFalse(bak.exists())
            # não foi mexido nesta execução: nada a desfazer
            self.assertEqual(sh('undo_install "$1" || echo nothing', str(dst))[1].strip(), "nothing")
            self.assertEqual(dst.read_text(), "old\n")

    def test_preflight_needs_zram_generator(self):
        # sem o zram-generator, o --apply para antes de copiar (a não ser com --no-zram ou --destdir)
        pre = 'ZRAM_GENERATOR=/nonexistent-aero-test; '
        rc, o, e = sh(pre + 'DESTDIR=""; NO_ZRAM=0; preflight && echo ready || echo not-ready')
        self.assertEqual(o.strip(), "not-ready")
        self.assertIn("--no-zram", e)
        self.assertEqual(out(pre + 'DESTDIR=""; NO_ZRAM=1; preflight && echo ready || echo not-ready'), "ready")
        self.assertEqual(out(pre + 'DESTDIR=/x; NO_ZRAM=0; preflight && echo ready || echo not-ready'), "ready")

    def test_ssd_swap_waits_for_zram(self):
        # sem o zram ligado, o swapfile do SSD não é criado (seria a única swap, com swappiness 150)
        stubs = ('install_files() { :; }; step_sysctl() { :; }; step_tmpfiles() { :; }; step_zram() { :; }; '
                 'step_service() { :; }; print_next_steps() { :; }; step_swap() { echo STEP_SWAP; }; ')
        def run(apply, no_zram, zram_on, no_swap="0"):
            code = (stubs + f'APPLY={apply}; NO_ZRAM={no_zram}; DESTDIR=""; '
                    f'zram_is_on() {{ return {0 if zram_on else 1}; }}; do_install 16 {no_swap} 0')
            rc, o, e = sh(code)
            self.assertEqual(rc, 0, e)
            return "STEP_SWAP" in o, e
        ran, e = run(1, 0, False)
        self.assertFalse(ran)
        self.assertIn("zram) is not on", e)
        self.assertTrue(run(1, 0, True)[0])
        self.assertTrue(run(1, 1, False)[0])     # --no-zram: escolha explícita, swappiness no padrão
        self.assertTrue(run(0, 0, False)[0])     # dry-run mostra o plano
        self.assertFalse(run(1, 0, True, no_swap="1")[0])


class Steps(unittest.TestCase):
    def test_run_counts_failures_and_goes_on(self):
        rc, o, e = sh('APPLY=1; run bash -c "exit 3"; echo "$FAILED $LAST_RC"; run true; echo "$FAILED $LAST_RC"')
        self.assertEqual(rc, 0)
        values = [l for l in o.splitlines() if not l.startswith("  + ")]
        self.assertEqual(values, ["1 3", "1 0"])
        self.assertIn("this step failed (exit 3)", e)

    def test_dry_run_only_prints(self):
        rc, o, _ = sh('APPLY=0; run touch /nonexistent-dir/x; echo "$FAILED"')
        self.assertEqual(rc, 0)
        self.assertIn("would run: touch /nonexistent-dir/x", o)
        self.assertEqual(o.splitlines()[-1], "0")

    @unittest.skipUnless(os.access("/usr/lib/systemd/system-generators/zram-generator", os.X_OK), "needs zram-generator")
    def test_zram_config_check(self):
        with tempfile.TemporaryDirectory() as d:
            env_tmp = Path(d, "tmp")
            env_tmp.mkdir()
            bad = Path(d, "bad.conf")
            bad.write_text("[zram0]\nzram-size = min(ram / 2,\n")
            code = f'export TMPDIR={env_tmp}; zram_config_ok "$1"'
            self.assertEqual(sh(code, str(MEM / "zram-generator.conf"))[0], 0)
            self.assertNotEqual(sh(code, str(bad))[0], 0)
            empty = Path(d, "empty.conf")
            empty.write_text("# no devices\n")
            self.assertNotEqual(sh(code, str(empty))[0], 0)
            self.assertEqual(list(env_tmp.iterdir()), [])      # nenhuma pasta temporária sobrou


class StagedInstall(unittest.TestCase):
    """install.sh --apply --destdir: copia de verdade, numa pasta temporária."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.repo = t / "repo" / "system" / "memory"
        self.repo.mkdir(parents=True)
        for name in A_FILES:
            shutil.copy2(MEM / name, self.repo / name)
        for name, text in B_STUBS.items():
            (self.repo / name).write_text(text)
        (self.repo / "aero-memory").chmod(0o755)
        (self.repo / "aero-memory-config").chmod(0o755)
        self.dest = t / "dest"
        self.dest.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_install(self, *args):
        p = subprocess.run(["bash", str(self.repo / "install.sh"), *args, "--destdir", str(self.dest)],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def backups(self):
        return sorted(str(p.relative_to(self.dest)) for p in self.dest.rglob("*") if BAK.search(p.name))

    def test_install_reinstall_update_uninstall(self):
        # um zram-generator.conf do usuário já existe
        zram = self.dest / "etc/systemd/zram-generator.conf"
        zram.parent.mkdir(parents=True)
        user_zram = "[zram0]\ncompression-algorithm = zstd\n"
        zram.write_text(user_zram)

        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 0, o + e)
        for dst, mode in DESTS.items():
            p = self.dest / dst.lstrip("/")
            self.assertTrue(p.is_file(), dst)
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), mode, dst)
        self.assertTrue((self.dest / "etc/aero").is_dir())
        self.assertEqual(zram.read_text(), (MEM / "zram-generator.conf").read_text())
        baks = self.backups()
        self.assertEqual(len(baks), 1)
        self.assertTrue(baks[0].startswith("etc/systemd/zram-generator.conf.bak-aero-"))
        self.assertEqual((self.dest / baks[0]).read_text(), user_zram)
        # nenhum arquivo temporário sobrou
        self.assertEqual([p for p in self.dest.rglob(".aero-install.*")], [])

        # de novo: nada muda
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(o.count("unchanged"), len(DESTS))
        self.assertEqual(self.backups(), baks)

        # o vigia mudou no repositório: atualiza e guarda o antigo sem +x
        (self.repo / "aero-memory").write_text(B_STUBS["aero-memory"] + "# v2\n")
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 0, o + e)
        new = [b for b in self.backups() if b not in baks]
        self.assertEqual(len(new), 1)
        old_bin = self.dest / new[0]
        self.assertTrue(new[0].startswith("usr/local/bin/aero-memory.bak-aero-"))
        self.assertEqual(old_bin.read_text(), B_STUBS["aero-memory"])
        self.assertFalse(os.stat(old_bin).st_mode & 0o111)
        self.assertIn("# v2", (self.dest / "usr/local/bin/aero-memory").read_text())

        # alguém editou o sysctl instalado
        sysctl = self.dest / "etc/sysctl.d/90-aero-memory.conf"
        sysctl.write_text(sysctl.read_text() + "vm.swappiness = 10\n")

        # desinstalar em dry-run não muda nada
        before = sorted(str(p) for p in self.dest.rglob("*"))
        rc, o, e = self.run_install("--uninstall")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(sorted(str(p) for p in self.dest.rglob("*")), before)

        rc, o, e = self.run_install("--uninstall", "--apply")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(zram.read_text(), user_zram)          # o original voltou
        for dst in DESTS:
            if dst != "/etc/systemd/zram-generator.conf":
                self.assertFalse((self.dest / dst.lstrip("/")).exists(), dst)
        moved = [b for b in self.backups() if b.startswith("etc/sysctl.d/90-aero-memory.conf.bak-aero-")]
        self.assertEqual(len(moved), 1)                        # o editado virou cópia
        self.assertIn("vm.swappiness = 10", (self.dest / moved[0]).read_text())
        self.assertTrue((self.dest / "etc/aero").is_dir())     # configurações ficam

    def test_uninstall_keeps_an_edited_zram_conf(self):
        # a config do Aero editada depois de instalada vira cópia antes do original voltar
        zram = self.dest / "etc/systemd/zram-generator.conf"
        zram.parent.mkdir(parents=True)
        user_zram = "[zram0]\ncompression-algorithm = lz4\n"
        zram.write_text(user_zram)
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 0, o + e)
        edited = zram.read_text().replace("zram-size = min(ram / 2, 16384)", "zram-size = ram")
        self.assertIn("Aero OS", edited)
        zram.write_text(edited)
        rc, o, e = self.run_install("--uninstall", "--apply")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(zram.read_text(), user_zram)
        aside = [b for b in self.backups() if b.startswith("etc/systemd/zram-generator.conf.bak-aero-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual((self.dest / aside[0]).read_text(), edited)
        self.assertIn("it was edited", o)

    def test_no_zram_install(self):
        rc, o, e = self.run_install("--apply", "--no-swap", "--no-zram")
        self.assertEqual(rc, 0, o + e)
        sysctl = self.dest / "etc/sysctl.d/90-aero-memory.conf"
        text = sysctl.read_text()
        self.assertNotIn("vm.swappiness", "\n".join(l for l in text.splitlines() if not l.startswith("#")))
        self.assertIn("vm.watermark_scale_factor = 125", text)
        self.assertFalse((self.dest / "etc/systemd/zram-generator.conf").exists())
        self.assertEqual([p for p in self.dest.rglob(".aero-install.*")], [])
        # de novo: igual (a variante gerada é a mesma)
        rc, o, e = self.run_install("--apply", "--no-swap", "--no-zram")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(o.count("unchanged"), len(DESTS) - 1)
        # desinstalar reconhece a variante como nossa: some, sem virar cópia
        rc, o, e = self.run_install("--uninstall", "--apply")
        self.assertEqual(rc, 0, o + e)
        self.assertFalse(sysctl.exists())
        self.assertEqual(self.backups(), [])

    @unittest.skipUnless(os.access("/usr/lib/systemd/system-generators/zram-generator", os.X_OK), "needs zram-generator")
    def test_broken_zram_conf_changes_nothing(self):
        # uma config que o zram-generator recusa deixaria o próximo boot sem zram: nem é copiada
        (self.repo / "zram-generator.conf").write_text("# Aero OS\n[zram0]\nzram-size = min(ram / 2,\n")
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 1)
        self.assertIn("zram-generator can't use", e)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_uninstall_leaves_a_foreign_zram_conf(self):
        zram = self.dest / "etc/systemd/zram-generator.conf"
        zram.parent.mkdir(parents=True)
        zram.write_text("[zram0]\n")
        rc, o, e = self.run_install("--uninstall", "--apply")
        self.assertEqual(rc, 0, o + e)
        self.assertEqual(zram.read_text(), "[zram0]\n")
        self.assertIn("not Aero's", o)

    def test_dry_run_writes_nothing(self):
        rc, o, e = self.run_install()
        self.assertEqual(rc, 0, o + e)
        self.assertIn("dry run", o)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_broken_repo_changes_nothing(self):
        (self.repo / "aero-memory").write_text("#!/usr/bin/env python3\ndef broken(:\n")
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 1)
        self.assertIn("python syntax error", e)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_symlinked_source_refused(self):
        (self.repo / "aero-memory-config").unlink()
        (self.repo / "aero-memory-config").symlink_to("/etc/hostname")
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 1)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_missing_b_files_refused(self):
        (self.repo / "aero-memory.service").unlink()
        rc, o, e = self.run_install("--apply", "--no-swap")
        self.assertEqual(rc, 1)
        self.assertIn("aero-memory.service", e)
        self.assertEqual(list(self.dest.iterdir()), [])


@unittest.skipIf(os.geteuid() == 0, "these checks are for a normal user")
class CommandLine(unittest.TestCase):
    def run_cli(self, *args):
        p = subprocess.run(["bash", str(INSTALL), *args], capture_output=True, text=True, timeout=60)
        return p.returncode, p.stdout, p.stderr

    def test_apply_needs_root(self):
        for args in (["--apply"], ["--uninstall", "--apply"], ["--apply", "--swap-size", "8"]):
            rc, _, err = self.run_cli(*args)
            self.assertEqual(rc, 3, args)
            self.assertIn("needs root", err)

    def test_bad_usage(self):
        for args in (["--swap-size", "3"], ["--swap-size", "8", "--no-swap"], ["--destdir", "relative/path"],
                     ["--destdir", "/"], ["--destdir"], ["--nope"]):
            self.assertEqual(self.run_cli(*args)[0], 2, args)

    def test_dry_run_on_this_system(self):
        rc, o, e = self.run_cli()
        # 1 = os arquivos do vigia (componente B) ainda não estão no repositório
        self.assertIn(rc, (0, 1), e)
        self.assertIn("dry run", o)
        self.assertIn("would run: sysctl -q --system", o)


class NoPersonalData(unittest.TestCase):
    """O repositório é público: nada de usuário, nome da máquina ou caminho de /home."""

    def test_component_a_files(self):
        user = getpass.getuser()
        host = socket.gethostname().split(".")[0]
        files = [MEM / n for n in A_FILES] + sorted(Path(__file__).parent.glob("test_swap_setup*.py"))
        home = "/" + "home" + "/"          # montado aqui para este arquivo não se acusar
        mac = re.compile(r"\b[0-9a-f]{2}(:[0-9a-f]{2}){5}\b", re.I)
        uuid = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
        for f in files:
            text = f.read_text()
            self.assertFalse(home in text, f"{f.name}: has a {home} path")
            if len(user) >= 3 and user not in ("root", "user"):
                self.assertFalse(user in text, f"{f.name}: has the user name")
            if len(host) >= 3 and host not in ("localhost", "archlinux"):
                self.assertFalse(host in text, f"{f.name}: has the host name")
            # números de série, MACs e UUIDs de disco não têm o que fazer aqui
            self.assertIsNone(mac.search(text), f"{f.name}: has a MAC address")
            self.assertIsNone(uuid.search(text), f"{f.name}: has a UUID")


if __name__ == "__main__":
    unittest.main()
