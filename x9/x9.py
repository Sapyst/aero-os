#!/usr/bin/env python3
"""
x9.py — X9, the admin activity log of Aero OS.

Shows every sudo / pkexec / su call that the root service x9d archived (one file per day in
/var/log/x9), with filters that combine: days, time of day, category, result, risk, app and text.
Entries can be deleted one by one, several at once or all; auto-delete rules keep the archive small.

The app itself is NOT root. It only reads the archive (group wheel). Everything that changes the
archive goes through pkexec + x9-admin, and polkit asks the admin password every time.

The X9 password is a lock for the screen (someone at your keyboard), not encryption: a program
running as you could read the archive files directly. What protects the logs is that only root can
change them.
"""
import json
import os
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

for pasta in ("/usr/local/lib/x9", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "system", "x9")):
    if os.path.isfile(os.path.join(pasta, "x9core.py")):
        sys.path.insert(0, pasta)
        break
import x9core as x  # noqa: E402

PKEXEC = "/usr/bin/pkexec"
ADMIN = "/usr/local/lib/x9/x9-admin"
SETLOCK = "/usr/local/lib/x9/x9-setlock"
MAX_LINHAS = 1500                      # rows drawn at once; the count label says when more matched
RISCO_ICONE = {"high": "dialog-warning-symbolic", "medium": "dialog-information-symbolic",
               "low": "emblem-ok-symbolic"}
CSS = """
.x9-pill { padding: 1px 8px; border-radius: 99px; font-size: 0.85em; }
.x9-allowed { background: alpha(@success_color, 0.15); color: @success_color; }
.x9-bad { background: alpha(@error_color, 0.15); color: @error_color; }
.x9-neutral { background: alpha(@window_fg_color, 0.08); }
.x9-risk-high { color: @error_color; }
.x9-risk-medium { color: @warning_color; }
.x9-risk-low { color: alpha(@window_fg_color, 0.45); }
.x9-mono { font-family: monospace; font-size: 0.9em; }
.x9-day { font-weight: bold; padding: 14px 12px 4px 12px; color: alpha(@window_fg_color, 0.7); }
"""


def rodar_admin(programa, acao, entrada):
    """pkexec + root helper; the text goes on stdin (never in argv, never in a file)."""
    cmd = [PKEXEC, programa] + ([acao] if acao else [])
    return subprocess.run(cmd, input=entrada, capture_output=True, text=True, timeout=600)


def em_thread(funcao, depois):
    import threading

    def tarefa():
        try:
            r = funcao()
        except Exception as erro:  # noqa: BLE001
            r = erro
        GLib.idle_add(depois, r)
    threading.Thread(target=tarefa, daemon=True).start()


def falha(r):
    """Short reason a root call failed, or '' if it worked."""
    if isinstance(r, Exception):
        return str(r)
    if r.returncode == 0:
        return ""
    if r.returncode == 126:
        return "Password cancelled"
    if r.returncode == 127:
        return "Not authorized"
    ultima = [linha for linha in (r.stderr or "").splitlines() if linha.strip()]
    return (ultima[-1] if ultima else f"Error {r.returncode}").replace("x9-admin: ", "").replace("x9-setlock: ", "")


def data_txt(ts, fmt="%H:%M:%S"):
    return time.strftime(fmt, time.localtime(ts))


def lista_suspensa(opcoes, ao_mudar=None):
    """opcoes = [(text, value)]"""
    d = Gtk.DropDown(model=Gtk.StringList.new([t for t, _ in opcoes]))
    d.opcoes = opcoes
    if ao_mudar:
        d.connect("notify::selected", lambda *_: ao_mudar())
    return d


def valor(d):
    i = d.get_selected()
    return d.opcoes[i][1] if 0 <= i < len(d.opcoes) else None


def pill(texto, classe):
    rotulo = Gtk.Label(label=texto, valign=Gtk.Align.CENTER)
    rotulo.add_css_class("x9-pill")
    rotulo.add_css_class(classe)
    return rotulo


