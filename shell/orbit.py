"""
orbit.py — ORBIT, the DotStyle file finder (Super+Shift+F; experimental, not the default file manager).

No window, no list: the search is the centre of the screen and the files orbit around it.
- Typing searches the current folder and everything under it (an index of ~ built in the background
  when ORBIT opens). The best matches are big and close to the centre, the rest smaller and farther out.
  With nothing typed, it shows the current folder, most recent first.
- Bubbles: images show their thumbnail, folders are clusters of dots (one per item inside, up to 24),
  other files show their kind in dots (DOC, </>, ♪, ▶, ZIP...).
- The pointer is the same lens as the start menu (nuvem.py): bubbles grow and push their neighbours,
  and the halftone filler between them is alive. The focused bubble gets satellites: OPEN, SHOW
  (its folder in the file manager), COPY (the path) and PEEK (a big round preview).
- Top: the time ring, an arc of dots from "3 years ago" to "now"; drag its two red handles to keep
  only files changed in that window. Top left: the path as a constellation (click a star to go back).
  Bottom: kind chips.
- Keys: type to search, Enter opens (a folder: goes inside), Tab / arrows move, Space peeks,
  Ctrl+C copies the path, Backspace with nothing typed goes up one folder, Esc steps back / closes.

Runs as its own small process (com.aero.Orbit) and quits by itself 10 minutes after closing.
"""
import hashlib
import math
from array import array
from bisect import bisect_right
import os
import random
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import cairo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nuvem  # noqa: E402
from nuvem import FG, RED, empacotar, pop, suave  # noqa: E402

CASA = os.path.expanduser("~")
DADOS = os.environ.get("AERO_DADOS", "/mnt/d")          # the data drive (shown as "D:" next to your folders)
RAIZES = [CASA] + ([DADOS] if os.path.ismount(DADOS) else [])
NOME_RAIZ = {CASA: "HOME", DADOS: "D:"}


def exibir(path):
    """~/x for the home, D:/x for the data drive."""
    for raiz, nome in ((DADOS, "D:"), (CASA, "~")):
        if path == raiz or path.startswith(raiz + "/"):
            return nome + path[len(raiz):]
    return path
FUNDO_BOLHA = (0.08, 0.08, 0.09)
PULAR = {"node_modules", "target", "__pycache__", "build", "dist", "venv", "site-packages", "steamapps",
         "compatdata", "shadercache", "Cache", "cache", "CachedData", "GPUCache", "System Volume Information",
         # engines/SDK installs and project caches: thousands of internal files nobody searches for
         "Library", "Temp", "obj", "Intermediate", "DerivedDataCache", "Unity", "sdk", "platforms",
         "ndk", "build-tools", "system-images", "Logs"}
LIMITE_INDICE = 400_000          # entries in all; each root also has a time budget (TEMPO_RAIZ)
TEMPO_RAIZ = {CASA: 5, DADOS: 10}
MAX_BOLHAS = 44
RAIOS = (70, 54, 42, 32, 25)     # rank 0, 1-5, 6-15, 16-29, the rest
BURACO = 185                     # the empty circle in the middle, where the search is written
FICAR_ABERTO = 600               # s: the process quits this long after closing (RAM)

TIPOS = {
    "img": ({"png", "jpg", "jpeg", "webp", "gif", "bmp", "svg", "avif", "heic", "tif", "tiff"}, "IMG", (0.79, 0.65, 0.92)),
    "doc": ({"pdf", "doc", "docx", "odt", "txt", "md", "rtf", "epub", "xls", "xlsx", "ods", "ppt", "pptx", "odp", "csv"},
            "DOC", RED),
    "code": ({"py", "js", "ts", "tsx", "jsx", "rs", "c", "h", "cpp", "hpp", "go", "sh", "java", "kt", "lua", "gd",
              "glsl", "json", "toml", "yaml", "yml", "html", "css", "scss", "xml", "conf", "ini", "cs", "rb", "php",
              "tscn", "zig", "nix", "sql"}, "</>", (0.65, 0.78, 0.93)),
    "aud": ({"mp3", "flac", "wav", "ogg", "opus", "m4a", "aac"}, "♪", (0.79, 0.65, 0.92)),
    "vid": ({"mp4", "mkv", "webm", "mov", "avi", "m4v"}, "▶", (0.95, 0.71, 0.56)),
    "arc": ({"zip", "tar", "gz", "xz", "7z", "rar", "zst", "bz2", "apk", "iso", "deb", "rpm", "appimage"},
            "ZIP", (0.94, 0.83, 0.60)),
}
CHIPS = [("ALL", None), ("IMAGES", "img"), ("DOCS", "doc"), ("CODE", "code"), ("AUDIO", "aud"),
         ("VIDEO", "vid"), ("FOLDERS", "dir")]
ACOES = [("OPEN", -100), ("SHOW", -55), ("COPY", -10), ("PEEK", 35)]

# time ring: dot i of N is an age on a log scale, from 3 years (left) to 1 hour (right)
ANEL_N = 70
IDADE_MAX, IDADE_MIN = 3 * 365 * 86400, 3600
MARCAS = [("3 Y", IDADE_MAX), ("YEAR", 365 * 86400), ("MONTH", 30 * 86400), ("WEEK", 7 * 86400),
          ("TODAY", 86400), ("NOW", IDADE_MIN)]


def fracao_idade(idade):
    a, b = math.log10(IDADE_MAX), math.log10(IDADE_MIN)
    return min(1.0, max(0.0, (a - math.log10(max(idade, 1))) / (a - b)))


def idade_fracao(f):
    a, b = math.log10(IDADE_MAX), math.log10(IDADE_MIN)
    return 10 ** (a + (b - a) * f)


def tipo_de(nome, eh_dir):
    if eh_dir:
        return "dir"
    ext = nome.rsplit(".", 1)[-1].casefold() if "." in nome else ""
    for t, (exts, _g, _c) in TIPOS.items():
        if ext in exts:
            return t
    return "other"


