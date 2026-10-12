"""
sec_aparencia.py — seção Appearance.

  Tema claro/escuro e filtro de luz azul: grava em painel.conf; quem aplica é o script
  ~/.local/bin/hypr-tema (roda em segundo plano e confere o horário a cada minuto).
  Vidro (transparência + desfoque): reescreve ~/.config/hypr/aparencia.conf.
  Tamanho do texto: gsettings (apps GTK).
  Animações: bloco "animations" no aparencia.conf, entre linhas-marca (doc 23).
"""
import os

from gi.repository import Adw, Gdk, Gio, Gtk

from comum import (Secao, aplicar_css, atualizar_painel, combo, em_segundo_plano, grupo_botao_aplicar, indice, ler_painel, linha_app,
                   pagina, rodar, valor_de)

APARENCIA = os.path.expanduser("~/.config/hypr/aparencia.conf")
HYPR_TEMA = os.path.expanduser("~/.local/bin/hypr-tema")

TEMA = [("Automatic (by time)", "auto"), ("Always light", "claro"), ("Always dark", "escuro")]
FILTRO = [("Automatic (by time)", "auto"), ("Always on", "ligado"), ("Off", "desligado")]
CALOR = [("Mild", "5000"), ("Medium", "4500"), ("Strong", "3500"), ("Very strong", "2800")]
# Cores de destaque (as mesmas 9 do GNOME): (nome no gsettings, hexadecimal, nome mostrado)
DESTAQUES = [("mono", "mono", "Monochrome (follows light/dark)"), ("blue", "3584e4", "Blue"), ("teal", "2190a4", "Teal"), ("green", "3a944a", "Green"),
             ("yellow", "c88800", "Yellow"), ("orange", "ed5b00", "Orange"), ("red", "e62d42", "Red"),
             ("pink", "d56199", "Pink"), ("purple", "9141ac", "Purple"), ("slate", "6f8396", "Slate")]
TAMANHO_CURSOR = [("Normal", "24"), ("Large", "32"), ("Larger", "48")]


def temas_de_cursor():
    """Pastas de ícones que têm a subpasta "cursors" = temas de cursor instalados."""
    temas = set()
    for base in ("/usr/share/icons", os.path.expanduser("~/.local/share/icons"), os.path.expanduser("~/.icons")):
        if os.path.isdir(base):
            temas |= {t for t in os.listdir(base) if os.path.isdir(os.path.join(base, t, "cursors"))}
    return [(t, t) for t in sorted(temas)] or [("Adwaita", "Adwaita")]


def aplicar_cursor(tema, tamanho):
    """Vale na hora (hyprctl setcursor) e pros apps GTK (gsettings). No próximo login, o
    hyprland.conf lê CURSOR_TEMA/CURSOR_TAM do painel.conf (doc 25)."""
    rodar(["hyprctl", "setcursor", tema, tamanho])
    rodar(["gsettings", "set", "org.gnome.desktop.interface", "cursor-theme", tema])
    rodar(["gsettings", "set", "org.gnome.desktop.interface", "cursor-size", tamanho])


FONTES = [("Inter", "Inter 11"), ("Adwaita Sans", "Adwaita Sans 11")]
CSS_DESTAQUES = "".join(f".bolinha-{n} {{ background: #{h}; min-width: 22px; min-height: 22px; padding: 0; "
                        f"border-radius: 99px; }}\n" for n, h, _ in DESTAQUES if h != "mono") + \
    (".bolinha-mono { background: linear-gradient(135deg, #f0f0f0 50%, #1e1e1e 50%); min-width: 22px; "
     "min-height: 22px; padding: 0; border-radius: 99px; box-shadow: inset 0 0 0 1px alpha(currentColor, 0.3); }\n") + \
    ".bolinha-escolhida { box-shadow: 0 0 0 2px @window_bg_color, 0 0 0 4px currentColor; }"
