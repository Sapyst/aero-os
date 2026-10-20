"""
Testes da seção Memory (sec_memoria.py). Só biblioteca padrão + gi (o mesmo que o Settings usa).

Rodar (da pasta do repositório):
  python3 -m unittest discover -s settings/tests -v

Os testes de widgets precisam de uma tela (Wayland/X); sem tela eles são pulados. Nenhum abre janela,
menos o do relógio numa janela de verdade (só com AERO_TESTE_JANELA=1).
Nada aqui chama sudo nem o systemctl de verdade: rodar_como_root, rodar e abrir são trocados por falsos,
e todos os caminhos (state.json, configs, ajudante) apontam pra uma pasta temporária.
"""
import contextlib
import io
import os
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import sec_memoria as m  # noqa: E402
from gi.repository import Adw, GLib, GObject, Gtk  # noqa: E402

MEMINFO = """MemTotal:       32000000 kB
MemFree:         2000000 kB
MemAvailable:   20000000 kB
SwapTotal:       4194300 kB
SwapFree:        4194300 kB
Bogus line without colon
HugePages_Total:       0
"""
SWAPS = """Filename				Type		Size		Used		Priority
/dev/zram0                              partition	4194300		1048576		100
/swap/swapfile                          file		16777212	0		10
"""


class TestLeitura(unittest.TestCase):
    def test_meminfo(self):
        mem = m.ler_meminfo(MEMINFO)
        self.assertEqual(mem["MemTotal"], 32000000)
        self.assertEqual(mem["MemAvailable"], 20000000)
        self.assertEqual(mem["HugePages_Total"], 0)
        self.assertEqual(m.ler_meminfo(None), {})
        self.assertEqual(m.ler_meminfo("lixo\n:::\nX: abc kB"), {})

    def test_psi(self):
        psi = m.ler_psi("some avg10=1.50 avg60=0.00 avg300=0.00 total=10\n"
                        "full avg10=0.25 avg60=0.00 avg300=0.00 total=5\n")
        self.assertEqual(psi, {"some": 1.5, "full": 0.25})
        self.assertEqual(m.ler_psi(None), {"some": None, "full": None})
        self.assertEqual(m.ler_psi("some avg10=nan? total=1")["some"], None)
        # fora de 0..100 (ou nan/inf) não é leitura de verdade: fica de fora, como no aero-memory
        for ruim in ("150.00", "-1.00", "nan", "inf"):
            self.assertEqual(m.ler_psi(f"some avg10={ruim} avg60=0.00\nfull avg10=100.00\n"),
                             {"some": None, "full": 100.0}, ruim)
        self.assertEqual(m.ler_meminfo("MemTotal: ²³ kB\nMemFree: 12 kB\n"), {"MemFree": 12})   # dígito "estranho"

    def test_swaps(self):
        s = m.ler_swaps(SWAPS)
        self.assertEqual(len(s), 2)
        self.assertTrue(s[0]["zram"])
        self.assertEqual((s[0]["kb"], s[0]["usado"], s[0]["prioridade"]), (4194300, 1048576, 100))
        self.assertFalse(s[1]["zram"])
        self.assertEqual(s[1]["tipo"], "file")
        self.assertEqual(m.ler_swaps(None), [])
        # nome que só parece zram não é zram; linhas quebradas são ignoradas
        lixo = "h\n/dev/zram0x partition 1 0 5\n/dev/sda2 partition x y z\ncurta 1 2\n/var/a\\040b file 10 0 -2\n"
        s = m.ler_swaps(lixo)
        self.assertEqual([x["nome"] for x in s], ["/dev/zram0x", "/var/a\\040b"])
        self.assertFalse(s[0]["zram"])
        self.assertEqual(s[1]["prioridade"], -2)

    def test_mm_stat(self):
        self.assertEqual(m.ler_mm_stat("    4096       64    20480        0    20480"), (4096, 64, 20480))
        self.assertIsNone(m.ler_mm_stat("1 2"))
        self.assertIsNone(m.ler_mm_stat("a b c"))
        self.assertIsNone(m.ler_mm_stat(None))
        self.assertIsNone(m.mm_stat_de({"nome": "/swap/swapfile", "zram": False}))

    def test_disco_da_raiz(self):
        mi = ("22 1 0:21 / /proc rw,nosuid shared:5 - proc proc rw\n"
              "25 1 0:24 /@ / rw,relatime shared:1 - btrfs /dev/nvme0n1p2 rw,compress=zstd:3,subvol=/@\n"
              "26 25 0:25 /@home /home rw shared:2 - btrfs /dev/nvme0n1p2 rw\n")
        self.assertEqual(m.disco_da_raiz(mi), "/dev/nvme0n1p2")
        # sem campos opcionais; e a última montagem em "/" é a que vale
        mi2 = mi + "40 25 253:0 / / rw - ext4 /dev/mapper/raiz rw\n"
        self.assertEqual(m.disco_da_raiz(mi2), "/dev/mapper/raiz")
        for ruim in (None, "", "lixo\n", "1 2 0:1 / / rw - overlay overlay rw\n",      # não é aparelho
                     "1 2 0:1 / / rw shared:1 tmpfs x\n", "1 2 0:1 / / rw - zfs\n"):
            self.assertIsNone(m.disco_da_raiz(ruim), ruim)

    def test_cifrado(self):
        # um /sys de mentira: dm/uuid diz quem é dm-crypt; slaves diz o que está embaixo
        with tempfile.TemporaryDirectory() as d:
            def no(nome, uuid=None, embaixo=()):
                p = os.path.join(d, nome)
                os.makedirs(p)
                if uuid is not None:
                    os.makedirs(os.path.join(p, "dm"))
                    with open(os.path.join(p, "dm", "uuid"), "w") as f:
                        f.write(uuid + "\n")
                if embaixo:
                    os.makedirs(os.path.join(p, "slaves"))
                    for e in embaixo:
                        os.symlink(os.path.join(d, e), os.path.join(p, "slaves", e))
                return p
            part = no("nvme0n1p2")
            luks = no("dm-0", "CRYPT-LUKS2-abc-cryptroot", ["nvme0n1p2"])
            lvm = no("dm-1", "LVM-xyz", ["dm-0"])                   # LVM em cima do LUKS
            lvm_cru = no("dm-2", "LVM-uvw", ["nvme0n1p2"])          # LVM sem cifra
            meio = no("md0", None, ["dm-0", "nvme0n1p2"])            # RAID com um lado sem cifra
            vazio = no("dm-3", "LVM-sem-nada")
            self.assertFalse(m.cifrado(part))
            self.assertTrue(m.cifrado(luks))
            self.assertTrue(m.cifrado(lvm))
            self.assertFalse(m.cifrado(lvm_cru))
            self.assertFalse(m.cifrado(meio))
            self.assertFalse(m.cifrado(vazio))
            self.assertFalse(m.cifrado(os.path.join(d, "nao-existe")))
            laco = no("dm-9", "LVM-laco")                           # laço (não existe de verdade): para
            os.makedirs(os.path.join(laco, "slaves"))
            os.symlink(laco, os.path.join(laco, "slaves", "dm-9"))
            self.assertFalse(m.cifrado(laco))
        # o disco de verdade desta máquina: só precisa responder sem erro
        self.assertIn(m.raiz_cifrada(m.ler_texto("/proc/self/mountinfo", 1024 * 1024)), (True, False, None))
        self.assertIsNone(m.raiz_cifrada("1 2 0:1 / / rw - ext4 /dev/nao-existe rw\n"))
        self.assertIsNone(m.raiz_cifrada("1 2 0:1 / / rw - ext4 /dev/null rw\n"))      # não é de bloco
        self.assertIsNone(m.raiz_cifrada(None))

    def test_ler_texto(self):
        with tempfile.TemporaryDirectory() as d:
            arq = os.path.join(d, "a")
            with open(arq, "w") as f:
                f.write("x=1\n")
            self.assertEqual(m.ler_texto(arq), "x=1\n")
            self.assertIsNone(m.ler_texto(os.path.join(d, "nao-existe")))
            self.assertIsNone(m.ler_texto(d))                                   # pasta
            grande = os.path.join(d, "g")
            with open(grande, "wb") as f:
                f.write(b"a" * 101)
            self.assertIsNone(m.ler_texto(grande, limite=100))
            self.assertEqual(len(m.ler_texto(grande, limite=101)), 101)
            fifo = os.path.join(d, "fifo")
            os.mkfifo(fifo)
            inicio = time.monotonic()
            self.assertIsNone(m.ler_texto(fifo))                                # sem escritor: não trava
            self.assertLess(time.monotonic() - inicio, 1)
            with open(os.path.join(d, "bin"), "wb") as f:
                f.write(b"\xff\xfeok")
            self.assertIn("ok", m.ler_texto(os.path.join(d, "bin")))
        self.assertIn("MemTotal", m.ler_texto("/proc/meminfo"))                 # /proc tem tamanho 0

    def test_ler_texto_nao_vaza_fd(self):
        antes = len(os.listdir("/proc/self/fd"))
        for _ in range(200):
            m.ler_texto("/proc/meminfo")
            m.ler_texto("/caminho/que/nao/existe")
            m.ler_texto("/proc")
        self.assertEqual(len(os.listdir("/proc/self/fd")), antes)

    def test_ler_json(self):
        self.assertEqual(m.ler_json('{"a": 1}'), {"a": 1})
        self.assertIsNone(m.ler_json("[1, 2]"))
        self.assertIsNone(m.ler_json("{quebrado"))
        self.assertIsNone(m.ler_json(""))
        self.assertIsNone(m.ler_json(None))
        self.assertIsNone(m.ler_json("[" * 50000 + "]" * 50000))               # fundo demais


