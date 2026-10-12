"""
sec_icones.py — Icons: o iconizador ASCII, o pack de ícones e o ícone de cada app.

Toda a lógica mora em ~/.local/share/aero-shell/icones.py (comando aero-icones); aqui é só a interface,
que chama o comando em segundo plano (os ícones podem levar alguns segundos para gerar).
  - ASCII icons: liga/desliga (desligar = restaurar os originais), refazer tudo.
  - Icon pack: o pack por baixo (Adwaita, Papirus, Breeze...) e instalar packs novos (pasta, .tar.*, .zip).
  - Apps: busca + cada app com imagem própria, ASCII só dele, ou voltar ao original.
"""
import json
import os
import subprocess

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from comum import Secao, em_segundo_plano, pagina  # noqa: E402

COMANDO = os.path.expanduser("~/.local/bin/aero-icones")
# ASCII icon colours (aero-icones --tint); per-workspace overrides live in Workspaces → each workspace
ICON_TINT = [("Classic (white on black)", "classic"), ("Follow system theme", "system"),
             ("General: system wallpaper", "general"), ("Per workspace: each wallpaper", "workspaces")]
ROTULO = {"ascii": "ASCII", "ascii-custom": "ASCII (this app only)", "custom": "Your image", "pack": "From the pack"}


