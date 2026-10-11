"""
sec_local.py — seção Location: onde você está (pro clima do Super Menu) e a unidade da temperatura (doc 37).

- Automatic: o lugar vem da sua conexão de internet (ipinfo.io), sem GPS.
- Ou você procura uma cidade (busca de cidades do Open-Meteo) e escolhe na lista.
O clima em si vem do Open-Meteo (grátis, sem cadastro). Quem busca e desenha é o
~/.local/share/aero-shell/clima.py — o mesmo que o Super Menu usa.

painel.conf: CLIMA_AUTO=1|0  CLIMA_LAT  CLIMA_LON  CLIMA_NOME="..."  CLIMA_UNIDADE=c|f
"""
import os
import sys

from gi.repository import Adw, GLib, Gtk

from comum import Secao, atualizar_painel, em_segundo_plano, ler_painel, pagina

sys.path.insert(0, os.path.expanduser("~/.local/share/aero-shell"))
import clima  # noqa: E402

UNIDADES = [("Celsius (°C)", "c"), ("Fahrenheit (°F)", "f")]


class SecaoLocal(Secao):
    id = "local"
    titulo = "Location"
    icone = "find-location-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.mexendo = False
        conteudo = Adw.PreferencesPage()

        # ---- o clima de agora (prévia, com o mesmo desenho do Super Menu) ----
        g_agora = Adw.PreferencesGroup(title="Weather now", description="Shown at the top of the Super Menu")
        conteudo.add(g_agora)
        self.agora = Adw.ActionRow(title="Loading…")
        self.desenho = Gtk.DrawingArea(content_width=48, content_height=48, valign=Gtk.Align.CENTER)
        self.desenho.set_draw_func(self.desenhar)
        self.clima = None
        self.agora.add_prefix(self.desenho)
        atualizar = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Refresh", valign=Gtk.Align.CENTER)
        atualizar.add_css_class("flat")
        atualizar.connect("clicked", lambda *_: self.buscar_clima(forcar=True))
        self.agora.add_suffix(atualizar)
        g_agora.add(self.agora)
        self.unidade = Adw.ComboRow(title="Temperature unit", model=Gtk.StringList.new([t for t, _ in UNIDADES]))
        self.unidade.connect("notify::selected", self.ao_unidade)
        g_agora.add(self.unidade)

        # ---- onde ----
        g_onde = Adw.PreferencesGroup(title="Location")
        conteudo.add(g_onde)
        self.auto = Adw.SwitchRow(title="Automatic", subtitle="Based on your internet connection")
        self.auto.connect("notify::active", self.ao_auto)
        g_onde.add(self.auto)
        self.busca = Adw.EntryRow(title="Search for a city", show_apply_button=True)
        self.busca.connect("apply", self.ao_buscar)
        self.busca.connect("entry-activated", self.ao_buscar)
        g_onde.add(self.busca)
        self.g_resultados = Adw.PreferencesGroup()
        conteudo.add(self.g_resultados)
        self.linhas_resultado = []

        self.pagina = pagina(self.titulo, conteudo)

    # ---------- mostrar ----------

    def ao_mostrar(self):
        p = ler_painel()
        self.mexendo = True
        self.auto.set_active(p.get("CLIMA_AUTO", "1") == "1")
        self.unidade.set_selected(1 if p.get("CLIMA_UNIDADE", "c") == "f" else 0)
        self.mexendo = False
        self.busca.set_visible(not self.auto.get_active())
        self.mostrar_lugar(p)
        self.mostrar_clima(clima.ler_cache().get("clima"))
        self.buscar_clima()

    def mostrar_lugar(self, p):
        nome = p.get("CLIMA_NOME", "").strip('"')
        if self.auto.get_active():
            self.auto.set_subtitle("Based on your internet connection")
        else:
            self.auto.set_subtitle(f"Using: {nome}" if nome else "Search for your city below")

    def desenhar(self, _area, cr, w, h):
        if self.clima:
            clima.desenhar(cr, min(w, h), self.clima.get("desenho", "nublado"), self.clima.get("dia", True))

    def mostrar_clima(self, c):
        if isinstance(c, Exception):
            c = None
        self.clima = c
        if c:
            self.agora.set_title(f"{c['temp']}°  ·  {c['texto']}")
            partes = [f"Feels like {c['sensacao']}°", f"H {c['max']}°  L {c['min']}°"]
            if c.get("lugar"):
                partes.append(c["lugar"])
            self.agora.set_subtitle("  ·  ".join(partes))
        else:
            self.agora.set_title("Weather unavailable")
            self.agora.set_subtitle("Check your internet connection")
        self.desenho.queue_draw()

    def buscar_clima(self, forcar=False):
        em_segundo_plano(lambda: clima.obter(forcar), self.mostrar_clima)

    # ---------- mudanças ----------

    def ao_unidade(self, *_):
        if self.mexendo:
            return
        atualizar_painel(CLIMA_UNIDADE=UNIDADES[self.unidade.get_selected()][1])
        self.buscar_clima(forcar=True)

    def ao_auto(self, *_):
        self.busca.set_visible(not self.auto.get_active())
        if self.mexendo:
            return
        p = ler_painel()
        if not self.auto.get_active() and not p.get("CLIMA_LAT"):
            # ainda não escolheu cidade: fica no automático até escolher uma da lista
            atualizar_painel(CLIMA_AUTO="1")
            self.busca.grab_focus()
        else:
            atualizar_painel(CLIMA_AUTO="1" if self.auto.get_active() else "0")
            self.buscar_clima(forcar=True)
        self.mostrar_lugar(ler_painel())

    def ao_buscar(self, *_):
        texto = self.busca.get_text().strip()
        if len(texto) < 2:
            return
        for linha in self.linhas_resultado:
            self.g_resultados.remove(linha)
        self.linhas_resultado = []
        self.g_resultados.set_title("Searching…")

        def depois(r):
            if isinstance(r, Exception) or not r:
                self.g_resultados.set_title("No cities found" if not isinstance(r, Exception)
                                            else "Couldn't search (no internet?)")
                return
            self.g_resultados.set_title("Pick your city")
            for nome, lat, lon in r:
                linha = Adw.ActionRow(title=nome, activatable=True)
                linha.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
                linha.connect("activated", lambda *_, n=nome, a=lat, o=lon: self.escolher(n, a, o))
                self.g_resultados.add(linha)
                self.linhas_resultado.append(linha)
        em_segundo_plano(lambda: clima.buscar_cidades(texto), depois)

    def escolher(self, nome, lat, lon):
        atualizar_painel(CLIMA_AUTO="0", CLIMA_LAT=f"{lat:.4f}", CLIMA_LON=f"{lon:.4f}",
                         CLIMA_NOME=f'"{clima.limpo(nome)}"')     # entre aspas: o bash lê o painel.conf
        for linha in self.linhas_resultado:
            self.g_resultados.remove(linha)
        self.linhas_resultado = []
        self.g_resultados.set_title("")
        self.busca.set_text("")
        self.mexendo = True
        self.auto.set_active(False)
        self.mexendo = False
        self.mostrar_lugar(ler_painel())
        self.janela.avisar(f"Location set: {nome}")
        self.buscar_clima(forcar=True)