class TestTextos(unittest.TestCase):
    def test_num(self):
        self.assertEqual(m.num(3), 3.0)
        self.assertEqual(m.num(2.5), 2.5)
        for ruim in (True, False, None, "3", float("nan"), float("inf"), 10 ** 400, [1], {}):
            self.assertIsNone(m.num(ruim), ruim if not isinstance(ruim, int) else "int")

    def test_limpar(self):
        self.assertEqual(m.limpar("fossilize_replay"), "fossilize_replay")
        self.assertEqual(m.limpar("a\nb\x1b[31m‮evil"), "ab[31mevil")
        self.assertEqual(m.limpar("x" * 100), "x" * 32)
        self.assertEqual(m.limpar(None), "")
        self.assertEqual(m.limpar(42), "")
        self.assertEqual(m.limpar("<b>&amp;</b>"), "<b>&amp;</b>")              # sem markup: fica literal

    def test_tamanho(self):
        self.assertEqual(m.tamanho(512 * 1024), "512 MB")
        self.assertEqual(m.tamanho(0), "0 MB")
        self.assertEqual(m.tamanho(-5), "0 MB")
        self.assertEqual(m.tamanho(16 * 1024 * 1024), "16.0 GB")
        self.assertEqual(m.tamanho(None), "?")

    def test_ha_quanto(self):
        agora = 1_000_000.0
        self.assertEqual(m.ha_quanto(agora - 5, agora), "just now")
        self.assertEqual(m.ha_quanto(agora - 125, agora), "2 min ago")
        self.assertEqual(m.ha_quanto(agora - 3 * 3600, agora), "3 h ago")
        self.assertEqual(m.ha_quanto(agora - 86400 - 5, agora), "1 day ago")
        self.assertEqual(m.ha_quanto(agora - 3 * 86400, agora), "3 days ago")
        self.assertEqual(m.ha_quanto(agora + 3600, agora), "")
        self.assertEqual(m.ha_quanto(None, agora), "")

    def test_nome_bonito(self):
        # o kernel corta o comm em 15 letras: "fossilize_replay" chega como "fossilize_repla"
        self.assertEqual(m.nome_bonito("fossilize_repla"), "Steam shader compilation")
        self.assertEqual(m.nome_bonito("fossilize_replay"), "Steam shader compilation")
        self.assertEqual(m.nome_bonito("firefox"), "firefox")
        # nome comprido de background=/protect= volta inteiro (mesma comparação do aero-memory)
        self.assertEqual(m.nome_bonito("my_render_worke", ["blender", "my_render_worker"]), "my_render_worker")
        self.assertEqual(m.nome_bonito("blend", ["blender"]), "blend")     # começo igual, mas não é corte
        self.assertEqual(m.nome_bonito("", ["x"]), "")

    def test_descrever_acao(self):
        agora = 1_000_000.0
        uid = 1000
        # como o aero-memory manda de verdade: comm cortado em 15 letras e o uid do dono
        pausa = {"kind": "paused", "uid": uid, "names": ["fossilize_repla"], "count": 7, "mb": 5222,
                 "t": agora - 120, "seq": 4}
        self.assertEqual(m.descrever_acao(pausa, agora, uid=uid),
                         "Paused Steam shader compilation (7 processes, 5.1 GB) · 2 min ago")
        self.assertEqual(m.descrever_acao({"kind": "paused", "names": ["fossilize_replay"] * 7, "count": 7,
                                           "mb": 5222, "t": agora - 120}, agora),
                         "Paused Steam shader compilation (7 processes, 5.1 GB) · 2 min ago")
        # de outro usuário: sem os nomes dele
        self.assertEqual(m.descrever_acao(pausa, agora, uid=uid + 1),
                         "Paused another user's background work (7 processes, 5.1 GB) · 2 min ago")
        morto = {"kind": "killed", "uid": uid, "name": "firefox", "pid": 1234, "mb": 6400, "count": 1}
        self.assertEqual(m.descrever_acao(morto, agora, uid=uid + 1),
                         "Closed another user's app (it was using 6.2 GB) to prevent a freeze")
        self.assertEqual(m.descrever_acao(dict(morto, uid=uid), agora, uid=uid),
                         "Closed firefox (it was using 6.2 GB) to prevent a freeze")
        self.assertEqual(m.descrever_acao({"kind": "resumed", "uid": 0, "names": ["x"], "reason": "calm"},
                                          agora, uid=uid), "Resumed another user's paused work")
        for sem_dono in (None, "1000", True, 1.5):                        # uid estranho = vale como nosso
            self.assertEqual(m.descrever_acao(dict(morto, uid=sem_dono), agora, uid=uid),
                             "Closed firefox (it was using 6.2 GB) to prevent a freeze", sem_dono)
        self.assertEqual(m.descrever_acao(dict(morto, name="my_render_worke"), agora, ["my_render_worker"]),
                         "Closed my_render_worker (it was using 6.2 GB) to prevent a freeze")
        # o OOM killer do próprio kernel (o aero-memory só conta quantas vezes)
        self.assertEqual(m.descrever_acao({"kind": "oom_kill", "count": 1, "t": agora - 30}, agora),
                         "The system closed an app (out of memory) · just now")
        self.assertEqual(m.descrever_acao({"kind": "oom_kill", "count": 3}, agora),
                         "The system closed 3 apps (out of memory)")
        self.assertEqual(m.descrever_acao({"kind": "oom_kill", "count": 1000}, agora),
                         "The system closed 1000 apps (out of memory)")
        self.assertEqual(m.descrever_acao({"kind": "oom_kill", "count": 10 ** 12}, agora),
                         "The system closed an app (out of memory)")
        # --dry-run só finge: não vira "última ação"
        self.assertEqual(m.descrever_acao(dict(pausa, dry_run=True), agora, uid=uid), "")
        self.assertEqual(m.descrever_acao({"kind": "killed", "name": "firefox", "pid": 1234, "mb": 6400}, agora),
                         "Closed firefox (it was using 6.2 GB) to prevent a freeze")
        self.assertEqual(m.descrever_acao({"kind": "resumed"}, agora), "Resumed paused work")
        self.assertEqual(m.descrever_acao({"kind": "resumed", "names": ["a", "b", "c", "d", "e"]}, agora),
                         "Resumed a, b, c +2")
        self.assertEqual(m.descrever_acao({"kind": "unloaded", "t": agora}, agora),
                         "Unloaded idle ComfyUI models · just now")
        self.assertEqual(m.descrever_acao({"kind": "orphan", "mb": 2048}, agora),
                         "Stopped the ComfyUI server and freed 2.0 GB")
        self.assertEqual(m.descrever_acao({"kind": "some_new_thing"}, agora), "Some new thing")
        self.assertEqual(m.descrever_acao("Freed 3 GB", agora), "Freed 3 GB")
        for ruim in (None, 5, [], {}, {"kind": 7}, {"kind": ""}):
            self.assertEqual(m.descrever_acao(ruim, agora), "")
        # dados estranhos não quebram nem passam controles
        texto = m.descrever_acao({"kind": "paused", "names": ["a\nb", None, 5, "x" * 99], "count": "7",
                                  "mb": float("nan"), "t": "ontem"}, agora)
        self.assertEqual(texto, "Paused ab, " + "x" * 32)

    def test_ler_estado(self):
        agora = 1_000_000.0
        est = m.ler_estado('{"t": %f, "level": "warning", "paused": ["fossilize_repla", 3, "a\\nb", '
                           '"fossilize_replay", "my_render_worke"], "paused_uids": [1000], '
                           '"config": {"enabled": 1, "pause_background": false, "kill_on_critical": "1", '
                           '"warn_available": 15, "protect": "x", "critical_available": [1], '
                           '"background": ["fossilize_replay", "my_render_worker", "a;b", 7]}, '
                           '"last_action": {"kind": "paused"}}' % (agora - 3), agora)
        self.assertTrue(est["existe"] and est["vivo"])
        self.assertEqual(est["nivel"], "warning")
        self.assertEqual(est["pausados"], ["Steam shader compilation", "ab", "my_render_worker"])
        self.assertEqual(est["conhecidos"], ["fossilize_replay", "my_render_worker"])
        self.assertEqual(est["uids_pausados"], [1000])
        self.assertEqual(est["config"], {"enabled": "1", "pause_background": "0", "kill_on_critical": "1",
                                         "warn_available": "15"})
        self.assertEqual(est["ultima"], {"kind": "paused"})
        for ruim in ('"x"', '[1, "2"]', '[true]', '{"1000": 1}'):
            self.assertIsNone(m.ler_estado('{"t": 1, "paused_uids": %s}' % ruim, agora)["uids_pausados"], ruim)
        velho = m.ler_estado('{"t": %f, "level": "normal"}' % (agora - 600), agora)
        self.assertTrue(velho["existe"])
        self.assertFalse(velho["vivo"])
        for ruim in (None, "", "[]", "{", '{"t": "x", "level": ["warning"], "config": 5, "paused": "a"}',
                     '{"t": 1e999, "level": {"a": 1}}', '{"t": %s}' % ("9" * 400)):
            est = m.ler_estado(ruim, agora)
            self.assertFalse(est["vivo"])
            self.assertIsNone(est["nivel"])
            self.assertEqual(est["config"], {})
            self.assertEqual(est["pausados"], [])

    def test_valor_conf(self):
        self.assertEqual(m.valor_conf(True), "1")
        self.assertEqual(m.valor_conf(False), "0")
        self.assertEqual(m.valor_conf(15), "15")
        self.assertEqual(m.valor_conf(" 4 "), "4")
        for ruim in (None, "abc", "1;rm", -1, 10 ** 30, float("nan"), [], "12345"):
            self.assertIsNone(m.valor_conf(ruim))

    def test_nivel_local(self):
        mem = m.ler_meminfo(MEMINFO)
        calmo = {"some": 0.0, "full": 0.0}
        self.assertEqual(m.nivel_local(mem, calmo), "normal")
        self.assertEqual(m.nivel_local(mem, {"some": 25.0, "full": 0.0}), "warning")
        apertado = dict(mem, MemAvailable=1000000, SwapFree=1000000)            # folga ~6%
        self.assertEqual(m.nivel_local(apertado, calmo), "warning")
        critico = dict(mem, MemAvailable=600000, SwapFree=100000)               # <4% e swap quase cheio
        self.assertEqual(m.nivel_local(critico, calmo), "critical")
        sem_swap = dict(mem, MemAvailable=600000, SwapTotal=0, SwapFree=0)
        self.assertEqual(m.nivel_local(sem_swap, calmo), "critical")
        self.assertIsNone(m.nivel_local({}, calmo))
        self.assertIsNone(m.nivel_local({"MemTotal": 100}, calmo))             # sem MemAvailable: não sei
        self.assertEqual(m.nivel_local(mem, {"some": None, "full": None}), "normal")

    def test_nivel_local_com_swap_no_ssd(self):
        # 31 GB de RAM, 0,8 GB disponível, zram quase cheio, SSD de 16 GB vazio e PSI zero: o SSD não é
        # folga (o aero-memory conta só a camada rápida, e o zram livre vale 2/3). Antes: "normal".
        gb = 1024 * 1024                                                        # kB
        mem = {"MemTotal": 31 * gb, "MemAvailable": int(0.8 * gb), "SwapTotal": int(31.5 * gb),
               "SwapFree": int(16.1 * gb)}
        swaps = m.ler_swaps("h\n/dev/zram0 partition %d %d 100\n/swap/swapfile file %d 0 10\n"
                            % (int(15.5 * gb), int(15.4 * gb), 16 * gb))
        calmo = {"some": 0.0, "full": 0.0}
        self.assertEqual(m.nivel_local(mem, calmo, swaps), "warning")
        self.assertEqual(m.nivel_local(mem, calmo, None), "normal")             # sem /proc/swaps: o SwapFree todo
        self.assertEqual(m.nivel_local(mem, calmo, []), "normal")               # tabela vazia com swap ligado
        # travando: PSI full alto com o swap rápido no fim, mesmo sem chegar em 4% disponível
        mem2 = dict(mem, MemAvailable=int(2 * gb))
        self.assertEqual(m.nivel_local(mem2, {"some": 60.0, "full": 35.0}, swaps), "critical")
        self.assertEqual(m.nivel_local(mem2, {"some": 60.0, "full": 35.0}, None), "warning")
        # a folga: só a prioridade mais alta; zram livre vale 2/3; um swap só, que não é zram, conta inteiro
        self.assertEqual(m.folga_swap(swaps), int((int(15.5 * gb) - int(15.4 * gb)) * 1024 * m.ZRAM_VALE))
        self.assertEqual(m.folga_swap(m.ler_swaps("h\n/swapfile file 1000 400 -2\n")), 600 * 1024)
        self.assertEqual(m.folga_swap([]), 0)

    def test_texto_guarda(self):
        est = m.ler_estado(None, 0)
        titulo, sub = m.texto_guarda(False, est, None)
        self.assertEqual(titulo, "Memory guard is not installed")
        self.assertEqual(m.texto_guarda(True, est, None), ("Status", "Checking…"))
        self.assertEqual(m.texto_guarda(True, est, True), ("Status", "Running"))
        self.assertIn("stopped", m.texto_guarda(True, est, False)[1])
        vivo = dict(est, vivo=True, pausados=["Steam shader compilation"], config={"enabled": "1"})
        self.assertIn("holding Steam shader compilation", m.texto_guarda(True, vivo, True)[1])
        desligado = dict(vivo, config={"enabled": "0"})
        self.assertTrue(m.texto_guarda(True, desligado, True)[1].startswith("Off"))
        # de quem são os pausados (paused_uids): com algo de outro usuário, sem os nomes
        self.assertIn("holding Steam shader", m.texto_guarda(True, dict(vivo, uids_pausados=[1000]), True, 1000)[1])
        for uids in ([1001], [1000, 1001]):
            sub = m.texto_guarda(True, dict(vivo, uids_pausados=uids), True, 1000)[1]
            self.assertIn("holding background work until memory frees up", sub)
            self.assertNotIn("Steam", sub)

    def test_texto_fechar(self):
        # a notificação vem do agente: só promete com ele instalado, não parado e notify=1
        com = m.texto_fechar(True, True, m.PADRAO_AGENTE)
        self.assertIn("You get a notification", com)
        self.assertEqual(m.texto_fechar(True, None, m.PADRAO_AGENTE), com)    # ainda não sei: vale o normal
        for sem in (m.texto_fechar(False, None, m.PADRAO_AGENTE), m.texto_fechar(True, False, m.PADRAO_AGENTE),
                    m.texto_fechar(True, True, dict(m.PADRAO_AGENTE, notify="0"))):
            self.assertNotIn("notification", sem)
            self.assertIn("Last action", sem)
            self.assertTrue(sem.startswith("Last resort"))

    def test_motivo_da_falha(self):
        def r(codigo, err=""):
            return subprocess.CompletedProcess([], codigo, "", err)
        self.assertEqual(m.motivo_da_falha(r(1, "sudo: a password is required\n")), "password cancelled")
        self.assertEqual(m.motivo_da_falha(r(3, "warning: x\nnot now: aero-swap-setup: error: not enough free "
                                               "space: a 32.0 GB swap file would leave less than 20%\n\n")),
                         "not enough free space: a 32.0 GB swap file would leave less than 20%")
        self.assertEqual(m.motivo_da_falha(r(3, "not now: aero-swap-setup: error: another disk swap is already "
                                               "on (/swapfile). Aero keeps a single SSD swap")),
                         "another disk swap is already on (/swapfile). Aero keeps a single SSD swap")
        self.assertEqual(m.motivo_da_falha(r(2, "refused: critical_available (20) must be lower than "
                                               "warn_available (15)")),
                         "critical_available (20) must be lower than warn_available (15)")
        self.assertEqual(m.motivo_da_falha(r(4, "failed: aero-swap-setup took too long")),
                         "aero-swap-setup took too long")
        # sem motivo, código estranho, exceção ou saída que não é texto: genérico
        for ruim in (r(4, ""), r(4, None), r(4, b"x"), r(127, "sudo: x"), r(-9), FileNotFoundError("sudo"), None):
            self.assertEqual(m.motivo_da_falha(ruim), "something went wrong")
        # longo e com controles: limpo e curto (vai num aviso)
        longo = m.motivo_da_falha(r(2, "refused: \x1b[31m<b>" + "x" * 500 + "\n"))
        self.assertEqual(longo, "[31m<b>" + "x" * 193)
        self.assertEqual(len(longo), 200)

    def test_texto_comfy(self):
        agora = 1000.0
        self.assertEqual(m.texto_comfy(False, None, None, agora)[0], "ComfyUI helper is not installed")
        self.assertIn("isn't running", m.texto_comfy(True, {"comfy": {"running": True}}, False, agora)[1])
        self.assertEqual(m.texto_comfy(True, None, None, agora), ("ComfyUI", "Not running"))
        self.assertEqual(m.texto_comfy(True, {"comfy": "x"}, True, agora), ("ComfyUI", "Not running"))
        est = {"comfy": {"running": True, "models_loaded": True, "idle_s": 300,
                         "last_action": {"kind": "unloaded", "t": agora - 60}}}
        self.assertEqual(m.texto_comfy(True, est, True, agora),
                         ("ComfyUI", "Running · models loaded · idle 5 min\nLast: Unloaded idle ComfyUI models"
                                     " · 1 min ago"))
        est = {"comfy": {"running": True, "models_loaded": False, "idle_s": "muito"}}
        self.assertEqual(m.texto_comfy(True, est, None, agora)[1], "Running · no models loaded")

    def test_aviso_privacidade(self):
        self.assertEqual(m.aviso_privacidade(True), "")
        self.assertIn("This disk isn't encrypted", m.aviso_privacidade(False))
        self.assertIn("If this disk isn't encrypted", m.aviso_privacidade(None))
        for x in (False, None):
            self.assertTrue(m.aviso_privacidade(x).startswith(" "))           # vai colado no fim do texto

    def test_opcoes(self):
        self.assertEqual(m.opcoes_ocioso("15"), m.OCIOSO)
        self.assertEqual(m.opcoes_ocioso("015"), m.OCIOSO)
        self.assertEqual(m.opcoes_ocioso("abc"), m.OCIOSO)
        self.assertEqual(m.opcoes_ocioso(None), m.OCIOSO)
        com10 = m.opcoes_ocioso("10")
        self.assertEqual([v for _, v in com10], ["0", "5", "10", "15", "30", "60"])
        self.assertEqual(m.opcoes_ssd(None)[0], ("Not set up", None))
        self.assertEqual([v for _, v in m.opcoes_ssd(16)], [8, 16, 32])
        self.assertEqual([v for _, v in m.opcoes_ssd(12)], [8, 12, 16, 32])

    def test_ssd_gerenciavel(self):
        s = m.ler_swaps(SWAPS)
        self.assertEqual(m.ssd_gerenciavel(s), (True, 16))
        self.assertEqual(m.ssd_gerenciavel([x for x in s if x["zram"]]), (True, None))
        part = [{"nome": "/dev/nvme0n1p3", "tipo": "partition", "kb": 8 << 20, "usado": 0, "prioridade": -2,
                 "zram": False}]
        self.assertEqual(m.ssd_gerenciavel(part), (False, None))               # partição: não mexe
        # qualquer outro swap de disco ligado = o aero-swap-setup recusa (F_FOREIGN): nem oferece
        cab = "Filename Type Size Used Priority\n"
        sozinho = m.ler_swaps(cab + "/swapfile file 8388604 0 -2\n")               # archinstall / ArchWiki
        self.assertEqual(m.ssd_gerenciavel(sozinho), (False, None))
        dois = m.ler_swaps(cab + "/swap/swapfile file 16777212 0 10\n/swapfile file 8388604 0 -2\n")
        self.assertEqual(m.ssd_gerenciavel(dois), (False, None))                # (antes: "24 GB")
        com_part = m.ler_swaps(cab + "/swap/swapfile file 16777212 0 10\n/dev/sda2 partition 8388604 0 -2\n")
        self.assertEqual(m.ssd_gerenciavel(com_part), (False, None))
        apagado = m.ler_swaps(cab + "/swap/swapfile\\040(deleted) file 16777212 0 10\n")
        self.assertEqual(m.ssd_gerenciavel(apagado), (False, None))

    def test_camadas_e_resumos(self):
        s = m.ler_swaps(SWAPS)
        linhas = m.camadas(s)
        self.assertEqual([t for _, t, _ in linhas], ["Compressed RAM · fast", "SSD · for things not used in a while"])
        self.assertEqual(linhas[0][2], "4.0 GB · priority 100 · used first")
        self.assertEqual(linhas[1][2], "16.0 GB · priority 10")
        self.assertEqual(m.camadas([])[0][1], "No swap is set up")
        fora = m.camadas(m.ler_swaps("h\n/dev/zram0 partition 4194300 0 100\n/swapfile file 8388604 0 -2\n"))
        self.assertEqual(fora[1][2], "8.0 GB · priority -2 · managed outside Aero OS")
        self.assertNotIn("outside", fora[0][2])
        texto, fracao = m.resumo_zram(s, [(3 << 30, 1 << 30, 1 << 30)])
        self.assertEqual(texto, "1.0 GB used of 4.0 GB · takes 1.0 GB of RAM (3.0× smaller)")
        self.assertAlmostEqual(fracao, 0.25, places=3)
        self.assertEqual(m.resumo_zram(s, [None])[0], "1.0 GB used of 4.0 GB")
        self.assertEqual(m.resumo_zram(s, [(4096, 64, 20480)])[0], "1.0 GB used of 4.0 GB")   # quase vazio
        self.assertEqual(m.resumo_zram(s, [(8 << 20, 9 << 20, 9 << 20)])[0], "1.0 GB used of 4.0 GB")
        self.assertEqual(m.resumo_zram([], []), ("Not set up", None))
        self.assertEqual(m.resumo_ssd(s), ("0 MB used of 16.0 GB", 0.0))
        self.assertEqual(m.resumo_ssd([]), ("Not set up", None))