# Curva dos cantos. "Round" (círculo) é o padrão: é a única que os apps (CSS) desenham igual ao Hyprland,
# então janelas, central, menus e Dock ficam com a MESMA curva (regra do Aero, doc 30).
CANTOS = [("Round", "2"), ("Smooth (Apple-like)", "3.5")]
ESMAECER = [("Off", "0"), ("Subtle", "0.08"), ("Medium", "0.15"), ("Strong", "0.25")]
OPACIDADE = [("70%", "0.70"), ("80%", "0.80"), ("90%", "0.90"), ("100%", "1.0")]
# taskbar: below ~35% Hyprland stops blurring it (ignore_alpha 0.3 in aparencia.conf)
BAR_OPACITY = [("35%", "0.35"), ("45%", "0.45"), ("60%", "0.60"), ("80%", "0.80"), ("100%", "1.0")]
WALL_ALVO = [("The workspace you are on", "workspace"), ("All workspaces", "all")]
VIDRO = [("Off (solid windows)", "desligado"), ("Subtle", "leve"), ("Medium", "media"), ("Strong", "forte")]
TEXTO = [("100%", "1.0"), ("110%", "1.1"), ("125%", "1.25"), ("150%", "1.5")]
# Horários de meia em meia hora: ("07:30", "07:30")
HORAS = [(f"{h:02d}:{m:02d}", f"{h:02d}:{m:02d}") for h in range(24) for m in (0, 30)]

# Velocidade das animações: multiplica o tempo (o Hyprland conta em décimos de segundo)
VELOCIDADE = [("Slow", "lento"), ("Normal", "normal"), ("Fast", "rapido")]
FATOR_VELOCIDADE = {"lento": 1.5, "normal": 1.0, "rapido": 0.6}
# (animação, tempo no "normal", curva, estilo) — curvas estilo macOS: "suave" sai rápido e pousa devagar;
# "mola" passa um tiquinho do ponto e volta (elástico leve)
ANIMACOES = [("windowsIn", 4, "mola", "popin 85%"), ("windowsOut", 3, "suave", "popin 90%"),
             ("windowsMove", 4, "suave", ""), ("fade", 3, "suave", ""),
             ("layersIn", 3, "suave", "fade"), ("layersOut", 2, "suave", "fade"),
             ("workspaces", 5, "suave", "slidefade 15%"), ("specialWorkspace", 4, "suave", "slidevert")]
# Workspace movement (Hyprland's own animation), chosen in Workspaces → Transition (TRANS_MOVE)
MOVIMENTO_AREAS = {"slidefade": "slidefade 15%", "slide": "slide", "vertical": "slidevert", "fade": "fade", "none": None}
MARCA_INICIO = "# >>> animações (gerado pelo Settings → Appearance → Motion; não editar à mão)"
MARCA_FIM = "# <<< animações"


def gerar_animacoes(ligado=True, velocidade="normal"):
    """Escreve o bloco "animations" no aparencia.conf, entre duas linhas-marca.
    Da próxima vez, troca só o que está entre as marcas (o resto do arquivo fica intacto)."""
    fator = FATOR_VELOCIDADE.get(velocidade, 1.0)
    bloco = [MARCA_INICIO, "animations {", f"    enabled = {'true' if ligado else 'false'}",
             "    bezier = suave, 0.25, 1, 0.5, 1",
             "    bezier = mola, 0.34, 1.4, 0.64, 1"]
    mov = ler_painel().get("TRANS_MOVE", "slidefade")
    for nome, tempo, curva, estilo in ANIMACOES:
        tempo = max(1, round(tempo * fator))
        if nome == "workspaces":
            if mov in MOVIMENTO_AREAS and MOVIMENTO_AREAS[mov] is None:
                bloco.append("    animation = workspaces, 0")
                continue
            estilo = MOVIMENTO_AREAS.get(mov, estilo)
        bloco.append(f"    animation = {nome}, 1, {tempo}, {curva}" + (f", {estilo}" if estilo else ""))
    bloco += ["    animation = border, 0", "}", MARCA_FIM]
    with open(APARENCIA) as f:
        linhas = f.read().splitlines()
    if MARCA_INICIO in linhas and MARCA_FIM in linhas:
        i, j = linhas.index(MARCA_INICIO), linhas.index(MARCA_FIM)
        linhas[i:j + 1] = bloco
    else:
        linhas += [""] + bloco
    with open(APARENCIA, "w") as f:
        f.write("\n".join(linhas) + "\n")


