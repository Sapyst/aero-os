#!/usr/bin/env python3
"""
solido.py — o visual "Solid": formas chapadas e sólidas no estilo do Pixel (Material You / Material 3).

  ativo()            o visual atual é o Solid?
  esquema()          a paleta tonal do momento (dict nome -> "rrggbb"), derivada da cor do wallpaper
  rgb(nome)          a mesma cor como (r, g, b) de 0 a 1, pra desenhar com cairo
  raio()             o raio dos cantos do sistema (RAIO_CANTOS do painel.conf; 24 se não tiver)
  css_shell()        o CSS GTK das peças do shell no Solid (cores com nome + classes .solid-*)
  aplicar_css()      carrega o css_shell() no display (uma vez por processo)
  vigiar(f)          a paleta mudou (wallpaper novo, claro/escuro): relê, recarrega o CSS e chama f()
  metaball(...)      uma animação de metaball chapado (liquido.CampoSolido) que só existe enquanto anima
  suave/ease         curvas de animação (smoothstep; "emphasized decelerate" do Material)

Como a paleta nasce (a ideia do Material You, sem a biblioteca do Google):
  1) a SEMENTE é a cor que o sistema já tira do wallpaper (COR_DESTAQUE_WALL, gravada pelo
     cores-do-wallpaper); sem ela, a cor de destaque escolhida (COR_DESTAQUE); wallpaper sem cor = cinza;
  2) a semente vira matiz + croma no espaço CIELAB (LCh), onde "luminosidade" é a do olho humano;
  3) cada papel do esquema é um TOM (0 = preto, 100 = branco) de uma das paletas: primária (croma 36),
     secundária (16), terciária (matiz +60°, 24), neutra (6) e neutra-variante (8) — os mesmos tons do
     esquema "Tonal Spot" do Material 3. Cor fora do que a tela mostra: o croma desce até caber.

Linha de comando (o hypr-tema e o aero-visual usam):
  solido.py vars escuro|claro      variáveis de shell (SOLID_SURFACE=... SOLID_PRIMARY=...)
  solido.py gtk escuro|claro       CSS pros apps GTK4/libadwaita (Settings incluso)
  solido.py json escuro|claro      o esquema inteiro
  solido.py wallpaper ARQUIVO      desenha o wallpaper padrão do Solid (formas chapadas)
"""
import json
import math
import os
import sys

H = os.path.expanduser("~")
PAINEL = os.path.join(H, ".config/hypr/painel.conf")
ESTADO_TEMA = os.path.join(H, ".cache/hypr-tema/tema")
NOME = "Solid"
SEMENTE_PADRAO = "4c6fd8"           # sem cor nenhuma escolhida: um azul "Pixel"


# ---------- painel.conf ----------

def ler_painel():
    v = {}
    try:
        for linha in open(PAINEL):
            if "=" in linha and not linha.startswith("#"):
                k, x = linha.rstrip("\n").split("=", 1)
                v[k.strip()] = x.strip().strip('"')
    except OSError:
        pass
    return v


def ativo(painel=None):
    return (painel or ler_painel()).get("VISUAL") == NOME


def raio(painel=None):
    r = (painel or ler_painel()).get("RAIO_CANTOS", "")
    return max(0, min(48, int(r))) if r.isdigit() else 24


def escuro_agora():
    """O tema que o hypr-tema aplicou por último (escuro/claro); sem ele, o TEMA_MODO."""
    try:
        return open(ESTADO_TEMA).read().strip() != "claro"
    except OSError:
        return ler_painel().get("TEMA_MODO", "escuro") != "claro"


# ---------- cor: sRGB <-> CIELAB (D65) ----------

def _lin(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _gama(c):
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


_BRANCO = (0.95047, 1.0, 1.08883)


def para_lab(r, g, b):
    r, g, b = _lin(r), _lin(g), _lin(b)
    x = 0.4124 * r + 0.3576 * g + 0.1805 * b
    y = 0.2126 * r + 0.7152 * g + 0.0722 * b
    z = 0.0193 * r + 0.1192 * g + 0.9505 * b
    f = [t ** (1 / 3) if t > 216 / 24389 else (24389 / 27 * t + 16) / 116
         for t in (x / _BRANCO[0], y / _BRANCO[1], z / _BRANCO[2])]
    return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])


