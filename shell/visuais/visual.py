#!/usr/bin/env python3
"""
visual.py — os "visuais" do sistema (Settings → Theme → Overview → Visual). Comando: aero-visual.

  AeroTerm  o visual de sempre: terminal, OLED, ASCII, glitch
  AerOS     claro, vidro pastel, cantos macios, movimento fluido (no Settings aparece como "AeroCloud")
  DotStyle  pontos: preto OLED, tudo em matriz de pontos, um único acento vermelho

Cada visual é um PERFIL em ~/.config/aero-shell/visuais/<Nome>/: ao sair de um, tudo o que define a
aparência é salvo nele; ao entrar, é restaurado. Assim voltar ao AeroTerm devolve exatamente o que era,
e os ajustes feitos dentro do AerOS também ficam guardados.
Salvo por perfil: as chaves de tema do painel.conf, waybar (config + estilo), hypr/visual.conf,
o wallpaper do sistema, os wallpapers por área e o vidro/escala dos widgets.

  aero-visual                 mostra o atual          aero-visual AerOS | AeroTerm    troca
"""
import json
import os
import shutil
import subprocess
import sys
import time

H = os.path.expanduser("~")
AQUI = os.path.dirname(os.path.abspath(__file__))
PERFIS = os.path.join(H, ".config/aero-shell/visuais")
PAINEL = os.path.join(H, ".config/hypr/painel.conf")
VISUAL_CONF = os.path.join(H, ".config/hypr/visual.conf")
WAYBAR = os.path.join(H, ".config/waybar")
WIDGETS = os.path.join(H, ".config/aero-shell/widgets.json")
HYPRPAPER = os.path.join(H, ".config/hypr/hyprpaper.conf")
BIN = os.path.join(H, ".local/bin")
VISUAIS = ["AeroTerm", "AerOS", "DotStyle"]

CHAVES = """TEMA_MODO TEMA_CLARO TEMA_ESCURO COR_CLARA COR_ESCURA COR_ALFA COR_SINC ESMAECER COR_CLARA_MANUAL
COR_ESCURA_MANUAL WALL_CORES COR_DESTAQUE WALL_SATURACAO WALL_BRILHO WALL_CONTRASTE ICONES_ASCII ICONES_BASE
ICONS_TINT VIDRO BAR_ALPHA ANIM ANIM_VEL TRANS_EFFECT TRANS_SPEED TRANS_STRENGTH TRANS_TINT CANTOS DOCK_TAMANHO
DOCK_ONDA BOTOES_JANELA WALL_TARGET""".split()

# o AerOS na primeira vez (depois, o que você ajustar fica no perfil)
AEROS = {"TEMA_MODO": "claro", "COR_CLARA": "f4f2fb", "COR_ALFA": "0.62", "COR_SINC": "1", "WALL_CORES": "0",
         "COR_DESTAQUE": "7b7ff0", "ICONES_ASCII": "0", "ICONES_BASE": "Papirus-Light", "VIDRO": "media",
         "BAR_ALPHA": "0.55", "TRANS_EFFECT": "none", "ESMAECER": "0.06", "ANIM": "1", "ANIM_VEL": "normal"}
AEROS_WALL = os.path.join(H, "Pictures/Wallpapers/aeros-glass.png")

# o DotStyle na primeira vez: parte do AeroTerm (ícones, dock...) e troca só o que é dele
DOTSTYLE = {"TEMA_MODO": "escuro", "COR_ESCURA": "0b0b0c", "COR_ALFA": "0.86", "COR_SINC": "1", "WALL_CORES": "0",
            "COR_DESTAQUE": "ecebe8", "TRANS_EFFECT": "dots", "TRANS_TINT": "none", "ESMAECER": "0.0", "ANIM": "1",
            "ANIM_VEL": "normal"}
DOTSTYLE_WALL = os.path.join(H, "Pictures/Wallpapers/dotstyle-venus.png")


