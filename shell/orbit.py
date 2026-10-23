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

HOME (a primeira estrela do caminho, onde o ORBIT abre) não é uma pasta: é uma vista que se adapta ao uso.
- "frequent" (padrão): as pastas que você mais visita (frecência, visitas.py): maior e mais perto do meio =
  mais usada. ~ e o D: sempre aparecem; sem histórico ainda, as pastas padrão (Desktop, Downloads...) servem de
  semente e somem sozinhas conforme o uso de verdade toma o lugar.
- "computer": o "Meu computador" do jeito do ORBIT: uma bolha por disco (/, D:, pendrives...) com um anel de
  pontos aceso até onde ele está cheio (vermelho passando de 90%), as pastas padrão e a Lixeira.
- A pílula FREQUENT | COMPUTER no alto da HOME troca o modo (o mesmo do Settings → Default apps → File finder,
  gravado em ~/.config/aero/orbit.json). Backspace em ~ ou na raiz de um disco volta pra HOME; buscar na HOME
  procura em tudo (~ e D:).

Runs as its own small process (com.aero.Orbit) and quits by itself 10 minutes after closing.
"""
import functools
import hashlib
import heapq
import math
from array import array
from bisect import bisect_right
from collections import deque
import os
import random
import re
import stat
import subprocess
import sys
import threading
import shutil
import time
import types
import urllib.parse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402
from gi.repository import Gtk4LayerShell as Camada  # noqa: E402

import cairo  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import campotexto  # noqa: E402
import liquido  # noqa: E402
import visor  # noqa: E402
import visitas  # noqa: E402
import nuvem  # noqa: E402
from nuvem import FG, RED, empacotar, pop, suave  # noqa: E402

CASA = os.path.expanduser("~")
DADOS = os.environ.get("AERO_DADOS", "/mnt/d")          # the data drive (shown as "D:" next to your folders)
RAIZES = [CASA] + ([DADOS] if os.path.ismount(DADOS) else [])
LIXO = visitas.LIXO                                     # a Lixeira (as coisas apagadas ficam em .../Trash/files)
NOME_RAIZ = {CASA: "~", DADOS: "D:"}
# a HOME virtual: um "endereço" que não é caminho de verdade (não começa com /), então nada o confunde com uma
# pasta: os.scandir falha, as visitas ignoram, e cada lugar que precisa trata ele antes
INICIO = "aero:home"
MODOS_TELA = [("FREQUENT", "frequent"), ("COMPUTER", "computer")]


def exibir(path):
    """~/x for the home, D:/x for the data drive; HOME for the virtual home."""
    if path == INICIO:
        return "HOME"
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
# O disco pode demorar (HD dormindo, pendrive morrendo) e a rede pode nunca responder: perguntas ao disco que a
# HOME precisa rodam cada uma na sua thread (Sondas), e a HOME espera no máximo SONDA_PRAZO por elas
SONDA_PRAZO = 0.3                # s: depois disso a HOME aparece com o que já sabe; o resto chega depois
SONDA_VALE = 3.0                 # s: uma resposta do disco vale esse tempo (abrir/trocar de modo não repergunta)
HOME_MAX = 16                    # pastas da HOME "frequent" (fora ~ e D:)
# a busca numa pasta fora do índice (pendrive, o SYSTEM /): limites pra ela nunca demorar
VIVO_FUNDO, VIVO_TEMPO, VIVO_ENTRADAS, VIVO_ACHADOS = 4, 0.8, 40_000, 400
_relogio = time.monotonic        # o relógio de verdade pros prazos de I/O

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
                 "grupo", "variantes", "pontos", "rank", "parado", "extra")

    def __init__(self, path, eh_dir, mtime, tam, grupo=0):
        self.extra = None                          # bolhas da HOME: como mostrar (raio, anel, rótulo, uso do disco)
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

SETOR = {"grupo": 180, "dir": 180, "disco": 180, "other": -132, "arc": -132, "img": -55, "doc": 0, "code": 58, "aud": 122, "vid": 122}
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

    def na_janela(mtime):
        idade = agora - mtime
        return not ((anel[0] > 0 and idade > lo) or (anel[1] < ANEL_N and idade < hi))

    def passa(eh_dir, nome, mtime):
        if p["filtro"] and tipo_de(nome, eh_dir) != p["filtro"]:
            return False
        return na_janela(mtime)

    q = p["consulta"].strip().casefold()
    cands = []
    inicio = p["pasta"] == INICIO
    # os pontos de montagem (procfs: sem encostar em disco). Vão pra thread principal (eh_raiz: um disco
    # inteiro nunca é copiado como arquivo) e pra HOME / a busca ao vivo (não entrar em disco de rede)
    p["montagens"] = visitas.montagens()
    p.setdefault("saida", {})["montagens"] = visitas.montados(p["montagens"])
    if not q and inicio:
        if p["filtro"] in (None, "dir") and tuple(anel) == (0, ANEL_N):
            return itens_inicio(p)                          # a HOME de sempre
        # um tipo/uma idade escolhidos na HOME: o mais novo daquilo em tudo
        cands = recentes(itens, p["filtro"], None if tuple(anel) == (0, ANEL_N) else na_janela)
    elif not q:
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
    else:
        # da HOME a busca cobre todas as raízes do índice (~ e D:); de uma pasta, só o que está debaixo dela
        bases = tuple(r + "/" for r in RAIZES) if inicio else (p["pasta"].rstrip("/") + "/",)
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
        if not inicio and not indexada(p["pasta"]):
            # um pendrive, o SYSTEM (/)...: o índice não tem (ou só tem a parte da ~): procura de verdade aqui
            p["saida"]["nao_indexado"] = True
            ja = {c[1] for c in cands}
            cands += [c for c in busca_ao_vivo(q, p["pasta"], passa, p["montagens"], p.get("vivo"))
                      if c[1] not in ja]
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


def indexada(pasta):
    """A pasta está dentro do que o índice cobre (~ ou D:)? Só texto, sem I/O."""
    return any(pasta == r or pasta.startswith(r + "/") for r in RAIZES)


def busca_ao_vivo(q, pasta, passa, monts, vivo=None):
    """Busca de verdade numa pasta que o índice não cobre: anda pelas pastas em largura (as mais rasas
    primeiro), com limites de profundidade, tempo, entradas lidas e achados. Não entra em outro disco montado
    lá dentro (como o find -xdev: nada de /proc, /sys, nem um NFS que pode travar), menos pendrive/HD externo.
    Roda na thread de trabalho; vivo() = False: o texto mudou, esta busca não vale mais e para.
    Devolve [(nota, path, eh_dir, mtime, tam)], como a busca no índice."""
    agora, t0 = time.time(), _relogio()
    inicio = pasta.rstrip("/") or "/"
    base = inicio.rstrip("/") + "/"
    pular = {m for _d, m, fs in monts if m != inicio and (not m.startswith(visitas.REMOVIVEL) or visitas.remoto(fs))}
    fila, fortes, fracos, lidas = deque([(inicio, 0)]), [], [], 0
    while fila and lidas < VIVO_ENTRADAS and len(fortes) < VIVO_ACHADOS and _relogio() - t0 < VIVO_TEMPO:
        if vivo is not None and not vivo():
            break
        d, fundo = fila.popleft()
        try:
            with os.scandir(d) as it:
                for e in it:
                    lidas += 1
                    if e.name.startswith((".", "$")):
                        continue
                    try:
                        eh_dir, link = e.is_dir(), e.is_symlink()
                    except OSError:
                        continue
                    if eh_dir and not link and fundo < VIVO_FUNDO and e.name not in PULAR and e.path not in pular:
                        fila.append((e.path, fundo + 1))
                    rel = e.path[len(base):]
                    nome_cf = e.name.casefold()
                    nota = pontuar(q, nome_cf, rel.casefold())
                    if nota <= 0 or (nota <= 1.0 and len(fracos) >= 200):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if not passa(eh_dir, nome_cf, st.st_mtime):
                        continue
                    forte = nota > 1.0                       # 1.0 = só as letras em ordem (erro de digitação)
                    nota += 2 * math.exp(-(agora - st.st_mtime) / (30 * 86400)) - 0.15 * rel.count("/")
                    (fortes if forte else fracos).append((nota, e.path, eh_dir, st.st_mtime, st.st_size))
        except OSError:
            continue
    return fortes or fracos


def recentes(itens, filtro, na_janela=None):
    """Na HOME, sem nada digitado, com um tipo (IMAGES, DOCS...) ou uma idade (a régua MODIFIED) escolhidos:
    os arquivos mais novos daquele tipo em TUDO o que está no índice. O tipo é achado com UMA expressão regular
    sobre todos os nomes de uma vez (velocidade de C), não com um loop Python chamando tipo_de() 250 mil vezes.
    na_janela(mtime): a régua (None = qualquer idade)."""
    if len(itens) != 6 or not itens[0]:
        return []                                         # o índice ainda está sendo montado
    caminhos, mtimes, tams, dirs, texto, inicios = itens
    if filtro in TIPOS:
        exts = "|".join(sorted((re.escape(e) for e in TIPOS[filtro][0]), key=len, reverse=True))
        # ".ext" no fim de um nome ($ com re.M = antes de cada \n). O começo do nome sai pelo bisect.
        idx = (bisect_right(inicios, m.start()) - 1 for m in re.finditer(r"\.(?:%s)$" % exts, texto, re.M))
        bons = (i for i in idx if not dirs[i] and (na_janela is None or na_janela(mtimes[i])))
    else:
        quer_dir = filtro == "dir"
        bons = (i for i in range(len(caminhos)) if (not quer_dir or dirs[i])
                and (na_janela is None or na_janela(mtimes[i])))
    return [(mtimes[i], caminhos[i], bool(dirs[i]), mtimes[i], tams[i])
            for i in heapq.nlargest(300, bons, key=mtimes.__getitem__)]


def _nome_disco(ponto, removivel):
    if ponto == "/":
        return "SYSTEM", "/"
    if ponto == DADOS:
        return "D:", "D:"
    nome = os.path.basename(ponto.rstrip("/")) or ponto
    return nome, ("USB" if removivel else nome[:3].upper())


class Sondas:
    """Perguntas ao disco que podem demorar (stat, statvfs num HD que está acordando, um pendrive morrendo):
    cada uma roda na SUA thread, e quem pergunta espera no máximo `prazo` segundos por todas juntas. A que não
    respondeu a tempo continua sozinha (presa ela, não a HOME), não é disparada de novo enquanto roda, e quando
    finalmente responde chama `avisar` (a HOME se refaz com o dado de verdade). Respostas valem SONDA_VALE s."""

    def __init__(self):
        self.trava = threading.Lock()
        self.tab = {}            # chave -> {"res", "fim" (relógio; None = rodando), "esperam": [pedido], "avisar": set}

    def perguntar(self, pedidos, prazo, avisar=None):
        """pedidos: {chave: função sem argumentos}. Devolve {chave: resposta} das que responderam (as outras
        ficam de fora: quem chamou mostra o que já sabe delas)."""
        agora = _relogio()
        pedido = {"faltam": set(), "evento": threading.Event()}
        novas = []
        with self.trava:
            for k in [k for k, e in self.tab.items() if e["fim"] is not None and agora - e["fim"] > 60]:
                del self.tab[k]                               # respostas velhas saem: a tabela fica pequena
            for chave, func in pedidos.items():
                e = self.tab.get(chave)
                if e is not None and e["fim"] is not None and agora - e["fim"] < SONDA_VALE:
                    continue                                  # resposta fresca: vale
                if e is None or e["fim"] is not None:         # nunca perguntada, ou velha: pergunta de novo
                    e = self.tab[chave] = {"res": None, "fim": None, "esperam": [], "avisar": set()}
                    novas.append((chave, func))
                e["esperam"].append(pedido)                   # (rodando: só espera pela mesma thread)
                pedido["faltam"].add(chave)
            if not pedido["faltam"]:
                pedido["evento"].set()
        for chave, func in novas:
            threading.Thread(target=self._rodar, args=(chave, func), daemon=True).start()
        pedido["evento"].wait(prazo)
        with self.trava:
            for chave in pedido["faltam"]:                    # desistiu destas: avisa quando chegarem
                e = self.tab.get(chave)
                if e is not None:
                    e["esperam"] = [q for q in e["esperam"] if q is not pedido]
                    if avisar is not None:
                        e["avisar"].add(avisar)               # (um método do ORBIT: o set não repete)
            return {k: self.tab[k]["res"] for k in pedidos if k in self.tab and self.tab[k]["fim"] is not None}

    def _rodar(self, chave, func):
        try:
            res = func()
        except Exception:                                     # noqa: BLE001 (uma sonda nunca derruba nada)
            res = None
        with self.trava:
            e = self.tab[chave]
            e["res"], e["fim"] = res, _relogio()
            for pedido in e["esperam"]:
                pedido["faltam"].discard(chave)
                if not pedido["faltam"]:
                    pedido["evento"].set()
            avisar, e["esperam"], e["avisar"] = list(e["avisar"]), [], set()
        for f in avisar:
            f()


SONDAS = Sondas()


def _sonda_pasta(path, montagem, monts):
    """(numa thread de sonda) Essa pasta ainda existe?
    ("dir", mtime) | ("sumiu",) apagada de verdade: esquece | ("fora",) o disco dela está desligado: não mostra,
    mas lembra | ("outro",) sem permissão, erro de disco...: não mostra e não esquece."""
    try:
        st = os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return ("fora",) if _fora_do_ar(path, montagem, monts) else ("sumiu",)
    except OSError:
        return ("outro",)
    if not stat.S_ISDIR(st.st_mode):
        return ("sumiu",)                                     # virou um arquivo: a pasta de antes não existe
    return ("dir", st.st_mtime)


def _sonda_disco(ponto):
    """(numa thread de sonda) (espaço usado, mtime) de um disco: o statvfs é o que acorda um HD dormindo."""
    try:
        mt = os.stat(ponto).st_mtime
    except OSError:
        mt = None
    return visitas.espaco(ponto), mt


def _fora_do_ar(path, montagem=None, monts=None, fstab=None):
    """A pasta sumiu só porque o disco dela não está ligado agora? Então não esquece. (Roda numa thread de
    sonda: encosta no disco.)
    1. pendrive / HD externo (/run/media, /media): o ponto de montagem some junto com o disco;
    2. `montagem` (onde ela foi vista existindo, anotada nas visitas) não está montada agora: disco desligado;
    3. visita antiga, sem anotação: ela ou uma pasta acima é ponto de montagem do /etc/fstab (ou o D:) que não
       está montado agora (um /mnt/e ou /mnt/nas com nofail / automount);
    4. senão: a pasta mais próxima acima dela que existe está no MESMO aparelho (st_dev) da montagem dela? Então
       ela foi apagada de verdade: esquece. Num aparelho diferente, algo está fora do lugar: não esquece."""
    if path.startswith(visitas.REMOVIVEL):
        return True
    monts = visitas.montagens() if monts is None else monts
    ligados = visitas.montados(monts)
    if montagem and montagem != "/" and montagem not in ligados:
        return True
    acima, x = [], path
    while x not in ("/", ""):
        acima.append(x)
        x = os.path.dirname(x)
    if not montagem:
        fstab = visitas.pontos_fstab() if fstab is None else fstab
        if any((x in fstab or x == DADOS) and x not in ligados for x in acima):
            return True
        montagem = visitas.montagem_de(path, monts)[0]
    try:
        aparelho = os.stat(montagem).st_dev
    except OSError:
        return True                                           # nem a montagem responde: melhor não esquecer
    for x in acima[1:] + ["/"]:
        try:
            return os.stat(x).st_dev != aparelho
        except OSError:
            continue
    return True


def itens_inicio(p):
    """A HOME virtual (roda na thread de trabalho: nada aqui encosta no desenho).
    - frequent: as pastas de maior frecência (raio e anel pela nota: maior e mais perto = mais usada), com ~ e D:
      sempre presentes; com pouco histórico, as pastas padrão entram como sementes pequenas.
    - computer: cada disco de verdade no primeiro anel (com o espaço usado), ~ / as pastas padrão / a Lixeira no
      segundo, cada uma do tamanho do quanto você usa (as posições ficam fixas: dá pra decorar onde está cada uma).
    Nada de esperar o disco sem limite: as perguntas (existe? espaço livre?) vão todas juntas pras Sondas, que
    esperam no máximo SONDA_PRAZO. Quem não respondeu aparece de memória ("lento") e a HOME se refaz quando a
    resposta chega (p["avisar"]); uma pasta num disco de rede/FUSE/automount nem é perguntada ("remoto").
    Cada item leva um 8º campo, o "extra" (raio, anel, nome, rótulo, uso), que orbitar() e desenhar_bolha() leem.
    p["saida"] devolve à thread principal os discos achados (pro caminho e o Backspace), os pontos de montagem,
    as pastas que sumiram e onde cada pasta foi vista (montagens_vistas)."""
    agora = time.time()
    vis = p.get("visitas") or {}
    notas = {k: visitas.nota(v, agora) for k, v in vis.items()}
    saida = p.setdefault("saida", {})
    monts = p.get("montagens")
    if monts is None:
        monts = visitas.montagens()
    ligados = visitas.montados(monts)
    lista_discos = visitas.discos(DADOS, monts)
    saida["discos"] = [d for d, _ in lista_discos]
    saida["montagens"] = ligados
    removiveis = {d for d, rem in lista_discos if rem}
    pontos_disco = {d for d, _ in lista_discos} | ligados
    vistas = saida.setdefault("montagens_vistas", {})
    resp = {}                                                 # (tipo, caminho) -> resposta da sonda

    def remota(path):
        return visitas.remoto(visitas.montagem_de(path, monts)[1])

    def eh_disco(path):                                       # a raiz de um disco: mostra como disco
        return path != CASA and path in pontos_disco and not remota(path)

    def chave(path):
        return ("disco" if eh_disco(path) else "pasta", path)

    def sondar(paths):
        pedidos = {}
        for c in paths:
            k = chave(c)
            if k in resp or k in pedidos or remota(c):
                continue
            pedidos[k] = (functools.partial(_sonda_disco, c) if k[0] == "disco" else
                          functools.partial(_sonda_pasta, c, (vis.get(c) or {}).get("m"), monts))
        if pedidos:
            resp.update(SONDAS.perguntar(pedidos, SONDA_PRAZO, p.get("avisar")))

    def estado(path):
        """"ok", "remota"/"lenta" (mostra de memória), ou o que a sonda disse ("sumiu", "fora", "outro")."""
        if remota(path):
            return "remota"
        r = resp.get(chave(path))
        if r is None:
            return "lenta"
        return "ok" if eh_disco(path) or r[0] == "dir" else r[0]

    def bolha(path, r, anel, nome=None):
        est = estado(path)
        if eh_disco(path):
            esp, mt = resp.get(chave(path)) or (None, None)
            nome_d, glifo = _nome_disco(path, path in removiveis)
            extra = {"r": r, "nivel": anel, "nome": nome_d, "glifo": glifo, "uso": esp, "rotulo": nome_d}
            if est == "lenta":
                extra["lento"] = True
            if esp:
                usado, total, livre = esp
                extra["uso_txt"] = visitas.uso_txt(usado, total)
                extra["rotulo"] = f"{nome_d} · {extra['uso_txt']}"
                extra["livre_txt"] = visitas.livre_txt(livre)
                extra["frac"] = usado / max(1, usado + livre)        # como o df: o reservado do root não conta
            return [path, True, mt or agora, 0, 0, 0, "disco", extra]
        if est not in ("ok", "remota", "lenta"):
            return None
        v = vis.get(path) or {}
        # ~ vira bolha com o nome da pasta (o seu usuário: um "~" solto some de tão pequeno); a estrela do caminho é "~"
        extra = {"r": r, "nivel": anel, "nome": nome or os.path.basename(path) or NOME_RAIZ.get(path),
                 "n": v.get("n", 0.0), "t": v.get("t", 0.0)}
        if est == "ok":
            mt = resp[chave(path)][1]
            m = visitas.montagem_de(path, monts)[0]
            if path in vis and v.get("m") != m:
                vistas[path] = m                              # onde ela existe (pra não esquecer à toa depois)
        else:
            extra["remoto" if est == "remota" else "lento"] = True
            mt = v.get("t") or agora
        return [path, True, mt, 0, 0, 0, "dir", extra]

    itens = []
    if p.get("modo") == "computer":
        lugares = [CASA] + visitas.lugares(checar=False) + [LIXO]
        sondar([d for d, _ in lista_discos] + lugares)
        for ponto, rem in lista_discos:
            itens.append(bolha(ponto, 50 if rem else 60, 0))
        if estado(LIXO) != "ok":
            lugares.remove(LIXO)                              # ainda sem Lixeira (nada apagado): fica de fora
        smax = max([notas.get(c, 0.0) for c in lugares] + [1.0])
        for c in lugares:
            r = 34 + 14 * math.sqrt(max(0.0, notas.get(c, 0.0) / smax))
            itens.append(bolha(c, r, 1, "Trash" if c == LIXO else None))
        return [it for it in itens if it is not None][:MAX_BOLHAS]

    fixos = [CASA] + ([DADOS] if DADOS in ligados else [])
    sementes = visitas.lugares(checar=False)
    fila = [c for c, _ in sorted(notas.items(), key=lambda kv: -kv[1]) if c not in fixos]
    lote, t0 = HOME_MAX + 8, _relogio()
    sondar(fixos + sementes + fila[:lote])                    # tudo de uma vez: o normal é responder em ms
    escolhidas, sumidas, i = [], [], 0
    while len(escolhidas) < HOME_MAX and i < len(fila):
        if i and i % lote == 0:                               # muitas sumiram: pergunta pelas próximas
            if _relogio() - t0 > 1.0:
                break                                         # (sem ficar nisso pra sempre: o resto fica pra próxima)
            sondar(fila[i:i + lote])
        c = fila[i]
        i += 1
        est = estado(c)
        if est in ("ok", "remota", "lenta"):
            escolhidas.append((c, notas[c]))
        elif est == "sumiu":
            sumidas.append(c)                                 # apagada/renomeada: esquece
    saida["sumidos"] = sumidas
    if len(escolhidas) < 6:
        # pouco histórico: as pastas padrão entram como sementes, com nota minúscula (ficam pequenas e por fora,
        # e vão sendo empurradas pra fora da HOME pelas pastas que você usa de verdade)
        for k, c in enumerate(sementes):
            if c not in fixos and all(c != e for e, _ in escolhidas) and estado(c) in ("ok", "remota", "lenta"):
                escolhidas.append((c, 0.01 * (6 - k)))
    # ~ e D: são âncoras: nota mínima 0,1 (acima das sementes), e crescem com o uso como as outras
    todas = escolhidas + [(f, max(0.1, notas.get(f, 0.0))) for f in fixos]
    todas.sort(key=lambda t: -t[1])
    smax = max([s for _, s in todas] + [1.0])
    # nomes iguais (dois "src") ganham a pasta de cima na frente: "aero-os/src"
    base = [os.path.basename(c) for c, _ in todas]
    for k, (c, s) in enumerate(todas):
        r = 30 + 36 * math.sqrt(max(0.0, s / smax))           # (max: nem um valor estranho vira raiz negativa)
        if c in fixos:
            r = max(r, 44.0)
        nome = None
        if base.count(base[k]) > 1 and c not in fixos:
            nome = os.path.basename(os.path.dirname(c)) + "/" + base[k]
        itens.append(bolha(c, r, nivel(k), nome))
    return [it for it in itens if it is not None][:MAX_BOLHAS]


CENTROS = {}                     # slice centre of each kind in the last layout (for the slice names)


def orbitar(itens, w, h):
    """Rings by rank (the best right next to the search), a slice of the orbit per kind (images up-right,
    docs right, code down-right, media down-left, folders left), then relaxation so nothing overlaps.
    Returns [(x, y, r)] around (0, 0)."""
    n = len(itens)
    # as bolhas da HOME trazem o próprio raio e anel no "extra" (pela nota de uso); as outras, pelo rank
    extras = [it[7] if len(it) > 7 else None for it in itens]
    raios = [e["r"] if e and "r" in e else raio_rank(k) for k, e in enumerate(extras)]
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
        nv = extras[k]["nivel"] if extras[k] and "nivel" in extras[k] else nivel(k)
        fatias.setdefault((nv, SETOR[it[6]]), []).append(k)
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
    lx, ly = w / 2 - 60, h / 2 - 175          # leaves room for the MODIFIED ruler and the chips
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
        # the liquid layer (GPU): bubbles are drops that pour in, join with necks, and the halftone around
        # them. Content (thumbnails, text) is drawn on top by cairo. Without GL it stays off (old drawing).
        self.liquido = liquido.CampoLiquido()
        raiz.add_overlay(self.liquido)
        # the media viewer paints inside its liquid window (visor.py); input still goes through self.area
        self.visor = visor.Visor()
        self.visor.ao_mudar = self._visor_mudou
        raiz.add_overlay(self.visor)
        raiz.add_overlay(self.area)
        self.vis = None
        self.editando = None
        self.selecionados = []                    # middle-click: files picked for Ctrl+C (kept across folders)
        rolar = Gtk.EventControllerScroll(flags=Gtk.EventControllerScrollFlags.VERTICAL)
        rolar.connect("scroll", self._rolar)
        self.area.add_controller(rolar)
        self.set_child(raiz)
        self.indice = Indice()
        self.pasta = INICIO
        # o que o ORBIT aprende: as pastas visitadas (visitas.py). _aqui = [pasta, quando entrou, abriu algo?]:
        # a visita só é contada ao sair dela, pra saber se você ficou ou só passou
        self.visitas = visitas.Visitas()
        self._aqui = None
        self.modo = visitas.ler_modo()
        self.discos = []                         # pontos de montagem achados na última HOME (caminho, Backspace)
        self.montagens = set()                   # todos os pontos montados, achados pela thread de trabalho (eh_raiz)
        # o modo da HOME é gravado por UMA thread, sempre o último pedido (_gravar_modo)
        self._modo_trava, self._modo_pedido, self._modo_gravando = threading.Lock(), None, False
        self._peek_token = None                  # o PEEK pedido por último (uma resposta velha não entra)
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

    def atualizar(self, imediato=False, manter=False):
        """Something changed (text, folder, filters, index ready): the search, the layout and the filler are
        worked out in a thread (debounced while typing); only the newest answer is applied.
        manter=True: é a mesma vista refeita (um disco lento respondeu): a seleção fica onde estava."""
        self._manter = manter
        self.geracao = getattr(self, "geracao", 0) + 1
        if getattr(self, "_agendado", None):
            GLib.source_remove(self._agendado)
        g = self.geracao
        self._agendado = GLib.timeout_add(0 if imediato else 60, lambda: self._disparar(g))

    def _disparar(self, g):
        self._agendado = None
        p = {"consulta": self.consulta, "pasta": self.pasta, "filtro": self.filtro, "anel": list(self.anel),
             "modo": self.modo, "saida": {"manter": getattr(self, "_manter", False)},
             "vivo": lambda: g == self.geracao,      # a busca ao vivo para quando já tem uma resposta mais nova
             "avisar": self._sonda_chegou}           # um disco lento respondeu depois: refaz a HOME
        if self.pasta == INICIO:
            p["visitas"] = self.visitas.copia()      # uma cópia: a thread lê sem disputar com quem registra
        # the real size; before the first show the area measures 0 and the default size may be -1
        w = next((v for v in (self.area.get_width(), self.get_default_size()[0]) if v > 200), 1920)
        h = next((v for v in (self.area.get_height(), self.get_default_size()[1]) if v > 200), 1080)
        itens, voo = self.indice.itens, getattr(self, "_abrindo", False)

        def trabalho():
            res = buscar(p, itens)
            xyr = orbitar(res, w, h)
            GLib.idle_add(self._aplicar, g, res, xyr, semear(xyr, voo), p["saida"])
        threading.Thread(target=trabalho, daemon=True).start()
        return False

    def _aplicar(self, g, res, xyr, sementes, saida=None):
        if saida:                                    # vale mesmo se esta resposta ficou velha
            self.discos = saida.get("discos", self.discos)
            self.montagens = saida.get("montagens", self.montagens)
            self.visitas.esquecer(saida.get("sumidos", ()))
            if saida.get("montagens_vistas"):
                self.visitas.anotar_montagens(saida["montagens_vistas"])
        if g != self.geracao:
            return False
        # a mesma vista refeita: a seleção do teclado e o foco continuam na mesma bolha (se ela ainda existe)
        manter = bool(saida and saida.get("manter"))
        sel_antes = self.bolhas[self.sel].path if manter and 0 <= self.sel < len(self.bolhas) else None
        foco_antes = (self.bolhas[self.foco].path if manter and self.foco is not None
                      and 0 <= self.foco < len(self.bolhas) else None)
        velhas = {(b.path, b.grupo > 0): b for b in self.bolhas}
        agora, novas = time.monotonic(), []
        abrindo = getattr(self, "_abrindo", False)
        for k, item in enumerate(res):
            path, eh_dir, mtime, tam, grupo, variantes, tipo = item[:7]
            extra = item[7] if len(item) > 7 else None
            chave = (path, grupo > 0)
            b = velhas.get(chave) or Arquivo(path, eh_dir, mtime, tam, grupo)
            b.grupo, b.variantes, b.rank, b.extra = grupo, variantes, k, extra
            b.mtime, b.tam = mtime, tam
            # a mesma pasta pode ser bolha da HOME (disco, nome "~") e depois resultado de busca: refeito sempre
            b.tipo = "grupo" if grupo else tipo
            b.nome = (extra or {}).get("nome") or NOME_RAIZ.get(path) or os.path.basename(path) or path
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
        if manter:
            caminhos = [b.path for b in novas]
            if sel_antes in caminhos:
                self.sel = caminhos.index(sel_antes)
            if foco_antes in caminhos:
                self.foco = caminhos.index(foco_antes)
        t_rel = agora - self.t_abrir
        for e in sementes:
            e[9] += t_rel
        self.enfeites, self._t_fisica = sementes, agora
        self._abrindo = False
        self.pedir_extras()
        self.animar()
        return False

    def _sonda_chegou(self):
        """(numa thread de sonda) um disco que demorou finalmente respondeu: a HOME se refaz com o dado de verdade."""
        GLib.idle_add(self._refazer_home)

    def _refazer_home(self):
        if self.get_visible() and self.t_fechar is None and self.pasta == INICIO and not self.consulta:
            self.atualizar(manter=True)
        return False

    def pedir_extras(self):
        """Thumbnails and folder sizes, loaded in a thread and handed back to the drawing.
        Uma pasta de disco de rede (remoto) ou que ainda não respondeu (lento) não é aberta pra contar."""
        faltam = [b for b in self.bolhas if not b.pedido and b.tipo in ("img", "dir")
                  and not (b.extra or {}).get("remoto") and not (b.extra or {}).get("lento")]
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
        """Go to a folder (or INICIO, the virtual home). manter=True keeps the search (a group bubble: its
        results, inside it). Toda pasta mostrada vira uma visita (contada quando você sair dela)."""
        self._sair_da_pasta()
        self.pasta = pasta
        if pasta != INICIO:
            self._aqui = [pasta, time.monotonic(), False]
        if not manter:
            self.consulta = ""
            self.campo.texto, self.campo.cursor, self.campo.tudo = "", 0, False
        self.atualizar(imediato=True)

    def _sair_da_pasta(self):
        """Saindo de uma pasta: agora ela é contada. Visita inteira se você ficou um pouco ou abriu algo nela;
        uma pequena (visitas.PASSOU) se só passou a caminho de outra."""
        aqui, self._aqui = self._aqui, None
        if aqui:
            pasta, t0, abriu = aqui
            ficou = abriu or time.monotonic() - t0 >= visitas.FICOU
            self.visitas.registrar(pasta, 1.0 if ficou else visitas.PASSOU)

    def _abriu_arquivo(self, path):
        """Um arquivo aberto pelo ORBIT (até de uma busca): a pasta dele é das "suas", ponto na hora."""
        pasta = os.path.dirname(path)
        self.visitas.registrar(pasta)
        if self._aqui is not None and self._aqui[0] == pasta:
            self._aqui[2] = True                          # e a estadia aqui conta inteira

    def abrir_item(self, b):
        if b.tipo == "grupo":
            self.ir_para(b.path, manter=True)
            return
        if b.tipo in ("dir", "disco"):
            self.ir_para(b.path)
            return
        if visor.tipo_midia(b.path):
            self.abrir_visor(b)                            # pictures, GIFs, videos, sound: the built-in viewer
            return
        self.abrir_fora(b)

    def abrir_fora(self, b):
        self._abriu_arquivo(b.path)
        self.fechar(lambda: subprocess.Popen(["gio", "open", b.path], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL, start_new_session=True))

    # ---------- the media viewer: a liquid window born from the bubble ----------

    def abrir_visor(self, b):
        self.peek = None
        self._abriu_arquivo(b.path)
        # os vizinhos (← →) vêm de uma leitura da pasta: numa thread; até lá, só este
        v = self.vis = {"b": b, "itens": [b.path], "i": 0, "t0": time.monotonic(), "fechando": None, "atual": None,
                        "tams": {}}
        self.visor.carregar(b.path)
        self.mouse_v = self.mouse
        self.animar()

        def trabalho():
            itens = visor.irmaos(b.path)
            GLib.idle_add(self._irmaos_prontos, v, itens)
        threading.Thread(target=trabalho, daemon=True).start()

    def _irmaos_prontos(self, v, itens):
        if self.vis is not v:
            return False                                   # já fechou (ou abriu outro)
        atual = v["itens"][v["i"]]
        itens = list(itens)
        if atual not in itens:
            itens.insert(0, atual)
        v["itens"], v["i"] = itens, itens.index(atual)
        self.area.queue_draw()
        return False

    def _tam_pronto(self, v, path, n):
        if self.vis is v:
            v["tams"][path] = n
            self.area.queue_draw()
        return False

    def fechar_visor(self):
        if self.vis is not None and self.vis["fechando"] is None:
            self.vis["fechando"] = time.monotonic()
            self.visor.parar()
            self.animar()

    def navegar(self, passo):
        v = self.vis
        if v is None or len(v["itens"]) < 2:
            return
        v["i"] = (v["i"] + passo) % len(v["itens"])
        self.visor.carregar(v["itens"][v["i"]])
        self.animar()

    def _visor_mudou(self, oque):
        if oque == "tamanho":
            self.animar()
        else:
            self.area.queue_draw()

    def visor_m(self, agora):
        """0 = still the bubble, 1 = the full window."""
        v = self.vis
        if v is None:
            return 0.0
        if v["fechando"] is not None:
            t = min(1.0, (agora - v["fechando"]) / 0.36)
            return 1 - (1 - (1 - t) ** 3)
        t = min(1.0, (agora - v["t0"]) / 0.45)
        return 1 - (1 - t) ** 3

    def visor_alvo(self):
        """Where the window wants to be: the whole picture, fitted (any shape), a bit above the middle."""
        w, h = self.area.get_width() or 1920, self.area.get_height() or 1080
        cx, cy = self._centro()
        tam = self.visor.tamanho or (420, 420)
        s = min(w * 0.84 / tam[0], (h - 300) / tam[1])
        return [cx, cy - 34, tam[0] * s / 2, tam[1] * s / 2]

    def visor_janela(self, agora):
        """This frame's window (cx, cy, half_w, half_h, corner), morphing from the bubble; None when gone."""
        v = self.vis
        if v is None:
            return None
        alvo = self.visor_alvo()
        if v["atual"] is None:
            v["atual"] = list(alvo)
        mexeu = False
        for k in range(4):                                  # a soft spring: changing pictures reshapes it
            dif = alvo[k] - v["atual"][k]
            if abs(dif) > 0.3:
                v["atual"][k] += dif * 0.22
                mexeu = True
        v["mexendo"] = mexeu
        m = self.visor_m(agora)
        bx, by, br, _a = self._onde(v["b"], agora)
        br = max(br, 8.0)
        cx = bx + (v["atual"][0] - bx) * m
        cy = by + (v["atual"][1] - by) * m
        hw = br + (v["atual"][2] - br) * m
        hh = br + (v["atual"][3] - br) * m
        canto = min(hw, hh, br + (24 - br) * m)
        if v["fechando"] is not None and m <= 0.01:
            self.vis, self.visor.janela = None, None
            self.visor.queue_draw()
            return None
        self.visor.janela = (cx, cy, hw, hh, canto)
        self.visor.alfa = min(1.0, max(0.0, (m - 0.7) / 0.3)) if self.visor.tamanho else 0.0
        self.visor.queue_draw()
        return (cx, cy, hw, hh, canto)

    def desenhar_visor_ui(self, cr, w, h, agora):
        """Name, position in the folder, size, zoom, the playback dots and the side arrows (as drops)."""
        v, jan = self.vis, self.visor.janela
        if v is None or jan is None or self.visor_m(agora) < 0.9 or v["fechando"] is not None:
            return
        cx, cy, hw, hh, _c = jan
        path = v["itens"][v["i"]]
        nome = os.path.basename(path)
        nome = nome if len(nome) <= 48 else nome[:46] + "…"
        y = cy + hh + 30
        self._texto_cor(cr, nome, cx, y, "Inter Semi-Bold 14", FG, 1)
        partes = [f"{v['i'] + 1}/{len(v['itens'])}"]
        if self.visor.tamanho and self.visor.tipo != "som":
            partes.append(f"{self.visor.tamanho[0]}×{self.visor.tamanho[1]}")
        tams = v.setdefault("tams", {})
        if path not in tams:                               # o tamanho: medido numa thread, uma vez por arquivo
            tams[path] = None

            def medir():
                try:
                    n = os.path.getsize(path)
                except OSError:
                    n = None
                GLib.idle_add(self._tam_pronto, v, path, n)
            threading.Thread(target=medir, daemon=True).start()
        elif tams[path] is not None:
            partes.append(tamanho_txt(tams[path]))
        if self.visor.zoom > 1.001:
            partes.append(f"{self.visor.zoom * 100:.0f}%")
        self._texto_cor(cr, "  ·  ".join(partes), cx, y + 22, "Doto Black 11", FG, 0.5)
        if self.visor.tipo == "som":
            self._texto_cor(cr, "♪", cx, cy - 10, f"Doto Black {int(min(hw, hh) * 0.5)}", (0.79, 0.65, 0.92), 0.9)
        if self.visor.erro:
            msg = "THIS NEEDS THE VIDEO/SOUND CODECS (GSTREAMER)" if self.visor.tipo in ("vid", "som") else "CAN'T OPEN THIS FILE"
            self._texto_cor(cr, msg, cx, cy, "Doto Black 13", RED, 0.9)
        prog = self.visor.progresso()
        if prog is not None:                               # playback: a row of dots, lit up to where it is
            frac, seg, tot = prog
            n, larg = 48, min(2 * hw, 560)
            x0, yb = cx - larg / 2, y + 48
            for k in range(n):
                px = x0 + larg * k / (n - 1)
                aceso = k / (n - 1) <= frac
                cr.new_sub_path()
                cr.arc(px, yb, 3.2 if aceso else 2.2, 0, 2 * math.pi)
                cr.set_source_rgba(*(RED if aceso and abs(k / (n - 1) - frac) < 1.5 / n else FG), 0.95 if aceso else 0.22)
                cr.fill()
                self.zonas.append(("busca", k / (n - 1), px, yb, larg / n))
            def mmss(x):
                return f"{int(x // 60)}:{int(x % 60):02d}"
            self._texto_cor(cr, f"{mmss(seg)} / {mmss(tot)}" + ("" if self.visor.tocando() else "  ·  PAUSED"),
                            cx, yb + 20, "Doto Black 10", FG, 0.45)
        if len(v["itens"]) > 1:                             # side arrows: little drops next to the window
            for passo, sx in ((-1, cx - hw - 46), (1, cx + hw + 46)):
                self._texto_cor(cr, "‹" if passo < 0 else "›", sx, cy - 3, "Doto Black 22", FG, 0.85)
                self.zonas.append(("nav", passo, sx, cy, 24))
        self._texto_cor(cr, "← → NEXT · WHEEL ZOOM · DRAG MOVE · SPACE PLAY · ESC BACK", cx, h - 40,
                        "Doto Black 10", FG, 0.35)

    def acao(self, nome, b):
        if nome == "OPEN":
            if b.tipo in ("dir", "grupo", "disco"):
                self.abrir_item(b)
            else:
                self.abrir_fora(b)                         # OPEN = its own app; click/Enter = the viewer
        elif nome == "SHOW":
            pasta = b.path if b.tipo == "disco" else os.path.dirname(b.path)    # um disco: ele mesmo
            self.visitas.registrar(pasta)
            self.fechar(lambda: subprocess.Popen(["gio", "open", pasta], stdout=subprocess.DEVNULL,
                                                 stderr=subprocess.DEVNULL, start_new_session=True))
        elif nome == "COPY":
            subprocess.Popen(["wl-copy", b.path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.aviso = ("PATH COPIED", time.monotonic())
            self.animar()
        elif nome == "PEEK":
            self.espiar(b)

    def espiar(self, b):
        """PEEK: a gota grande aparece NA HORA, com "…" (ou a miniatura que a bolha já tem), e o conteúdo é lido
        numa thread (um HD dormindo ou um pendrive lento nunca congelam a tela) e entra quando chega."""
        if visor.tipo_midia(b.path) and b.tipo not in ("dir", "grupo", "disco"):
            self.abrir_visor(b)
            return
        if self.peek is not None and self.peek[0] is b:
            self.peek = None
            self._peek_token = None
            self.animar()
            return
        lido = conteudo_peek_lido(b)
        provisorio = b.sup if b.tipo == "img" else (["…"] if lido else None)
        self.peek = (b, provisorio, time.monotonic())
        token = self._peek_token = object()
        if lido:
            def trabalho():
                conteudo = conteudo_peek(b.path, b.tipo, b.tam)
                GLib.idle_add(self._peek_pronto, b, conteudo, token)
            threading.Thread(target=trabalho, daemon=True).start()
        self.animar()

    def _peek_pronto(self, b, conteudo, token):
        if self.peek is None or self.peek[0] is not b or token is not self._peek_token:
            return False                                   # fechou, ou já é outro PEEK
        if isinstance(conteudo, GdkPixbuf.Pixbuf):
            conteudo = pixbuf_superficie(conteudo)
        elif conteudo is None and b.tipo == "img":
            conteudo = self.peek[1]                        # não deu pra ler grande: fica a miniatura
        self.peek = (b, conteudo, self.peek[2])
        self.area.queue_draw()
        return False

    # ---------- open / close ----------

    def abrir(self):
        self.ajustar_ao_monitor()
        self._sair_da_pasta()
        # sempre abre na HOME, no modo gravado agora (o Settings pode ter mudado enquanto estava fechado); se
        # uma troca da pílula ainda está sendo gravada, o modo certo é o que está na memória
        with self._modo_trava:
            gravando = self._modo_gravando
        self.pasta = INICIO
        if not gravando:
            self.modo = visitas.ler_modo()
        self.consulta, self.peek, self.foco, self.filtro = "", None, None, None
        self.editando = None
        self.campo.texto, self.campo.cursor, self.campo.tudo = "", 0, False
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
        na_home_filtrando = self.pasta == INICIO and (self.filtro not in (None, "dir") or
                                                      tuple(self.anel) != (0, ANEL_N))
        if self.get_visible() and (self.consulta or na_home_filtrando):
            self.atualizar()
        self.area.queue_draw()

    def trocar_modo(self, modo):
        """A pílula FREQUENT | COMPUTER: grava (numa thread) e refaz a HOME. As bolhas que existem nos dois
        modos (~, D:, as pastas padrão) deslizam até o lugar novo; as outras escorrem pra dentro."""
        if modo == self.modo or modo not in visitas.MODOS:
            return
        self.modo = modo
        self._gravar_modo(modo)
        self.atualizar(imediato=True)
        self.animar()

    def _gravar_modo(self, modo):
        """Uma thread só grava, e sempre o pedido mais novo: antes era uma thread por clique, e duas terminando
        fora de ordem podiam deixar gravado o penúltimo modo. Agora o último clique sempre vence."""
        with self._modo_trava:
            self._modo_pedido = modo
            if self._modo_gravando:
                return                                     # a thread que está gravando pega este antes de sair
            self._modo_gravando = True
        threading.Thread(target=self._gravador_modo, daemon=True).start()

    def _gravador_modo(self):
        gravado = None
        while True:
            with self._modo_trava:
                modo = self._modo_pedido
                if modo == gravado:
                    self._modo_gravando = False
                    return
            visitas.gravar_modo(modo)
            gravado = modo

    def fechar(self, depois=None):
        self._sair_da_pasta()
        if self.vis is not None:
            self.visor.parar()
            self.vis, self.visor.janela = None, None
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
                    or (self.vis is not None and (agora - self.vis["t0"] < 0.6 or self.vis["fechando"] is not None
                                                  or self.vis.get("mexendo")))
                    or (getattr(self, "aviso", None) and agora - self.aviso[1] < 1.6)
                    or getattr(self, "_caixa_mexendo", False) or getattr(self, "_pilula_mexendo", False))
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
        """The "modified" ruler: a row of dots above the kind chips (x0, y, length)."""
        L = min(560, w * 0.4)
        return (w - L) / 2, h - 134, L

    def ponto_anel(self, i, w, h):
        x0, y, L = self.geo_anel(w, h)
        return x0 + L * i / ANEL_N, y

    @staticmethod
    def idade_rotulo(i):
        if i <= 0:
            return "ANY TIME"
        if i >= ANEL_N:
            return "NOW"
        s = idade_fracao(i / ANEL_N)
        for lim, div, u in ((3600 * 48, 3600, "H"), (86400 * 14, 86400, "D"), (86400 * 60, 7 * 86400, "W"),
                            (86400 * 365 * 2, 30 * 86400, "MO")):
            if s < lim:
                return f"{max(1, round(s / div))} {u} AGO"
        return f"{round(s / (365 * 86400))} Y AGO"

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
        if self.vis is None:
            self.desenhar_anel(cr, w, h, fundo)
            self.desenhar_guias(cr, w, h, fundo)
        liq = self.liquido.ok
        if self.bolhas and not liq:
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
        if self.vis is not None:
            foco = None                                     # the viewer has the attention
        self.gif_foco(self.bolhas[foco] if foco is not None and 0 <= foco < len(self.bolhas) else None)
        ordem = sorted(range(len(self.bolhas)), key=lambda i: (i == foco, self.bolhas[i].r))
        for i in ordem:
            self.desenhar_bolha(cr, self.bolhas[i], agora, i == foco)
        if foco is not None and 0 <= foco < len(self.bolhas) and self.t_fechar is None:
            self.desenhar_satelites(cr, self.bolhas[foco], agora)
        self.desenhar_centro(cr, w, h, fundo, agora)
        self.desenhar_modo(cr, w, h, fundo)
        if liq:
            self.formas_liquidas(agora, foco, fundo)
        self.desenhar_caminho(cr, w, h, fundo)
        if self.vis is None:                             # the viewer owns the screen: no chips under it
            self.desenhar_chips(cr, w, h, fundo)
        if self.peek is not None:
            self.desenhar_peek(cr, w, h, agora)
        if self.vis is not None:
            self.desenhar_visor_ui(cr, w, h, agora)

    def fonte(self):
        """The bubble the peek or the viewer came out of (the others melt away meanwhile)."""
        if self.vis is not None:
            return self.vis["b"]
        return self.peek[0] if self.peek is not None else None

    def peek_k(self, agora):
        """0..1: how far the peek drop / the viewer window has grown out of its bubble."""
        if self.vis is not None:
            return self.visor_m(agora)
        if self.peek is None:
            return 0.0
        t = min(1.0, (agora - self.peek[2]) / 0.42)
        return 1 - (1 - t) ** 3

    def formas_liquidas(self, agora, foco, fundo):
        """Hands the GPU layer this frame's drops: every bubble, the drops pouring into newborn ones, the
        satellites of the focused one, and the peek drop stretching out of its bubble."""
        formas, pk = [], self.peek_k(agora)
        cx, cy = self._centro()
        for i, b in enumerate(self.bolhas):
            x, y, r, a = self._onde(b, agora)
            if self.fonte() is not None and self.fonte() is not b:
                r *= 1 - 0.75 * pk                                  # the others melt away while peeking
            if r > 0.6:
                formas.append((x, y, r))
            formas += liquido.gotas_de_nascimento(x, y, b.r, agora - b.nasce, i + 1)
        if (foco is not None and 0 <= foco < len(self.bolhas) and self.t_fechar is None and self.peek is None
                and self.vis is None):
            formas += [(sx, sy, 23 * a) for _n, sx, sy, a in self.satelites(self.bolhas[foco], agora)]
        if self.peek is not None:
            bx, by, br, _a = self._onde(self.peek[0], agora)
            R = min(self.area.get_width(), self.area.get_height()) * 0.3
            formas.append((bx + (cx - bx) * pk, by + (cy - by) * pk, br + (R - br) * pk))
        janelas = [j for j in (getattr(self, "caixa_jan", None), getattr(self, "pilula_jan", None)) if j]
        jan = self.visor_janela(agora)
        if jan is not None:
            janelas.append(jan)
            if self.visor_m(agora) > 0.9 and len(self.vis["itens"]) > 1:      # the side arrows are drops
                formas += [(jan[0] - jan[2] - 46, jan[1], 20), (jan[0] + jan[2] + 46, jan[1], 20)]
        self.liquido.formas(formas, fundo, janelas)

    def desenhar_bolha(self, cr, b, agora, focada):
        x, y, r, a = self._onde(b, agora)
        if r < 0.6 or a <= 0.01:
            return
        liq = self.liquido.ok
        if liq:
            # the content shows up once the drops have joined: the pour hides the loading
            a *= min(1.0, max(0.0, (agora - b.nasce - 0.28) / 0.25))
            if self.fonte() is not None and self.fonte() is not b:
                r *= 1 - 0.75 * self.peek_k(agora)
                a *= 1 - 0.85 * self.peek_k(agora)
            elif self.vis is not None and self.vis["b"] is b:
                a *= 1 - self.visor_m(agora)                       # it became the window
            if a <= 0.01:
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
            if not liq:                                    # with the liquid layer, the GPU draws the drop
                cr.arc(x, y, r, 0, 2 * math.pi)
                cr.set_source_rgba(*FUNDO_BOLHA, 0.9 * a)
                cr.fill_preserve()
                cr.set_source_rgba(*FG, 0.12 * a)
                cr.set_line_width(1.2)
                cr.stroke()
            if b.tipo == "disco":
                self.medidor(cr, b, x, y, r, a)
            elif b.tipo in ("dir", "grupo"):
                self.aglomerado(cr, b, x, y, r, a)
                if b.tipo == "grupo":
                    self._texto_cor(cr, str(b.grupo), x, y, f"Doto Black {max(10, int(r * 0.5))}", FG, a)
            else:
                glifo, cor = self.glifo(b)
                self._texto_cor(cr, glifo, x, y, f"Doto Black {max(8, int(r * (0.42 if len(glifo) > 2 else 0.6)))}",
                                cor, a)
        if b.path in self.selecionados:                         # picked with the middle click
            cr.new_path()
            cr.arc(x, y, r + 5, 0, 2 * math.pi)
            cr.set_source_rgba(*RED, 0.9 * a)
            cr.set_line_width(2.4)
            cr.stroke()
        if b.variantes and r > 14:                              # same name, other formats: +N
            bx, by = x + r * 0.72, y - r * 0.72
            cr.new_path()
            cr.arc(bx, by, 10, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, 0.92 * a)
            cr.fill()
            self._texto_cor(cr, f"+{b.variantes}", bx, by, "Doto Black 9", (0.05, 0.05, 0.05), a)
        e = b.extra
        if e is not None and not focada:                        # HOME: todas têm nome (são poucas)
            nome = e.get("rotulo") or b.nome
            nome = nome if len(nome) <= 30 else nome[:28] + "…"
            self._texto_cor(cr, nome, x, y + r + 14, "Inter 10.5", FG, 0.78 * a)
        elif b.rank < 5 and not focada:                         # names only on the first ring (no pile-up)
            nome = b.nome if len(b.nome) <= 22 else b.nome[:20] + "…"
            if b.tipo == "grupo":
                nome = f"{b.grupo} in {nome}"
            self._texto_cor(cr, nome, x, y + r + 14, "Inter 10.5", FG, 0.72 * a)
        elif focada:
            nome = (e.get("rotulo") if e and b.tipo == "disco" else None) or b.nome
            nome = nome if len(nome) <= 34 else nome[:32] + "…"
            self._texto_cor(cr, nome, x, y + r + 16, "Inter Semi-Bold 12.5", FG, a)
            if b.tipo == "disco":
                frac = (e or {}).get("frac")
                sub = (f"{round(frac * 100)}% FULL · {e['livre_txt']} FREE" if frac is not None
                       else "DRIVE · CHECKING …" if (e or {}).get("lento") else "DRIVE · ENTER TO OPEN")
            elif e is not None:                                  # uma pasta da HOME: o que o ORBIT sabe dela
                partes = []
                if e.get("n"):
                    v = math.ceil(e["n"])
                    partes.append(f"{v} VISIT{'S' if v != 1 else ''} · {idade_txt(e['t'])}")
                if e.get("remoto"):
                    partes.append("REMOTE")                  # disco de rede/FUSE: mostrado de memória
                elif e.get("lento"):
                    partes.append("CHECKING …")              # o disco ainda não respondeu
                elif b.filhos is not None:
                    partes.append(f"{b.filhos} ITEMS")
                sub = " · ".join(partes) or "FOLDER"
            else:
                sub = (f"{b.grupo} RESULTS · ENTER TO OPEN" if b.tipo == "grupo" else
                       f"{b.filhos} ITEMS" if b.tipo == "dir" and b.filhos is not None else
                       "FOLDER" if b.tipo == "dir" else tamanho_txt(b.tam)) + " · " + idade_txt(b.mtime)
            self._texto_cor(cr, sub, x, y + r + 34, "Doto Black 10", FG, 0.5 * a)
            if e is not None and b.tipo != "disco" and b.path != CASA:     # onde fica (pastas fundas: nome curto)
                self._texto_cor(cr, exibir(b.path), x, y + r + 52, "Inter 10", FG, 0.4 * a)

    def medidor(self, cr, b, x, y, r, a):
        """Um disco: a letra dele no meio e um anel de pontos por dentro da borda, aceso no sentido do relógio a
        partir do topo até onde o disco está cheio (vermelho passando de 90%: quase sem espaço)."""
        e = b.extra or {}
        frac = e.get("frac")
        n = 44 if r > 40 else 32
        acesos = round((frac or 0.0) * n)
        raio, pr = r * 0.8, max(1.1, r * 0.034)
        for aceso in (False, True):                       # dois preenchimentos (um por cor), não um por ponto
            cr.new_path()
            for k in range(n):
                if (k < acesos) != aceso:
                    continue
                ang = -math.pi / 2 + 2 * math.pi * k / n
                cr.new_sub_path()
                cr.arc(x + raio * math.cos(ang), y + raio * math.sin(ang), pr if aceso else pr * 0.7, 0, 2 * math.pi)
            if aceso:
                cr.set_source_rgba(*(RED if (frac or 0) >= 0.9 else FG), 0.92 * a)
            else:
                cr.set_source_rgba(*FG, 0.2 * a)
            cr.fill()
        glifo = e.get("glifo", "•")
        self._texto_cor(cr, glifo, x, y, f"Doto Black {max(9, int(r * (0.5 if len(glifo) <= 2 else 0.34)))}", FG, a)

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
        if b.tipo == "disco":
            return (b.extra or {}).get("glifo", "•"), FG
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
            if self.liquido.ok and not sobre:              # the drop itself comes from the liquid layer
                cr.new_path()
                self._texto_cor(cr, nome, sx, sy, "Doto Black 10.5", FG, a)
                self.zonas.append(("acao", (nome, b), sx, sy, 23))
                continue
            cr.set_source_rgba(*((0.93, 0.92, 0.91) if sobre else FUNDO_BOLHA), 0.95 * a)
            cr.fill_preserve()
            cr.set_source_rgba(*FG, 0.16 * a)
            cr.set_line_width(1)
            cr.stroke()
            self._texto_cor(cr, nome, sx, sy, "Doto Black 10.5", (0.05, 0.05, 0.05) if sobre else FG, a)
            self.zonas.append(("acao", (nome, b), sx, sy, 23))

    def desenhar_centro(self, cr, w, h, fundo, agora):
        """No placeholder: the middle is empty until you type. Then a dot appears there and opens into a
        liquid text box (a window of the liquid layer) with the text, the cursor and the result count."""
        cx, cy = self._centro()
        fonte = "Doto Black 30"
        texto = self.consulta.upper()
        tw = self._texto_cor(cr, texto or " ", -9999, -9999, fonte, FG, 0) if texto else 0
        ligado = bool(self.consulta) and self.vis is None
        m, larg = getattr(self, "_caixa", (0.0, 70.0))
        m += ((1.0 if ligado else 0.0) - m) * 0.28
        larg += (max(70.0, tw / 2 + 36) - larg) * 0.35
        if not ligado and m < 0.02:
            m = 0.0
        self._caixa = (m, larg)
        self._caixa_mexendo = abs((1.0 if ligado else 0.0) - m) > 0.01
        if m <= 0.0:
            self.caixa_jan = None
        else:
            hw, hh = 7 + (larg - 7) * m, 7 + (31 - 7) * m
            self.caixa_jan = (cx, cy, hw, hh, hh)
            if not self.liquido.ok:
                cr.new_path()
                cr.arc(cx - hw + hh, cy, hh, math.pi / 2, 3 * math.pi / 2)
                cr.arc(cx + hw - hh, cy, hh, -math.pi / 2, math.pi / 2)
                cr.close_path()
                cr.set_source_rgba(*FUNDO_BOLHA, 0.95 * fundo)
                cr.fill()
            a_txt = min(1.0, max(0.0, (m - 0.55) / 0.45)) * fundo
            if texto and a_txt > 0:
                piscar = 1.0 if int(agora * 1.6) % 2 == 0 else 0.25
                campotexto.desenhar(cr, self.campo, cx, cy, fonte, a_txt, piscar=piscar)
                if self.t_fechar is None and not getattr(self, "_pisca", None):
                    def piscar_():                                  # one timer while there is text to blink
                        if self.consulta and self.t_fechar is None and self.get_visible():
                            self.area.queue_draw()
                            return True
                        self._pisca = None
                        return False
                    self._pisca = GLib.timeout_add(620, piscar_)
                if self.pasta != INICIO and not indexada(self.pasta):
                    info = "NOT INDEXED · SEARCHING THIS FOLDER"     # um pendrive, o /: busca de verdade aqui
                elif not self.indice.pronto:
                    info = "INDEXING …"
                else:
                    info = f"{len(self.bolhas)} FILES · {exibir(self.pasta)}"
                self._texto_cor(cr, info.upper(), cx, cy + hh + 18, "Doto Black 11", FG, 0.42 * a_txt)
        aviso = getattr(self, "aviso", None)
        if aviso and agora - aviso[1] < 1.6:
            self._texto_cor(cr, aviso[0], cx, cy + 66, "Doto Black 12", RED, 0.8 * fundo)
        elif self.selecionados and not ligado and self.vis is None:
            self._texto_cor(cr, f"{len(self.selecionados)} PICKED · CTRL+C COPY · ESC CLEAR", cx, cy,
                            "Doto Black 12", RED, 0.85 * fundo)

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
        """MODIFIED: a ruler of dots from "any time" (left) to "now" (right); drag the two red handles to
        keep only files changed in that window. Faint while unused, the chosen range written in words."""
        lo, hi = self.anel
        usado = (lo, hi) != (0, ANEL_N)
        x0, y, L = self.geo_anel(w, h)
        perto = self.mouse is not None and abs(self.mouse[1] - y) < 40 and x0 - 60 < self.mouse[0] < x0 + L + 60
        forca = (1.0 if usado or perto or self.arrasto in (0, 1) else 0.45) * fundo
        apagado, aceso = [], []
        for i in range(ANEL_N + 1):
            (aceso if lo <= i <= hi else apagado).append(self.ponto_anel(i, w, h))
        for pts, rr, al in ((apagado, 2.0, 0.18), (aceso, 2.8, 0.75)):
            cr.new_path()
            for x, yy in pts:
                cr.new_sub_path()
                cr.arc(x, yy, rr, 0, 2 * math.pi)
            cr.set_source_rgba(*FG, al * forca)
            cr.fill()
        for nome, idade in MARCAS:                             # faint ticks under the row
            self._texto_cor(cr, nome, x0 + L * fracao_idade(idade), y + 17, "Doto Black 9", FG, 0.3 * forca)
        mw = self._texto_cor(cr, "MODIFIED", -9999, -9999, "Doto Black 11", FG, 0)
        self._texto_cor(cr, "MODIFIED", x0 - 28 - mw / 2, y, "Doto Black 11", FG, 0.55 * forca)
        faixa = "ANY TIME" if not usado else f"{self.idade_rotulo(lo)} → {self.idade_rotulo(hi)}"
        fw = self._texto_cor(cr, faixa, -9999, -9999, "Doto Black 11", FG, 0)          # measure, then left-align
        self._texto_cor(cr, faixa, x0 + L + 28 + fw / 2, y, "Doto Black 11", RED if usado else FG, (0.9 if usado else 0.5) * forca)
        for k in (0, 1):
            x, yy = self.ponto_anel(self.anel[k], w, h)
            cr.new_path()
            cr.arc(x, yy, 6.5, 0, 2 * math.pi)
            cr.set_source_rgba(*RED, forca)
            cr.set_line_width(2.2)
            cr.stroke()
            self.zonas.append(("anel", k, x, yy, 16))

    # ---------- the virtual HOME: tops, the path from HOME, the mode pill ----------

    def eh_raiz(self, b):
        """Um disco inteiro, a ~ ou a Lixeira: nunca vão pra área de transferência como ARQUIVOS (colar copiaria
        o disco todo, ou uma pasta pra dentro dela mesma). Ctrl+C neles copia só o caminho.
        Vale do jeito que for alcançado: a bolha de disco da HOME, ou o mesmo /mnt/d entrando por / → mnt
        (aí ele é uma pasta "dir" comum). Raiz = qualquer ponto de montagem (os discos da HOME, o D:, os topos,
        e todos os que a thread de trabalho achou no /proc/mounts), e também uma pasta que CONTÉM um deles
        (/mnt, /home, /run/media/você): copiar ela levaria o disco junto. Só conjuntos em memória: sem I/O."""
        p = b.path.rstrip("/") or "/"
        if b.tipo == "disco" or p in (CASA, LIXO, "/", DADOS) or p in self.topos() or p in self.montagens:
            return True
        dentro = set(self.discos) | set(self.montagens) | {CASA, DADOS}
        return any(m.startswith(p + "/") for m in dentro)

    def topos(self):
        """Onde um caminho "começa" visto da HOME: ~, D:, a Lixeira e cada disco montado."""
        return [CASA, DADOS, LIXO] + self.discos

    def topo_de(self, pasta):
        """O topo mais comprido que contém a pasta (a Lixeira ganha de ~, um pendrive ganha de /); senão /."""
        melhor = "/"
        for t in self.topos():
            if (pasta == t or pasta.startswith(t.rstrip("/") + "/")) and len(t) > len(melhor):
                melhor = t
        return melhor

    def cadeia(self):
        """As estrelas do caminho: [(nome, pasta)] da HOME até aqui (a HOME e as 5 últimas, se for fundo)."""
        passos = [("HOME", INICIO)]
        if self.pasta == INICIO:
            return passos
        raiz = self.topo_de(self.pasta)
        nomes = {CASA: "~", DADOS: "D:", LIXO: "TRASH", "/": "/"}
        passos.append((nomes.get(raiz) or os.path.basename(raiz.rstrip("/")) or raiz, raiz))
        atual = raiz
        for parte in self.pasta[len(raiz):].split("/"):
            if parte:
                atual = os.path.join(atual, parte)
                passos.append((parte, atual))
        return passos if len(passos) <= 6 else passos[:1] + passos[-5:]

    def desenhar_modo(self, cr, w, h, fundo):
        """O botão da HOME: uma pílula líquida pequena no alto, FREQUENT | COMPUTER, com uma gota clara que
        desliza até o lado ligado. Só na HOME sem nada digitado (fora dela a pílula derrete até sumir)."""
        ligado = self.pasta == INICIO and not self.consulta and self.vis is None and self.peek is None
        m = getattr(self, "_pilula", 0.0)
        m += ((1.0 if ligado else 0.0) - m) * 0.25
        if abs((1.0 if ligado else 0.0) - m) < 0.01:
            m = 1.0 if ligado else 0.0
        fonte = "Doto Black 11"
        larg = [self._texto_cor(cr, nome, -9999, -9999, fonte, FG, 0) + 30 for nome, _ in MODOS_TELA]
        cx, cy, total = w / 2, 100, sum(larg)
        centros = [cx - total / 2 + larg[0] / 2, cx + total / 2 - larg[1] / 2]
        i = 1 if self.modo == "computer" else 0
        px = getattr(self, "_pil_x", None)
        px = centros[i] if px is None or m == 0.0 else px + (centros[i] - px) * 0.3
        if abs(centros[i] - px) < 0.3:
            px = centros[i]
        self._pilula, self._pil_x = m, px
        self._pilula_mexendo = m not in (0.0, 1.0) or px != centros[i]
        if m <= 0.0:
            self.pilula_jan = None
            return
        hw, hh = 6 + (total / 2 + 4 - 6) * m, 6 + (17 - 6) * m          # nasce de um ponto, como a caixa da busca
        self.pilula_jan = (cx, cy, hw, hh, hh)
        if not self.liquido.ok:
            cr.new_path()
            cr.arc(cx - hw + hh, cy, hh, math.pi / 2, 3 * math.pi / 2)
            cr.arc(cx + hw - hh, cy, hh, -math.pi / 2, math.pi / 2)
            cr.close_path()
            cr.set_source_rgba(*FUNDO_BOLHA, 0.95 * fundo)
            cr.fill()
        a = min(1.0, max(0.0, (m - 0.5) / 0.5)) * fundo
        if a <= 0.01:
            return
        t = (px - centros[0]) / ((centros[1] - centros[0]) or 1)       # 0 = FREQUENT, 1 = COMPUTER
        gw, gh = (larg[0] + (larg[1] - larg[0]) * t) / 2 - 4, 12.5
        cr.new_path()                                                    # a gota clara embaixo do modo ligado
        cr.arc(px - gw + gh, cy, gh, math.pi / 2, 3 * math.pi / 2)
        cr.arc(px + gw - gh, cy, gh, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(0.93, 0.92, 0.91, 0.95 * a)
        cr.fill()
        for k, (nome, modo) in enumerate(MODOS_TELA):
            sob = abs(px - centros[k]) < larg[k] / 4                    # a gota está em cima deste: letra escura
            self._texto_cor(cr, nome, centros[k], cy, fonte, (0.05, 0.05, 0.05) if sob else FG,
                            a * (1.0 if sob else 0.55))
            self.zonas.append(("modo", modo, centros[k], cy, larg[k] / 2))

    def desenhar_caminho(self, cr, w, h, fundo):
        """The path as a constellation: one star per folder, joined by a dotted line. The first star is HOME."""
        passos = self.cadeia()
        pts, x = [], 80
        for i, (nome, _alvo) in enumerate(passos):
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
            self.zonas.append(("pasta", passos[i][1], x, y - 8, 22))
        # the path as editable text (click / Ctrl+L) and a COPY drop next to it
        editando = getattr(self, "editando", None)
        texto = editando if editando is not None else exibir(self.pasta)
        fonte = "Inter 12.5"
        tw = self._texto_cor(cr, texto or " ", -9999, -9999, fonte, FG, 0)
        x0, y0, alt = 60, 136, 30
        larg = max(160, tw + 30)
        cr.new_path()
        cr.arc(x0 + alt / 2, y0, alt / 2, math.pi / 2, 3 * math.pi / 2)
        cr.arc(x0 + larg - alt / 2, y0, alt / 2, -math.pi / 2, math.pi / 2)
        cr.close_path()
        cr.set_source_rgba(*FUNDO_BOLHA, (0.95 if editando is not None else 0.6) * fundo)
        cr.fill_preserve()
        cr.set_source_rgba(*(RED if editando is not None else FG), (0.7 if editando is not None else 0.12) * fundo)
        cr.set_line_width(1.2)
        cr.stroke()
        self._texto_cor(cr, texto, x0 + 15 + tw / 2, y0, fonte, FG, (1 if editando is not None else 0.75) * fundo)
        if editando is not None:
            cr.rectangle(x0 + 17 + tw, y0 - 8, 2, 16)
            cr.set_source_rgba(*RED, fundo)
            cr.fill()
        self.zonas.append(("editar", None, x0 + larg / 2, y0, larg / 2))
        if self.pasta == INICIO:
            return                                           # a HOME não é um caminho: nada pra copiar
        bx = x0 + larg + 26
        cr.new_path()
        cr.arc(bx, y0, 18, 0, 2 * math.pi)
        cr.set_source_rgba(*FUNDO_BOLHA, 0.85 * fundo)
        cr.fill()
        self._texto_cor(cr, "COPY", bx, y0, "Doto Black 9.5", FG, 0.8 * fundo)
        self.zonas.append(("copiar", None, bx, y0, 18))

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
        cx, cy = self._centro()
        if self.liquido.ok:
            # the peek is a drop that left its bubble and grew in the middle; content once it settles
            pk = self.peek_k(agora)
            bx, by, br, _a = self._onde(b, agora)
            cx, cy = bx + (cx - bx) * pk, by + (cy - by) * pk
            R = br + (min(w, h) * 0.3 - br) * pk
            if pk < 0.75:
                return
        else:
            R = min(w, h) * 0.3 * pop(agora - t0)
        if R < 1:
            return
        cr.new_path()
        if not self.liquido.ok:
            cr.set_source_rgba(0.01, 0.01, 0.012, 0.6 * min(1, (agora - t0) / 0.2))
            cr.paint()
        g = cairo.RadialGradient(cx, cy, R * 0.9, cx, cy, R * 1.35)
        g.add_color_stop_rgba(0, *FG, 0.16)
        g.add_color_stop_rgba(1, *FG, 0.0)
        cr.set_source(g)
        cr.arc(cx, cy, R * 1.35, 0, 2 * math.pi)
        cr.fill()
        if not self.liquido.ok:
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
        if b.tipo == "disco":
            sub = (b.extra or {}).get("uso_txt", "DRIVE")
        else:
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
            if tipo in ("chip", "editar", "modo"):
                if abs(x - zx) <= zr and abs(y - zy) <= 17:
                    return tipo, valor
            elif math.hypot(x - zx, y - zy) <= zr:
                return tipo, valor
        return None

    def dentro_visor(self, x, y):
        j = self.visor.janela
        return j is not None and abs(x - j[0]) <= j[2] and abs(y - j[1]) <= j[3]

    def _rolar(self, _c, _dx, dy):
        if self.vis is None:
            return False
        mx, my = getattr(self, "mouse_v", None) or (None, None)
        self.visor.ampliar(1.18 ** (-dy), mx, my)
        self.area.queue_draw()
        return True

    def _clique(self, g, n, x, y):
        if getattr(self, "_arrastou", False):
            self._arrastou = False
            return
        if self.vis is not None:
            z = self.zona_em(x, y)
            if z is not None and z[0] == "busca":
                self.visor.buscar(z[1])
            elif z is not None and z[0] == "nav":
                self.navegar(z[1])
            elif self.dentro_visor(x, y):
                if n == 2:                                  # double click: fit <-> 2x at the pointer
                    self.visor.ampliar(1 / self.visor.zoom if self.visor.zoom > 1.01 else 2.0, x, y)
                elif self.visor.tipo in ("vid", "som", "gif"):
                    self.visor.tocar_pausar()
                self.area.queue_draw()
            else:
                self.fechar_visor()
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
            elif tipo == "modo":
                self.trocar_modo(valor)
            elif tipo == "editar":
                self.editando = exibir(self.pasta)
                self.area.queue_draw()
            elif tipo == "copiar":
                subprocess.Popen(["wl-copy", self.pasta], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.aviso = ("PATH COPIED", time.monotonic())
                self.animar()
            return
        i = self.bolha_em(x, y)
        if i is not None and g.get_current_button() == 2:     # middle click: pick / unpick this file
            p = self.bolhas[i].path
            if self.eh_raiz(self.bolhas[i]):
                self.aviso = ("A WHOLE DRIVE OR HOME CAN'T BE PICKED", time.monotonic())
                self.animar()
                return
            if p in self.selecionados:
                self.selecionados.remove(p)
            else:
                self.selecionados.append(p)
            self.area.queue_draw()
            return
        if i is None:
            self.fechar()
        elif g.get_current_button() == 1:
            self.abrir_item(self.bolhas[i])
        elif g.get_current_button() == 3:
            self.espiar(self.bolhas[i])

    def _arrasto_ini(self, g, x, y):
        if self.vis is not None:
            self.arrasto = "pan" if self.dentro_visor(x, y) and self.visor.zoom > 1.001 else None
            self._pan0 = list(self.visor.pan)
            return
        z = self.zona_em(x, y)
        self.arrasto = z[1] if z is not None and z[0] == "anel" else None
        self._ini = (x, y)

    def _arrasto_mov(self, g, dx, dy):
        if self.arrasto == "pan":
            self._arrastou = abs(dx) + abs(dy) > 3
            self.visor.pan = [self._pan0[0] + dx, self._pan0[1] + dy]
            self.visor.queue_draw()
            return
        if self.arrasto is None:
            return
        self._arrastou = True
        x, y = self._ini[0] + dx, self._ini[1] + dy
        w, h = self.area.get_width(), self.area.get_height()
        x0, _y, L = self.geo_anel(w, h)
        i = max(0, min(ANEL_N, round((x - x0) / L * ANEL_N)))
        if self.arrasto == 0:
            self.anel[0] = min(i, self.anel[1] - 2)
        else:
            self.anel[1] = max(i, self.anel[0] + 2)
        self.area.queue_draw()

    def _arrasto_fim(self):
        if self.arrasto == "pan":
            self.arrasto = None
            return False
        if self.arrasto is not None:
            self.arrasto = None
            self.atualizar()
        return False

    def _tecla(self, _c, tecla, _cod, mods):
        ctrl = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and tecla in (Gdk.KEY_l, Gdk.KEY_L):
            self.editando = exibir(self.pasta)
            self.area.queue_draw()
            return True
        if getattr(self, "editando", None) is not None:
            return self.tecla_caminho(tecla, ctrl)
        if self.vis is not None:
            m = self.visor.midia
            if tecla == Gdk.KEY_Escape:
                self.fechar_visor()
            elif tecla in (Gdk.KEY_Left, Gdk.KEY_Right):
                self.navegar(-1 if tecla == Gdk.KEY_Left else 1)
            elif tecla == Gdk.KEY_space:
                self.visor.tocar_pausar()
            elif tecla in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self.visor.ampliar(1.25)
            elif tecla in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self.visor.ampliar(0.8)
            elif tecla == Gdk.KEY_0:
                self.visor.ampliar(1 / self.visor.zoom)
            elif tecla in (Gdk.KEY_Up, Gdk.KEY_Down) and m is not None:
                m.set_volume(max(0.0, min(1.0, m.get_volume() + (0.1 if tecla == Gdk.KEY_Up else -0.1))))
            elif tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):          # Enter: open this file in its own app
                self.abrir_fora(types.SimpleNamespace(path=self.vis["itens"][self.vis["i"]]))
            self.area.queue_draw()
            return True
        atual = self.bolhas[self.sel] if 0 <= self.sel < len(self.bolhas) else None
        if self.foco is not None and 0 <= self.foco < len(self.bolhas) and self.mouse is not None:
            atual = self.bolhas[self.foco]
        if tecla == Gdk.KEY_Escape:
            if self.peek is not None:
                self.peek = None
                self.animar()
            elif self.consulta:
                self.mudar_busca("", 0)
            elif self.selecionados:
                self.selecionados = []
                self.area.queue_draw()
            else:
                self.fechar()
            return True
        if tecla == Gdk.KEY_space and atual is not None and not self.consulta:
            self.espiar(atual)                             # with text typed, space is just a space
            return True
        if ctrl and tecla in (Gdk.KEY_c, Gdk.KEY_C) and not self.campo.tudo:
            if not self.selecionados and atual is not None and self.eh_raiz(atual):
                self.acao("COPY", atual)                   # um disco / a ~: só o caminho, como texto
                return True
            alvos = list(self.selecionados) or ([atual.path] if atual is not None else [])
            if alvos:
                self.copiar_arquivos(alvos)
            return True
        if ctrl and tecla in (Gdk.KEY_v, Gdk.KEY_V) and not self.consulta:
            self.colar_arquivos()
            return True
        if self.editar_busca(tecla, ctrl, mods):
            return True
        if self.peek is not None:
            self.peek = None
            self.animar()
        if tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if atual is not None:
                self.abrir_item(atual)
            return True
        if ctrl and tecla in (Gdk.KEY_c, Gdk.KEY_C) and not self.campo.tudo:
            if atual is not None:
                self.acao("COPY", atual)
            return True
        if tecla == Gdk.KEY_BackSpace:
            if self.pasta == INICIO:
                pass                                     # a HOME é o topo de tudo
            elif self.pasta == "/" or self.pasta in self.topos():
                self.ir_para(INICIO)                     # ~, D:, um disco, a Lixeira: um passo acima é a HOME
            else:
                self.ir_para(os.path.dirname(self.pasta.rstrip("/")) or "/")
            return True
        if tecla in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab) and self.bolhas:
            passo = -1 if (tecla == Gdk.KEY_ISO_Left_Tab or mods & Gdk.ModifierType.SHIFT_MASK) else 1
            self.sel = (self.sel + passo) % len(self.bolhas)
            self.navegou, self.mouse, self.foco = True, None, None
            self.animar()
            return True
        direcoes = {Gdk.KEY_Left: (-1, 0), Gdk.KEY_Right: (1, 0), Gdk.KEY_Up: (0, -1), Gdk.KEY_Down: (0, 1)}
        if tecla in direcoes:                              # (with text, left/right moved the cursor above)
            self.foco = None
            self.mover_selecao(*direcoes[tecla])
            self.animar()
            return True
        return False

    # ---------- copy / paste files ----------

    def copiar_arquivos(self, caminhos):
        """Puts the files on the system clipboard as a file list (text/uri-list): Ctrl+V here or in any
        file manager pastes copies of them."""
        uris = "\r\n".join("file://" + urllib.parse.quote(p) for p in caminhos) + "\r\n"
        try:
            subprocess.run(["wl-copy", "-t", "text/uri-list"], input=uris.encode(), timeout=3)
        except (OSError, subprocess.SubprocessError):
            return
        self.aviso = (f"{len(caminhos)} COPIED · CTRL+V IN ANY FOLDER", time.monotonic())
        self.selecionados = []
        self.animar()

    def colar_arquivos(self):
        """Ctrl+V: copies of the files on the clipboard into the current folder (name (copy).ext when taken)."""
        if self.pasta == INICIO:
            self.aviso = ("OPEN A FOLDER TO PASTE INTO", time.monotonic())
            self.animar()
            return
        try:
            r = subprocess.run(["wl-paste", "-n", "-t", "text/uri-list"], capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            return
        fontes = [urllib.parse.unquote(u[7:]) for u in r.stdout.decode(errors="replace").split()
                  if u.startswith("file://")]
        destino = self.pasta
        # uma pasta nunca é colada dentro dela mesma (o copytree iria se copiando pra sempre)
        fontes = [f for f in fontes if os.path.exists(f) and not (destino + "/").startswith(f.rstrip("/") + "/")]
        if not fontes:
            self.aviso = ("NOTHING TO PASTE", time.monotonic())
            self.animar()
            return
        self.aviso = (f"PASTING {len(fontes)} …", time.monotonic())
        self.animar()

        def livre(nome):
            base, ext = os.path.splitext(nome)
            alvo, n = os.path.join(destino, nome), 1
            while os.path.exists(alvo):
                alvo = os.path.join(destino, f"{base} (copy{'' if n == 1 else ' ' + str(n)}){ext}")
                n += 1
            return alvo

        def trabalho():
            feitos = 0
            for f in fontes:
                try:
                    alvo = livre(os.path.basename(f.rstrip("/")))
                    if os.path.isdir(f):
                        shutil.copytree(f, alvo, symlinks=True)
                    else:
                        shutil.copy2(f, alvo)
                    feitos += 1
                except OSError:
                    continue
            def pronto():
                self.aviso = (f"{feitos} PASTED" if feitos == len(fontes) else f"{feitos}/{len(fontes)} PASTED",
                              time.monotonic())
                if self.pasta == destino:
                    self.atualizar(imediato=True)
                self.animar()
                return False
            GLib.idle_add(pronto)
        threading.Thread(target=trabalho, daemon=True).start()

    def _busca_mudou(self):
        self.consulta = self.campo.texto
        self.atualizar()
        self.animar()

    def mudar_busca(self, texto, cursor):
        self.campo.definir(texto, cursor)

    def editar_busca(self, tecla, ctrl, mods):
        """The search box is the shared DotStyle text field (campotexto.py)."""
        return self.campo.tecla(tecla, ctrl, self.get_display())

    def resolver(self, texto):
        t = texto.strip()
        if t.upper() == "HOME":
            return INICIO                                  # o nome da HOME virtual leva de volta pra ela
        if t.upper().startswith("D:"):
            t = DADOS + t[2:]
        elif t.startswith("~"):
            t = CASA + t[1:]
        return os.path.normpath(os.path.expanduser(t)) if t else self.pasta

    def tecla_caminho(self, tecla, ctrl):
        """Typing a path: Enter goes there (a file: its folder), Tab completes the folder name,
        Ctrl+V pastes, Esc cancels."""
        e = self.editando
        # na HOME o campo começa com "HOME", que não é um caminho: digitar/colar troca o texto todo
        novo = e == exibir(INICIO)
        if tecla == Gdk.KEY_Escape:
            self.editando = None
        elif tecla in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            alvo = self.resolver(e)
            if alvo != INICIO and os.path.isfile(alvo):
                alvo = os.path.dirname(alvo)
            if alvo == INICIO or os.path.isdir(alvo):
                self.editando = None
                self.ir_para(alvo)
            else:
                self.aviso = ("NOT FOUND", time.monotonic())
                self.animar()
        elif tecla == Gdk.KEY_Tab and (novo or not e):
            self.editando = "~/"                           # de "HOME" (ou vazio), o Tab começa pela ~
        elif tecla == Gdk.KEY_Tab:
            base = self.resolver(e)
            pai, parte = (base, "") if e.endswith("/") else os.path.split(base)
            try:
                nomes = sorted(n for n in os.listdir(pai) if n.casefold().startswith(parte.casefold())
                               and not n.startswith(".") and os.path.isdir(os.path.join(pai, n)))
            except OSError:
                nomes = []
            if nomes:
                self.editando = exibir(os.path.join(pai, nomes[0])) + "/"
        elif tecla == Gdk.KEY_BackSpace:
            self.editando = "" if novo else e[:-1]
        elif ctrl and tecla in (Gdk.KEY_v, Gdk.KEY_V):
            def colado(cb, res):
                try:
                    t = cb.read_text_finish(res)
                except GLib.Error:
                    t = None
                if t and t.strip() and self.editando is not None:
                    base = "" if self.editando == exibir(INICIO) else self.editando
                    self.editando = base + t.strip().splitlines()[0]
                    self.area.queue_draw()
            self.get_display().get_clipboard().read_text_async(None, colado)
        else:
            u = Gdk.keyval_to_unicode(tecla)
            if u and chr(u).isprintable() and not ctrl:
                self.editando = ("" if novo else e) + chr(u)
        self.area.queue_draw()
        return True

    def _mover(self, _c, x, y):
        if self.vis is not None:                             # while viewing: no lens behind the window
            self.mouse_v = None if x is None else (x, y)
            self.mouse = None
            return
        super()._mover(_c, x, y)


# ---------- the peek's content (read in a thread) ----------

def conteudo_peek_lido(b):
    """O PEEK dessa bolha precisa ler o disco? (imagem grande, o começo de um texto, a lista de uma pasta)"""
    if b.tipo in ("img", "dir", "grupo", "disco"):
        return True
    return b.tipo in ("code", "doc", "other") and b.tam < 4_000_000 and not b.nome.endswith(".pdf")


def conteudo_peek(path, tipo, tam):
    """(numa thread) O que o PEEK mostra: um pixbuf grande (imagem), as primeiras linhas de um texto, ou os
    nomes de dentro de uma pasta/disco (no máximo 5000 lidos: uma pasta enorme não demora). None = nada."""
    if tipo == "img":
        return miniatura(path, tam, 900)
    if tipo in ("dir", "grupo", "disco"):
        nomes = []
        try:
            with os.scandir(path) as it:
                for k, e in enumerate(it):
                    if k >= 5000:
                        break
                    if e.name.startswith("."):
                        continue
                    try:
                        d = e.is_dir()
                    except OSError:
                        d = False
                    nomes.append(e.name + ("/" if d else ""))
        except OSError:
            pass
        return sorted(nomes)[:28]
    try:
        with open(path, "rb") as f:
            bruto = f.read(4096)
    except OSError:
        return None
    if b"\0" in bruto:
        return None
    return bruto.decode("utf-8", "replace").expandtabs(4).splitlines()[:28]


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
    # leave without Python's teardown: the video decoder (GStreamer/VA) can crash while being torn down
    sys.stdout.flush()
    os._exit(0)
