"""
sec_daimon.py — Settings page for Daimon compositor.

Configures:
- Window visual style (Solid / Shadow dots / Dilution)
- Corner radius (smooth continuous curvature)
- Alt+Tab mode (Native Solid MRU cards / Classic shell)
- Default window mode (Tiling / Floating)
- Wallpaper transitions (Wave animation type & duration)
- Gaming performance (Variable Refresh Rate VRR & async tearing)
- Dedicated GPU offload (NVIDIA application list)

Saves atomically to ~/.config/aero/daimon.conf, which Daimon reloads live.
"""
import os
import shutil

from gi.repository import Adw, Gtk

from comum import Secao, combo, indice, pagina, rodar, valor_de

DAIMON_CONF = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "aero",
    "daimon.conf",
)
PAINEL_CONF = os.path.expanduser("~/.config/hypr/painel.conf")

ESTILOS = [
    ("Solid (Material 3)", "solid"),
    ("Shadow dots (DotStyle)", "sombra"),
    ("Dilution (Fluid mesh)", "diluicao"),
]

ALTTAB_MODOS = [
    ("Native (Solid cards)", "nativo"),
    ("Shell (Classic)", "shell"),
]

VRR_MODOS = [
    ("Games only (Recommended)", "jogos"),
    ("Always enabled", "sempre"),
    ("Disabled", "nao"),
]

ONDA_MODOS = [
    ("Wave animation", "onda"),
    ("Instant (None)", "nenhuma"),
]

RAIO_MIN, RAIO_MAX, RAIO_PADRAO = 0, 48, 18


def ler_daimon():
    valores = {}
    try:
        with open(DAIMON_CONF) as f:
            for linha in f:
                linha = linha.strip()
                if not linha or linha.startswith("#"):
                    continue
                if "=" in linha:
                    k, v = linha.split("=", 1)
                    valores[k.strip()] = v.strip()
    except OSError:
        pass
    return valores


def gravar_daimon(**chaves):
    """Grava atomicamente as chaves fornecidas no daimon.conf, preservando comentários."""
    linhas = []
    try:
        with open(DAIMON_CONF) as f:
            linhas = f.read().splitlines()
    except OSError:
        linhas = ["# Daimon Compositor Configuration (Settings → Daimon)"]
    falta = dict(chaves)
    novas_linhas = []
    for l in linhas:
        l_strip = l.strip()
        if l_strip.startswith("#") or "=" not in l:
            novas_linhas.append(l)
            continue
        k = l.split("=", 1)[0].strip()
        if k in falta:
            val = falta.pop(k)
            if val is not None:
                novas_linhas.append(f"{k} = {val}")
        else:
            novas_linhas.append(l)
    for k, v in falta.items():
        if v is not None:
            novas_linhas.append(f"{k} = {v}")
    os.makedirs(os.path.dirname(DAIMON_CONF), exist_ok=True)
    tmp = DAIMON_CONF + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(novas_linhas) + "\n")
    os.replace(tmp, DAIMON_CONF)

    # Avisa ao vivo caso o hyprctl-compat ou daimonctl estejam no ambiente
    if os.environ.get("DAIMON_SOCKET") or os.environ.get("WAYLAND_DISPLAY"):
        rodar(["daimonctl", "config-reload"])


def aplicar_raio(n):
    """Sincroniza o raio dos cantos no painel.conf, daimon.conf e ao vivo."""
    n = max(RAIO_MIN, min(RAIO_MAX, int(n)))
    gravar_daimon(raio_cantos=n)
    # Atualiza painel.conf também para coerência com o shell
    try:
        linhas = open(PAINEL_CONF).read().splitlines() if os.path.exists(PAINEL_CONF) else []
        achou = False
        for i, l in enumerate(linhas):
            if l.partition("=")[0].strip() == "RAIO_CANTOS":
                linhas[i] = f"RAIO_CANTOS={n}"
                achou = True
        if not achou:
            linhas.append(f"RAIO_CANTOS={n}")
        tmp = PAINEL_CONF + ".tmp"
        with open(tmp, "w") as f:
            f.write("\n".join(linhas) + "\n")
        os.replace(tmp, PAINEL_CONF)
    except OSError:
        pass
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        rodar(["hyprctl", "keyword", "decoration:rounding", str(n)])
    return n


