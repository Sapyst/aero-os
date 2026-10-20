"""
sec_areas.py — Workspaces: cada área de trabalho com o SEU wallpaper e os SEUS widgets.

Grava no mesmo ~/.config/aero-shell/widgets.json dos widgets (o daemon vigia e aplica na hora):
  "por_area": {"2": {"wallpaper": "/caminho/imagem.png"}}   área sem wallpaper = o wallpaper normal do sistema
  cada widget: "areas": [1, 3]                               sem "areas" = aparece em todas
Ao trocar de área, os widgets de fora ficam escondidos (não fecham: o que estiver rodando neles continua).
"""
import os
import subprocess

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

from comum import Secao, pagina  # noqa: E402
from sec_widgets import CATALOGO, gravar, icone_do, ler, nome_do  # noqa: E402

ICONES_AREA = [("Global (Theme → Icons)", "global"), ("From this wallpaper", "wallpaper"), ("Classic (white on black)", "classic")]
AREAS = list(range(1, 6))       # o sistema tem 5 áreas (Super+1…5, as 5 bolinhas da barra)


def area_ativa():
    try:
        import json
        r = subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True, timeout=2)
        a = json.loads(r.stdout).get("id", 1)
        return a if a in AREAS else 1
    except (OSError, ValueError, subprocess.SubprocessError):
        return 1


def descrever_areas(areas):
    if not areas:
        return "On every workspace"
    return "Only on " + ", ".join(str(a) for a in sorted(areas))


