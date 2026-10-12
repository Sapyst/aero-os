#!/usr/bin/env python3
"""
icones.py — ícones do Aero: o iconizador ASCII e o gerenciador de ícones/packs (Settings → Icons).

Como funciona: um tema de ícones PRÓPRIO, ~/.local/share/icons/Aero, que herda do pack escolhido
(ICONES_BASE no painel.conf; padrão Adwaita) e é o tema ativo do sistema. Dentro dele ficam só:
  - os ícones ASCII (quando ICONES_ASCII=1), com o mesmo nome do ícone original;
  - os ícones personalizados por app (imagem sua ou ASCII só daquele app).
Nada do sistema é alterado: desligar = apagar os ASCII do tema Aero; o pack por baixo volta a aparecer.

Marcador (aero-manifesto.json): para cada ícone, o tipo, o arquivo de origem, a data dele e a versão do
gerador. No login (--auto) só se gera o que é novo ou mudou: não refaz o que já foi feito.

Comando (~/.local/bin/aero-icones):
  --auto                gera o que falta, se o iconizador estiver ligado (roda no login)
  --tudo                refaz todos os ASCII          --restaurar   apaga os ASCII (desliga)
  --ligar / --desligar  liga/desliga o iconizador (e gera/apaga)
  --base TEMA           muda o pack de ícones por baixo     --packs       lista os packs instalados
  --instalar CAMINHO    instala um pack (pasta, .tar.*, .zip) em ~/.local/share/icons
  --app ID --imagem ARQ | --app ID --ascii | --app ID --original     ícone de um app só
  --status              resumo em JSON
  --system on|off       on: the pack also replaces the system's black-and-white icons (default off)
  --reload              reload icons everywhere (theme + cache + dock, Alt+Tab, Super Menu, tray)
  --tint MODE           ASCII icon colours: classic | system | general | workspaces (ICONS_TINT)
  --retint              redraw the ASCII icons with the current colours (fast: uses the saved grids)
  --watch               daemon: per-workspace icon theme, and redraws when theme/wallpaper changes

Colours (ICONS_TINT): classic = white on near-black (the original look); system = the theme's dark/light
colours; general = taken from the system wallpaper; workspaces = like general, plus a theme per workspace
that has its own wallpaper (Aero-ws2, ...: only the ASCII PNGs, inheriting Aero), switched on the fly.
"""
import colorsys
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
import cairo  # noqa: E402
from gi.repository import GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

VERSAO = 2                      # sobe quando o visual do gerador muda (força refazer)
TEMA = "Aero"
PASTA_ICONES = os.path.expanduser("~/.local/share/icons")
RAIZ = os.path.join(PASTA_ICONES, TEMA)
MANIFESTO = os.path.join(RAIZ, "aero-manifesto.json")
PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")
TAMANHOS = (512, 256, 128, 64, 48, 32)
RAMP = " .:-=+*#%@"
GRADES = os.path.join(RAIZ, "aero-grades.json")        # the ASCII grid of each icon: recolour without redoing
WIDGETS_JSON = os.path.expanduser("~/.config/aero-shell/widgets.json")   # por_area: per-workspace wallpaper
TINT_MODES = ("classic", "system", "general", "workspaces")
CLASSIC = {"plate": (0.08, 0.08, 0.09), "ink": (1.0, 1.0, 1.0)}
paleta = dict(CLASSIC)                                  # the colours desenhar() uses right now
COLUNAS = 36


# ---------------- painel.conf (só leitura/escrita de CHAVE=valor) ----------------
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


def gravar_painel(**kv):
    try:
        linhas = open(PAINEL).read().splitlines()
    except OSError:
        linhas = []
    falta = {k: str(v).replace("\n", "").replace("\r", "") for k, v in kv.items()}
    for i, l in enumerate(linhas):
        k = l.split("=", 1)[0].strip()
        if k in falta and not l.startswith("#"):
            linhas[i] = f"{k}={falta.pop(k)}"
    linhas += [f"{k}={v}" for k, v in falta.items()]
    with open(PAINEL + ".tmp", "w") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(PAINEL + ".tmp", PAINEL)


def ligado():
    return ler_painel().get("ICONES_ASCII", "0") == "1"


def base():
    return ler_painel().get("ICONES_BASE", "") or "Adwaita"


# ---------------- tema Aero ----------------
def manifesto():
    try:
        return json.load(open(MANIFESTO))
    except (OSError, ValueError):
        return {}


def gravar_manifesto(m):
    os.makedirs(RAIZ, exist_ok=True)
    with open(MANIFESTO + ".tmp", "w") as f:
        json.dump(m, f, indent=1, sort_keys=True)
    os.replace(MANIFESTO + ".tmp", MANIFESTO)


ADWAITA = "/usr/share/icons/Adwaita"


def system_sync():
    """ICONS_SYSTEM=1: the pack/ASCII also changes the system's own (symbolic) icons. Default: no."""
    return ler_painel().get("ICONS_SYSTEM", "0") == "1"


