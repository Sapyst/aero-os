"""
sec_theme.py — Theme → Overview: one look at the whole theme, each row opens its sub-page in the tree.

Theme gathers what decides how the system looks: colors and mode, wallpaper, glass, icons, dock, widgets,
text and motion (they used to be separate sections). This page only reads the settings and links to them;
each workspace can override parts of it in Workspaces.
"""
import json
import os
import subprocess

from comum import Adw, GLib, Gtk, Secao, aplicar_css, em_segundo_plano, ler_painel, pagina

# (internal name, name on screen, description). "AerOS" is shown as AeroCloud: the profile keeps its old
# name so the settings saved inside it are not lost.
VISUAIS = [("DotStyle", "DotStyle", "Dots, OLED black, one red accent"),
           ("AeroTerm", "AeroTerm", "Terminal, OLED black, ASCII and glitch"),
           ("AerOS", "AeroCloud", "Light pastel glass, soft corners, fluid motion")]
PASTA_VISUAIS = os.path.expanduser("~/.local/share/aero-shell/visuais")
CSS_VISUAL = """
.cartao-visual { padding: 6px; border-radius: 16px; background: alpha(currentColor, 0.04); }
.cartao-visual:checked { background: alpha(@accent_bg_color, 0.14); box-shadow: inset 0 0 0 2px @accent_bg_color; }
.cartao-visual picture { border-radius: 11px; }
"""

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
              ("Atlas", "user-desktop-symbolic", "dock", None),
              ("Widgets", "view-grid-symbolic", "widgets", None),
              ("Text and motion", "font-x-generic-symbolic", "aparencia", "text"),
              ("Workspace transition", "media-playlist-shuffle-symbolic", "transitions", None)]

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()
        aplicar_css(CSS_VISUAL)
        gv = Adw.PreferencesGroup(title="Visual",
                                  description="The whole look of the system at once. Each visual remembers its own "
                                              "settings, so switching back restores everything.")
        caixa = Gtk.Box(spacing=12, homogeneous=True)
        self.cartoes = {}
        primeiro = None
        for nome, rotulo, desc in VISUAIS:
            b = Gtk.ToggleButton(group=primeiro)
            primeiro = primeiro or b
            b.add_css_class("cartao-visual")
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            img = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, height_request=150, can_shrink=True)
            prev = os.path.join(PASTA_VISUAIS, f"previa-{nome}.png")
            if os.path.exists(prev):
                img.set_filename(prev)
            v.append(img)
            v.append(Gtk.Label(label=rotulo, xalign=0, css_classes=["heading"], margin_start=4))
            v.append(Gtk.Label(label=desc, xalign=0, wrap=True, css_classes=["dim-label", "caption"], margin_start=4,
                               margin_bottom=2))
            b.set_child(v)
            b.connect("toggled", lambda bt, n=nome: bt.get_active() and not self.carregando_visual and self.trocar_visual(n))
            caixa.append(b)
            self.cartoes[nome] = b
        self.carregando_visual = False
        gv.add(caixa)
        conteudo.add(gv)
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

    def trocar_visual(self, nome):
        for b in self.cartoes.values():
            b.set_sensitive(False)
        rotulo = next((r for n, r, _ in VISUAIS if n == nome), nome)
        self.janela.avisar(f"Switching to {rotulo}…")

        def fim(r):
            for b in self.cartoes.values():
                b.set_sensitive(True)
            if isinstance(r, Exception) or getattr(r, "returncode", 0):
                self.janela.avisar(f"Couldn't switch: {(getattr(r, 'stderr', '') or str(r)).strip()[-120:]}")
            else:
                self.janela.avisar(f"{rotulo} is on")
            self.ao_mostrar()
        em_segundo_plano(lambda: subprocess.run([os.path.expanduser("~/.local/bin/aero-visual"), nome],
                                                capture_output=True, text=True, timeout=120), fim)

    def ao_mostrar(self):
        p = ler_painel()
        self.carregando_visual = True
        self.cartoes.get(p.get("VISUAL", "AeroTerm"), self.cartoes["AeroTerm"]).set_active(True)
        self.carregando_visual = False
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
            "Atlas": DOCK.get(p.get("DOCK_MODO", ""), p.get("DOCK_MODO", "—")),
            "Widgets": f"{ativos} active",
            "Text and motion": "Animations on" if p.get("ANIM", "1") == "1" else "Animations off",
            "Workspace transition": nomes.get(efeito_global(p), efeito_global(p))
                                    + (f" · {proprios} workspace(s) with their own theme" if proprios else ""),
        }
        for titulo, linha in self.linhas.items():
            linha.set_subtitle(subs.get(titulo, ""))
