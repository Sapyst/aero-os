"""
sec_widgets.py — seção Widgets: os widgets da área de trabalho (doc 51).

Tudo grava em ~/.config/aero-shell/widgets.json e vale NA HORA: o daemon (~/.local/bin/widgets, código em
~/.local/share/aero-shell/widgets.py) vigia o arquivo. Aqui dá pra adicionar, remover, ligar/desligar, mudar
as opções de cada widget, ligar o modo "arrumar" (arrastar na área de trabalho), a escala e o movimento.
O botão direito na área de trabalho faz as mesmas coisas, no lugar.
"""
import json
import os

from gi.repository import Adw, Gio, GLib, Gtk

from comum import Secao, botao, combo, confirmar, indice, pagina

CONFIG = os.path.expanduser("~/.config/aero-shell/widgets.json")

# (tipo, nome, ícone, descrição) — os mesmos tipos registrados nos widget_*.py
CATALOGO = [
    ("relogio", "Clock", "alarm-symbolic", "Time and date"),
    ("sistema", "System", "computer-symbolic", "The PC at a glance, like fastfetch"),
    ("recursos", "Resources", "power-profile-balanced-symbolic", "CPU, memory, temperatures, disks, network, battery"),
    ("clima", "Weather", "weather-clear-symbolic", "Now and the next days (place from Settings → Location)"),
    ("processos", "Processes", "view-list-symbolic", "Live top: what is using the CPU and memory right now"),
]
# Opções de cada tipo: (chave, título, subtítulo, "combo" + lista (texto, valor) | "switch", valor padrão).
# Os padrões repetem os opcoes_padrao dos widget_*.py (o Settings não importa a camada deles).
OPCOES = {
    "relogio": [("estilo", "Style", "Minimal: thin digits · Segments: seven-segment display",
                 [("Minimal", "minimal"), ("Segments", "segmentos")], "minimal"),
                ("formato", "Hours", None, [("24-hour", "24h"), ("12-hour", "12h")], "24h"),
                ("segundos", "Show seconds", "Updates every second instead of every minute", "switch", False),
                ("data", "Show the date", None, "switch", True)],
    "sistema": [("pacotes", "Count packages", "pacman and flatpak", "switch", True),
                ("privado", "Private prompt", "Shows user@aero instead of your user and PC name (for screenshots)", "switch", False),
                ("tema", "Show the theme name", None, "switch", True)],
    "recursos": [("nucleos", "Per-core bars", None, "switch", True),
                 ("temperaturas", "Temperatures", "CPU, GPU and SSD sensors", "switch", True),
                 ("discos", "Disks", "/ and the mounted data disk", "switch", True),
                 ("rede", "Network", "Download and upload rates", "switch", True),
                 ("bateria", "Battery", None, "switch", True)],
    "processos": [("ordenar", "Sort by", None, [("CPU", "cpu"), ("Memory", "mem")], "cpu"),
                  ("linhas", "Rows", None, [("6", 6), ("8", 8), ("10", 10), ("12", 12)], 8)],
    "clima": [("privado", "Private", "Hide the place name and sunrise/sunset (for screenshots)", "switch", False),
              ("dias", "Days", None, [("5 days", 5), ("7 days", 7)], 7),
              ("detalhes", "Details", "Feels like, humidity, wind, sunrise and sunset", "switch", True)],
}


def carregar_extras():
    """Widgets de pacotes de terceiros: cada widget_*.py extra traz um widget_*.json ao lado, com o que
    o Settings precisa (tipo, nome, ícone, descrição e as opções no mesmo formato do OPCOES acima)."""
    pasta = os.path.expanduser("~/.local/share/aero-shell")
    try:
        nomes = sorted(f for f in os.listdir(pasta) if f.startswith("widget_") and f.endswith(".json"))
    except OSError:
        return
    for nome in nomes:
        try:
            with open(os.path.join(pasta, nome), encoding="utf-8") as f:
                d = json.load(f)
            if d["tipo"] in {t for t, *_ in CATALOGO}:
                continue
            CATALOGO.append((d["tipo"], d["nome"], d.get("icone", "applications-other-symbolic"), d.get("descricao", "")))
            OPCOES[d["tipo"]] = [tuple(o if not isinstance(o, list) else [tuple(x) for x in o] for o in op) for op in d.get("opcoes", [])]
        except (OSError, ValueError, KeyError, TypeError):
            continue


