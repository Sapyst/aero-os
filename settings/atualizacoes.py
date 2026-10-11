"""
atualizacoes.py — descobre o que está instalado, de onde veio, a categoria, e o que tem atualização.
Também monta os "passos" (comandos) pra atualizar.

Fontes de programas no Aero:
  repo     repositórios oficiais do Arch (pacman). Checagem segura com `checkupdates`.
  aur      AUR, receitas da comunidade (ex: claude-desktop). Versão nova via API do site do AUR.
  flatpak  apps Flatpak (Flathub).
  npm      pacotes globais do Node (ex: Claude Code).

Por que pacotes do repositório não atualizam "um por um":
  No Arch, os pacotes oficiais são feitos pra funcionar JUNTOS na mesma versão. Atualizar só um
  (uma "atualização parcial") pode deixar ele pedindo uma biblioteca mais nova que a instalada,
  e o programa quebra. Por isso eles vão sempre juntos, com `pacman -Syu`.
  AUR, Flatpak e npm não têm esse problema, então esses dá pra atualizar individualmente.
"""
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from comum import como_root, rodar

FONTES = {"repo": "Official repos", "aur": "AUR", "flatpak": "Flatpak", "npm": "npm",
          "manual": "Installed manually"}

CATEGORIAS = [
    ("sistema", "System", "Kernel, drivers, firmware, core tools and libraries"),
    ("integrado", "Desktop", "What makes up your desktop: Hyprland, bar, menu, sound, network, Bluetooth"),
    ("extras", "Apps", "Programs you installed: Steam, Firefox, Claude…"),
]

# Pacotes (e dependências) que formam o seu ambiente gráfico
PREFIXOS_DESKTOP = (
    "hypr", "aquamarine", "xdg-desktop-portal", "pipewire", "wireplumber", "waybar", "wofi", "mako",
    "kitty", "thunar", "gvfs", "tumbler", "blueman", "bluez", "networkmanager", "network-manager-applet",
    "nm-connection-editor", "libnma", "pavucontrol", "nwg-", "grim", "slurp", "wl-clipboard",
    "noto-fonts", "ttf-", "gtk3", "gtk4", "libadwaita", "polkit", "xdg-user-dirs", "i3", "dmenu",
    "gst-plugin-pipewire", "libpulse", "qt5-wayland", "qt6-wayland", "python-gobject", "xorg-xwayland")

# Programas instalados de propósito que são "parte do sistema"
SISTEMA = {"base", "base-devel", "amd-ucode", "btrfs-progs", "efibootmgr", "mkinitcpio", "sudo",
           "sof-firmware", "zram-generator", "xf86-video-amdgpu", "wpa_supplicant", "pacman-contrib",
           "git", "nano", "vim", "wget", "nodejs", "npm", "flatpak"}

NOMES = {"claude-desktop": "Claude Desktop", "@anthropic-ai/claude-code": "Claude Code",
         "steam": "Steam", "firefox": "Firefox"}
NOTAS = {"steam": "The Steam client and your games update themselves inside Steam"}


@dataclass
class Programa:
    nome: str
    fonte: str
    categoria: str
    atual: str
    nova: str = ""
    descricao: str = ""
    explicito: bool = True          # False = dependência (biblioteca) puxada por outro programa
    extra: dict = field(default_factory=dict)

    @property
    def titulo(self):
        return NOMES.get(self.nome) or self.extra.get("titulo") or self.nome

    @property
    def individual(self):
        """Pode ser atualizado sozinho? (só fora dos repositórios oficiais)"""
        return self.fonte in ("aur", "flatpak", "npm")


def categoria_de(nome, explicito, fonte):
    if fonte != "repo":
        return "extras"
    if nome.startswith(PREFIXOS_DESKTOP):
        return "integrado"
    if not explicito or nome in SISTEMA or nome.startswith(("linux", "nvidia", "lib32-", "xorg-")):
        return "sistema"
    return "extras"


def pares(cmd):
    """Saída "nome versão" por linha -> {nome: versão}"""
    r = rodar(cmd)
    return dict(l.split(" ", 1) for l in r.stdout.splitlines() if " " in l)