def de_lab(L, a, b):
    fy = (L + 16) / 116
    fx, fz = fy + a / 500, fy - b / 200
    inv = lambda f: f ** 3 if f ** 3 > 216 / 24389 else (116 * f - 16) / (24389 / 27)  # noqa: E731
    x, y, z = inv(fx) * _BRANCO[0], inv(fy) * _BRANCO[1], inv(fz) * _BRANCO[2]
    r = 3.2406 * x - 1.5372 * y - 0.4986 * z
    g = -0.9689 * x + 1.8758 * y + 0.0415 * z
    bb = 0.0557 * x - 0.2040 * y + 1.0570 * z
    return _gama(max(r, 0)) if r > 0 else r, _gama(max(g, 0)) if g > 0 else g, _gama(max(bb, 0)) if bb > 0 else bb


def tom(matiz, croma, t):
    """A cor de tom t (0..100) com essa matiz (graus) e croma; croma reduzido até caber no sRGB."""
    if t >= 100:
        return (1.0, 1.0, 1.0)
    if t <= 0:
        return (0.0, 0.0, 0.0)
    h = math.radians(matiz)
    lo, hi = 0.0, croma
    melhor = de_lab(t, 0, 0)
    for _ in range(18):
        c = (lo + hi) / 2 if _ else hi
        rgb = de_lab(t, c * math.cos(h), c * math.sin(h))
        if all(-0.0005 <= x <= 1.0005 for x in rgb):
            melhor, lo = rgb, c
            if not _:
                break
        else:
            hi = c
    return tuple(max(0.0, min(1.0, x)) for x in melhor)


def hexa(rgb):
    return "".join(f"{round(max(0, min(1, c)) * 255):02x}" for c in rgb)


def de_hexa(s):
    s = (s or "").strip().lstrip("#")
    if len(s) != 6:
        raise ValueError(s)
    return tuple(int(s[i:i + 2], 16) / 255 for i in (0, 2, 4))


# ---------- o esquema ----------

def semente(painel=None):
    p = painel or ler_painel()
    for chave in (("COR_DESTAQUE_WALL",) if p.get("WALL_CORES") == "1" else ()) + ("COR_DESTAQUE",):
        try:
            return de_hexa(p.get(chave, ""))
        except ValueError:
            continue
    return None                                     # "mono": paleta cinza


# (papel, paleta, tom no escuro, tom no claro)
PAPEIS = [
    ("primary", "p", 80, 40), ("on_primary", "p", 20, 100),
    ("primary_container", "p", 30, 90), ("on_primary_container", "p", 90, 10),
    ("secondary", "s", 80, 40), ("on_secondary", "s", 20, 100),
    ("secondary_container", "s", 30, 90), ("on_secondary_container", "s", 90, 10),
    ("tertiary", "t", 80, 40), ("on_tertiary", "t", 20, 100),
    ("tertiary_container", "t", 30, 90), ("on_tertiary_container", "t", 90, 10),
    ("error", "e", 80, 40), ("on_error", "e", 20, 100),
    ("error_container", "e", 30, 90), ("on_error_container", "e", 90, 10),
    ("surface", "n", 6, 98), ("surface_dim", "n", 6, 87), ("surface_bright", "n", 24, 98),
    ("surface_container_lowest", "n", 4, 100), ("surface_container_low", "n", 10, 96),
    ("surface_container", "n", 12, 94), ("surface_container_high", "n", 17, 92),
    ("surface_container_highest", "n", 22, 90),
    ("on_surface", "n", 90, 10), ("on_surface_variant", "v", 80, 30),
    ("outline", "v", 60, 50), ("outline_variant", "v", 30, 80),
    ("inverse_surface", "n", 90, 20), ("inverse_on_surface", "n", 20, 95),
    ("inverse_primary", "p", 40, 80), ("scrim", "n", 0, 0),
]