class SecaoDaimon(Secao):
    id = "daimon"
    titulo = "Daimon"
    icone = "preferences-system-windows-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = True
        self.gpu_rows = []

        conteudo = Adw.PreferencesPage()

        # =========================================================================
        # Grupo 1: Janelas e Visual
        # =========================================================================
        grp_visual = Adw.PreferencesGroup(
            title="Window Appearance",
            description="Rendering style, analytical corners and workspace layout",
        )

        conf = ler_daimon()

        # Estilo das janelas
        estilo_atual = conf.get("janela_estilo", "solid")
        self.row_estilo = combo(
            "Window style",
            ESTILOS,
            indice(ESTILOS, estilo_atual, 0),
            "Visual treatment for windows, borders and background",
        )
        self.row_estilo.connect("notify::selected", self.ao_mudar_estilo)
        grp_visual.add(self.row_estilo)

        # Raio dos cantos
        raio_ini = int(conf.get("raio_cantos", RAIO_PADRAO))
        self.row_raio = Adw.SpinRow.new_with_range(RAIO_MIN, RAIO_MAX, 1)
        self.row_raio.set_title("Corner roundness")
        self.row_raio.set_subtitle("Smooth continuous corner radius (px)")
        self.row_raio.set_value(raio_ini)
        self.row_raio.connect("notify::value", self.ao_mudar_raio)
        grp_visual.add(self.row_raio)

        # Alt+Tab nativo / shell
        alttab_atual = conf.get("alttab", "nativo")
        self.row_alttab = combo(
            "Alt+Tab switcher",
            ALTTAB_MODOS,
            indice(ALTTAB_MODOS, alttab_atual, 0),
            "Native Solid MRU cards with preview or classic shell switcher",
        )
        self.row_alttab.connect("notify::selected", self.ao_mudar_alttab)
        grp_visual.add(self.row_alttab)

        # Mosaico padrão
        modo_tiling = conf.get("modo", "tiling") == "tiling"
        self.row_tiling = Adw.SwitchRow(
            title="Default window tiling",
            subtitle="Automatically arrange new windows in a clean side-by-side tile",
            active=modo_tiling,
        )
        self.row_tiling.connect("notify::active", self.ao_mudar_tiling)
        grp_visual.add(self.row_tiling)

        conteudo.add(grp_visual)

        # =========================================================================
        # Grupo 2: Transições de Wallpaper
        # =========================================================================
        grp_papel = Adw.PreferencesGroup(
            title="Wallpaper Transitions",
            description="Fluid GPU shader effects when switching workspaces",
        )

        onda_atual = conf.get("papel_transicao", "onda")
        self.row_onda = combo(
            "Transition animation",
            ONDA_MODOS,
            indice(ONDA_MODOS, onda_atual, 0),
            "Smooth circular liquid unroll or instantaneous change",
        )
        self.row_onda.connect("notify::selected", self.ao_mudar_onda)
        grp_papel.add(self.row_onda)

        dur_atual = int(conf.get("papel_duracao_ms", 1200))
        self.row_duracao = Adw.SpinRow.new_with_range(100, 5000, 100)
        self.row_duracao.set_title("Wave duration")
        self.row_duracao.set_subtitle("Duration of the circular liquid expansion (ms)")
        self.row_duracao.set_value(dur_atual)
        self.row_duracao.connect("notify::value", self.ao_mudar_duracao)
        grp_papel.add(self.row_duracao)

        conteudo.add(grp_papel)

        # =========================================================================
        # Grupo 3: Jogos e Desempenho
        # =========================================================================
        grp_jogos = Adw.PreferencesGroup(
            title="Gaming & Direct Scanout",
            description="High refresh rate display features and low-latency presentation",
        )

        vrr_atual = conf.get("vrr", "jogos")
        self.row_vrr = combo(
            "Variable Refresh Rate (VRR)",
            VRR_MODOS,
            indice(VRR_MODOS, vrr_atual, 0),
            "Adaptive sync / G-Sync / FreeSync timing for fullscreen games",
        )
        self.row_vrr.connect("notify::selected", self.ao_mudar_vrr)
        grp_jogos.add(self.row_vrr)

        tearing_ativo = conf.get("jogos.rasgo", "sim") in ("sim", "yes", "true", "on")
        self.row_tearing = Adw.SwitchRow(
            title="Async page flips (tearing)",
            subtitle="Allow immediate unbuffered presentation when requested by games",
            active=tearing_ativo,
        )
        self.row_tearing.connect("notify::active", self.ao_mudar_tearing)
        grp_jogos.add(self.row_tearing)

        conteudo.add(grp_jogos)

        # =========================================================================
        # Grupo 4: GPU Dedicada (NVIDIA Offload)
        # =========================================================================
        self.grp_gpu = Adw.PreferencesGroup(
            title="Dedicated GPU Applications",
            description="Apps configured to launch on the high-performance NVIDIA GPU",
        )

        # Linha para adicionar novo app
        self.row_add_app = Adw.EntryRow(title="Add application ID (e.g. steam, gamescope)")
        btn_add = Gtk.Button.new_from_icon_name("list-add-symbolic")
        btn_add.set_valign(Gtk.Align.CENTER)
        btn_add.add_css_class("flat")
        btn_add.connect("clicked", self.ao_adicionar_gpu_app)
        self.row_add_app.add_suffix(btn_add)
        self.row_add_app.connect("entry-activated", self.ao_adicionar_gpu_app)
        self.grp_gpu.add(self.row_add_app)

        conteudo.add(self.grp_gpu)

        self.pagina = pagina("Daimon", conteudo)
        self.carregando = False
        self.recarregar_gpu_apps()

    def ao_mostrar(self):
        self.carregando = True
        conf = ler_daimon()

        self.row_estilo.set_selected(indice(ESTILOS, conf.get("janela_estilo", "solid"), 0))
        self.row_raio.set_value(int(conf.get("raio_cantos", RAIO_PADRAO)))
        self.row_alttab.set_selected(indice(ALTTAB_MODOS, conf.get("alttab", "nativo"), 0))
        self.row_tiling.set_active(conf.get("modo", "tiling") == "tiling")
        self.row_onda.set_selected(indice(ONDA_MODOS, conf.get("papel_transicao", "onda"), 0))
        self.row_duracao.set_value(int(conf.get("papel_duracao_ms", 1200)))
        self.row_vrr.set_selected(indice(VRR_MODOS, conf.get("vrr", "jogos"), 0))
        self.row_tearing.set_active(conf.get("jogos.rasgo", "sim") in ("sim", "yes", "true", "on"))

        self.recarregar_gpu_apps()
        self.carregando = False

    def recarregar_gpu_apps(self):
        for r in self.gpu_rows:
            self.grp_gpu.remove(r)
        self.gpu_rows.clear()

        conf = ler_daimon()
        for k, v in sorted(conf.items()):
            if k.startswith("gpu_") and v.lower() == "nvidia":
                app_name = k[4:]
                row = Adw.ActionRow(title=app_name, subtitle="Launches with dedicated NVIDIA GPU")
                btn_del = Gtk.Button.new_from_icon_name("user-trash-symbolic")
                btn_del.set_valign(Gtk.Align.CENTER)
                btn_del.add_css_class("flat")
                btn_del.connect("clicked", lambda _, a=app_name: self.ao_remover_gpu_app(a))
                row.add_suffix(btn_del)
                self.grp_gpu.add(row)
                self.gpu_rows.append(row)

    def ao_mudar_estilo(self, row, _param):
        if self.carregando:
            return
        val = valor_de(row, ESTILOS)
        gravar_daimon(janela_estilo=val)

    def ao_mudar_raio(self, row, _param):
        if self.carregando:
            return
        aplicar_raio(row.get_value())

    def ao_mudar_alttab(self, row, _param):
        if self.carregando:
            return
        val = valor_de(row, ALTTAB_MODOS)
        gravar_daimon(alttab=val)

    def ao_mudar_tiling(self, row, _param):
        if self.carregando:
            return
        val = "tiling" if row.get_active() else "janelas"
        gravar_daimon(modo=val)

    def ao_mudar_onda(self, row, _param):
        if self.carregando:
            return
        val = valor_de(row, ONDA_MODOS)
        gravar_daimon(papel_transicao=val)

    def ao_mudar_duracao(self, row, _param):
        if self.carregando:
            return
        gravar_daimon(papel_duracao_ms=int(row.get_value()))

    def ao_mudar_vrr(self, row, _param):
        if self.carregando:
            return
        val = valor_de(row, VRR_MODOS)
        gravar_daimon(vrr=val)

    def ao_mudar_tearing(self, row, _param):
        if self.carregando:
            return
        val = "sim" if row.get_active() else "nao"
        gravar_daimon(**{"jogos.rasgo": val})

    def ao_adicionar_gpu_app(self, *_args):
        app = self.row_add_app.get_text().strip().lower()
        if not app:
            return
        chave = f"gpu_{app}"
        gravar_daimon(**{chave: "nvidia"})
        self.row_add_app.set_text("")
        self.recarregar_gpu_apps()

    def ao_remover_gpu_app(self, app):
        chave = f"gpu_{app}"
        gravar_daimon(**{chave: None})
        self.recarregar_gpu_apps()