carregar_extras()
ESCALAS = [("Small", "0.85"), ("Normal", "1"), ("Large", "1.15"), ("Larger", "1.3")]
VIDROS = [("Terminal", "0.62"), ("Solid", "0.92"), ("Frosted", "0.75"), ("Light", "0.45"), ("Liquid", "0.30")]


def ler():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"versao": 1, "editar": False, "escala": "1", "menos_movimento": False, "widgets": []}


def gravar(d):
    os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
    with open(CONFIG + ".tmp", "w") as f:
        json.dump(d, f, indent=2)
    os.replace(CONFIG + ".tmp", CONFIG)


def nome_do(tipo):
    return next((n for t, n, _, _ in CATALOGO if t == tipo), tipo)


def icone_do(tipo):
    return next((i for t, _, i, _ in CATALOGO if t == tipo), "view-grid-symbolic")


class SecaoWidgets(Secao):
    id = "widgets"
    titulo = "Widgets"
    icone = "view-grid-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        self.linhas = []
        conteudo = Adw.PreferencesPage()

        self.lista = Adw.PreferencesGroup(
            title="On the desktop",
            description="Widgets sit between the wallpaper and the windows. Changes apply right away.")
        adicionar = Gtk.MenuButton(valign=Gtk.Align.CENTER)
        adicionar.set_child(Adw.ButtonContent(label="Add", icon_name="list-add-symbolic"))
        adicionar.add_css_class("flat")
        menu = Gio.Menu()
        for tipo, nome, _, _ in CATALOGO:
            item = Gio.MenuItem.new(nome, None)
            item.set_action_and_target_value("widgets.adicionar", GLib.Variant.new_string(tipo))
            menu.append_item(item)
        adicionar.set_menu_model(menu)
        grupo_acoes = Gio.SimpleActionGroup()
        acao = Gio.SimpleAction.new("adicionar", GLib.VariantType.new("s"))
        acao.connect("activate", lambda _a, p: self.adicionar(p.get_string()))
        grupo_acoes.add_action(acao)
        adicionar.insert_action_group("widgets", grupo_acoes)
        self.lista.set_header_suffix(adicionar)
        conteudo.add(self.lista)

        layout = Adw.PreferencesGroup(title="Layout")
        self.editar = Adw.SwitchRow(title="Arrange widgets",
                                    subtitle="Drag them on the desktop; click Done when you finish. "
                                             "Tip: right-click the desktop does this too.")
        self.escala = combo("Size", ESCALAS, 1, "Scale of every widget")
        self.vidro = combo("Surface", VIDROS, 0, "Terminal: the same glass as your windows. Liquid: see-through with a bright rim")
        self.movimento = Adw.SwitchRow(title="Reduce motion", subtitle="No sliding, shaking or breathing animations")
        for r in (self.editar, self.escala, self.vidro, self.movimento):
            layout.add(r)
        resetar = Adw.ActionRow(use_markup=False, title="Reset layout",
                                subtitle="Back to the default widgets and positions")
        resetar.add_suffix(botao("Reset", self.resetar, "destructive-action"))
        layout.add(resetar)
        conteudo.add(layout)

        self.editar.connect("notify::active", lambda *_: self.gravar_geral())
        self.escala.connect("notify::selected", lambda *_: self.gravar_geral())
        self.vidro.connect("notify::selected", lambda *_: self.gravar_geral())
        self.movimento.connect("notify::active", lambda *_: self.gravar_geral())
        self.pagina = pagina("Widgets", conteudo)

    # ---- mostrar ----
    def ao_mostrar(self):
        self.carregando = True
        d = ler()
        self.editar.set_active(bool(d.get("editar")))
        self.escala.set_selected(indice(ESCALAS, str(d.get("escala", "1")), 1))
        self.vidro.set_selected(indice(VIDROS, str(d.get("vidro", "0.62")), 0))
        self.movimento.set_active(bool(d.get("menos_movimento")))
        for linha in self.linhas:
            self.lista.remove(linha)
        self.linhas = []
        for reg in d.get("widgets") or []:
            linha = self.linha_widget(reg)
            self.lista.add(linha)
            self.linhas.append(linha)
        if not self.linhas:
            vazio = Adw.ActionRow(use_markup=False, title="No widgets", subtitle="Use Add, or right-click the desktop")
            self.lista.add(vazio)
            self.linhas.append(vazio)
        self.carregando = False

    def linha_widget(self, reg):
        tipo, wid = reg["tipo"], reg["id"]
        linha = Adw.ExpanderRow(use_markup=False, title=nome_do(tipo),
                                subtitle=f"at {reg.get('x', 0)}, {reg.get('y', 0)}" + ("" if reg.get("ativo", True) else " · hidden"))
        linha.add_prefix(Gtk.Image.new_from_icon_name(icone_do(tipo)))
        ligado = Gtk.Switch(valign=Gtk.Align.CENTER, active=reg.get("ativo", True), tooltip_text="Show on the desktop")
        ligado.connect("notify::active", lambda s, _p: self.mudar(wid, ativo=s.get_active()))
        linha.add_suffix(ligado)
        opcoes = dict(reg.get("opcoes") or {})
        for chave, titulo, sub, controle, padrao in OPCOES.get(tipo, []):
            valor = opcoes.get(chave, padrao)
            if controle == "switch":
                r = Adw.SwitchRow(title=titulo, subtitle=sub or "", active=bool(valor))
                r.connect("notify::active", lambda s, _p, k=chave: self.mudar_opcao(wid, k, s.get_active()))
            else:
                r = combo(titulo, controle, indice(controle, valor, 0), sub)
                r.connect("notify::selected", lambda s, _p, k=chave, ops=controle: self.mudar_opcao(wid, k, ops[s.get_selected()][1]))
            linha.add_row(r)
        remover = Adw.ActionRow(use_markup=False, title="Remove this widget")
        remover.add_suffix(botao("Remove", lambda: confirmar(self.janela, f"Remove {nome_do(tipo)}?",
                                                               "You can add it again later.", "Remove",
                                                               lambda: self.remover(wid), destrutivo=True),
                                 "destructive-action flat"))
        linha.add_row(remover)
        return linha

    # ---- gravar ----
    def gravar_geral(self):
        if self.carregando:
            return
        d = ler()
        d["editar"] = self.editar.get_active()
        d["escala"] = ESCALAS[self.escala.get_selected()][1]
        d["vidro"] = VIDROS[self.vidro.get_selected()][1]
        d["menos_movimento"] = self.movimento.get_active()
        gravar(d)

    def mudar(self, wid, **campos):
        if self.carregando:
            return
        d = ler()
        for w in d.get("widgets") or []:
            if w["id"] == wid:
                w.update(campos)
        gravar(d)

    def mudar_opcao(self, wid, chave, valor):
        if self.carregando:
            return
        d = ler()
        for w in d.get("widgets") or []:
            if w["id"] == wid:
                w.setdefault("opcoes", {})[chave] = valor
        gravar(d)

    def adicionar(self, tipo):
        d = ler()
        lista = d.setdefault("widgets", []) or []
        n = 1
        while any(w["id"] == f"{tipo}-{n}" for w in lista):
            n += 1
        quantos = sum(1 for w in lista if w["tipo"] == tipo)
        lista.append({"id": f"{tipo}-{n}", "tipo": tipo, "ativo": True, "opcoes": {},
                      "x": 24 + 40 * quantos, "y": 66 + 40 * quantos})
        d["widgets"] = lista
        gravar(d)
        self.janela.avisar(f"{nome_do(tipo)} added — turn on Arrange widgets to move it")
        self.ao_mostrar()

    def remover(self, wid):
        d = ler()
        d["widgets"] = [w for w in (d.get("widgets") or []) if w["id"] != wid]
        gravar(d)
        self.ao_mostrar()

    def resetar(self):
        def fazer():
            d = ler()
            d["widgets"] = None            # o daemon recria o layout padrão
            d["editar"] = False
            gravar(d)
            GLib.timeout_add(600, lambda: (self.ao_mostrar(), False)[1])
        confirmar(self.janela, "Reset the widget layout?", "Default widgets and positions will be restored.",
                  "Reset", fazer, destrutivo=True)
