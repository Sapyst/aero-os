#!/usr/bin/env python3
"""
main.py — janela principal do app Settings (Super+I).

Barra lateral em ÁRVORE (doc 57): grupos com filhos que abrem como sanfona (só o grupo escolhido mostra
os filhos); cada filho abre uma seção, às vezes numa sub-página dela (abrir_sub). Grupos:
  Theme (visual do sistema) · Workspaces (item único) · Devices · Connections · System

Layout (igual às Configurações do GNOME/Windows):
  ┌────────────┬──────────────────────────────┐
  │ Display    │  conteúdo da seção escolhida │
  │ Power      │                              │
  │ Sound      │  (clicar em algo pode abrir  │
  │ ...        │   uma sub-página, com "<")   │
  └────────────┴──────────────────────────────┘
  Adw.NavigationSplitView = a divisão lateral | conteúdo
  Adw.NavigationView      = pilha de páginas do lado direito (empilhar / voltar)

Cada seção mora no seu próprio arquivo sec_*.py. As seções só são criadas quando
abertas pela primeira vez (abre mais rápido), e ao_mostrar() atualiza os dados toda vez.

Busca (Ctrl+F ou o campo no topo da barra lateral): na primeira letra digitada, cria TODAS as seções (~250 ms)
e percorre os widgets delas: cada Adw.PreferencesRow / PreferencesGroup vira uma entrada do índice (título,
subtítulo, onde fica). Nada de lista escrita à mão que fica velha: opção nova no Settings já aparece na busca.
APELIDOS_BUSCA só acrescenta palavras que a pessoa digitaria e não estão escritas na tela ("hz", "wifi"...).
Clicar num resultado abre a seção, rola até a opção e pisca ela com a cor do tema.
"""
import os
import unicodedata
from collections import deque

from comum import CSS_BOLINHAS, Adw, Gtk, aplicar_css, botoes_janela
from gi.repository import GLib, Graphene
from sec_aparencia import SecaoAparencia
from sec_areas import SecaoAreas
from sec_atualizacoes import SecaoAtualizacoes
from sec_bluetooth import SecaoBluetooth
from sec_dock import SecaoDock
from sec_energia import SecaoEnergia
from sec_icones import SecaoIcones
from sec_local import SecaoLocal
from sec_memoria import SecaoMemoria
from sec_mouse import SecaoMouse
from sec_notificacoes import SecaoNotificacoes
from sec_padroes import SecaoPadroes
from sec_rede import SecaoRede
from sec_servicos import SecaoServicos
from sec_recording import SecaoGravacao
from sec_sobre import SecaoSobre
from sec_som import SecaoSom
from sec_teclado import SecaoTeclado
from sec_tela import SecaoTela
from sec_theme import SecaoTema
from sec_widgets import SecaoWidgets

