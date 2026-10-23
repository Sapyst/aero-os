"""widget_sistema.py — System: o "fetch" do dashboard. À esquerda, a deusa grega do dia (uma estátua em
ASCII, sorteada pela data: a mesma o dia inteiro), com o nome e o que ela rege; à direita, um sub-cartão
com a ficha do PC em chaves coloridas
("os • Arch Aero OS"...). Coleta UMA vez ao abrir (pacman e flatpak rodam só aí); uptime e memória a cada 60 s."""
import datetime
import json
import os
import re
import subprocess

from gi.repository import GdkPixbuf, Gtk, Pango

from widgets import LARGURA_ESTREITA, SOLIDO, Quadro, Widget, hypr, hypr_json, registrar

# As deusas: estátuas de fotos de uso livre (domínio público, CC0 ou CC BY), em ASCII
# (tools/make-goddesses.py; créditos no README).
PASTA_DEUSAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deusas")
DEUSAS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deusas.json")


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


def deusa_do_dia():
    """A deusa sorteada pela data: muda uma vez por dia, igual pra todo mundo que olhar."""
    try:
        with open(DEUSAS, encoding="utf-8") as f:
            deusas = json.load(f)
    except (OSError, ValueError):
        return None
    import random
    return random.Random(datetime.date.today().toordinal()).choice(deusas) if deusas else None


@registrar
class Sistema(Widget):
    tipo = "sistema"
    nome = "System"
    icone = "computer-symbolic"
    opcoes_padrao = {"deusa": True, "pacotes": True, "privado": False}
    tamanho = (LARGURA_ESTREITA, 222)

    def montar(self):
        e = self.e
        self.cartao.set_spacing(4 * e)
        usuario = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
        host = ler("/etc/hostname") or os.uname().nodename
        if self.opcoes.get("privado"):          # prints e lives: nada do usuário nem do nome do PC
            usuario, host = "user", "aero"
        # linha de prompt, como no terminal: usuario@host ~ ❯ fetch. Solid: só "usuario @ host", em Inter
        prompt = Gtk.Box(spacing=0)
        partes = (((usuario, "titulo-s rosa"), ("@", "titulo-s dim"), (host, "titulo-s")) if SOLIDO else
                  ((usuario, "prompt verde"), ("@", "prompt apagado"), (host, "prompt azul"), ("  ~ ", "prompt apagado"),
                   ("❯ ", "prompt rosa"), ("fetch --goddess", "prompt")))
        for s_, cls in partes:
            prompt.append(self.rotulo(s_, cls))
        self.cartao.append(prompt)
        corpo = Gtk.Box(spacing=10 * e, vexpand=SOLIDO)
        deusa = deusa_do_dia() if self.opcoes.get("deusa", True) else None
        if deusa:
            coluna = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3 * e, valign=Gtk.Align.CENTER)
            alto = int(104 * e)
            try:
                img = GdkPixbuf.Pixbuf.new_from_file_at_scale(os.path.join(PASTA_DEUSAS, deusa["imagem"]), int(100 * e), alto, True)
                estatua = Gtk.Picture.new_for_pixbuf(img)
                estatua.set_size_request(img.get_width(), img.get_height())
                estatua.set_can_shrink(False)
                coluna.append(estatua)
            except Exception:  # noqa: BLE001 — sem a imagem, fica só o nome
                pass
            coluna.append(self.rotulo(deusa["nome"].lower(), "mono pequeno", halign=Gtk.Align.CENTER))
            coluna.append(self.rotulo(deusa["rege"], "mono apagado pequeno", halign=Gtk.Align.CENTER))
            c = deusa["credito"]
            coluna.set_tooltip_text(f"{deusa['nome']} ({deusa['romano']}), goddess of {deusa['rege']}\n"
                                    f"{deusa['obra']}, {deusa['cidade']}\nPhoto: {c['fotografo']}, {c['licenca']}")
            coluna.set_size_request(int(100 * e), -1)
            corpo.append(coluna)
        quadro = Quadro("system", e, espaco=0)
        quadro.set_hexpand(True)
        quadro.set_valign(Gtk.Align.FILL if SOLIDO else Gtk.Align.CENTER)
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
        grade = Gtk.Grid(column_spacing=8 * e, row_spacing=(2 if SOLIDO else 1) * e, valign=Gtk.Align.CENTER, vexpand=True)
        self.valores = {}
        for i, (chave, valor, cor) in enumerate(linhas):
            k = self.rotulo(chave, "chave-s pequeno" if SOLIDO else f"mono negrito pequeno {cor}", xalign=0,
                            ellipsize=Pango.EllipsizeMode.NONE)
            k.set_width_chars(6)
            v = self.rotulo(valor, "mono pequeno")
            grade.attach(k, 0, i, 1, 1)
            if not SOLIDO:                     # Solid: chave e valor bastam (sem o "•" de terminal)
                grade.attach(self.rotulo("•", "mono apagado pequeno"), 1, i, 1, 1)
            grade.attach(v, 2, i, 1, 1)
            self.valores[chave] = v
        quadro.append(grade)
        corpo.append(quadro)
        self.cartao.append(corpo)
        self.a_cada(60, self.atualizar)
        if SOLIDO:                             # Solid: sem o rodapé de terminal (e sem o cursor piscando 1x/s)
            return
        rodape = Gtk.Box(spacing=0, valign=Gtk.Align.END, vexpand=True)
        rodape.append(self.rotulo("❯ ", "prompt rosa"))
        self.cursor = self.rotulo("_", "cursor")
        rodape.append(self.cursor)
        self.cartao.append(rodape)
        if not self.menos_movimento:
            self.a_cada(1, self.piscar)        # o cursor pisca 1x por segundo (só um rótulo muda)

    def piscar(self):
        self.cursor.set_opacity(0.0 if self.cursor.get_opacity() > 0.5 else 1.0)

    def atualizar(self):
        self.valores["up"].set_label(tempo_ligado())
        self.valores["mem"].set_label(memoria())
