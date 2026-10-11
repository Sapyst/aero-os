"""
comum.py — peças usadas por todas as seções do app Settings.

Aqui ficam: como rodar comandos, como rodar coisas como root, como fazer tarefas demoradas
sem travar a janela, e "fábricas" de widgets repetidos (listas suspensas, linhas que abrem apps...).
"""
import os
import subprocess
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

PASTA_APP = os.path.dirname(os.path.abspath(__file__))
ASKPASS = os.path.expanduser("~/.local/bin/aero-askpass")   # janela de senha (askpass.py)


# ---------- comandos ----------

def rodar(cmd, **kw):
    """Roda um comando e espera. Devolve o resultado (returncode, stdout, stderr)."""
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ambiente_root():
    """Ambiente com SUDO_ASKPASS: quando o sudo precisar de senha e não houver terminal,
    ele chama o nosso askpass.py, que mostra uma janelinha de senha."""
    env = dict(os.environ)
    env["SUDO_ASKPASS"] = ASKPASS
    return env


def como_root(cmd):
    """sudo -A = "se precisar de senha, peça pelo programa do SUDO_ASKPASS".
    Se o sudo ainda lembra a senha, nem pergunta."""
    return ["sudo", "-A"] + cmd


def rodar_como_root(cmd):
    return rodar(como_root(cmd), env=ambiente_root())


def em_segundo_plano(funcao, depois=None):
    """Roda funcao() numa thread (tarefa paralela) pra janela não congelar.
    Quando terminar, chama depois(resultado) na thread principal: só ela pode mexer na interface."""
    def tarefa():
        try:
            resultado = funcao()
        except Exception as erro:  # noqa: BLE001 — devolve o erro pra quem chamou decidir
            resultado = erro
        if depois:
            GLib.idle_add(depois, resultado)
    threading.Thread(target=tarefa, daemon=True).start()


def abrir(cmd):
    """Abre um programa separado deste app (a GLib cuida de recolher o processo quando ele fechar)."""
    GLib.spawn_async(cmd, flags=GLib.SpawnFlags.SEARCH_PATH)


# ---------- widgets ----------

def indice(opcoes, valor, padrao=0):
    """Posição de `valor` numa lista de (texto, valor); `padrao` se não achar."""
    for i, (_, v) in enumerate(opcoes):
        if v == valor:
            return i
    return padrao


def combo(titulo, opcoes, selecionado, subtitulo=None):
    """Linha com lista suspensa. `opcoes` = [(texto mostrado, valor gravado), ...]"""
    linha = Adw.ComboRow(title=titulo, model=Gtk.StringList.new([t for t, _ in opcoes]))
    if subtitulo:
        linha.set_subtitle(subtitulo)
    linha.set_selected(selecionado)
    return linha


def valor_de(linha, opcoes):
    return opcoes[linha.get_selected()][1]


def linha_app(titulo, sub, cmd, icone):
    """Linha clicável que abre outro programa (ex: o pavucontrol no "Advanced")."""
    linha = Adw.ActionRow(use_markup=False, title=titulo, subtitle=sub, activatable=True)
    linha.add_prefix(Gtk.Image.new_from_icon_name(icone))
    linha.add_suffix(Gtk.Image.new_from_icon_name("adw-external-link-symbolic"))
    linha.connect("activated", lambda *_: abrir(cmd))
    return linha


def linha_seta(titulo, sub, icone, ao_clicar):
    """Linha clicável com seta ">", que abre uma sub-página."""
    linha = Adw.ActionRow(use_markup=False, title=titulo, subtitle=sub or "", activatable=True)
    if icone:
        linha.add_prefix(Gtk.Image.new_from_icon_name(icone))
    linha.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
    linha.connect("activated", lambda *_: ao_clicar())
    return linha


def botao(texto, ao_clicar, estilo=None, icone=None):
    b = Gtk.Button(valign=Gtk.Align.CENTER)
    if icone and texto:
        b.set_child(Adw.ButtonContent(label=texto, icon_name=icone))
    elif icone:
        b.set_icon_name(icone)
        b.set_tooltip_text(texto)
    else:
        b.set_label(texto)
    for e in (estilo or "").split():
        b.add_css_class(e)
    b.connect("clicked", lambda *_: ao_clicar())
    return b


def grupo_botao_aplicar(acao, texto="Apply"):
    b = botao(texto, acao, "suggested-action pill")
    b.set_halign(Gtk.Align.CENTER)
    b.set_margin_top(6)
    g = Adw.PreferencesGroup()
    g.add(b)
    return g


def limpar_grupo(grupo, linhas):
    """Tira do grupo as linhas que adicionamos antes (pra redesenhar uma lista)."""
    for linha in linhas:
        grupo.remove(linha)
    linhas.clear()


def pagina(titulo, conteudo, *botoes_cabecalho):
    """Monta uma página com barra de título (HeaderBar) e conteúdo.
    É o que aparece do lado direito, e também as sub-páginas."""
    cabecalho = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
    for b in botoes_cabecalho:
        cabecalho.pack_end(b)
    moldura = Adw.ToolbarView(content=conteudo)
    moldura.add_top_bar(cabecalho)
    return Adw.NavigationPage(title=titulo, child=moldura)


