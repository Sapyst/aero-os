#!/usr/bin/env python3
"""
supermenu.py — o Super Menu (menu "Iniciar"), aberto com a tecla Super.

Visual (inspirado no vídeo de referência do Aero):
- O painel DESCE de trás da barra de cima (animação "slide top" do Hyprland).
- Layout de "loja de apps": categorias numa coluna à esquerda, busca no topo, apps em cartões
  (ícone, nome, descrição) em 3 colunas. O destaque azul segue o mouse; clique abre.
- Teclado: digite pra buscar, setas, Enter abre, Esc fecha. Clicar fora (perder o foco) fecha.
- Botão direito num app: fixar/desafixar, adicionar ao Dock.

Por que é uma JANELA e não uma "camada" (layer-shell) como a barra:
  Neste Hyprland (0.56) o desfoque de fundo não funcionou em camadas GTK4, por mais que a regra
  estivesse certa; em janelas funciona. Então o menu é uma janela flutuante sem barra de título,
  posicionada logo abaixo da barra pelas regras do hyprland.conf (doc 16).
- Fica carregado em segundo plano (--daemon); rodar `supermenu` de novo abre/fecha.
"""
import math
import os
import subprocess
import sys
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import apps  # noqa: E402
import clima  # noqa: E402

LARGURA, ALTURA = 1040, 760     # tamanho do painel
ALTURA_FAIXA = 140              # a faixa com o papel de parede, no topo (doc 36)
PASTA_FAIXA = os.path.expanduser("~/.cache/aero-shell/menu-faixa")   # cópias pequenas da imagem
# A imagem da faixa: a mesma do desktop (padrão) ou uma só do menu (Settings → Wallpapers, doc 37)
IMAGEM_PROPRIA = os.path.expanduser("~/.local/share/aero-shell/fundos/menu")
FIXADOS_PADRAO = ["com.anthropic.Claude.desktop", "firefox.desktop", "com.aero.Settings.desktop",
                  "kitty.desktop", "thunar.desktop", "steam.desktop"]

CSS = """
/* O Super Menu é uma CAMADA (não uma janela/app, doc 33): a janela é transparente e o cartão tem a
   cor do tema, cantos de 22 px e contorno fino por dentro. Nada de desfoque/sombra grande em camada:
   viraria um quadrado em volta dos cantos (doc 26). */
window.supermenu { background: transparent; backdrop-filter: none; }
/* O fundo do cartão fica SÓ embaixo da faixa (no .corpo). Se ficasse atrás da faixa também, a borda
   suavizada (antialias) da curva da imagem deixava o fundo escuro vazar: uma linha fininha na curva. */
.conteudo { background: none; border-radius: 22px; }
.corpo { padding: 16px; background-color: var(--popover-bg-color, @popover_bg_color);
         border-radius: 0 0 22px 22px; border: 1px solid alpha(currentColor, 0.10); border-top-width: 0; }
/* Faixa do topo (doc 36): o papel de parede atual, com os MESMOS cantos de 22 px do cartão em cima.
   O degradê escuro embaixo garante que o texto branco sempre dá pra ler, em qualquer wallpaper. */
/* A imagem e os degradês são desenhados em código (desenhar_faixa), com a curva recortada lá. */
.faixa { border-radius: 22px 22px 0 0; }
.faixa .temp, .faixa .condicao, .faixa .detalhes { text-shadow: 0 1px 3px rgba(0,0,0,0.55); }
.faixa .temp { color: white; font-size: 40px; font-weight: 300; }
.faixa .condicao { color: white; font-size: 16px; font-weight: 700; }
.faixa .detalhes { color: rgba(255,255,255,0.90); font-size: 13px; }
.conteudo searchentry { min-height: 40px; border-radius: 12px; font-size: 15px; }
.lateral { background: transparent; }
.lateral row { border-radius: 10px; padding: 6px 10px; }
.lateral row:selected { background: alpha(currentColor, 0.12); }
.rotulo-lateral { font-size: 11px; font-weight: 700; opacity: 0.55; margin: 12px 10px 4px 10px; }
flowboxchild.cartao { padding: 10px 12px; border-radius: 12px; background: alpha(currentColor, 0.045); }
flowboxchild.cartao:selected { background: @accent_bg_color; color: @accent_fg_color; }
.cartao .nome { font-weight: 600; }
.cartao .descricao { font-size: 12px; opacity: 0.7; }
flowboxchild.cartao:selected .descricao { opacity: 0.9; }
.titulo-secao { font-size: 18px; font-weight: 700; }
.rodape { padding-top: 8px; border-top: 1px solid alpha(currentColor, 0.10); }
"""