def esquema(escuro=None, painel=None):
    """O esquema do momento: {papel: "rrggbb"} (mais "dark": True/False)."""
    p = painel or ler_painel()
    if escuro is None:
        escuro = escuro_agora()
    s = semente(p)
    if s is None:
        matiz, c = 260.0, 0.0                       # sem cor: tudo cinza (um fio de azul no primário)
        cp, cs, ct, cn, cv = 6.0, 3.0, 4.0, 1.5, 2.0
    else:
        L, a, b = para_lab(*s)
        matiz, c = math.degrees(math.atan2(b, a)) % 360, math.hypot(a, b)
        cp, cs, ct, cn, cv = 36.0, 16.0, 24.0, 6.0, 8.0          # os cromas do "Tonal Spot"
        if c < 8:                                   # semente quase cinza: paleta contida
            cp, cs, ct, cn, cv = 10.0, 5.0, 8.0, 2.0, 3.0
    paletas = {"p": (matiz, cp), "s": (matiz, cs), "t": ((matiz + 60) % 360, ct),
               "n": (matiz, cn), "v": (matiz, cv), "e": (25.0, 84.0)}
    out = {"dark": bool(escuro)}
    for papel, pal, te, tc in PAPEIS:
        m, cr = paletas[pal]
        out[papel] = hexa(tom(m, cr, te if escuro else tc))
    return out


_cache = {}


def rgb(nome, esq=None):
    """(r, g, b) 0..1 de um papel do esquema atual (lido uma vez por processo; recarregar() relê)."""
    if esq is None:
        if "esq" not in _cache:
            _cache["esq"] = esquema()
        esq = _cache["esq"]
    return de_hexa(esq[nome])


def recarregar():
    _cache.clear()


# ---------- CSS ----------

def css_shell(esq=None):
    """CSS das peças do shell no Solid: as cores com nome (@solid_*) e as formas comuns."""
    e = esq or esquema()
    r = raio()
    cores = "\n".join(f"@define-color solid_{k} #{v};" for k, v in e.items() if k != "dark")
    return cores + f"""
.solid-surface {{ background-color: @solid_surface_container; color: @solid_on_surface; border-radius: {r}px;
  border: none; box-shadow: none; }}
.solid-card {{ background-color: @solid_surface_container_high; color: @solid_on_surface; border-radius: {max(12, r - 4)}px;
  border: none; box-shadow: none; }}
.solid-pill {{ border-radius: 999px; border: none; box-shadow: none; min-height: 40px; padding: 0 18px;
  background-color: @solid_surface_container_highest; color: @solid_on_surface; font-weight: 500; }}
.solid-pill:hover {{ background-image: linear-gradient(alpha(@solid_on_surface, 0.08), alpha(@solid_on_surface, 0.08)); }}
.solid-pill:active {{ background-image: linear-gradient(alpha(@solid_on_surface, 0.12), alpha(@solid_on_surface, 0.12)); }}
.solid-pill.ligado, .solid-pill:checked {{ background-color: @solid_primary; color: @solid_on_primary; }}
.solid-titulo {{ font-size: 22px; font-weight: 400; color: @solid_on_surface; }}
.solid-texto {{ font-size: 14px; color: @solid_on_surface; }}
.solid-fraco {{ font-size: 12px; color: @solid_on_surface_variant; }}
"""


_provedor = [None]


def aplicar_css(prioridade_extra=2):
    if _provedor[0] is not None:
        return
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gdk, Gtk
    p = Gtk.CssProvider()
    p.load_from_string(css_shell())
    Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), p,
                                              Gtk.STYLE_PROVIDER_PRIORITY_USER + prioridade_extra)
    _provedor[0] = p


ARQ_CORES = os.path.join(H, ".cache/hypr-tema/cores")      # o hypr-tema reescreve a cada troca de cor/tema


def vigiar(ao_mudar=None):
    """Vigia a troca de cores (inotify, zero custo parado). Ao mudar: relê a paleta, recarrega o CSS do
    shell (se aplicar_css() foi usado) e chama ao_mudar(). Guarde o retorno (o monitor) num atributo."""
    from gi.repository import Gio, GLib
    mon = Gio.File.new_for_path(ARQ_CORES).monitor_file(Gio.FileMonitorFlags.NONE, None)
    pendente = [None]

    def ir():
        pendente[0] = None
        recarregar()
        if _provedor[0] is not None:
            _provedor[0].load_from_string(css_shell())
        if ao_mudar:
            ao_mudar()
        return False

    def mudou(*_):
        if pendente[0] is None:
            pendente[0] = GLib.timeout_add(150, ir)
    mon.connect("changed", mudou)
    return mon


# ---------- movimento ----------

