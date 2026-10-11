"""
apps.py — lista dos apps instalados, com categorias e busca.
Usado pelo Super Menu e pelo Dock (no "Add apps").

Cada app instalado tem um arquivo .desktop (em /usr/share/applications ou
~/.local/share/applications) com nome, ícone, comando e categorias. O Gio (biblioteca do GLib)
lê todos eles pra gente: Gio.AppInfo.get_all().
"""
import json
import os
import re
import shlex

from gi.repository import Gio

CONFIG = os.path.expanduser("~/.config/aero-shell")

# Filtros: (chave, nome mostrado, categorias do padrão freedesktop que entram nele)
CATEGORIAS = [
    ("todos", "All", None),
    ("internet", "Internet", {"Network", "WebBrowser", "Email", "Chat", "InstantMessaging", "P2P"}),
    ("jogos", "Games", {"Game"}),
    ("dev", "Development", {"Development", "IDE", "TerminalEmulator", "Debugger"}),
    ("midia", "Media", {"AudioVideo", "Audio", "Video", "Graphics", "Photography", "Player"}),
    ("escritorio", "Office", {"Office", "TextEditor", "Viewer", "WordProcessor", "Spreadsheet"}),
    ("sistema", "System", {"System", "Settings", "Monitor", "FileManager", "Utility",
                           "PackageManager", "Accessibility", "HardwareSettings", "DesktopSettings"}),
]


def todos():
    """Apps que devem aparecer em menus (should_show tira os "escondidos"), em ordem alfabética."""
    lista = [a for a in Gio.AppInfo.get_all() if a.should_show() and isinstance(a, Gio.DesktopAppInfo)]
    return sorted(lista, key=lambda a: a.get_display_name().casefold())


def por_id(app_id):
    try:
        return Gio.DesktopAppInfo.new(app_id)
    except TypeError:
        return None          # app foi desinstalado


def categorias(app):
    return set(filter(None, (app.get_categories() or "").split(";")))


def na_categoria(app, chave):
    grupo = dict((c, g) for c, _, g in CATEGORIAS).get(chave)
    return grupo is None or bool(categorias(app) & grupo)


def combina(app, texto):
    """Busca por nome, nome genérico ("Web Browser"), palavras-chave e comando."""
    texto = texto.casefold().strip()
    if not texto:
        return True
    campos = [app.get_display_name(), app.get_generic_name() or "", app.get_executable() or "",
              " ".join(app.get_keywords() or [])]
    return any(texto in c.casefold() for c in campos)


def abrir(app):
    """Abre pelo Hyprland (hyprctl dispatch exec): o app fica independente deste programa
    e recebe as regras de janela normalmente. "gio launch" lê o .desktop e roda o comando certo."""
    Gio.Subprocess.new(["hyprctl", "dispatch", "exec", f"gio launch {shlex.quote(app.get_filename())}"],
                       Gio.SubprocessFlags.STDOUT_SILENCE)


# ---------- listas salvas (fixados no menu, apps do dock) ----------