# ---------- lock screen ----------

class Trava(Gtk.Box):
    def __init__(self, janela):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.janela = janela
        self.uid = os.getuid()
        pagina = Adw.StatusPage(icon_name="security-high-symbolic", title="X9", vexpand=True)
        self.append(pagina)
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER, width_request=360)
        pagina.set_child(caixa)
        self.grupo = Adw.PreferencesGroup()
        caixa.append(self.grupo)
        self.senha = Adw.PasswordEntryRow(title="Password")
        self.confirma = Adw.PasswordEntryRow(title="Repeat password")
        self.grupo.add(self.senha)
        self.grupo.add(self.confirma)
        self.senha.connect("entry-activated", lambda *_: self.entrar())
        self.confirma.connect("entry-activated", lambda *_: self.entrar())
        self.erro = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        caixa.append(self.erro)
        self.botao = Gtk.Button(css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.botao.connect("clicked", lambda *_: self.entrar())
        caixa.append(self.botao)
        self.esqueci = Gtk.Button(label="Forgot password?", css_classes=["flat"], halign=Gtk.Align.CENTER)
        self.esqueci.connect("clicked", lambda *_: self.esqueceu())
        caixa.append(self.esqueci)
        self.pagina = pagina
        self.mostrar()

    def mostrar(self):
        self.registro = x.ler_trava(self.uid)
        primeira = self.registro is None
        self.pagina.set_description("Create a password for X9. It locks this log on your screen."
                                    if primeira else "Admin activity log. Type your X9 password.")
        self.confirma.set_visible(primeira)
        self.esqueci.set_visible(not primeira)
        self.botao.set_label("Create password" if primeira else "Unlock")
        self.senha.set_text("")
        self.confirma.set_text("")
        self.erro.set_visible(False)
        self.senha.grab_focus()

    def avisar(self, texto):
        self.erro.set_label(texto)
        self.erro.set_visible(True)

    def entrar(self):
        s = self.senha.get_text()
        if self.registro is not None:
            if x.confere_senha(s, self.registro):
                self.janela.destravar()
            else:
                self.avisar("Wrong password")
                self.senha.set_text("")
            return
        problema = x.senha_boa(s)
        if problema:
            return self.avisar(problema)
        if s != self.confirma.get_text():
            return self.avisar("The passwords are different")
        self.botao.set_sensitive(False)
        em_thread(lambda: rodar_admin(SETLOCK, None, json.dumps({"old": None, "new": s})), self.depois_criar)

    def depois_criar(self, r):
        self.botao.set_sensitive(True)
        motivo = falha(r)
        if motivo:
            return self.avisar(f"Not saved: {motivo}")
        self.janela.destravar()

    def esqueceu(self):
        dialogo = DialogoSenha(self.janela, "Reset X9 password",
                               "Pick a new X9 password. Then your admin (sudo) password is asked to confirm it.",
                               pedir_antiga=False, ao_salvar=self.resetar)
        dialogo.present(self.janela)

    def resetar(self, nova, dialogo):
        em_thread(lambda: rodar_admin(ADMIN, "reset-lock", nova), lambda r: self.depois_resetar(r, dialogo))

    def depois_resetar(self, r, dialogo):
        motivo = falha(r)
        if motivo:
            dialogo.erro(f"Not changed: {motivo}")
            return
        dialogo.close()
        self.janela.destravar()


class DialogoSenha(Adw.Dialog):
    def __init__(self, janela, titulo, texto, pedir_antiga, ao_salvar):
        super().__init__(title=titulo, content_width=420)
        self.ao_salvar = ao_salvar
        vista = Adw.ToolbarView()
        vista.add_top_bar(Adw.HeaderBar())
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_start=18, margin_end=18,
                        margin_bottom=18, margin_top=6)
        caixa.append(Gtk.Label(label=texto, wrap=True, xalign=0, css_classes=["dim-label"]))
        grupo = Adw.PreferencesGroup()
        self.antiga = Adw.PasswordEntryRow(title="Current X9 password", visible=pedir_antiga)
        self.nova = Adw.PasswordEntryRow(title="New password")
        self.repete = Adw.PasswordEntryRow(title="Repeat new password")
        for linha in (self.antiga, self.nova, self.repete):
            grupo.add(linha)
        caixa.append(grupo)
        self.aviso = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        caixa.append(self.aviso)
        self.salvar = Gtk.Button(label="Save", css_classes=["suggested-action", "pill"], halign=Gtk.Align.CENTER)
        self.salvar.connect("clicked", lambda *_: self.ok())
        caixa.append(self.salvar)
        vista.set_content(caixa)
        self.set_child(vista)

    def erro(self, texto):
        self.aviso.set_label(texto)
        self.aviso.set_visible(True)
        self.salvar.set_sensitive(True)

    def ok(self):
        nova = self.nova.get_text()
        problema = x.senha_boa(nova)
        if problema:
            return self.erro(problema)
        if nova != self.repete.get_text():
            return self.erro("The passwords are different")
        self.salvar.set_sensitive(False)
        if self.antiga.get_visible():
            self.ao_salvar(self.antiga.get_text(), nova, self)
        else:
            self.ao_salvar(nova, self)