def suave(a, b, x):
    if x <= a:
        return 0.0
    if x >= b:
        return 1.0
    t = (x - a) / (b - a)
    return t * t * (3 - 2 * t)


def enfatizado(t):
    """O "emphasized decelerate" do Material 3 (cubic-bezier .05 .7 .1 1): sai rápido, assenta macio."""
    t = max(0.0, min(1.0, t))
    lo, hi = 0.0, 1.0
    for _ in range(22):                       # acha u com x(u) = t na bezier (0,0)(.05,.7)(.1,1)(1,1)
        u = (lo + hi) / 2
        x = 3 * (1 - u) ** 2 * u * 0.05 + 3 * (1 - u) * u * u * 0.1 + u ** 3
        lo, hi = (u, hi) if x < t else (lo, u)
    u = (lo + hi) / 2
    return 3 * (1 - u) ** 2 * u * 0.7 + 3 * (1 - u) * u * u + u ** 3


def metaball(overlay, dur, quadro, cor, fim=None, k=22.0, abaixo_de=None):
    """Uma animação de metaball CHAPADO por cima do overlay (Gtk.Overlay), só enquanto anima.
    quadro(t) -> (gotas [(x, y, r)], caixas [(cx, cy, meia_l, meia_a, canto)]) em pixels lógicos do overlay,
    t de 0 a 1. Quando t chega a 1 a GLArea sai do overlay (sem GL parado) e fim() é chamado.
    Sem OpenGL: nada é desenhado e fim() vem no tempo certo do mesmo jeito. Devolve um objeto com .parar().
    abaixo_de: um overlay que deve ficar POR CIMA do metaball (o texto, por exemplo)."""
    from gi.repository import GLib
    from liquido import CampoSolido
    campo = CampoSolido(cor=cor, k=k)
    if abaixo_de is not None and abaixo_de.get_parent() is overlay:
        campo.insert_before(overlay, abaixo_de)
    else:
        overlay.add_overlay(campo)
    estado = {"t0": None, "vivo": True}

    def acabar():
        if estado["vivo"]:
            estado["vivo"] = False
            if campo.get_parent() is overlay:
                overlay.remove_overlay(campo)
            if fim:
                fim()

    def tique(_w, relogio):
        if not estado["vivo"]:
            return GLib.SOURCE_REMOVE
        agora = relogio.get_frame_time() / 1e6
        if estado["t0"] is None:
            estado["t0"] = agora
        t = min(1.0, (agora - estado["t0"]) / max(0.01, dur))
        gotas, caixas = quadro(t)
        campo.formas(list(gotas), 1.0, list(caixas))
        if t >= 1.0:
            GLib.idle_add(lambda: acabar() and False)
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
    campo.add_tick_callback(tique)

    class _Anim:
        """parar(): interrompe (a GLArea sai na hora); com chamar_fim=True, chama o fim() também."""
        def parar(self_, chamar_fim=False):
            if chamar_fim:
                return acabar()
            if estado["vivo"]:
                estado["vivo"] = False
                if campo.get_parent() is overlay:
                    overlay.remove_overlay(campo)

        @property
        def vivo(self_):
            return estado["vivo"]
    return _Anim()