# A árvore: (nome do grupo, ícone, [(nome do filho, classe da seção, sub-página ou None), ...])
ARVORE = [
    ("Theme", "preferences-desktop-appearance-symbolic", [
        ("Overview", SecaoTema, None), ("Colors and mode", SecaoAparencia, "colors"),
        ("Wallpaper", SecaoAparencia, "wallpaper"), ("Glass and windows", SecaoAparencia, "glass"),
        ("Icons", SecaoIcones, None), ("Atlas", SecaoDock, None), ("Widgets", SecaoWidgets, None),
        ("Text and motion", SecaoAparencia, "text")]),
    # Workspaces: um item só (a página tem o seletor de área; Transition é sub-página dela)
    ("Workspaces", "view-paged-symbolic", SecaoAreas),
    ("Devices", "computer-symbolic", [
        ("Display", SecaoTela, None), ("Sound", SecaoSom, None), ("Keyboard", SecaoTeclado, None),
        ("Mouse & Touchpad", SecaoMouse, None)]),
    ("Screen recording", "media-record-symbolic", SecaoGravacao),
    ("Connections", "network-wireless-symbolic", [("Network", SecaoRede, None), ("Bluetooth", SecaoBluetooth, None)]),
    ("System", "emblem-system-symbolic", [
        ("Power", SecaoEnergia, None), ("Memory", SecaoMemoria, None), ("Notifications", SecaoNotificacoes, None), ("Default apps", SecaoPadroes, None), ("Location", SecaoLocal, None),
        ("Updates", SecaoAtualizacoes, None), ("Background services", SecaoServicos, None),
        ("About", SecaoSobre, None)]),
]
# Animações da árvore (doc 57): filhos deslizam (Gtk.Revealer), a seta gira, a linha clareia ao passar
CSS_ARVORE = """
.navigation-sidebar > row { transition: background-color 150ms ease-out; }
.navigation-sidebar > row.arvore-filho { min-height: 0; padding-top: 0; padding-bottom: 0; margin-bottom: 0; }
.arvore-seta { transition: -gtk-icon-transform 220ms ease-out; }
.arvore-seta.aberta { -gtk-icon-transform: rotate(90deg); }
"""
DURACAO = 220      # ms: abrir/fechar grupo e a página nova entrando

# Palavras extras por seção ("sec" ou "sec:sub-página") pra busca: o que a pessoa digita e não está escrito na tela
APELIDOS_BUSCA = {
    "tema": "visual aeros aeroterm look style", "aparencia:colors": "theme dark light mode night accent",
    "aparencia:wallpaper": "background image picture super menu start menu", "aparencia:glass": "blur transparency opacity rounded corners",
    "aparencia:text": "font size animations motion", "icones": "icons ascii pack icon theme super menu dock",
    "dock": "dock taskbar autohide intellihide", "widgets": "clock weather pokemon desktop widgets",
    "areas": "workspaces desktops virtual wallpaper per workspace transition glitch",
    "tela": "monitor screen resolution refresh rate hz 60 144 165 180 scale mirror external hdmi",
    "som": "audio volume speakers headphones headset microphone mic output input sound",
    "teclado": "keyboard layout shortcuts keys hotkeys clipboard super menu", "mouse": "touchpad trackpad gestures scroll "
    "pointer cursor speed hot corners super menu", "recording": "screen recording video capture record mp4",
    "rede": "wifi wi-fi internet network wireless ethernet", "bluetooth": "pair pairing headphones devices",
    "energia": "power battery sleep suspend lid idle lock charge limit health shutdown",
    "local": "location weather city temperature celsius fahrenheit super menu", "atualizacoes": "updates upgrade packages "
    "pacman aur flatpak", "sobre": "about system info version hardware specs",
    "padroes": "default apps applications open with file types associations mime programs video player browser orbit file finder",
    "notificacoes": "notifications quick center central control center"}
CSS_BUSCA = """
.busca-lateral { margin: 6px 8px 4px 8px; }
.resultado .titulo { font-weight: 600; }
.resultado .onde { font-size: 11.5px; opacity: 0.6; }
@keyframes achado { 0% { background-color: alpha(var(--accent-bg-color), 0.40); }
                    100% { background-color: transparent; } }
row.achado, .achado { animation: achado 1800ms cubic-bezier(0.22, 1, 0.36, 1); }
"""


def normalizar(texto):
    """Minúsculas e sem acento: "Áudio" acha "audio"."""
    t = unicodedata.normalize("NFKD", texto or "").casefold()
    return "".join(c for c in t if not unicodedata.combining(c))


# nomes antigos de CONFIG_ABA (scripts e Super Menu) -> (seção, sub-página)
APELIDOS = {"wallpapers": ("aparencia", "wallpaper"), "tema": ("tema", None), "theme": ("tema", None),
            "aparencia": ("aparencia", "colors"), "areas": ("areas", None)}