def adwaita_symbolic():
    """Adwaita's black-and-white "symbolic" folders and their index.theme blocks."""
    try:
        txt = open(os.path.join(ADWAITA, "index.theme")).read()
    except OSError:
        return [], []
    blocks, name, lines = [], None, []
    for line in txt.splitlines() + ["["]:
        if line.startswith("["):
            if name and name.startswith("symbolic/"):
                blocks.append((name, lines))
            name, lines = line.strip("[]"), []
        elif name:
            lines.append(line)
    return [n for n, _ in blocks], [f"[{n}]\n" + "\n".join(l).strip() + "\n" for n, l in blocks]


def escrever_tema():
    """index.theme do Aero: herda do pack escolhido e, por fim, do hicolor.
    Unless ICONS_SYSTEM=1, Adwaita's symbolic icons (menus, settings, panels) come FIRST, through a link
    inside the Aero theme: the system keeps its black-and-white icons whatever the pack; only app and
    desktop icons follow the pack / ASCII."""
    os.makedirs(RAIZ, exist_ok=True)
    pastas = [f"{t}x{t}/apps" for t in TAMANHOS]
    herda = ",".join(dict.fromkeys([base(), "Adwaita", "hicolor"]))
    link = os.path.join(RAIZ, "symbolic")
    sym_dirs, sym_blocks = ([], []) if system_sync() else adwaita_symbolic()
    if sym_dirs:
        if not os.path.islink(link):
            os.symlink(os.path.join(ADWAITA, "symbolic"), link)
    elif os.path.islink(link):
        os.remove(link)
    txt = [f"[Icon Theme]", f"Name={TEMA}", "Comment=Arch Aero OS: your pack + ASCII and custom app icons",
           f"Inherits={herda}", f"Directories={','.join(pastas + sym_dirs)}", ""]
    for t in TAMANHOS:
        txt += [f"[{t}x{t}/apps]", f"Size={t}", "Context=Applications", "Type=Fixed", ""]
    txt += sym_blocks
    with open(os.path.join(RAIZ, "index.theme"), "w") as f:
        f.write("\n".join(txt))


def ativar_tema():
    """Põe o Aero como tema de ícones (GTK4 pelo gsettings, GTK3 pelo settings.ini)."""
    escrever_tema()
    subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", RAIZ], capture_output=True)
    subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "icon-theme", tema_da_area()], capture_output=True)
    for v in ("gtk-3.0", "gtk-4.0"):
        p = os.path.expanduser(f"~/.config/{v}/settings.ini")
        try:
            linhas = open(p).read().splitlines()
        except OSError:
            linhas = ["[Settings]"]
        linhas = [l for l in linhas if not l.startswith("gtk-icon-theme-name")]
        i = next((n for n, l in enumerate(linhas) if l.strip() == "[Settings]"), None)
        if i is None:
            linhas.insert(0, "[Settings]"); i = 0
        linhas.insert(i + 1, f"gtk-icon-theme-name={TEMA}")
        with open(p, "w") as f:
            f.write("\n".join(linhas) + "\n")


def recarregar_icones():
    """Faz os apps abertos relerem os ícones: troca o tema por um instante e volta."""
    subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", RAIZ], capture_output=True)
    subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "icon-theme", base()], capture_output=True)
    subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "icon-theme", tema_da_area()], capture_output=True)


# The shell pieces that keep the icons they already drew: they must start again to show new ones.
# (script, command that starts it again; None = its watcher restarts it by itself)
SHELL_WITH_ICONS = [
    ("dock.py", None),
    ("alttab.py", "~/.local/bin/alttab --daemon"),
    ("supermenu.py", "~/.local/bin/supermenu --daemon"),
    ("bandeja.py", "~/.local/bin/bandeja"),
]


def restart_shell():
    """Closes and reopens the shell pieces that hold icons (dock, Alt+Tab, Super Menu, tray)."""
    import signal
    import time
    me = os.getpid()
    for script, again in SHELL_WITH_ICONS:
        for pid in os.listdir("/proc"):
            if not pid.isdigit() or int(pid) == me:
                continue
            try:
                cmd = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if len(cmd) > 1 and cmd[0].endswith(b"python3") and cmd[1].endswith(b"aero-shell/" + script.encode()):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        if again:
            time.sleep(0.4)
            subprocess.run(["hyprctl", "dispatch", "exec", os.path.expanduser(again)], capture_output=True)


def reload_all():
    """The "Reload icons" button: theme and cache written again, apps told, shell restarted."""
    ativar_tema()
    recarregar_icones()
    restart_shell()


# ---------------- apps e onde está o ícone original ----------------
def apps_visiveis():
    """Apps que aparecem no menu/dock (os ícones "da frente"): [(id, nome, ícone)]"""
    saida = []
    for a in Gio.AppInfo.get_all():
        if not a.should_show() or not a.get_icon():
            continue
        saida.append((a.get_id(), a.get_display_name(), a.get_icon().to_string()))
    return sorted(saida, key=lambda x: x[1].casefold())


