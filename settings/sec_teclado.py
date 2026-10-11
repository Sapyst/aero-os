"""
sec_teclado.py — seção Keyboard (layouts e atalhos).

Os atalhos vêm de `hyprctl binds -j`: a lista REAL que o Hyprland está usando agora,
então essa tela nunca fica desatualizada em relação ao hyprland.conf.
"""
import json
import os

from gi.repository import Adw, Gtk

from comum import (Secao, atualizar_painel, botao, combo, confirmar, indice, ler_painel, linha_app,
                   pagina, rodar, valor_de)

CLIP_TAMANHOS = [("25 items", "25"), ("50 items", "50"), ("100 items", "100")]
CLIP_PASTA = os.path.expanduser("~/.local/share/aero-clipboard")

NOMES_LAYOUT = {"br": "Portuguese (Brazil, ABNT2)", "us": "English (US)"}

# modmask: cada tecla modificadora é um "bit" de um número. Somando, dá a combinação.
MODIFICADORES = [(64, "Super"), (4, "Ctrl"), (8, "Alt"), (1, "Shift")]
TECLAS = {"left": "←", "right": "→", "up": "↑", "down": "↓", "Print": "Print Screen",
          "mouse:272": "Left-drag", "mouse:273": "Right-drag", "SUPER_L": "(press and release)",
          "space": "Space", "Tab": "Tab", "period": ".", "Escape": "Esc"}
DIRECOES = {"l": "left", "r": "right", "u": "up", "d": "down"}


def descrever(b):
    d, arg = b["dispatcher"], b["arg"]
    if d == "exec":
        for trecho, texto in (("papel-de-parede", "Set copied image as wallpaper"), ("hypr-encaixar left", "Snap window to the left half"), ("hypr-encaixar right", "Snap window to the right half"),
                              ("hypr-area-de-trabalho", "Show desktop"), ("emojis", "Emoji picker"),
                              ("energia logout", "Log out (asks first)"), ("alttab overview", "All windows (overview)"), ("alttab next", "Switch windows (Alt+Tab)"), ("clipboard", "Clipboard history"), ("alttab prev", "Switch windows, backwards"),
                              ("hypr-minimizar", "Minimize window"), ("hypr-sempre-visivel", "Always visible (no dim)"),
                              ("supermenu", "Open Super Menu"), ("hypr-minimizados", "Restore a minimized window"), ("wofi", "Open Super Menu"), ("kitty", "Open terminal"),
                              ("thunar", "Open Files"), ("configuracoes", "Open Settings"),
                              ("print-area", "Screenshot of an area")):
            if trecho in arg:
                return texto
        return f"Run {arg}"
    if d in ("movefocus", "movewindow") and arg in DIRECOES:
        return ("Focus window " if d == "movefocus" else "Move window ") + DIRECOES[arg]
    textos = {"killactive": "Close window", "exit": "Log out (quit Hyprland)",
              "fullscreen": "Fullscreen", "togglefloating": "Float / tile window",
              "movewindow": "Move window", "resizewindow": "Resize window"}
    if d == "workspace":
        return f"Go to workspace {arg}"
    if d == "movetoworkspace":
        return f"Move window to workspace {arg}"
    return textos.get(d, f"{d} {arg}".strip())


def combinacao(b):
    partes = [nome for bit, nome in MODIFICADORES if b["modmask"] & bit]   # & = "este bit está ligado?"
    tecla = TECLAS.get(b["key"], b["key"])
    return " + ".join(partes + [tecla]) if b["key"] != "SUPER_L" else f"Super {tecla}"


