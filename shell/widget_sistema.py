"""widget_sistema.py — System: o "fetch" do dashboard. À esquerda, o Pokémon do dia (sprite do pokefetch,
sorteado pela data: o mesmo o dia inteiro); à direita, um sub-cartão com a ficha do PC em chaves coloridas
("os • Arch Aero OS"...). Coleta UMA vez ao abrir (pacman e flatpak rodam só aí); uptime e memória a cada 60 s."""
import datetime
import json
import os
import re
import subprocess

from gi.repository import Gtk, Pango

from widgets import LARGURA_ESTREITA, PixelArt, Quadro, Widget, hypr, hypr_json, registrar

CATALOGO = os.path.expanduser("~/.local/share/pokefetch/catalogo.json")


def ler(caminho, padrao=""):
    try:
        with open(caminho) as f:
            return f.read().strip()
    except OSError:
        return padrao


def tempo_ligado():
    s = float(ler("/proc/uptime", "0 0").split()[0])
    d, h, m = int(s // 86400), int(s % 86400 // 3600), int(s % 3600 // 60)
    return f"{d}d {h}h" if d else f"{h}h {m:02d}m" if h else f"{m}m"


def memoria():
    info = {}
    for linha in ler("/proc/meminfo").splitlines():
        k, _, v = linha.partition(":")
        info[k] = int(v.split()[0]) * 1024 if v.strip() else 0
    total, usado = info.get("MemTotal", 0), info.get("MemTotal", 0) - info.get("MemAvailable", 0)
    return f"{usado / 2**30:.1f} / {total / 2**30:.0f} GB"


def cpu():
    nome = next((l.split(":", 1)[1].strip() for l in ler("/proc/cpuinfo").splitlines() if l.startswith("model name")), "?")
    nome = re.sub(r"\s*(w/|with).*$", "", nome).replace("(R)", "").replace("(TM)", "").replace("AMD ", "").strip()
    return nome


def gpus():
    try:
        saida = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    lista = []
    for l in saida.splitlines():
        if not re.search(r"VGA|3D|Display", l):
            continue
        grupos = re.findall(r"\[([^\]]+)\]", l.split(": ", 1)[-1])
        nome = grupos[-1] if grupos else l.split(": ", 1)[-1]
        nome = re.sub(r"\s*\(rev.*\)|\s*/ Mobile| Max-Q", "", nome).strip().replace("GeForce ", "")
        lista.append(nome)
    return lista


def pacotes():
    partes = []
    try:
        partes.append(str(len(subprocess.run(["pacman", "-Qq"], capture_output=True, text=True, timeout=10).stdout.splitlines())))
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        n = len(subprocess.run(["flatpak", "list", "--app", "--columns=application"], capture_output=True, text=True, timeout=10).stdout.splitlines())
        if n:
            partes.append(f"{n} flatpak")
    except (OSError, subprocess.SubprocessError):
        pass
    return " + ".join(partes) or "?"


def wm():
    v = hypr("version").splitlines()
    m = re.search(r"Hyprland\s+(\S+)", v[0]) if v else None
    return f"hyprland {m.group(1)}" if m else "hyprland"


def tela():
    ms = hypr_json("monitors", [])
    m = next((m for m in ms if m.get("focused")), ms[0] if ms else None)
    return f"{m['width']}×{m['height']} {round(m.get('refreshRate', 0))}Hz" if m else "?"


def pokemon_do_dia():
    """(id, nome) sorteado pela data: muda uma vez por dia, igual pra todo mundo que olhar."""
    try:
        with open(CATALOGO) as f:
            pokes = json.load(f)["pokemon"]
    except (OSError, ValueError, KeyError):
        return None, None
    ids = sorted(pokes, key=int)
    import random
    pid = random.Random(datetime.date.today().toordinal()).choice(ids)
    return int(pid), pokes[pid]["nome"]


@registrar
class Sistema(Widget):
    tipo = "sistema"
    nome = "System"
    icone = "computer-symbolic"
    opcoes_padrao = {"pokemon": True, "pacotes": True, "privado": False}
    tamanho = (LARGURA_ESTREITA, 222)

    def montar(self):
        e = self.e
        self.cartao.set_spacing(4 * e)
        usuario = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
        host = ler("/etc/hostname") or os.uname().nodename
        if self.opcoes.get("privado"):          # prints e lives: nada do usuário nem do nome do PC
            usuario, host = "user", "aero"
        # linha de prompt, como no terminal: usuario@host ~ ❯ fetch
        prompt = Gtk.Box(spacing=0)
        for s_, cls in ((usuario, "prompt verde"), ("@", "prompt apagado"), (host, "prompt azul"), ("  ~ ", "prompt apagado"),
                        ("❯ ", "prompt rosa"), ("fetch --pokemon", "prompt")):
            prompt.append(self.rotulo(s_, cls))
        self.cartao.append(prompt)
        corpo = Gtk.Box(spacing=10 * e)
        if self.opcoes["pokemon"]:
            pid, nome = pokemon_do_dia()
            coluna = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4 * e, valign=Gtk.Align.CENTER)
            sprite = PixelArt(int(96 * e))
            if pid:
                sprite.carregar(pid)
            coluna.append(sprite)
            coluna.append(self.rotulo((nome or "?").lower(), "mono pequeno", halign=Gtk.Align.CENTER))
            coluna.append(self.rotulo(f"#{pid:04d}" if pid else "", "mono apagado pequeno", halign=Gtk.Align.CENTER))
            coluna.set_size_request(int(100 * e), -1)
            corpo.append(coluna)
        quadro = Quadro("system", e, espaco=0)
        quadro.set_hexpand(True)
        quadro.set_valign(Gtk.Align.CENTER)
        osr = next((l.split("=", 1)[1].strip('"') for l in ler("/etc/os-release").splitlines()
                    if l.startswith("PRETTY_NAME=")), "Linux")
        linhas = [("os", osr, "rosa"), ("kernel", os.uname().release, "azul"), ("up", tempo_ligado(), "pessego")]
        if self.opcoes["pacotes"]:
            linhas.append(("pkgs", pacotes(), "verde"))
        linhas += [("wm", wm(), "lilas"), ("shell", os.path.basename(os.environ.get("SHELL", "sh")), "amarelo"),
                   ("res", tela(), "rosa"), ("cpu", cpu(), "azul")]
        for i, g in enumerate(gpus()):
            linhas.append(("gpu" if i == 0 else "", g, "pessego"))
        linhas.append(("mem", memoria(), "verde"))
        grade = Gtk.Grid(column_spacing=8 * e, row_spacing=1 * e)
        self.valores = {}
        for i, (chave, valor, cor) in enumerate(linhas):
            k = self.rotulo(chave, f"mono negrito pequeno {cor}", xalign=0, ellipsize=Pango.EllipsizeMode.NONE)
            k.set_width_chars(6)
            ponto = self.rotulo("•", "mono apagado pequeno")
            v = self.rotulo(valor, "mono pequeno")
            grade.attach(k, 0, i, 1, 1)
            grade.attach(ponto, 1, i, 1, 1)
            grade.attach(v, 2, i, 1, 1)
            self.valores[chave] = v
        quadro.append(grade)
        corpo.append(quadro)
        self.cartao.append(corpo)
        rodape = Gtk.Box(spacing=0, valign=Gtk.Align.END, vexpand=True)
        rodape.append(self.rotulo("❯ ", "prompt rosa"))
        self.cursor = self.rotulo("_", "cursor")
        rodape.append(self.cursor)
        self.cartao.append(rodape)
        if not self.menos_movimento:
            self.a_cada(1, self.piscar)        # o cursor pisca 1x por segundo (só um rótulo muda)
        self.a_cada(60, self.atualizar)

    def piscar(self):
        self.cursor.set_opacity(0.0 if self.cursor.get_opacity() > 0.5 else 1.0)

    def atualizar(self):
        self.valores["up"].set_label(tempo_ligado())
        self.valores["mem"].set_label(memoria())
