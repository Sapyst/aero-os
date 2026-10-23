"""
sec_servicos.py — seção Background services (System): qual versão de cada serviço de fundo roda.

Os lançadores (~/.local/bin/clipboard --daemon, ~/.local/bin/bandeja) rodam pelo aero-supervise:
  versão nativa (Rust) por padrão; se ela cair 3x em 60 s, o supervisor troca sozinho pra clássica (Python).
Esta página só grava ~/.config/aero/daemons.conf (NOME=rust|python) e manda o supervisor reler
(aero-supervise --reload NOME). O estado de agora vem de $XDG_RUNTIME_DIR/aero-supervise-NOME.state
("rust chosen", "python fallback"...), escrito pelo supervisor.
"""
import os
import subprocess

from gi.repository import Adw, GLib

from comum import Secao, combo, pagina

CONF = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "aero/daemons.conf")
RUN = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
BIN = os.path.expanduser("~/.local/bin")
# (chave no daemons.conf, título, lançador que sobe o supervisor se ele não estiver rodando)
SERVICOS = [("clipboard", "Clipboard history", "clipboard --daemon"), ("tray", "System tray", "bandeja")]
OPCOES = [("Native (Rust)", "rust"), ("Classic (Python)", "python")]


def ler_conf():
    valores = {}
    try:
        for linha in open(CONF):
            chave, _, valor = linha.partition("=")
            if _ and not chave.strip().startswith("#"):
                valores[chave.strip()] = valor.strip()
    except OSError:
        pass
    return valores


def gravar_conf(chave, valor):
    linhas, achou = [], False
    try:
        linhas = open(CONF).read().splitlines()
    except OSError:
        linhas = ["# Background services (Settings → System → Background services)"]
    for i, linha in enumerate(linhas):
        if linha.partition("=")[0].strip() == chave:
            linhas[i], achou = f"{chave}={valor}", True
    if not achou:
        linhas.append(f"{chave}={valor}")
    os.makedirs(os.path.dirname(CONF), exist_ok=True)
    with open(CONF + ".tmp", "w") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(CONF + ".tmp", CONF)


def estado(chave):
    try:
        modo, como = (open(os.path.join(RUN, f"aero-supervise-{chave}.state")).read().split() + ["", ""])[:2]
    except OSError:
        return "Not running"
    nome = "native" if modo == "rust" else "classic"
    return f"Running: {nome}" + (" (fell back after crashes)" if como == "fallback" else "")


class SecaoServicos(Secao):
    id = "servicos"
    titulo = "Background services"
    icone = "system-run-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        conteudo = Adw.PreferencesPage()
        grupo = Adw.PreferencesGroup(
            title="Background services",
            description="The native versions use less memory. If one keeps crashing, the classic version "
                        "takes over by itself until the next login.")
        self.linhas = {}
        for chave, titulo, _ in SERVICOS:
            linha = combo(titulo, OPCOES, 0)
            linha.connect("notify::selected", self.mudou, chave)
            grupo.add(linha)
            self.linhas[chave] = linha
        conteudo.add(grupo)
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.carregando = True
        conf = ler_conf()
        for chave, linha in self.linhas.items():
            linha.set_selected(1 if conf.get(chave) == "python" else 0)
            linha.set_subtitle(estado(chave))
        self.carregando = False

    def mudou(self, linha, _p, chave):
        if self.carregando:
            return
        gravar_conf(chave, OPCOES[linha.get_selected()][1])
        lancador = dict((c, l) for c, _t, l in SERVICOS)[chave]
        try:
            pid = int(open(os.path.join(RUN, f"aero-supervise-{chave}.pid")).read().strip())
            os.kill(pid, 0)
            subprocess.run([os.path.join(BIN, "aero-supervise"), "--reload", chave], timeout=3)
        except (OSError, ValueError, subprocess.SubprocessError):
            subprocess.run(["hyprctl", "dispatch", "exec", os.path.join(BIN, lancador)], capture_output=True)
        linha.set_subtitle("Switching…")
        GLib.timeout_add(1500, lambda: (linha.set_subtitle(estado(chave)), False)[1])