def waybar_dotstyle(config_base):
    """A barra do DotStyle a partir da de sempre: o limitador sai da barra (fica só a bateria), e a bateria
    mostra os números em azul quando está na tomada. O relógio continua no centro exato."""
    import re
    s = open(config_base).read()
    s = re.sub(r',\s*"custom/limite"', "", s)
    s = re.sub(r'"format": "\{icon\}  \{capacity\}%"', '"format": "{icon}  {capacity}"', s)
    s = re.sub(r'"format-charging": "[^"]*"',
               '"format-charging": "{icon}  <span color=\'#5aa9ff\'>{capacity}</span>",\n'
               '        "format-plugged": "{icon}  <span color=\'#5aa9ff\'>{capacity}</span>"', s, count=1)
    return s


# ---------- painel.conf ----------
def ler_painel():
    v = {}
    try:
        for l in open(PAINEL):
            if "=" in l and not l.startswith("#"):
                k, x = l.rstrip("\n").split("=", 1)
                v[k.strip()] = x.strip().strip('"')
    except OSError:
        pass
    return v


def gravar_painel(kv):
    linhas = open(PAINEL).read().splitlines() if os.path.exists(PAINEL) else []
    falta = {k: str(v).replace("\n", "").replace("\r", "") for k, v in kv.items()}
    for i, l in enumerate(linhas):
        k = l.split("=", 1)[0].strip()
        if k in falta and not l.startswith("#"):
            linhas[i] = f"{k}={falta.pop(k)}"
    linhas += [f"{k}={v}" for k, v in falta.items()]
    with open(PAINEL + ".tmp", "w") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(PAINEL + ".tmp", PAINEL)


def atual():
    return ler_painel().get("VISUAL", "AeroTerm")


def wallpaper_atual():
    try:
        for l in open(HYPRPAPER):
            if l.strip().startswith("path"):
                return os.path.expanduser(l.split("=", 1)[1].strip())
    except OSError:
        pass
    return None


# ---------- salvar / restaurar um perfil ----------
def salvar(nome):
    d = os.path.join(PERFIS, nome)
    os.makedirs(d, exist_ok=True)
    p = ler_painel()
    w = json.load(open(WIDGETS)) if os.path.exists(WIDGETS) else {}
    estado = {"painel": {k: p[k] for k in CHAVES if k in p}, "wallpaper": wallpaper_atual(),
              "widgets": {k: w.get(k) for k in ("vidro", "escala", "menos_movimento", "por_area") if k in w},
              "salvo_em": int(time.time())}
    json.dump(estado, open(os.path.join(d, "estado.json"), "w"), indent=2)
    for arq in ("config.jsonc", "style.css"):
        if os.path.exists(os.path.join(WAYBAR, arq)):
            shutil.copy2(os.path.join(WAYBAR, arq), os.path.join(d, "waybar-" + arq))
    shutil.copy2(VISUAL_CONF, os.path.join(d, "visual.conf")) if os.path.exists(VISUAL_CONF) else \
        open(os.path.join(d, "visual.conf"), "w").write("")


def waybar_aeros(config_base):
    """Layout do AerOS a partir da config de sempre (mantém os módulos e as funções de cada um):
    status à esquerda · pontinhos + nome do app no centro · hora/data/bateria à direita (bandeja e central no fim)."""
    s = open(config_base).read()
    trocas = {'"margin-top": 8': '"margin-top": 0',
              '"margin-left": 12': '"margin-left": 0', '"margin-right": 12': '"margin-right": 0'}
    for a, b in trocas.items():
        s = s.replace(a, b)
    import re
    s = re.sub(r'"modules-left": \[[^\]]*\]', '"modules-left": ["network", "bluetooth", "pulseaudio", "hyprland/language"]', s, count=1)
    s = re.sub(r'"modules-center": \[[^\]]*\]', '"modules-center": ["hyprland/workspaces", "hyprland/window"]', s, count=1)
    s = re.sub(r'"modules-right": \[[^\]]*\]', '"modules-right": ["custom/minimizados", "custom/notificacoes", '
               '"custom/settings", "clock", "battery", "custom/espaco-bandeja", "custom/central"]', s, count=1)
    s = s.replace('"format": "{:%a %d %b   %H:%M}"', '"format": "{:%H:%M   %a, %b %d}"')
    s = re.sub(r'"format-wifi": "[^"]*"', r'"format-wifi": "\\uf1eb"', s)
    extra = ('    "hyprland/window": { "format": "{initialTitle}", "max-length": 28, "separate-outputs": true,\n'
             '        "rewrite": { "": "Desktop" } },\n')
    s = s.replace('"hyprland/workspaces": {', extra + '    "hyprland/workspaces": {', 1)
    return s


