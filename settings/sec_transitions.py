"""
sec_transitions.py — Workspaces → Transition: the effect played when you switch workspace.

The effects are folders in ~/.local/share/aero-shell/transitions/<id>/ (info.json + shader.frag.in); this page
lists whatever is there, so a new transition = a new folder, nothing to change here. The engine is
~/.local/bin/hypr-transitions (doc 57). Everything applies right away (the engine reads painel.conf on each
switch); only "Movement" (Hyprland's own slide) rewrites the animations block and reloads Hyprland.
Per workspace, Workspaces → N → Transition overrides the effect there.
"""
import json
import os
import signal
import subprocess

from comum import Adw, Gtk, Secao, atualizar_painel, combo, indice, ler_painel, pagina, valor_de

PASTA = os.path.expanduser("~/.local/share/aero-shell/transitions")
FORCA = [("Subtle", "0.6"), ("Medium", "1.0"), ("Strong", "1.4")]
VELOCIDADE = [("Slow", "1.5"), ("Normal", "1.0"), ("Fast", "0.7")]
TINT = [("None (the picture's own colors)", "none"), ("From the wallpaper", "wallpaper"), ("White", "white")]
MOVIMENTO = [("Slide and fade", "slidefade"), ("Slide", "slide"), ("Vertical", "vertical"), ("Fade", "fade"),
             ("None", "none")]


def transicoes():
    """[(id, info)] of the installed transitions, in their "order"."""
    lista = []
    try:
        nomes = os.listdir(PASTA)
    except OSError:
        nomes = []
    for nome in nomes:
        try:
            info = json.load(open(os.path.join(PASTA, nome, "info.json")))
        except (OSError, ValueError):
            continue
        if os.path.isfile(os.path.join(PASTA, nome, "shader.frag.in")):
            lista.append((nome, info))
    return sorted(lista, key=lambda t: (t[1].get("order", 99), t[1].get("name", t[0])))


def efeito_global(p=None):
    p = p or ler_painel()
    return p.get("TRANS_EFFECT") or ("none" if p.get("TRANSICAO_GLITCH") == "0" else "glitch")


def previsualizar():
    """Asks the engine to play the current workspace's transition (SIGUSR1)."""
    r = subprocess.run(["pgrep", "-f", "^python3 .*/hypr-transitions$"], capture_output=True, text=True)
    for pid in r.stdout.split():
        try:
            os.kill(int(pid), signal.SIGUSR1)
        except (OSError, ValueError):
            pass
    return bool(r.stdout.split())


class SecaoTransicoes(Secao):
    id = "transitions"
    titulo = "Transition"
    icone = "media-playlist-shuffle-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        conteudo = Adw.PreferencesPage()

        self.grupo_efeito = Adw.PreferencesGroup(
            title="Effect",
            description="Played over the screen when you switch workspace. Each workspace can pick its own "
                        "(Workspaces → a workspace → Transition).")
        teste = Gtk.Button(valign=Gtk.Align.CENTER, tooltip_text="Play it now")
        teste.set_child(Adw.ButtonContent(label="Preview", icon_name="media-playback-start-symbolic"))
        teste.add_css_class("flat")
        teste.connect("clicked", lambda *_: previsualizar() or self.janela.avisar("The transition engine isn't running"))
        self.grupo_efeito.set_header_suffix(teste)
        self.linhas = {}

        ajustes = Adw.PreferencesGroup(title="Settings")
        self.forca = combo("Strength", FORCA, 1)
        self.velocidade = combo("Speed", VELOCIDADE, 1)
        self.tint = combo("Tint", TINT, 0, "From the wallpaper = the workspace you are going to")
        for linha, chave in ((self.forca, "TRANS_STRENGTH"), (self.velocidade, "TRANS_SPEED"), (self.tint, "TRANS_TINT")):
            opcoes = {self.forca: FORCA, self.velocidade: VELOCIDADE, self.tint: TINT}[linha]
            linha.connect("notify::selected", lambda l, _p, c=chave, o=opcoes: self.carregando or atualizar_painel(**{c: valor_de(l, o)}))
            ajustes.add(linha)

        mov = Adw.PreferencesGroup(title="Movement", description="How the workspaces themselves move (Hyprland).")
        self.movimento = combo("Workspace movement", MOVIMENTO, 0)
        self.movimento.connect("notify::selected", lambda *_: self.carregando or self.mudar_movimento())
        mov.add(self.movimento)

        nota = Adw.PreferencesGroup(
            description="New transitions appear here by themselves: one folder each in "
                        "~/.local/share/aero-shell/transitions")
        for g in (self.grupo_efeito, ajustes, mov, nota):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        p = ler_painel()
        self.carregando = True
        for linha in self.linhas.values():
            self.grupo_efeito.remove(linha)
        self.linhas = {}
        primeiro = None
        atual = efeito_global(p)
        for ident, info in transicoes() + [("none", {"name": "None", "description": "Only the movement below",
                                                       "icon": "action-unavailable-symbolic"})]:
            linha = Adw.ActionRow(use_markup=False, title=info.get("name", ident), subtitle=info.get("description", ""),
                                  activatable=True)
            linha.add_prefix(Gtk.Image.new_from_icon_name(info.get("icon", "image-x-generic-symbolic")))
            marca = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=ident == atual)
            if primeiro:
                marca.set_group(primeiro)
            primeiro = primeiro or marca
            marca.connect("toggled", lambda m, i=ident: m.get_active() and not self.carregando and self.escolher(i))
            linha.add_suffix(marca)
            linha.set_activatable_widget(marca)
            self.grupo_efeito.add(linha)
            self.linhas[ident] = linha
        self.forca.set_selected(indice(FORCA, p.get("TRANS_STRENGTH", "1.0"), 1))
        self.velocidade.set_selected(indice(VELOCIDADE, p.get("TRANS_SPEED", "1.0"), 1))
        self.tint.set_selected(indice(TINT, p.get("TRANS_TINT", "none"), 0))
        self.movimento.set_selected(indice(MOVIMENTO, p.get("TRANS_MOVE", "slidefade"), 0))
        self.carregando = False

    def escolher(self, ident):
        atualizar_painel(TRANS_EFFECT=ident)
        if ident != "none":
            previsualizar()

    def mudar_movimento(self):
        atualizar_painel(TRANS_MOVE=valor_de(self.movimento, MOVIMENTO))
        from sec_aparencia import gerar_animacoes
        p = ler_painel()
        gerar_animacoes(p.get("ANIM", "1") == "1", p.get("ANIM_VEL", "normal"))
        subprocess.run(["hyprctl", "reload"], capture_output=True)
