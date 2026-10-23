"""
sec_padroes.py — seção Default Apps: que programa abre cada tipo de arquivo (Page do servicos/padroes.py).

- Os tipos vêm separados por categoria (Video, Music and audio, Images, Camera RAW…), cada uma numa linha
  que abre como sanfona.
- "App for all": escolher um app aplica a TODOS os tipos da categoria que ele abre (só aparecem os apps
  que abrem algum tipo dela, com quantos ele cobre). Os tipos que ele não abre ficam como estavam.
- Logo abaixo, cada tipo com o seu app; mudar um tipo deixa a categoria "Custom" sozinha.
- A busca no topo acha tipos por nome ou MIME ("mp4", "matroska", "pdf").
A escolha vale pro sistema todo (~/.config/mimeapps.list, o mesmo do xdg-open e do "Abrir com").
"""
import os
import sys

from gi.repository import Adw, GLib, Gtk

from comum import Secao, pagina

sys.path.insert(0, os.path.expanduser("~/.local/share/aero-shell"))
from servicos import padroes  # noqa: E402
import visitas  # noqa: E402  (aero-shell/visitas.py: onde o ORBIT abre, e o que ele aprende)

CUSTOM = "Custom"
# ORBIT (Super+Shift+F): onde ele abre. O mesmo arquivo que a pílula FREQUENT | COMPUTER do próprio ORBIT grava
# (~/.config/aero/orbit.json); o ORBIT relê toda vez que abre, então vale na próxima abertura.
ORBIT_HOME = [("Most visited folders", "frequent"), ("This computer", "computer")]


def _nome(app):
    return app.get_display_name() if app else "None"


