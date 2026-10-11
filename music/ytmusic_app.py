#!/usr/bin/env python3
"""
YTMusic — app nativo (GTK4/libadwaita) do YouTube Music, no layout do Euphonica:

  ┌ menu ──────────┬ lista ──────────────────┬ Now Playing ─────────┐
  │ Search         │ (busca / recentes /     │   capa grande        │
  │ Recent         │  playlist / fila)       │   título · artista   │
  │ Liked          │  capa · título · artista│   letra sincronizada │
  │ Playlists...   │  ▲ ▼ ✕ na fila          │   ⟲ ⏮ ⏯ ⏭ ⤮          │
  │ Queue      12  │                         │   ━━━━○━━━━━  1:02   │
  └────────────────┴─────────────────────────┴──────────────────────┘

Quem toca é o motor (motor.py, mpv + sua conta via login do Firefox). O TerMusic usa o mesmo motor:
os dois mostram e controlam a mesma música. Fundo de vidro: a janela é semitransparente e o Hyprland
desfoca o que está atrás. Cores: as do tema do sistema (~/.config/waybar/cores.css).
"""
import hashlib
import os
import re
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gsk, Gtk, Pango  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cliente import ErroMotor, chamar  # noqa: E402

CAPAS = os.path.expanduser("~/.cache/ytmotor/capas")
CORES = os.path.expanduser("~/.config/waybar/cores.css")
_REAL = GLib.get_real_name()
NOME = os.environ.get("YTMUSIC_NOME") or (_REAL if _REAL not in ("", "Unknown") else GLib.get_user_name())
piscina = ThreadPoolExecutor(max_workers=6)


# ---------------- utilidades ----------------
def em_fundo(funcao, depois=None):
    def tarefa():
        try:
            r = funcao()
        except Exception as e:  # noqa: BLE001
            r = e
        if depois:
            GLib.idle_add(lambda: (depois(r), False)[1])
    piscina.submit(tarefa)


def tempo(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}" if s < 3600 else f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def cores_do_tema():
    c = {"fundo-solido": "#1e1e1e", "texto": "#f0f0f0", "destaque": "#f0f0f0"}
    try:
        for nome, valor in re.findall(r"@define-color\s+([\w-]+)\s+([^;]+);", open(CORES).read()):
            c[nome] = valor.strip()
    except OSError:
        pass
    return c


def rgb(hexa):
    hexa = hexa.lstrip("#")
    return ",".join(str(int(hexa[i:i + 2], 16)) for i in (0, 2, 4))


class Imagens:
    """Baixa e guarda as capas (~/.cache/ytmotor/capas) e põe no Gtk.Picture quando chegam."""
    memoria = {}

    @classmethod
    def por(cls, url, picture):
        if not url:
            picture.set_paintable(None)
            return
        picture._url = url
        if url in cls.memoria:
            picture.set_paintable(cls.memoria[url])
            return
        picture.set_paintable(None)

        def baixar():
            os.makedirs(CAPAS, exist_ok=True)
            arq = os.path.join(CAPAS, hashlib.sha1(url.encode()).hexdigest() + ".img")
            if not os.path.exists(arq):
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ytmusic"}),
                                            timeout=15) as r:
                    dados = r.read(8_000_000)
                with open(arq + ".tmp", "wb") as f:
                    f.write(dados)
                os.replace(arq + ".tmp", arq)
            return arq

        def pronto(arq):
            if isinstance(arq, Exception):
                return
            try:
                tex = Gdk.Texture.new_from_filename(arq)
            except GLib.Error:
                return
            cls.memoria[url] = tex
            if getattr(picture, "_url", None) == url:
                picture.set_paintable(tex)
        em_fundo(baixar, pronto)