# Cada nível de vidro: (opacidade da janela ativa, das inativas, raio do desfoque, passadas)
# Regra do Aero (doc 44): a janela EM FOCO é sempre sólida (1.0); só as SEM foco ficam de vidro.
NIVEIS_VIDRO = {"desligado": (1.0, 1.0, 0, 0), "leve": (1.0, 0.85, 8, 3),
                "media": (1.0, 0.72, 12, 4), "forte": (1.0, 0.60, 16, 5)}


def gerar_aparencia(nivel, esmaecer="0.08", cantos="2"):
    """Reescreve só os números do aparencia.conf conforme o nível de vidro escolhido.
    Anda linha por linha lembrando em qual bloco está ("decoration", "blur", "shadow"...),
    pra mudar o "enabled" do blur sem mexer no "enabled" da sombra."""
    ativa, inativa, tamanho, passadas = NIVEIS_VIDRO[nivel]
    trocas = {("decoration", "active_opacity"): ativa, ("decoration", "inactive_opacity"): inativa,
              ("blur", "size"): tamanho, ("blur", "passes"): passadas,
              ("blur", "enabled"): "false" if nivel == "desligado" else "true",
              ("decoration", "dim_inactive"): "false" if esmaecer == "0" else "true",
              ("decoration", "dim_strength"): esmaecer if esmaecer != "0" else "0.08",
              ("decoration", "rounding_power"): cantos}
    with open(APARENCIA) as f:
        linhas = f.read().splitlines()
    blocos = []                                     # pilha: ["decoration", "blur"]
    for i, linha in enumerate(linhas):
        limpa = linha.split("#")[0].strip()
        if limpa.endswith("{"):
            blocos.append(limpa[:-1].strip())
        elif limpa == "}":
            blocos.pop()
        elif "=" in limpa and blocos:
            chave = limpa.split("=")[0].strip()
            if (blocos[-1], chave) in trocas:
                recuo = linha[:len(linha) - len(linha.lstrip())]
                comentario = linha[linha.index("#"):] if "#" in linha else ""
                valor = f"{recuo}{chave} = {trocas[(blocos[-1], chave)]}"
                linhas[i] = f"{valor:<29}{comentario}" if comentario else valor
    with open(APARENCIA, "w") as f:
        f.write("\n".join(linhas) + "\n")


