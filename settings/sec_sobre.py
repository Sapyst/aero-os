"""
sec_sobre.py — seção About (o aparelho, discos, placa de vídeo, pastas).

Quase tudo vem de arquivos que o kernel expõe:
  /sys/class/dmi/id/...   fabricante e modelo do aparelho
  /proc/cpuinfo           processador
  /proc/meminfo           memória
  /etc/os-release         nome do sistema
"""
import json
import os
import platform
import shutil

from gi.repository import Adw, GLib, Gtk

from comum import Secao, linha_app, pagina, rodar

NVIDIA_PCI = "/sys/bus/pci/devices/0000:64:00.0"


def ler_arquivo(caminho, padrao=""):
    try:
        with open(caminho) as f:
            return f.read().strip()
    except OSError:
        return padrao


def placas_de_video():
    """Placas de vídeo pelo lspci (o nome comercial vem no último [...])."""
    import re as _re
    nomes = []
    for linha in (rodar(["lspci"]).stdout or "").splitlines():
        if " VGA " in linha or " 3D " in linha or " Display " in linha:
            nome = linha.split(": ", 1)[-1]
            c = _re.findall(r"\[([^\]]+)\]", nome)
            nomes.append(c[-1] if c else nome)
    return " + ".join(nomes) or "Unknown"


def processador():
    for linha in ler_arquivo("/proc/cpuinfo").splitlines():
        if linha.startswith("model name"):
            return linha.split(":", 1)[1].strip()
    return "?"


def memoria():
    for linha in ler_arquivo("/proc/meminfo").splitlines():
        if linha.startswith("MemTotal"):
            return f"{int(linha.split()[1]) / 1024 / 1024:.0f} GB"   # vem em kB
    return "?"


def sistema():
    for linha in ler_arquivo("/etc/os-release").splitlines():
        if linha.startswith("PRETTY_NAME="):
            return linha.split("=", 1)[1].strip('"')
    return "Linux"


def status_nvidia():
    """Lê se a NVIDIA está dormindo direto do kernel (ler isso NÃO acorda a placa).
    Só chama o nvidia-smi quando ela já está ligada."""
    estado = ler_arquivo(f"{NVIDIA_PCI}/power/runtime_status", None)
    if estado is None:
        return "Not found"
    if estado == "suspended":
        return "Asleep (powered off, ~0 W)"
    r = rodar(["nvidia-smi", "--query-gpu=power.draw,pstate,utilization.gpu",
               "--format=csv,noheader,nounits"])
    if r.returncode != 0:
        return "On"
    w, p, u = [x.strip() for x in r.stdout.split(",")]
    return f"On: {float(w):.1f} W · state {p} · load {u}%"


def tamanho(bytes_):
    return f"{bytes_ / 1000**3:.0f} GB"


class SecaoSobre(Secao):
    id = "sobre"
    titulo = "About"
    icone = "computer-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()

        aparelho = Adw.PreferencesGroup(title="Device")
        marca = ler_arquivo("/sys/class/dmi/id/sys_vendor")
        produto = ler_arquivo("/sys/class/dmi/id/product_name")
        modelo = produto if marca.lower() in produto.lower() else f"{marca} {produto}"
        hypr = rodar(["hyprctl", "version", "-j"])
        versao_hypr = json.loads(hypr.stdout).get("tag", "") if hypr.returncode == 0 else ""
        for titulo, valor in (("Model", modelo), ("Processor", processador()), ("Memory", memoria()),
                              ("Graphics", placas_de_video()),
                              ("System", sistema()), ("Kernel", platform.release()),
                              ("Desktop", f"Hyprland {versao_hypr}")):
            linha = Adw.ActionRow(use_markup=False, title=titulo, subtitle=valor, subtitle_selectable=True)
            linha.add_css_class("property")   # estilo "rótulo pequeno em cima, valor embaixo"
            aparelho.add(linha)

        gpu = Adw.PreferencesGroup(title="Dedicated graphics card")
        self.linha_nvidia = Adw.ActionRow(use_markup=False, title="NVIDIA RTX 5070", subtitle=status_nvidia())
        gpu.add(self.linha_nvidia)
        GLib.timeout_add_seconds(3, self.atualizar_nvidia)   # repete a cada 3 s

        discos = Adw.PreferencesGroup(title="Storage")
        for titulo, ponto in (("System and home", "/"), ("Data disk", os.environ.get("AERO_DADOS", "/mnt/data"))):
            if not os.path.ismount(ponto):
                continue
            uso = shutil.disk_usage(ponto)
            linha = Adw.ActionRow(use_markup=False, title=titulo,
                                  subtitle=f"{tamanho(uso.free)} free of {tamanho(uso.total)} · {ponto}")
            barra = Gtk.LevelBar(min_value=0, max_value=1, value=uso.used / uso.total,
                                 valign=Gtk.Align.CENTER)
            barra.set_size_request(160, -1)
            linha.add_suffix(barra)
            discos.add(linha)

        pastas = Adw.PreferencesGroup(title="Folders")
        imagens = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_PICTURES) \
            or os.path.expanduser("~/Pictures")
        for titulo, sub, caminho, icone in (
                ("Hyprland config files", "~/.config/hypr", "~/.config/hypr", "folder-symbolic"),
                ("Prints", "Print key: drag an area; it goes to Ctrl+V and to this folder",
                 os.path.join(imagens, "Prints"), "camera-photo-symbolic"),):
            pastas.add(linha_app(titulo, sub, ["thunar", os.path.expanduser(caminho)], icone))

        for g in (aparelho, gpu, discos, pastas):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def atualizar_nvidia(self):
        self.linha_nvidia.set_subtitle(status_nvidia())
        return True  # True = continuar repetindo