def restaurar(nome):
    d = os.path.join(PERFIS, nome)
    novo = not os.path.exists(os.path.join(d, "estado.json"))
    base = os.path.join(PERFIS, "AeroTerm")
    if novo and nome == "DotStyle":
        pai = json.load(open(os.path.join(base, "estado.json"))) if os.path.exists(os.path.join(base, "estado.json")) \
            else {"painel": {}, "widgets": {}}
        if not os.path.exists(DOTSTYLE_WALL):
            os.makedirs(os.path.dirname(DOTSTYLE_WALL), exist_ok=True)
            subprocess.run(["python3", "-I", os.path.join(AQUI, "dotstyle.py"), "wallpaper", DOTSTYLE_WALL],
                           capture_output=True)
        estado = {"painel": dict(pai.get("painel", {}), **DOTSTYLE),
                  "wallpaper": DOTSTYLE_WALL if os.path.exists(DOTSTYLE_WALL) else None,
                  "widgets": dict(pai.get("widgets") or {}, por_area={})}
        open(VISUAL_CONF, "w").write(open(os.path.join(AQUI, "dotstyle-visual.conf")).read())
        open(os.path.join(WAYBAR, "config.jsonc"), "w").write(waybar_dotstyle(os.path.join(base, "waybar-config.jsonc")))
        shutil.copy2(os.path.join(AQUI, "dotstyle-style.css"), os.path.join(WAYBAR, "style.css"))
    elif novo and nome == "AerOS":
        estado = {"painel": dict(AEROS), "wallpaper": AEROS_WALL if os.path.exists(AEROS_WALL) else None,
                  "widgets": {"vidro": "0.45", "por_area": {}}}
        open(VISUAL_CONF, "w").write(open(os.path.join(AQUI, "aeros-visual.conf")).read())
        open(os.path.join(WAYBAR, "config.jsonc"), "w").write(waybar_aeros(os.path.join(base, "waybar-config.jsonc")))
        shutil.copy2(os.path.join(AQUI, "aeros-style.css"), os.path.join(WAYBAR, "style.css"))
    elif novo:
        raise SystemExit(f"no saved profile for {nome}")
    else:
        estado = json.load(open(os.path.join(d, "estado.json")))
        for arq in ("config.jsonc", "style.css"):
            p = os.path.join(d, "waybar-" + arq)
            if os.path.exists(p):
                shutil.copy2(p, os.path.join(WAYBAR, arq))
        shutil.copy2(os.path.join(d, "visual.conf"), VISUAL_CONF)
    return estado