def confirmar(janela, titulo, texto, botao_ok, ao_confirmar, destrutivo=False):
    """Caixa de diálogo "Tem certeza?"."""
    d = Adw.AlertDialog(heading=titulo, body=texto)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", botao_ok)
    d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if destrutivo
                              else Adw.ResponseAppearance.SUGGESTED)
    d.connect("response", lambda _d, r: ao_confirmar() if r == "ok" else None)
    d.present(janela)


class Secao:
    """Base de cada seção da barra lateral.
    Cada seção define `titulo`, `icone` e monta self.pagina no construtor.
    ao_mostrar() é chamado sempre que a seção é aberta (pra atualizar dados)."""
    id = ""
    titulo = ""
    icone = ""

    def __init__(self, janela):
        self.janela = janela
        self.pagina = None

    def ao_mostrar(self):
        pass


# ---------- painel.conf (configurações compartilhadas com os scripts hypr-lid e hypr-tema) ----------

PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")


def ler_painel():
    """painel.conf é um arquivo CHAVE=valor (formato que o bash consegue "source")."""
    valores = {}
    try:
        with open(PAINEL) as f:
            for linha in f:
                linha = linha.strip()
                if "=" in linha and not linha.startswith("#"):
                    k, v = linha.split("=", 1)
                    valores[k] = v
    except FileNotFoundError:
        pass
    return valores


def atualizar_painel(**chaves):
    """Muda SÓ as chaves pedidas e mantém o resto do arquivo (outras seções e comentários)."""
    try:
        with open(PAINEL) as f:
            linhas = f.read().splitlines()
    except FileNotFoundError:
        linhas = ["# Gerado pelo app Settings (Super+I). Lido por hypr-lid e hypr-tema."]
    faltando = {k: str(v).replace("\n", "").replace("\r", "") for k, v in chaves.items()}   # 1 chave = 1 linha
    for i, linha in enumerate(linhas):
        k = linha.split("=", 1)[0].strip()
        if not linha.startswith("#") and k in faltando:
            linhas[i] = f"{k}={faltando.pop(k)}"
    linhas += [f"{k}={v}" for k, v in faltando.items()]
    with open(PAINEL, "w") as f:
        f.write("\n".join(linhas) + "\n")


# ---------- botões da janela (fechar, minimizar, maximizar) ----------
# O Hyprland ignora o pedido de "minimizar" que vem dos apps, então os botões nativos do GTK não
# funcionam pra isso. Fazemos os nossos três, com o mesmo visual de bolinhas da barra do sistema:
#   minimizar -> ~/.local/bin/hypr-minimizar (manda a janela ativa pra special:minimized)
#   maximizar -> hypr-maximizar (o mesmo que a bolinha verde da barra do sistema)

CSS_BOLINHAS = """
/* Botões da janela: SÍMBOLOS monocromáticos na cor do texto, iguais às barras do sistema (doc 42):
   ✖ fechar · ▲ minimizar · ▼ maximizar · ● sempre visível */
.bolinhas button { min-width: 24px; min-height: 24px; padding: 0; margin: 0 1px; border-radius: 99px;
                   background: transparent; box-shadow: none; color: currentColor; }
.bolinhas button label { font-family: "Aero Symbols"; font-size: 14px; }
.bolinhas button:hover { background: alpha(currentColor, 0.10); }
"""


def aplicar_css(css):
    from gi.repository import Gdk
    provedor = Gtk.CssProvider()
    provedor.load_from_string(css)
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provedor,
                                              Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


CAIXAS_BOLINHAS = []   # pra esconder/mostrar na hora (Settings → Theme → Window buttons)


def mostrar_bolinhas(mostrar):
    for caixa in CAIXAS_BOLINHAS:
        caixa.set_visible(mostrar)


def botoes_janela(janela):
    caixa = Gtk.Box(valign=Gtk.Align.CENTER, margin_start=4)
    caixa.add_css_class("bolinhas")
    # Botões de janela desligados (modo Hyprland puro, ~/.local/bin/botoes-de-janela): nasce escondida
    caixa.set_visible(ler_painel().get("BOTOES_JANELA", "1") != "0")
    CAIXAS_BOLINHAS.append(caixa)
    for classe, icone, dica, acao in (
            ("fechar", "", "Close", janela.close),
            ("maximizar", "", "Maximize / restore",
             lambda: abrir([os.path.expanduser("~/.local/bin/hypr-maximizar")])),
            ("minimizar", "", "Minimize",
             lambda: abrir([os.path.expanduser("~/.local/bin/hypr-minimizar")])),
            ("visivel", "", "Always visible (don't dim when unfocused)",
             lambda: abrir([os.path.expanduser("~/.local/bin/hypr-sempre-visivel")]))):
        b = Gtk.Button(child=Gtk.Label(label=icone), tooltip_text=dica)   # símbolo, não bolinha colorida
        b.add_css_class(classe)
        b.connect("clicked", lambda _b, a=acao: a())
        caixa.append(b)
    return caixa