def ler_lista(nome, padrao):
    try:
        with open(os.path.join(CONFIG, f"{nome}.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return list(padrao)


def gravar_lista(nome, itens):
    os.makedirs(CONFIG, exist_ok=True)
    caminho = os.path.join(CONFIG, f"{nome}.json")
    with open(caminho + ".tmp", "w") as f:
        json.dump(itens, f, indent=2)
    os.replace(caminho + ".tmp", caminho)


# ---------- Dock (doc 24) ----------

# Apps que o Dock tem quando ainda não existe ~/.config/aero-shell/dock.json (o Aero pediu estes três).
# O Super Menu também usa: "Add to Dock" antes do Dock existir não pode apagar os três.
DOCK_PADRAO = [{"tipo": "app", "id": "com.anthropic.Claude.desktop"},
               {"tipo": "app", "id": "firefox.desktop"},
               {"tipo": "app", "id": "com.aero.Settings.desktop"}]


def nomes_de_janela(app):
    """Nomes que a janela de um app pode ter no Hyprland ("class"), em minúsculas:
    o nome do .desktop (com.aero.Settings), o StartupWMClass (com.anthropic.Claude)
    e o nome do programa (firefox)."""
    nomes = {app.get_id().removesuffix(".desktop").lower()}
    # Atalho de jogo criado pela Steam (Exec=steam steam://rungameid/570): o programa é "steam", mas a janela
    # do JOGO não é "steam". Ela é steam_app_570 (Proton) ou o nome do jogo sem espaço (ex: dota2, nativo).
    # Sem isso, "Dota 2" (vem antes de "Steam" na ordem alfabética) roubava a janela da própria Steam.
    jogo = re.search(r"steam://rungameid/(\d+)", app.get_string("Exec") or "")
    if jogo:
        nomes.add(f"steam_app_{jogo.group(1)}")
        nomes.add(re.sub(r"[^a-z0-9]", "", app.get_name().lower()))
        if app.get_startup_wm_class():
            nomes.add(app.get_startup_wm_class().lower())
        return nomes
    if app.get_startup_wm_class():
        nomes.add(app.get_startup_wm_class().lower())
    executavel = os.path.basename((app.get_executable() or "").split()[0] if app.get_executable() else "")
    if executavel and executavel not in ("env", "flatpak", "sh", "bash", "python3", "gio"):
        nomes.add(executavel.lower())
    return nomes


_por_classe = {}


def app_da_janela(classe):
    """O contrário: dada a "class" de uma janela, qual app (.desktop) é? (guardado num cache)"""
    chave = (classe or "").lower()
    if chave not in _por_classe:
        _por_classe[chave] = next((a for a in todos() if chave in nomes_de_janela(a)), None)
    return _por_classe[chave]


def origem(app):
    """De onde o app veio: "sistema" (pacman), "flatpak", "steam" (atalho de jogo) ou "meus"."""
    arquivo = app.get_filename() or ""
    if "/flatpak/" in arquivo:
        return "flatpak"
    if arquivo.startswith(os.path.expanduser("~/.local/share/applications")):
        return "steam" if "steam://rungameid" in (app.get_string("Exec") or "") else "meus"
    return "sistema"


# ---------- busca esperta do Super Menu (doc 25) ----------

import difflib  # noqa: E402  (vem com o Python)

# Palavras que levam direto a uma seção do Settings: (palavras, id da seção, nome mostrado, ícone)
ATALHOS_SETTINGS = [
    (("display", "screen", "monitor", "resolution", "scale", "tela"), "tela", "Display", "video-display-symbolic"),
    (("power", "battery", "lid", "sleep", "energia", "bateria"), "energia", "Power", "battery-symbolic"),
    (("sound", "audio", "volume", "speaker", "microphone", "mic", "som"), "som", "Sound", "audio-volume-high-symbolic"),
    (("bluetooth", "headphones", "fone"), "bluetooth", "Bluetooth", "bluetooth-symbolic"),
    (("network", "wifi", "wi-fi", "internet", "ethernet", "rede"), "rede", "Network", "network-wireless-symbolic"),
    (("theme", "appearance", "dark", "light", "color", "accent", "blur", "glass", "night", "font", "tema",
      "window buttons", "buttons", "minimize", "botões", "cursor", "pointer"),
     "aparencia", "Theme", "preferences-desktop-appearance-symbolic"),
    (("wallpaper", "background", "papel"), "wallpapers", "Theme → Wallpaper", "preferences-desktop-wallpaper-symbolic"),
    (("dock",), "dock", "Dock", "view-app-grid-symbolic"),
    (("keyboard", "shortcut", "layout", "clipboard", "alt+tab", "teclado"), "teclado", "Keyboard",
     "input-keyboard-symbolic"),
    (("mouse", "touchpad", "trackpad", "gesture", "scroll"), "mouse", "Mouse & Touchpad", "input-mouse-symbolic"),
    (("update", "upgrade", "packages", "atualizar"), "atualizacoes", "Updates", "software-update-available-symbolic"),
    (("about", "system", "version", "sobre"), "sobre", "About", "help-about-symbolic"),
]


def pontuar(app, texto):
    """Quão bem o app combina com o texto: 3 = o nome começa com ele, 2 = contém, 1 = parecido
    (erro de digitação, ex: "fierfox"), 0 = nada. O difflib compara letra a letra."""
    texto = texto.casefold().strip()
    nome = app.get_display_name().casefold()
    if nome.startswith(texto) or any(p.startswith(texto) for p in nome.split()):
        return 3
    if combina(app, texto):
        return 2
    if len(texto) >= 4 and max((difflib.SequenceMatcher(None, texto, p).ratio() for p in nome.split()),
                               default=0) > 0.75:
        return 1
    return 0


def secoes_settings(texto):
    texto = texto.casefold().strip()
    if len(texto) < 2:
        return []
    return [(sid, nome, icone) for palavras, sid, nome, icone in ATALHOS_SETTINGS
            if any(p.startswith(texto) or texto in p for p in palavras)]


def calcular(texto):
    """Se o texto for uma conta (2+2, 15% de 80 escrito "80*0.15", (3+4)/2...), devolve o resultado.
    Usa a "árvore" do Python (ast) e só aceita números e + - * / % ** — nunca roda código."""
    import ast
    import operator
    texto = texto.replace(",", ".").replace("x", "*").replace("^", "**").strip()
    if not texto or not any(c in texto for c in "+-*/%") or not any(c.isdigit() for c in texto):
        return None
    ops = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Mod: operator.mod, ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}

    def avaliar(no):
        if isinstance(no, ast.Constant) and isinstance(no.value, (int, float)):
            return no.value
        if isinstance(no, ast.BinOp) and type(no.op) in ops:
            if isinstance(no.op, ast.Pow) and abs(avaliar(no.right)) > 100:
                raise ValueError("expoente grande demais")
            return ops[type(no.op)](avaliar(no.left), avaliar(no.right))
        if isinstance(no, ast.UnaryOp) and type(no.op) in ops:
            return ops[type(no.op)](avaliar(no.operand))
        raise ValueError("não é conta")
    try:
        r = avaliar(ast.parse(texto, mode="eval").body)
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None
    return f"{r:,.10g}".replace(",", " ") if isinstance(r, float) else f"{r:,}".replace(",", " ")
