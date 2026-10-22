#!/usr/bin/env python3
"""
clipboard.py — histórico da área de transferência (Ctrl+Shift+V).

- Mostra os itens copiados (até 50, o mais recente em cima): textos, imagens, arquivos.
- Clique (ou Enter) num item: ele volta pra área de transferência E é colado no app em que você
  estava. Del apaga o item; Esc fecha; digitar filtra os textos.
- Quem GUARDA os itens é o ~/.local/bin/clipboard-guardar (chamado pelo "wl-paste --watch" a cada
  cópia); aqui a gente só LÊ o histórico de ~/.local/share/aero-clipboard/.
- Fica carregado em segundo plano (--daemon), como o Super Menu: abre na hora.
- Imagens: o botãozinho de "quadro" (ou botão direito → "Set as Wallpaper") põe a imagem de papel de
  parede pelo ~/.local/bin/papel-de-parede (doc 21). Funciona com o "Copy Image" de qualquer navegador.

Colar sozinho: o Hyprland tem o "sendshortcut", que manda um atalho de teclado direto pra uma janela.
Depois de escolher, a gente devolve o foco pro app e manda Ctrl+V (ou Ctrl+Shift+V nos terminais,
que usam esse atalho pra colar).
"""
import json
import os
import warnings
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Adw, Gdk, GdkPixbuf, Gio, GLib, Gtk  # noqa: E402

warnings.filterwarnings("ignore", category=DeprecationWarning)

PASTA = os.path.expanduser("~/.local/share/aero-clipboard")
INDICE = os.path.join(PASTA, "indice.json")
LARGURA, ALTURA = 520, 640
TERMINAIS = {"kitty", "foot", "alacritty", "org.wezfurlong.wezterm", "Alacritty", "com.mitchellh.ghostty"}

CSS = """
window.clipboard { background: @window_bg_color; backdrop-filter: blur(20px); }   /* cor do tema (doc 18) */
.clipboard .cabecalho { padding: 14px 14px 6px 14px; }
.clipboard searchentry { min-height: 38px; border-radius: 12px; }
.clipboard list { background: transparent; }
.clipboard row.item { border-radius: 12px; padding: 8px 10px; margin: 2px 8px; }
.clipboard row.item:selected { background: alpha(var(--accent-bg-color, #3584e4), 0.18); }
.clipboard row.item:hover { background: alpha(currentColor, 0.06); }
.clipboard row.item:selected:hover { background: alpha(var(--accent-bg-color, #3584e4), 0.24); }
.clipboard .previa { font-size: 13px; }
.clipboard .info { font-size: 11px; opacity: 0.6; }
.clipboard .miniatura { border-radius: 8px; }
.clipboard .apagar { min-width: 26px; min-height: 26px; padding: 0; border-radius: 99px; }
.clipboard .vazio { opacity: 0.55; }
"""


def ha_quanto(t):
    s = int(time.time() - t)
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def tamanho(n):
    for unidade in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}"
        n /= 1024
    return f"{n:.1f} GB"


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True).stdout


