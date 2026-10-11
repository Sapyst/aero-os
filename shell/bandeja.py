#!/usr/bin/env python3
"""
bandeja.py — a bandeja do sistema (system tray), com cliques no estilo do Windows:
  clique simples = nada | clique DUPLO = abrir o app | clique DIREITO = menu do app | meio = ação secundária

Por que fazer a nossa: a bandeja da waybar decide sozinha o clique. Pra apps feitos com AppIndicator
(como a Steam), que não têm ação de "abrir", ela mostra o menu até no clique esquerdo.

Como bandejas funcionam no Linux (padrão StatusNotifierItem, via D-Bus):
  - Um "cartório" (o Watcher, nome org.kde.StatusNotifierWatcher) recebe o registro dos ícones.
    Quem antes fazia isso era a waybar (por isso tiramos o módulo "tray" dela).
  - Cada app publica um objeto com Id, Title, IconName/IconPixmap, ToolTip e o endereço do Menu.
  - O menu segue outro padrão (com.canonical.dbusmenu): pedimos a árvore com GetLayout e avisamos
    o clique com Event(id, "clicked", ...).
  - D-Bus = o "correio" entre programas do mesmo usuário. Gio (do GLib) fala D-Bus pra gente.

Onde aparece: uma camada (layer-shell) em cima da ponta direita da barra. A waybar reserva o espaço
exato com o módulo custom/espaco-bandeja (este programa avisa quantos ícones há).
Precisa de LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so (o lançador ~/.local/bin/bandeja faz isso).
"""
import json
import os
import subprocess
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import apps  # noqa: E402

LARGURA_ICONE = 30                     # tem que bater com o CSS da waybar (#custom-espaco-bandeja.nX)
TAMANHO_ICONE = 18
# Distância da borda direita: 12 (margem da waybar) + 40 (botão da central, #custom-central no style.css)
MARGEM_DIREITA = 12 + 40
ESTADO = os.path.expanduser("~/.cache/aero-shell/bandeja.json")   # lido pela waybar
SINAL_WAYBAR = 9                       # pkill -RTMIN+9 waybar = "releia o módulo do espaço"

WATCHER_XML = """
<node>
  <interface name="org.kde.StatusNotifierWatcher">
    <method name="RegisterStatusNotifierItem"><arg type="s" name="service" direction="in"/></method>
    <method name="RegisterStatusNotifierHost"><arg type="s" name="service" direction="in"/></method>
    <property name="RegisteredStatusNotifierItems" type="as" access="read"/>
    <property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
    <property name="ProtocolVersion" type="i" access="read"/>
    <signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
    <signal name="StatusNotifierItemUnregistered"><arg type="s"/></signal>
    <signal name="StatusNotifierHostRegistered"/>
  </interface>
</node>
"""

CSS = """
/* Sem vidro aqui: a bandeja fica POR CIMA da barra; desfocar esconderia a barra embaixo dela */
window.bandeja { background: transparent; backdrop-filter: none; }
/* mesmo "fundinho" dos ícones da barra (waybar/style.css): 26 px de altura, 4 px de folga, hover leve */
.item { min-width: 30px; min-height: 26px; margin: 4px 0; border-radius: 10px; }
.item:hover { background: alpha(currentColor, 0.07); }
/* Ícones de uma cor só, sempre com contraste com a barra (doc 35):
   brancos (steam_tray_mono, Discord) no tema CLARO viram escuros; pretos (Claude) no tema ESCURO viram claros */
window.bandeja.claro .mono { filter: invert(0.85); }
window.bandeja:not(.claro) .mono-escuro { filter: invert(0.92); }
.menu-bandeja button { padding: 6px 14px; border-radius: 8px; font-weight: normal; }
.menu-bandeja button label { min-width: 160px; }
"""


def sem_mnemonico(texto):
    """Menus usam "_" pra marcar a letra de atalho (ex: "_Library"). "__" é um "_" de verdade."""
    return texto.replace("__", "\0").replace("_", "").replace("\0", "_")


