"""
sec_tela.py — seção Display (monitores).

Grava:
  ~/.config/hypr/painel.conf     MODO e ESCALA_LAPTOP (lido pelo ~/.local/bin/hypr-lid)
  ~/.config/hypr/monitores.conf  linhas "monitor=" (incluído no hyprland.conf com "source =")
"""
import json
import os

from gi.repository import Adw

from comum import Secao, atualizar_painel, combo, grupo_botao_aplicar, indice, pagina, rodar, valor_de

HYPR = os.path.expanduser("~/.config/hypr")
PAINEL = os.path.join(HYPR, "painel.conf")
MONITORES = os.path.join(HYPR, "monitores.conf")
LAPTOP = "eDP-1"

MODOS_LAPTOP = [
    ("Mirror the monitor", "espelhar"),
    ("Monitor only (laptop screen off)", "so-externo"),
    ("Extend (two screens)", "estender"),
]
ESCALAS = [("100%", "1"), ("125%", "1.25"), ("160%", "1.6"), ("200%", "2")]


def ler_painel():
    cfg = {"MODO": "espelhar", "ESCALA_LAPTOP": "1.6"}
    try:
        with open(PAINEL) as f:
            for linha in f:
                if "=" in linha and not linha.startswith("#"):
                    k, v = linha.strip().split("=", 1)
                    cfg[k] = v
    except FileNotFoundError:
        pass
    return cfg


def ler_regras_monitores():
    """{nome: (modo, escala)} das linhas monitor= já gravadas."""
    regras = {}
    try:
        with open(MONITORES) as f:
            for linha in f:
                if linha.startswith("monitor="):
                    partes = linha.strip()[len("monitor="):].split(",")
                    if partes[0] and partes[0] != LAPTOP and len(partes) >= 4:
                        regras[partes[0]] = (partes[1], partes[3])
    except FileNotFoundError:
        pass
    return regras


def monitores_conectados():
    """Lista do `hyprctl monitors all -j` (inclui desligados e espelhados)."""
    r = rodar(["hyprctl", "monitors", "all", "-j"])
    return json.loads(r.stdout) if r.returncode == 0 else []


def modos_do_monitor(m):
    """availableModes vem como '2560x1440@144.00Hz'. Devolve [(texto, '2560x1440@144.00')]."""
    vistos, modos = set(), []
    for s in m.get("availableModes", []):
        res, hz = s.removesuffix("Hz").split("@")
        valor = f"{res}@{hz}"
        if valor not in vistos:
            vistos.add(valor)
            modos.append((f"{res.replace('x', '×')}  ·  {round(float(hz))} Hz", valor))
    return modos


def modo_atual(m, modos):
    """Índice do modo em uso agora (mesma resolução e taxa mais próxima)."""
    melhor, menor = 0, None
    for i, (_, v) in enumerate(modos):
        res, hz = v.split("@")
        w, h = map(int, res.split("x"))
        dif = abs(float(hz) - m["refreshRate"]) + (0 if (w, h) == (m["width"], m["height"]) else 10000)
        if menor is None or dif < menor:
            melhor, menor = i, dif
    return melhor


class SecaoTela(Secao):
    id = "tela"
    titulo = "Display"
    icone = "video-display-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.conteudo = Adw.PreferencesPage()
        self.pagina = pagina(self.titulo, self.conteudo)
        self.grupos = []
        self.montar()

    def ao_mostrar(self):
        self.montar()  # monitores podem ter sido plugados/desplugados

    def montar(self):
        for g in self.grupos:
            self.conteudo.remove(g)
        self.grupos = []
        cfg = ler_painel()

        laptop = Adw.PreferencesGroup(
            title="Laptop screen",
            description="With the lid closed and a monitor connected, the laptop screen always turns off.")
        self.linha_modo = combo("With the lid open", MODOS_LAPTOP, indice(MODOS_LAPTOP, cfg["MODO"]))
        self.linha_escala_laptop = combo("Scale", ESCALAS, indice(ESCALAS, cfg["ESCALA_LAPTOP"], 2))
        laptop.add(self.linha_modo)
        laptop.add(self.linha_escala_laptop)

        externos = Adw.PreferencesGroup(title="External monitors")
        regras = ler_regras_monitores()
        self.linhas_externos = []  # (nome, linha_modo, modos, linha_escala)
        for m in monitores_conectados():
            if m["name"] == LAPTOP:
                continue
            modos = modos_do_monitor(m)
            escala = regras.get(m["name"], (None, str(m["scale"]).rstrip("0").rstrip(".")))[1]
            subtitulo = f"Connected to {m['name']}"
            if m["name"].startswith("HDMI"):
                subtitulo += " · via NVIDIA (144 Hz ≈ 13 W, 60 Hz ≈ 4 W)"
            expansor = Adw.ExpanderRow(use_markup=False, title=f"{m['make']} {m['model']}".strip(),
                                       subtitle=subtitulo, expanded=True)
            linha_modo = combo("Resolution and refresh rate", modos, modo_atual(m, modos))
            linha_escala = combo("Scale", ESCALAS, indice(ESCALAS, escala))
            expansor.add_row(linha_modo)
            expansor.add_row(linha_escala)
            externos.add(expansor)
            self.linhas_externos.append((m["name"], linha_modo, modos, linha_escala))
        if not self.linhas_externos:
            externos.add(Adw.ActionRow(use_markup=False, title="No external monitor connected"))

        for g in (laptop, externos, grupo_botao_aplicar(self.aplicar)):
            self.conteudo.add(g)
            self.grupos.append(g)

    def aplicar(self):
        modo = valor_de(self.linha_modo, MODOS_LAPTOP)
        escala_laptop = valor_de(self.linha_escala_laptop, ESCALAS)

        atualizar_painel(MODO=modo, ESCALA_LAPTOP=escala_laptop)   # só as chaves desta seção

        linhas, x, conectados = [], 0, set()
        for nome, linha_modo, modos, linha_escala in self.linhas_externos:
            conectados.add(nome)
            valor = valor_de(linha_modo, modos)
            escala = valor_de(linha_escala, ESCALAS)
            linhas.append(f"monitor={nome},{valor},{x}x0,{escala}")
            x += round(int(valor.split("x")[0]) / float(escala))  # próximo monitor fica à direita
        # Mantém regras de monitores já configurados que não estão ligados agora
        for nome, (valor, escala) in ler_regras_monitores().items():
            if nome not in conectados:
                linhas.append(f"monitor={nome},{valor},auto,{escala}")

        # Regra base do laptop (o hypr-lid ajusta depois conforme tampa e modo)
        primeiro = self.linhas_externos[0][0] if self.linhas_externos else None
        if modo == "espelhar" and primeiro:
            laptop = f"monitor={LAPTOP},preferred,auto,{escala_laptop},mirror,{primeiro}"
        else:
            laptop = f"monitor={LAPTOP},preferred,auto,{escala_laptop}"

        with open(MONITORES, "w") as f:
            f.write("# Gerado pelo app Settings (Super+I). O app reescreve este arquivo ao aplicar.\n")
            f.write("\n".join(linhas + [laptop, "monitor=,preferred,auto,1"]) + "\n")

        rodar(["hyprctl", "reload"])  # o "exec = hypr-lid" do hyprland.conf roda junto
        self.janela.avisar("Display settings applied")