class Capa(Gtk.Widget):
    """Capa de tamanho FIXO (o Gtk.Picture cresce até o tamanho da imagem), recortada em quadrado."""

    def __init__(self, tamanho):
        super().__init__(width_request=tamanho, height_request=tamanho, overflow=Gtk.Overflow.HIDDEN,
                         valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER)
        self.tamanho = tamanho
        self.textura = None
        self.raio = 16 if tamanho >= 150 else 8

    def set_paintable(self, tex):
        self.textura = tex
        self.queue_draw()

    def do_measure(self, orientation, for_size):
        return self.tamanho, self.tamanho, -1, -1

    def do_snapshot(self, snap):
        if not self.textura:
            return
        w, h = self.get_width(), self.get_height()
        tw, th = self.textura.get_width(), self.textura.get_height()
        escala = max(w / tw, h / th)            # "cover": preenche e corta as sobras
        dw, dh = tw * escala, th * escala
        gi.require_version("Graphene", "1.0")
        from gi.repository import Graphene
        r = Graphene.Rect()
        r.init((w - dw) / 2, (h - dh) / 2, dw, dh)
        caixa = Graphene.Rect()
        caixa.init(0, 0, w, h)
        canto = Gsk.RoundedRect()
        canto.init_from_rect(caixa, self.raio)              # cantos arredondados de verdade
        snap.push_rounded_clip(canto)
        snap.append_scaled_texture(self.textura, Gsk.ScalingFilter.TRILINEAR, r)
        snap.pop()


def capa_widget(tamanho, raio="capa"):
    c = Capa(tamanho)
    c.add_css_class(raio)
    return c, c


def botao(icone, dica, acao, *classes):
    b = Gtk.Button(icon_name=icone, tooltip_text=dica, valign=Gtk.Align.CENTER)
    b.add_css_class("flat")
    for c in classes:
        b.add_css_class(c)
    b.connect("clicked", lambda *_: acao())
    return b


def css():
    c = cores_do_tema()
    fundo, texto = c["fundo-solido"], c["texto"]
    return f"""
window.ytm {{ background-color: rgba({rgb(fundo)}, 0.62); }}
window.ytm .lateral {{ background-color: rgba({rgb(fundo)}, 0.45); }}
window.ytm .meio {{ background-color: rgba({rgb(fundo)}, 0.28);
  border-left: 1px solid rgba({rgb(texto)}, 0.07); border-right: 1px solid rgba({rgb(texto)}, 0.07); }}
window.ytm headerbar {{ background: transparent; box-shadow: none; }}
window.ytm list, window.ytm scrolledwindow, window.ytm viewport {{ background: transparent; }}
.marca {{ font-weight: 800; font-size: 15px; }}
.dono {{ font-size: 12px; opacity: .55; }}
.secao {{ font-size: 11px; font-weight: 700; letter-spacing: .08em; opacity: .45; margin: 14px 12px 4px; }}
.lateral row {{ border-radius: 10px; margin: 1px 8px; padding: 2px 4px; }}
.lateral row:selected {{ background: rgba({rgb(texto)}, 0.10); }}
.contagem {{ font-weight: 700; opacity: .7; }}
.titulo-lista {{ font-weight: 700; font-size: 15px; }}
.sub-lista {{ font-size: 12px; opacity: .55; }}
.faixa {{ border-bottom: 1px solid rgba({rgb(texto)}, 0.06); padding: 6px 8px; }}
.faixa:hover {{ background: rgba({rgb(texto)}, 0.05); }}
.faixa.atual {{ background: rgba({rgb(texto)}, 0.11); }}
.faixa-titulo {{ font-weight: 650; }}
.faixa-info {{ font-size: 12px; opacity: .55; }}
.capa {{ border-radius: 8px; border: none; background: rgba({rgb(texto)}, 0.06); }}
.capa-grande {{ border-radius: 16px; border: none; background: rgba({rgb(texto)}, 0.06);
  box-shadow: 0 10px 40px rgba(0,0,0,.35); }}
.np-titulo {{ font-weight: 800; font-size: 19px; }}
.np-artista {{ font-size: 14px; opacity: .8; }}
.np-album {{ font-size: 13px; opacity: .55; }}
.letra {{ font-size: 15px; opacity: .32; padding: 3px 12px; transition: all 200ms ease; }}
.letra.atual {{ opacity: 1; font-weight: 650; font-size: 16px; }}
.letra.passada {{ opacity: .45; }}
.tocar {{ min-width: 52px; min-height: 52px; border-radius: 999px; background: rgba({rgb(texto)}, 0.12); }}
.tocar:hover {{ background: rgba({rgb(texto)}, 0.2); }}
.ligado {{ color: {c.get('destaque', texto)}; background: rgba({rgb(texto)}, 0.12); }}
.tempo {{ font-size: 12px; font-feature-settings: "tnum"; opacity: .7; }}
scale.progresso trough {{ min-height: 4px; border-radius: 99px; background: rgba({rgb(texto)}, 0.14); }}
scale.progresso highlight {{ background: {texto}; border-radius: 99px; }}
scale.progresso slider {{ background: {texto}; box-shadow: none; }}
.vazio {{ opacity: .45; }}
searchbar, entry.busca {{ border-radius: 999px; }}
"""