def atalho_do_app(ident):
    """Acha o atalho (.desktop) do app pelo Id do ícone, em ordem de confiança:
    1) nome do atalho igual ao Id ("steam" -> steam.desktop)
    2) atalho terminando com o Id ("org.exemplo.steam.desktop")
    3) atalho que roda um programa com esse nome (por último: os atalhos dos JOGOS da Steam
       também rodam o programa "steam", e não queremos abrir o Dota 2 no lugar da Steam!)"""
    ident = ident.lower()
    if not ident:
        return None
    todos = apps.todos()
    testes = (lambda nome, app: nome == ident,
              lambda nome, app: nome.endswith("." + ident),
              lambda nome, app: os.path.basename(app.get_executable() or "").lower() == ident)
    for teste in testes:
        for app in todos:
            if teste(app.get_id().removesuffix(".desktop").lower(), app):
                return app
    return None



def desenho_branco(dados):
    """True se o desenho (bytes ARGB) é praticamente todo branco/cinza claro, ou seja, um ícone de uma
    cor só feito pra barras escuras. Olha só os pixels visíveis (alfa > 40)."""
    visiveis = brancos = 0
    for i in range(0, len(dados) - 3, 4):
        a, r, g, b = dados[i], dados[i + 1], dados[i + 2], dados[i + 3]
        if a > 40:
            visiveis += 1
            if min(r, g, b) > 190 and max(r, g, b) - min(r, g, b) < 30:
                brancos += 1
    return visiveis > 0 and brancos / visiveis > 0.9


def desenho_preto(dados):
    """O contrário: desenho todo preto/cinza escuro (ex: o asterisco do Claude), feito pra barras CLARAS.
    No tema escuro ele sumiria na barra: a gente inverte (vira claro)."""
    visiveis = pretos = 0
    for i in range(0, len(dados) - 3, 4):
        a, r, g, b = dados[i], dados[i + 1], dados[i + 2], dados[i + 3]
        if a > 40:
            visiveis += 1
            if max(r, g, b) < 80 and max(r, g, b) - min(r, g, b) < 30:
                pretos += 1
    return visiveis > 0 and pretos / visiveis > 0.9

class Item:
    """Um ícone registrado: endereço no D-Bus + propriedades."""

    def __init__(self, servico, caminho):
        self.servico, self.caminho = servico, caminho
        self.props = {}

    @property
    def chave(self):
        return self.servico + self.caminho