# ---------- auto-delete settings ----------

class DialogoRetencao(Adw.Dialog):
    GUARDAR = [("Forever", 0), ("1 day", 1), ("7 days", 7), ("30 days", 30), ("90 days", 90),
               ("180 days", 180), ("1 year", 365), ("2 years", 730)]

    def __init__(self, janela):
        super().__init__(title="Auto-delete", content_width=640, content_height=560)
        self.janela = janela
        atual = x.ler_retencao()
        vista = Adw.ToolbarView()
        vista.add_top_bar(Adw.HeaderBar())
        pagina = Adw.PreferencesPage()
        geral = Adw.PreferencesGroup(title="Keep logs",
                                     description="Older entries are deleted once an hour by the X9 service.")
        self.guardar = Adw.ComboRow(title="Keep everything for",
                                    model=Gtk.StringList.new([t for t, _ in self.GUARDAR]))
        valores = [v for _, v in self.GUARDAR]
        self.guardar.set_selected(valores.index(atual["keep_days"]) if atual["keep_days"] in valores else 4)
        geral.add(self.guardar)
        pagina.add(geral)
        self.grupo_regras = Adw.PreferencesGroup(
            title="Rules", description="Delete some entries sooner, by category and/or result. "
                                       "Example: Inspect (read-only), any result, after 24 hours.")
        adicionar = Gtk.Button(icon_name="list-add-symbolic", css_classes=["flat"], tooltip_text="Add rule")
        adicionar.connect("clicked", lambda *_: self.nova_regra())
        self.grupo_regras.set_header_suffix(adicionar)
        pagina.add(self.grupo_regras)
        self.regras = []
        for r in atual["rules"]:
            self.nova_regra(r)
        fim = Adw.PreferencesGroup()
        self.aviso = Gtk.Label(css_classes=["error"], wrap=True, visible=False)
        fim.add(self.aviso)
        self.salvar = Gtk.Button(label="Save (asks admin password)", css_classes=["suggested-action", "pill"],
                                 halign=Gtk.Align.CENTER, margin_top=12)
        self.salvar.connect("clicked", lambda *_: self.gravar())
        fim.add(self.salvar)
        pagina.add(fim)
        vista.set_content(pagina)
        self.set_child(vista)

    def nova_regra(self, r=None):
        r = r or {"category": "any", "result": "any", "after": 24, "unit": "hours"}
        linha = Adw.ActionRow(title="Delete")
        cat = lista_suspensa([("Any category", "any")] + [(c, c) for c in x.CATEGORIAS])
        res = lista_suspensa([("Any result", "any")] + [(t, k) for k, t in x.RESULTADOS.items()])
        depois = Gtk.SpinButton.new_with_range(1, 87600, 1)
        depois.set_value(r["after"])
        unidade = lista_suspensa([("hours", "hours"), ("days", "days")])
        for d, v in ((cat, r["category"]), (res, r["result"]), (unidade, r["unit"])):
            d.set_selected(next((i for i, (_, o) in enumerate(d.opcoes) if o == v), 0))
        tirar = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], tooltip_text="Remove rule")
        for w in (cat, res, Gtk.Label(label="after"), depois, unidade, tirar):
            w.set_valign(Gtk.Align.CENTER)
            linha.add_suffix(w)
        item = (linha, cat, res, depois, unidade)
        tirar.connect("clicked", lambda *_: (self.grupo_regras.remove(linha), self.regras.remove(item)))
        self.regras.append(item)
        self.grupo_regras.add(linha)

    def gravar(self):
        dados = {"keep_days": self.GUARDAR[self.guardar.get_selected()][1],
                 "rules": [{"category": valor(c), "result": valor(r), "after": int(d.get_value()),
                            "unit": valor(u)} for _, c, r, d, u in self.regras]}
        try:
            x.validar_retencao(dados)
        except ValueError as erro:
            self.aviso.set_label(str(erro))
            self.aviso.set_visible(True)
            return
        self.salvar.set_sensitive(False)
        em_thread(lambda: rodar_admin(ADMIN, "set-retention", json.dumps(dados)), self.depois)

    def depois(self, r):
        self.salvar.set_sensitive(True)
        motivo = falha(r)
        if motivo:
            self.aviso.set_label(f"Not saved: {motivo}")
            self.aviso.set_visible(True)
            return
        self.close()
        self.janela.toast("Auto-delete saved")
        self.janela.carregar()


