"""
sec_notificacoes.py — seção Notifications: tudo do Hermes (o daemon de notificações do Aero, shell/hermes.py).

Grava ~/.config/aero/hermes.json (arquivo do usuário, sem senha) e manda o Hermes reler com
`hermesctl reload`. As regras de validação são as do próprio Hermes (hermes.validar_config): o que
esta página grava é o que ele aceita. Quem quiser pode editar o arquivo à mão também.
"""
import json
import os
import subprocess
import sys

from gi.repository import Adw, Gdk, GLib, Gtk

from comum import Secao, pagina

sys.path.insert(0, os.path.expanduser("~/.local/share/aero-shell"))
try:
    import hermes as H
except ImportError:                       # Hermes not installed: the page explains it
    H = None

CTL = os.path.expanduser("~/.local/bin/hermesctl")
POSICOES = [("Under the notification bell", "follow-bell"), ("Next to the quick center", "follow-central"), ("Top left", "top-left"), ("Top center", "top-center"),
            ("Top right", "top-right"), ("Bottom left", "bottom-left"), ("Bottom center", "bottom-center"),
            ("Bottom right", "bottom-right")]
ESTILOS = [("DotStyle", "dotstyle"), ("AeroTerm", "aeroterm"), ("AeroCloud", "aerocloud")]
ORIGENS = [("Where the card appears", "notification"), ("Center of the screen", "screen-center")]
SOBRE_CHEIA = [("Faded", "faded"), ("Normal", "normal"), ("Off", "off")]
CARTAO_CHEIA = [("Hold until I leave fullscreen", "hold"), ("Show anyway", "show")]
MOVIMENTO = [("Follow the system", "auto"), ("Always reduced", "on"), ("Never reduced", "off")]
STEAM = [("Wave where they appear", "wave"), ("Move them next to the quick center", "move"),
         ("Leave them alone", "off")]


def ler_bruto():
    try:
        with open(H.CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def gravar_bruto(d):
    os.makedirs(os.path.dirname(H.CONFIG), exist_ok=True)
    tmp = H.CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)
    os.replace(tmp, H.CONFIG)


def quem_cuida():
    """'hermes', 'mako' or ''."""
    r = subprocess.run(["busctl", "--user", "status", "com.aero.Hermes"], capture_output=True)
    if r.returncode == 0:
        return "hermes"
    r = subprocess.run(["busctl", "--user", "status", "org.freedesktop.Notifications"], capture_output=True,
                       text=True)
    return "mako" if r.returncode == 0 else ""


def hex_de(rgba):
    return "#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in (rgba.red, rgba.green, rgba.blue)))


