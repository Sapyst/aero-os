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

No visual Solid (solido.py): superfícies tonais chapadas, chips e cartões em pílula, sem contornos, e o menu
NASCE da ilha do relógio da AeroBar num metaball (solido.brotar: a gota escorre e vira o painel), voltando
pra ela ao fechar. A GLArea do metaball só existe durante a animação.
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
from gi.repository import GdkPixbuf  # noqa: E402
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import apps  # noqa: E402
import clima  # noqa: E402
import solido  # noqa: E402

CSS_SOLIDO = """
window.solid .conteudo { background-color: @solid_surface_container; color: @solid_on_surface; border: none;
  border-radius: %(r)dpx; }
window.solid .caixa-imagem searchentry { border-radius: 999px; border: none; background: @solid_surface_container_highest;
  color: @solid_on_surface; }
window.solid .caixa-imagem searchentry text { color: @solid_on_surface; }
window.solid .caixa-imagem searchentry image { color: @solid_on_surface_variant; }
window.solid .previsao { border: none; border-radius: %(r2)dpx; background: alpha(black, 0.30); }
window.solid .modo { border: none; border-radius: 999px; background: alpha(black, 0.32); }
window.solid .modo:checked { background: @solid_primary; color: @solid_on_primary; }
window.solid .chip { border-radius: 999px; background: @solid_surface_container_high; padding: 8px 14px; }
window.solid .chip:checked { background: @solid_secondary_container; color: @solid_on_secondary_container; }
window.solid flowboxchild.cartao { border-radius: %(r2)dpx; background: @solid_surface_container_high; }
window.solid flowboxchild.cartao:hover { background: mix(@solid_surface_container_high, @solid_on_surface, 0.08); }
window.solid flowboxchild.cartao:selected { background: @solid_primary_container; color: @solid_on_primary_container; }
window.solid .titulo-secao { font-weight: 500; }
window.solid .rodape { border-top: none; }
window.solid .rodape button { border-radius: 999px; }
"""

LARGURA, ALTURA = 1080, 700     # tamanho do painel
LARGURA_CAIXA = 380             # a CAIXA DA IMAGEM à esquerda (wallpaper + busca + clima + seções)
PASTA_FAIXA = os.path.expanduser("~/.cache/aero-shell/menu-faixa")   # cópias pequenas da imagem
# A imagem da faixa: a mesma do desktop (padrão) ou uma só do menu (Settings → Wallpapers, doc 37)
IMAGEM_PROPRIA = os.path.expanduser("~/.local/share/aero-shell/fundos/menu")
FIXADOS_PADRAO = ["com.anthropic.Claude.desktop", "firefox.desktop", "com.aero.Settings.desktop",
                  "kitty.desktop", "thunar.desktop", "steam.desktop"]