def nome_no_tema(icone, app_id):
    """Ícone por nome usa o próprio nome; ícone por caminho ganha um nome nosso (aero-app-...)."""
    if icone.startswith("/"):
        return "aero-app-" + app_id.removesuffix(".desktop").replace("/", "-")
    return icone


def origem(icone):
    """Arquivo do ícone ORIGINAL (no pack escolhido, nunca no tema Aero). None se não achar."""
    if icone.startswith("/"):
        return icone if os.path.isfile(icone) else None
    t = Gtk.IconTheme()
    t.set_theme_name(base())
    pagina = t.lookup_icon(icone, None, 512, 1, Gtk.TextDirection.NONE, Gtk.IconLookupFlags.FORCE_REGULAR)
    f = pagina.get_file() if pagina else None
    caminho = f.get_path() if f else None
    if not caminho or caminho.startswith(RAIZ) or "image-missing" in caminho or not os.path.isfile(caminho):
        return None
    return caminho


def carregar_1600(caminho):
    """Abre o ícone de origem já em 1600x1600 (SVG pelo rsvg, o resto pelo GdkPixbuf)."""
    if caminho.endswith((".svg", ".svgz")):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            png = tmp.name
        try:
            subprocess.run(["rsvg-convert", "-w", "1600", "-h", "1600", "-a", caminho, "-o", png],
                           check=True, capture_output=True, timeout=30)
            pb = GdkPixbuf.Pixbuf.new_from_file(png)
        finally:
            os.unlink(png)
    else:
        pb = GdkPixbuf.Pixbuf.new_from_file(caminho)
    if not pb.get_has_alpha():
        pb = pb.add_alpha(False, 0, 0, 0)
    lado = max(pb.get_width(), pb.get_height())
    quadro = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, lado, lado)
    quadro.fill(0)
    pb.copy_area(0, 0, pb.get_width(), pb.get_height(), quadro,
                 (lado - pb.get_width()) // 2, (lado - pb.get_height()) // 2)
    return quadro.scale_simple(1600, 1600, GdkPixbuf.InterpType.HYPER)


# ---------------- o filtro ASCII ----------------
def grade_ascii(pb, cols=COLUNAS):
    W, H, n, st, px = pb.get_width(), pb.get_height(), pb.get_n_channels(), pb.get_rowstride(), pb.get_pixels()
    cw = W / cols
    ch = cw * 2.0
    rows = int(H / ch)
    grade = []
    for r in range(rows):
        linha = []
        for c in range(cols):
            sl = sa = 0.0
            cnt = 0
            for y in range(int(r * ch), min(H, int((r + 1) * ch)), 3):
                for x in range(int(c * cw), min(W, int((c + 1) * cw)), 3):
                    i = y * st + x * n
                    a = px[i + 3] / 255
                    sl += (0.2126 * px[i] + 0.7152 * px[i + 1] + 0.0722 * px[i + 2]) / 255 * a
                    sa += a
                    cnt += 1
            cob = sa / cnt if cnt else 0
            linha.append(("?", sl / sa) if cob >= 0.3 else (" ", 0.0))
        grade.append(linha)
    lums = sorted(v for l in grade for c, v in l if c == "?")
    lo, hi = (lums[int(len(lums) * 0.05)], lums[int(len(lums) * 0.95)]) if lums else (0, 1)
    for l in grade:
        for i, (c, lum) in enumerate(l):
            if c == "?":
                v = min(1.0, max(0.0, (lum - lo) / max(0.08, hi - lo)))
                v = 0.22 + 0.78 * (v * v * (3 - 2 * v))
                l[i] = (RAMP[max(1, round(v * (len(RAMP) - 1)))], v)
    return grade, cols, rows


def desenhar(grade, cols, rows, tam):
    s = cairo.ImageSurface(cairo.FORMAT_ARGB32, tam, tam)
    cr = cairo.Context(s)
    m, r = tam * 0.04, tam * 0.22                        # placa escura arredondada + contorno de 1px
    cr.new_sub_path()
    for cx, cy, a0 in ((tam - m - r, m + r, -90), (tam - m - r, tam - m - r, 0), (m + r, tam - m - r, 90), (m + r, m + r, 180)):
        cr.arc(cx, cy, r, math.radians(a0), math.radians(a0 + 90))
    cr.close_path()
    ink = paleta["ink"]
    claro = luminancia(paleta["plate"]) > 0.4             # light plate: dark characters, softer glow
    cr.set_source_rgb(*paleta["plate"])
    cr.fill_preserve()
    cr.set_source_rgba(*ink, 0.22 if claro else 0.16)
    cr.set_line_width(max(1, tam / 256))
    cr.stroke()
    area = tam * 0.74
    camada = cairo.ImageSurface(cairo.FORMAT_ARGB32, tam, tam)
    cc = cairo.Context(camada)
    lay = PangoCairo.create_layout(cc)
    fd = Pango.FontDescription("JetBrains Mono Bold")
    fd.set_absolute_size(100 * Pango.SCALE)
    lay.set_font_description(fd)
    lay.set_text("M" * 10, -1)
    adv100 = lay.get_pixel_size()[0] / 10
    adv = area / cols
    lh = adv * 2.0
    fd.set_absolute_size(100 * adv / adv100 * Pango.SCALE)
    lay.set_font_description(fd)
    x0, y0 = (tam - cols * adv) / 2, (tam - rows * lh) / 2 - lh * 0.15
    for ri, l in enumerate(grade):
        for ci, (car, v) in enumerate(l):
            if car != " ":
                lay.set_text(car, -1)
                cc.set_source_rgba(*ink, (0.8 + 0.2 * v) if claro else (0.75 + 0.25 * v))
                cc.move_to(x0 + ci * adv, y0 + ri * lh)
                PangoCairo.show_layout(cc, lay)
    f = max(2, tam // 48)                                  # brilho (glow) de fósforo
    peq = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, tam // f), max(1, tam // f))
    cp = cairo.Context(peq)
    cp.scale(1 / f, 1 / f)
    cp.set_source_surface(camada, 0, 0)
    cp.paint()
    cr.save()
    cr.scale(f, f)
    cr.set_source_surface(peq, 0, 0)
    cr.get_source().set_filter(cairo.FILTER_BILINEAR)
    cr.paint_with_alpha(0.25 if claro else 0.55)
    cr.restore()
    cr.set_source_surface(camada, 0, 0)
    cr.paint()
    return s


def instalar_pngs(nome, superficies, raiz=RAIZ):
    for tam, s in superficies.items():
        pasta = os.path.join(raiz, f"{tam}x{tam}", "apps")
        os.makedirs(pasta, exist_ok=True)
        s.write_to_png(os.path.join(pasta, nome + ".png"))


def apagar_pngs(nome):
    for t in TAMANHOS:
        try:
            os.unlink(os.path.join(RAIZ, f"{t}x{t}", "apps", nome + ".png"))
        except FileNotFoundError:
            pass


def gerar_ascii(nome, fonte):
    grade, cols, rows = grade_ascii(carregar_1600(fonte))
    g = grades()
    g[nome] = grade
    gravar_grades(g)
    instalar_pngs(nome, {t: desenhar(grade, cols, rows, t) for t in TAMANHOS})


def redesenhar(nome, grade, raiz=RAIZ):
    """Draws an icon again from its saved grid, with the current palette (no source reading)."""
    instalar_pngs(nome, {t: desenhar(grade, len(grade[0]), len(grade), t) for t in TAMANHOS}, raiz)


def grades():
    try:
        return json.load(open(GRADES))
    except (OSError, ValueError):
        return {}


def gravar_grades(g):
    with open(GRADES + ".tmp", "w") as f:
        json.dump(g, f, separators=(",", ":"))
    os.replace(GRADES + ".tmp", GRADES)


# ---------------- colours (ICONS_TINT) ----------------
def luminancia(c):
    f = lambda x: x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2])