class Bandeja(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Tray")
        self.add_css_class("bandeja")
        Camada.init_for_window(self)
        # Mesmo nível da barra (TOP): some junto com ela quando um jogo/vídeo está em tela cheia.
        # (OVERLAY ficaria por cima até de jogos.) Fica por cima da waybar: veja vigiar_barra().
        Camada.set_layer(self, Camada.Layer.TOP)
        Camada.set_namespace(self, "bandeja")
        Camada.set_anchor(self, Camada.Edge.TOP, True)
        Camada.set_anchor(self, Camada.Edge.RIGHT, True)
        Camada.set_margin(self, Camada.Edge.TOP, 8)       # mesma margem da waybar (config.jsonc)
        Camada.set_margin(self, Camada.Edge.RIGHT, MARGEM_DIREITA)
        Camada.set_exclusive_zone(self, -1)               # fica POR CIMA da barra, sem empurrar nada
        # Acompanha o tema claro/escuro (o hypr-tema muda esta configuração pelo horário)
        self.interface = Gio.Settings.new("org.gnome.desktop.interface")
        self.interface.connect("changed::color-scheme", lambda *_: self.aplicar_tema())
        self.aplicar_tema()
        self.caixa = Gtk.Box(valign=Gtk.Align.START)
        self.set_child(self.caixa)
        self.set_default_size(LARGURA_ICONE, 34)          # cresce só o necessário pros ícones

        self.itens = {}          # chave -> Item (na ordem de registro)
        self.widgets = {}        # chave -> widget do ícone
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.iniciar_watcher()
        self.vigiar_barra()

    def vigiar_barra(self):
        """A bandeja e a waybar são camadas do mesmo nível (TOP): a que abriu POR ÚLTIMO fica na frente.
        Se a waybar reabrir (reiniciada, recarregada), ela cobriria os ícones (Claude, Steam...).
        Então a gente ouve o Hyprland (socket2): quando abre a camada "waybar", a bandeja se esconde e
        reaparece num instante, virando a última aberta e voltando pra frente (doc 30)."""
        import socket
        import threading
        caminho = os.path.join(os.environ.get("XDG_RUNTIME_DIR", ""), "hypr",
                               os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""), ".socket2.sock")

        def ouvir():
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(caminho)
            except OSError:
                return
            for linha in s.makefile("r", encoding="utf-8", errors="replace"):
                if linha.strip() == "openlayer>>waybar":
                    GLib.timeout_add(300, self.reerguer)    # espera a barra terminar de aparecer

        threading.Thread(target=ouvir, daemon=True).start()

    def reerguer(self):
        self.set_visible(False)
        GLib.timeout_add(60, lambda: (self.present(), False)[1])
        return False

    def aplicar_tema(self):
        if self.interface.get_string("color-scheme") == "prefer-light":
            self.add_css_class("claro")
        else:
            self.remove_css_class("claro")

    # ---------- o "cartório" (Watcher) ----------

    def iniciar_watcher(self):
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        self.bus.register_object("/StatusNotifierWatcher", info, self.ao_metodo, self.ao_propriedade, None)
        # REPLACE: se outro watcher estiver rodando e permitir, assumimos o lugar
        Gio.bus_own_name_on_connection(self.bus, "org.kde.StatusNotifierWatcher",
                                       Gio.BusNameOwnerFlags.REPLACE, None, None)

    def ao_metodo(self, _bus, remetente, _caminho, _iface, metodo, parametros, chamada):
        if metodo == "RegisterStatusNotifierItem":
            servico = parametros.unpack()[0]
            # Alguns apps mandam o caminho do objeto ("/org/ayatana/..."), outros o nome no barramento
            if servico.startswith("/"):
                self.adicionar(remetente, servico)
            else:
                self.adicionar(servico, "/StatusNotifierItem")
        chamada.return_value(None)

    def ao_propriedade(self, _bus, _remetente, _caminho, _iface, nome):
        if nome == "RegisteredStatusNotifierItems":
            return GLib.Variant("as", [i.servico + i.caminho for i in self.itens.values()])
        if nome == "IsStatusNotifierHostRegistered":
            return GLib.Variant("b", True)
        if nome == "ProtocolVersion":
            return GLib.Variant("i", 0)
        return None

    def emitir(self, sinal, parametros=None):
        self.bus.emit_signal(None, "/StatusNotifierWatcher", "org.kde.StatusNotifierWatcher", sinal, parametros)

    # ---------- itens ----------

    def adicionar(self, servico, caminho):
        item = Item(servico, caminho)
        if item.chave in self.itens:
            return
        self.itens[item.chave] = item
        # Quando o app fechar, o nome dele some do barramento: aí tiramos o ícone
        Gio.bus_watch_name_on_connection(self.bus, servico, Gio.BusNameWatcherFlags.NONE, None,
                                         lambda *_: self.remover(item.chave))
        # Quando o app mudar ícone/título/status, ele avisa com estes sinais
        self.bus.signal_subscribe(servico, "org.kde.StatusNotifierItem", None, caminho, None,
                                  Gio.DBusSignalFlags.NONE, lambda *_: self.atualizar(item.chave))
        self.emitir("StatusNotifierItemRegistered", GLib.Variant("(s)", (item.chave,)))
        self.atualizar(item.chave)

    def remover(self, chave):
        if self.itens.pop(chave, None):
            widget = self.widgets.pop(chave, None)
            if widget:
                self.caixa.remove(widget)
            self.emitir("StatusNotifierItemUnregistered", GLib.Variant("(s)", (chave,)))
            self.avisar_waybar()

    def atualizar(self, chave):
        item = self.itens.get(chave)
        if not item:
            return

        def pronto(bus, resultado):
            try:
                item.props = bus.call_finish(resultado).unpack()[0]
            except GLib.Error:
                return
            self.desenhar(item)
        self.bus.call(item.servico, item.caminho, "org.freedesktop.DBus.Properties", "GetAll",
                      GLib.Variant("(s)", ("org.kde.StatusNotifierItem",)), None,
                      Gio.DBusCallFlags.NONE, 3000, None, pronto)

    def icone(self, item):
        p = item.props
        tema = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        if p.get("IconThemePath"):
            tema.add_search_path(p["IconThemePath"])       # ex: a pasta de ícones da própria Steam
        if p.get("IconName") and tema.has_icon(p["IconName"]):
            imagem = Gtk.Image(icon_name=p["IconName"], pixel_size=TAMANHO_ICONE)
            if "mono" in p["IconName"] or p["IconName"].endswith("-symbolic"):
                imagem.add_css_class("mono")       # ícone de uma cor só: muda com o tema
            return imagem
        pixmaps = p.get("IconPixmap") or []
        if pixmaps:
            # Imagem crua: (largura, altura, bytes ARGB). Pega a maior e vira uma textura do GTK.
            w, h, dados = max(pixmaps, key=lambda x: x[0] * x[1])
            textura = Gdk.MemoryTexture.new(w, h, Gdk.MemoryFormat.A8R8G8B8, GLib.Bytes.new(bytes(dados)), w * 4)
            imagem = Gtk.Image.new_from_paintable(textura)
            imagem.set_pixel_size(TAMANHO_ICONE)
            if desenho_branco(dados):
                imagem.add_css_class("mono")       # ex: Discord manda um desenho branco: inverte no claro
            elif desenho_preto(dados):
                imagem.add_css_class("mono-escuro")   # ex: Claude manda um desenho preto: inverte no escuro
            return imagem
        return Gtk.Image(icon_name=p.get("IconName") or "application-x-executable-symbolic",
                         pixel_size=TAMANHO_ICONE)

    def desenhar(self, item):
        antigo = self.widgets.get(item.chave)
        caixa = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        caixa.add_css_class("item")
        caixa.set_size_request(LARGURA_ICONE, 26)      # mesma altura do "fundinho" da barra
        imagem = self.icone(item)
        imagem.set_hexpand(True)
        caixa.append(imagem)
        dica = item.props.get("ToolTip")
        titulo = (dica[2] if dica and len(dica) > 2 and dica[2] else "") or item.props.get("Title") or item.props.get("Id", "")
        caixa.set_tooltip_text(titulo)
        clique = Gtk.GestureClick(button=0)                  # 0 = qualquer botão
        clique.connect("pressed", self.ao_clicar, item, caixa)
        caixa.add_controller(clique)
        caixa.set_visible(item.props.get("Status") != "Passive")   # "Passive" = o app pediu pra esconder

        if antigo:
            self.caixa.insert_child_after(caixa, antigo)
            self.caixa.remove(antigo)
        else:
            self.caixa.append(caixa)
        self.widgets[item.chave] = caixa
        self.avisar_waybar()

    def avisar_waybar(self):
        """Diz pra waybar quantos ícones visíveis há, pra ela reservar o espaço certo."""
        n = sum(1 for w in self.widgets.values() if w.get_visible())
        os.makedirs(os.path.dirname(ESTADO), exist_ok=True)
        with open(ESTADO, "w") as f:
            json.dump({"text": " " if n else "", "class": f"n{n}"}, f)
        subprocess.run(["pkill", f"-RTMIN+{SINAL_WAYBAR}", "-x", "waybar"])
        self.set_visible(n > 0)

    # ---------- cliques ----------

    def ao_clicar(self, gesto, n_cliques, x, y, item, widget):
        botao = gesto.get_current_button()
        if botao == 1 and n_cliques == 2:
            self.abrir(item, widget)
        elif botao == 3:
            self.mostrar_menu(item, widget)
        elif botao == 2:
            self.chamar(item, "SecondaryActivate")

    def chamar(self, item, metodo, ao_falhar=None):
        def pronto(bus, resultado):
            try:
                bus.call_finish(resultado)
            except GLib.Error:
                if ao_falhar:
                    ao_falhar()
        self.bus.call(item.servico, item.caminho, "org.kde.StatusNotifierItem", metodo,
                      GLib.Variant("(ii)", (0, 0)), None, Gio.DBusCallFlags.NONE, 3000, None, pronto)

    def abrir(self, item, widget):
        """Pede ao app pra abrir (Activate). Se ele não souber (AppIndicator, ex: Steam), abrimos o app
        pelo atalho dele: o pedido de "me mostra" que ele fizer é atendido pelo hypr-minimizar-escuta."""
        def pelo_atalho():
            app = atalho_do_app(item.props.get("Id") or "")
            if app:
                apps.abrir(app)
            else:
                self.mostrar_menu(item, widget)       # não achou o app: pelo menos mostra o menu
        self.chamar(item, "Activate", ao_falhar=pelo_atalho)

    # ---------- menu (dbusmenu) ----------

    def mostrar_menu(self, item, widget):
        caminho_menu = item.props.get("Menu")
        if not caminho_menu:
            return
        try:
            self.bus.call_sync(item.servico, caminho_menu, "com.canonical.dbusmenu", "AboutToShow",
                               GLib.Variant("(i)", (0,)), None, Gio.DBusCallFlags.NONE, 1000, None)
        except GLib.Error:
            pass                                     # opcional no padrão; alguns apps não têm
        try:
            _rev, raiz = self.bus.call_sync(item.servico, caminho_menu, "com.canonical.dbusmenu", "GetLayout",
                                            GLib.Variant("(iias)", (0, -1, [])), None,
                                            Gio.DBusCallFlags.NONE, 3000, None).unpack()
        except GLib.Error:
            return
        self.popover(item, caminho_menu, raiz[2], widget)

    def popover(self, item, caminho_menu, filhos, ancora):
        pop = Gtk.Popover(has_arrow=False)
        lista = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        lista.add_css_class("menu-bandeja")
        for ident, props, netos in filhos:
            if not props.get("visible", True):
                continue
            if props.get("type") == "separator":
                lista.append(Gtk.Separator(margin_top=4, margin_bottom=4))
                continue
            texto = sem_mnemonico(props.get("label", ""))
            if props.get("toggle-type") and props.get("toggle-state") == 1:
                texto = "✓  " + texto
            submenu = props.get("children-display") == "submenu" and netos
            b = Gtk.Button(label=texto + ("   ›" if submenu else ""), sensitive=props.get("enabled", True))
            b.add_css_class("flat")
            b.get_child().set_xalign(0)
            if submenu:
                b.connect("clicked", lambda _b, n=netos, bb=b: self.popover(item, caminho_menu, n, bb))
            else:
                b.connect("clicked", lambda _b, i=ident: (pop.popdown(), self.clicar_menu(item, caminho_menu, i)))
            lista.append(b)
        pop.set_child(lista)
        pop.set_parent(ancora)
        pop.connect("closed", lambda p: GLib.idle_add(p.unparent))
        pop.popup()

    def clicar_menu(self, item, caminho_menu, ident):
        self.bus.call(item.servico, caminho_menu, "com.canonical.dbusmenu", "Event",
                      GLib.Variant("(isvu)", (ident, "clicked", GLib.Variant("i", 0), 0)), None,
                      Gio.DBusCallFlags.NONE, 3000, None, None)


class App(Adw.Application):    # Adw = acompanha o claro/escuro do sistema (doc 35)
    def __init__(self):
        super().__init__(application_id="com.aero.Tray")

    def do_activate(self):
        if self.props.active_window:
            return
        provedor = Gtk.CssProvider()
        provedor.load_from_string(CSS)
        # Prioridade ACIMA da do usuário: o ~/.config/gtk-4.0/gtk.css (prioridade USER) liga o vidro
        # em todas as janelas, e aqui precisamos desligar (senão o desfoque cobre a barra embaixo).
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        janela = Bandeja(self)
        janela.present()
        self.hold()


if __name__ == "__main__":
    sys.exit(App().run([]))