def rodar(*args, timeout=600):
    r = subprocess.run([COMANDO, *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "failed")
    return r.stdout


class SecaoIcones(Secao):
    id = "icones"
    titulo = "Icons"
    icone = "applications-graphics-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        self.linhas_apps = []
        self.estado = {}
        conteudo = Adw.PreferencesPage()

        # ---- iconizador ASCII ----
        g1 = Adw.PreferencesGroup(
            title="ASCII icons",
            description="Every app icon in the menu, dock and Alt+Tab becomes a monochrome ASCII tile. "
                        "Done once and remembered; new apps are converted at login. Turning it off restores the originals.")
        self.chave = Adw.SwitchRow(title="Iconize app icons")
        self.chave.connect("notify::active", lambda *_: self.carregando or self.alternar())
        g1.add(self.chave)
        self.linha_estado = Adw.ActionRow(title="Status", subtitle="…")
        self.giro = Gtk.Spinner(valign=Gtk.Align.CENTER, visible=False)
        self.linha_estado.add_suffix(self.giro)
        refazer = Gtk.Button(label="Redo all", valign=Gtk.Align.CENTER, tooltip_text="Convert every icon again")
        refazer.connect("clicked", lambda *_: self.executar(["--tudo"], "Redoing every icon…"))
        self.refazer = refazer
        self.linha_estado.add_suffix(refazer)
        g1.add(self.linha_estado)
        self.tint = Adw.ComboRow(title="Icon colors", model=Gtk.StringList.new([t for t, _ in ICON_TINT]),
                                 subtitle="Light wallpaper = light tiles with dark characters")
        self.tint.connect("notify::selected", lambda *_: self.carregando or self.executar(
            ["--tint", ICON_TINT[self.tint.get_selected()][1]], "Redrawing the icons…"))
        g1.add(self.tint)

        # ---- pack ----
        g2 = Adw.PreferencesGroup(title="Icon pack",
                                  description="The icons underneath: used for everything that isn't ASCII or custom.")
        self.packs = []
        self.combo_pack = Adw.ComboRow(title="Pack", model=Gtk.StringList.new([]))
        self.combo_pack.connect("notify::selected", lambda *_: self.carregando or self.mudar_pack())
        g2.add(self.combo_pack)
        instalar = Adw.ActionRow(title="Install a pack", subtitle="A folder or an archive (.tar.gz, .tar.xz, .zip) with index.theme",
                                 activatable=True)
        instalar.add_suffix(Gtk.Image.new_from_icon_name("document-open-symbolic"))
        instalar.connect("activated", lambda *_: self.escolher_pack())
        g2.add(instalar)
        # the system's own icons (menus, settings, panels): black-and-white unless you include them
        self.chave_sistema = Adw.SwitchRow(title="Use the pack for system icons too",
                                           subtitle="Off: settings, menus and panels keep the black-and-white system icons; "
                                                    "only apps and the desktop follow the pack and ASCII")
        self.chave_sistema.connect("notify::active", lambda *_: self.carregando or self.executar(
            ["--system", "on" if self.chave_sistema.get_active() else "off"], "Applying…"))
        g2.add(self.chave_sistema)
        # icons that got stuck (blank or "broken image" in the dock): read everything again
        recarregar = Adw.ActionRow(title="Reload icons",
                                   subtitle="If icons look broken or old: rebuilds the icon cache and restarts the dock, Alt+Tab, Super Menu and tray")
        botao_recarregar = Gtk.Button(label="Reload", valign=Gtk.Align.CENTER)
        botao_recarregar.connect("clicked", lambda *_: self.executar(["--reload"], "Reloading icons…"))
        recarregar.add_suffix(botao_recarregar)
        g2.add(recarregar)
        abrir = Adw.ActionRow(title="Open the icons folder", subtitle="~/.local/share/icons", activatable=True)
        abrir.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
        abrir.connect("activated", lambda *_: GLib.spawn_async([os.path.expanduser("~/.local/bin/ez-fm"),
                                                                 os.path.expanduser("~/.local/share/icons")]))
        g2.add(abrir)

        # ---- apps ----
        self.g3 = Adw.PreferencesGroup(title="App icons", description="Pick any app to give it its own icon.")
        self.busca = Gtk.SearchEntry(placeholder_text="Search apps", hexpand=True)
        self.busca.connect("search-changed", lambda *_: self.filtrar())
        caixa = Gtk.Box(margin_bottom=6)
        caixa.append(self.busca)
        self.g3.add(caixa)

        for g in (g1, g2, self.g3):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    # ---------- dados ----------
    def ao_mostrar(self):
        self.ocupado(True, "Reading icons…")
        em_segundo_plano(lambda: (json.loads(rodar("--status")), json.loads(rodar("--packs"))), self.mostrar)

    def mostrar(self, resultado):
        self.ocupado(False)
        if isinstance(resultado, Exception):
            self.linha_estado.set_subtitle(f"Error: {resultado}")
            return
        self.estado, packs = resultado
        self.carregando = True
        self.chave.set_active(self.estado["ligado"])
        self.chave_sistema.set_active(self.estado.get("system", False))
        modos = [v for _, v in ICON_TINT]
        self.tint.set_selected(modos.index(self.estado.get("tint", "classic")) if self.estado.get("tint") in modos else 0)
        self.packs = sorted(packs.items(), key=lambda kv: kv[1].casefold())
        self.combo_pack.set_model(Gtk.StringList.new([n for _, n in self.packs]))
        ids = [k for k, _ in self.packs]
        if self.estado["base"] in ids:
            self.combo_pack.set_selected(ids.index(self.estado["base"]))
        self.carregando = False
        apps = self.estado["apps"]
        n_ascii = sum(1 for a in apps.values() if a["tipo"] == "ascii")
        self.linha_estado.set_subtitle(
            f"{n_ascii} of {len(apps)} apps in ASCII · {self.estado['custom']} custom" if self.estado["ligado"]
            else f"Off · {self.estado['custom']} custom icon(s)")
        self.refazer.set_sensitive(self.estado["ligado"])
        for l in self.linhas_apps:
            self.g3.remove(l)
        self.linhas_apps = []
        tema = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        for app_id, info in apps.items():
            icone = info["icone"]
            img = Gtk.Image(pixel_size=32)
            if icone.startswith("/"):
                img.set_from_file(icone)
            else:
                img.set_from_icon_name(icone)
            linha = Adw.ActionRow(use_markup=False, title=info["nome"], subtitle=ROTULO.get(info["tipo"], info["tipo"]))
            linha.add_prefix(img)
            menu = Gio.Menu()
            for rotulo, acao in (("Choose an image…", "imagem"), ("ASCII for this app", "ascii"), ("Back to original", "original")):
                item = Gio.MenuItem.new(rotulo, None)
                item.set_action_and_target_value(f"icone.{acao}", GLib.Variant.new_string(app_id))
                menu.append_item(item)
            botao = Gtk.MenuButton(icon_name="view-more-symbolic", valign=Gtk.Align.CENTER, menu_model=menu)
            botao.add_css_class("flat")
            linha.add_suffix(botao)
            linha.app_busca = f"{info['nome']} {app_id}".casefold()
            self.g3.add(linha)
            self.linhas_apps.append(linha)
        grupo = Gio.SimpleActionGroup()
        for acao, fn in (("imagem", self.escolher_imagem), ("ascii", lambda a: self.executar(["--app", a, "--ascii"], "Making ASCII…")),
                         ("original", lambda a: self.executar(["--app", a, "--original"], "Restoring…"))):
            ac = Gio.SimpleAction.new(acao, GLib.VariantType.new("s"))
            ac.connect("activate", lambda _a, p, f=fn: f(p.get_string()))
            grupo.add_action(ac)
        self.g3.insert_action_group("icone", grupo)
        self.filtrar()
        del tema

    def filtrar(self):
        t = self.busca.get_text().casefold().strip()
        for l in self.linhas_apps:
            l.set_visible(not t or t in l.app_busca)

    # ---------- ações ----------
    def ocupado(self, sim, texto=None):
        self.giro.set_visible(sim)
        self.giro.set_spinning(sim)
        self.chave.set_sensitive(not sim)
        if texto:
            self.linha_estado.set_subtitle(texto)

    def executar(self, args, texto):
        self.ocupado(True, texto)
        em_segundo_plano(lambda: rodar(*args), self.depois)

    def depois(self, r):
        if isinstance(r, Exception):
            self.janela.avisar(f"Icons: {r}")
        self.ao_mostrar()

    def alternar(self):
        if self.chave.get_active():
            self.executar(["--ligar"], "Converting icons… (first time takes a few seconds)")
        else:
            self.executar(["--desligar"], "Restoring the original icons…")

    def mudar_pack(self):
        i = self.combo_pack.get_selected()
        if 0 <= i < len(self.packs):
            self.executar(["--base", self.packs[i][0]], f"Switching to {self.packs[i][1]}…")

    def escolher_pack(self):
        d = Gtk.FileDialog(title="Install an icon pack")

        def pronto(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f:
                self.executar(["--instalar", f.get_path()], "Installing the pack…")
        d.open(self.janela, None, pronto)

    def escolher_imagem(self, app_id):
        d = Gtk.FileDialog(title="Icon image")
        filtro = Gtk.FileFilter(name="Images")
        filtro.add_pixbuf_formats()
        filtro.add_suffix("svg")
        lista = Gio.ListStore.new(Gtk.FileFilter)
        lista.append(filtro)
        d.set_filters(lista)

        def pronto(dlg, res):
            try:
                f = dlg.open_finish(res)
            except GLib.Error:
                return
            if f:
                self.executar(["--app", app_id, "--imagem", f.get_path()], "Applying your image…")
        d.open(self.janela, None, pronto)