def descricoes(nomes):
    """Lê a descrição de vários pacotes de uma vez com `pacman -Qi`."""
    if not nomes:
        return {}
    r = rodar(["pacman", "-Qi", *nomes])
    saida, atual = {}, None
    for linha in r.stdout.splitlines():
        if linha.startswith("Name "):
            atual = linha.split(":", 1)[1].strip()
        elif linha.startswith("Description ") and atual:
            saida[atual] = linha.split(":", 1)[1].strip()
    return saida


def mais_nova(instalada, outra):
    """vercmp é o comparador de versões oficial do pacman (entende 1.10 > 1.9, epochs etc.)."""
    r = rodar(["vercmp", instalada, outra])
    return r.returncode == 0 and r.stdout.strip() == "-1"


# ---------- checagem por fonte ----------

def checar_repo(estrangeiros):
    explicitos = pares(["pacman", "-Qe"])
    # checkupdates baixa a lista nova numa cópia temporária: NÃO mexe no seu sistema.
    # Código de saída: 0 = tem atualizações, 2 = nenhuma, 1 = erro (ex: sem internet)
    r = rodar(["checkupdates"])
    if r.returncode == 1:
        raise RuntimeError("Could not reach the Arch servers (no internet?)")
    novas = {}
    for linha in r.stdout.splitlines():
        partes = linha.split()                  # "nome 1.0-1 -> 1.1-1"
        if len(partes) >= 4:
            novas[partes[0]] = (partes[1], partes[3])

    nomes = [n for n in explicitos if n not in estrangeiros] + [n for n in novas if n not in explicitos]
    desc = descricoes(nomes)
    programas = []
    for nome in nomes:
        explicito = nome in explicitos
        atual, nova = novas.get(nome, (explicitos.get(nome, ""), ""))
        programas.append(Programa(nome, "repo", categoria_de(nome, explicito, "repo"), atual, nova,
                                  NOTAS.get(nome, desc.get(nome, "")), explicito))
    return programas


def checar_aur(estrangeiros):
    if not estrangeiros:
        return []
    # API pública do AUR: pergunta a versão de vários pacotes numa chamada só
    consulta = urllib.parse.urlencode([("arg[]", n) for n in estrangeiros])
    with urllib.request.urlopen(f"https://aur.archlinux.org/rpc/v5/info?{consulta}", timeout=15) as resp:
        no_aur = {p["Name"]: p for p in json.load(resp)["results"]}
    desc = descricoes(list(estrangeiros))
    programas = []
    for nome, atual in estrangeiros.items():
        p = no_aur.get(nome)
        if p is None:
            programas.append(Programa(nome, "manual", "extras", atual, "", desc.get(nome, "")))
            continue
        nova = p["Version"] if mais_nova(atual, p["Version"]) else ""
        programas.append(Programa(nome, "aur", "extras", atual, nova,
                                  p.get("Description") or desc.get(nome, ""),
                                  extra={"url": p["URLPath"]}))
    return programas


def checar_flatpak():
    if rodar(["which", "flatpak"]).returncode != 0:
        return []
    r = rodar(["flatpak", "list", "--app", "--columns=application,name,version"])
    instalados = [l.split("\t") for l in r.stdout.splitlines() if l.strip()]
    if not instalados:
        return []
    r = rodar(["flatpak", "remote-ls", "--updates", "--app", "--columns=application,version"])
    novas = dict(l.split("\t", 1) for l in r.stdout.splitlines() if "\t" in l)
    programas = []
    for colunas in instalados:
        app, nome, versao = (colunas + ["", ""])[:3]   # completa colunas que vierem vazias
        nova = (novas.get(app) or "new version") if app in novas else ""
        programas.append(Programa(app, "flatpak", "extras", versao or "?", nova,
                                  f"Flatpak app · {app}", extra={"titulo": nome or app}))
    return programas


def checar_npm():
    if rodar(["which", "npm"]).returncode != 0:
        return []
    instalados = json.loads(rodar(["npm", "ls", "-g", "--depth=0", "--json"]).stdout or "{}")
    deps = instalados.get("dependencies", {})
    # npm outdated termina com código 1 quando HÁ atualizações; não é erro
    desatualizados = json.loads(rodar(["npm", "outdated", "-g", "--json"]).stdout or "{}")
    return [Programa(nome, "npm", "extras", info.get("version", "?"),
                     desatualizados.get(nome, {}).get("latest", ""),
                     "Command-line package (Node.js)")
            for nome, info in deps.items() if nome != "npm"]


