"""
sec_mouse.py — seção Mouse & Touchpad (doc 23).

As escolhas ficam no painel.conf (chaves TOUCH_*, GESTO_*, MOUSE_*) e viram o arquivo
~/.config/hypr/entrada.conf, que o hyprland.conf inclui com "source =". Muda na hora: cada troca
regrava o arquivo e roda "hyprctl reload" (sem botão Apply).

Gestos do touchpad (Hyprland 0.51+):
  gesture = 3, horizontal, workspace          3 dedos pro lado = troca de área de trabalho
  gesture = 3, up, dispatcher, exec, CMD      3 dedos pra cima = roda um comando
"""
import os

from gi.repository import Adw

from comum import Secao, atualizar_painel, combo, indice, ler_painel, pagina, rodar, valor_de

ENTRADA = os.path.expanduser("~/.config/hypr/entrada.conf")
BIN = os.path.expanduser("~/.local/bin")

ROLAGEM = [("Slow", "0.4"), ("Normal", "0.6"), ("Fast", "1.0")]
VELOCIDADE_MOUSE = [("Slower", "-0.4"), ("Slow", "-0.2"), ("Normal", "0"), ("Fast", "0.2"), ("Faster", "0.4")]

# (chave no painel.conf, título, subtítulo, padrão)
OPCOES_TOUCHPAD = [
    ("TOUCH_NATURAL", "Natural scrolling", "Content follows your fingers, like on a phone", "1"),
    ("TOUCH_TOQUE", "Tap to click", "A light tap is a click", "1"),
    ("TOUCH_DOIS_DEDOS", "Two-finger click = right click", "Instead of clicking the bottom-right corner", "1"),
    ("TOUCH_DIGITANDO", "Disable while typing", "Ignores the palm while you type", "1"),
]
GESTOS = [
    ("GESTO_AREAS", "Swipe 3 fingers sideways", "Switch workspace", "1"),
    ("GESTO_MENU", "Swipe 3 fingers up", "Open Super Menu", "1"),
    ("GESTO_MINIMIZAR", "Swipe 4 fingers down", "Minimize the window", "1"),
]


def gerar_entrada(p):
    """Escreve o entrada.conf a partir do painel.conf."""
    sim = lambda chave, padrao="1": "true" if p.get(chave, padrao) == "1" else "false"   # noqa: E731
    linhas = [
        "# Gerado pelo Settings → Mouse & Touchpad (sec_mouse.py). Não editar à mão: é reescrito.",
        "input {",
        f"    sensitivity = {p.get('MOUSE_VELOCIDADE', '0')}      # velocidade do mouse (-1 a 1)",
        f"    accel_profile = {'adaptive' if p.get('MOUSE_ACELERACAO', '1') == '1' else 'flat'}"
        "      # adaptive = acelera com o movimento rápido; flat = sempre igual (jogos)",
        "    touchpad {",
        f"        natural_scroll = {sim('TOUCH_NATURAL')}",
        f"        tap-to-click = {sim('TOUCH_TOQUE')}",
        f"        clickfinger_behavior = {sim('TOUCH_DOIS_DEDOS')}",
        f"        disable_while_typing = {sim('TOUCH_DIGITANDO')}",
        f"        scroll_factor = {p.get('TOUCH_ROLAGEM', '0.6')}",
        "    }",
        "}",
    ]
    if p.get("GESTO_AREAS", "1") == "1":
        linhas.append("gesture = 3, horizontal, workspace")
    if p.get("GESTO_MENU", "1") == "1":
        linhas.append(f"gesture = 3, up, dispatcher, exec, {BIN}/supermenu")
    if p.get("GESTO_MINIMIZAR", "1") == "1":
        linhas.append(f"gesture = 4, down, dispatcher, exec, {BIN}/hypr-minimizar")
    with open(ENTRADA, "w") as f:
        f.write("\n".join(linhas) + "\n")


class SecaoMouse(Secao):
    id = "mouse"
    titulo = "Mouse & Touchpad"
    icone = "input-mouse-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()
        self.carregando = False     # True enquanto mostramos os valores salvos (não gravar nada)
        self.linhas = {}

        touchpad = Adw.PreferencesGroup(title="Touchpad")
        for chave, titulo, sub, _ in OPCOES_TOUCHPAD:
            touchpad.add(self.interruptor(chave, titulo, sub))
        self.rolagem = combo("Scrolling speed", ROLAGEM, 1)
        self.rolagem.connect("notify::selected", lambda *_: self.mudar(TOUCH_ROLAGEM=valor_de(self.rolagem, ROLAGEM)))
        touchpad.add(self.rolagem)

        gestos = Adw.PreferencesGroup(title="Touchpad gestures")
        for chave, titulo, sub, _ in GESTOS:
            gestos.add(self.interruptor(chave, titulo, sub))

        mouse = Adw.PreferencesGroup(title="Mouse")
        self.velocidade = combo("Pointer speed", VELOCIDADE_MOUSE, 2)
        self.velocidade.connect("notify::selected",
                                lambda *_: self.mudar(MOUSE_VELOCIDADE=valor_de(self.velocidade, VELOCIDADE_MOUSE)))
        mouse.add(self.velocidade)
        mouse.add(self.interruptor("MOUSE_ACELERACAO", "Pointer acceleration",
                                   "Turn off for games: the same hand movement always moves the same distance"))

        for g in (touchpad, gestos, mouse):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def interruptor(self, chave, titulo, sub):
        linha = Adw.SwitchRow(use_markup=False, title=titulo, subtitle=sub)
        linha.connect("notify::active", lambda l, _p: self.mudar(**{chave: "1" if l.get_active() else "0"}))
        self.linhas[chave] = linha
        return linha

    def mudar(self, **chaves):
        if self.carregando:
            return
        atualizar_painel(**chaves)
        gerar_entrada(ler_painel())
        rodar(["hyprctl", "reload"])

    def ao_mostrar(self):
        p = ler_painel()
        self.carregando = True
        for chave, _, _, padrao in OPCOES_TOUCHPAD + GESTOS + [("MOUSE_ACELERACAO", "", "", "1")]:
            self.linhas[chave].set_active(p.get(chave, padrao) == "1")
        self.rolagem.set_selected(indice(ROLAGEM, p.get("TOUCH_ROLAGEM", "0.6"), 1))
        self.velocidade.set_selected(indice(VELOCIDADE_MOUSE, p.get("MOUSE_VELOCIDADE", "0"), 2))
        self.carregando = False