def contraste(a, b):
    la, lb = sorted((luminancia(a), luminancia(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def chave(p):
    return "".join(f"{round(x * 255):02x}" for x in p["plate"] + p["ink"])


def tint_mode():
    v = ler_painel().get("ICONS_TINT", "classic")
    return v if v in TINT_MODES else "classic"


def sistema_escuro():
    r = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"], capture_output=True, text=True)
    return "light" not in r.stdout


def paleta_sistema():
    p = ler_painel()
    hexa = lambda h, d: tuple(int((h or d)[i:i + 2], 16) / 255 for i in (0, 2, 4))
    escura, clara = hexa(p.get("COR_ESCURA"), "1e1e1e"), hexa(p.get("COR_CLARA"), "f6f6f6")
    return {"plate": escura, "ink": clara} if sistema_escuro() else {"plate": clara, "ink": escura}


def paleta_wallpaper(caminho):
    """The icon colours MIRROR the wallpaper: light picture = pale plate in its colour + dark ink of the
    same hue; dark picture = deep plate + light tinted ink. A grey picture gives grey icons."""
    try:
        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(caminho, 64, 64, True)
    except (GLib.Error, TypeError):
        return dict(CLASSIC)
    n, st, px = pb.get_n_channels(), pb.get_rowstride(), pb.get_pixels()
    soma, cor, peso = [0.0] * 3, [0.0] * 3, 0.0
    for y in range(pb.get_height()):
        for x in range(pb.get_width()):
            i = y * st + x * n
            rgb = [px[i + k] / 255 for k in range(3)]
            _, sat, val = colorsys.rgb_to_hsv(*rgb)
            w = (sat * val) ** 2                           # vivid pixels decide the hue
            for k in range(3):
                soma[k] += rgb[k]; cor[k] += rgb[k] * w
            peso += w
    total = pb.get_width() * pb.get_height()
    media = [v / total for v in soma]
    viva = [v / peso for v in cor] if peso > total * 0.01 else media
    h, sat, _ = colorsys.rgb_to_hsv(*viva)
    sat = sat if peso > total * 0.01 else 0.0
    if luminancia(media) > 0.3:
        plate, ink = colorsys.hsv_to_rgb(h, min(sat, 0.5) * 0.55, 0.94), colorsys.hsv_to_rgb(h, min(1, sat * 0.9 + 0.15), 0.25)
    else:
        plate, ink = colorsys.hsv_to_rgb(h, min(sat, 0.7) * 0.75, 0.14), colorsys.hsv_to_rgb(h, min(sat, 0.6) * 0.4, 0.97)
    # always readable: push the ink towards black/white until the contrast is high (7:1)
    alvo = (0, 0, 0) if luminancia(plate) > 0.4 else (1, 1, 1)
    for _ in range(10):
        if contraste(plate, ink) >= 7:
            break
        ink = tuple(a + (b - a) * 0.25 for a, b in zip(ink, alvo))
    return {"plate": tuple(plate), "ink": tuple(ink)}


def wallpaper_sistema():
    return ler_painel().get("WALL_ORIGINAL", "")


def paleta_base():
    """The colours of the main Aero theme (every workspace without its own wallpaper)."""
    m = tint_mode()
    if m == "system":
        return paleta_sistema()
    if m in ("general", "workspaces"):
        return paleta_wallpaper(wallpaper_sistema())
    return dict(CLASSIC)


def walls_por_area():
    try:
        d = json.load(open(WIDGETS_JSON)).get("por_area") or {}
    except (OSError, ValueError):
        return {}
    return {int(a): v["wallpaper"] for a, v in d.items() if a.isdigit() and (v or {}).get("wallpaper")}


def por_area():
    try:
        return json.load(open(WIDGETS_JSON)).get("por_area") or {}
    except (OSError, ValueError):
        return {}


def paletas_das_areas():
    """{area: palette} for the workspaces whose icons differ from the main Aero theme.
    Per workspace (Settings → Workspaces → N → Icon colors): "global" (default) follows the main mode
    (in "workspaces" mode = its own wallpaper, if it has one); "wallpaper" = always its wallpaper;
    "classic" = white on black there."""
    modo, base_k = tint_mode(), chave(paleta_base())
    saida = {}
    for a, reg in por_area().items():
        if not a.isdigit():
            continue
        reg = reg or {}
        escolha, wall = reg.get("icons", "global"), reg.get("wallpaper")
        if escolha == "classic":
            pal = dict(CLASSIC)
        elif escolha == "wallpaper":
            pal = paleta_wallpaper(wall or wallpaper_sistema())
        elif modo == "workspaces" and wall:
            pal = paleta_wallpaper(wall)
        else:
            continue
        if chave(pal) != base_k:
            saida[int(a)] = pal
    return saida


def retint():
    """Main Aero theme: redraws the ASCII icons whose colours are not the current ones (from saved grids)."""
    global paleta
    paleta = paleta_base()
    k = chave(paleta)
    m, g = manifesto(), grades()
    feitos = 0
    for nome, reg in m.items():
        ascii_ = reg.get("tipo") == "ascii" or reg.get("ascii")
        if ascii_ and reg.get("paleta") != k:
            if nome in g:
                redesenhar(nome, g[nome])
            else:                                       # made before the grids were saved: once from source
                fonte = reg.get("original") or reg.get("fonte")
                if not fonte or not os.path.isfile(fonte):
                    continue
                try:
                    gerar_ascii(nome, fonte)
                except Exception as erro:  # noqa: BLE001
                    print(f"aero-icones: {nome}: {erro}", file=sys.stderr)
                    continue
            reg["paleta"] = k
            feitos += 1
    if feitos:
        gravar_manifesto(m)
    return feitos


def variantes():
    """Workspaces mode: one small theme per workspace with its own wallpaper (only the ASCII PNGs,
    inheriting Aero). Redrawn only when that wallpaper's colours change. Others are deleted."""
    global paleta
    querer = paletas_das_areas()
    for d in os.listdir(PASTA_ICONES):
        if d.startswith(TEMA + "-ws") and d[len(TEMA) + 3:].isdigit() and int(d[len(TEMA) + 3:]) not in querer:
            shutil.rmtree(os.path.join(PASTA_ICONES, d), ignore_errors=True)
    m, g = manifesto(), grades()
    nomes = sorted(n for n, r in m.items() if (r.get("tipo") == "ascii" or r.get("ascii")) and n in g)
    for area, pal in querer.items():
        paleta = pal
        raiz = os.path.join(PASTA_ICONES, f"{TEMA}-ws{area}")
        marca = os.path.join(raiz, "aero-variante.json")
        assin = {"paleta": chave(paleta), "nomes": nomes}
        try:
            if json.load(open(marca)) == assin:
                continue
        except (OSError, ValueError):
            pass
        shutil.rmtree(raiz, ignore_errors=True)
        for nome in nomes:
            redesenhar(nome, g[nome], raiz)
        txt = ["[Icon Theme]", f"Name={TEMA}-ws{area}", f"Comment=Aero ASCII icons in the colours of workspace {area}",
               f"Inherits={TEMA}", "Directories=" + ",".join(f"{t}x{t}/apps" for t in TAMANHOS), ""]
        for t in TAMANHOS:
            txt += [f"[{t}x{t}/apps]", f"Size={t}", "Context=Applications", "Type=Fixed", ""]
        with open(os.path.join(raiz, "index.theme"), "w") as f:
            f.write("\n".join(txt))
        json.dump(assin, open(marca, "w"))
        subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", raiz], capture_output=True)
    paleta = paleta_base()


def area_ativa():
    try:
        return json.loads(subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True).stdout)["id"]
    except (ValueError, KeyError):
        return 0


def tema_da_area(area=None):
    """The icon theme for this workspace: its own variant if it has one, else Aero."""
    area = area_ativa() if area is None else area
    nome = f"{TEMA}-ws{area}"
    if os.path.isfile(os.path.join(PASTA_ICONES, nome, "index.theme")):
        return nome
    return TEMA


def usar_tema(nome):
    atual = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "icon-theme"],
                           capture_output=True, text=True).stdout.strip().strip("'")
    if atual != nome:
        subprocess.run(["gsettings", "set", "org.gnome.desktop.interface", "icon-theme", nome], capture_output=True)