# ---------- details ----------

class DialogoDetalhe(Adw.Dialog):
    def __init__(self, janela, ev):
        super().__init__(title="Entry", content_width=620, content_height=620)
        vista = Adw.ToolbarView()
        vista.add_top_bar(Adw.HeaderBar())
        pagina = Adw.PreferencesPage()
        grupo = Adw.PreferencesGroup(title=ev.get("purpose", ""))
        campos = [("When", data_txt(ev["ts"], "%A, %d %B %Y · %H:%M:%S")),
                  ("Result", x.RESULTADOS.get(ev.get("result"), ev.get("result", "")) +
                   (f" — {ev['reason']}" if ev.get("reason") else "")),
                  ("Category", ev.get("category", "")), ("Risk", ev.get("risk", "").capitalize()),
                  ("Asked by (app)", ev.get("app", "")),
                  ("User", f"{ev.get('user', '')} → {ev.get('as', 'root')}"),
                  ("Tool", ev.get("tool", "")),
                  ("Terminal", ev.get("tty", "") if ev.get("terminal") else "No terminal (app or background)"),
                  ("Folder", ev.get("cwd", "") or "—")]
        for titulo, texto in campos:
            linha = Adw.ActionRow(title=titulo, subtitle=texto or "—", subtitle_selectable=True)
            linha.set_use_markup(False)
            grupo.add(linha)
        pagina.add(grupo)
        cmd = Adw.PreferencesGroup(title="Command")
        rotulo = Gtk.Label(label=ev.get("command", "") or "—", wrap=True, selectable=True, xalign=0,
                           css_classes=["x9-mono", "card"], margin_top=4)
        rotulo.set_wrap_mode(2)                       # WORD_CHAR: long paths still break
        cmd.add(rotulo)
        pagina.add(cmd)
        acoes = Adw.PreferencesGroup()
        apagar = Gtk.Button(label="Delete this entry", css_classes=["destructive-action", "pill"],
                            halign=Gtk.Align.CENTER)
        apagar.connect("clicked", lambda *_: (self.close(), janela.apagar([ev["id"]])))
        acoes.add(apagar)
        pagina.add(acoes)
        vista.set_content(pagina)
        self.set_child(vista)


# ---------- the log ----------