class SecaoPadroes(Secao):
    id = "padroes"
    titulo = "Default Apps"
    icone = "preferences-system-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.mexendo = False
        self.categorias = {}          # cat -> {"linha", "combo", "apps", "tipos": {tipo: (combo, [apps])}, "montada"}
        conteudo = Adw.PreferencesPage()

        g_busca = Adw.PreferencesGroup(description="Which app opens each kind of file. A whole category can go to "
                                                   "one app; changing a single type makes the category Custom.")
        conteudo.add(g_busca)
        self.busca = Adw.EntryRow(title="Search file types (mp4, pdf, matroska…)")
        self.busca.connect("changed", self.ao_buscar)
        g_busca.add(self.busca)
        self.g_resultados = Adw.PreferencesGroup(title="Matching types", visible=False)
        conteudo.add(self.g_resultados)
        self.linhas_resultado = []

        self.g_cats = Adw.PreferencesGroup(title="Categories")
        conteudo.add(self.g_cats)
        for cat, nome, icone in padroes.CATEGORIAS:
            linha = Adw.ExpanderRow(title=nome)
            linha.add_prefix(Gtk.Image.new_from_icon_name(icone))
            linha.connect("notify::expanded", self.ao_expandir, cat)
            self.g_cats.add(linha)
            self.categorias[cat] = {"linha": linha, "montada": False, "tipos": {}}

        g_orbit = Adw.PreferencesGroup(title="File finder",
                                       description="Argus (Super+Shift+F) learns the folders you visit.")
        self.orbit_home = Adw.ComboRow(title="Argus opens on",
                                       subtitle="Your folders by use, or a hub with every drive and place",
                                       model=Gtk.StringList.new([t for t, _ in ORBIT_HOME]))
        self.orbit_home.add_prefix(Gtk.Image.new_from_icon_name("folder-saved-search-symbolic"))
        self.orbit_home.connect("notify::selected", self.ao_orbit_home)
        g_orbit.add(self.orbit_home)
        conteudo.add(g_orbit)

        self.pagina = pagina(self.titulo, conteudo)

    # ---------- mostrar ----------

    def ao_mostrar(self):
        padroes.recarregar()
        modo = visitas.ler_modo()                   # a pílula do ORBIT pode ter trocado desde a última vez
        self.mexendo = True
        self.orbit_home.set_selected(next((i for i, (_, m) in enumerate(ORBIT_HOME) if m == modo), 0))
        self.mexendo = False
        for cat, d in self.categorias.items():
            n = len(padroes.tipos(cat))
            d["linha"].set_visible(n > 0)
            self.atualizar_resumo(cat)
            if d["montada"]:
                self.atualizar_combos(cat)

    def atualizar_resumo(self, cat):
        d = self.categorias[cat]
        n = len(padroes.tipos(cat))
        tipo, app_id = padroes.estado(cat)
        if tipo == "app":
            quem = next((_nome(a) for a, _ in padroes.apps_da_categoria(cat) if a.get_id() == app_id), app_id)
        else:
            quem = CUSTOM if tipo == "custom" else "No default app"
        d["linha"].set_subtitle(f"{quem} · {n} type{'s' if n != 1 else ''}")

    # ---------- montar a categoria só quando abre (Code e Other têm 150+ tipos) ----------

    def ao_expandir(self, linha, _p, cat):
        d = self.categorias[cat]
        if not linha.get_expanded() or d["montada"]:
            return
        d["montada"] = True
        d["apps"] = padroes.apps_da_categoria(cat)
        total = len(padroes.tipos(cat))
        rotulos = [CUSTOM] + [f"{_nome(a)}  ({n} of {total})" for a, n in d["apps"]]
        combo = Adw.ComboRow(title="App for all", subtitle="Applies to every type it can open",
                             model=Gtk.StringList.new(rotulos))
        combo.add_css_class("property")
        combo.connect("notify::selected", self.ao_categoria, cat)
        linha.add_row(combo)
        d["combo"] = combo
        for t in padroes.tipos(cat):
            apps = padroes.apps_do_tipo(t)
            c = Adw.ComboRow(title=padroes.descricao(t), subtitle=t,
                             model=Gtk.StringList.new(["None"] + [_nome(a) for a in apps]))
            c.connect("notify::selected", self.ao_tipo, cat, t)
            linha.add_row(c)
            d["tipos"][t] = (c, apps)
        self.atualizar_combos(cat)

    def atualizar_combos(self, cat):
        d = self.categorias[cat]
        self.mexendo = True
        tipo, app_id = padroes.estado(cat)
        ids = [a.get_id() for a, _ in d["apps"]]
        d["combo"].set_selected(ids.index(app_id) + 1 if tipo == "app" and app_id in ids else 0)
        for t, (c, apps) in d["tipos"].items():
            p = padroes.padrao(t)
            pid = p.get_id() if p else None
            c.set_selected(next((i + 1 for i, a in enumerate(apps) if a.get_id() == pid), 0))
        self.mexendo = False

    # ---------- mudanças ----------

    def ao_categoria(self, combo, _p, cat):
        if self.mexendo:
            return
        i = combo.get_selected()
        d = self.categorias[cat]
        if i == 0 or i > len(d["apps"]):
            self.atualizar_combos(cat)          # "Custom" não é uma escolha: volta pro estado real
            return
        app = d["apps"][i - 1][0]
        padroes.definir_categoria(cat, app.get_id())
        self.atualizar_combos(cat)
        self.atualizar_resumo(cat)

    def ao_tipo(self, combo, _p, cat, t):
        if self.mexendo:
            return
        i = combo.get_selected()
        apps = self.categorias[cat]["tipos"][t][1]
        if 0 < i <= len(apps):
            padroes.definir(t, apps[i - 1].get_id())
        self.atualizar_combos(cat)
        self.atualizar_resumo(cat)

    def ao_orbit_home(self, combo, _p):
        if self.mexendo:
            return
        i = combo.get_selected()
        if 0 <= i < len(ORBIT_HOME):
            visitas.gravar_modo(ORBIT_HOME[i][1])

    # ---------- busca ----------

    def ao_buscar(self, entrada):
        texto = entrada.get_text().strip().casefold()
        for r in self.linhas_resultado:
            self.g_resultados.remove(r)
        self.linhas_resultado = []
        self.g_resultados.set_visible(bool(texto))
        if not texto:
            return
        achados = []
        for cat, *_ in padroes.CATEGORIAS:
            for t in padroes.tipos(cat):
                if texto in t.casefold() or texto in padroes.descricao(t).casefold():
                    achados.append((cat, t))
        for cat, t in achados[:40]:
            apps = padroes.apps_do_tipo(t)
            c = Adw.ComboRow(title=padroes.descricao(t), subtitle=t,
                             model=Gtk.StringList.new(["None"] + [_nome(a) for a in apps]))
            p = padroes.padrao(t)
            pid = p.get_id() if p else None
            c.set_selected(next((i + 1 for i, a in enumerate(apps) if a.get_id() == pid), 0))
            c.connect("notify::selected", self.ao_resultado, cat, t, apps)
            self.g_resultados.add(c)
            self.linhas_resultado.append(c)
        if not achados:
            r = Adw.ActionRow(title="No file type matches")
            self.g_resultados.add(r)
            self.linhas_resultado.append(r)

    def ao_resultado(self, combo, _p, cat, t, apps):
        i = combo.get_selected()
        if 0 < i <= len(apps):
            padroes.definir(t, apps[i - 1].get_id())
            self.atualizar_resumo(cat)
            if self.categorias[cat]["montada"]:
                GLib.idle_add(self.atualizar_combos, cat)