def vigiar():
    """--watch: switches the icon theme with the workspace and redraws when the colours' source changes
    (theme dark/light, system wallpaper, a workspace's wallpaper). Idle in the classic mode."""
    import socket
    import threading
    laco = GLib.MainLoop()
    estado = {"fonte": None, "timer": 0}

    def fonte():
        m = tint_mode()
        if m == "system":
            return (m, chave(paleta_sistema()))
        if m in ("general", "workspaces"):
            return (m, wallpaper_sistema(), json.dumps(por_area(), sort_keys=True))
        return (m, json.dumps({a: (r or {}).get("icons") for a, r in por_area().items()}, sort_keys=True))

    def conferir():
        estado["timer"] = 0
        f = fonte()
        if f != estado["fonte"] and ligado():
            estado["fonte"] = f
            subprocess.run([sys.executable, os.path.abspath(__file__), "--retint"], capture_output=True)
        usar_tema(tema_da_area())
        return GLib.SOURCE_REMOVE

    def depois(*_):
        if estado["timer"]:
            GLib.source_remove(estado["timer"])
        estado["timer"] = GLib.timeout_add(1500, conferir)

    def ouvir():
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.connect(os.path.join(os.environ["XDG_RUNTIME_DIR"], "hypr", os.environ["HYPRLAND_INSTANCE_SIGNATURE"], ".socket2.sock"))
        except (OSError, KeyError):
            return
        buf = b""
        while (dados := s.recv(4096)):
            buf += dados
            *linhas, buf = buf.split(b"\n")
            for l in linhas:
                ev, _, v = l.decode(errors="replace").partition(">>")
                if ev == "workspacev2" and v.split(",", 1)[0].isdigit():
                    GLib.idle_add(lambda a=int(v.split(",", 1)[0]): (usar_tema(tema_da_area(a)), False)[1])

    GLib.timeout_add_seconds(3, conferir)              # at login: colours may have changed while off
    vig = [Gio.File.new_for_path(p).monitor_file(Gio.FileMonitorFlags.NONE, None) for p in (PAINEL, WIDGETS_JSON)]
    for v in vig:
        v.connect("changed", depois)
    cfg = Gio.Settings.new("org.gnome.desktop.interface")
    cfg.connect("changed::color-scheme", depois)
    threading.Thread(target=ouvir, daemon=True).start()
    laco.run()