# ---------------- linha de faixa ----------------
class LinhaFaixa(Gtk.ListBoxRow):
    def __init__(self, janela, faixa, indice, na_fila):
        super().__init__()
        self.faixa, self.indice = faixa, indice
        self.add_css_class("faixa")
        if na_fila and faixa.get("atual"):
            self.add_css_class("atual")
        caixa = Gtk.Box(spacing=12)
        moldura, pic = capa_widget(44)
        Imagens.por(faixa.get("capa"), pic)
        caixa.append(moldura)
        textos = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True, spacing=2)
        t = Gtk.Label(label=faixa.get("titulo") or "?", xalign=0, ellipsize=Pango.EllipsizeMode.END)
        t.add_css_class("faixa-titulo")
        info = " · ".join(x for x in (faixa.get("artista"), faixa.get("album")) if x)
        i = Gtk.Label(label=info, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        i.add_css_class("faixa-info")
        textos.append(t)
        textos.append(i)
        caixa.append(textos)
        if faixa.get("duracao"):
            d = Gtk.Label(label=tempo(faixa["duracao"]))
            d.add_css_class("tempo")
            caixa.append(d)
        if na_fila:
            caixa.append(botao("go-up-symbolic", "Move up", lambda: janela.mover(indice, indice - 1)))
            caixa.append(botao("go-down-symbolic", "Move down", lambda: janela.mover(indice, indice + 1)))
            caixa.append(botao("list-remove-symbolic", "Remove", lambda: janela.remover(indice)))
        else:
            caixa.append(botao("list-add-symbolic", "Add to queue", lambda: janela.enfileirar([faixa])))
            caixa.append(botao("media-playlist-consecutive-symbolic", "Play next",
                               lambda: janela.tocar_depois([faixa])))
            caixa.append(botao("ytmusic-radio-symbolic", "Start radio from this song", lambda: janela.radio(faixa)))
        self.set_child(caixa)


# ---------------- janela ----------------
class Janela(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="YTMusic", default_width=1240, default_height=820)
        self.add_css_class("ytm")
        self.set_size_request(900, 560)
        self.visao = None
        self.lista = []            # faixas da visão do meio
        self.estado = {}
        self.ids_fila = None
        self.id_letra = None
        self.letra = None
        self.ref = (0.0, time.time(), False)   # (pos, quando, tocando) pra interpolar o tempo
        self.arrastando = False

        raiz = Gtk.Box()
        raiz.append(self.montar_lateral())
        raiz.append(self.montar_meio())
        raiz.append(self.montar_agora())
        self.toasts = Adw.ToastOverlay(child=raiz)
        self.set_content(self.toasts)

        teclas = Gtk.EventControllerKey()
        teclas.connect("key-pressed", self.tecla)
        self.add_controller(teclas)

        self.escolher_visao("fila")
        self.atualizar()
        GLib.timeout_add(700, self.atualizar)
        GLib.timeout_add(150, self.tique)
        em_fundo(lambda: chamar("playlists"), self.mostrar_playlists)

    # ---- coluna 1: menu ----
    def montar_lateral(self):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=230)
        caixa.add_css_class("lateral")
        cab = Adw.HeaderBar(show_end_title_buttons=False)
        titulo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        m = Gtk.Label(label="YTMusic")
        m.add_css_class("marca")
        d = Gtk.Label(label=NOME)
        d.add_css_class("dono")
        titulo.append(m)
        titulo.append(d)
        cab.set_title_widget(titulo)
        caixa.append(cab)

        self.menu = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.menu.add_css_class("navigation-sidebar")
        self.itens_menu = {}
        for chave, icone, nome in (("busca", "system-search-symbolic", "Search"),
                                   ("recentes", "document-open-recent-symbolic", "Recent"),
                                   ("curtidas", "starred-symbolic", "Liked"),
                                   ("fila", "view-list-symbolic", "Queue")):
            linha = Gtk.ListBoxRow()
            b = Gtk.Box(spacing=12, margin_top=8, margin_bottom=8, margin_start=6, margin_end=6)
            b.append(Gtk.Image.new_from_icon_name(icone))
            b.append(Gtk.Label(label=nome, xalign=0, hexpand=True))
            if chave == "fila":
                self.contagem = Gtk.Label(label="")
                self.contagem.add_css_class("contagem")
                b.append(self.contagem)
            linha.set_child(b)
            linha.chave = chave
            self.itens_menu[chave] = linha
            self.menu.append(linha)
        self.menu.connect("row-activated", lambda _l, r: self.escolher_visao(r.chave))
        caixa.append(self.menu)

        s = Gtk.Label(label="PLAYLISTS", xalign=0)
        s.add_css_class("secao")
        caixa.append(s)
        self.lista_playlists = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.lista_playlists.add_css_class("navigation-sidebar")
        self.lista_playlists.connect("row-activated", lambda _l, r: self.escolher_visao("playlist:" + r.pid, r.nome))
        rolagem = Gtk.ScrolledWindow(child=self.lista_playlists, vexpand=True,
                                     hscrollbar_policy=Gtk.PolicyType.NEVER)
        caixa.append(rolagem)
        return caixa

    def mostrar_playlists(self, pls):
        if isinstance(pls, Exception):
            self.avisar(f"Couldn't load playlists: {pls}")
            return
        for p in pls:
            if p["id"] == "LM":
                continue                       # "Liked" já está no menu
            linha = Gtk.ListBoxRow()
            b = Gtk.Box(spacing=10, margin_top=4, margin_bottom=4, margin_start=4, margin_end=4)
            moldura, pic = capa_widget(30)
            Imagens.por(p.get("capa"), pic)
            b.append(moldura)
            b.append(Gtk.Label(label=p["titulo"], xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END))
            if p.get("qtd"):
                q = Gtk.Label(label=str(p["qtd"]))
                q.add_css_class("dono")
                b.append(q)
            linha.set_child(b)
            linha.pid, linha.nome = p["id"], p["titulo"]
            self.lista_playlists.append(linha)

    # ---- coluna 2: lista ----
    def montar_meio(self):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=440, hexpand=True)
        caixa.add_css_class("meio")
        cab = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        t = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER)
        self.titulo_lista = Gtk.Label(label="Queue")
        self.titulo_lista.add_css_class("titulo-lista")
        self.sub_lista = Gtk.Label(label="")
        self.sub_lista.add_css_class("sub-lista")
        t.append(self.titulo_lista)
        t.append(self.sub_lista)
        cab.set_title_widget(t)
        # listas (busca, recentes, playlists): tocar tudo, tocar embaralhado, pôr tudo na fila
        self.bt_tocar_tudo = botao("media-playback-start-symbolic", "Play all", lambda: self.tocar_lista(0))
        self.bt_tocar_embaralhado = botao("media-playlist-shuffle-symbolic", "Shuffle play",
                                          lambda: self.tocar_embaralhado())
        self.bt_enfileirar_tudo = botao("list-add-symbolic", "Add all to queue",
                                        lambda: self.lista and self.enfileirar(self.lista))
        # fila: embaralhar, rádio da música atual, limpar
        self.bt_embaralhar = botao("media-playlist-shuffle-symbolic", "Shuffle queue",
                                   lambda: self.comando("embaralhar"))
        self.bt_radio_atual = botao("ytmusic-radio-symbolic", "Radio from the current song",
                                    lambda: self.estado.get("atual") and self.radio(self.estado["atual"]))
        self.bt_limpar = botao("edit-clear-all-symbolic", "Clear queue", lambda: self.comando("limpar"))
        cab.pack_start(self.bt_tocar_tudo)
        cab.pack_start(self.bt_tocar_embaralhado)
        cab.pack_end(self.bt_enfileirar_tudo)
        cab.pack_start(self.bt_embaralhar)
        cab.pack_start(self.bt_radio_atual)
        cab.pack_end(self.bt_limpar)
        caixa.append(cab)

        self.busca = Gtk.SearchEntry(placeholder_text="Search songs, artists, albums…", margin_start=12,
                                     margin_end=12, margin_bottom=8)
        self.busca.add_css_class("busca")
        self.busca.connect("activate", lambda e: self.buscar(e.get_text()))
        caixa.append(self.busca)

        self.pilha = Gtk.Stack()
        self.lista_meio = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.lista_meio.connect("row-activated", self.ao_ativar_faixa)
        self.pilha.add_named(Gtk.ScrolledWindow(child=self.lista_meio, vexpand=True,
                                                hscrollbar_policy=Gtk.PolicyType.NEVER), "lista")
        self.status = Adw.StatusPage(icon_name="folder-music-symbolic", title="", vexpand=True)
        self.pilha.add_named(self.status, "status")
        self.carregando = Gtk.Spinner(spinning=True, width_request=32, height_request=32,
                                      halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.pilha.add_named(self.carregando, "carregando")
        caixa.append(self.pilha)
        return caixa

    # ---- coluna 3: Now Playing ----
    def montar_agora(self):
        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, width_request=420)
        cab = Adw.HeaderBar(show_start_title_buttons=False)
        np = Gtk.Label(label="Now Playing")
        np.add_css_class("titulo-lista")
        cab.set_title_widget(np)
        menu = Gio.Menu()
        sec1 = Gio.Menu()
        sec1.append("Autoplay similar songs", "win.autoplay")
        sec1.append("Radio from this song", "win.radio-atual")
        menu.append_section(None, sec1)
        sec2 = Gio.Menu()
        sec2.append("Open TerMusic", "win.termusic")
        sec2.append("TerMusic widget", "win.widget")
        menu.append_section(None, sec2)
        sec3 = Gio.Menu()
        sec3.append("Reload library", "win.recarregar")
        sec3.append("Keyboard shortcuts", "win.atalhos")
        menu.append_section(None, sec3)
        cab.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
        caixa.append(cab)
        self.criar_acoes()

        corpo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_start=22, margin_end=22,
                        margin_bottom=18, vexpand=True)
        moldura, self.capa = capa_widget(250, "capa-grande")
        moldura.set_margin_top(6)
        corpo.append(moldura)
        self.np_titulo = Gtk.Label(label="Nothing playing", wrap=True, justify=Gtk.Justification.CENTER,
                                   margin_top=12)
        self.np_titulo.add_css_class("np-titulo")
        self.np_artista = Gtk.Label(label="", ellipsize=Pango.EllipsizeMode.END)
        self.np_artista.add_css_class("np-artista")
        self.np_album = Gtk.Label(label="", ellipsize=Pango.EllipsizeMode.END)
        self.np_album.add_css_class("np-album")
        for w in (self.np_titulo, self.np_artista, self.np_album):
            corpo.append(w)

        self.letras = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.rolagem_letra = Gtk.ScrolledWindow(child=self.letras, vexpand=True, margin_top=8,
                                                hscrollbar_policy=Gtk.PolicyType.NEVER)
        corpo.append(self.rolagem_letra)

        controles = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=6)
        self.bt_repetir = botao("media-playlist-repeat-symbolic", "Repeat (off → queue → one)",
                                lambda: self.comando("repetir"))
        self.bt_play = botao("media-playback-start-symbolic", "Play / pause (Space)", lambda: self.comando("pausar"),
                             "tocar")
        self.bt_radio = botao("ytmusic-autoplay-symbolic", "Autoplay similar songs when the queue ends",
                              lambda: self.comando("auto_radio"))
        for w in (self.bt_repetir, botao("media-skip-backward-symbolic", "Previous", lambda: self.comando("anterior")),
                  self.bt_play, botao("media-skip-forward-symbolic", "Next", lambda: self.comando("proxima")),
                  self.bt_radio):
            controles.append(w)
        corpo.append(controles)

        self.progresso = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, hexpand=True)
        self.progresso.add_css_class("progresso")
        self.progresso.set_range(0, 1)
        self.progresso.connect("change-value", self.ao_buscar_tempo)
        corpo.append(self.progresso)
        tempos = Gtk.Box()
        self.t_pos = Gtk.Label(label="0:00", xalign=0, hexpand=True)
        self.t_dur = Gtk.Label(label="0:00", xalign=1)
        vol_icone = Gtk.Image.new_from_icon_name("audio-volume-medium-symbolic")
        self.volume = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, draw_value=False, width_request=110)
        self.volume.add_css_class("progresso")
        self.volume.set_range(0, 100)
        self.volume.connect("change-value", lambda _s, _t, v: (self.comando("volume", v=v), False)[1])
        for w in (self.t_pos, self.t_dur):
            w.add_css_class("tempo")
        meio = Gtk.Box(spacing=4, halign=Gtk.Align.CENTER, hexpand=True)
        meio.append(vol_icone)
        meio.append(self.volume)
        tempos.append(self.t_pos)
        tempos.append(meio)
        tempos.append(self.t_dur)
        corpo.append(tempos)
        caixa.append(corpo)
        return caixa

    # ---- menu ----
    def criar_acoes(self):
        self.acao_autoplay = Gio.SimpleAction.new_stateful("autoplay", None, GLib.Variant.new_boolean(True))
        self.acao_autoplay.connect("activate", lambda *_: self.comando("auto_radio"))
        self.add_action(self.acao_autoplay)
        for nome, f in (("radio-atual", lambda: self.estado.get("atual") and self.radio(self.estado["atual"])),
                        ("termusic", lambda: Gio.Subprocess.new([os.path.expanduser("~/.local/bin/termusic")], 0)),
                        ("widget", lambda: Gio.Subprocess.new([os.path.expanduser("~/.local/bin/termusic"), "--widget"], 0)),
                        ("recarregar", self.recarregar), ("atalhos", self.mostrar_atalhos)):
            a = Gio.SimpleAction.new(nome, None)
            a.connect("activate", lambda *_a, f=f: f())
            self.add_action(a)

    def recarregar(self):
        while (f := self.lista_playlists.get_first_child()) is not None:
            self.lista_playlists.remove(f)
        em_fundo(lambda: chamar("playlists"), self.mostrar_playlists)
        if self.visao not in ("fila", "busca"):
            self.escolher_visao(self.visao, self.titulo_lista.get_label())
        self.avisar("Library reloaded")

    def mostrar_atalhos(self):
        d = Adw.ShortcutsDialog()
        for titulo, itens in (("Playback", (("Play / pause", "space"), ("Next", "<Control>Right"),
                                            ("Previous", "<Control>Left"))),
                              ("Library", (("Search", "<Control>f"), ("Queue", "<Control>1"), ("Recent", "<Control>2"),
                                           ("Liked", "<Control>3"))),
                              ("Lists", (("Play the song", "Return"),))):
            sec = Adw.ShortcutsSection(title=titulo)
            for nome, acel in itens:
                sec.add(Adw.ShortcutsItem(title=nome, accelerator=acel))
            d.add(sec)
        d.present(self)

    # ---- visões ----
    def escolher_visao(self, chave, nome=None):
        self.visao = chave
        self.busca.set_visible(chave == "busca")
        for b in (self.bt_embaralhar, self.bt_limpar, self.bt_radio_atual):
            b.set_visible(chave == "fila")
        for b in (self.bt_tocar_tudo, self.bt_tocar_embaralhado, self.bt_enfileirar_tudo):
            b.set_visible(chave != "fila")
        linha = self.itens_menu.get(chave)
        if linha:
            self.menu.select_row(linha)
            self.lista_playlists.unselect_all()
        else:
            self.menu.unselect_all()
        if chave == "fila":
            self.titulo_lista.set_label("Queue")
            self.ids_fila = None
            self.mostrar_fila(self.estado.get("fila") or [])
        elif chave == "busca":
            self.titulo_lista.set_label("Search")
            self.sub_lista.set_label("")
            self.mostrar_status("system-search-symbolic", "Search YouTube Music")
            self.busca.grab_focus()
        else:
            titulos = {"recentes": "Recent", "curtidas": "Liked"}
            self.titulo_lista.set_label(nome or titulos.get(chave, ""))
            self.sub_lista.set_label("Loading…")
            self.pilha.set_visible_child_name("carregando")
            if chave == "recentes":
                pedido = lambda: chamar("recentes")
            elif chave == "curtidas":
                pedido = lambda: chamar("playlist", id="LM")
            else:
                pedido = lambda pid=chave.split(":", 1)[1]: chamar("playlist", id=pid)
            em_fundo(pedido, lambda r, v=chave: self.mostrar_lista(r) if self.visao == v else None)

    def buscar(self, texto):
        if not texto.strip():
            return
        self.sub_lista.set_label("Searching…")
        self.pilha.set_visible_child_name("carregando")
        em_fundo(lambda: chamar("buscar", q=texto), lambda r: self.mostrar_lista(r) if self.visao == "busca" else None)

    def mostrar_status(self, icone, titulo, texto=""):
        self.status.set_icon_name(icone)
        self.status.set_title(titulo)
        self.status.set_description(texto)
        self.pilha.set_visible_child_name("status")

    def preencher(self, faixas, na_fila):
        while (filho := self.lista_meio.get_first_child()) is not None:
            self.lista_meio.remove(filho)
        for i, f in enumerate(faixas):
            self.lista_meio.append(LinhaFaixa(self, f, i, na_fila))

    def mostrar_lista(self, r):
        if isinstance(r, Exception):
            self.sub_lista.set_label("")
            self.mostrar_status("dialog-warning-symbolic", "Couldn't load", str(r))
            return
        self.lista = r
        self.sub_lista.set_label(f"{len(r)} songs")
        if not r:
            self.mostrar_status("folder-music-symbolic", "Nothing here")
            return
        self.preencher(r, False)
        self.pilha.set_visible_child_name("lista")

    def mostrar_fila(self, fila):
        self.lista = fila
        self.sub_lista.set_label(f"{len(fila)} songs")
        if not fila:
            self.mostrar_status("view-list-symbolic", "The queue is empty",
                                "Search or open a playlist and press a song to play it.")
            return
        self.preencher(fila, True)
        self.pilha.set_visible_child_name("lista")

    def ao_ativar_faixa(self, _lista, linha):
        if self.visao == "fila":
            self.comando("pular_para", i=linha.indice)
        else:
            self.tocar_lista(linha.indice)

    # ---- ações ----
    def comando(self, cmd, **p):
        em_fundo(lambda: chamar(cmd, **p), lambda r: self.avisar(str(r)) if isinstance(r, Exception) else
                 GLib.timeout_add(120, lambda: (self.atualizar(), False)[1]))

    def tocar_lista(self, i):
        if self.lista:
            self.comando("tocar", faixas=self.lista, inicio=i)

    def tocar_embaralhado(self):
        import random
        if self.lista:
            fs = list(self.lista)
            random.shuffle(fs)
            self.comando("tocar", faixas=fs, inicio=0)

    def enfileirar(self, fs):
        self.comando("adicionar", faixas=fs)
        self.avisar("Added to queue")

    def tocar_depois(self, fs):
        self.comando("proxima_na_fila", faixas=fs)
        self.avisar("Plays next")

    def radio(self, f):
        self.comando("radio", id=f["id"])
        self.avisar(f"Radio from “{f['titulo']}”")

    def mover(self, de, para):
        if 0 <= para < len(self.estado.get("fila") or []):
            self.comando("mover", de=de, para=para)

    def remover(self, i):
        self.comando("remover", i=i)

    def avisar(self, texto):
        self.toasts.add_toast(Adw.Toast(title=texto, timeout=2, use_markup=False))

    def tecla(self, _c, tecla, _cod, estado):
        foco = self.get_focus()
        if isinstance(foco, (Gtk.Text, Gtk.Entry, Gtk.SearchEntry)):
            return False
        if tecla == Gdk.KEY_space:
            self.comando("pausar")
            return True
        if estado & Gdk.ModifierType.CONTROL_MASK and tecla in (Gdk.KEY_f, Gdk.KEY_k):
            self.escolher_visao("busca")
            return True
        if estado & Gdk.ModifierType.CONTROL_MASK and tecla in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3):
            self.escolher_visao({Gdk.KEY_1: "fila", Gdk.KEY_2: "recentes", Gdk.KEY_3: "curtidas"}[tecla])
            return True
        if estado & Gdk.ModifierType.CONTROL_MASK and tecla == Gdk.KEY_Right:
            self.comando("proxima")
            return True
        if estado & Gdk.ModifierType.CONTROL_MASK and tecla == Gdk.KEY_Left:
            self.comando("anterior")
            return True
        return False

    def ao_buscar_tempo(self, _s, _tipo, valor):
        self.comando("ir", s=valor)
        self.ref = (valor, time.time(), self.ref[2])
        return False

    # ---- estado do motor ----
    def atualizar(self):
        em_fundo(lambda: chamar("estado", timeout=5), self.aplicar_estado)
        return True

    def aplicar_estado(self, e):
        if isinstance(e, Exception):
            return
        self.estado = e
        self.ref = (e["pos"], time.time(), e["tocando"])
        fila = e["fila"]
        self.contagem.set_label(str(len(fila)) if fila else "")
        ids = [(f["id"], f.get("atual")) for f in fila]
        if self.visao == "fila" and ids != self.ids_fila:
            self.ids_fila = ids
            self.mostrar_fila(fila)
        self.bt_play.set_icon_name("media-playback-pause-symbolic" if e["tocando"] else "media-playback-start-symbolic")
        if self.acao_autoplay.get_state().get_boolean() != e["radio"]:
            self.acao_autoplay.set_state(GLib.Variant.new_boolean(e["radio"]))
        for b, ligado in ((self.bt_repetir, e["repetir"] or e["repetir_uma"]), (self.bt_radio, e["radio"])):
            (b.add_css_class if ligado else b.remove_css_class)("ligado")
        self.bt_repetir.set_icon_name("media-playlist-repeat-song-symbolic" if e["repetir_uma"]
                                      else "media-playlist-repeat-symbolic")
        if not self.volume.has_css_class("mexendo"):
            self.volume.set_value(e["volume"])
        a = e.get("atual")
        if a:
            self.np_titulo.set_label(a["titulo"])
            self.np_artista.set_label(a["artista"])
            self.np_album.set_label(a["album"])
            Imagens.por(a.get("capa"), self.capa)
            self.progresso.set_range(0, max(e["duracao"] or a["duracao"] or 1, 1))
            self.t_dur.set_label(tempo(e["duracao"] or a["duracao"]))
            if a["id"] != self.id_letra:
                self.id_letra = a["id"]
                self.letra = None
                self.mostrar_letra_carregando()
                em_fundo(lambda f=a: chamar("letra", id=f["id"], faixa=f), lambda r, i=a["id"]: self.mostrar_letra(r, i))
        else:
            self.np_titulo.set_label("Nothing playing")
            self.np_artista.set_label("Search or pick a playlist")
            self.np_album.set_label("")
            self.capa.set_paintable(None)

    # ---- letra ----
    def limpar_letra(self):
        while (f := self.letras.get_first_child()) is not None:
            self.letras.remove(f)

    def mostrar_letra_carregando(self):
        self.limpar_letra()
        l = Gtk.Label(label="…")
        l.add_css_class("letra")
        self.letras.append(l)

    def mostrar_letra(self, r, fid):
        if fid != self.id_letra:
            return
        self.limpar_letra()
        self.letra = None if isinstance(r, Exception) else r
        self.linhas_letra = []
        if not self.letra or (not self.letra["sincronizada"] and not self.letra["texto"]):
            l = Gtk.Label(label="No lyrics for this song")
            l.add_css_class("letra")
            self.letras.append(l)
            return
        if self.letra["sincronizada"]:
            for t, texto in self.letra["sincronizada"]:
                l = Gtk.Label(label=texto or "·", wrap=True, justify=Gtk.Justification.CENTER)
                l.add_css_class("letra")
                l._t = t
                self.letras.append(l)
                self.linhas_letra.append(l)
        else:
            l = Gtk.Label(label=self.letra["texto"], wrap=True, justify=Gtk.Justification.CENTER)
            l.add_css_class("letra")
            l.add_css_class("atual")
            self.letras.append(l)
        fonte = Gtk.Label(label=f"Lyrics: {self.letra['fonte']}", margin_top=10)
        fonte.add_css_class("dono")
        self.letras.append(fonte)
        self.linha_atual = -1

    def tique(self):
        """5x por segundo: tempo interpolado (sem perguntar ao motor), barra e letra."""
        pos, quando, tocando = self.ref
        agora = pos + (time.time() - quando if tocando else 0)
        self.t_pos.set_label(tempo(agora))
        self.progresso.set_value(agora)
        if self.letra and self.letra.get("sincronizada") and getattr(self, "linhas_letra", None):
            idx = -1
            for i, l in enumerate(self.linhas_letra):
                if l._t <= agora + 0.25:
                    idx = i
            if idx != self.linha_atual:
                for i, l in enumerate(self.linhas_letra):
                    for c in ("atual", "passada"):
                        l.remove_css_class(c)
                    if i == idx:
                        l.add_css_class("atual")
                    elif i < idx:
                        l.add_css_class("passada")
                self.linha_atual = idx
                if idx >= 0:
                    GLib.idle_add(self.centralizar_letra, self.linhas_letra[idx])
        return True

    def centralizar_letra(self, linha):
        ok, ponto = linha.compute_point(self.letras, Graphene_ponto(0, 0))
        if not ok:
            return False
        adj = self.rolagem_letra.get_vadjustment()
        alvo = ponto.y - adj.get_page_size() / 2 + linha.get_height() / 2
        adj.set_value(max(0, min(alvo, adj.get_upper() - adj.get_page_size())))
        return False


def Graphene_ponto(x, y):
    gi.require_version("Graphene", "1.0")
    from gi.repository import Graphene
    p = Graphene.Point()
    p.init(x, y)
    return p


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.YTMusic", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        prov = Gtk.CssProvider()
        prov.load_from_string(css())
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        (self.props.active_window or Janela(self)).present()


if __name__ == "__main__":
    try:
        chamar("estado", timeout=10)        # já sobe o motor, se precisar
    except ErroMotor:
        pass
    App().run(sys.argv)