def ler(caminho):
    with open(caminho) as f:
        return f.read()


def conf_agente(texto):
    return m.conf_valida(texto, m.PADRAO_AGENTE, ruim_vira_padrao=False)


class TestConfig(unittest.TestCase):
    def test_conf_valida_formato(self):
        c = m.conf_valida("# comentário\nenabled=1   # liga\n\n kill_on_critical = 0\nlixo\n=5\nprotect=a,b\n"
                          "# pause_background=0\nwarn_available # x =20\n", m.PADRAO_GUARDA)
        self.assertEqual(c, {"enabled": "1", "kill_on_critical": "0"})        # só as chaves conhecidas
        self.assertEqual(m.conf_valida(None, m.PADRAO_GUARDA), {})
        self.assertEqual(m.conf_valida("enabled=0\n" * 500 + "enabled=1\n", m.PADRAO_GUARDA),
                         {"enabled": "0"})                                       # só 500 linhas, como eles

    def test_conf_valida_regras_do_guarda(self):
        # aero-memory _bool/_pct: yes/no/true/false/on/off, 1..50; valor ruim = padrão; vale a última linha
        c = m.conf_valida("enabled=no\npause_background = TRUE\nkill_on_critical=Off\nwarn_available=05\n"
                          "critical_available=3\n", m.PADRAO_GUARDA)
        self.assertEqual(c, {"enabled": "0", "pause_background": "1", "kill_on_critical": "0",
                             "warn_available": "5", "critical_available": "3"})
        for ruim in ("sim", "", "2", "1 0", "yess"):
            self.assertEqual(m.conf_valida(f"enabled={ruim}\n", m.PADRAO_GUARDA), {}, ruim)
        for ruim in ("0", "51", "99999", "015", "-3", "1.5", "x"):
            self.assertEqual(m.conf_valida(f"warn_available={ruim}\n", m.PADRAO_GUARDA), {}, ruim)
        self.assertEqual(m.conf_valida("enabled=0\nenabled=talvez\n", m.PADRAO_GUARDA), {})   # ruim = padrão

    def test_conf_valida_regras_do_agente(self):
        # aero-memory-agent: BOOL_VALUES, minutos 0..1440 (zeros à esquerda valem); valor ruim é ignorado
        c = conf_agente("comfy_free_on_close=off\ncomfy_free_for_games=Yes\ncomfy_idle_unload_min=015\n"
                        "notify=0\ncomfy_port=8188\n")
        self.assertEqual(c, {"comfy_free_on_close": "0", "comfy_free_for_games": "1",
                             "comfy_idle_unload_min": "15", "notify": "0"})
        self.assertEqual(conf_agente("comfy_idle_unload_min=1440\n"), {"comfy_idle_unload_min": "1440"})
        for ruim in ("2000", "1441", "-1", "abc", "", "1e3", "000015", "²"):
            self.assertEqual(conf_agente(f"comfy_idle_unload_min={ruim}\n"), {}, ruim)
        self.assertEqual(conf_agente("comfy_free_on_close=0\ncomfy_free_on_close=talvez\n"),
                         {"comfy_free_on_close": "0"})                         # fica o que já tinha

    def test_config_guarda(self):
        self.assertEqual(m.config_guarda({}), m.PADRAO_GUARDA)
        self.assertEqual(m.config_guarda({"enabled": "0", "warn_available": "20", "critical_available": "5",
                                          "outra": "1"}),
                         dict(m.PADRAO_GUARDA, enabled="0", warn_available="20", critical_available="5"))
        # critical >= warn: os dois voltam ao padrão (como o aero-memory faz)
        for par in ({"warn_available": "10", "critical_available": "10"}, {"critical_available": "20"},
                    {"warn_available": "3"}):
            cfg = m.config_guarda(par)
            self.assertEqual((cfg["warn_available"], cfg["critical_available"]), ("15", "4"), par)
        # o que vem do state.json também passa pelas regras
        self.assertEqual(m.config_guarda({"enabled": "15", "warn_available": "9999"}), m.PADRAO_GUARDA)

    def test_pasta_config(self):
        # mesma conta do config_path() do agente
        self.assertEqual(m.pasta_config({"XDG_CONFIG_HOME": "/x/cfg"}), "/x/cfg")
        self.assertEqual(m.pasta_config({"XDG_CONFIG_HOME": ""}), os.path.expanduser("~/.config"))
        self.assertEqual(m.pasta_config({}), os.path.expanduser("~/.config"))
        esperado = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                                "aero", "memory-agent.conf")
        self.assertEqual(m.CONF_AGENTE, esperado)

    def test_atualizar_conf(self):
        texto = "# meu comentário\ncomfy_port=8188  # porta\ncomfy_free_on_close=1\ncomfy_free_on_close=1\n"
        novo = m.atualizar_conf(texto, {"comfy_free_on_close": "0", "comfy_idle_unload_min": "30"})
        self.assertEqual(novo, "# meu comentário\ncomfy_port=8188  # porta\ncomfy_free_on_close=0\n"
                               "comfy_idle_unload_min=30\n")
        self.assertTrue(m.atualizar_conf("", {"a": "1"}).startswith("#"))
        self.assertTrue(m.atualizar_conf(None, {"a": "1"}).endswith("a=1\n"))
        # comentário com a chave não é trocado
        self.assertEqual(m.atualizar_conf("#a=5\n", {"a": "1"}), "#a=5\na=1\n")
        for chave, valor in (("a", "1\nenabled=0"), ("a b", "1"), ("A", "1"), ("a", "$(x)"), ("", "1"),
                             ("a", "1 # x")):
            with self.assertRaises(ValueError):
                m.atualizar_conf("", {chave: valor})

    def test_gravar_conf(self):
        with tempfile.TemporaryDirectory() as d:
            arq = os.path.join(d, "sub", "memory-agent.conf")
            m.gravar_conf(arq, {"comfy_free_on_close": "0"})
            self.assertEqual(conf_agente(ler(arq)), {"comfy_free_on_close": "0"})
            m.gravar_conf(arq, {"comfy_idle_unload_min": "5"})
            self.assertEqual(conf_agente(ler(arq)), {"comfy_free_on_close": "0", "comfy_idle_unload_min": "5"})
            self.assertEqual(os.stat(arq).st_mode & 0o777, 0o600)
            # link de dotfiles: grava no destino e o link continua sendo link
            real = os.path.join(d, "real.conf")
            with open(real, "w") as f:
                f.write("# meu\nx=1\n")
            link = os.path.join(d, "link.conf")
            os.symlink(real, link)
            m.gravar_conf(link, {"comfy_free_for_games": "0"})
            self.assertTrue(os.path.islink(link))
            self.assertEqual(ler(real), "# meu\nx=1\ncomfy_free_for_games=0\n")
            # nada de temporários largados
            self.assertEqual(sorted(os.listdir(d)), ["link.conf", "real.conf", "sub"])
            self.assertEqual(os.listdir(os.path.join(d, "sub")), ["memory-agent.conf"])
            with self.assertRaises(ValueError):
                m.gravar_conf(arq, {"comfy_free_on_close": "0\nevil=1"})
            self.assertNotIn("evil", ler(arq))
            # arquivo que não dá pra ler (ex: FIFO no lugar): recusa em vez de apagar o que tinha
            fifo = os.path.join(d, "fifo.conf")
            os.mkfifo(fifo)
            with self.assertRaises(OSError):
                m.gravar_conf(fifo, {"a": "1"})