def gerar_imagem(nome, arquivo):
    """Ícone personalizado (imagem sua): só redimensiona para os tamanhos do tema."""
    pb = carregar_1600(arquivo)
    for t in TAMANHOS:
        pasta = os.path.join(RAIZ, f"{t}x{t}", "apps")
        os.makedirs(pasta, exist_ok=True)
        pb.scale_simple(t, t, GdkPixbuf.InterpType.HYPER).savev(os.path.join(pasta, nome + ".png"), "png", [], [])


def assinatura(fonte):
    try:
        st = os.stat(fonte)
        return f"{fonte}|{int(st.st_mtime)}|{st.st_size}|{base()}"
    except OSError:
        return ""


# ---------------- ações ----------------
def iconizar(forcar=False, progresso=None):
    """Gera os ASCII que faltam (ou todos, com forcar). Pula os já feitos e os personalizados."""
    global paleta
    paleta = paleta_base()
    m = manifesto()
    apps = apps_visiveis()
    feitos = novos = falhas = 0
    for i, (app_id, nome_app, icone) in enumerate(apps):
        nome = nome_no_tema(icone, app_id)
        reg = m.get(nome, {})
        if reg.get("tipo") == "custom":
            continue
        # app com ícone por caminho que nós já sobrepusemos: a origem é o caminho guardado no marcador
        fonte = reg.get("original") if icone.startswith("aero-app-") else origem(icone)
        if not fonte:
            falhas += 1
            continue
        ass = assinatura(fonte)
        if not forcar and reg.get("tipo") == "ascii" and reg.get("assinatura") == ass and reg.get("versao") == VERSAO:
            feitos += 1
            continue
        try:
            gerar_ascii(nome, fonte)
        except Exception as erro:  # noqa: BLE001 — um ícone ruim não para os outros
            print(f"aero-icones: {nome_app}: {erro}", file=sys.stderr)
            falhas += 1
            continue
        m[nome] = {"tipo": "ascii", "assinatura": ass, "versao": VERSAO, "app": app_id, "original": fonte,
                   "paleta": chave(paleta)}
        novos += 1
        if icone.startswith("/"):
            sobrepor_lancador(app_id, nome)
        gravar_manifesto(m)
        if progresso:
            progresso(i + 1, len(apps), nome_app)
    return {"novos": novos, "ja_feitos": feitos, "falhas": falhas, "total": len(apps)}