def tamanho_txt(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024


def idade_txt(ts):
    s = max(0, time.time() - ts)
    if s < 3600:
        return "NOW"
    if s < 86400:
        return f"{int(s // 3600)} H"
    if s < 7 * 86400:
        return f"{int(s // 86400)} D"
    if s < 60 * 86400:
        return f"{int(s // (7 * 86400))} W"
    if s < 365 * 86400:
        return f"{int(s // (30 * 86400))} MO"
    return f"{int(s // (365 * 86400))} Y"


def pontuar(q, nome_cf, rel_cf):
    """How well a file matches: the name equal, starting with, a word starting with, containing the text...
    then the folders on its path, then the letters in order (typos with gaps)."""
    if nome_cf == q:
        return 10.0
    if nome_cf.startswith(q):
        return 8.0
    for sep in " ._-":
        nome_cf = nome_cf.replace(sep, " ")
    if any(p.startswith(q) for p in nome_cf.split()):
        return 6.0
    if q in nome_cf:
        return 4.0
    if q in rel_cf:
        return 1.5
    it = iter(nome_cf)
    if len(q) >= 3 and all(c in it for c in q):
        return 1.0
    return 0.0


class Indice:
    """Every file and folder under ~ and the data drive D: (hidden ones, Windows system folders and heavy
    build/cache folders skipped), walked in a thread when ORBIT opens. The home is ready first (search
    works right away), D: is added after. Kept as parallel arrays (path list + compact number arrays):
    about 3x less RAM than a tuple per file."""

    def __init__(self):
        self.itens = ([], array("d"), array("q"), bytearray())      # paths, mtimes, sizes, is_dir
        self.pronto, self.rodando, self.quando = False, False, 0.0

    def construir(self, ao_terminar):
        if self.rodando or (self.pronto and time.monotonic() - self.quando < 300):
            return
        self.rodando = True

        def trabalho():
            itens = ([], array("d"), array("q"), bytearray())
            for raiz in RAIZES:
                self._andar(raiz, itens)
                n = len(itens[0])
                # all names, casefolded, in ONE string ("\n" between them) + where each one starts: a search
                # is then str.find (C speed) instead of a Python loop over 250k files
                nomes = [c[c.rfind("/") + 1:].casefold() for c in itens[0][:n]]
                inicios, k = array("q"), 0
                for nm in nomes:
                    inicios.append(k)
                    k += len(nm) + 1
                self.itens = (itens[0][:n], itens[1][:n], itens[2][:n], itens[3][:n], "\n".join(nomes), inicios)
                self.pronto = True
                GLib.idle_add(lambda: (ao_terminar(), False)[1])
            self.rodando, self.quando = False, time.monotonic()
        threading.Thread(target=trabalho, daemon=True).start()

    def _andar(self, raiz, itens):
        pilha, t0, prazo = [raiz], time.monotonic(), TEMPO_RAIZ.get(raiz, 5)
        caminhos, mtimes, tams, dirs = itens
        while pilha and len(caminhos) < LIMITE_INDICE and time.monotonic() - t0 < prazo:
            pasta = pilha.pop()
            try:
                with os.scandir(pasta) as it:
                    for e in it:
                        if e.name.startswith((".", "$")):
                            continue
                        try:
                            eh_dir = e.is_dir()                 # a link to a folder counts as a folder...
                            link = e.is_symlink()
                            st = e.stat()
                        except OSError:
                            continue
                        caminhos.append(e.path)
                        mtimes.append(st.st_mtime)
                        tams.append(st.st_size)
                        dirs.append(1 if eh_dir else 0)
                        if eh_dir and not link and e.name not in PULAR:     # ...but is not walked into
                            pilha.append(e.path)
            except OSError:
                continue


class Arquivo:
    __slots__ = ("path", "nome", "tipo", "mtime", "tam", "r", "x", "y", "cx", "cy", "alvo_x", "alvo_y", "vx", "vy",
                 "escala", "visivel", "lente", "ox", "oy", "atraso", "nasce", "filhos", "sup", "pedido",
                 "grupo", "variantes", "pontos", "rank", "parado")

    def __init__(self, path, eh_dir, mtime, tam, grupo=0):
        self.path, self.nome = path, NOME_RAIZ.get(path) or os.path.basename(path) or path
        self.grupo = grupo                         # > 0: a folder standing for that many results inside it
        self.tipo = "grupo" if grupo else tipo_de(self.nome, eh_dir)
        self.mtime, self.tam = mtime, tam
        self.r, self.rank, self.variantes = 30, 0, 0
        self.x = self.y = self.cx = self.cy = self.alvo_x = self.alvo_y = 0.0
        self.vx = self.vy = self.ox = self.oy = self.lente = 0.0
        self.escala, self.visivel, self.atraso = 1.0, 1.0, 0.0
        self.nasce = time.monotonic()
        self.filhos = grupo or None
        self.sup, self.pedido, self.pontos, self.parado = None, False, None, None


# ---------- the heavy part (search, layout, filler), done in a thread: typing never stutters ----------

SETOR = {"grupo": 180, "dir": 180, "other": -132, "arc": -132, "img": -55, "doc": 0, "code": 58, "aud": 122, "vid": 122}
NOME_SETOR = {180: "FOLDERS", -132: "OTHER", -55: "IMAGES", 0: "DOCS", 58: "CODE", 122: "MEDIA"}
SX, SY = 1.45, 0.82              # the orbit is an ellipse: screens are wide
ANEIS = (75, 205, 320)           # ring distances past the empty middle (best results on the first ring)


def raio_rank(k):
    return RAIOS[0] if k == 0 else RAIOS[1] if k < 5 else RAIOS[2] if k < 16 else RAIOS[3] if k < 30 else RAIOS[4]


def nivel(k):
    return 0 if k < 5 else 1 if k < 16 else 2


def buscar(p, itens):
    """[(path, eh_dir, mtime, tam, grupo, variantes, tipo)], best first. With nothing typed: the folder,
    newest first. Typing: the index scored. Same-name files (venus.png, venus.txt) collapse into one
    bubble with a +N badge, and a folder holding 5+ results becomes one bubble that stands for them."""
    agora = time.time()
    anel = p["anel"]
    lo, hi = idade_fracao(anel[0] / ANEL_N), idade_fracao(anel[1] / ANEL_N)

    def passa(eh_dir, nome, mtime):
        if p["filtro"] and tipo_de(nome, eh_dir) != p["filtro"]:
            return False
        idade = agora - mtime
        return not ((anel[0] > 0 and idade > lo) or (anel[1] < ANEL_N and idade < hi))

    q = p["consulta"].strip().casefold()
    cands = []
    if not q:
        try:
            with os.scandir(p["pasta"]) as it:
                for e in it:
                    if e.name.startswith("."):
                        continue
                    try:
                        eh_dir, st = e.is_dir(), e.stat()
                    except OSError:
                        continue
                    if passa(eh_dir, e.name, st.st_mtime):
                        cands.append((st.st_mtime, e.path, eh_dir, st.st_mtime, st.st_size))
        except OSError:
            pass
        if p["pasta"] == CASA and DADOS in RAIZES and p["filtro"] in (None, "dir"):
            cands.append((float("inf"), DADOS, True, agora, 0))        # the D: drive, always in the home view
    else:
        bases = tuple(r + "/" for r in RAIZES) if p["pasta"] == CASA else (p["pasta"].rstrip("/") + "/",)
        caminhos, mtimes, tams, dirs, texto, inicios = itens if len(itens) == 6 else (*itens, "", array("q"))
        achados, pos = [], texto.find(q)
        while pos != -1 and len(achados) < 20000:
            i = bisect_right(inicios, pos) - 1
            achados.append(i)
            pos = texto.find(q, inicios[i + 1]) if i + 1 < len(inicios) else -1
        if not achados and len(q) >= 3:                    # nothing: try letters-in-order (typos), slower
            vistos = set(achados)
            achados += [i for i, nm in enumerate(texto.split("\n")) if i not in vistos and pontuar(q, nm, "") > 0][:200]
        for i in achados:
            path = caminhos[i]
            if not path.startswith(bases):
                continue
            nome_cf = texto[inicios[i]:(inicios[i + 1] - 1) if i + 1 < len(inicios) else len(texto)]
            eh_dir, mtime, tam = bool(dirs[i]), mtimes[i], tams[i]
            rel = path[len(next(b for b in bases if path.startswith(b))):]
            nota = pontuar(q, nome_cf, rel.casefold())
            if nota <= 0 or not passa(eh_dir, nome_cf, mtime):
                continue
            nota += 2 * math.exp(-(agora - mtime) / (30 * 86400)) - 0.15 * rel.count("/")
            cands.append((nota, path, eh_dir, mtime, tam))
    cands.sort(key=lambda t: -t[0])
    unicos, vistos = [], {}
    for _n, path, eh_dir, mtime, tam in cands[:400]:
        nome = os.path.basename(path)
        chave = None if eh_dir else (os.path.dirname(path), nome.split(".")[0].casefold())
        if chave is not None and chave in vistos:
            unicos[vistos[chave]][5] += 1
            continue
        if chave is not None:
            vistos[chave] = len(unicos)
        unicos.append([path, eh_dir, mtime, tam, 0, 0, tipo_de(nome, eh_dir)])
    if q:
        conta = {}
        for u in unicos[:200]:
            d = os.path.dirname(u[0])
            conta[d] = conta.get(d, 0) + 1
        grandes = {d for d, n in conta.items() if n >= 5 and d != p["pasta"].rstrip("/") and d not in RAIZES}
        saida, feitas = [], set()
        for u in unicos:
            d = os.path.dirname(u[0])
            if d in grandes:
                if d not in feitas:
                    feitas.add(d)
                    try:
                        mt = os.stat(d).st_mtime
                    except OSError:
                        mt = u[2]
                    saida.append([d, True, mt, 0, conta[d], 0, "grupo"])
                continue
            saida.append(u)
        unicos = saida
    return unicos[:MAX_BOLHAS]


CENTROS = {}                     # slice centre of each kind in the last layout (for the slice names)


def orbitar(itens, w, h):
    """Rings by rank (the best right next to the search), a slice of the orbit per kind (images up-right,
    docs right, code down-right, media down-left, folders left), then relaxation so nothing overlaps.
    Returns [(x, y, r)] around (0, 0)."""
    n = len(itens)
    raios = [raio_rank(k) for k in range(n)]
    R = [BURACO + a for a in ANEIS]
    # each kind's slice grows with how much it has (a folder full of folders uses the whole orbit),
    # keeping the order around the circle: folders, other, images, docs, code, media
    ordem = [180, -132, -55, 0, 58, 122]
    peso = {g: 0.0 for g in ordem}
    for k, it in enumerate(itens):
        peso[SETOR[it[6]]] += 2 * raios[k] + 18
    total = sum(peso.values()) or 1.0
    centro, vaos, inicio = {}, {}, 180 - 180 * peso[180] / total
    for g in ordem:
        vaos[g] = 360 * peso[g] / total
        centro[g] = inicio + vaos[g] / 2
        inicio += vaos[g]
    fatias = {}
    for k, it in enumerate(itens):
        fatias.setdefault((nivel(k), SETOR[it[6]]), []).append(k)
    alvo = [(0.0, 0.0)] * n
    for (nv, g), ks in fatias.items():
        ang = centro[g]
        mais = menos = 0.0
        # a roomy slice spreads its ring along the whole arc (no lopsided pile); a crowded one packs tight
        folga = max(1.0, math.radians(vaos[g]) * 0.92 / (sum((2 * raios[k] + 18) / R[nv] for k in ks) or 1))
        for j, k in enumerate(ks):
            passo = (2 * raios[k] + 18) / R[nv] * folga
            if j == 0:
                off, mais, menos = 0.0, passo / 2, passo / 2
            elif j % 2:
                off, mais = mais + passo / 2, mais + passo
            else:
                off, menos = -(menos + passo / 2), menos + passo
            a = math.radians(ang) + off
            alvo[k] = (R[nv] * math.cos(a) * SX, R[nv] * math.sin(a) * SY)
    CENTROS.clear()
    CENTROS.update({g: c for g, c in centro.items() if peso[g] > 0})
    pos = [list(a) for a in alvo]
    lx, ly = w / 2 - 60, h / 2 - 115
    hx, hy = BURACO * 1.55, BURACO * 0.72
    for _ in range(60):
        for i in range(n):
            pi, ri = pos[i], raios[i]
            pi[0] += (alvo[i][0] - pi[0]) * 0.05
            pi[1] += (alvo[i][1] - pi[1]) * 0.05
            e = math.hypot(pi[0] / (hx + ri), pi[1] / (hy + ri))
            if 1e-6 < e < 1:
                pi[0] /= e
                pi[1] /= e
            pi[0] = max(-lx + ri, min(lx - ri, pi[0]))
            pi[1] = max(-ly + ri, min(ly - ri, pi[1]))
        for i in range(n):
            pi, ri = pos[i], raios[i]
            for j in range(i + 1, n):
                pj = pos[j]
                lim = ri + raios[j] + 10
                dx, dy = pj[0] - pi[0], pj[1] - pi[1]
                if dx > lim or dx < -lim or dy > lim or dy < -lim:
                    continue
                d = math.hypot(dx, dy)
                if d >= lim:
                    continue
                if d < 0.01:
                    dx, dy, d = 1.0, 0.0, 1.0
                s = (lim - d) / 2 / d
                pi[0] -= dx * s
                pi[1] -= dy * s
                pj[0] += dx * s
                pj[1] += dy * s
    return [(pos[k][0], pos[k][1], raios[k]) for k in range(n)]


def semear(xyr, voo):
    """The halftone filler around the new layout (the same physics as the start menu's, nuvem.py).
    Nearest bubbles looked up in a coarse grid, so it is ~10x cheaper than checking all of them.
    e[9] is each dot's delay; voo=True: dots fly in from far away (opening), else they just fade in."""
    passo, alc, cel = 16, 150, 160
    if not xyr:
        return []
    baldes = {}
    for i, (x, y, r) in enumerate(xyr):
        for cx in range(int((x - r - alc) // cel), int((x + r + alc) // cel) + 1):
            for cy in range(int((y - r - alc) // cel), int((y + r + alc) // cel) + 1):
                baldes.setdefault((cx, cy), []).append(i)
    x0 = min(x - r for x, y, r in xyr) - alc
    x1 = max(x + r for x, y, r in xyr) + alc
    y0 = min(y - r for x, y, r in xyr) - alc
    y1 = max(y + r for x, y, r in xyr) + alc
    rnd = random.Random(len(xyr))
    lim = max(x1 - x0, y1 - y0) / 2 or 1
    pontos, y, linha = [], y0, 0
    while y <= y1:
        x = x0 + (passo / 2 if linha % 2 else 0)
        while x <= x1:
            cand = baldes.get((int(x // cel), int(y // cel)))
            if cand:
                dists = sorted((math.hypot(x - xyr[i][0], y - xyr[i][1]) - xyr[i][2], i) for i in cand)
                borda = dists[0][0]
                if 5 < borda < alc and math.hypot(x / 1.55, y / 0.72) > BURACO * 0.9:
                    t = borda / alc
                    if t < 0.35 or rnd.random() > (t - 0.35) * 1.2:
                        if voo:
                            ang, longe = rnd.uniform(0, 2 * math.pi), rnd.uniform(180, 420)
                            sx, sy = x + math.cos(ang) * longe, y + math.sin(ang) * longe
                            atraso = math.hypot(x, y) / lim * 0.35 + rnd.uniform(0, 0.06)
                        else:
                            sx, sy, atraso = x, y, rnd.uniform(0, 0.18)
                        pontos.append([x, y, sx, sy, 0.0, 0.0, 1.0 + 3.8 * (1 - t) ** 1.6,
                                       0.06 + 0.36 * (1 - t) ** 1.3, [i for _, i in dists[:3]], atraso])
            x += passo
        y += passo * 0.87
        linha += 1
    return pontos


class Orbita(nuvem.Nuvem):
    def __init__(self, app):
        super().__init__(app)
        Camada.set_namespace(self, "orbit")
        self.app_orbit = app
        # the dark veil is a CSS box under the drawing (painted by the GPU, faded with set_opacity), not a
        # full-screen cairo paint every frame
        prov = Gtk.CssProvider()
        prov.load_from_string(".orbit-veu { background-color: rgba(5, 5, 6, 0.84); }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_USER + 3)
        self.veu = Gtk.Box(hexpand=True, vexpand=True, can_target=False)
        self.veu.add_css_class("orbit-veu")
        self.set_child(None)
        raiz = Gtk.Overlay()
        raiz.set_child(self.veu)
        raiz.add_overlay(self.area)
        self.set_child(raiz)
        self.indice = Indice()
        self.pasta = CASA
        self.consulta = ""
        self.filtro = None
        self.anel = [0, ANEL_N]                  # handles (dot indexes): all ages
        self.arrasto = None                      # which handle is being dragged
        self.foco = None                         # sticky focus (the bubble that has satellites)
        self.peek = None                         # (Arquivo, big surface or text lines)
        self.reseed_suave = False
        self.navegou = False
        self.zonas = []                          # clickable things drawn this frame: (kind, value, x, y, r)
        arr = Gtk.GestureDrag()
        arr.connect("drag-begin", self._arrasto_ini)
        arr.connect("drag-update", self._arrasto_mov)
        arr.connect("drag-end", lambda *_: GLib.idle_add(self._arrasto_fim))
        self.area.add_controller(arr)

    # ---------- data ----------

    def carregar(self):
        pass

    def atualizar(self, imediato=False):
        """Something changed (text, folder, filters, index ready): the search, the layout and the filler are
        worked out in a thread (debounced while typing); only the newest answer is applied."""
        self.geracao = getattr(self, "geracao", 0) + 1
        if getattr(self, "_agendado", None):
            GLib.source_remove(self._agendado)
        g = self.geracao
        self._agendado = GLib.timeout_add(0 if imediato else 60, lambda: self._disparar(g))

    def _disparar(self, g):
        self._agendado = None
        p = {"consulta": self.consulta, "pasta": self.pasta, "filtro": self.filtro, "anel": list(self.anel)}
        w = self.area.get_width() or self.get_default_size()[0] or 1920
        h = self.area.get_height() or self.get_default_size()[1] or 1080
        itens, voo = self.indice.itens, getattr(self, "_abrindo", False)

        def trabalho():
            res = buscar(p, itens)
            xyr = orbitar(res, w, h)
            GLib.idle_add(self._aplicar, g, res, xyr, semear(xyr, voo))
        threading.Thread(target=trabalho, daemon=True).start()
        return False

    def _aplicar(self, g, res, xyr, sementes):
        if g != self.geracao:
            return False
        velhas = {(b.path, b.grupo > 0): b for b in self.bolhas}
        agora, novas = time.monotonic(), []
        abrindo = getattr(self, "_abrindo", False)
        for k, (path, eh_dir, mtime, tam, grupo, variantes, _tipo) in enumerate(res):
            chave = (path, grupo > 0)
            b = velhas.get(chave) or Arquivo(path, eh_dir, mtime, tam, grupo)
            b.grupo, b.variantes, b.rank = grupo, variantes, k
            if grupo:
                b.filhos = grupo
            b.x, b.y, b.r = xyr[k]
            b.alvo_x, b.alvo_y = b.x, b.y
            if chave not in velhas:
                b.nasce = agora + (0.12 + min(0.4, math.hypot(b.x, b.y) / 1600) if abrindo else min(0.3, k * 0.01))
                b.cx, b.cy = b.x * 0.3, b.y * 0.3
            novas.append(b)
        self.bolhas = novas
        self.sel = 0 if novas else -1
        self.foco = None
        t_rel = agora - self.t_abrir
        for e in sementes:
            e[9] += t_rel
        self.enfeites, self._t_fisica = sementes, agora
        self._abrindo = False
        self.pedir_extras()
        self.animar()
        return False

    def pedir_extras(self):
        """Thumbnails and folder sizes, loaded in a thread and handed back to the drawing."""
        faltam = [b for b in self.bolhas if not b.pedido and b.tipo in ("img", "dir")]
        for b in faltam:
            b.pedido = True
        if not faltam:
            return

        def trabalho():
            for b in faltam:
                if b.tipo == "dir":
                    try:
                        with os.scandir(b.path) as it:
                            n = sum(1 for e in it if not e.name.startswith("."))
                    except OSError:
                        n = 0
                    GLib.idle_add(self._extra_pronto, b, n, None)
                else:
                    GLib.idle_add(self._extra_pronto, b, None, miniatura(b.path, b.tam))
        threading.Thread(target=trabalho, daemon=True).start()

    def _extra_pronto(self, b, filhos, pb):
        if filhos is not None:
            b.filhos, b.pontos = filhos, None
        if pb is not None:
            b.sup = b.parado = pixbuf_superficie(pb)
        self.area.queue_draw()
        return False

    # ---------- GIFs: only the one under the pointer plays ----------

    def gif_foco(self, b):
        """Called every frame with the focused bubble. A GIF starts playing when it gets the focus and goes
        back to its still thumbnail when it loses it (one animation decoded at a time: cheap)."""
        atual = getattr(self, "gif_atual", None)
        if b is atual:
            return
        if atual is not None:
            atual.sup = atual.parado
        self.gif_anim = None
        if getattr(self, "gif_fonte", None):
            GLib.source_remove(self.gif_fonte)
            self.gif_fonte = None
        eh_gif = b is not None and b.nome.casefold().endswith(".gif") and b.tam < 30_000_000
        self.gif_atual = b if eh_gif else None
        if not eh_gif:
            return

        def carregar():
            try:
                anim = GdkPixbuf.PixbufAnimation.new_from_file(b.path)
            except GLib.Error:
                return
            GLib.idle_add(self._gif_pronto, b, anim)
        threading.Thread(target=carregar, daemon=True).start()

    def _gif_pronto(self, b, anim):
        if b is not getattr(self, "gif_atual", None) or anim.is_static_image():
            return False
        self.gif_anim = anim.get_iter(None)
        self._gif_quadro()
        return False

    def _gif_quadro(self):
        self.gif_fonte = None
        b, it = getattr(self, "gif_atual", None), getattr(self, "gif_anim", None)
        if b is None or it is None or not self.get_visible() or self.t_fechar is not None:
            return False
        it.advance(None)
        pb = it.get_pixbuf()
        w, h = pb.get_width(), pb.get_height()
        k = 240 / max(w, h)
        b.sup = pixbuf_superficie(pb.scale_simple(max(1, int(w * k)), max(1, int(h * k)), GdkPixbuf.InterpType.BILINEAR)
                                  if k < 1 else pb)
        if self.peek is not None and self.peek[0] is b:          # the big preview plays too
            k = 900 / max(w, h)
            grande = pb.scale_simple(int(w * k), int(h * k), GdkPixbuf.InterpType.BILINEAR) if k < 1 else pb
            self.peek = (b, pixbuf_superficie(grande), self.peek[2])
        self.area.queue_draw()
        atraso = it.get_delay_time()
        self.gif_fonte = GLib.timeout_add(max(20, atraso if atraso > 0 else 100), self._gif_quadro)
        return False

    def semear_enfeites(self, w, h):
        self.enfeites, self._t_fisica = [], time.monotonic()     # seeded in the thread (semear)

    # ---------- navigation ----------

    def ir_para(self, pasta, manter=False):
        """Go to a folder. manter=True keeps the search (a group bubble: its results, inside it)."""
        self.pasta = pasta
        if not manter:
            self.consulta = ""
        self.atualizar(imediato=True)

    def abrir_item(self, b):
        if b.tipo == "grupo":
            self.ir_para(b.path, manter=True)
            return
        if b.tipo == "dir":
            self.ir_para(b.path)
            return
        self.fechar(lambda: subprocess.Popen(["gio", "open", b.path], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True))

    def acao(self, nome, b):
        if nome == "OPEN":
            self.abrir_item(b)
        elif nome == "SHOW":
            pasta = os.path.dirname(b.path)
            self.fechar(lambda: subprocess.Popen(["gio", "open", pasta], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, start_new_session=True))
        elif nome == "COPY":
            subprocess.Popen(["wl-copy", b.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.aviso = ("PATH COPIED", time.monotonic())
            self.animar()
        elif nome == "PEEK":
            self.espiar(b)

    def espiar(self, b):
        if self.peek is not None and self.peek[0] is b:
            self.peek = None
        else:
            conteudo = None
            if b.tipo == "img":
                pb = miniatura(b.path, b.tam, 900)
                conteudo = pixbuf_superficie(pb) if pb is not None else None
            elif b.tipo in ("code", "doc", "other") and b.tam < 4_000_000 and not b.nome.endswith(".pdf"):
                try:
                    with open(b.path, "rb") as f:
                        bruto = f.read(4096)
                    if b"\0" not in bruto:
                        conteudo = bruto.decode("utf-8", "replace").expandtabs(4).splitlines()[:28]
                except OSError:
                    pass
            elif b.tipo in ("dir", "grupo"):
                try:
                    conteudo = sorted((e.name + ("/" if e.is_dir() else "")) for e in os.scandir(b.path)
                                      if not e.name.startswith("."))[:28]
                except OSError:
                    conteudo = []
            self.peek = (b, conteudo, time.monotonic())
        self.animar()

    # ---------- open / close ----------

    def abrir(self):
        self.ajustar_ao_monitor()
        self.consulta, self.peek, self.foco, self.filtro = "", None, None, None
        self.anel = [0, ANEL_N]
        self.bolhas, self.enfeites = [], []
        self.t_abrir, self.t_fechar = time.monotonic(), None
        self._abrindo = True
        self.atualizar(imediato=True)
        self.indice.construir(self._indice_pronto)
        Camada.set_keyboard_mode(self, Camada.KeyboardMode.EXCLUSIVE)
        self.present()
        self.animar()
        self.app_orbit.segurar()

    def _indice_pronto(self):
        if self.get_visible() and self.consulta:
            self.atualizar()
        self.area.queue_draw()

    def fechar(self, depois=None):
        def tudo():
            if depois:
                depois()
            self.app_orbit.soltar_depois()
        super().fechar(tudo)

    # ---------- animation ----------

    def _quadro(self, *a):
        r = super()._quadro(*a)
        agora = time.monotonic()
        if r == GLib.SOURCE_REMOVE and self.t_fechar is None and self.get_visible():
            vivo = (any(agora - b.nasce < 0.6 for b in self.bolhas)
                    or (self.peek is not None and agora - self.peek[2] < 0.5)
                    or (getattr(self, "aviso", None) and agora - self.aviso[1] < 1.6))
            if vivo:
                GLib.idle_add(lambda: (self.animar(), False)[1])
        return r

    def _onde(self, b, agora):
        cx, cy = self._centro()
        x, y, r = cx + b.cx, cy + b.cy, b.r * b.escala
        if self.t_fechar is None:
            k = pop(agora - b.nasce)
            x, y = cx + (x - cx) * min(1.0, 0.6 + 0.4 * k), cy + (y - cy) * min(1.0, 0.6 + 0.4 * k)
            r *= k
            a = min(1.0, max(0.0, (agora - b.nasce) / 0.12))
        else:
            p = suave(0, 0.3, agora - self.t_fechar)
            x, y = x + (cx - x) * p * 0.8, y + (cy - y) * p * 0.8
            r *= 1 - p * 0.85
            a = 1 - p
        r *= 1 + 0.5 * b.lente
        return x + b.ox, y + b.oy, r, a

    # ---------- geometry of the extras ----------

    def geo_anel(self, w, h):
        cx, cy = self._centro()
        R = min(h * 0.43, w * 0.3, 520)
        return cx, cy + 40, R, math.radians(200), math.radians(340)

    def ponto_anel(self, i, w, h):
        cx, cy, R, a0, a1 = self.geo_anel(w, h)
        a = a0 + (a1 - a0) * i / ANEL_N
        return cx + R * math.cos(a), cy + R * math.sin(a)

    def satelites(self, b, agora):
        x, y, r, a = self._onde(b, agora)
        dist = b.r * 1.5 + 34
        return [(nome, x + dist * math.cos(math.radians(ang)), y + dist * math.sin(math.radians(ang)), a)
                for nome, ang in ACOES]

    # ---------- drawing ----------

    def desenhar(self, _area, cr, w, h):
        agora = time.monotonic()
        self.zonas = []
        if self.t_fechar is None:
            fundo = min(1.0, (agora - self.t_abrir) / 0.25)
        else:
            fundo = 1 - suave(0, 0.3, agora - self.t_fechar)
        if abs(self.veu.get_opacity() - fundo) > 0.01:
            self.veu.set_opacity(fundo)
        self.desenhar_anel(cr, w, h, fundo)
        self.desenhar_guias(cr, w, h, fundo)
        if self.bolhas:
            self.desenhar_enfeites(cr, w, h, agora, fundo)
        sobre = self.bolha_em(*self.mouse) if self.mouse else None
        if sobre is not None:
            self.foco = sobre
        elif self.foco is not None and self.mouse is not None and 0 <= self.foco < len(self.bolhas):
            fx, fy, _r, _a = self._onde(self.bolhas[self.foco], agora)
            if math.hypot(self.mouse[0] - fx, self.mouse[1] - fy) > self.bolhas[self.foco].r * 1.5 + 80:
                self.foco = None
        if self.foco is None and (self.consulta or self.navegou) and 0 <= self.sel < len(self.bolhas):
            foco = self.sel
        else:
            foco = self.foco
        self.gif_foco(self.bolhas[foco] if foco is not None and 0 <= foco < len(self.bolhas) else None)
        ordem = sorted(range(len(self.bolhas)), key=lambda i: (i == foco, self.bolhas[i].r))
        for i in ordem:
            self.desenhar_bolha(cr, self.bolhas[i], agora, i == foco)
        if foco is not None and 0 <= foco < len(self.bolhas) and self.t_fechar is None:
            self.desenhar_satelites(cr, self.bolhas[foco], agora)
        self.desenhar_centro(cr, w, h, fundo, agora)
        self.desenhar_caminho(cr, w, h, fundo)
        self.desenhar_chips(cr, w, h, fundo)
        if self.peek is not None:
            self.desenhar_peek(cr, w, h, agora)

    def desenhar_bolha(self, cr, b, agora, focada):
        x, y, r, a = self._onde(b, agora)
        if r < 0.6 or a <= 0.01:
            return
        cr.new_path()
        if focada:
            g = cairo.RadialGradient(x, y, r * 0.8, x, y, r * 1.7)
            for k in range(9):
                t = k / 8
                g.add_color_stop_rgba(t, *FG, 0.24 * a * math.exp(-4 * t * t) * (1 - t ** 6))
            cr.set_source(g)
            cr.arc(x, y, r * 1.7, 0, 2 * math.pi)
            cr.fill()
        if b.tipo == "img" and b.sup is not None:
            sup = b.sup
            cr.save()
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.clip()
            esc = 2 * r / min(sup.get_width(), sup.get_height())      # cover the circle
            cr.translate(x, y)
            cr.scale(esc, esc)
            cr.set_source_surface(sup, -sup.get_width() / 2, -sup.get_height() / 2)
            cr.get_source().set_filter(cairo.FILTER_NEAREST if esc > 2 else cairo.FILTER_GOOD)   # pixel art stays crisp
            cr.paint_with_alpha(a)
            cr.restore()
            cr.new_path()
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, 0.14 * a)
            cr.set_line_width(1)
            cr.stroke()
        else:
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(*FUNDO_BOLHA, 0.9 * a)
            cr.fill_preserve()
            cr.set_source_rgba(*FG, 0.12 * a)
            cr.set_line_width(1.2)
            cr.stroke()
            if b.tipo in ("dir", "grupo"):
                self.aglomerado(cr, b, x, y, r, a)
                if b.tipo == "grupo":
                    self._texto_cor(cr, str(b.grupo), x, y, f"Doto Black {max(10, int(r * 0.5))}", FG, a)
            else:
                glifo, cor = self.glifo(b)
                self._texto_cor(cr, glifo, x, y, f"Doto Black {max(8, int(r * (0.42 if len(glifo) > 2 else 0.6)))}",
                                cor, a)
        if b.variantes and r > 14:                              # same name, other formats: +N
            bx, by = x + r * 0.72, y - r * 0.72
            cr.new_path()
            cr.arc(bx, by, 10, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, 0.92 * a)
            cr.fill()
            self._texto_cor(cr, f"+{b.variantes}", bx, by, "Doto Black 9", (0.05, 0.05, 0.05), a)
        if b.rank < 5 and not focada:                           # names only on the first ring (no pile-up)
            nome = b.nome if len(b.nome) <= 22 else b.nome[:20] + "…"
            if b.tipo == "grupo":
                nome = f"{b.grupo} in {nome}"
            self._texto_cor(cr, nome, x, y + r + 14, "Inter 10.5", FG, 0.72 * a)
        elif focada:
            nome = b.nome if len(b.nome) <= 34 else b.nome[:32] + "…"
            self._texto_cor(cr, nome, x, y + r + 16, "Inter Semi-Bold 12.5", FG, a)
            sub = (f"{b.grupo} RESULTS · ENTER TO OPEN" if b.tipo == "grupo" else
                   f"{b.filhos} ITEMS" if b.tipo == "dir" and b.filhos is not None else
                   "FOLDER" if b.tipo == "dir" else tamanho_txt(b.tam)) + " · " + idade_txt(b.mtime)
            self._texto_cor(cr, sub, x, y + r + 34, "Doto Black 10", FG, 0.5 * a)

    def aglomerado(self, cr, b, x, y, r, a):
        """A folder: one dot per item inside (up to 24), scattered like a little cluster (made once)."""
        n = 0 if b.filhos is None else min(24, b.filhos)
        if n == 0:
            cr.arc(x, y, r * 0.12, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, 0.25 * a)
            cr.fill()
            return
        if b.pontos is None:
            rnd = random.Random(int(hashlib.md5(b.path.encode()).hexdigest()[:8], 16))
            b.pontos = []
            for _ in range(n):
                ang, d = rnd.uniform(0, 2 * math.pi), 0.6 * math.sqrt(rnd.random())
                b.pontos.append((d * math.cos(ang), d * math.sin(ang), rnd.uniform(0.05, 0.11), rnd.uniform(0.35, 0.92)))
        for dx, dy, rr, al in b.pontos:
            cr.new_sub_path()
            cr.arc(x + dx * r, y + dy * r, rr * r, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, al * a)
            cr.fill()

    def glifo(self, b):
        if b.tipo in TIPOS:
            _e, g, cor = TIPOS[b.tipo]
            if b.tipo == "doc":
                ext = b.nome.rsplit(".", 1)[-1].upper() if "." in b.nome else "DOC"
                g = ext if len(ext) <= 4 else "DOC"
            return g, cor
        ext = b.nome.rsplit(".", 1)[-1].upper() if "." in b.nome else "•"
        return (ext[:4] if len(ext) <= 5 else "•"), (0.55, 0.545, 0.535)

    def desenhar_satelites(self, cr, b, agora):
        for nome, sx, sy, a in self.satelites(b, agora):
            cr.new_path()
            cr.arc(sx, sy, 23, 0, 2 * math.pi)
            sobre = self.mouse is not None and math.hypot(self.mouse[0] - sx, self.mouse[1] - sy) < 23
            cr.set_source_rgba(*((0.93, 0.92, 0.91) if sobre else FUNDO_BOLHA), 0.95 * a)
            cr.fill_preserve()
            cr.set_source_rgba(*FG, 0.16 * a)
            cr.set_line_width(1)
            cr.stroke()
            self._texto_cor(cr, nome, sx, sy, "Doto Black 10.5", (0.05, 0.05, 0.05) if sobre else FG, a)
            self.zonas.append(("acao", (nome, b), sx, sy, 23))

    def desenhar_centro(self, cr, w, h, fundo, agora):
        cx, cy = self._centro()
        g = cairo.RadialGradient(cx, cy, 0, cx, cy, BURACO)
        g.add_color_stop_rgba(0, *FG, 0.07 * fundo)
        g.add_color_stop_rgba(1, *FG, 0.0)
        cr.set_source(g)
        cr.arc(cx, cy, BURACO, 0, 2 * math.pi)
        cr.fill()
        texto = self.consulta.upper() if self.consulta else "SEARCH"
        tam = 54 if len(texto) <= 8 else 40 if len(texto) <= 14 else 28
        tw = self._texto_cor(cr, texto, cx, cy, f"Doto Black {tam}", FG, fundo * (1 if self.consulta else 0.32))
        piscar = 1.0 if int(agora * 1.6) % 2 == 0 or not self.consulta else 0.25
        cr.rectangle(cx + tw / 2 + 8, cy - tam * 0.42, 5, tam * 0.84)
        cr.set_source_rgba(*RED, fundo * piscar)
        cr.fill()
        if self.consulta and self.t_fechar is None and not getattr(self, "_pisca", None):
            def piscar_():                                          # one timer while there is text to blink
                if self.consulta and self.t_fechar is None and self.get_visible():
                    self.area.queue_draw()
                    return True
                self._pisca = None
                return False
            self._pisca = GLib.timeout_add(620, piscar_)
        if self.consulta and not self.indice.pronto:
            info = "INDEXING …"
        else:
            onde = exibir(self.pasta)
            info = f"{len(self.bolhas)} {'FILES' if self.consulta else 'ITEMS'} · {onde}"
        aviso = getattr(self, "aviso", None)
        if aviso and agora - aviso[1] < 1.6:
            info = aviso[0]
        self._texto_cor(cr, info.upper(), cx, cy + tam * 0.5 + 26, "Doto Black 12", FG, 0.42 * fundo)

    def desenhar_guias(self, cr, w, h, fundo):
        """The rings (faint dotted ellipses) and the name of each slice that has something in it."""
        cx, cy = self._centro()
        cr.save()
        cr.set_dash([0.1, 11])
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_width(2.2)
        for a in ANEIS:
            R = BURACO + a
            cr.save()
            cr.translate(cx, cy)
            cr.scale(SX, SY)
            cr.new_path()
            cr.arc(0, 0, R, 0, 2 * math.pi)
            cr.restore()
            cr.set_source_rgba(*FG, 0.07 * fundo)
            cr.stroke()
        cr.restore()
        if not self.bolhas:
            return
        presentes = {SETOR[b.tipo] for b in self.bolhas}
        if len(presentes) < 2:
            return                                         # one kind only: no slices to name
        R = BURACO + ANEIS[-1] + 95
        for g in presentes:
            a = math.radians(CENTROS.get(g, g))
            ang = g
            x = max(110, min(w - 110, cx + R * math.cos(a) * SX))
            y = max(150, min(h - 130, cy + R * math.sin(a) * SY))
            self._texto_cor(cr, NOME_SETOR[ang], x, y, "Doto Black 12", FG, 0.3 * fundo)

    def desenhar_anel(self, cr, w, h, fundo):
        lo, hi = self.anel
        apagado, aceso = [], []
        for i in range(ANEL_N + 1):
            (aceso if lo <= i <= hi else apagado).append(self.ponto_anel(i, w, h))
        for pts, rr, al in ((apagado, 2.2, 0.16), (aceso, 3.1, 0.75)):
            cr.new_path()
            for x, y in pts:
                cr.new_sub_path()
                cr.arc(x, y, rr, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, al * fundo)
            cr.fill()
        cx, cy, R, a0, a1 = self.geo_anel(w, h)
        for nome, idade in MARCAS:
            f = fracao_idade(idade)
            a = a0 + (a1 - a0) * f
            self._texto_cor(cr, nome, cx + (R + 26) * math.cos(a), cy + (R + 26) * math.sin(a),
                            "Doto Black 11", FG, 0.42 * fundo)
        for k in (0, 1):
            x, y = self.ponto_anel(self.anel[k], w, h)
            cr.new_path()
            cr.arc(x, y, 7, 0, 2 * math.pi)
            cr.set_source_rgba(*RED, fundo)
            cr.set_line_width(2.2)
            cr.stroke()
            self.zonas.append(("anel", k, x, y, 16))

    def desenhar_caminho(self, cr, w, h, fundo):
        """The path as a constellation: one star per folder, joined by a dotted line."""
        raiz = next((r for r in RAIZES[::-1] if self.pasta == r or self.pasta.startswith(r + "/")), None)
        if raiz is not None:
            partes = ([NOME_RAIZ[CASA]] if raiz != CASA else []) + [NOME_RAIZ[raiz]] + \
                     [p for p in self.pasta[len(raiz):].split("/") if p]
        else:
            partes = ["/"] + [p for p in self.pasta.split("/") if p]
        partes = partes[-6:]
        pts, x = [], 80
        for i, nome in enumerate(partes):
            y = 92 + (14 if i % 2 else -6)
            pts.append((x, y, nome))
            x += max(90, 12 * len(nome) + 40)
        for (x1, y1, _), (x2, y2, _) in zip(pts, pts[1:]):
            cr.new_path()
            cr.set_dash([1, 7])
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_line_width(2)
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
            cr.set_source_rgba(*FG, 0.25 * fundo)
            cr.stroke()
            cr.set_dash([])
        for i, (x, y, nome) in enumerate(pts):
            ultimo = i == len(pts) - 1
            cr.new_path()
            cr.arc(x, y, 7 if ultimo else 4.5, 0, 2 * math.pi)
            cr.set_source_rgba(*(RED if ultimo else FG), (1 if ultimo else 0.7) * fundo)
            cr.fill()
            self._texto_cor(cr, nome.upper(), x, y - 20, "Doto Black 13", FG, (0.9 if ultimo else 0.5) * fundo)
            subir = len(partes) - 1 - i
            alvo = self.pasta
            for _ in range(subir):
                alvo = CASA if alvo in RAIZES[1:] else os.path.dirname(alvo)
            self.zonas.append(("pasta", alvo, x, y - 8, 22))

    def desenhar_chips(self, cr, w, h, fundo):
        layout = PangoCairo.create_layout(cr)
        layout.set_font_description(Pango.FontDescription.from_string("Doto Black 13"))
        larguras = []
        for nome, _t in CHIPS:
            layout.set_text(nome, -1)
            larguras.append(layout.get_pixel_size()[0] + 30)
        total = sum(larguras) + 12 * (len(CHIPS) - 1)
        x, y, alt = (w - total) / 2, h - 86, 34
        for (nome, t), lw in zip(CHIPS, larguras):
            ligado = self.filtro == t
            cr.new_path()
            r = alt / 2
            cr.arc(x + r, y + r, r, math.pi / 2, 3 * math.pi / 2)
            cr.arc(x + lw - r, y + r, r, -math.pi / 2, math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(*((0.93, 0.92, 0.91) if ligado else FUNDO_BOLHA), 0.95 * fundo)
            cr.fill()
            self._texto_cor(cr, nome, x + lw / 2, y + alt / 2, "Doto Black 13",
                            (0.05, 0.05, 0.05) if ligado else FG, fundo * (1 if ligado else 0.6))
            self.zonas.append(("chip", t, x + lw / 2, y + alt / 2, lw / 2))
            x += lw + 12
        self._texto_cor(cr, "ORBIT", 80, h - 69, "Doto Black 18", FG, 0.55 * fundo)
        dica = "TAB ORBIT · ENTER OPEN · SPACE PEEK · ⌫ UP · ESC DISSOLVE"
        layout.set_font_description(Pango.FontDescription.from_string("Doto Black 10"))
        layout.set_text(dica, -1)
        dw = layout.get_pixel_size()[0]
        self._texto_cor(cr, dica, w - 60 - dw / 2, h - 69, "Doto Black 10", FG, 0.35 * fundo)

    def desenhar_peek(self, cr, w, h, agora):
        b, conteudo, t0 = self.peek
        k = pop(agora - t0)
        cx, cy = self._centro()
        R = min(w, h) * 0.3 * k
        if R < 1:
            return
        cr.new_path()
        cr.set_source_rgba(0.01, 0.01, 0.012, 0.6 * min(1, (agora - t0) / 0.2))
        cr.paint()
        g = cairo.RadialGradient(cx, cy, R * 0.9, cx, cy, R * 1.35)
        g.add_color_stop_rgba(0, *FG, 0.16)
        g.add_color_stop_rgba(1, *FG, 0.0)
        cr.set_source(g)
        cr.arc(cx, cy, R * 1.35, 0, 2 * math.pi)
        cr.fill()
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.set_source_rgba(*FUNDO_BOLHA, 0.97)
        cr.fill()
        if isinstance(conteudo, cairo.ImageSurface):
            cr.save()
            cr.arc(cx, cy, R, 0, 2 * math.pi)
            cr.clip()
            esc = 2 * R / min(conteudo.get_width(), conteudo.get_height())
            cr.translate(cx, cy)
            cr.scale(esc, esc)
            cr.set_source_surface(conteudo, -conteudo.get_width() / 2, -conteudo.get_height() / 2)
            cr.get_source().set_filter(cairo.FILTER_NEAREST if esc > 2 else cairo.FILTER_GOOD)
            cr.paint()
            cr.restore()
        elif isinstance(conteudo, list) and conteudo:
            cr.save()
            cr.arc(cx, cy, R - 10, 0, 2 * math.pi)
            cr.clip()
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string("JetBrainsMono Nerd Font, monospace 11"))
            lado = R * 1.36                                   # the square inside the circle
            x0, y0 = cx - lado / 2, cy - lado / 2
            layout.set_width(int(lado * Pango.SCALE))
            layout.set_ellipsize(Pango.EllipsizeMode.END)
            for i, linha in enumerate(conteudo[:int(lado // 19)]):
                layout.set_text(linha[:200], -1)
                cr.move_to(x0, y0 + i * 19)
                cr.set_source_rgba(*FG, 0.85)
                PangoCairo.show_layout(cr, layout)
            cr.restore()
        else:
            glifo, cor = self.glifo(b) if b.tipo != "dir" else ("DIR", FG)
            self._texto_cor(cr, glifo, cx, cy, f"Doto Black {int(R * 0.3)}", cor, 1)
        cr.new_path()
        cr.arc(cx, cy, R, 0, 2 * math.pi)
        cr.set_source_rgba(*FG, 0.2)
        cr.set_line_width(1.2)
        cr.stroke()
        self._texto_cor(cr, b.nome, cx, cy + R + 30, "Inter Semi-Bold 15", FG, 1)
        sub = (tamanho_txt(b.tam) if b.tipo != "dir" else f"{b.filhos or 0} ITEMS") + " · " + idade_txt(b.mtime)
        self._texto_cor(cr, sub, cx, cy + R + 54, "Doto Black 12", FG, 0.5)
        onde = exibir(os.path.dirname(b.path))
        self._texto_cor(cr, onde, cx, cy + R + 76, "Inter 10.5", FG, 0.4)

    def _texto_cor(self, cr, s, x, y, fonte, cor, alfa):
        """Centred text (x, y = its centre). Returns the width."""
        cr.new_path()
        cache = self.__dict__.setdefault("_layouts", {})
        item = cache.get((s, fonte))
        if item is None:                         # shaping text is the expensive part: done once per text
            layout = PangoCairo.create_layout(cr)
            layout.set_font_description(Pango.FontDescription.from_string(fonte))
            layout.set_text(s, -1)
            if len(cache) > 500:
                cache.clear()
            item = cache[(s, fonte)] = (layout, *layout.get_pixel_size())
        else:
            PangoCairo.update_layout(cr, item[0])
        layout, tw, th = item
        cr.move_to(x - tw / 2, y - th / 2)
        cr.set_source_rgba(*cor[:3], alfa)
        PangoCairo.show_layout(cr, layout)
        cr.new_path()
        return tw

    # ---------- input ----------

    def zona_em(self, x, y):
        for tipo, valor, zx, zy, zr in reversed(self.zonas):
            if tipo == "chip":
                if abs(x - zx) <= zr and abs(y - zy) <= 17:
                    return tipo, valor
            elif math.hypot(x - zx, y - zy) <= zr:
                return tipo, valor
        return None

    def _clique(self, g, _n, x, y):
        if getattr(self, "_arrastou", False):
            self._arrastou = False
            return
        if self.peek is not None:
            self.peek = None
            self.animar()
            return
        z = self.zona_em(x, y)
        if z is not None:
            tipo, valor = z
            if tipo == "acao":
                self.acao(*valor)
            elif tipo == "pasta":
                self.ir_para(valor)
            elif tipo == "chip":
                self.filtro = valor
                self.atualizar()
            return
        i = self.bolha_em(x, y)
        if i is None:
            self.fechar()
        elif g.get_current_button() == 1:
            self.abrir_item(self.bolhas[i])
        elif g.get_current_button() == 3:
            self.espiar(self.bolhas[i])

    def _arrasto_ini(self, g, x, y):
        z = self.zona_em(x, y)
        self.arrasto = z[1] if z is not None and z[0] == "anel" else None
        self._ini = (x, y)

    def _arrasto_mov(self, g, dx, dy):
        if self.arrasto is None:
            return
        self._arrastou = True
        x, y = self._ini[0] + dx, self._ini[1] + dy
        w, h = self.area.get_width(), self.area.get_height()
        cx, cy, R, a0, a1 = self.geo_anel(w, h)
        a = math.atan2(y - cy, x - cx)
        if a < 0:
            a += 2 * math.pi
        i = round((a - a0) / (a1 - a0) * ANEL_N)
        i = max(0, min(ANEL_N, i))
        if self.arrasto == 0:
            self.anel[0] = min(i, self.anel[1] - 2)
        else:
            self.anel[1] = max(i, self.anel[0] + 2)
        self.area.queue_draw()

    def _arrasto_fim(self):
        if self.arrasto is not None:
            self.arrasto = None
            self.atualizar()
        return False

    def _tecla(self, _c, tecla, _cod, mods):
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        atual = self.bolhas[self.sel] if 0 <= self.sel < len(self.bolhas) else None
        if self.foco is not None and 0 <= self.foco < len(self.bolhas) and self.mouse is not None:
            atual = self.bolhas[self.foco]
        if tecla == Gdk.KEY_Escape:
            if self.peek is not None:
                self.peek = None
                self.animar()
            elif self.consulta:
                self.consulta = ""
                self.atualizar()
            else:
                self.fechar()
            return True
        if tecla == Gdk.KEY_space and atual is not None:
            self.espiar(atual)
            return True
        if self.peek is not None:
            self.peek = None
            self.animar()
        if tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if atual is not None:
                self.abrir_item(atual)
            return True
        if ctrl and tecla in (Gdk.KEY_c, Gdk.KEY_C):
            if atual is not None:
                self.acao("COPY", atual)
            return True
        if tecla == Gdk.KEY_BackSpace:
            if self.consulta:
                self.consulta = self.consulta[:-1]
                self.atualizar()
            elif self.pasta in RAIZES[1:]:
                self.ir_para(CASA)                       # D: lives next to your folders
            elif self.pasta != "/":
                self.ir_para(os.path.dirname(self.pasta.rstrip("/")) or "/")
            return True
        if tecla in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and self.bolhas:
            passo = -1 if (tecla == Gdk.KEY_ISO_Left_Tab or mods & Gdk.ModifierType.SHIFT_MASK) else 1
            self.sel = (self.sel + passo) % len(self.bolhas)
            self.navegou, self.mouse, self.foco = True, None, None
            self.animar()
            return True
        direcoes = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if tecla in direcoes:
            self.foco = None
            self.mover_selecao(*direcoes[tecla])
            self.animar()
            return True
        u = Gdk.keyval_to_unicode(tecla)
        if u and chr(u).isprintable() and not ctrl and len(self.consulta) < 40:
            self.consulta += chr(u)
            self.atualizar()
            return True
        return False

    def _mover(self, _c, x, y):
        super()._mover(_c, x, y)


# ---------- thumbnails ----------

def miniatura(path, tam, px=256):
    """A thumbnail pixbuf: the freedesktop cache (made by file managers) if there is one, else the
    image itself scaled down (skipped above 40 MB)."""
    uri = Gio.File.new_for_path(path).get_uri()
    md5 = hashlib.md5(uri.encode()).hexdigest()
    pastas = ("x-large", "large", "normal") if px > 256 else ("large", "normal", "x-large")
    for pasta in pastas:
        cand = os.path.join(os.path.expanduser("~/.cache/thumbnails"), pasta, md5 + ".png")
        if os.path.exists(cand) and px <= 512:
            try:
                return GdkPixbuf.Pixbuf.new_from_file(cand)
            except GLib.Error:
                break
    if tam > 40_000_000:
        return None
    try:
        return GdkPixbuf.Pixbuf.new_from_file_at_scale(path, px, px, True)
    except GLib.Error:
        return None


def pixbuf_superficie(pb):
    if pb is None:
        return None
    sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, pb.get_width(), pb.get_height())
    cr = cairo.Context(sup)
    Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
    cr.paint()
    return sup


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Orbit", flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.janela = None
        self.segura = False
        self.sair_fonte = None

    def do_activate(self):
        if self.janela is None:
            self.janela = Orbita(self)
        self.janela.alternar()

    def segurar(self):
        if self.sair_fonte:
            GLib.source_remove(self.sair_fonte)
            self.sair_fonte = None
        if not self.segura:
            self.hold()
            self.segura = True

    def soltar_depois(self):
        def sair():
            self.sair_fonte = None
            if not self.janela.get_visible():
                self.quit()                      # 10 min closed: give the RAM back
            return False
        if self.sair_fonte:
            GLib.source_remove(self.sair_fonte)
        self.sair_fonte = GLib.timeout_add_seconds(FICAR_ABERTO, sair)


if __name__ == "__main__":
    App().run(sys.argv[:1])