class Historico(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Clipboard", resizable=False,
                         default_width=LARGURA, default_height=ALTURA)
        self.add_css_class("clipboard")
        self.miniaturas = {}     # hash -> textura (cache: não recarregar as imagens toda vez)
        self.anterior = None     # janela em que você estava (pra colar nela)

        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cabecalho = Gtk.Box(spacing=8)
        cabecalho.add_css_class("cabecalho")
        self.busca = Gtk.SearchEntry(placeholder_text="Search clipboard", hexpand=True)
        self.busca.connect("search-changed", lambda *_: self.lista.invalidate_filter())
        self.busca.connect("activate", lambda *_: self.escolher_selecionado())
        cabecalho.append(self.busca)
        limpar = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Clear all")
        limpar.add_css_class("flat")
        limpar.connect("clicked", lambda *_: self.limpar_tudo())
        cabecalho.append(limpar)
        caixa.append(cabecalho)

        self.lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True)
        self.lista.set_filter_func(self.filtro)
        self.lista.connect("row-activated", lambda _l, linha: self.escolher(linha.item))
        self.vazio = Gtk.Label(label="Nothing copied yet", vexpand=True, visible=False)
        self.vazio.add_css_class("vazio")
        caixa.append(self.vazio)
        rolagem = Gtk.ScrolledWindow(child=self.lista, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        rolagem.set_margin_bottom(10)
        caixa.append(rolagem)
        self.set_child(caixa)
        import dotkit
        dotkit.tema_apps(self)                  # DotStyle (dotstyle-apps.css), only in that visual

        # Perdeu o foco = fecha (como o Super Menu). Menos nos primeiros 0,4 s: às vezes outro app
        # pede atenção bem na hora em que a janelinha abre e "rouba" o foco por um instante.
        self.aberto_em = 0.0
        self.connect("notify::is-active", self.ao_mudar_foco)
        teclas = Gtk.EventControllerKey()
        teclas.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        teclas.connect("key-pressed", self.ao_tecla)
        self.add_controller(teclas)

    # ---------- montar a lista ----------

    def ler(self):
        try:
            with open(INDICE) as f:
                return json.load(f)
        except (OSError, ValueError):
            return []

    def montar(self):
        self.lista.remove_all()
        itens = self.ler()
        for item in itens:
            self.lista.append(self.linha(item))
        self.vazio.set_visible(not itens)
        primeira = self.lista.get_row_at_index(0)
        if primeira:
            self.lista.select_row(primeira)

    def linha(self, item):
        caixa = Gtk.Box(spacing=10)
        tipo = item["tipo"]
        caminho = os.path.join(PASTA, item["arquivo"])
        if tipo.startswith("image/"):
            # A miniatura já vem no tamanho de mostrar (até 280x110). A Picture sozinha esticaria
            # até a largura da janela (e a altura junto, pra manter a proporção): a moldura de
            # tamanho fixo segura ela no tamanho certo.
            textura = self.miniatura(item, caminho)
            imagem = Gtk.Picture(paintable=textura, can_shrink=True, content_fit=Gtk.ContentFit.CONTAIN)
            imagem.add_css_class("miniatura")
            conteudo = Gtk.Box(halign=Gtk.Align.START, overflow=Gtk.Overflow.HIDDEN)
            conteudo.add_css_class("miniatura")
            if textura:
                conteudo.set_size_request(textura.get_width(), textura.get_height())
            conteudo.append(imagem)
            conteudo.add_css_class("miniatura")
            info = f"Image · {tamanho(item['tamanho'])}"
        elif tipo == "text/uri-list":
            arquivos = [GLib.filename_from_uri(u)[0] if u.startswith("file://") else u
                        for u in item.get("previa", "").split() if u]
            texto = "\n".join(os.path.basename(a.rstrip("/")) or a for a in arquivos[:3])
            if len(arquivos) > 3:
                texto += f"\n+{len(arquivos) - 3} more"
            conteudo = Gtk.Label(label=texto, xalign=0, ellipsize=3, lines=3, wrap=True,
                                 use_markup=False)
            conteudo.add_css_class("previa")
            info = f"{len(arquivos)} file{'s' if len(arquivos) != 1 else ''}"
            caixa.append(Gtk.Image(icon_name="folder-documents-symbolic", pixel_size=22))
        elif "previa" in item:
            texto = " ".join(item["previa"].split())    # junta espaços e quebras de linha
            conteudo = Gtk.Label(label=texto, xalign=0, ellipsize=3, lines=3, wrap=True,
                                 wrap_mode=2, max_width_chars=50, use_markup=False)  # 2 = quebra em qualquer lugar
            conteudo.add_css_class("previa")
            info = "Text" if item["tamanho"] < 1024 else f"Text · {tamanho(item['tamanho'])}"
        else:
            conteudo = Gtk.Label(label=tipo, xalign=0, use_markup=False)
            info = tamanho(item["tamanho"])

        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        textos.append(conteudo)
        rodape = Gtk.Label(label=f"{info} · {ha_quanto(item['quando'])}", xalign=0)
        rodape.add_css_class("info")
        textos.append(rodape)
        caixa.append(textos)

        botoes = []          # botõezinhos que só aparecem com o mouse em cima da linha
        if tipo.startswith("image/"):
            wallpaper = Gtk.Button(icon_name="preferences-desktop-wallpaper-symbolic", valign=Gtk.Align.CENTER,
                                   tooltip_text="Set as Wallpaper", opacity=0)
            wallpaper.add_css_class("flat")
            wallpaper.add_css_class("apagar")       # mesmo formato redondinho do X
            wallpaper.connect("clicked", lambda *_: self.usar_de_wallpaper(item))
            caixa.append(wallpaper)
            botoes.append(wallpaper)
        apagar = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                            tooltip_text="Remove", opacity=0)
        apagar.add_css_class("flat")
        apagar.add_css_class("apagar")
        caixa.append(apagar)
        botoes.append(apagar)

        linha = Gtk.ListBoxRow(child=caixa)
        linha.add_css_class("item")
        linha.item = item
        apagar.connect("clicked", lambda *_: self.apagar(linha))
        # o X (e o do wallpaper) só aparecem com o mouse em cima da linha
        movimento = Gtk.EventControllerMotion()
        movimento.connect("enter", lambda *_: [b.set_opacity(1) for b in botoes])
        movimento.connect("leave", lambda *_: [b.set_opacity(0) for b in botoes])
        linha.add_controller(movimento)
        # botão direito: menu com as ações do item
        direito = Gtk.GestureClick(button=3)
        direito.connect("pressed", lambda *_: self.menu_item(linha))
        linha.add_controller(direito)
        return linha

    def menu_item(self, linha):
        item = linha.item
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        pop = Gtk.Popover(child=caixa, has_arrow=False)
        acoes = [("Paste", lambda: self.escolher(item))]
        if item["tipo"].startswith("image/"):
            acoes.append(("Set as Wallpaper", lambda: self.usar_de_wallpaper(item)))
        acoes.append(("Remove", lambda: self.apagar(linha)))
        for texto, acao in acoes:
            b = Gtk.Button(label=texto)
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            b.connect("clicked", lambda _b, a=acao: (pop.popdown(), a()))
            caixa.append(b)
        pop.set_parent(linha)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def usar_de_wallpaper(self, item):
        """Manda a imagem pro papel-de-parede: ele COPIA pra ~/Pictures/Wallpapers (o item continua
        no histórico), aponta o hyprpaper pra cópia e mostra a notificação "Wallpaper set"."""
        caminho = os.path.join(PASTA, item["arquivo"])
        Gio.Subprocess.new([os.path.expanduser("~/.local/bin/papel-de-parede"), caminho], Gio.SubprocessFlags.NONE)
        self.fechar()

    def miniatura(self, item, caminho):
        if item["hash"] not in self.miniaturas:
            try:
                # carrega já reduzida (rápido e leve, mesmo pra prints grandes)
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(caminho, 280, 110, True)
                self.miniaturas[item["hash"]] = Gdk.Texture.new_for_pixbuf(pb)  # noqa (avisa "obsoleto", mas funciona)
            except GLib.Error:
                return None
        return self.miniaturas[item["hash"]]

    def filtro(self, linha):
        texto = self.busca.get_text().casefold().strip()
        return not texto or texto in linha.item.get("previa", "").casefold()

    # ---------- ações ----------

    def escolher_selecionado(self):
        linha = self.lista.get_selected_row()
        if linha is None:   # com filtro, a selecionada pode ter sumido: pega a 1ª visível
            linha = next((r for r in self.linhas() if r.get_child_visible()), None)
        if linha:
            self.escolher(linha.item)

    def linhas(self):
        linha = self.lista.get_first_child()
        while linha:
            yield linha
            linha = linha.get_next_sibling()

    def escolher(self, item):
        """Põe o item de volta na área de transferência e cola no app em que você estava."""
        caminho = os.path.join(PASTA, item["arquivo"])
        with open(caminho, "rb") as f:
            # wl-copy fica rodando em segundo plano "segurando" o conteúdo até a próxima cópia
            subprocess.run(["wl-copy", "--type", item["tipo"]], stdin=f)
        self.fechar()
        anterior = self.anterior
        if anterior:
            hyprctl("dispatch", "focuswindow", f"address:{anterior['address']}")
            atalho = "CTRL SHIFT" if anterior.get("class") in TERMINAIS else "CTRL"
            # espera o foco voltar pro app antes de mandar o "colar"
            GLib.timeout_add(120, lambda: hyprctl("dispatch", "sendshortcut",
                                                  f"{atalho}, V, address:{anterior['address']}") and False)

    def apagar(self, linha):
        itens = [i for i in self.ler() if i["hash"] != linha.item["hash"]]
        self.gravar(itens)
        try:
            os.remove(os.path.join(PASTA, linha.item["arquivo"]))
        except OSError:
            pass
        proxima = linha.get_next_sibling() or linha.get_prev_sibling()
        self.lista.remove(linha)
        if proxima:
            self.lista.select_row(proxima)
        self.vazio.set_visible(not itens)

    def limpar_tudo(self):
        for item in self.ler():
            try:
                os.remove(os.path.join(PASTA, item["arquivo"]))
            except OSError:
                pass
        self.gravar([])
        self.montar()

    def gravar(self, itens):
        os.makedirs(PASTA, exist_ok=True)
        with open(INDICE + ".tmp", "w") as f:
            json.dump(itens, f, ensure_ascii=False)
        os.replace(INDICE + ".tmp", INDICE)

    def ao_tecla(self, _ctrl, tecla, _codigo, _mods):
        if tecla == Gdk.KEY_Escape:
            self.fechar()
            return True
        if tecla == Gdk.KEY_Delete and not self.busca.get_text():
            linha = self.lista.get_selected_row()
            if linha:
                self.apagar(linha)
            return True
        if tecla in (Gdk.KEY_Down, Gdk.KEY_Up) and self.busca.has_focus():
            linha = self.lista.get_selected_row()
            if linha:
                linha.grab_focus()      # da busca pra lista; as setas seguem dali
            return False
        return False

    # ---------- abrir / fechar ----------

    def alternar(self):
        if self.get_visible():
            self.fechar()
            return
        ativa = json.loads(hyprctl("activewindow", "-j") or "{}")
        self.anterior = ativa if ativa.get("address") else None
        self.busca.set_text("")
        self.montar()
        self.aberto_em = time.monotonic()
        self.present()
        self.busca.grab_focus()

    def ao_mudar_foco(self, *_):
        if self.is_active() or not self.get_visible():
            return
        if time.monotonic() - self.aberto_em < 0.4:
            self.present()          # pegou o foco de volta
        else:
            self.fechar()

    def fechar(self):
        if self.get_visible():
            self.set_visible(False)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Clipboard",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.janela = None

    def do_command_line(self, linha):
        """Roda na instância já aberta: "clipboard" abre/fecha; "--daemon" só carrega."""
        if self.janela is None:
            provedor = Gtk.CssProvider()
            provedor.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.janela = Historico(self)
            self.hold()
            # Ação "alternar" exposta no D-Bus: o lançador chama direto com gdbus (~10 ms),
            # sem precisar iniciar outro Python só pra avisar este programa
            acao = Gio.SimpleAction.new("alternar", None)
            acao.connect("activate", lambda *_: self.janela.alternar())
            self.add_action(acao)
        if "--daemon" not in linha.get_arguments():
            self.janela.alternar()
        return 0


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