class Registro(Gtk.Box):
    def __init__(self, janela):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.janela = janela
        self.eventos = []
        self.visiveis = []
        self.marcados = set()
        self.selecionando = False
        self.assinatura = None

        filtros = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, min_children_per_line=2,
                              column_spacing=6, row_spacing=6, margin_start=12, margin_end=12, margin_top=10,
                              margin_bottom=6, homogeneous=False)
        self.busca = Gtk.SearchEntry(placeholder_text="Search command, app, purpose…", width_chars=26)
        self.busca.connect("search-changed", lambda *_: self.filtrar())
        self.dia_de = BotaoData("From day", self.filtrar)
        self.dia_ate = BotaoData("To day", self.filtrar)
        self.hora_de = Gtk.Entry(placeholder_text="From HH:MM", width_chars=10, max_length=5)
        self.hora_ate = Gtk.Entry(placeholder_text="To HH:MM", width_chars=10, max_length=5)
        for e in (self.hora_de, self.hora_ate):
            e.connect("changed", lambda *_: self.filtrar())
        self.categoria = lista_suspensa([("Any category", "")] + [(c, c) for c in x.CATEGORIAS], self.filtrar)
        self.resultado = lista_suspensa([("Any result", "")] + [(t, k) for k, t in x.RESULTADOS.items()], self.filtrar)
        self.risco = lista_suspensa([("Any risk", ""), ("High", "high"), ("Medium", "medium"), ("Low", "low")],
                                    self.filtrar)
        self.app = lista_suspensa([("Any app", "")], self.filtrar)
        limpar = Gtk.Button(label="Clear filters", css_classes=["flat"])
        limpar.connect("clicked", lambda *_: self.limpar_filtros())
        for w in (self.busca, self.dia_de, self.dia_ate, self.hora_de, self.hora_ate, self.categoria,
                  self.resultado, self.risco, self.app, limpar):
            filtros.append(w)
        self.append(filtros)

        self.contagem = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], margin_start=14, margin_bottom=4)
        self.append(self.contagem)

        self.lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"],
                                 margin_start=12, margin_end=12, margin_bottom=12, valign=Gtk.Align.START)
        self.lista.connect("row-activated", self.ao_clicar)
        self.vazio = Adw.StatusPage(icon_name="security-high-symbolic", title="Nothing here",
                                    description="No sudo, pkexec or su calls match these filters.", vexpand=True)
        rolar = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.pilha = Gtk.Stack(vexpand=True)
        rolar.set_child(self.lista)
        self.pilha.add_named(rolar, "lista")
        self.pilha.add_named(self.vazio, "vazio")
        self.append(self.pilha)

        self.barra = Gtk.ActionBar(revealed=False)
        todos = Gtk.Button(label="Select all shown")
        todos.connect("clicked", lambda *_: self.marcar_todos(True))
        nenhum = Gtk.Button(label="Select none")
        nenhum.connect("clicked", lambda *_: self.marcar_todos(False))
        self.apagar_sel = Gtk.Button(label="Delete selected", css_classes=["destructive-action"])
        self.apagar_sel.connect("clicked", lambda *_: self.janela.apagar(sorted(self.marcados)))
        self.barra.pack_start(todos)
        self.barra.pack_start(nenhum)
        self.barra.pack_end(self.apagar_sel)
        self.append(self.barra)
        GLib.timeout_add_seconds(3, self.vigiar)

    # --- data ---

    def assinatura_do_arquivo(self):
        try:
            itens = [(n, os.stat(os.path.join(x.ARQUIVO, n)).st_mtime_ns) for n in os.listdir(x.ARQUIVO)
                     if n.endswith(".jsonl")]
            return tuple(sorted(itens))
        except OSError as erro:
            return ("erro", erro.errno)

    def vigiar(self):
        if self.janela.destravada and self.assinatura_do_arquivo() != self.assinatura:
            self.carregar()
        return True

    def carregar(self):
        self.assinatura = self.assinatura_do_arquivo()
        if not os.access(x.ARQUIVO, os.R_OK | os.X_OK):
            self.eventos = []
            self.vazio.set_title("Can't read the archive")
            self.vazio.set_description("X9 is not installed, or your user is not in the admin group (wheel).")
        else:
            self.vazio.set_title("Nothing here")
            self.eventos = sorted((e for d in x.dias() for e in x.ler_dia(d)), key=lambda e: e.get("ts", 0),
                                  reverse=True)
        ids = {e["id"] for e in self.eventos}
        self.marcados &= ids
        apps = sorted({e.get("app", "") for e in self.eventos if e.get("app")})
        atual = valor(self.app)
        self.app.opcoes = [("Any app", "")] + [(a, a) for a in apps]
        self.app.set_model(Gtk.StringList.new([t for t, _ in self.app.opcoes]))
        self.app.set_selected(next((i for i, (_, v) in enumerate(self.app.opcoes) if v == atual), 0))
        self.filtrar()

    def filtros(self):
        return {"text": self.busca.get_text(), "day_from": self.dia_de.dia, "day_to": self.dia_ate.dia,
                "time_from": self.hora_de.get_text().strip(), "time_to": self.hora_ate.get_text().strip(),
                "category": valor(self.categoria), "result": valor(self.resultado), "risk": valor(self.risco),
                "app": valor(self.app)}

    def limpar_filtros(self):
        self.busca.set_text("")
        self.dia_de.limpar()
        self.dia_ate.limpar()
        self.hora_de.set_text("")
        self.hora_ate.set_text("")
        for d in (self.categoria, self.resultado, self.risco, self.app):
            d.set_selected(0)
        self.filtrar()

    def filtrar(self):
        f = self.filtros()
        for campo, chave in ((self.hora_de, "time_from"), (self.hora_ate, "time_to")):
            try:
                x.minutos(f[chave])
                campo.remove_css_class("error")
            except ValueError:
                campo.add_css_class("error")
                f[chave] = ""
        self.visiveis = [e for e in self.eventos if x.passa(e, f)]
        self.desenhar()

    # --- drawing ---

    def desenhar(self):
        self.lista.remove_all()
        mostrados = self.visiveis[:MAX_LINHAS]
        dia_anterior = None
        for ev in mostrados:
            dia = data_txt(ev["ts"], "%Y-%m-%d")
            if dia != dia_anterior:
                n = sum(1 for e in self.visiveis if data_txt(e["ts"], "%Y-%m-%d") == dia)
                cab = Gtk.ListBoxRow(activatable=False, selectable=False)
                cab.set_child(Gtk.Label(label=f"{data_txt(ev['ts'], '%A, %d %B %Y')} · {n} "
                                              f"entr{'y' if n == 1 else 'ies'}", xalign=0, css_classes=["x9-day"]))
                self.lista.append(cab)
                dia_anterior = dia
            self.lista.append(self.linha(ev))
        total, filtrados = len(self.eventos), len(self.visiveis)
        texto = f"{filtrados} of {total} entries"
        if filtrados > MAX_LINHAS:
            texto += f" · showing the newest {MAX_LINHAS}, narrow the filters to see the rest"
        if self.marcados:
            texto += f" · {len(self.marcados)} selected"
        self.contagem.set_label(texto)
        self.apagar_sel.set_label(f"Delete selected ({len(self.marcados)})")
        self.apagar_sel.set_sensitive(bool(self.marcados))
        self.pilha.set_visible_child_name("lista" if mostrados else "vazio")

    def linha(self, ev):
        linha = Adw.ActionRow(title=ev.get("purpose", "") or ev.get("command", ""), activatable=True)
        linha.set_use_markup(False)
        linha.set_title_lines(1)
        linha.set_subtitle_lines(1)
        cmd = ev.get("command", "").replace("\n", " ")
        linha.set_subtitle(f"{data_txt(ev['ts'])} · {ev.get('app', '?')} · {ev.get('tool')} → {ev.get('as')} · {cmd}")
        linha.ev = ev
        if self.selecionando:
            marca = Gtk.CheckButton(active=ev["id"] in self.marcados, valign=Gtk.Align.CENTER)
            marca.connect("toggled", lambda b, i=ev["id"]: self.marcar(i, b.get_active()))
            linha.add_prefix(marca)
            linha.marca = marca
        icone = Gtk.Image.new_from_icon_name(RISCO_ICONE.get(ev.get("risk"), "dialog-question-symbolic"))
        icone.add_css_class(f"x9-risk-{ev.get('risk', 'low')}")
        icone.set_tooltip_text(f"Risk: {ev.get('risk', '?')}")
        linha.add_prefix(icone)
        res = ev.get("result", "")
        linha.add_suffix(pill(ev.get("category", ""), "x9-neutral"))
        linha.add_suffix(pill(x.RESULTADOS.get(res, res), "x9-allowed" if res == "allowed" else "x9-bad"))
        return linha

    def ao_clicar(self, _lista, linha):
        ev = getattr(linha, "ev", None)
        if ev is None:
            return
        if self.selecionando and hasattr(linha, "marca"):
            linha.marca.set_active(not linha.marca.get_active())
            return
        DialogoDetalhe(self.janela, ev).present(self.janela)

    # --- selection ---

    def modo_selecao(self, ligado):
        self.selecionando = ligado
        if not ligado:
            self.marcados.clear()
        self.barra.set_revealed(ligado)
        self.desenhar()

    def marcar(self, id_, sim):
        (self.marcados.add if sim else self.marcados.discard)(id_)
        n = len(self.marcados)
        self.apagar_sel.set_label(f"Delete selected ({n})")
        self.apagar_sel.set_sensitive(bool(n))

    def marcar_todos(self, sim):
        if sim:
            self.marcados |= {e["id"] for e in self.visiveis}
        else:
            self.marcados.clear()
        self.desenhar()