def aplicar(estado, nome):
    antes = ler_painel()
    gravar_painel(dict(estado["painel"], VISUAL=nome))
    if os.path.exists(WIDGETS) and estado.get("widgets"):
        w = json.load(open(WIDGETS))
        for k, v in estado["widgets"].items():
            w[k] = v
        json.dump(w, open(WIDGETS + ".tmp", "w"), indent=2)
        os.replace(WIDGETS + ".tmp", WIDGETS)
    rodar = lambda *c: subprocess.run(list(c), capture_output=True)  # noqa: E731
    if estado.get("wallpaper") and os.path.exists(estado["wallpaper"]):
        rodar(os.path.join(BIN, "papel-de-parede"), "--all", estado["wallpaper"])
        if estado.get("widgets", {}).get("por_area"):         # --all limpou os por área: devolve os do perfil
            w = json.load(open(WIDGETS)); w["por_area"] = estado["widgets"]["por_area"]
            json.dump(w, open(WIDGETS, "w"), indent=2)
    rodar(os.path.join(BIN, "hypr-tema"), "--forcar")
    p = estado["painel"]
    if p.get("ICONES_BASE") and p.get("ICONES_BASE") != antes.get("ICONES_BASE"):
        rodar(os.path.join(BIN, "aero-icones"), "--base", p["ICONES_BASE"])
    if p.get("ICONES_ASCII") != antes.get("ICONES_ASCII"):
        rodar(os.path.join(BIN, "aero-icones"), "--ligar" if p.get("ICONES_ASCII") == "1" else "--desligar")
    rodar("hyprctl", "reload")
    recarregar_shell()


def processo(nome):
    """PID do programa do shell (python3 .../aero-shell/<nome>.py), comparando o caminho exato."""
    alvo = os.path.join(H, ".local/share/aero-shell", nome + ".py")
    for pid in os.listdir("/proc"):
        if pid.isdigit():
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if len(args) > 1 and args[0].endswith(b"python3") and args[1].decode(errors="replace") == alvo:
                return int(pid)
    return None


def recarregar_shell():
    """Soft reload depois de trocar o visual: a barra primeiro (os outros se alinham a ela), depois
    bandeja, Dock, Super Menu, central, Alt+Tab e Command Bar — cada um relê cores e posições.
    Os widgets relêem sozinhos (vigiam o widgets.json); aqui só recebem o --recarregar."""
    import signal
    rodar = lambda *c: subprocess.run(list(c), capture_output=True)  # noqa: E731
    # a barra do visual: AeroBar no DotStyle, Waybar nos outros (aero-barra decide e fecha a outra)
    pid_aerobar = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "aerobar.pid")
    if os.path.exists(pid_aerobar):
        try:
            os.kill(int(open(pid_aerobar).read().strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    rodar("pkill", "-x", "waybar")
    time.sleep(0.4)
    rodar("hyprctl", "dispatch", "exec", os.path.join(BIN, "aero-barra"))
    for _ in range(30):                                   # espera a barra existir (a bandeja se mede por ela)
        camadas = subprocess.run(["hyprctl", "layers"], capture_output=True, text=True).stdout
        if "namespace: waybar" in camadas or "namespace: aerobar" in camadas:
            break
        time.sleep(0.1)
    reabrir = {"bandeja": "~/.local/bin/bandeja", "supermenu": "~/.local/bin/supermenu --daemon",
               "som": "~/.local/bin/som --daemon", "alttab": "~/.local/bin/alttab --daemon",
               "comando": "~/.local/bin/comando --daemon", "dock": None}       # o Dock tem vigia que reabre sozinho
    for nome, cmd in reabrir.items():
        pid = processo(nome)
        if pid:
            os.kill(pid, signal.SIGTERM)
    time.sleep(0.6)
    for nome, cmd in reabrir.items():
        if cmd and not processo(nome):
            rodar("hyprctl", "dispatch", "exec", cmd)
    rodar(os.path.join(BIN, "widgets"), "--recarregar")


def trocar(nome):
    if nome not in VISUAIS:
        raise SystemExit(f"unknown visual: {nome} (use {' or '.join(VISUAIS)})")
    de = atual()
    if de == nome:
        return print(f"already {nome}")
    if not os.path.exists(VISUAL_CONF):
        open(VISUAL_CONF, "w").write("")
    salvar(de)
    aplicar(restaurar(nome), nome)
    print(f"{de} -> {nome}")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--recarregar":
        recarregar_shell()
    elif len(sys.argv) > 1:
        trocar(sys.argv[1])
    else:
        print(atual())