class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Settings", default_width=1000, default_height=720)
        self.set_size_request(760, 480)
        self.secoes = {}

        # Barra lateral em árvore: linha de grupo (com seta) + linhas filhas recuadas
        self.lista = lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        lista.add_css_class("navigation-sidebar")
        self.entradas = {}            # linha -> ("grupo", índice) ou ("filho", (grupo, classe, sub))
        self.filhos = {}              # grupo -> [linhas filhas]
        self.setas = {}
        self.aberto = None
        self.ultimo = {}              # grupo -> último filho aberto (ao reabrir, volta nele)
        self.animacao = None
        for g, (nome, icone, filhos) in enumerate(ARVORE):
            caixa = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
            caixa.append(Gtk.Image.new_from_icon_name(icone))
            caixa.append(Gtk.Label(label=nome, xalign=0, hexpand=True))
            linha = Gtk.ListBoxRow(child=caixa)
            lista.append(linha)
            self.filhos[g] = []
            if not isinstance(filhos, list):          # item sem filhos: abre a seção direto
                self.entradas[linha] = ("filho", (g, filhos, None))
                self.setas[g] = None
                continue
            seta = Gtk.Image.new_from_icon_name("pan-end-symbolic")
            seta.add_css_class("arvore-seta")
            caixa.append(seta)
            self.entradas[linha] = ("grupo", g)
            self.setas[g] = seta
            for titulo, classe, sub in filhos:
                rotulo = Gtk.Label(label=titulo, xalign=0, margin_top=9, margin_bottom=9, margin_start=40)
                rotulo.add_css_class("dim-label")
                cortina = Gtk.Revealer(child=rotulo, transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN,
                                       transition_duration=DURACAO)
                filho = Gtk.ListBoxRow(child=cortina, visible=False)
                filho.add_css_class("arvore-filho")
                # fechou de vez: a linha some (não fica foco de teclado em linha escondida)
                cortina.connect("notify::child-revealed",
                                lambda r, _p, l=filho: r.get_reveal_child() or l.set_visible(False))
                lista.append(filho)
                self.entradas[filho] = ("filho", (g, classe, sub))
                self.filhos[g].append(filho)
        lista.connect("row-selected", self.ao_escolher)
        lista.connect("row-activated", self.ao_ativar)      # clique no grupo: abre / fecha

        # Busca: o campo fica em cima da árvore; com texto, a árvore dá lugar à lista de resultados
        self.indice_busca = None
        self.busca = Gtk.SearchEntry(placeholder_text="Search settings", css_classes=["busca-lateral"])
        self.busca.connect("search-changed", self.ao_buscar)
        self.busca.connect("activate", self.abrir_primeiro)
        self.busca.connect("stop-search", lambda *_: self.busca.set_text(""))
        self.resultados = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["navigation-sidebar"])
        self.resultados.connect("row-activated", lambda _l, r: self.abrir_resultado(r.entrada))
        self.lados = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, transition_duration=150,
                               vexpand=True)
        self.lados.add_named(Gtk.ScrolledWindow(child=lista), "arvore")
        self.lados.add_named(Gtk.ScrolledWindow(child=self.resultados), "busca")
        coluna = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        coluna.append(self.busca)
        coluna.append(self.lados)
        lateral = Adw.ToolbarView(content=coluna)
        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self.atalho_busca)
        self.add_controller(teclas)
        cabecalho = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        cabecalho.pack_start(botoes_janela(self))       # fechar, minimizar, maximizar (que funcionam)
        lateral.add_top_bar(cabecalho)

        self.nav = Adw.NavigationView()
        divisao = Adw.NavigationSplitView(
            sidebar=Adw.NavigationPage(title="Settings", child=lateral),
            content=Adw.NavigationPage(title="Settings", child=self.nav),
            min_sidebar_width=220, max_sidebar_width=260)

        self.avisos = Adw.ToastOverlay(child=divisao)   # "toasts" = avisos que somem sozinhos
        self.set_content(self.avisos)

        # Abrir direto numa seção: CONFIG_ABA=som configuracoes (nomes antigos valem: APELIDOS)
        inicial = os.environ.get("CONFIG_ABA", "tema")
        sec, sub = APELIDOS.get(inicial, (inicial, None))
        if not self.ir(sec, sub):
            self.ir("tema")

    def abrir_grupo(self, g):
        """Sanfona: os filhos do grupo g deslizam pra baixo, os dos outros recolhem. g=None fecha tudo."""
        self.aberto = g
        for outro, linhas in self.filhos.items():
            for l in linhas:
                if outro == g:
                    l.set_visible(True)
                l.get_child().set_reveal_child(outro == g)
            if self.setas[outro]:
                (self.setas[outro].add_css_class if outro == g else self.setas[outro].remove_css_class)("aberta")

    def ao_ativar(self, _lista, linha):
        """Clique (ou Enter) num grupo: se está aberto, fecha; se fechado, abre no último filho visto."""
        tipo, g = self.entradas[linha]
        if tipo != "grupo":
            return
        if self.aberto == g:
            self.abrir_grupo(None)
            self.lista.select_row(linha)         # o grupo fica marcado: a página dele continua aberta
            return
        self.abrir_grupo(g)
        self.lista.select_row(self.ultimo.get(g) or self.filhos[g][0])

    def ao_escolher(self, _lista, linha):
        if linha is None:
            return
        tipo, valor = self.entradas[linha]
        if tipo == "grupo":                      # quem decide é o ao_ativar (abre / fecha)
            return
        g, classe, sub = valor
        self.ultimo[g] = linha
        if self.aberto != g:
            self.abrir_grupo(g)
        if classe.id not in self.secoes:
            self.secoes[classe.id] = classe(self)
        secao = self.secoes[classe.id]
        if hasattr(secao, "abrir_sub"):
            secao.abrir_sub(sub)
        self.nav.replace([secao.pagina])   # troca o lado direito (e descarta sub-páginas abertas)
        secao.ao_mostrar()
        self.entrar(secao.pagina)

    def entrar(self, pagina):
        """A página nova aparece subindo de leve e clareando (o replace do Adw não anima sozinho)."""
        if self.animacao:
            self.animacao.skip()

        def passo(v):
            pagina.set_opacity(v)
            pagina.set_margin_top(round((1 - v) * 14))
        alvo = Adw.CallbackAnimationTarget.new(passo)
        self.animacao = Adw.TimedAnimation.new(pagina, 0, 1, DURACAO, alvo)
        self.animacao.set_easing(Adw.Easing.EASE_OUT_CUBIC)
        self.animacao.play()

    def ir(self, sec_id, sub=None):
        """Seleciona na árvore o filho da seção sec_id (e sub-página sub). False se não existir.
        "transitions" não está na árvore: abre Workspaces e empilha a Transition."""
        if sec_id == "transitions":
            self.ir("areas")
            self.secoes["areas"].abrir_transicoes()
            return True
        for linha, (tipo, valor) in self.entradas.items():
            if tipo == "filho" and valor[1].id == sec_id and (sub is None or valor[2] == sub):
                self.lista.select_row(linha)
                return True
        return False

    # ---------- busca ----------
    def atalho_busca(self, _c, tecla, _cod, estado):
        from gi.repository import Gdk
        if estado & Gdk.ModifierType.CONTROL_MASK and tecla in (Gdk.KEY_f, Gdk.KEY_F):
            self.busca.grab_focus()
            return True
        return False

    def montar_indice(self):
        """Cria todas as seções (as mesmas que a árvore usa depois) e lista as opções de cada uma."""
        indice = []
        onde = {}                                     # classe.id -> [(nome do grupo, nome do filho, sub)]
        for grupo, _icone, filhos in ARVORE:
            for titulo, classe, sub in (filhos if isinstance(filhos, list) else [(grupo, filhos, None)]):
                onde.setdefault(classe.id, []).append((grupo, titulo, sub))
                if classe.id not in self.secoes:
                    self.secoes[classe.id] = classe(self)
                caminho = grupo if titulo == grupo else f"{grupo}  ›  {titulo}"
                apelidos = APELIDOS_BUSCA.get(f"{classe.id}:{sub}") or APELIDOS_BUSCA.get(classe.id, "")
                indice.append(dict(titulo=titulo, onde=caminho, sec=classe.id, sub=sub, linha=None, grupo="",
                                   apelidos=normalizar(apelidos), texto=normalizar(f"{titulo} {grupo} {apelidos}"),
                                   secao=True))
        alvos = [(sid, sec.pagina) for sid, sec in self.secoes.items()]
        areas = self.secoes.get("areas")
        if areas is not None:                         # Transition: sub-página de Workspaces
            from sec_transitions import SecaoTransicoes
            if not hasattr(areas, "transicoes"):
                areas.transicoes = SecaoTransicoes(self)
            alvos.append(("transitions", areas.transicoes.pagina))
            onde["transitions"] = [("Workspaces", "Transition", None)]
        for sid, raiz in alvos:
            secao = self.secoes.get(sid)
            subs = getattr(secao, "subs", None) if sid != "transitions" else None
            fila = deque([(raiz, None)])
            while fila:
                w, grupo = fila.popleft()
                if isinstance(w, Adw.PreferencesGroup):
                    grupo = w
                if isinstance(w, Adw.PreferencesRow) and w.get_visible() and w.get_title():
                    sub = None
                    if subs and grupo is not None:
                        sub = next((k for k, (_t, gs) in subs.items() if grupo in gs), None)
                    nomes = onde.get(sid, [("", "", None)])
                    g_nome, f_nome, _ = next((n for n in nomes if n[2] == sub), nomes[0])
                    partes = [g_nome] + ([f_nome] if f_nome != g_nome else []) + \
                             ([grupo.get_title()] if grupo is not None and grupo.get_title() else [])
                    sub_t = w.get_subtitle() if hasattr(w, "get_subtitle") else ""
                    indice.append(dict(titulo=w.get_title(), onde="  ›  ".join(partes), sec=sid, sub=sub, linha=w,
                                       grupo=normalizar(grupo.get_title() if grupo else ""), apelidos="",
                                       texto=normalizar(f"{w.get_title()} {sub_t or ''} "
                                                        f"{grupo.get_title() if grupo else ''}"), secao=False))
                filho = w.get_first_child()
                while filho is not None:
                    fila.append((filho, grupo))
                    filho = filho.get_next_sibling()
        return indice

    def ao_buscar(self, campo):
        texto = normalizar(campo.get_text().strip())
        if not texto:
            self.lados.set_visible_child_name("arvore")
            return
        if self.indice_busca is None:
            self.indice_busca = self.montar_indice()
        palavras = texto.split()

        def comeca(campo):
            return campo.startswith(texto) or any(p.startswith(palavras[0]) for p in campo.split())

        def nota(e):
            """Menor = melhor: o nome da opção > o nome do grupo dela > um apelido > só no subtítulo."""
            t = normalizar(e["titulo"])
            if t.startswith(texto):
                n = 0
            elif comeca(t):
                n = 1
            elif texto in t:
                n = 2
            elif comeca(e["grupo"]):
                n = 3
            elif comeca(e["apelidos"]):
                n = 4
            else:
                n = 5
            return (n, not e["secao"], len(t))
        achados = sorted((e for e in self.indice_busca if all(p in e["texto"] for p in palavras)), key=nota)
        vistos, unicos = set(), []
        for e in achados:                              # a mesma opção pode aparecer em duas sub-páginas
            chave = (e["titulo"], e["onde"])
            if chave not in vistos:
                vistos.add(chave)
                unicos.append(e)
        while (l := self.resultados.get_first_child()) is not None:
            self.resultados.remove(l)
        for e in unicos[:40]:
            caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                            margin_start=6, margin_end=6)
            caixa.append(Gtk.Label(label=e["titulo"], xalign=0, wrap=True, css_classes=["titulo"]))
            caixa.append(Gtk.Label(label=e["onde"], xalign=0, wrap=True, css_classes=["onde"]))
            linha = Gtk.ListBoxRow(child=caixa, css_classes=["resultado"])
            linha.entrada = e
            self.resultados.append(linha)
        if not unicos:
            vazio = Gtk.ListBoxRow(child=Gtk.Label(label="Nothing found", margin_top=12, css_classes=["dim-label"]),
                                   activatable=False)
            self.resultados.append(vazio)
        self.lados.set_visible_child_name("busca")

    def abrir_primeiro(self, *_):
        primeira = self.resultados.get_row_at_index(0)
        if primeira is not None and hasattr(primeira, "entrada"):
            self.abrir_resultado(primeira.entrada)

    def abrir_resultado(self, e):
        self.ir(e["sec"], e["sub"])
        if e["linha"] is not None:
            GLib.timeout_add(DURACAO + 40, self.destacar, e)

    def destacar(self, e):
        """Rola até a opção (deslizando) e pisca ela. Se a seção remontou as linhas, acha pelo título."""
        linha = e["linha"]
        if linha.get_root() is not self:
            linha = None
            pagina = self.nav.get_visible_page()
            fila = deque([pagina] if pagina else [])
            while fila and linha is None:
                w = fila.popleft()
                if isinstance(w, Adw.PreferencesRow) and w.get_title() == e["titulo"]:
                    linha = w
                f = w.get_first_child()
                while f is not None:
                    fila.append(f)
                    f = f.get_next_sibling()
        if linha is None:
            return False
        pai = linha.get_parent()
        while pai is not None and not isinstance(pai, Gtk.ScrolledWindow):
            if isinstance(pai, Adw.ExpanderRow):
                pai.set_expanded(True)
            pai = pai.get_parent()
        if pai is not None:
            ok, ponto = linha.compute_point(pai.get_child(), Graphene.Point())
            if ok:
                ajuste = pai.get_vadjustment()
                alvo = max(0, min(ponto.y - 80, ajuste.get_upper() - ajuste.get_page_size()))
                anim = Adw.TimedAnimation.new(pai, ajuste.get_value(), alvo, 380,
                                              Adw.PropertyAnimationTarget.new(ajuste, "value"))
                anim.set_easing(Adw.Easing.EASE_OUT_CUBIC)
                anim.play()
        linha.remove_css_class("achado")
        linha.add_css_class("achado")
        GLib.timeout_add(1900, lambda: (linha.remove_css_class("achado"), False)[1])
        return False

    def atualizar_secoes(self):
        """Depois de trocar o wallpaper: as páginas que mostram ele se atualizam."""
        from comum import ler_painel
        if "areas" in self.secoes:
            self.secoes["areas"].mostrar_area()
        if "aparencia" in self.secoes:
            self.secoes["aparencia"].mostrar_wallpaper(ler_painel())

    # Usados pelas seções:
    def empilhar(self, pagina):
        self.nav.push(pagina)       # abre uma sub-página (aparece o botão "<")

    def voltar(self):
        self.nav.pop()

    def avisar(self, texto):
        self.avisos.add_toast(Adw.Toast(title=texto, timeout=3, use_markup=False))


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Settings")

    def do_startup(self):
        Adw.Application.do_startup(self)
        aplicar_css(CSS_BOLINHAS)
        aplicar_css(CSS_ARVORE)
        aplicar_css(CSS_BUSCA)

    def do_activate(self):
        # Se já estiver aberto, só traz a janela pra frente
        (self.props.active_window or Janela(self)).present()


if __name__ == "__main__":
    App().run([])