class BotaoData(Gtk.MenuButton):
    """Button that opens a calendar. dia = 'YYYY-MM-DD' or '' (any)."""

    def __init__(self, rotulo, ao_mudar):
        super().__init__(label=rotulo)
        self.rotulo, self.ao_mudar, self.dia = rotulo, ao_mudar, ""
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=6, margin_end=6,
                        margin_top=6, margin_bottom=6)
        self.calendario = Gtk.Calendar()
        self.calendario.connect("day-selected", lambda *_: self.escolher())
        limpar = Gtk.Button(label="Any day", css_classes=["flat"])
        limpar.connect("clicked", lambda *_: (self.limpar(), self.ao_mudar(), self.get_popover().popdown()))
        caixa.append(self.calendario)
        caixa.append(limpar)
        pop = Gtk.Popover()
        pop.set_child(caixa)
        self.set_popover(pop)

    def escolher(self):
        d = self.calendario.get_date()
        self.dia = d.format("%Y-%m-%d")
        self.set_label(f"{self.rotulo.split()[0]} {d.format('%d %b %Y')}")
        self.ao_mudar()

    def limpar(self):
        self.dia = ""
        self.set_label(self.rotulo)


# ---------- window ----------

class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="X9", default_width=1040, default_height=720)
        self.destravada = False
        self.toasts = Adw.ToastOverlay()
        vista = Adw.ToolbarView()
        self.cabecalho = Adw.HeaderBar()
        vista.add_top_bar(self.cabecalho)
        self.selecionar = Gtk.ToggleButton(icon_name="selection-mode-symbolic", tooltip_text="Select entries",
                                           visible=False)
        self.selecionar.connect("toggled", lambda b: self.registro.modo_selecao(b.get_active()))
        self.cabecalho.pack_start(self.selecionar)
        menu = Gio.Menu()
        menu.append("Auto-delete…", "win.retencao")
        menu.append("Change X9 password…", "win.senha")
        menu.append("Delete all logs…", "win.tudo")
        menu.append("Lock", "win.travar")
        self.menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, visible=False)
        self.cabecalho.pack_end(self.menu)
        for nome, funcao in (("retencao", lambda: DialogoRetencao(self).present(self)),
                             ("senha", self.trocar_senha), ("tudo", self.apagar_tudo), ("travar", self.travar)):
            acao = Gio.SimpleAction.new(nome, None)
            acao.connect("activate", lambda *_a, f=funcao: f())
            self.add_action(acao)
        self.pilha = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        if not os.path.exists(SETLOCK):
            self.pilha.add_named(Adw.StatusPage(icon_name="security-high-symbolic", title="X9 is not installed",
                                                description="The X9 service is missing. Install it with "
                                                            "system/x9/install.sh --apply"), "falta")
        self.trava = Trava(self)
        self.registro = Registro(self)
        self.pilha.add_named(self.trava, "trava")
        self.pilha.add_named(self.registro, "registro")
        self.pilha.set_visible_child_name("falta" if not os.path.exists(SETLOCK) else "trava")
        self.toasts.set_child(self.pilha)
        vista.set_content(self.toasts)
        self.set_content(vista)

    def toast(self, texto):
        self.toasts.add_toast(Adw.Toast(title=texto, timeout=3))

    def destravar(self):
        self.destravada = True
        self.selecionar.set_visible(True)
        self.menu.set_visible(True)
        self.registro.carregar()
        self.pilha.set_visible_child_name("registro")

    def travar(self):
        self.destravada = False
        self.selecionar.set_active(False)
        self.selecionar.set_visible(False)
        self.menu.set_visible(False)
        self.registro.eventos = []
        self.registro.visiveis = []
        self.registro.lista.remove_all()
        self.trava.mostrar()
        self.pilha.set_visible_child_name("trava")

    def carregar(self):
        self.registro.carregar()

    def trocar_senha(self):
        def salvar(antiga, nova, dialogo):
            em_thread(lambda: rodar_admin(SETLOCK, None, json.dumps({"old": antiga, "new": nova})),
                      lambda r: depois(r, dialogo))

        def depois(r, dialogo):
            if not isinstance(r, Exception) and r.returncode == 3:
                return dialogo.erro("The current X9 password is wrong")
            motivo = falha(r)
            if motivo:
                return dialogo.erro(f"Not changed: {motivo}")
            dialogo.close()
            self.toast("X9 password changed")
        DialogoSenha(self, "Change X9 password", "Type the current X9 password and a new one.",
                     pedir_antiga=True, ao_salvar=salvar).present(self)

    def confirmar(self, titulo, texto, botao, ao_ok):
        d = Adw.AlertDialog(heading=titulo, body=texto)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", botao)
        d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_default_response("cancel")
        d.connect("response", lambda _d, r: ao_ok() if r == "ok" else None)
        d.present(self)

    def apagar(self, ids):
        if not ids:
            return
        n = len(ids)
        self.confirmar(f"Delete {n} entr{'y' if n == 1 else 'ies'}?",
                       "This can't be undone. Your admin password is asked. X9 keeps a note that entries "
                       "were deleted.", "Delete",
                       lambda: em_thread(lambda: rodar_admin(ADMIN, "delete", json.dumps(ids)), self.depois_apagar))

    def apagar_tudo(self):
        self.confirmar("Delete all logs?", "Every archived day goes away. This can't be undone. "
                       "Your admin password is asked.", "Delete all",
                       lambda: em_thread(lambda: rodar_admin(ADMIN, "delete-all", ""), self.depois_apagar))

    def depois_apagar(self, r):
        motivo = falha(r)
        if motivo:
            return self.toast(f"Not deleted: {motivo}")
        self.toast("Deleted")
        if self.selecionar.get_active():
            self.registro.marcados.clear()
        self.registro.carregar()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.X9", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_activate(self):
        janela = self.get_active_window()
        if janela is None:
            provedor = Gtk.CssProvider()
            provedor.load_from_string(CSS)
            from gi.repository import Gdk
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            janela = Janela(self)
        janela.present()


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