def css_gtk(escuro):
    """Os apps GTK4/libadwaita no Solid: superfícies tonais chapadas (sem vidro), destaque = primária,
    cartões e linhas sem contorno, botões em pílula, cantos no raio do sistema."""
    e = esquema(escuro)
    r = raio()
    return f"""/* Gerado pelo hypr-tema + solido.py (visual Solid): paleta tonal a partir da cor do wallpaper. */
:root {{
    --window-bg-color: #{e['surface']}; --window-fg-color: #{e['on_surface']};
    --view-bg-color: #{e['surface']}; --view-fg-color: #{e['on_surface']};
    --headerbar-bg-color: #{e['surface']}; --headerbar-fg-color: #{e['on_surface']};
    --headerbar-backdrop-color: #{e['surface']}; --headerbar-shade-color: transparent;
    --headerbar-border-color: transparent;
    --sidebar-bg-color: #{e['surface_container_low']}; --sidebar-fg-color: #{e['on_surface']};
    --sidebar-backdrop-color: #{e['surface_container_low']}; --sidebar-shade-color: transparent;
    --sidebar-border-color: transparent;
    --secondary-sidebar-bg-color: #{e['surface_container_low']};
    --card-bg-color: #{e['surface_container']}; --card-fg-color: #{e['on_surface']}; --card-shade-color: transparent;
    --dialog-bg-color: #{e['surface_container_high']}; --dialog-fg-color: #{e['on_surface']};
    --popover-bg-color: #{e['surface_container']}; --popover-fg-color: #{e['on_surface']};
    --popover-shade-color: transparent; --thumbnail-bg-color: #{e['surface_container_high']};
    --accent-bg-color: #{e['primary']}; --accent-color: #{e['primary']}; --accent-fg-color: #{e['on_primary']};
    --destructive-bg-color: #{e['error']}; --destructive-fg-color: #{e['on_error']};
    --destructive-color: #{e['error']};
    --border-color: transparent; --shade-color: transparent; --scrollbar-outline-color: transparent;
    --window-radius: {r}px;
}}
floating-sheet > sheet, bottom-sheet > sheet {{ background-color: var(--dialog-bg-color); }}
.boxed-list, .card, list.boxed-list, .boxed-list-separate > row {{ box-shadow: none; border: none;
    border-radius: {max(12, r - 6)}px; }}
.boxed-list > row, list.boxed-list > row {{ border-bottom: 2px solid var(--window-bg-color); }}
.boxed-list > row:last-child {{ border-bottom: none; }}
button {{ border-radius: 999px; }}
button.flat, button.image-button, button.circular {{ border-radius: 999px; }}
button.suggested-action {{ background-color: var(--accent-bg-color); color: var(--accent-fg-color); }}
entry, spinbutton, dropdown > button, combobox > box > button {{ border-radius: 999px; }}
entry:focus-within, spinbutton:focus-within {{ box-shadow: 0 0 0 2px var(--accent-color); }}
switch {{ border-radius: 999px; }}
switch:checked > slider {{ background-color: var(--accent-fg-color); }}
scale > trough {{ min-height: 6px; border-radius: 999px; background-color: #{e['surface_container_highest']}; }}
scale > trough > highlight {{ border-radius: 999px; background-color: var(--accent-bg-color); }}
.navigation-sidebar > row {{ border-radius: 999px; min-height: 44px; }}
.navigation-sidebar > row:selected {{ background-color: #{e['secondary_container']}; color: #{e['on_secondary_container']}; }}
popover > contents {{ border-radius: {max(12, r - 6)}px; box-shadow: none; }}
headerbar {{ box-shadow: none; }}
"""


def vars_shell(escuro):
    e = esquema(escuro)
    return "\n".join(f"SOLID_{k.upper()}={v}" for k, v in e.items() if k != "dark")


# ---------- o wallpaper padrão ----------

def wallpaper(destino, w=2560, h=1600):
    """Formas grandes, chapadas e macias (nada de foto: nada pessoal aparece). Tons de um azul-petróleo."""
    import cairo
    s = cairo.ImageSurface(cairo.FORMAT_RGB24, w, h)
    cr = cairo.Context(s)
    base = 205.0
    cr.set_source_rgb(*tom(base, 30, 22))
    cr.paint()
    formas = [(0.82, 0.18, 0.55, 40, 34), (0.12, 0.92, 0.62, 30, 46), (0.62, 1.05, 0.42, 48, 30),
              (0.05, 0.12, 0.30, 52, 28), (0.95, 0.75, 0.30, 70, 40), (0.45, 0.42, 0.16, 85, 24)]
    for fx, fy, fr, t, c in formas:
        cr.set_source_rgb(*tom(base + (t - 50) * 0.4, c, t))
        cr.arc(fx * w, fy * h, fr * h, 0, 2 * math.pi)
        cr.fill()
    s.write_to_png(destino)


if __name__ == "__main__":
    acao = sys.argv[1] if len(sys.argv) > 1 else ""
    modo = (sys.argv[2] if len(sys.argv) > 2 else "") or ("escuro" if escuro_agora() else "claro")
    if acao == "vars":
        print(vars_shell(modo != "claro"))
    elif acao == "gtk":
        print(css_gtk(modo != "claro"))
    elif acao == "json":
        print(json.dumps(esquema(modo != "claro"), indent=2))
    elif acao == "wallpaper" and len(sys.argv) > 2:
        wallpaper(sys.argv[2])
    else:
        print(__doc__)
        sys.exit(2)