def verificar():
    """Checa todas as fontes. Devolve (programas, erros). Uma fonte falhar não impede as outras."""
    estrangeiros = pares(["pacman", "-Qm"])   # instalados que não vieram dos repositórios oficiais
    programas, erros = [], []
    for nome, funcao in (("Official repos", lambda: checar_repo(estrangeiros)),
                         ("AUR", lambda: checar_aur(estrangeiros)),
                         ("Flatpak", checar_flatpak), ("npm", checar_npm)):
        try:
            programas += funcao()
        except Exception as erro:  # noqa: BLE001
            erros.append(f"{nome}: {erro}")
    return programas, erros


# ---------- passos de atualização ----------
# Cada passo: (descrição, comando). Rodam em ordem; se um falhar, para.

# AUR = receitas de qualquer pessoa. Nunca compila às cegas: abre um terminal, mostra o PKGBUILD
# (a receita, que roda no seu PC) e só compila se você responder "y". Fica fora do "Update everything".
SCRIPT_AUR = r"""
set -euo pipefail
pasta=$(mktemp -d)                # pasta temporária só pra esta compilação
trap 'rm -rf "$pasta"' EXIT       # apaga ela no final, dando certo ou não
cd "$pasta"
echo ">> Downloading the AUR recipe (PKGBUILD) for $2"
curl -fsSL --proto =https "https://aur.archlinux.org$1" | tar xz
cd "$(ls -d */ | head -1)"
echo ">> Files in this recipe:"; ls -la
echo ">> Read the recipe (and its install script, which runs as root) before building (q closes)"; sleep 2
${PAGER:-less} PKGBUILD $(ls *.install 2>/dev/null)
read -r -p ">> Build and install $2? [y/N] " resposta
[[ $resposta == [yY]* ]] || { echo "Cancelled."; sleep 1; exit 1; }
makepkg -si --needed
read -r -p ">> Done. Press Enter to close. " _
"""


def passo_autenticar():
    # sudo -v = só "validar": pede a senha (se precisar; o sudo a lembra pelo tempo configurado), sem rodar nada
    return ("Authenticate", ["sudo", "-A", "-v"])


def passo_sistema():
    # -S sync, -y baixa a lista nova de pacotes, -u atualiza tudo. --noconfirm = sem perguntas
    return ("Upgrade system packages (pacman -Syu)", como_root(["pacman", "-Syu", "--noconfirm"]))


def passo_individual(p):
    if p.fonte == "aur":
        return (f"Review and build {p.titulo} from the AUR (in a terminal)",
                ["kitty", "--title", f"AUR: {p.nome}", "bash", "-c", SCRIPT_AUR, "aur", p.extra["url"], p.nome])
    if p.fonte == "flatpak":
        return (f"Update {p.titulo} (Flatpak)", ["flatpak", "update", "-y", "--noninteractive", p.nome])
    if p.fonte == "npm":
        return (f"Update {p.titulo} (npm)", ["npm", "install", "-g", f"{p.nome}@latest"])
    raise ValueError(p.fonte)


def passos_para(programas, so_sistema=False, unico=None):
    """Monta a lista de passos.
    so_sistema = botão "Upgrade system"; unico = botão "Update" de um programa; senão = tudo."""
    if unico:
        passos = [passo_individual(unico)]
    else:
        pendentes = [p for p in programas if p.nova]
        passos = []
        if any(p.fonte == "repo" for p in pendentes) or so_sistema:
            passos.append(passo_sistema())
        if not so_sistema:
            # AUR fica de fora: cada pacote do AUR é revisado e confirmado no botão dele
            passos += [passo_individual(p) for p in pendentes if p.individual and p.fonte != "aur"]
    if any(cmd[0] == "sudo" or cmd[0] == "bash" for _, cmd in passos):
        passos.insert(0, passo_autenticar())   # pede a senha logo no começo, uma vez só
    return passos