# Itens da coluna da esquerda: (chave, nome, ícone). Chaves de categoria vêm de apps.CATEGORIAS.
SECOES = [("fixados", "Pinned", "view-pin-symbolic"),
          ("recentes", "Recent", "document-open-recent-symbolic"),
          ("todos", "All apps", "view-app-grid-symbolic")]
ICONES_CATEGORIA = {"internet": "web-browser-symbolic", "jogos": "input-gaming-symbolic",
                    "dev": "utilities-terminal-symbolic", "midia": "applications-multimedia-symbolic",
                    "escritorio": "x-office-document-symbolic", "sistema": "preferences-system-symbolic"}


class SuperMenu(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Super Menu", resizable=False,
                         default_width=LARGURA, default_height=ALTURA)
        self.add_css_class("supermenu")
        # CAMADA (layer-shell), não janela: não vira "app" (Super+F, maximizar, Alt+Tab e Dock não pegam
        # nele), fica por cima de tudo e desce da barra de cima com o mesmo gap das janelas (doc 33).
        Camada.init_for_window(self)
        Camada.set_layer(self, Camada.Layer.OVERLAY)
        Camada.set_namespace(self, "supermenu")
        Camada.set_anchor(self, Camada.Edge.TOP, True)      # sem esquerda/direita = centralizado
        Camada.set_margin(self, Camada.Edge.TOP, 12)        # o gap (a camada já começa abaixo da barra)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        self.set_child(self.montar_conteudo())
        # Perdeu o foco (clicou fora, trocou de janela) = fecha.
        # Mas o menu de botão direito (popover) tira o foco por um instante: com ele aberto não fecha,
        # e a checagem espera um pouquinho pra ignorar essas trocas rápidas de foco.
        self.popover = None
        # Fechar ao clicar fora: o vigia de cliques (hypr-botoes, doc 33) chama "supermenu --clique-fora".

        import threading
        threading.Thread(target=clima.obter, daemon=True).start()
        GLib.timeout_add_seconds(15 * 60, lambda: (threading.Thread(target=clima.obter, daemon=True).start(), True)[1])

        teclas = Gtk.EventControllerKey()
        teclas.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        teclas.connect("key-pressed", self.ao_tecla)
        self.add_controller(teclas)

    # ---------- conteúdo ----------

    def montar_faixa(self):
        """A faixa do topo: papel de parede (cortado pra caber) + degradê + "Good evening, nome"."""
        faixa = Gtk.Overlay(height_request=ALTURA_FAIXA)
        faixa.add_css_class("faixa")
        # A imagem e o degradê são DESENHADOS aqui (cairo), com a curva de 22 px recortada no próprio
        # desenho. Com fundo CSS, o renderizador do GTK deixava o canto de cima à esquerda reto na tela
        # real (doc 37). DrawingArea não impõe tamanho: obedece aos 140 px.
        self.pixbuf_faixa = None
        self.imagem_faixa = Gtk.DrawingArea(hexpand=True)      # sem vexpand: senão a faixa cresce além dos 140 px
        self.imagem_faixa.set_draw_func(self.desenhar_faixa)
        faixa.set_child(self.imagem_faixa)
        # O clima de agora (doc 37): desenho + temperatura + condição + detalhes
        self.clima = None
        linha = Gtk.Box(spacing=14, valign=Gtk.Align.END, halign=Gtk.Align.START,
                        margin_start=22, margin_bottom=14)
        self.desenho_clima = Gtk.DrawingArea(content_width=76, content_height=76, valign=Gtk.Align.CENTER)
        self.desenho_clima.set_draw_func(self.desenhar_clima)
        linha.append(self.desenho_clima)
        self.temp = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        self.temp.add_css_class("temp")
        linha.append(self.temp)
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=2)
        self.condicao = Gtk.Label(xalign=0)
        self.condicao.add_css_class("condicao")
        self.detalhes = Gtk.Label(xalign=0)
        self.detalhes.add_css_class("detalhes")
        textos.append(self.condicao)
        textos.append(self.detalhes)
        linha.append(textos)
        self.linha_clima = linha
        faixa.add_overlay(linha)
        return faixa

    def desenhar_faixa(self, _area, cr, w, h):
        """Imagem "cover" (preenche cortando as sobras) + degradês, tudo dentro do contorno arredondado."""
        import cairo
        r = 22
        cr.new_sub_path()                                  # contorno: cantos de cima redondos, de baixo retos
        cr.arc(r, r, r, math.pi, 1.5 * math.pi)
        cr.arc(w - r, r, r, 1.5 * math.pi, 2 * math.pi)
        cr.line_to(w, h)
        cr.line_to(0, h)
        cr.close_path()
        cr.clip()
        pb = self.pixbuf_faixa
        if pb:
            pw, ph = pb.get_width(), pb.get_height()
            escala = max(w / pw, h / ph)                   # "cover"
            cr.save()
            cr.translate((w - pw * escala) / 2, (h - ph * escala) / 2)
            cr.scale(escala, escala)
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
            cr.restore()
        else:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.25)
            cr.paint()
        # degradês escuros: da esquerda (onde fica o clima) e de baixo — leitura em qualquer imagem
        for x0, y0, x1, y1, paradas in ((0, 0, w, 0, ((0, 0.50), (0.55, 0.10), (0.75, 0))),
                                        (0, h, 0, 0, ((0, 0.55), (0.65, 0.12), (1, 0)))):
            g = cairo.LinearGradient(x0, y0, x1, y1)
            for pos, a in paradas:
                g.add_color_stop_rgba(pos, 0, 0, 0, a)
            cr.set_source(g)
            cr.paint()

    def desenhar_clima(self, _area, cr, largura, altura):
        if self.clima:
            clima.desenhar(cr, min(largura, altura), self.clima.get("desenho", "nublado"), self.clima.get("dia", True))

    def mostrar_clima(self, c):
        self.clima = c
        self.linha_clima.set_visible(bool(c))
        if c:
            self.temp.set_label(f"{c['temp']}°")
            self.condicao.set_label(c["texto"])
            partes = [f"Feels like {c['sensacao']}°", f"H {c['max']}°  L {c['min']}°"]
            if c.get("chance_chuva"):
                partes.append(f"{c['chance_chuva']}% rain")
            if c.get("lugar"):
                partes.append(c["lugar"])
            self.detalhes.set_label("  ·  ".join(partes))
            self.desenho_clima.queue_draw()
        return False

    def atualizar_clima(self):
        """Mostra NA HORA o último clima guardado; busca o novo em segundo plano (se passou de 15 min)."""
        import threading
        self.mostrar_clima(clima.ler_cache().get("clima"))
        threading.Thread(target=lambda: GLib.idle_add(self.mostrar_clima, clima.obter()), daemon=True).start()

    @staticmethod
    def imagem_da_faixa():
        """Mesma do desktop (FUNDO_MENU_SINC=1, padrão) ou a imagem própria do menu."""
        sinc = "1"
        try:
            for linha in open(os.path.expanduser("~/.config/hypr/painel.conf")):
                if linha.startswith("FUNDO_MENU_SINC="):
                    sinc = linha.split("=", 1)[1].strip()
        except OSError:
            pass
        if sinc != "1" and os.path.exists(IMAGEM_PROPRIA):
            return IMAGEM_PROPRIA
        texto = open(os.path.expanduser("~/.config/hypr/hyprpaper.conf")).read()
        return os.path.expanduser(next(l.split("=", 1)[1].strip() for l in texto.splitlines()
                                       if l.strip().startswith("path")))

    def atualizar_faixa(self):
        """Ao abrir: o clima e, se a imagem mudou, uma cópia pequena dela (rápida de desenhar)."""
        self.atualizar_clima()
        try:
            origem = self.imagem_da_faixa()
            marca = f"{origem}|{os.path.getmtime(origem)}"
        except (OSError, StopIteration):
            return
        if marca == getattr(self, "marca_faixa", None):
            return
        import hashlib
        import threading
        # uma cópia pequena por imagem (o nome vem do caminho + data do arquivo)
        copia = os.path.join(PASTA_FAIXA, hashlib.sha1(marca.encode()).hexdigest()[:16] + ".jpg")

        def usar():
            from gi.repository import GdkPixbuf
            try:
                self.pixbuf_faixa = GdkPixbuf.Pixbuf.new_from_file(copia)
            except GLib.Error:
                return False
            self.marca_faixa = marca
            self.imagem_faixa.queue_draw()
            return False

        if os.path.exists(copia):
            usar()
            return

        def reduzir():
            # o wallpaper pode ser 4K: guarda uma cópia de 1600 px de largura (desenha num instante)
            from gi.repository import GdkPixbuf
            try:
                img = GdkPixbuf.Pixbuf.new_from_file_at_scale(origem, 1600, -1, True)
                os.makedirs(PASTA_FAIXA, exist_ok=True)
                for velha in os.listdir(PASTA_FAIXA):          # só fica a do wallpaper atual
                    os.remove(os.path.join(PASTA_FAIXA, velha))
                img.savev(copia, "jpeg", ["quality"], ["90"])
            except (GLib.Error, OSError):
                return
            GLib.idle_add(usar)
        threading.Thread(target=reduzir, daemon=True).start()

    def montar_conteudo(self):
        # O cartão: a faixa do wallpaper em cima e, embaixo, as colunas (seções | busca e apps)
        cartao = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cartao.set_size_request(LARGURA - 34, ALTURA - 64)
        cartao.add_css_class("conteudo")
        cartao.append(self.montar_faixa())
        conteudo = Gtk.Box(spacing=16, vexpand=True)
        conteudo.add_css_class("corpo")
        cartao.append(conteudo)

        # Coluna da esquerda: seções e categorias, e no pé o usuário e o botão de energia
        esquerda = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=False)   # largura fixa: não cresce
        esquerda.set_size_request(210, -1)
        self.lateral = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.lateral.add_css_class("lateral")
        self.chaves_lateral = []
        for chave, nome, icone in SECOES:
            self.adicionar_lateral(chave, nome, icone)
        rotulo = Gtk.Label(label="CATEGORIES", xalign=0)
        rotulo.add_css_class("rotulo-lateral")
        linha_rotulo = Gtk.ListBoxRow(child=rotulo, selectable=False, activatable=False)
        self.lateral.append(linha_rotulo)
        self.chaves_lateral.append(None)
        for chave, nome, _ in apps.CATEGORIAS:
            if chave != "todos":
                self.adicionar_lateral(chave, nome, ICONES_CATEGORIA.get(chave, "folder-symbolic"))
        self.lateral.connect("row-selected", self.ao_escolher_secao)
        esquerda.append(Gtk.ScrolledWindow(child=self.lateral, vexpand=True,
                                           hscrollbar_policy=Gtk.PolicyType.NEVER))

        rodape = Gtk.Box(spacing=8)
        rodape.add_css_class("rodape")
        nome = GLib.get_real_name()
        avatar = Adw.Avatar(size=24, text=GLib.get_user_name() if nome in ("", "Unknown") else nome,
                            show_initials=True)
        try:
            avatar.set_custom_image(Gdk.Texture.new_from_filename(os.path.expanduser("~/.face")))
        except GLib.Error:
            pass
        rodape.append(avatar)
        rodape.append(Gtk.Label(label=GLib.get_user_name() if nome in ("", "Unknown") else nome,
                                xalign=0, hexpand=True, ellipsize=3))
        energia = Gtk.MenuButton(icon_name="system-shutdown-symbolic", direction=Gtk.ArrowType.UP,
                                 tooltip_text="Power")
        energia.add_css_class("flat")
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Restart, Shut down e Log out PERGUNTAM antes (energia.py, doc 23); Lock trava a tela (doc 25)
        energia_bin = os.path.expanduser("~/.local/bin/energia")
        for texto, cmd in (("Lock", [os.path.expanduser("~/.local/bin/travar")]),
                           ("Sleep", ["systemctl", "suspend"]), ("Restart…", [energia_bin, "reboot"]),
                           ("Shut down…", [energia_bin, "poweroff"]), ("Log out…", [energia_bin, "logout"])):
            b = Gtk.Button(label=texto)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, c=cmd: (self.fechar(), subprocess.Popen(c)))
            caixa.append(b)
        energia.set_popover(Gtk.Popover(child=caixa))
        rodape.append(energia)
        esquerda.append(rodape)
        conteudo.append(esquerda)

        # Área principal: busca, título e os cartões
        direita = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        self.busca = Gtk.SearchEntry(placeholder_text="Search apps")
        self.busca.connect("search-changed", lambda *_: self.mostrar())
        self.busca.connect("activate", lambda *_: self.abrir_selecionado())
        direita.append(self.busca)
        self.titulo = Gtk.Label(xalign=0)
        self.titulo.add_css_class("titulo-secao")
        direita.append(self.titulo)
        self.grade = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                                 homogeneous=True, max_children_per_line=3, min_children_per_line=3,
                                 row_spacing=8, column_spacing=8, valign=Gtk.Align.START)
        self.grade.connect("child-activated", lambda _g, filho: self.ativar(filho))
        self.vazio = Gtk.Label(label="Nothing here yet", vexpand=True, visible=False)
        self.vazio.add_css_class("dim-label")
        direita.append(self.vazio)
        direita.append(Gtk.ScrolledWindow(child=self.grade, vexpand=True,
                                          hscrollbar_policy=Gtk.PolicyType.NEVER))
        conteudo.append(direita)
        return cartao

    def adicionar_lateral(self, chave, nome, icone):
        caixa = Gtk.Box(spacing=10)
        caixa.append(Gtk.Image(icon_name=icone))
        caixa.append(Gtk.Label(label=nome, xalign=0))
        self.lateral.append(Gtk.ListBoxRow(child=caixa))
        self.chaves_lateral.append(chave)

    def cartao(self, app):
        caixa = Gtk.Box(spacing=12)
        icone = Gtk.Image(gicon=app.get_icon() or Gio.ThemedIcon.new("application-x-executable"), pixel_size=40)
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        nome = Gtk.Label(label=app.get_display_name(), xalign=0, ellipsize=3)
        nome.add_css_class("nome")
        descricao = Gtk.Label(label=app.get_description() or app.get_generic_name() or "", xalign=0,
                              ellipsize=3, max_width_chars=28)
        descricao.add_css_class("descricao")
        textos.append(nome)
        textos.append(descricao)
        caixa.append(icone)
        caixa.append(textos)
        filho = Gtk.FlowBoxChild(child=caixa)
        filho.add_css_class("cartao")
        filho.app = app
        # O destaque azul segue o mouse: passar por cima = selecionar (o mesmo destaque das setas)
        movimento = Gtk.EventControllerMotion()
        movimento.connect("enter", lambda *_: self.grade.select_child(filho))
        filho.add_controller(movimento)
        direito = Gtk.GestureClick(button=3)
        direito.connect("pressed", lambda *_: self.menu_app(filho, app))
        filho.add_controller(direito)
        return filho

    # ---------- o que mostrar ----------

    def ao_escolher_secao(self, _lista, linha):
        if linha is not None and self.chaves_lateral[linha.get_index()]:
            self.secao = self.chaves_lateral[linha.get_index()]
            if self.busca.get_text():
                self.busca.set_text("")        # dispara mostrar() pelo search-changed
            else:
                self.mostrar()

    def mostrar(self):
        texto = self.busca.get_text()
        nomes = dict((c, n) for c, n, _ in SECOES) | dict((c, n) for c, n, _ in apps.CATEGORIAS)
        especiais = []
        if texto:
            # Apps: os que começam com o texto primeiro, depois os que contêm, depois os parecidos
            pontos = [(apps.pontuar(a, texto), a) for a in self.todos_apps]
            lista = [a for p, a in sorted(pontos, key=lambda x: -x[0]) if p > 0]
            self.titulo.set_label(f"Results for “{texto}”")
            especiais = self.resultados_extras(texto)
        elif self.secao == "fixados":
            lista = [a for a in map(apps.por_id, self.fixados) if a]
            self.titulo.set_label("Pinned")
        elif self.secao == "recentes":
            lista = [a for a in map(apps.por_id, apps.ler_lista("menu-recentes", [])) if a]
            self.titulo.set_label("Recent")
        else:
            lista = [a for a in self.todos_apps if apps.na_categoria(a, self.secao)]
            self.titulo.set_label(nomes.get(self.secao, "All apps"))
        self.grade.remove_all()
        conta = [e for e in especiais if e[0] == "conta"]       # resultado de conta vem primeiro
        for e in conta:
            self.grade.append(self.cartao_especial(*e[1:]))
        for app in lista:
            self.grade.append(self.cartao(app))
        for e in especiais:
            if e[0] != "conta":
                self.grade.append(self.cartao_especial(*e[1:]))
        self.vazio.set_visible(not lista and not especiais)
        primeiro = self.grade.get_child_at_index(0)
        if primeiro:
            self.grade.select_child(primeiro)

    def ao_mudar_foco(self, *_):
        if self.is_active():
            return
        def conferir():
            if not self.is_active() and self.popover is None:
                self.fechar()
            return False
        GLib.timeout_add(150, conferir)

    def resultados_extras(self, texto):
        """Cartões além dos apps (doc 25): conta, seções do Settings e busca na web."""
        extras = []
        resultado = apps.calcular(texto)
        if resultado is not None:
            extras.append(("conta", "accessories-calculator-symbolic", f"= {resultado}",
                           "Press Enter to copy", lambda r=resultado: self.copiar(r.replace(" ", ""))))
        for sid, nome, icone in apps.secoes_settings(texto):
            extras.append(("settings", icone, nome, "Settings",
                           lambda s=sid: self.rodar_e_fechar(
                               f"CONFIG_ABA={s} {os.path.expanduser('~/.local/bin/configuracoes')}")))
        url = "https://duckduckgo.com/?q=" + urllib.parse.quote(texto)
        extras.append(("web", "web-browser-symbolic", f"Search the web for “{texto}”", "DuckDuckGo",
                       lambda u=url: self.rodar_e_fechar(f"xdg-open '{u}'")))
        return extras

    def cartao_especial(self, icone, titulo, descricao, acao):
        """Cartão igual ao de app, mas que roda uma ação em vez de abrir um app."""
        caixa = Gtk.Box(spacing=12)
        caixa.append(Gtk.Image(icon_name=icone, pixel_size=40))
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        nome = Gtk.Label(label=titulo, xalign=0, ellipsize=3, use_markup=False)
        nome.add_css_class("nome")
        sub = Gtk.Label(label=descricao, xalign=0, ellipsize=3, use_markup=False)
        sub.add_css_class("descricao")
        textos.append(nome)
        textos.append(sub)
        caixa.append(textos)
        filho = Gtk.FlowBoxChild(child=caixa)
        filho.add_css_class("cartao")
        filho.acao = acao
        movimento = Gtk.EventControllerMotion()
        movimento.connect("enter", lambda *_: self.grade.select_child(filho))
        filho.add_controller(movimento)
        return filho

    def ativar(self, filho):
        if getattr(filho, "acao", None):
            filho.acao()
        else:
            self.abrir_app(filho.app)

    def copiar(self, texto):
        subprocess.run(["wl-copy", "--", texto])
        self.fechar()

    def rodar_e_fechar(self, comando):
        subprocess.Popen(["hyprctl", "dispatch", "exec", comando], stdout=subprocess.DEVNULL)
        self.fechar()

    def menu_app(self, filho, app):
        if self.popover is not None:
            self.popover.popdown()
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        app_id = app.get_id()
        fixado = app_id in self.fixados
        itens = [("Remove from Pinned" if fixado else "Pin as favorite",
                  lambda: self.alternar_fixado(app))]
        if self.secao == "recentes" and not self.busca.get_text():
            itens.append(("Remove from Recent", lambda: self.remover_recente(app)))
        itens.append(("Add to Dock", lambda: self.adicionar_dock(app)))
        pop = Gtk.Popover(child=caixa)
        for texto, acao in itens:
            b = Gtk.Button(label=texto)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, a=acao: (pop.popdown(), GLib.idle_add(lambda: (a(), False)[1])))
            caixa.append(b)

        def ao_fechar(*_):
            self.popover = None
            GLib.idle_add(lambda: (pop.unparent(), False)[1])     # solta o popover (o cartão pode já ter sumido)
            self.busca.grab_focus()
        pop.connect("closed", ao_fechar)
        pop.set_parent(filho)
        self.popover = pop
        pop.popup()

    def remover_recente(self, app):
        recentes = [i for i in apps.ler_lista("menu-recentes", []) if i != app.get_id()]
        apps.gravar_lista("menu-recentes", recentes)
        self.mostrar()

    def alternar_fixado(self, app):
        app_id = app.get_id()
        self.fixados = [i for i in self.fixados if i != app_id] if app_id in self.fixados else self.fixados + [app_id]
        apps.gravar_lista("menu-fixados", self.fixados)
        self.mostrar()

    def adicionar_dock(self, app):
        dock = apps.ler_lista("dock", apps.DOCK_PADRAO)   # sem dock.json ainda: começa dos 3 padrão
        if {"tipo": "app", "id": app.get_id()} not in dock:
            apps.gravar_lista("dock", dock + [{"tipo": "app", "id": app.get_id()}])

    def abrir_app(self, app):
        apps.abrir(app)
        recentes = [app.get_id()] + [i for i in apps.ler_lista("menu-recentes", []) if i != app.get_id()]
        apps.gravar_lista("menu-recentes", recentes[:12])     # guarda os 12 últimos
        self.fechar()

    def abrir_selecionado(self):
        selecionados = self.grade.get_selected_children()
        if selecionados:
            self.ativar(selecionados[0])

    def ao_tecla(self, _ctrl, tecla, _codigo, _mods):
        if tecla == Gdk.KEY_Escape:
            self.fechar()
            return True
        if tecla in (Gdk.KEY_Down, Gdk.KEY_Up) and self.busca.has_focus():
            selecionados = self.grade.get_selected_children()
            if selecionados:
                selecionados[0].grab_focus()     # da busca pros cartões; as setas seguem dali
            return True
        return False

    # ---------- abrir / fechar ----------
    # Mostrar/esconder a janela; o deslizar de cima pra baixo é a animação de janela do Hyprland.

    def alternar(self):
        if self.get_visible():
            self.fechar()
        else:
            self.abrir()

    def abrir(self):
        self.todos_apps = apps.todos()
        self.fixados = apps.ler_lista("menu-fixados", FIXADOS_PADRAO)
        self.busca.set_text("")
        self.secao = "fixados"
        self.lateral.select_row(self.lateral.get_row_at_index(0))
        self.mostrar()
        self.atualizar_faixa()
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)   # digitar já vai pra busca
        self.present()
        self.busca.grab_focus()

    def fechar(self):
        if self.popover is not None:
            self.popover.popdown()
        if self.get_visible():
            self.set_visible(False)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)

    def clique_fora(self):
        """Clique em qualquer lugar com o menu aberto: fecha se foi FORA dele (e sem menu de contexto)."""
        if not self.get_visible() or self.popover is not None:
            return
        import json
        x, y = (int(v) for v in subprocess.run(["hyprctl", "cursorpos"], capture_output=True,
                                                text=True).stdout.split(","))
        camadas = json.loads(subprocess.run(["hyprctl", "layers", "-j"], capture_output=True, text=True).stdout)
        for l in (l for m in camadas.values() for n in m["levels"].values() for l in n):
            if l["namespace"] == "supermenu" and l["x"] <= x <= l["x"] + l["w"] and l["y"] <= y <= l["y"] + l["h"]:
                return
        self.fechar()


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.SuperMenu",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.janela = None

    def do_command_line(self, linha):
        """Roda na instância que já está aberta: "supermenu" alterna; "--daemon" só carrega."""
        if self.janela is None:
            provedor = Gtk.CssProvider()
            provedor.load_from_string(CSS)
            # Prioridade ACIMA da do usuário (USER + 1), igual à central e à Dock: o ~/.config/gtk-4.0/gtk.css
            # liga "backdrop-filter: blur" em toda janela, e com prioridade menor ele vencia o nosso "none".
            # Resultado: o Hyprland desfocava o RETÂNGULO inteiro atrás do menu (um quadrado em volta
            # dos cantos redondos, doc 37).
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
            self.janela = SuperMenu(self)
            self.hold()       # continua rodando mesmo com a janela escondida
        args = linha.get_arguments()
        if "--clique-fora" in args:
            self.janela.clique_fora()
        elif "--daemon" not in args:
            self.janela.alternar()
        return 0


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