class SecaoAparencia(Secao):
    id = "aparencia"
    titulo = "Theme"
    icone = "preferences-desktop-appearance-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        aplicar_css(CSS_DESTAQUES)          # as bolinhas coloridas da cor de destaque
        conteudo = Adw.PreferencesPage()

        tema = Adw.PreferencesGroup(title="Light and dark",
                                    description="Apps, title bars, the top bar, menu and notifications follow it.")
        # Dark mode = UM interruptor: liga escuro, desliga volta pro claro. Vale NA HORA (sem "Apply"),
        # igual ao botão "Dark mode" da central. O automático por horário é opcional, à parte (doc 35).
        self.escuro_sw = Adw.SwitchRow(title="Dark mode", subtitle="Dark look for the whole system")
        self.escuro_sw.connect("notify::active", self.ao_trocar_escuro)
        self.auto_sw = Adw.SwitchRow(title="Switch automatically by time",
                                     subtitle="Light during the day, dark at night")
        self.auto_sw.connect("notify::active", self.ao_trocar_auto)
        self.claro = combo("Light from", HORAS, 0)
        self.escuro = combo("Dark from", HORAS, 0)
        for r in (self.escuro_sw, self.auto_sw, self.claro, self.escuro):
            tema.add(r)
        self.mexendo_tema = False
        # se o tema mudar por fora (botão da central, horário), o interruptor acompanha
        self.interface = Gio.Settings.new("org.gnome.desktop.interface")
        self.interface.connect("changed::color-scheme", lambda *_: self.mostrar_tema())

        cor = Adw.PreferencesGroup(
            title="Theme color",
            description="The taskbar color. When synced, title bars, menus, notifications and "
                        "apps use it too.")
        self.sinc = Adw.SwitchRow(title="Sync everything with the taskbar",
                                  subtitle="Turn off to keep apps with their default colors")
        # Cores tiradas do papel de parede (doc 21). Desligado por padrão: vale na hora, sem "Apply".
        # Ligar guarda as cores escolhidas à mão (COR_*_MANUAL); desligar devolve elas.
        self.cores_wall = Adw.SwitchRow(title="Wallpaper color scheme",
                                        subtitle="Match the taskbar and system colors to the wallpaper")
        self.cores_wall.connect("notify::active", self.ao_mudar_cores_wall)
        self.mudando_cores_wall = False      # True = fomos nós que mexemos no botão (não reagir)
        self.cor_clara = self.linha_cor("Light theme color")
        self.cor_escura = self.linha_cor("Dark theme color")
        self.opacidade = combo("Opacity", OPACIDADE, 1)
        self.bar_opacity = combo("Taskbar opacity", BAR_OPACITY, 1, "Frosted: the taskbar blurs what is behind it")
        # Cor de destaque: vale na hora (sem Apply). Seleções, interruptores, Alt+Tab, print...
        self.linha_destaque = Adw.ActionRow(use_markup=False, title="Accent color")
        caixa = Gtk.Box(spacing=8, valign=Gtk.Align.CENTER)
        self.bolinhas = {}
        for nome, _hex, mostrado in DESTAQUES:
            b = Gtk.Button(tooltip_text=mostrado, valign=Gtk.Align.CENTER)
            b.add_css_class(f"bolinha-{nome}")
            b.connect("clicked", lambda _b, n=nome: self.escolher_destaque(n))
            self.bolinhas[nome] = b
            caixa.append(b)
        self.linha_destaque.add_suffix(caixa)
        for r in (self.sinc, self.cores_wall, self.cor_clara.linha, self.cor_escura.linha, self.opacidade, self.bar_opacity,
                  self.linha_destaque):
            cor.add(r)
        self.grupo_cor = cor

        filtro = Adw.PreferencesGroup(title="Night light",
                                      description="Reduces blue light to make the screen easier on the eyes at night.")
        self.filtro = combo("Night light", FILTRO, 0)
        self.inicio = combo("Starts at", HORAS, 0)
        self.fim = combo("Ends at", HORAS, 0)
        self.calor = combo("Warmth", CALOR, 1)
        self.filtro.connect("notify::selected", lambda *_: self.mostrar_horarios())
        for r in (self.filtro, self.inicio, self.fim, self.calor):
            filtro.add(r)

        # ---- Windows: botões de janela + vidro/cantos/esmaecer (reorganizado: antes "Glass") ----
        vidro = Adw.PreferencesGroup(title="Windows",
                                     description="Glass = transparency with a frosted blur (games and fullscreen "
                                                 "videos are always solid).")
        # Botões minimizar/maximizar/fechar. Vale NA HORA (sem Apply): ~/.local/bin/botoes-de-janela
        self.botoes_sw = Adw.SwitchRow(
            title="Window buttons",
            subtitle="Off = pure Hyprland, keyboard only: Super+↓ minimize · Super+↑ maximize · "
                     "Super+C quit app · Alt+F4 close window · Super+F fullscreen · Super+M minimized windows")
        self.botoes_sw.connect("notify::active", self.ao_trocar_botoes)
        self.mexendo_botoes = False
        self.vidro = combo("Glass effect", VIDRO, 2)
        self.texto = combo("Text size", TEXTO, 0)
        # Fonts (fonts.py, doc 47): only installed fonts are offered; lists refreshed in ao_mostrar
        self.fonte = combo("Interface font", [("Inter", "Inter")], 0)
        self.fonte_mono = combo("Monospace font", [("Adwaita Mono", "Adwaita Mono")], 0,
                                "Terminal and code. Applied to kitty right away")
        self.fonte_doc = combo("Document font", [("Same as interface", "")], 0,
                               "Long texts, and the default serif for web pages")
        self.temas_cursor = temas_de_cursor()
        self.cursor_tema = combo("Pointer style", self.temas_cursor, 0)
        self.cursor_tam = combo("Pointer size", TAMANHO_CURSOR, 0)
        self.esmaecer = combo("Dim unfocused windows", ESMAECER, 1,
                              "Super+O (or the blue title-bar button) keeps a window from dimming")
        self.cantos = combo("Window corners", CANTOS, 1)
        for r in (self.botoes_sw, self.vidro, self.cantos, self.esmaecer):
            vidro.add(r)

        fontes = Adw.PreferencesGroup(title="Text and pointer",
                                      description="Missing letters (other languages, emoji) fall back to Noto automatically.")
        for r in (self.texto, self.fonte, self.fonte_mono, self.fonte_doc, self.cursor_tema, self.cursor_tam):
            fontes.add(r)
        self.grupo_fontes = fontes

        movimento = Adw.PreferencesGroup(title="Motion",
                                         description="Windows pop in, workspaces slide, menus fade.")
        self.animar = Adw.SwitchRow(title="Animations")
        self.velocidade = combo("Animation speed", VELOCIDADE, 1)
        self.animar.connect("notify::active", lambda *_: self.velocidade.set_sensitive(self.animar.get_active()))
        movimento.add(self.animar)
        movimento.add(self.velocidade)

        avancado = Adw.PreferencesGroup()
        avancado.add(linha_app("Advanced appearance", "GTK theme, icons, cursor, fonts (nwg-look)",
                               ["nwg-look"], "preferences-system-symbolic"))

        # ---- Wallpaper: miniatura do atual + navegar nos repositórios + ajustes (antes era outra aba) ----
        papel = Adw.PreferencesGroup(title="Wallpaper")
        self.wall_linha = Adw.ActionRow(use_markup=False, title="Current wallpaper", activatable=True,
                                        subtitle="Browse your folder and GitHub collections")
        self.wall_mini = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, width_request=96, height_request=54,
                                     can_shrink=True, valign=Gtk.Align.CENTER, margin_top=6, margin_bottom=6)
        self.wall_mini.add_css_class("card")
        self.wall_linha.add_prefix(self.wall_mini)
        ajustar = Gtk.Button(label="Adjust", valign=Gtk.Align.CENTER, tooltip_text="Brightness, contrast, saturation")
        ajustar.connect("clicked", lambda *_: __import__("wall_ajustes").abrir(self.janela))
        self.wall_linha.add_suffix(ajustar)
        self.wall_linha.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        self.wall_linha.connect("activated", lambda *_: self.abrir_wallpapers())
        papel.add(self.wall_linha)
        # where a new wallpaper goes, from anywhere (this browser, Thunar, clipboard): papel-de-parede
        self.wall_alvo = combo("New wallpapers go to", WALL_ALVO, 0,
                               "Each workspace can also have its own: Workspaces → Choose…")
        self.wall_alvo.connect("notify::selected", lambda *_: self.carregando_alvo or
                               atualizar_painel(WALL_TARGET=valor_de(self.wall_alvo, WALL_ALVO)))
        self.carregando_alvo = False
        papel.add(self.wall_alvo)
        self.secao_wall = None

        aplicar = grupo_botao_aplicar(self.aplicar)
        for g in (papel, tema, cor, vidro, fontes, filtro, movimento, aplicar, avancado):
            conteudo.add(g)
        # Theme in the sidebar tree: each sub-page shows only its groups of this one page
        self.subs = {"colors": ("Colors and mode", [tema, cor, filtro, aplicar]),
                     "wallpaper": ("Wallpaper", [papel]),
                     "glass": ("Glass and windows", [vidro, aplicar]),
                     "text": ("Text and motion", [fontes, movimento, aplicar, avancado])}
        self.todos_grupos = (papel, tema, cor, vidro, fontes, filtro, movimento, aplicar, avancado)
        self.conteudo = conteudo
        self.pagina = pagina(self.titulo, conteudo)

    def linha_cor(self, titulo):
        """Linha com um botão que abre o seletor de cor do GTK (Gtk.ColorDialog)."""
        botao = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(with_alpha=False), valign=Gtk.Align.CENTER)
        linha = Adw.ActionRow(use_markup=False, title=titulo)
        linha.add_suffix(botao)
        botao.linha = linha
        return botao

    @staticmethod
    def para_hex(botao):
        c = botao.get_rgba()
        return "".join(f"{round(x * 255):02x}" for x in (c.red, c.green, c.blue))

    @staticmethod
    def de_hex(botao, texto):
        c = Gdk.RGBA()
        c.parse("#" + texto)
        botao.set_rgba(c)

    def modo_tema(self):
        """O TEMA_MODO que os interruptores querem: auto, escuro ou claro."""
        if self.auto_sw.get_active():
            return "auto"
        return "escuro" if self.escuro_sw.get_active() else "claro"

    def mostrar_tema(self):
        """Interruptor = o tema EM USO agora (no automático também)."""
        self.mexendo_tema = True
        self.escuro_sw.set_active(self.interface.get_string("color-scheme") == "prefer-dark")
        self.mexendo_tema = False

    def ao_trocar_escuro(self, *_):
        if self.mexendo_tema:
            return
        self.mexendo_tema = True
        self.auto_sw.set_active(False)          # escolheu na mão: o automático desliga
        self.mexendo_tema = False
        self.mostrar_horarios()
        atualizar_painel(TEMA_MODO=self.modo_tema())
        em_segundo_plano(lambda: rodar([HYPR_TEMA]))

    def ao_trocar_auto(self, *_):
        self.mostrar_horarios()
        if self.mexendo_tema:
            return
        atualizar_painel(TEMA_MODO=self.modo_tema(), TEMA_CLARO=valor_de(self.claro, HORAS),
                         TEMA_ESCURO=valor_de(self.escuro, HORAS))
        em_segundo_plano(lambda: rodar([HYPR_TEMA]))

    def mostrar_horarios(self):
        """Os horários só aparecem no modo automático."""
        auto_tema = self.auto_sw.get_active()
        self.claro.set_visible(auto_tema)
        self.escuro.set_visible(auto_tema)
        auto_filtro = valor_de(self.filtro, FILTRO) == "auto"
        self.inicio.set_visible(auto_filtro)
        self.fim.set_visible(auto_filtro)
        self.calor.set_visible(valor_de(self.filtro, FILTRO) != "desligado")

    # ---- wallpaper ----
    def abrir_wallpapers(self):
        """Abre o navegador de wallpapers (sec_wallpapers) como sub-página, com o "<" pra voltar."""
        from sec_wallpapers import SecaoWallpapers
        if self.secao_wall is None:
            self.secao_wall = SecaoWallpapers(self.janela)
        self.janela.empilhar(self.secao_wall.pagina)
        self.secao_wall.ao_mostrar()
        return self.secao_wall

    def mostrar_wallpaper(self, p):
        self.carregando_alvo = True
        self.wall_alvo.set_selected(indice(WALL_ALVO, p.get("WALL_TARGET", "workspace"), 0))
        self.carregando_alvo = False
        # the workspace you are on: its own wallpaper, if it has one
        from sec_areas import area_ativa
        from sec_widgets import ler as ler_widgets
        area = area_ativa()
        propria = ((ler_widgets().get("por_area") or {}).get(str(area)) or {}).get("wallpaper")
        if propria and os.path.isfile(propria):
            self.wall_linha.set_subtitle(f"Workspace {area}'s own wallpaper · browse your folder and GitHub collections")
            atual = propria
        else:
            self.wall_linha.set_subtitle(f"System wallpaper (workspace {area} and every one without its own)")
            atual = str(p.get("WALL_ORIGINAL", "")).strip('"')
        if not os.path.isfile(atual):
            try:
                with open(os.path.expanduser("~/.config/hypr/hyprpaper.conf")) as f:
                    atual = next((l.split("=", 1)[1].strip() for l in f if l.strip().startswith("path")), "")
            except OSError:
                atual = ""
        if os.path.isfile(atual):
            self.wall_linha.set_title(os.path.basename(atual))
            try:
                self.wall_mini.set_paintable(Gdk.Texture.new_from_filename(atual))
            except Exception:  # noqa: BLE001 — sem miniatura, só o nome
                pass

    # ---- botões de janela ----
    def ao_trocar_botoes(self, *_):
        if self.mexendo_botoes:
            return
        ligar = self.botoes_sw.get_active()
        em_segundo_plano(lambda: __import__("subprocess").run(
            [os.path.expanduser("~/.local/bin/botoes-de-janela"), "on" if ligar else "off"]))
        __import__("comum").mostrar_bolinhas(ligar)      # as bolinhas deste Settings, na hora
        self.janela.avisar("Window buttons on" if ligar else "Window buttons off: use Super+↑ ↓ and Super+C")

    def abrir_sub(self, sub):
        titulo, grupos = self.subs.get(sub) or ("Theme", self.todos_grupos)
        for g in self.todos_grupos:
            g.set_visible(g in grupos)
        self.pagina.set_title(titulo)
        self.conteudo.scroll_to_top()

    def ao_mostrar(self):
        p = ler_painel()
        self.mexendo_botoes = True
        self.botoes_sw.set_active(p.get("BOTOES_JANELA", "1") != "0")
        self.mexendo_botoes = False
        self.mostrar_wallpaper(p)
        self.mexendo_tema = True
        self.auto_sw.set_active(p.get("TEMA_MODO", "claro") == "auto")   # padrão: fixo (o Aero escolheu; doc 30)
        self.mexendo_tema = False
        self.mostrar_tema()
        self.claro.set_selected(indice(HORAS, p.get("TEMA_CLARO", "07:00")))
        self.escuro.set_selected(indice(HORAS, p.get("TEMA_ESCURO", "18:00")))
        self.filtro.set_selected(indice(FILTRO, p.get("FILTRO_MODO", "auto")))
        self.inicio.set_selected(indice(HORAS, p.get("FILTRO_INICIO", "19:00")))
        self.fim.set_selected(indice(HORAS, p.get("FILTRO_FIM", "07:00")))
        self.calor.set_selected(indice(CALOR, p.get("FILTRO_TEMP", "4500"), 1))
        self.vidro.set_selected(indice(VIDRO, p.get("VIDRO", "media"), 2))
        self.esmaecer.set_selected(indice(ESMAECER, p.get("ESMAECER", "0.08"), 1))
        self.cantos.set_selected(indice(CANTOS, p.get("CANTOS", "2"), 0))
        self.sinc.set_active(p.get("COR_SINC", "1") == "1")
        self.animar.set_active(p.get("ANIM", "1") == "1")
        self.velocidade.set_selected(indice(VELOCIDADE, p.get("ANIM_VEL", "normal"), 1))
        self.mudando_cores_wall = True
        self.cores_wall.set_active(p.get("WALL_CORES", "0") == "1")
        self.mudando_cores_wall = False
        self.mostrar_cores(p)
        self.opacidade.set_selected(indice(OPACIDADE, p.get("COR_ALFA", "0.80"), 1))
        self.bar_opacity.set_selected(indice(BAR_OPACITY, p.get("BAR_ALPHA", "0.45"), 1))
        fator = rodar(["gsettings", "get", "org.gnome.desktop.interface", "text-scaling-factor"]).stdout.strip()
        self.texto.set_selected(min(range(len(TEXTO)),
                                    key=lambda i: abs(float(TEXTO[i][1]) - float(fator or 1))))
        import fonts
        self.listas_fontes = {}
        for linha, opcoes, chave in ((self.fonte, fonts.INTERFACE, "font-name"),
                                     (self.fonte_mono, fonts.MONOSPACE, "monospace-font-name"),
                                     (self.fonte_doc, fonts.DOCUMENT, "document-font-name")):
            lista = fonts.available(opcoes)
            self.listas_fontes[linha] = lista
            linha.set_model(Gtk.StringList.new([t for t, _ in lista]))
            atual = fonts.gsettings_get(chave)
            if linha is self.fonte_doc and atual == fonts.gsettings_get("font-name"):
                atual = ""                                   # documento igual à interface = "Same as interface"
            linha.set_selected(next((i for i, (_, f) in enumerate(lista) if f == atual), 0))
        self.marcar_destaque(p.get("COR_DESTAQUE", "3584e4"))
        self.cursor_tema.set_selected(indice(self.temas_cursor, p.get("CURSOR_TEMA", "Adwaita"), 0))
        self.cursor_tam.set_selected(indice(TAMANHO_CURSOR, p.get("CURSOR_TAM", "24"), 0))
        self.mostrar_horarios()

    def mostrar_cores(self, p):
        """Os botões de cor mostram as cores EM USO. Com as cores do wallpaper ligadas, eles ficam
        travados (quem escolhe é o wallpaper); as suas ficam guardadas pra quando desligar."""
        self.de_hex(self.cor_clara, p.get("COR_CLARA", "f6f6f6"))
        self.de_hex(self.cor_escura, p.get("COR_ESCURA", "1e1e1e"))
        do_wall = p.get("WALL_CORES", "0") == "1"
        for botao in (self.cor_clara, self.cor_escura):
            botao.linha.set_sensitive(not do_wall)
            botao.linha.set_subtitle("From the wallpaper" if do_wall else "")

    def ao_mudar_cores_wall(self, *_):
        if self.mudando_cores_wall:
            return
        ligar = self.cores_wall.get_active()
        self.cores_wall.set_sensitive(False)          # trava até terminar (calcular leva ~1 s)
        script = os.path.expanduser("~/.local/bin/cores-do-wallpaper")

        def pronto(_resultado):
            self.cores_wall.set_sensitive(True)
            self.mostrar_cores(ler_painel())
            self.janela.avisar("Colors from the wallpaper" if ligar else "Your colors are back")
        em_segundo_plano(lambda: rodar([script, "--ligar" if ligar else "--desligar"]), pronto)

    def marcar_destaque(self, cor):
        for nome, hexa, _ in DESTAQUES:
            (self.bolinhas[nome].add_css_class if hexa == cor else self.bolinhas[nome].remove_css_class)(
                "bolinha-escolhida")

    def escolher_destaque(self, nome):
        """Grava a cor e aplica já: o hypr-tema regera cores-tema.css (apps GTK), a barra etc."""
        hexa = next(h for n, h, _ in DESTAQUES if n == nome)
        atualizar_painel(COR_DESTAQUE=hexa)
        rodar(["gsettings", "set", "org.gnome.desktop.interface", "accent-color",
               "slate" if nome == "mono" else nome])  # apps do GNOME (não têm "mono": o cinza é o mais perto)
        self.marcar_destaque(hexa)
        em_segundo_plano(lambda: rodar([HYPR_TEMA, "--forcar"]),
                         lambda _r: self.janela.avisar("Accent color applied (open apps update when reopened)"))

    def aplicar(self):
        vidro = valor_de(self.vidro, VIDRO)
        atualizar_painel(
            TEMA_MODO=self.modo_tema(), TEMA_CLARO=valor_de(self.claro, HORAS),
            TEMA_ESCURO=valor_de(self.escuro, HORAS), FILTRO_MODO=valor_de(self.filtro, FILTRO),
            FILTRO_INICIO=valor_de(self.inicio, HORAS), FILTRO_FIM=valor_de(self.fim, HORAS),
            FILTRO_TEMP=valor_de(self.calor, CALOR), VIDRO=vidro, ESMAECER=valor_de(self.esmaecer, ESMAECER),
            COR_SINC="1" if self.sinc.get_active() else "0", COR_CLARA=self.para_hex(self.cor_clara),
            COR_ESCURA=self.para_hex(self.cor_escura), COR_ALFA=valor_de(self.opacidade, OPACIDADE), BAR_ALPHA=valor_de(self.bar_opacity, BAR_OPACITY),
            CANTOS=valor_de(self.cantos, CANTOS), CURSOR_TEMA=valor_de(self.cursor_tema, self.temas_cursor),
            CURSOR_TAM=valor_de(self.cursor_tam, TAMANHO_CURSOR), ANIM="1" if self.animar.get_active() else "0", ANIM_VEL=valor_de(self.velocidade, VELOCIDADE))
        gerar_aparencia(vidro, valor_de(self.esmaecer, ESMAECER), valor_de(self.cantos, CANTOS))
        gerar_animacoes(self.animar.get_active(), valor_de(self.velocidade, VELOCIDADE))
        rodar(["gsettings", "set", "org.gnome.desktop.interface", "text-scaling-factor", valor_de(self.texto, TEXTO)])
        import fonts
        escolha = {l: self.listas_fontes[l][l.get_selected()][1] for l in (self.fonte, self.fonte_mono, self.fonte_doc)}
        fonts.apply(escolha[self.fonte], escolha[self.fonte_mono], escolha[self.fonte_doc])
        aplicar_cursor(valor_de(self.cursor_tema, self.temas_cursor), valor_de(self.cursor_tam, TAMANHO_CURSOR))
        rodar(["hyprctl", "reload"])
        rodar([HYPR_TEMA, "--forcar"])    # aplica tema e filtro já, sem esperar o próximo minuto
        self.janela.avisar("Appearance applied")