def sobrepor_lancador(app_id, nome):
    """Só para apps com ícone por CAMINHO: um lançador do usuário com Icon=<nome do tema>."""
    a = Gio.DesktopAppInfo.new(app_id)
    if not a:
        return
    origem_desk = a.get_filename()
    destino = os.path.join(os.path.expanduser("~/.local/share/applications"), app_id)
    if origem_desk != destino:
        shutil.copy2(origem_desk, destino)
        marca = destino + ".aero-copia"
        open(marca, "w").write("criado pelo aero-icones; apagado ao restaurar\n")
    linhas = open(destino).read().splitlines()
    linhas = [f"Icon={nome}" if l.startswith("Icon=") else l for l in linhas]
    open(destino, "w").write("\n".join(linhas) + "\n")


def restaurar_lancadores(nomes_apps):
    pasta = os.path.expanduser("~/.local/share/applications")
    for app_id in nomes_apps:
        d = os.path.join(pasta, app_id)
        if os.path.exists(d + ".aero-copia"):
            os.unlink(d)
            os.unlink(d + ".aero-copia")


def restaurar():
    """Apaga só os ASCII automáticos. Os personalizados ficam."""
    m = manifesto()
    caminho = [k for k, v in m.items() if v.get("tipo") == "ascii"]
    restaurar_lancadores([m[k].get("app", "") for k in caminho if k.startswith("aero-app-")])
    for k in caminho:
        apagar_pngs(k)
        del m[k]
    gravar_manifesto(m)
    return len(caminho)


def app_personalizar(app_id, imagem=None, ascii_=False, original=False):
    a = Gio.DesktopAppInfo.new(app_id)
    if not a or not a.get_icon():
        raise SystemExit(f"app não encontrado: {app_id}")
    icone = a.get_icon().to_string()
    if a.get_filename().startswith(os.path.expanduser("~/.local/share/applications")) and icone.startswith("aero-app-"):
        icone = icone                     # já sobreposto por nós
    nome = nome_no_tema(icone, app_id) if not icone.startswith("aero-app-") else icone
    m = manifesto()
    if original:
        apagar_pngs(nome)
        m.pop(nome, None)
        if nome.startswith("aero-app-"):
            restaurar_lancadores([app_id])
        gravar_manifesto(m)
        if ligado():
            iconizar()                    # volta a ser ASCII se o iconizador estiver ligado
        return
    if imagem:
        gerar_imagem(nome, imagem)
        m[nome] = {"tipo": "custom", "fonte": os.path.abspath(imagem), "app": app_id}
    elif ascii_:
        fonte = origem(icone) if not icone.startswith("aero-app-") else None
        if not fonte:
            raise SystemExit("não achei o ícone original deste app")
        global paleta
        paleta = paleta_base()
        gerar_ascii(nome, fonte)
        m[nome] = {"tipo": "custom", "fonte": fonte, "ascii": True, "app": app_id, "paleta": chave(paleta)}
    if icone.startswith("/"):
        sobrepor_lancador(app_id, nome)
    gravar_manifesto(m)


def packs():
    """Temas de ícones instalados que têm ícones de apps (não cursores)."""
    vistos = {}
    for raiz in (os.path.expanduser("~/.local/share/icons"), os.path.expanduser("~/.icons"), "/usr/share/icons"):
        try:
            nomes = sorted(os.listdir(raiz))
        except OSError:
            continue
        for n in nomes:
            idx = os.path.join(raiz, n, "index.theme")
            if n in vistos or n == TEMA or not os.path.isfile(idx):
                continue
            txt = open(idx, errors="replace").read()
            if "Directories=" not in txt or "cursors" in os.listdir(os.path.join(raiz, n)) and "apps" not in txt:
                continue
            nome = next((l.split("=", 1)[1].strip() for l in txt.splitlines() if l.startswith("Name=")), n)
            vistos[n] = nome
    return vistos


