"""
sec_theme.py — Theme → Overview: one look at the whole theme, each row opens its sub-page in the tree.

Theme gathers what decides how the system looks: colors and mode, wallpaper, glass, icons, dock, widgets,
text and motion (they used to be separate sections). This page only reads the settings and links to them;
each workspace can override parts of it in Workspaces.
"""
import json
import os
import subprocess

from comum import Adw, Gtk, Secao, ler_painel, pagina

VIDRO = {"desligado": "Off", "leve": "Subtle", "media": "Medium", "forte": "Strong"}
TINT = {"classic": "classic colors", "system": "theme colors", "general": "wallpaper colors",
        "workspaces": "colors per workspace"}
DOCK = {"inteligente": "Intellihide", "sempre": "Always visible", "auto": "Auto-hide"}


class SecaoTema(Secao):
    id = "tema"
    titulo = "Overview"
    icone = "preferences-desktop-appearance-symbolic"

    # (title, icon, section id, sub-page)
    LINHAS = [("Colors and mode", "weather-clear-night-symbolic", "aparencia", "colors"),
              ("Wallpaper", "image-x-generic-symbolic", "aparencia", "wallpaper"),
              ("Glass and windows", "window-new-symbolic", "aparencia", "glass"),
              ("Icons", "applications-graphics-symbolic", "icones", None),
              ("Dock", "user-desktop-symbolic", "dock", None),
              ("Widgets", "view-grid-symbolic", "widgets", None),
              ("Text and motion", "font-x-generic-symbolic", "aparencia", "text"),
              ("Workspace transition", "media-playlist-shuffle-symbolic", "transitions", None)]

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()
        g = Adw.PreferencesGroup(title="Global theme",
                                 description="The default for every workspace. Workspaces can change parts of it.")
        self.linhas = {}
        for titulo, icone, sec, sub in self.LINHAS:
            linha = Adw.ActionRow(use_markup=False, title=titulo, activatable=True)
            linha.add_prefix(Gtk.Image.new_from_icon_name(icone))
            linha.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            linha.connect("activated", lambda *_, s=sec, b=sub: self.janela.ir(s, b))
            g.add(linha)
            self.linhas[titulo] = linha
        conteudo.add(g)
        self.pagina = pagina("Theme", conteudo)

    def ao_mostrar(self):
        p = ler_painel()
        escuro = "dark" in subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                                          capture_output=True, text=True).stdout
        modo = "Automatic by time" if p.get("TEMA_MODO") == "auto" else ("Dark" if escuro else "Light")
        cor = "colors from the wallpaper" if p.get("WALL_CORES") == "1" else f"accent {p.get('COR_DESTAQUE', 'mono')}"
        try:
            w = json.load(open(os.path.expanduser("~/.config/aero-shell/widgets.json")))
        except (OSError, ValueError):
            w = {}
        ativos = sum(1 for x in w.get("widgets") or [] if x.get("ativo", True))
        proprios = sum(1 for r in (w.get("por_area") or {}).values() if r)
        from sec_transitions import efeito_global, transicoes
        nomes = {i: info.get("name", i) for i, info in transicoes()} | {"none": "None"}
        subs = {
            "Colors and mode": f"{modo} · {cor}",
            "Wallpaper": os.path.basename(p.get("WALL_ORIGINAL", "").strip('"')) or "—",
            "Glass and windows": f"Glass {VIDRO.get(p.get('VIDRO', 'media'), '?').lower()} · "
                                 f"taskbar {round(float(p.get('BAR_ALPHA', '0.45')) * 100)}%",
            "Icons": ("ASCII, " + TINT.get(p.get("ICONS_TINT", "classic"), "")) if p.get("ICONES_ASCII") == "1"
                     else f"Pack {p.get('ICONES_BASE', 'Adwaita')}",
            "Dock": DOCK.get(p.get("DOCK_MODO", ""), p.get("DOCK_MODO", "—")),
            "Widgets": f"{ativos} active",
            "Text and motion": "Animations on" if p.get("ANIM", "1") == "1" else "Animations off",
            "Workspace transition": nomes.get(efeito_global(p), efeito_global(p))
                                    + (f" · {proprios} workspace(s) with their own theme" if proprios else ""),
        }
        for titulo, linha in self.linhas.items():
            linha.set_subtitle(subs.get(titulo, ""))