CSS = """
/* O Super Menu é uma CAMADA (não uma janela/app, doc 33): janela transparente, cartão com a cor do tema.
   Layout (13/10, ideia do menu do binnewbs/arch-hyprland, escrito do zero aqui): à esquerda uma CAIXA
   DA IMAGEM grande (o wallpaper) com a busca em cima, o clima animado e as seções embaixo; à direita as
   categorias em "chips", os apps e o rodapé. Nada de desfoque/sombra grande em camada (doc 26). */
window.supermenu { background: transparent; backdrop-filter: none; }
.conteudo { padding: 12px; background-color: var(--popover-bg-color, @popover_bg_color); border-radius: 24px;
            border: 1px solid alpha(currentColor, 0.10); }
.caixa-imagem searchentry { min-height: 42px; border-radius: 14px; font-size: 15px;
                            background: alpha(black, 0.38); color: white; border: 1px solid alpha(white, 0.22); }
.caixa-imagem searchentry text { color: white; }
.caixa-imagem searchentry image { color: alpha(white, 0.8); }
.caixa-imagem .temp, .caixa-imagem .condicao, .caixa-imagem .detalhes, .caixa-imagem .dia-sem,
.caixa-imagem .dia-temp { color: white; text-shadow: 0 1px 4px rgba(0,0,0,0.55); }
.caixa-imagem .temp { font-size: 54px; font-weight: 200; }
.caixa-imagem .condicao { font-size: 17px; font-weight: 700; }
.caixa-imagem .detalhes { font-size: 12.5px; opacity: 0.92; }
.caixa-imagem .dia-sem { font-size: 11px; font-weight: 700; opacity: 0.85; }
.caixa-imagem .dia-temp { font-size: 11.5px; }
.previsao { background: alpha(black, 0.28); border-radius: 14px; padding: 8px 6px; border: 1px solid alpha(white, 0.12); }
.modo { padding: 9px 10px; border-radius: 12px; background: alpha(black, 0.32); color: white; font-weight: 600;
        border: 1px solid alpha(white, 0.14); }
.modo:checked { background: var(--accent-bg-color); color: var(--accent-fg-color); border-color: transparent; }
.modo:hover { background: alpha(black, 0.45); }
.modo:checked:hover { background: var(--accent-bg-color); }
.lado { padding: 6px 6px 0 18px; }
.chip { padding: 6px 12px; border-radius: 99px; background: alpha(currentColor, 0.06); font-size: 13px; }
.chip:checked { background: alpha(var(--accent-bg-color), 0.22); font-weight: 600; }
flowboxchild.cartao { padding: 10px 12px; border-radius: 14px; background: alpha(currentColor, 0.045); }
flowboxchild.cartao:selected { background: var(--accent-bg-color); color: var(--accent-fg-color); }
.cartao .nome { font-weight: 600; }
.cartao .descricao { font-size: 12px; opacity: 0.7; }
flowboxchild.cartao:selected .descricao { opacity: 0.9; }
.titulo-secao { font-size: 22px; font-weight: 700; }
.rodape { padding-top: 10px; border-top: 1px solid alpha(currentColor, 0.10); }
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
        self.solido = visual_atual() == "Solid"
        self.anim_s = None
        if self.solido:              # Solid: encosta na ilha do relógio e o painel nasce dela (metaball)
            self.add_css_class("solid")
            self.cartao_s = self.montar_conteudo()
            self.cartao_s.set_margin_top(12)
            self.raiz_s = Gtk.Overlay()
            self.raiz_s.set_child(self.cartao_s)
            self.set_child(self.raiz_s)
            Camada.set_margin(self, Camada.Edge.TOP, 0)
        else:
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
        """A CAIXA DA IMAGEM (esquerda): o wallpaper inteiro na altura do menu e, por cima, a busca,
        o clima de agora (ícone ANIMADO), a previsão dos próximos dias e os botões de seção."""
        faixa = Gtk.Overlay(width_request=LARGURA_CAIXA, vexpand=True)
        faixa.add_css_class("caixa-imagem")
        self.pixbuf_faixa = None
        self.imagem_faixa = Gtk.DrawingArea(vexpand=True)
        self.imagem_faixa.set_draw_func(self.desenhar_faixa)
        faixa.set_child(self.imagem_faixa)
        camada = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=16, margin_bottom=16,
                         margin_start=16, margin_end=16)
        self.busca = Gtk.SearchEntry(placeholder_text="Search apps")
        self.busca.connect("search-changed", lambda *_: self.mostrar())
        self.busca.connect("activate", lambda *_: self.abrir_selecionado())
        camada.append(self.busca)
        camada.append(Gtk.Box(vexpand=True))                    # empurra o clima pra parte de baixo
        # clima de agora: ícone animado (ícones da amCharts via Caule pack, svg_animado.py) + textos
        self.clima = None
        self.linha_clima = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        topo = Gtk.Box(spacing=10)
        self.desenho_clima = Gtk.DrawingArea(content_width=92, content_height=92, valign=Gtk.Align.CENTER)
        self.desenho_clima.set_draw_func(self.desenhar_clima)
        topo.append(self.desenho_clima)
        self.temp = Gtk.Label(xalign=0, valign=Gtk.Align.CENTER)
        self.temp.add_css_class("temp")
        topo.append(self.temp)
        self.linha_clima.append(topo)
        self.condicao = Gtk.Label(xalign=0)
        self.condicao.add_css_class("condicao")
        self.detalhes = Gtk.Label(xalign=0, wrap=True)
        self.detalhes.add_css_class("detalhes")
        self.linha_clima.append(self.condicao)
        self.linha_clima.append(self.detalhes)
        self.previsao = Gtk.Box(homogeneous=True, spacing=2, margin_top=8)
        self.previsao.add_css_class("previsao")
        self.linha_clima.append(self.previsao)
        camada.append(self.linha_clima)
        # seções (como os "modos" do rofi): Pinned / Recent / All apps
        modos = Gtk.Box(spacing=8, homogeneous=True)
        self.botoes_secao = {}
        grupo = None
        for chave, nome, icone in SECOES:
            b = Gtk.ToggleButton(group=grupo)
            grupo = grupo or b
            b.set_child(Adw.ButtonContent(label=nome, icon_name=icone))
            b.add_css_class("modo")
            b.connect("toggled", lambda bt, c=chave: bt.get_active() and self.escolher_secao(c))
            modos.append(b)
            self.botoes_secao[chave] = b
        camada.append(modos)
        faixa.add_overlay(camada)
        self.animando = None
        return faixa

    def desenhar_faixa(self, _area, cr, w, h):
        """Imagem "cover" (preenche cortando as sobras) + degradês, tudo dentro do contorno arredondado."""
        import cairo
        r = 18
        cr.new_sub_path()                                  # caixa com os 4 cantos redondos
        cr.arc(r, r, r, math.pi, 1.5 * math.pi)
        cr.arc(w - r, r, r, 1.5 * math.pi, 2 * math.pi)
        cr.arc(w - r, h - r, r, 0, 0.5 * math.pi)
        cr.arc(r, h - r, r, 0.5 * math.pi, math.pi)
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
        # degradês escuros em cima (busca) e embaixo (clima e seções) — leitura em qualquer imagem
        for x0, y0, x1, y1, paradas in ((0, 0, 0, h, ((0, 0.35), (0.18, 0.05), (0.3, 0))),
                                        (0, h, 0, 0, ((0, 0.72), (0.45, 0.30), (0.75, 0)))):
            g = cairo.LinearGradient(x0, y0, x1, y1)
            for pos, a in paradas:
                g.add_color_stop_rgba(pos, 0, 0, 0, a)
            cr.set_source(g)
            cr.paint()

    def desenhar_clima(self, _area, cr, largura, altura):
        if self.clima:
            import time
            import svg_animado
            try:
                svg_animado.icone_clima(self.clima.get("desenho", "nublado"), self.clima.get("dia", True)).desenhar(
                    cr, 0, 0, min(largura, altura), time.monotonic())
            except Exception:  # noqa: BLE001 — sem o SVG: o desenho de sempre
                clima.desenhar(cr, min(largura, altura), self.clima.get("desenho", "nublado"), self.clima.get("dia", True))

    def animar_clima(self, ligar):
        """O ícone se mexe só com o menu aberto (20 quadros/s); fechado não gasta nada."""
        if ligar and self.animando is None:
            self.animando = GLib.timeout_add(50, lambda: (self.desenho_clima.queue_draw(), True)[1])
        elif not ligar and self.animando is not None:
            GLib.source_remove(self.animando)
            self.animando = None

    def mostrar_previsao(self, dias):
        filho = self.previsao.get_first_child()
        while filho:
            prox = filho.get_next_sibling()
            self.previsao.remove(filho)
            filho = prox
        import datetime
        for d in (dias or [])[1:6]:
            col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            col.append(Gtk.Label(label=datetime.date.fromisoformat(d["data"]).strftime("%a"), css_classes=["dia-sem"]))
            area = Gtk.DrawingArea(content_width=30, content_height=30, halign=Gtk.Align.CENTER)
            area.set_draw_func(lambda _a, cr, w, h, dd=d: self.icone_pequeno(cr, min(w, h), dd))
            col.append(area)
            col.append(Gtk.Label(label=f"{d['max']}° {d['min']}°", css_classes=["dia-temp"]))
            self.previsao.append(col)
        self.previsao.set_visible(bool(dias))
        return False

    @staticmethod
    def icone_pequeno(cr, tam, d):
        import svg_animado
        try:
            svg_animado.icone_clima(d.get("desenho", "nublado"), True).desenhar(cr, 0, 0, tam, 0.0)
        except Exception:  # noqa: BLE001
            clima.desenhar(cr, tam, d.get("desenho", "nublado"), True)

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
        def semana():
            try:
                dias = (clima.obter_semana(dias=6) or {}).get("dias") or []
            except Exception:  # noqa: BLE001
                dias = []
            GLib.idle_add(self.mostrar_previsao, dias)
        threading.Thread(target=semana, daemon=True).start()

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
        cartao = Gtk.Box(spacing=0)
        cartao.set_size_request(LARGURA - 34, ALTURA - 64)
        cartao.add_css_class("conteudo")
        cartao.append(self.montar_faixa())
        direita = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, hexpand=True)
        direita.add_css_class("lado")
        self.titulo = Gtk.Label(xalign=0)
        self.titulo.add_css_class("titulo-secao")
        direita.append(self.titulo)
        # categorias em "chips" (o que antes era a coluna da esquerda)
        chips = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=8, row_spacing=6,
                            column_spacing=6)
        self.chips = {}
        for chave, nome, _ in apps.CATEGORIAS:
            if chave == "todos":
                continue
            b = Gtk.ToggleButton(label=nome, group=self.botoes_secao["fixados"])
            b.add_css_class("chip")
            b.connect("toggled", lambda bt, c=chave: bt.get_active() and self.escolher_secao(c))
            chips.append(b)
            self.chips[chave] = b
        direita.append(chips)
        self.grade = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.SINGLE, activate_on_single_click=True,
                                 homogeneous=True, max_children_per_line=2, min_children_per_line=2,
                                 row_spacing=8, column_spacing=8, valign=Gtk.Align.START)
        self.grade.connect("child-activated", lambda _g, filho: self.ativar(filho))
        self.vazio = Gtk.Label(label="Nothing here yet", vexpand=True, visible=False)
        self.vazio.add_css_class("dim-label")
        direita.append(self.vazio)
        direita.append(Gtk.ScrolledWindow(child=self.grade, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER))
        rodape = Gtk.Box(spacing=8)
        rodape.add_css_class("rodape")
        nome = GLib.get_real_name()
        avatar = Adw.Avatar(size=24, text=GLib.get_user_name() if nome in ("", "Unknown") else nome,
                            show_initials=True)
        try:
            # miniatura (a ~/.face pode ser uma foto de 30 MP: decodificar inteira travava o menu; ver som.py)
            avatar.set_custom_image(Gdk.Texture.new_for_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_scale(
                os.path.expanduser("~/.face"), 96, 96, True)))
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
        direita.append(rodape)
        cartao.append(direita)
        return cartao

    def escolher_secao(self, chave):
        self.secao = chave
        if self.busca.get_text():
            self.busca.set_text("")        # dispara mostrar() pelo search-changed
        else:
            self.mostrar()

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
        itens.append(("Add to Atlas", lambda: self.adicionar_dock(app)))
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
        if self.get_visible() and not getattr(self, "fechando", False):
            self.fechar()
        else:
            self.abrir()

    def abrir(self):
        self.todos_apps = apps.todos()
        self.fixados = apps.ler_lista("menu-fixados", FIXADOS_PADRAO)
        self.busca.set_text("")
        self.secao = "fixados"
        self.botoes_secao["fixados"].set_active(True)
        self.mostrar()
        self.atualizar_faixa()
        self.animar_clima(True)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)   # digitar já vai pra busca
        self.present()
        self.busca.grab_focus()
        if self.solido:
            self.fechando = False
            self.brotar(True)

    def brotar(self, abrindo, depois=None):
        """Solid: o painel nasce da ilha do relógio (ou volta pra ela). solido.brotar = o código comum."""
        if self.anim_s is not None:
            self.anim_s.parar()
        largura = self.get_width() or LARGURA
        x_rel = solido.barra().get("icones", {}).get("clock")
        try:
            mon = Gdk.Display.get_default().get_monitor_at_surface(self.get_surface())
            tela = mon.get_geometry().width
        except (AttributeError, TypeError):
            tela = 2560
        ix = (x_rel - (tela - largura) / 2) if x_rel is not None else largura / 2
        ix = min(max(20.0, ix), largura - 20.0)

        def retangulos():
            ok, b = self.cartao_s.compute_bounds(self.raiz_s)
            return [(b.get_x(), b.get_y(), b.get_width(), b.get_height())] if ok and b.get_width() > 4 else []

        def fim():
            self.anim_s = None
            if depois:
                depois()
        self.anim_s = solido.brotar(self.raiz_s, retangulos, self.cartao_s, ix, abrindo, fim,
                                    dur=0.5 if abrindo else 0.26, k=30.0)

    def fechar(self):
        self.animar_clima(False)
        if self.popover is not None:
            self.popover.popdown()
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.NONE)
        if self.get_visible() and self.solido:
            self.fechando = True

            def sumiu():
                self.fechando = False
                self.set_visible(False)
                self.cartao_s.set_opacity(1.0)
            self.brotar(False, sumiu)
        elif self.get_visible():
            self.set_visible(False)

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


def visual_atual():
    try:
        for linha in open(os.path.expanduser("~/.config/hypr/painel.conf")):
            if linha.startswith("VISUAL="):
                return linha.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "AeroTerm"


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.SuperMenu",
                         flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE)
        self.janela = None
        self.nuvem = None          # DotStyle: the bubble cloud (nuvem.py), made on first use

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
            if visual_atual() == "Solid":
                solido.aplicar_css()
                prov_s = Gtk.CssProvider()
                r = solido.raio()
                prov_s.load_from_string(CSS_SOLIDO % {"r": r, "r2": max(12, r - 8)})
                Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov_s,
                                                          Gtk.STYLE_PROVIDER_PRIORITY_USER + 2)
                self.vigia_cores = solido.vigiar()
            self.janela = SuperMenu(self)
            self.hold()       # continua rodando mesmo com a janela escondida
        args = linha.get_arguments()
        dots = visual_atual() == "DotStyle"
        if "--clique-fora" in args:
            if not dots:
                self.janela.clique_fora()      # the cloud covers the screen and closes on its own empty clicks
        elif "--daemon" not in args:
            if dots:
                if self.janela.get_visible():
                    self.janela.fechar()
                if self.nuvem is None:
                    import nuvem
                    self.nuvem = nuvem.Nuvem(self)
                self.nuvem.alternar()
            else:
                if self.nuvem is not None and self.nuvem.get_visible():
                    self.nuvem.fechar()
                self.janela.alternar()
        return 0


if __name__ == "__main__":
    sys.exit(App().run(sys.argv))