def instalar_pack(caminho):
    """Instala um pack (pasta ou arquivo .tar.*/.zip) em ~/.local/share/icons. Devolve os temas instalados.
    Segurança: nada de caminhos absolutos, '..' ou links saindo da pasta temporária."""
    tmp = tempfile.mkdtemp(prefix="aero-pack-")
    try:
        if os.path.isdir(caminho):
            fonte = caminho
        else:
            if zipfile.is_zipfile(caminho):
                with zipfile.ZipFile(caminho) as z:
                    for info in z.infolist():
                        alvo = os.path.realpath(os.path.join(tmp, info.filename))
                        if not alvo.startswith(tmp + os.sep):
                            raise SystemExit(f"arquivo suspeito no pacote: {info.filename}")
                    z.extractall(tmp)
            elif tarfile.is_tarfile(caminho):
                with tarfile.open(caminho) as t:
                    t.extractall(tmp, filter="data")      # o filtro "data" barra links e caminhos perigosos
            else:
                raise SystemExit("formato não suportado (use pasta, .tar.gz/.tar.xz/.tar.bz2 ou .zip)")
            fonte = tmp
        instalados = []
        for raiz, dirs, arqs in os.walk(fonte):
            if "index.theme" in arqs:
                nome = os.path.basename(raiz.rstrip("/")) or "pack"
                destino = os.path.join(PASTA_ICONES, nome)
                if nome == TEMA:
                    continue
                if os.path.exists(destino):
                    shutil.rmtree(destino)
                shutil.copytree(raiz, destino, symlinks=True, ignore_dangling_symlinks=True)
                subprocess.run(["gtk-update-icon-cache", "-f", "-t", "-q", destino], capture_output=True)
                instalados.append(nome)
                dirs[:] = []                  # não procura temas dentro de um tema
        if not instalados:
            raise SystemExit("nenhum tema de ícones (index.theme) encontrado")
        return instalados
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def status():
    m = manifesto()
    apps = apps_visiveis()
    estado = {}
    for app_id, nome_app, icone in apps:
        reg = m.get(nome_no_tema(icone, app_id)) or m.get(icone) or {}
        estado[app_id] = {"nome": nome_app, "icone": icone,
                          "tipo": "ascii-custom" if reg.get("ascii") else reg.get("tipo", "pack")}
    return {"ligado": ligado(), "base": base(), "tema_ativo": TEMA, "apps": estado, "system": system_sync(), "tint": tint_mode(),
            "ascii": sum(1 for v in m.values() if v.get("tipo") == "ascii"),
            "custom": sum(1 for v in m.values() if v.get("tipo") == "custom")}


def main(args):
    Gtk.init()
    if "--status" in args:
        print(json.dumps(status(), ensure_ascii=False))
        return
    if "--system" in args:
        gravar_painel(ICONS_SYSTEM=1 if args[args.index("--system") + 1] == "on" else 0)
        reload_all()
        return
    if "--watch" in args:
        vigiar()
        return
    if "--tint" in args:
        modo = args[args.index("--tint") + 1]
        if modo not in TINT_MODES:
            raise SystemExit(f"modes: {', '.join(TINT_MODES)}")
        gravar_painel(ICONS_TINT=modo)
        n = retint()
        variantes()
        reload_all()
        print(json.dumps({"tint": modo, "redrawn": n}))
        return
    if "--retint" in args:
        n = retint()
        variantes()
        if n:
            recarregar_icones()
            restart_shell()
        else:
            usar_tema(tema_da_area())
        print(json.dumps({"redrawn": n}))
        return
    if "--reload" in args:
        reload_all()
        print(json.dumps({"reloaded": True}))
        return
    if "--packs" in args:
        print(json.dumps(packs(), ensure_ascii=False))
        return
    if "--base" in args:
        gravar_painel(ICONES_BASE=args[args.index("--base") + 1])
        ativar_tema()
        if ligado():
            print(json.dumps(iconizar()))     # as origens mudaram: o marcador refaz só o que mudou
        recarregar_icones()
        return
    if "--instalar" in args:
        print(json.dumps(instalar_pack(args[args.index("--instalar") + 1])))
        return
    if "--app" in args:
        app_id = args[args.index("--app") + 1]
        ativar_tema()
        if "--imagem" in args:
            app_personalizar(app_id, imagem=args[args.index("--imagem") + 1])
        elif "--ascii" in args:
            app_personalizar(app_id, ascii_=True)
        elif "--original" in args:
            app_personalizar(app_id, original=True)
        recarregar_icones()
        return
    if "--ligar" in args:
        gravar_painel(ICONES_ASCII=1)
    if "--desligar" in args or "--restaurar" in args:
        if "--desligar" in args:
            gravar_painel(ICONES_ASCII=0)
        n = restaurar()
        recarregar_icones()
        print(json.dumps({"restaurados": n}))
        return
    ativar_tema()
    if "--tudo" in args or "--ligar" in args or ("--auto" in args and ligado()):
        prog = (lambda i, t, n: print(f"PROGRESSO {i}/{t} {n}", flush=True)) if "--progresso" in args else None
        r = iconizar(forcar="--tudo" in args, progresso=prog)
        if r["novos"]:
            recarregar_icones()
        print(json.dumps(r))


if __name__ == "__main__":
    main(sys.argv[1:])