class SecaoTeclado(Secao):
    id = "teclado"
    titulo = "Keyboard"
    icone = "input-keyboard-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.conteudo = Adw.PreferencesPage()
        self.grupos = []
        self.pagina = pagina(self.titulo, self.conteudo)

    def ao_mostrar(self):
        for g in self.grupos:
            self.conteudo.remove(g)
        self.grupos = []

        layouts = Adw.PreferencesGroup(title="Layouts", description="Switch layout: Super + Space")
        r = rodar(["hyprctl", "getoption", "input:kb_layout", "-j"])
        codigos = json.loads(r.stdout).get("str", "").split(",") if r.returncode == 0 else []
        for i, codigo in enumerate(c for c in codigos if c):
            linha = Adw.ActionRow(use_markup=False, title=NOMES_LAYOUT.get(codigo, codigo),
                                  subtitle="Default" if i == 0 else "")
            layouts.add(linha)

        # Alt+Tab (o programa lê o painel.conf a cada abertura: muda na hora, sem reiniciar nada)
        painel = ler_painel()
        alttab = Adw.PreferencesGroup(
            title="Alt+Tab", description="Hold Alt and press Tab to cycle; release Alt to switch. "
                                         "A quick Alt+Tab jumps back to the previous window.")
        for chave, titulo, sub in (("ALTTAB_TODAS", "Windows from all workspaces",
                                    "Off: only the current workspace (minimized windows always show)"),
                                   ("ALTTAB_MINIATURAS", "Show window previews",
                                    "Off: just the app icon (lighter)")):
            linha = Adw.SwitchRow(use_markup=False, title=titulo, subtitle=sub,
                                  active=painel.get(chave, "1") == "1")
            linha.connect("notify::active", lambda l, _p, c=chave: atualizar_painel(**{c: "1" if l.get_active() else "0"}))
            alttab.add(linha)

        # Histórico da área de transferência (doc 22). O clipboard-guardar lê CLIP_MAX a cada cópia.
        clip = Adw.PreferencesGroup(title="Clipboard history",
                                    description="Ctrl+Shift+V shows recent copies: text, images and files.")
        tamanho = combo("Keep", CLIP_TAMANHOS, indice(CLIP_TAMANHOS, painel.get("CLIP_MAX", "50"), 1))
        tamanho.connect("notify::selected", lambda l, _p: atualizar_painel(CLIP_MAX=valor_de(l, CLIP_TAMANHOS)))
        clip.add(tamanho)
        limpar = Adw.ActionRow(use_markup=False, title="Clear history",
                               subtitle="Deletes everything saved (what is copied right now stays)")
        limpar.add_suffix(botao("Clear", lambda *_: confirmar(
            self.janela, "Clear clipboard history?", "All saved items will be deleted.", "Clear",
            self.limpar_clipboard, destrutivo=True), estilo="destructive-action"))
        clip.add(limpar)

        atalhos = Adw.PreferencesGroup(title="Shortcuts")
        r = rodar(["hyprctl", "binds", "-j"])
        for b in json.loads(r.stdout) if r.returncode == 0 else []:
            if b["key"].startswith("switch:") or "Lid" in b["key"]:
                continue                         # sensores (tampa), não são teclas
            linha = Adw.ActionRow(use_markup=False, title=descrever(b))
            rotulo = Gtk.Label(label=combinacao(b))
            rotulo.add_css_class("dim-label")
            rotulo.add_css_class("monospace")
            linha.add_suffix(rotulo)
            atalhos.add(linha)

        editar = Adw.PreferencesGroup()
        editar.add(linha_app("Edit shortcuts", "Opens hyprland.conf in the terminal (see doc 03)",
                             ["kitty", "nano", os.path.expanduser("~/.config/hypr/hyprland.conf")],
                             "document-edit-symbolic"))

        for g in (layouts, alttab, clip, atalhos, editar):
            self.conteudo.add(g)
            self.grupos.append(g)

    def limpar_clipboard(self):
        """Apaga os arquivos do histórico e zera o índice (doc 22)."""
        if os.path.isdir(CLIP_PASTA):
            for nome in os.listdir(CLIP_PASTA):
                if nome.endswith((".txt", ".bin")) or nome == "indice.json":
                    os.remove(os.path.join(CLIP_PASTA, nome))
