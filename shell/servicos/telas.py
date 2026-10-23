"""
telas.py — SERVICE: monitores e taxa de atualização (Hz).

API
  monitores() -> list            monitores ligados (do "monitors all", sem os desligados)
  taxas(m) -> [float]            taxas disponíveis na resolução em uso
  alvo(m, modo) -> float         "max" = a maior; "60" = a mais perto de 60 Hz
  definir_taxa(m, hz)            troca só a taxa, mantendo resolução, posição, escala, espelho e rotação
"""
import os

from . import hypr


def monitores():
    return [m for m in hypr.json_de("monitors all", []) if not m.get("disabled")]


def taxas(m):
    res = f"{m['width']}x{m['height']}@"
    return sorted({float(s[len(res):].removesuffix("Hz")) for s in m.get("availableModes", []) if s.startswith(res)})


def teto_do_dono(nome):
    """A taxa que o DONO escolheu para esse monitor no monitores.conf (Settings → Display). É o TETO: o
    monitor anuncia modos que ele não aguenta direito (o Dell anuncia 180 Hz, mas o dono usa 144), e
    passar dele pode deixar a tela sem imagem. None = monitor sem regra própria."""
    try:
        with open(os.path.expanduser("~/.config/hypr/monitores.conf")) as f:
            for l in f:
                l = l.strip()
                if l.startswith(f"monitor={nome},") and "@" in l:
                    return float(l.split(",")[1].split("@")[1].removesuffix("Hz"))
    except (OSError, ValueError, IndexError):
        pass
    return None


def alvo(m, modo):
    t = taxas(m) or [m["refreshRate"]]
    teto = teto_do_dono(m["name"])
    if teto:
        t = [hz for hz in t if hz <= teto + 0.5] or [min(t, key=lambda hz: abs(hz - teto))]
    return max(t) if modo == "max" else min(t, key=lambda hz: abs(hz - 60))


def definir_taxa(m, hz):
    regra = f"{m['name']},{m['width']}x{m['height']}@{hz:.2f},{m['x']}x{m['y']},{m['scale']:g}"
    if m.get("mirrorOf") not in (None, "", "none"):
        regra += f",mirror,{m['mirrorOf']}"
    if m.get("transform"):
        regra += f",transform,{m['transform']}"
    lembrar_taxa(m["name"], regra)
    return hypr.pedir(f"keyword monitor {regra}")


TAXA_CONF = os.path.expanduser("~/.config/hypr/taxa-atual.conf")


def lembrar_taxa(nome, regra):
    """Grava a regra do monitor num arquivo que o hyprland.conf lê logo DEPOIS do monitores.conf.
    Sem isso, todo reload do Hyprland (e ele se recarrega sozinho quando QUALQUER .conf muda, como na troca
    de tema) voltava o monitor à taxa do monitores.conf e o taxa.py subia de novo: duas trocas de modo,
    a tela apagava duas vezes. Com a regra lembrada, o reload pede o mesmo modo e nada pisca."""
    linhas = []
    try:
        with open(TAXA_CONF) as f:
            linhas = [l for l in f.read().splitlines() if l.startswith("monitor=") and not l.startswith(f"monitor={nome},")]
    except OSError:
        pass
    linhas.append(f"monitor={regra}")
    novo = "# Gerado pelo taxa.py (taxa de atualização em uso). Não editar.\n" + "\n".join(linhas) + "\n"
    try:
        with open(TAXA_CONF) as f:
            if f.read() == novo:
                return
    except OSError:
        pass
    tmp = TAXA_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write(novo)
    os.replace(tmp, TAXA_CONF)


if __name__ == "__main__":
    for m in monitores():
        print(m["name"], f"{m['refreshRate']:.2f} Hz", "->", taxas(m))