# ---------- contrato com os serviços de verdade (os arquivos do repositório) ----------

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def carregar(nome, caminho):
    """Carrega um script sem .py como módulo (sem gravar __pycache__ ao lado dele)."""
    import importlib.machinery
    import importlib.util
    antes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        loader = importlib.machinery.SourceFileLoader(nome, caminho)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(nome, loader))
        loader.exec_module(mod)
        return mod
    finally:
        sys.dont_write_bytecode = antes


class TestContrato(unittest.TestCase):
    """A página lê as configs com as MESMAS regras dos serviços e entende as mensagens do ajudante.
    Compara com o código de verdade do aero-memory, do aero-memory-agent e do aero-memory-config."""

    @classmethod
    def setUpClass(cls):
        cls.guarda = carregar("aero_memory_t", os.path.join(REPO, "system", "memory", "aero-memory"))
        cls.agente = carregar("aero_memory_agent_t", os.path.join(REPO, "bin", "aero-memory-agent"))
        cls.ajudante = os.path.join(REPO, "system", "memory", "aero-memory-config")
        cls.config = carregar("aero_memory_config_t", cls.ajudante)

    def test_mesmos_padroes(self):
        for k, v in m.PADRAO_GUARDA.items():
            self.assertEqual(str(int(self.guarda.DEFAULTS[k])), v, k)
        for k, v in m.PADRAO_AGENTE.items():
            self.assertEqual(str(self.agente.DEFAULTS[k]), v, k)

    def test_mesma_leitura_de_config(self):
        sorteio = random.Random(20261020)
        valores = ["1", "0", "yes", "No", "TRUE", "false", "on", "OFF", "2", "", "sim", "5", "05", "015", "15",
                   "4", "50", "51", "1440", "1441", "2000", "00015", "000015", " 7 ", "1e3", "-1", "1=2", "²",
                   "3 # comentário"]
        chaves = list(m.PADRAO_GUARDA) + list(m.PADRAO_AGENTE) + ["protect", "outra"]
        for _ in range(3000):
            linhas = []
            for _ in range(sorteio.randint(0, 8)):
                k, v = sorteio.choice(chaves), sorteio.choice(valores)
                linhas.append(sorteio.choice([f"{k}={v}", f" {k} = {v}", f"#{k}={v}", f"{k}"]))
            texto = "\n".join(linhas)
            cfg, _problemas = self.guarda.parse_config(texto)
            servico = {k: str(int(cfg[k])) for k in m.PADRAO_GUARDA}
            self.assertEqual(m.config_guarda(m.conf_valida(texto, m.PADRAO_GUARDA)), servico, texto)
            conf = self.agente.parse_config(texto)
            servico = {k: str(conf[k]) for k in m.PADRAO_AGENTE}
            self.assertEqual(dict(m.PADRAO_AGENTE) | conf_agente(texto), servico, texto)

    def test_mesmos_nomes_e_contas(self):
        g, a = self.guarda, self.agente
        self.assertEqual(m.ZRAM_VALE, g.ZRAM_GAIN)
        self.assertEqual((m.TRAVANDO_FULL, m.TRAVANDO_RAPIDO), (g.THRASH_FULL, g.THRASH_FAST_PCT))
        self.assertEqual(m.COMM_LEN, g.COMM_LEN)
        self.assertEqual(m.NOME_PROCESSO.pattern, g.NAME_RE.pattern)
        self.assertEqual(m.AMIGAVEIS, a.FRIENDLY)                     # a página e a notificação falam igual
        for nome in ("fossilize_replay", "fossilize_repla", "firefox"):
            self.assertEqual(m.nome_bonito(nome), a.friendly(nome), nome)
        # caminhos e nomes que a página usa = os dos serviços
        self.assertEqual(m.ESTADO, os.path.join(g.RUN_DIR, "state.json"))
        self.assertEqual(m.CONF_GUARDA, g.CONF_PATH)
        self.assertEqual(m.CONF_AGENTE, a.config_path())
        self.assertEqual(os.path.basename(m.ESTADO_AGENTE), "aero-memory-agent.json")
        with open(os.path.join(REPO, "system", "memory", "install.sh"), encoding="utf-8") as f:
            self.assertIn(f"aero-memory-config|{m.AJUDANTE}|755", f.read())
        with open(os.path.join(REPO, "config", "systemd", "user", "aero-memory-agent.service")) as f:
            unidade = f.read()
        self.assertIn("ExecStart=%h/" + os.path.relpath(m.AGENTE, os.path.expanduser("~")), unidade)
        self.assertIn("ExecReload=", unidade)                         # o try-reload-or-restart da página
        with open(os.path.join(REPO, "system", "memory", "aero-memory.service")) as f:
            self.assertIn("ExecReload=", f.read())

    def test_mesmo_nivel_do_guarda(self):
        """Sem o serviço, a página estima o nível com as contas do decide_level (partindo de normal).
        Mesmos arquivos de /proc de mentira pros dois: o read_readings dele e as leituras daqui."""
        sorteio = random.Random(1020)
        gb = 1024 * 1024
        vistos = set()
        with tempfile.TemporaryDirectory() as proc:
            os.makedirs(os.path.join(proc, "pressure"))
            caminhos = {"meminfo": os.path.join(proc, "meminfo"), "swaps": os.path.join(proc, "swaps"),
                        "psi": os.path.join(proc, "pressure", "memory")}
            for _ in range(1500):
                total = sorteio.randint(1 * gb, 64 * gb)
                swaps = []
                for nome, prio in sorteio.sample([("/dev/zram0", 100), ("/dev/zram1", 100), ("/swap/swapfile", 10),
                                                  ("/swapfile", -2), ("/dev/nvme0n1p3", sorteio.choice([-2, 100]))],
                                                 sorteio.randint(0, 3)):
                    tam = sorteio.randint(0, 32 * gb)
                    usado = sorteio.choice([0, tam, sorteio.randint(0, tam), int(tam * 0.97), tam + 5])
                    swaps.append((nome, tam, usado if sorteio.random() > 0.03 else -1, prio))
                swap_total = sum(max(t, 0) for _, t, _, _ in swaps)
                swap_livre = swap_total - sum(min(max(u, 0), t) for _, t, u, _ in swaps)
                if sorteio.random() < 0.1:
                    swap_livre = sorteio.randint(0, swap_total + 10)          # meminfo e swaps sem bater
                disp = sorteio.choice([sorteio.randint(0, total), sorteio.randint(0, total // 10),
                                       sorteio.randint(0, total // 20), total + 100])
                with open(caminhos["meminfo"], "w") as f:
                    f.write(f"MemTotal: {total} kB\nMemFree: 1 kB\nMemAvailable: {disp} kB\n"
                            f"SwapTotal: {swap_total} kB\nSwapFree: {swap_livre} kB\n")
                with_swaps = sorteio.random() > 0.05
                if with_swaps:
                    with open(caminhos["swaps"], "w") as f:
                        f.write("Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n")
                        for nome, tam, usado, prio in swaps:
                            tipo = "file" if "swapfile" in nome else "partition"
                            f.write(f"{nome}{' ' * 30}{tipo}\t\t{tam}\t\t{usado}\t\t{prio}\n")
                elif os.path.exists(caminhos["swaps"]):
                    os.unlink(caminhos["swaps"])
                some = sorteio.choice([0.0, 3.0, 19.9, 20.5, 60.0, 150.0, sorteio.uniform(0, 100)])
                full = sorteio.choice([0.0, 9.0, 10.5, 29.0, 31.0, 120.0, sorteio.uniform(0, 100)])
                with_psi = sorteio.random() > 0.05
                if with_psi:
                    with open(caminhos["psi"], "w") as f:
                        f.write(f"some avg10={some:.2f} avg60=0.00 avg300=0.00 total=1\n"
                                f"full avg10={full:.2f} avg60=0.00 avg300=0.00 total=1\n")
                elif os.path.exists(caminhos["psi"]):
                    os.unlink(caminhos["psi"])
                aviso = sorteio.randint(2, 50)
                critico = sorteio.randint(1, aviso - 1)
                cfg = dict(self.guarda.DEFAULTS, warn_available=aviso, critical_available=critico)
                r = self.guarda.read_readings(proc_root=proc)
                dele, _ = self.guarda.decide_level(self.guarda.NORMAL, None, r, cfg, 0.0)
                texto_swaps = m.ler_texto(caminhos["swaps"])
                daqui = m.nivel_local(m.ler_meminfo(m.ler_texto(caminhos["meminfo"])),
                                      m.ler_psi(m.ler_texto(caminhos["psi"])),
                                      m.ler_swaps(texto_swaps) if texto_swaps is not None else None,
                                      aviso, critico)
                self.assertEqual(daqui, dele, (ler(caminhos["meminfo"]), swaps, with_swaps, some, full, with_psi,
                                               aviso, critico))
                vistos.add(dele)
        self.assertEqual(vistos, {"normal", "warning", "critical"})        # o sorteio passou pelos três

    def test_estado_de_um_guarda_de_verdade(self):
        """O aero-memory de verdade pausa um processo (nome comprido: o kernel corta em 15 letras) e
        grava o state.json; a página mostra o nome inteiro e esconde os nomes de quem é de outro usuário."""
        g_mod = self.guarda
        if not g_mod.is_user((os.getuid(),) * 4):
            self.skipTest("o aero-memory só pausa processos de pessoas (uid 1000..60000)")
        nome = "aerot" + "".join(random.choice("abcdefghij") for _ in range(11))     # 16 letras
        codigo = (f"import ctypes, time\nctypes.CDLL(None).prctl(15, {nome!r}.encode(), 0, 0, 0)\n"
                  "print('ready', flush=True)\ntime.sleep(60)\n")
        filho = subprocess.Popen([sys.executable, "-I", "-c", codigo], stdout=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory() as d:
            g = None
            try:
                filho.stdout.readline()
                g = g_mod.Guard("/nonexistent.conf", os.path.join(d, "run"))
                g.cfg = dict(g_mod.DEFAULTS, background=(nome,))
                g.level = g_mod.WARNING
                gib = 1024 ** 3
                r = {"mem_total": 32 * gib, "mem_available": gib, "swap_total": 4 * gib, "swap_free": 0,
                     "psi_some": 30.0, "psi_full": 0.0}
                with contextlib.redirect_stderr(io.StringIO()):
                    g.pressure_actions(time.monotonic(), r)
                    g.write_state(r)
                with open(os.path.join(d, "run", "state.json")) as f:
                    est = m.ler_estado(f.read(), time.time())
                self.assertTrue(est["vivo"])
                self.assertEqual(est["nivel"], "warning")
                self.assertEqual(est["pausados"], [nome])                  # e não o "aerot..." cortado
                eu = os.getuid()
                self.assertTrue(m.descrever_acao(est["ultima"], time.time(), est["conhecidos"], eu)
                                .startswith(f"Paused {nome}"), est["ultima"])
                self.assertTrue(m.descrever_acao(est["ultima"], time.time(), est["conhecidos"], eu + 1)
                                .startswith("Paused another user's background work"))
                self.assertIn(f"holding {nome}", m.texto_guarda(True, est, True, eu)[1])
                with contextlib.redirect_stderr(io.StringIO()):
                    g.resume_all("test", "calm")
                    g.write_state(r)
                with open(os.path.join(d, "run", "state.json")) as f:
                    est = m.ler_estado(f.read(), time.time())
                self.assertEqual(est["pausados"], [])
                self.assertTrue(m.descrever_acao(est["ultima"], time.time(), est["conhecidos"], eu)
                                .startswith(f"Resumed {nome}"), est["ultima"])
            finally:
                if g is not None:
                    g.store.close()
                try:
                    os.kill(filho.pid, signal.SIGCONT)                     # nunca deixar parado
                    filho.kill()
                except ProcessLookupError:
                    pass
                filho.wait(timeout=5)
                filho.stdout.close()

    def test_motivo_das_mensagens_do_ajudante(self):
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "memory.conf")
            for args, esperado in ((["set", "enabled", "5"], "enabled must be 0 or 1"),
                                   (["set", "critical_available", "20"],
                                    "critical_available (20) must be lower than warn_available (15)")):
                r = subprocess.run([sys.executable, "-I", self.ajudante, "--config-path", conf] + args,
                                   capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(m.motivo_da_falha(r), esperado)
        # aero-swap-setup recusou (4) -> o ajudante sai com 3 e "not now: ..." no stderr
        erro = self.config.swap_setup_result(4, "warning: zram is off\nerror: another disk swap is already on "
                                                "(/swapfile). Aero keeps a single SSD swap.\n")
        r = subprocess.CompletedProcess([], 3, "", f"not now: {erro}\n")
        self.assertEqual(m.motivo_da_falha(r), "another disk swap is already on (/swapfile). Aero keeps a "
                                               "single SSD swap.")


class TestEntradaNoMain(unittest.TestCase):
    """A página só aparece se o main.py importar SecaoMemoria e puser ("Memory", SecaoMemoria, None)
    logo depois de Power no grupo System. Lê o main.py com ast (não abre o app nem importa o GTK dele).
    Na instalação, a mesma entrada vai à mão no main.py INSTALADO (que tem coisas a mais, como a busca);
    nunca copiar o main.py/comum.py do repositório por cima do instalado."""

    def test_memory_logo_depois_de_power(self):
        import ast
        with open(os.path.join(REPO, "settings", "main.py"), encoding="utf-8") as f:
            arvore = ast.parse(f.read())
        importa = any(isinstance(n, ast.ImportFrom) and n.module == "sec_memoria"
                      and any(a.name == "SecaoMemoria" for a in n.names) for n in arvore.body)
        self.assertTrue(importa, "main.py não importa SecaoMemoria de sec_memoria")
        grupo = None
        for n in arvore.body:
            if isinstance(n, ast.Assign) and any(getattr(a, "id", "") == "ARVORE" for a in n.targets):
                for item in n.value.elts:
                    if ast.literal_eval(item.elts[0]) == "System":
                        grupo = item.elts[2]
        self.assertIsNotNone(grupo, "grupo System não achado no ARVORE")
        nomes = [ast.literal_eval(e.elts[0]) for e in grupo.elts]
        self.assertIn("Memory", nomes)
        self.assertEqual(nomes[nomes.index("Memory") - 1], "Power")
        entrada = grupo.elts[nomes.index("Memory")]
        self.assertEqual(entrada.elts[1].id, "SecaoMemoria")
        self.assertIsNone(ast.literal_eval(entrada.elts[2]))
        self.assertEqual(m.SecaoMemoria.titulo, "Memory")


# ---------- widgets (precisam de tela) ----------

def tem_tela():
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")):
        return False
    try:
        Adw.init()
        return True
    except Exception:  # noqa: BLE001
        return False


class RaizFalsa(GObject.Object):
    """Faz o papel da janela do Settings: só a propriedade "suspended" (com o aviso notify::suspended)."""
    suspended = GObject.Property(type=bool, default=False)

    def is_suspended(self):
        return self.suspended


class JanelaFalsa:
    def __init__(self):
        self.avisos = []

    def avisar(self, texto):
        self.avisos.append(texto)


def girar(ate, segundos=3.0):
    """Roda o laço da GLib até `ate()` ficar verdadeiro (ou o tempo acabar)."""
    ctx = GLib.MainContext.default()
    fim = time.monotonic() + segundos
    while time.monotonic() < fim:
        while ctx.pending():
            ctx.iteration(False)
        if ate():
            return True
        time.sleep(0.01)
    return ate()


@unittest.skipUnless(tem_tela(), "sem tela (Wayland/X) pra criar widgets")
class TestPagina(unittest.TestCase):
    def setUp(self):
        self.pasta = tempfile.TemporaryDirectory()
        d = self.pasta.name
        self.chamadas_root, self.abertos = [], []
        self.resposta_root, self.erro_root = 1, ""
        ajudante = os.path.join(d, "aero-memory-config")
        with open(ajudante, "w") as f:
            f.write("#!/bin/sh\nexit 1\n")
        self.ajudante = ajudante

        def root_falso(cmd):
            self.chamadas_root.append(list(cmd))
            return subprocess.CompletedProcess(cmd, self.resposta_root, "", self.erro_root)

        def rodar_falso(cmd, **_kw):
            return subprocess.CompletedProcess(cmd, 3, "inactive\n", "")
        self.trocas = [
            mock.patch.object(m, "AJUDANTE", ajudante),
            mock.patch.object(m, "ESTADO", os.path.join(d, "state.json")),
            mock.patch.object(m, "CONF_GUARDA", os.path.join(d, "memory.conf")),
            mock.patch.object(m, "CONF_AGENTE", os.path.join(d, "aero", "memory-agent.conf")),
            mock.patch.object(m, "ESTADO_AGENTE", os.path.join(d, "aero-memory-agent.json")),
            mock.patch.object(m, "AGENTE", os.path.join(d, "aero-memory-agent")),
            mock.patch.object(m, "UNIDADES_AGENTE", (os.path.join(d, "unit"),)),
            mock.patch.object(m, "rodar_como_root", root_falso),
            mock.patch.object(m, "rodar", rodar_falso),
            mock.patch.object(m, "abrir", lambda cmd: self.abertos.append(list(cmd))),
        ]
        for t in self.trocas:
            t.start()
        self.janela = JanelaFalsa()

    def tearDown(self):
        for t in reversed(self.trocas):
            t.stop()
        self.pasta.cleanup()

    def instalar(self, guarda=True, agente=True):
        os.chmod(self.ajudante, 0o755 if guarda else 0o644)
        if agente:
            open(m.AGENTE, "w").close()

    def test_sem_nada_instalado(self):
        os.chmod(self.ajudante, 0o644)                    # não executável = não instalado
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.assertEqual(s.status.get_title(), "Memory guard is not installed")
        self.assertEqual(s.comfy.get_title(), "ComfyUI helper is not installed")
        for r in list(s.chaves.values()) + [s.ao_fechar, s.ocioso, s.jogos]:
            self.assertFalse(r.get_sensitive())
        for r in s.chaves.values():
            self.assertFalse(r.get_active())
        self.assertFalse(s.tamanho_ssd.get_visible())
        self.assertTrue(s.ram.get_subtitle().endswith("available"))
        self.assertIn(s.selo.get_label(), ("Calm", "Busy", "Critical"))
        # o switch não grava nada mesmo se mudar (ex: alguém mexeu pelo teclado) e volta sem ficar em laço
        avisos = []
        s.chaves["enabled"].connect("notify::active", lambda *_: avisos.append(1))
        s.chaves["enabled"].set_active(True)
        self.assertTrue(girar(lambda: not s.chaves["enabled"].get_active()))
        girar(lambda: False, 0.2)
        self.assertFalse(s.chaves["enabled"].get_active())
        self.assertEqual(len(avisos), 2)                                # foi e voltou, uma vez só
        self.assertEqual(self.chamadas_root, [])
        s.ao_sumir()
        self.assertIsNone(s.relogio)

    def test_relogio_so_com_a_pagina_na_tela(self):
        s = m.SecaoMemoria(self.janela)
        self.assertIsNone(s.relogio)                      # o construtor não liga nada
        s.ao_mostrar()
        self.assertIsNotNone(s.relogio)
        s.ligar_relogio()                                 # chamar de novo não cria um segundo
        primeiro = s.relogio
        s.ligar_relogio()
        self.assertEqual(s.relogio, primeiro)
        # a página nunca apareceu (sem janela): o próprio tique desliga o relógio
        self.assertEqual(s.tique(), GLib.SOURCE_REMOVE)
        self.assertIsNone(s.relogio)
        GLib.source_remove(primeiro)                      # (o laço não rodou; tira a fonte à mão)
        s.ao_mostrar()
        s.ao_sumir()
        self.assertIsNone(s.relogio)
        s.ao_sumir()                                      # duas vezes não dá erro

    @unittest.skipUnless(os.environ.get("AERO_TESTE_JANELA") == "1",
                         "abre uma janela de verdade: só com AERO_TESTE_JANELA=1 (de preferência num "
                         "workspace escondido do Hyprland)")
    def test_relogio_numa_janela_de_verdade(self):
        self.instalar()
        win = Gtk.Window()
        s = m.SecaoMemoria(self.janela)
        win.set_child(s.pagina)
        s.ao_mostrar()
        win.present()
        self.assertTrue(girar(lambda: s.pagina.get_mapped()))
        self.assertIsNotNone(s.relogio)
        win.set_child(None)                               # trocar de seção = a página sai da tela
        self.assertTrue(girar(lambda: not s.pagina.get_mapped()))
        self.assertIsNone(s.relogio)
        win.set_child(s.pagina)                           # voltou: o relógio volta (sinal "map")
        self.assertTrue(girar(lambda: s.pagina.get_mapped()))
        self.assertIsNotNone(s.relogio)
        win.destroy()
        self.assertTrue(girar(lambda: s.relogio is None))

    def test_switch_volta_se_o_root_falhar(self):
        self.instalar()
        with open(m.CONF_GUARDA, "w") as f:
            f.write("enabled=1\npause_background=1\nkill_on_critical=1\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        sw = s.chaves["kill_on_critical"]
        self.assertTrue(sw.get_active() and sw.get_sensitive())
        threads = threading.active_count()
        self.resposta_root = 1                            # senha cancelada
        sw.set_active(False)
        self.assertFalse(sw.get_sensitive())              # ocupado: um comando por vez
        self.assertFalse(s.chaves["enabled"].get_sensitive())
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(self.chamadas_root, [[self.ajudante, "set", "kill_on_critical", "0"]])
        self.assertTrue(sw.get_active())                  # voltou
        self.assertTrue(sw.get_sensitive())
        self.assertEqual(self.janela.avisos, ["Not saved: password cancelled"])
        self.assertTrue(girar(lambda: threading.active_count() <= threads))
        s.ao_sumir()

    def test_switch_grava_se_o_root_der_certo(self):
        self.instalar()
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.resposta_root = 0
        s.chaves["enabled"].set_active(False)
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(self.chamadas_root, [[self.ajudante, "set", "enabled", "0"]])
        self.assertFalse(s.chaves["enabled"].get_active())
        self.assertEqual(s.guarda["enabled"], "0")
        # guarda desligado: as outras duas ficam cinzas
        self.assertFalse(s.chaves["pause_background"].get_sensitive())
        self.assertEqual(self.janela.avisos, [])
        # voltar à página no meio de outra gravação não desfaz o clique
        s.chaves["enabled"].set_active(True)
        s.ao_mostrar()
        self.assertTrue(s.chaves["enabled"].get_active())
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(s.guarda["enabled"], "1")
        s.ao_sumir()

    def test_estado_do_servico(self):
        self.instalar()
        agora = time.time()
        eu = os.getuid()
        # o formato do aero-memory de verdade: comm cortado em 15 letras e o uid do dono
        with open(m.ESTADO, "w") as f:
            f.write('{"t": %d, "level": "critical", "paused": ["fossilize_repla"], "paused_count": 2, '
                    '"config": {"enabled": 1, "background": ["fossilize_replay"], "protect": []}, '
                    '"last_action": {"t": %d, "seq": 9, "kind": "paused", "uid": %d, "names": ["fossilize_repla"], '
                    '"count": 2, "mb": 1500}}' % (agora, agora - 130, eu))
        with open(m.ESTADO_AGENTE, "w") as f:
            f.write('{"comfy": {"running": true, "models_loaded": true, "idle_s": 120}}')
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.assertEqual(s.selo.get_label(), "Critical")
        self.assertIn("error", s.selo.get_css_classes())
        self.assertIn("holding Steam shader compilation", s.status.get_subtitle())
        self.assertEqual(s.ultima.get_subtitle(), "Paused Steam shader compilation (2 processes, 1.5 GB) · 2 min ago")
        self.assertTrue(s.comfy.get_subtitle().startswith("Running · models loaded · idle 2 min"))
        # systemctl (falso) diz "inactive" pro agente: a página avisa que ele não está rodando
        self.assertTrue(girar(lambda: not s.checando))
        self.assertIn("isn't running", s.comfy.get_subtitle())
        # o guarda fechou o app de OUTRO usuário: a página não diz o nome dele
        with open(m.ESTADO, "w") as f:
            f.write('{"t": %d, "level": "critical", "paused": ["fossilize_repla"], "paused_uids": [%d], '
                    '"config": {"enabled": 1}, "last_action": {"t": %d, "kind": "killed", "uid": %d, '
                    '"name": "firefox", "pid": 4242, "mb": 3100, "count": 1}}' % (agora, eu + 1, agora, eu + 1))
        s.atualizar()
        self.assertEqual(s.ultima.get_subtitle(), "Closed another user's app (it was using 3.0 GB) to prevent a "
                                                  "freeze · just now")
        self.assertIn("holding background work until memory frees up", s.status.get_subtitle())
        s.ao_sumir()

    def test_dialogo_de_criar_o_swap_avisa_da_privacidade(self):
        self.instalar()
        with open(self.ajudante, "a") as f:
            f.write("# swap-size N\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        dialogos = []
        for cifrado_, ssd, esperado in ((False, None, "This disk isn't encrypted"),
                                        (None, None, "If this disk isn't encrypted"),
                                        (True, None, None), (False, 16, None)):
            s.montar_ssd(m.ler_swaps(SWAPS if ssd else "h\n/dev/zram0 partition 4194300 0 100\n"))
            self.assertEqual(s.ssd_atual, ssd)
            with mock.patch.object(m, "raiz_cifrada", lambda _mi, c=cifrado_: c), \
                    mock.patch.object(Adw.AlertDialog, "present", lambda d, pai: dialogos.append(d)):
                s.tamanho_ssd.set_selected(m.indice(s.opcoes_ssd, 32))      # a pessoa escolhe 32 GB
            d = dialogos[-1]
            self.assertEqual(d.get_heading(), "Change the SSD swap to 32 GB?")
            self.assertFalse(d.get_body_use_markup())
            if esperado:
                self.assertIn(esperado, d.get_body())
            else:
                self.assertNotIn("encrypted", d.get_body())
            d.emit("response", "cancel")                                    # cancelar: a lista volta
            self.assertEqual(m.escolhido(s.tamanho_ssd, s.opcoes_ssd), ssd)
        self.assertEqual(len(dialogos), 4)
        self.assertEqual(self.chamadas_root, [])                            # cancelado: nada de senha
        s.ao_sumir()

    def test_comfy_grava_config_e_reinicia_o_agente(self):
        self.instalar()
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.assertTrue(s.ao_fechar.get_sensitive() and s.ao_fechar.get_active())
        s.ao_fechar.set_active(False)
        s.ocioso.set_selected(m.indice(s.opcoes_ocioso, "60"))
        with open(m.CONF_AGENTE) as f:
            conf = conf_agente(f.read())
        self.assertEqual(conf, {"comfy_free_on_close": "0", "comfy_idle_unload_min": "60"})
        self.assertIsNotNone(s.recarga)
        self.assertEqual(self.abertos, [])                # junta os cliques: ainda não reiniciou
        self.assertTrue(girar(lambda: s.recarga is None, 2))
        self.assertEqual(self.abertos, [["systemctl", "--user", "--no-block", "try-reload-or-restart",
                                         "aero-memory-agent.service"]])
        # sair da página com reinício pendente: faz na hora
        s.jogos.set_active(False)
        s.ao_sumir()
        self.assertIsNone(s.recarga)
        self.assertEqual(len(self.abertos), 2)
        self.assertEqual(self.chamadas_root, [])          # ComfyUI nunca pede senha

    def test_comfy_valor_editado_a_mao(self):
        self.instalar()
        os.makedirs(os.path.dirname(m.CONF_AGENTE))
        with open(m.CONF_AGENTE, "w") as f:
            f.write("comfy_idle_unload_min=10\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.assertEqual(m.escolhido(s.ocioso, s.opcoes_ocioso), "10")
        self.assertEqual(self.abertos, [])                # mostrar não grava nada
        with open(m.CONF_AGENTE) as f:
            self.assertEqual(f.read(), "comfy_idle_unload_min=10\n")
        s.ao_sumir()

    def test_comfy_falha_ao_gravar(self):
        self.instalar()
        os.makedirs(os.path.dirname(m.CONF_AGENTE))
        os.mkfifo(m.CONF_AGENTE)                          # não dá pra ler/gravar
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        s.jogos.set_active(False)
        self.assertTrue(girar(lambda: s.jogos.get_active()))            # voltou
        girar(lambda: False, 0.2)                                       # (nenhum aviso atrasado depois)
        self.assertTrue(s.jogos.get_active())
        self.assertEqual(self.janela.avisos, ["Couldn't save the ComfyUI settings"])
        self.assertIsNone(s.recarga)
        s.ao_sumir()

    def test_tamanho_do_swap(self):
        self.instalar()
        with open(self.ajudante, "a") as f:
            f.write("# swap-size N\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        # mexer no combo pelo código (carregando) não abre diálogo nem grava
        s.montar_ssd(m.ler_swaps(SWAPS))
        self.assertTrue(s.tamanho_ssd.get_visible())
        self.assertEqual(s.ssd_atual, 16)
        self.assertEqual(m.escolhido(s.tamanho_ssd, s.opcoes_ssd), 16)
        explicados = []
        s.explicar = lambda titulo, texto: explicados.append((titulo, texto))
        s.trocar_ssd(32)                                  # o "Change" do diálogo; senha cancelada (sudo: 1)
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(self.chamadas_root, [[self.ajudante, "swap-size", "32"]])
        self.assertEqual(self.janela.avisos, ["Swap not changed: password cancelled"])
        self.assertEqual(explicados, [])
        # recusado pelo aero-swap-setup (via ajudante, código 3): o motivo aparece numa caixa
        self.resposta_root = 3
        self.erro_root = ("warning: x\nnot now: aero-swap-setup: error: not enough free space: a 32.0 GB swap "
                          "file would leave less than 20% of the disk free\n")
        s.trocar_ssd(32)
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(explicados, [("Swap not changed", "Not enough free space: a 32.0 GB swap file would "
                                                           "leave less than 20% of the disk free")])
        self.assertEqual(len(self.janela.avisos), 1)
        self.resposta_root = 0
        s.trocar_ssd(8)
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertEqual(self.janela.avisos[-1], "SSD swap is now 8 GB")
        self.assertTrue(s.tamanho_ssd.get_sensitive())
        s.ao_sumir()

    def test_swap_de_fora_nao_oferece_tamanho(self):
        self.instalar()
        with open(self.ajudante, "a") as f:
            f.write("# swap-size N\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        s.montar_ssd(m.ler_swaps(SWAPS))
        self.assertTrue(s.tamanho_ssd.get_visible())
        s.montar_ssd(m.ler_swaps("h\n/dev/zram0 partition 4194300 0 100\n/swapfile file 8388604 0 -2\n"))
        self.assertFalse(s.tamanho_ssd.get_visible())    # o aero-swap-setup recusaria: nem pede senha
        self.assertIsNone(s.ssd_atual)
        s.ao_sumir()

    def test_explicar_sem_markup(self):
        s = m.SecaoMemoria(self.janela)
        vistos = []
        with mock.patch.object(Adw.AlertDialog, "present", lambda d, pai: vistos.append((d, pai))):
            s.explicar("Swap not changed", "<b>x</b> & y")
        (d, pai), = vistos
        self.assertIs(pai, self.janela)
        self.assertEqual((d.get_heading(), d.get_body()), ("Swap not changed", "<b>x</b> & y"))
        self.assertFalse(d.get_heading_use_markup() or d.get_body_use_markup())

    def test_switch_mostra_o_motivo_da_recusa(self):
        self.instalar()
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.resposta_root = 2
        self.erro_root = "refused: kill_on_critical must be 0 or 1\n"
        s.chaves["kill_on_critical"].set_active(False)
        self.assertTrue(girar(lambda: not s.ocupado))
        self.assertTrue(s.chaves["kill_on_critical"].get_active())         # voltou
        self.assertEqual(self.janela.avisos, ["Not saved: kill_on_critical must be 0 or 1"])
        s.ao_sumir()

    def test_config_lida_com_as_regras_dos_servicos(self):
        self.instalar()
        agora = time.time()
        with open(m.CONF_GUARDA, "w") as f:
            f.write("enabled=no\npause_background=on\nwarn_available=10\ncritical_available=10\n")
        with open(m.ESTADO, "w") as f:
            f.write('{"t": %f, "level": "normal", "config": {"enabled": 0}}' % agora)
        os.makedirs(os.path.dirname(m.CONF_AGENTE))
        with open(m.CONF_AGENTE, "w") as f:
            f.write("comfy_free_on_close=off\ncomfy_free_for_games=yes\ncomfy_idle_unload_min=015\n")
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()
        self.assertFalse(s.chaves["enabled"].get_active())                 # "no" = desligado, como o serviço
        self.assertTrue(s.status.get_subtitle().startswith("Off"))         # e bate com a linha de estado
        self.assertTrue(s.chaves["pause_background"].get_active())
        self.assertEqual((s.guarda["warn_available"], s.guarda["critical_available"]), ("15", "4"))  # par ruim
        self.assertFalse(s.ao_fechar.get_active())
        self.assertTrue(s.jogos.get_active())
        self.assertEqual(s.opcoes_ocioso, m.OCIOSO)
        self.assertEqual(m.escolhido(s.ocioso, s.opcoes_ocioso), "15")     # 015 = 15 (antes: "Never")
        with open(m.CONF_AGENTE, "w") as f:
            f.write("comfy_idle_unload_min=2000\n")                        # o agente recusa: usa 15
        s.ao_mostrar()
        self.assertEqual(s.opcoes_ocioso, m.OCIOSO)
        self.assertEqual(m.escolhido(s.ocioso, s.opcoes_ocioso), "15")
        s.ao_sumir()

    def test_subtitulo_de_fechar_o_app(self):
        s = m.SecaoMemoria(self.janela)
        s.ao_mostrar()                                    # sem o agente: nada de prometer notificação
        sub = s.chaves["kill_on_critical"].get_subtitle()
        self.assertIn("Last action", sub)
        self.instalar()
        s.ao_mostrar()
        self.assertIn("You get a notification", s.chaves["kill_on_critical"].get_subtitle())
        # o systemctl (falso) diz "inactive": o agente está parado, ninguém vai notificar
        self.assertTrue(girar(lambda: not s.checando and s.servico_agente is False))
        self.assertIn("Last action", s.chaves["kill_on_critical"].get_subtitle())
        s.ao_sumir()

    def test_relogio_para_com_a_janela_escondida(self):
        s = m.SecaoMemoria(self.janela)
        raiz = RaizFalsa()
        s.pagina.get_root = lambda: raiz              # a página "numa janela" e "na tela"
        s.pagina.get_mapped = lambda: True
        s.ao_mostrar()
        self.assertIsNotNone(s.relogio)
        ligacao = s.id_raiz
        s.ligar_relogio()
        self.assertEqual(s.id_raiz, ligacao)              # liga o aviso uma vez só
        self.assertEqual(s.tique(), GLib.SOURCE_CONTINUE)
        raiz.suspended = True                             # outro workspace / minimizada
        self.assertIsNone(s.relogio)
        s.ligar_relogio()                                 # um "map" com a janela escondida não religa
        self.assertIsNone(s.relogio)
        s.ram.set_subtitle("velho")
        raiz.suspended = False                            # voltou: atualiza na hora e religa
        self.assertIsNotNone(s.relogio)
        self.assertNotEqual(s.ram.get_subtitle(), "velho")
        # tique com a janela escondida (sem ter vindo o aviso) também para
        primeiro = s.relogio
        with mock.patch.object(RaizFalsa, "is_suspended", lambda _self: True):
            self.assertEqual(s.tique(), GLib.SOURCE_REMOVE)
        self.assertIsNone(s.relogio)
        GLib.source_remove(primeiro)                      # (o laço não rodou; tira a fonte à mão)
        # trocou de janela: solta a antiga
        nova = RaizFalsa()
        s.pagina.get_root = lambda: nova
        s.ligar_relogio()
        self.assertFalse(GObject.signal_handler_is_connected(raiz, ligacao))
        self.assertIs(s.raiz, nova)
        raiz.suspended = True                             # a antiga não mexe mais no relógio
        self.assertIsNotNone(s.relogio)
        s.ao_sumir()
        self.assertIsNone(s.relogio)
        # escondida com a página fora da tela: voltar não liga o relógio
        nova.suspended = True
        s.pagina.get_mapped = lambda: False
        nova.suspended = False
        self.assertIsNone(s.relogio)


if __name__ == "__main__":
    unittest.main()
