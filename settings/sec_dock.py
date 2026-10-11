"""
sec_dock.py — seção Dock (doc 24).

Tudo grava chaves DOCK_* no painel.conf e vale NA HORA: o Dock (~/.local/share/aero-shell/dock.py)
vigia o arquivo e se ajusta sozinho. Não tem botão "Apply".
Os apps do Dock ficam em ~/.config/aero-shell/dock.json. Adicionar apps é no próprio Dock: botão direito →
"Add App" (busca ali mesmo). O Dock é parte do sistema: não tem opção de desligar.
"""
import os

from gi.repository import Adw, Gtk

from comum import Secao, atualizar_painel, botao, combo, indice, ler_painel, pagina

LISTA = os.path.expanduser("~/.config/aero-shell/dock.json")

MODOS = [("Intellihide", "inteligente"), ("Always visible", "sempre"), ("Auto-hide", "auto")]
TAMANHOS = [("Small", "40"), ("Medium", "48"), ("Large", "56"), ("Huge", "64")]
ONDAS = [("Off", "0"), ("Subtle", "35"), ("Medium", "60"), ("Strong", "90")]
ALCANCES = [("Narrow", "0.8"), ("Medium", "1.2"), ("Wide", "1.8")]


class SecaoDock(Secao):
    id = "dock"
    titulo = "Dock"
    icone = "view-app-grid-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False          # True enquanto preenchemos os campos (não gravar nada)
        conteudo = Adw.PreferencesPage()

        geral = Adw.PreferencesGroup(
            title="Dock", description="The app bar at the bottom of the screen. Changes apply right away.")
        self.modo = combo("Behavior", MODOS, 0)
        self.modo.set_subtitle("Intellihide: hides when a window touches it. Hidden, it leaves a thin "
                               "line at the bottom: touch it to bring the Dock up.")
        self.abertos = Adw.SwitchRow(title="Show open apps that aren't pinned",
                                     subtitle="They appear after a separator")
        for r in (self.modo, self.abertos):
            geral.add(r)

        visual = Adw.PreferencesGroup(title="Look and motion")
        self.tamanho = combo("Icon size", TAMANHOS, 1)
        self.onda = combo("Wave effect", ONDAS, 2, "How much icons grow and rise under the mouse")
        self.alcance = combo("Wave width", ALCANCES, 1, "How many neighbors rise with it")
        self.movimento = Adw.SwitchRow(title="Reduce motion",
                                       subtitle="No wave, no bounce: the Dock just appears and disappears")
        for r in (self.tamanho, self.onda, self.alcance, self.movimento):
            visual.add(r)

        # Cada mudança grava na hora (os combos e interruptores chamam gravar())
        for linha in (self.modo, self.tamanho, self.onda, self.alcance):
            linha.connect("notify::selected", lambda *_: self.gravar())
        for linha in (self.abertos, self.movimento):
            linha.connect("notify::active", lambda *_: self.gravar())

        apps_grupo = Adw.PreferencesGroup(title="Apps")
        editar = Adw.ActionRow(use_markup=False, title="Add or remove apps",
                               subtitle="Right-click the Dock (an icon or the bar) → Add App: search and click. "
                                        "To remove, right-click the icon → Remove from Dock.")
        editar.add_prefix(Gtk.Image(icon_name="input-mouse-symbolic"))
        padrao = Adw.ActionRow(use_markup=False, title="Reset apps",
                               subtitle="Back to Claude, Firefox and Settings")
        padrao.add_suffix(botao("Reset", self.resetar))
        apps_grupo.add(editar)
        apps_grupo.add(padrao)

        for g in (geral, visual, apps_grupo):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.carregando = True
        p = ler_painel()
        self.modo.set_selected(indice(MODOS, p.get("DOCK_MODO", "inteligente")))
        self.abertos.set_active(p.get("DOCK_ABERTOS", "1") == "1")
        self.tamanho.set_selected(indice(TAMANHOS, p.get("DOCK_TAMANHO", "48"), 1))
        self.onda.set_selected(indice(ONDAS, p.get("DOCK_ONDA", "60"), 2))
        self.alcance.set_selected(indice(ALCANCES, p.get("DOCK_ALCANCE", "1.2"), 1))
        self.movimento.set_active(p.get("DOCK_MOVIMENTO", "0") == "1")
        self.carregando = False

    def gravar(self):
        if self.carregando:
            return
        atualizar_painel(DOCK_MODO=MODOS[self.modo.get_selected()][1],
                         DOCK_ABERTOS="1" if self.abertos.get_active() else "0",
                         DOCK_TAMANHO=TAMANHOS[self.tamanho.get_selected()][1],
                         DOCK_ONDA=ONDAS[self.onda.get_selected()][1],
                         DOCK_ALCANCE=ALCANCES[self.alcance.get_selected()][1],
                         DOCK_MOVIMENTO="1" if self.movimento.get_active() else "0")

    def resetar(self):
        try:
            os.remove(LISTA)          # sem o arquivo, o Dock volta pros 3 apps padrão
        except FileNotFoundError:
            pass
        self.janela.avisar("Dock apps reset")