class SecaoNotificacoes(Secao):
    id = "notificacoes"
    titulo = "Notifications"
    icone = "preferences-system-notifications-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = True
        self.bruto = {}
        self.salvar_id = 0
        conteudo = Adw.PreferencesPage()
        self.pagina = pagina(self.titulo, conteudo)
        if H is None:
            g = Adw.PreferencesGroup(title="Hermes is not installed",
                                     description="Copy shell/hermes.py and hermes_ui.py to ~/.local/share/aero-shell.")
            conteudo.add(g)
            return

        # --- status + tests ---
        self.status = Adw.PreferencesGroup(title="Hermes")
        self.linha_status = Adw.ActionRow(title="Notifications")
        teste = Gtk.Button(label="Test", valign=Gtk.Align.CENTER)
        teste.connect("clicked", lambda *_: self.testar(False))
        urgente = Gtk.Button(label="Urgent test", valign=Gtk.Align.CENTER)
        urgente.add_css_class("destructive-action")
        urgente.connect("clicked", lambda *_: self.testar(True))
        self.linha_status.add_suffix(teste)
        self.linha_status.add_suffix(urgente)
        self.status.add(self.linha_status)
        conteudo.add(self.status)

        # --- cards ---
        cartoes = Adw.PreferencesGroup(title="Cards")
        self.estilo = self.combo("Style", ESTILOS, ("style",))
        self.posicao = self.combo("Position", POSICOES, ("position",))
        self.largura = self.spin("Width", 200, 900, 10, ("width",), "px")
        self.maximo = self.spin("Cards on screen at once", 1, 12, 1, ("max_visible",))
        self.margem = self.spin("Distance from the edge", 0, 200, 2, ("margin",), "px")
        self.icones = self.switch("Show icons and pictures", ("icons",))
        self.t_normal = self.spin("Normal ones stay for", 0, 60, 1, ("timeout", "normal"), "s", fator=1000)
        self.t_baixa = self.spin("Low ones stay for", 0, 60, 1, ("timeout", "low"), "s", fator=1000)
        self.t_critica = self.spin("Urgent ones stay for", 0, 600, 5, ("timeout", "critical"), "s (0 = until closed)",
                                   fator=1000)
        for w in (self.estilo, self.posicao, self.largura, self.maximo, self.margem, self.icones, self.t_normal,
                  self.t_baixa, self.t_critica):
            cartoes.add(w)
        conteudo.add(cartoes)

        # --- wave ---
        onda = Adw.PreferencesGroup(title="Wave", description="Dots that ripple across the screen when "
                                    "something arrives. They never take a click or the keyboard.")
        self.o_ligada = self.switch("Wave", ("wave", "enabled"))
        self.o_origem = self.combo("Starts from", ORIGENS, ("wave", "origin"))
        self.o_cor = self.cor("Color", ("wave", "color"), automatico=True)
        self.o_urgente = self.cor("Urgent color", ("wave", "urgent_color"))
        self.o_forca = self.escala("Strength", 0.05, 1.0, ("wave", "intensity"))
        self.o_alcance = self.escala("Reach", 0.1, 1.5, ("wave", "reach"))
        self.o_duracao = self.spin("Duration", 200, 6000, 100, ("wave", "duration_ms"), "ms")
        self.o_espaco = self.spin("Space between dots", 8, 80, 1, ("wave", "dot_spacing"), "px")
        self.o_tamanho = self.escala("Dot size", 0.05, 0.5, ("wave", "dot_size"))
        self.o_movimento = self.combo("Reduced motion", MOVIMENTO, ("wave", "reduced_motion"))
        for w in (self.o_ligada, self.o_origem, self.o_cor, self.o_urgente, self.o_forca, self.o_alcance,
                  self.o_duracao, self.o_espaco, self.o_tamanho, self.o_movimento):
            onda.add(w)
        conteudo.add(onda)

        # --- fullscreen ---
        cheia = Adw.PreferencesGroup(title="While a game or video is fullscreen")
        self.c_onda = self.combo("Wave over it", SOBRE_CHEIA, ("wave", "fullscreen"))
        self.c_forca = self.escala("Faded strength", 0.02, 1.0, ("wave", "fullscreen_intensity"))
        self.c_cartao = self.combo("Cards", CARTAO_CHEIA, ("fullscreen",))
        for w in (self.c_onda, self.c_forca, self.c_cartao):
            cheia.add(w)
        conteudo.add(cheia)

        # --- do not disturb + steam ---
        quieto = Adw.PreferencesGroup(title="Quiet")
        self.dnd = Adw.SwitchRow(title="Do not disturb", subtitle="Cards and waves stop; everything still goes "
                                 "to the history")
        self.dnd.connect("notify::active", self.ao_dnd)
        self.dnd_critica = self.switch("Let urgent ones through", ("dnd", "allow_critical"))
        self.steam = self.combo("Steam's own popups", STEAM, ("steam",))
        for w in (self.dnd, self.dnd_critica, self.steam):
            quieto.add(w)
        conteudo.add(quieto)

        # --- rules ---
        self.grupo_regras = Adw.PreferencesGroup(
            title="Rules per app", description="Change how one app's notifications behave. "
                                               "Names match as text, ignoring upper and lower case.")
        nova = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Add a rule", valign=Gtk.Align.CENTER)
        nova.add_css_class("flat")
        nova.connect("clicked", lambda *_: self.dialogo_regra())
        self.grupo_regras.set_header_suffix(nova)
        self.linhas_regras = []
        conteudo.add(self.grupo_regras)

        # --- advanced ---
        avancado = Adw.PreferencesGroup(title="Advanced")
        arquivo = Adw.ActionRow(title="Settings file", subtitle=H.CONFIG.replace(os.path.expanduser("~"), "~"),
                                activatable=True)
        arquivo.add_suffix(Gtk.Image(icon_name="document-open-symbolic"))
        arquivo.connect("activated", lambda *_: subprocess.Popen(["xdg-open", H.CONFIG]) if os.path.exists(H.CONFIG)
                        else self.janela.avisar("Change something first: the file is created on the first change"))
        padrao = Adw.ActionRow(title="Back to defaults", subtitle="DotStyle, next to the quick center", activatable=True)
        padrao.add_suffix(Gtk.Image(icon_name="edit-undo-symbolic"))
        padrao.connect("activated", lambda *_: self.restaurar())
        avancado.add(arquivo)
        avancado.add(padrao)
        conteudo.add(avancado)
        self.carregando = False

    # ---------- widgets bound to a key path ----------

    def _valor(self, caminho, cfg=None):
        v = cfg or self.cfg
        for k in caminho:
            v = v[k]
        return v

    def _mudar(self, caminho, valor):
        if self.carregando:
            return
        d = self.bruto
        for k in caminho[:-1]:
            if not isinstance(d.get(k), dict):
                d[k] = {}
            d = d[k]
        d[caminho[-1]] = valor
        self.agendar_salvar()

    def combo(self, titulo, opcoes, caminho):
        linha = Adw.ComboRow(title=titulo, model=Gtk.StringList.new([t for t, _ in opcoes]))
        linha.opcoes, linha.caminho = opcoes, caminho
        linha.connect("notify::selected", lambda l, _p: self._mudar(caminho, opcoes[l.get_selected()][1]))
        return linha

    def switch(self, titulo, caminho):
        linha = Adw.SwitchRow(title=titulo)
        linha.caminho = caminho
        linha.connect("notify::active", lambda l, _p: self._mudar(caminho, l.get_active()))
        return linha

    def spin(self, titulo, minimo, maximo, passo, caminho, unidade="", fator=1):
        linha = Adw.SpinRow.new_with_range(minimo, maximo, passo)
        linha.set_title(titulo)
        if unidade:
            linha.set_subtitle(unidade)
        linha.caminho, linha.fator = caminho, fator
        linha.connect("notify::value", lambda l, _p: self._mudar(caminho, int(round(l.get_value() * fator))))
        return linha

    def escala(self, titulo, minimo, maximo, caminho):
        linha = Adw.ActionRow(title=titulo)
        ajuste = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimo, maximo, 0.01)
        ajuste.set_size_request(220, -1)
        ajuste.set_draw_value(False)
        ajuste.set_valign(Gtk.Align.CENTER)
        ajuste.connect("value-changed", lambda e: self._mudar(caminho, round(e.get_value(), 2)))
        linha.add_suffix(ajuste)
        linha.escala, linha.caminho = ajuste, caminho
        return linha

    def cor(self, titulo, caminho, automatico=False):
        linha = Adw.ActionRow(title=titulo)
        botao = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        botao.connect("notify::rgba", lambda b, _p: self._mudar(caminho, hex_de(b.get_rgba())))
        linha.botao, linha.caminho, linha.auto = botao, caminho, None
        if automatico:
            auto = Gtk.CheckButton(label="Theme", valign=Gtk.Align.CENTER)

            def trocar(c):
                botao.set_sensitive(not c.get_active())
                self._mudar(caminho, "auto" if c.get_active() else hex_de(botao.get_rgba()))
            auto.connect("toggled", trocar)
            linha.add_suffix(auto)
            linha.auto = auto
        linha.add_suffix(botao)
        return linha

    # ---------- load / save ----------

    def ao_mostrar(self):
        if H is None:
            return
        self.carregando = True
        self.bruto = ler_bruto()
        self.cfg = H.validar_config(self.bruto)
        quem = quem_cuida()
        self.linha_status.set_subtitle({"hermes": "Hermes is showing your notifications",
                                        "mako": "mako is showing them now (Hermes takes over when it's switched on)"
                                        }.get(quem, "No notification service is running"))
        for w in (self.estilo, self.posicao, self.o_origem, self.o_movimento, self.c_onda, self.c_cartao, self.steam):
            atual = self._valor(w.caminho)
            w.set_selected(next((i for i, (_, v) in enumerate(w.opcoes) if v == atual), 0))
        for w in (self.icones, self.o_ligada, self.dnd_critica):
            w.set_active(bool(self._valor(w.caminho)))
        for w in (self.largura, self.maximo, self.margem, self.t_normal, self.t_baixa, self.t_critica, self.o_duracao,
                  self.o_espaco):
            w.set_value(self._valor(w.caminho) / w.fator)
        for w in (self.o_forca, self.o_alcance, self.o_tamanho, self.c_forca):
            w.escala.set_value(self._valor(w.caminho))
        for w in (self.o_cor, self.o_urgente):
            v = self._valor(w.caminho)
            cor = Gdk.RGBA()
            cor.parse(self.cfg["colors"]["text"] if v == "auto" else v)
            w.botao.set_rgba(cor)
            if w.auto is not None:
                w.auto.set_active(v == "auto")
                w.botao.set_sensitive(v != "auto")
        modos = subprocess.run([CTL, "mode"], capture_output=True, text=True).stdout.split() \
            if os.access(CTL, os.X_OK) else []
        self.dnd.set_active("dnd" in modos)
        self.montar_regras()
        self.carregando = False

    def agendar_salvar(self):
        if self.salvar_id:
            GLib.source_remove(self.salvar_id)
        self.salvar_id = GLib.timeout_add(350, self.salvar)

    def salvar(self):
        self.salvar_id = 0
        self.cfg = H.validar_config(self.bruto)
        gravar_bruto(self.bruto)
        if os.access(CTL, os.X_OK):
            subprocess.Popen([CTL, "reload"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return GLib.SOURCE_REMOVE

    def restaurar(self):
        self.bruto = {}
        gravar_bruto({})
        self.salvar()
        self.ao_mostrar()
        self.janela.avisar("Notifications are back to the defaults")

    def testar(self, urgente):
        args = ["notify-send", "-a", "Settings", "-i", "preferences-system-notifications-symbolic"]
        if urgente:
            args += ["-u", "critical", "Urgent test", "This one stays until you close it."]
        else:
            args += ["Test notification", "This is how your notifications look now."]
        subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def ao_dnd(self, linha, _p):
        if not self.carregando and os.access(CTL, os.X_OK):
            subprocess.Popen([CTL, "mode", "-a" if linha.get_active() else "-r", "dnd"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ---------- rules ----------

    def montar_regras(self):
        for linha in self.linhas_regras:
            self.grupo_regras.remove(linha)
        self.linhas_regras = []
        regras = self.bruto.get("rules") if isinstance(self.bruto.get("rules"), list) else []
        if not regras:
            vazio = Adw.ActionRow(title="No rules", subtitle="Example: Steam with no wave and 2 seconds on screen")
            self.grupo_regras.add(vazio)
            self.linhas_regras.append(vazio)
            return
        for i, r in enumerate(regras):
            m, s = r.get("match", {}), r.get("set", {})
            quem = " · ".join(f"{k} “{v}”" for k, v in m.items()) or "Any app"
            faz = []
            if s.get("hide"):
                faz.append("hidden (history only)")
            if s.get("wave") is False:
                faz.append("no wave")
            if "timeout" in s:
                faz.append(f"{s['timeout'] / 1000:g} s on screen")
            if s.get("history") is False:
                faz.append("not kept in history")
            if "urgency" in s:
                faz.append(f"treated as {s['urgency']}")
            valida = H.validar_regra(r) is not None
            linha = Adw.ActionRow(title=quem, subtitle=", ".join(faz) + ("" if valida else "  (invalid, ignored)"))
            linha.set_use_markup(False)
            apagar = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove rule", valign=Gtk.Align.CENTER)
            apagar.add_css_class("flat")
            apagar.connect("clicked", lambda *_, i=i: self.apagar_regra(i))
            linha.add_suffix(apagar)
            self.grupo_regras.add(linha)
            self.linhas_regras.append(linha)

    def apagar_regra(self, i):
        regras = list(self.bruto.get("rules") or [])
        if 0 <= i < len(regras):
            regras.pop(i)
            self.bruto["rules"] = regras
            self.salvar()
            self.montar_regras()

    def dialogo_regra(self):
        d = Adw.Dialog(title="New rule", content_width=440)
        vista = Adw.ToolbarView()
        vista.add_top_bar(Adw.HeaderBar())
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                        margin_bottom=18)
        quem = Adw.PreferencesGroup(title="Which notifications")
        app = Adw.EntryRow(title="App name contains (for example: steam)")
        texto = Adw.EntryRow(title="Title contains (optional)")
        quem.add(app)
        quem.add(texto)
        o_que = Adw.PreferencesGroup(title="What to do")
        esconder = Adw.SwitchRow(title="Hide (history only)")
        sem_onda = Adw.SwitchRow(title="No wave")
        sem_hist = Adw.SwitchRow(title="Don't keep in history")
        tempo = Adw.SpinRow.new_with_range(0, 600, 1)
        tempo.set_title("Seconds on screen")
        tempo.set_subtitle("0 = keep the default")
        for w in (esconder, sem_onda, sem_hist, tempo):
            o_que.add(w)
        aviso = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        salvar = Gtk.Button(label="Add rule", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)

        def ok(*_):
            import re
            m = {}
            if app.get_text().strip():
                m["app"] = re.escape(app.get_text().strip())
            if texto.get_text().strip():
                m["summary"] = re.escape(texto.get_text().strip())
            s = {}
            if esconder.get_active():
                s["hide"] = True
            if sem_onda.get_active():
                s["wave"] = False
            if sem_hist.get_active():
                s["history"] = False
            if tempo.get_value() > 0:
                s["timeout"] = int(tempo.get_value() * 1000)
            regra = {"match": m, "set": s}
            if not m or not s or H.validar_regra(regra) is None:
                aviso.set_label("Fill the app name or the title, and pick at least one thing to do")
                aviso.set_visible(True)
                return
            self.bruto["rules"] = list(self.bruto.get("rules") or []) + [regra]
            self.salvar()
            self.montar_regras()
            d.close()
        salvar.connect("clicked", ok)
        for w in (quem, o_que, aviso, salvar):
            caixa.append(w)
        vista.set_content(caixa)
        d.set_child(vista)
        d.present(self.janela)