class SecaoAreas(Secao):
    id = "areas"
    titulo = "Workspaces"
    icone = "view-paged-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        self.linhas = []
        conteudo = Adw.PreferencesPage()

        topo = Adw.PreferencesGroup(
            title="Workspace",
            description="Each workspace can have its own wallpaper and its own widgets. Changes apply right away.")
        self.escolha = Adw.ComboRow(title="Editing workspace", model=Gtk.StringList.new([str(a) for a in AREAS]))
        self.escolha.connect("notify::selected", lambda *_: self.carregando or self.mostrar_area())
        topo.add(self.escolha)

        self.grupo_wall = Adw.PreferencesGroup(title="Wallpaper")
        self.linha_wall = Adw.ActionRow(use_markup=False)
        self.miniatura = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=112, height_request=63,
                                     valign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        self.miniatura.add_css_class("card")
        self.linha_wall.add_prefix(self.miniatura)
        escolher = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER,
                              tooltip_text="Your folder and GitHub collections, or a file")
        escolher.connect("clicked", lambda *_: self.escolher_wallpaper())
        self.padrao = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
                                 tooltip_text="Use the system wallpaper on this workspace")
        self.padrao.add_css_class("flat")
        self.padrao.connect("clicked", lambda *_: self.definir_wallpaper(None))
        self.linha_wall.add_suffix(self.padrao)
        self.linha_wall.add_suffix(escolher)
        self.grupo_wall.add(self.linha_wall)

        self.grupo_widgets = Adw.PreferencesGroup(
            title="Widgets on this workspace",
            description="Turn a widget off to hide it here. Widgets you never touch show everywhere.")
        adicionar = Gtk.MenuButton(valign=Gtk.Align.CENTER, tooltip_text="A new widget that lives only on this workspace")
        adicionar.set_child(Adw.ButtonContent(label="Add here", icon_name="list-add-symbolic"))
        adicionar.add_css_class("flat")
        menu = Gio.Menu()
        for tipo, nome, _, _ in CATALOGO:
            item = Gio.MenuItem.new(nome, None)
            item.set_action_and_target_value("areas.adicionar", GLib.Variant.new_string(tipo))
            menu.append_item(item)
        adicionar.set_menu_model(menu)
        grupo_acoes = Gio.SimpleActionGroup()
        acao = Gio.SimpleAction.new("adicionar", GLib.VariantType.new("s"))
        acao.connect("activate", lambda _a, p: self.adicionar(p.get_string()))
        grupo_acoes.add_action(acao)
        adicionar.insert_action_group("areas", grupo_acoes)
        self.grupo_widgets.set_header_suffix(adicionar)

        # ---- this workspace's own theme: every row starts on "Global" (= what Theme says) ----
        tema = Adw.PreferencesGroup(
            title="Theme of this workspace",
            description="Anything left on Global follows Theme. Applies when you switch to this workspace.")
        self.icones_area = Adw.ComboRow(title="Icon colors", model=Gtk.StringList.new([t for t, _ in ICONES_AREA]))
        self.icones_area.connect("notify::selected", lambda *_: self.carregando or self.gravar_tema())
        self.transicao_area = Adw.ComboRow(title="Transition", subtitle="Played when arriving here")
        self.transicao_area.connect("notify::selected", lambda *_: self.carregando or self.gravar_tema())
        tema.add(self.icones_area)
        tema.add(self.transicao_area)
        # the transition effects themselves (all workspaces): a sub-page
        efeitos = Adw.ActionRow(title="Transition effects", subtitle="Effect, strength, speed, tint and movement",
                                activatable=True)
        efeitos.add_prefix(Gtk.Image.new_from_icon_name("media-playlist-shuffle-symbolic"))
        efeitos.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        efeitos.connect("activated", lambda *_: self.abrir_transicoes())
        tema.add(efeitos)

        for g in (topo, self.grupo_wall, tema, self.grupo_widgets):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)
        self.pagina.connect("showing", lambda *_: self.mostrar_area())     # back from the browser

    @property
    def area(self):
        return AREAS[self.escolha.get_selected()]

    def ao_mostrar(self):
        if getattr(self, "pedida", None):          # opened from the sidebar tree: that workspace
            area, self.pedida = self.pedida, None
        else:
            area = area_ativa()
        self.carregando = True
        self.escolha.set_selected(AREAS.index(area))
        self.carregando = False
        self.mostrar_area()

    def abrir_transicoes(self):
        from sec_transitions import SecaoTransicoes
        if not hasattr(self, "transicoes"):
            self.transicoes = SecaoTransicoes(self.janela)
        self.janela.empilhar(self.transicoes.pagina)
        self.transicoes.ao_mostrar()

    # ---- tela ----
    def mostrar_area(self):
        d = ler()
        from sec_transitions import efeito_global, transicoes
        reg_area = (d.get("por_area") or {}).get(str(self.area)) or {}
        nomes = {"none": "None"} | {i: info.get("name", i) for i, info in transicoes()}
        self.opcoes_transicao = [(f"Global ({nomes.get(efeito_global(), efeito_global())})", "")] + \
            [(n, i) for i, n in nomes.items() if i != "none"] + [("None", "none")]
        self.carregando = True
        self.transicao_area.set_model(Gtk.StringList.new([t for t, _ in self.opcoes_transicao]))
        self.transicao_area.set_selected(next((k for k, (_, v) in enumerate(self.opcoes_transicao)
                                               if v == reg_area.get("transition", "")), 0))
        self.icones_area.set_selected(next((k for k, (_, v) in enumerate(ICONES_AREA)
                                            if v == reg_area.get("icons", "global")), 0))
        self.carregando = False
        wall = ((d.get("por_area") or {}).get(str(self.area)) or {}).get("wallpaper")
        if wall and os.path.isfile(wall):
            self.linha_wall.set_title(os.path.basename(wall))
            self.linha_wall.set_subtitle(f"Only on workspace {self.area}")
            self.padrao.set_visible(True)
            mostrar = wall
        else:
            self.linha_wall.set_title("System wallpaper")
            self.linha_wall.set_subtitle("The same as the workspaces without their own")
            self.padrao.set_visible(False)
            mostrar = self.wallpaper_do_sistema()
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(mostrar, 224, 126, True)
            self.miniatura.set_paintable(Gdk.Texture.new_for_pixbuf(pb))
        except (GLib.Error, TypeError):
            self.miniatura.set_paintable(None)

        for linha in self.linhas:
            self.grupo_widgets.remove(linha)
        self.linhas = []
        widgets = d.get("widgets") or []
        if not widgets:
            vazio = Adw.ActionRow(title="No widgets yet", subtitle="Add one with “Add here”")
            self.grupo_widgets.add(vazio)
            self.linhas.append(vazio)
        for reg in widgets:
            linha = Adw.SwitchRow(use_markup=False, title=nome_do(reg["tipo"]) + self.sufixo(reg, widgets),
                                  subtitle=descrever_areas(reg.get("areas")))
            linha.add_prefix(Gtk.Image.new_from_icon_name(icone_do(reg["tipo"])))
            linha.set_active(not reg.get("areas") or self.area in reg["areas"])
            linha.set_sensitive(reg.get("ativo", True))
            linha.connect("notify::active", lambda l, _p, wid=reg["id"]: self.alternar(wid, l.get_active()))
            self.grupo_widgets.add(linha)
            self.linhas.append(linha)

    @staticmethod
    def sufixo(reg, todos):
        mesmos = [w for w in todos if w["tipo"] == reg["tipo"]]
        return f" {mesmos.index(reg) + 1}" if len(mesmos) > 1 else ""

    @staticmethod
    def wallpaper_do_sistema():
        try:
            for linha in open(os.path.expanduser("~/.config/hypr/hyprpaper.conf")):
                if linha.strip().startswith("path"):
                    return os.path.expanduser(linha.split("=", 1)[1].strip())
        except OSError:
            pass
        return ""

    # ---- gravar ----
    def alternar(self, wid, ligado):
        d = ler()
        for w in d.get("widgets") or []:
            if w["id"] != wid:
                continue
            areas = set(w.get("areas") or AREAS)
            (areas.add if ligado else areas.discard)(self.area)
            if areas >= set(AREAS):
                w.pop("areas", None)                    # em todas = sem a chave
            else:
                w["areas"] = sorted(areas)
        gravar(d)
        GLib.idle_add(lambda: self.mostrar_area() and False)

    def adicionar(self, tipo):
        d = ler()
        lista = d.setdefault("widgets", []) or []
        n = 1
        while any(w["id"] == f"{tipo}-{n}" for w in lista):
            n += 1
        quantos = sum(1 for w in lista if w["tipo"] == tipo)
        lista.append({"id": f"{tipo}-{n}", "tipo": tipo, "ativo": True, "opcoes": {}, "areas": [self.area],
                      "x": 24 + 40 * quantos, "y": 66 + 40 * quantos})
        d["widgets"] = lista
        gravar(d)
        self.janela.avisar(f"{nome_do(tipo)} added to workspace {self.area} — Arrange widgets to place it")
        self.mostrar_area()

    def escolher_wallpaper(self):
        """The same wallpaper browser as Theme → Wallpaper, already set to this workspace."""
        from sec_wallpapers import SecaoWallpapers
        if not hasattr(self, "navegador"):
            self.navegador = SecaoWallpapers(self.janela)
        self.navegador.para_area(self.area)
        self.janela.empilhar(self.navegador.pagina)
        self.navegador.ao_mostrar()

    def definir_wallpaper(self, caminho):
        d = ler()
        por_area = d.setdefault("por_area", {})
        if caminho:
            por_area.setdefault(str(self.area), {})["wallpaper"] = caminho
        else:
            (por_area.get(str(self.area)) or {}).pop("wallpaper", None)
            if not por_area.get(str(self.area)):
                por_area.pop(str(self.area), None)
        gravar(d)
        self.mostrar_area()
