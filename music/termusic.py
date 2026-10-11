#!/usr/bin/env python3
"""
TerMusic — o YouTube Music no terminal (kitty), com o mesmo motor do app YTMusic (motor.py).

Modos (o lançador ~/.local/bin/termusic monta a janela):
  termusic.py tui      o player: status, abas Queue/Search/Recent/Playlists/Liked, tabela de músicas
  termusic.py capa     a capa da música atual desenhada com pontos braille (contornos, nas cores da capa)
  termusic.py widget   versão compacta: capa pequena + música + progresso + mini visualizador

Teclas do player (tui):
  Tab / 1-5  trocar de aba          ↑↓ j k PgUp PgDn g G  andar na lista
  Enter      tocar (na fila: pular pra ela; em Playlists: abrir; Backspace volta)
  /          buscar                 a  pôr na fila    A  tocar em seguida    r  rádio da música
  d / Del    tirar da fila          J K  mover na fila
  espaço     tocar/pausar           n p  próxima/anterior    ← →  -5s/+5s    + -  volume
  s embaralhar   R repetir   o rádio automático   C limpar a fila toda (2x)   q fechar (a música continua)
Widget: espaço, n, p, ← →, + -, q.
"""
import hashlib
import json
import math
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
import unicodedata
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cliente import ErroMotor, chamar  # noqa: E402

CAPAS = os.path.expanduser("~/.cache/ytmotor/capas")
CAVA_RAW = os.path.expanduser("~/.config/termusic/cava-raw.conf")

# ---------- cores: as do tema do sistema (~/.config/waybar/cores.css, o mesmo da barra) ----------
def _cores_do_tema():
    c = {"fundo-solido": "#1e1e1e", "texto": "#f0f0f0", "destaque": "#f0f0f0", "destaque-texto": "#1e1e1e"}
    try:
        for nome, valor in re.findall(r"@define-color\s+([\w-]+)\s+(#[0-9a-fA-F]{6});",
                                      open(os.path.expanduser("~/.config/waybar/cores.css")).read()):
            c[nome] = valor
    except OSError:
        pass
    return {k: tuple(int(v[i:i + 2], 16) for i in (1, 3, 5)) for k, v in c.items()}


_C = _cores_do_tema()
FUNDO, TEXTO = _C["fundo-solido"], _C["texto"]
DESTAQUE, DESTAQUE_TEXTO = _C["destaque"], _C["destaque-texto"]
R = "\033[0m"
NEG = "\033[1m"


def fg(c):
    return f"\033[38;2;{c[0]};{c[1]};{c[2]}m"


def bg(c):
    return f"\033[48;2;{c[0]};{c[1]};{c[2]}m"


def mistura(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


APAGADO = mistura(TEXTO, FUNDO, 0.55)      # texto secundário
LINHA = mistura(TEXTO, FUNDO, 0.80)        # linhas, coisas desligadas
SUAVE = mistura(TEXTO, FUNDO, 0.25)        # texto quase normal (linhas alternadas)


def degrade(t):
    """0..1 -> cinza escuro até a cor do texto (visualizador e progresso): neutro, segue o tema."""
    return mistura(mistura(TEXTO, FUNDO, 0.72), TEXTO, t)


# ---------- texto com largura certa (acentos, cirílico, CJK, emoji) ----------
SEM_CONTROLE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
def larg(s):
    n = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


def cortar(s, n):
    """Corta s pra caber em n colunas (com …) e completa com espaços."""
    s = SEM_CONTROLE.sub(" ", s or "")      # títulos vêm de fora: nada de ESC/controles no terminal
    if n <= 0:
        return ""
    if larg(s) <= n:
        return s + " " * (n - larg(s))
    out, w = "", 0
    for ch in s:
        cw = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if w + cw > n - 1:
            break
        out += ch
        w += cw
    return out + "…" + " " * (n - w - 1)


ESC = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def caber(texto, n):
    """Corta uma linha COM cores pra no máximo n colunas visíveis (as cores não contam)."""
    out, w, i = [], 0, 0
    while i < len(texto):
        m = ESC.match(texto, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        ch = texto[i]
        cw = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1)
        if w + cw > n:
            break
        out.append(ch)
        w += cw
        i += 1
    return "".join(out) + R


def tempo(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def tamanho_terminal():
    t = shutil.get_terminal_size((80, 24))
    return t.columns, t.lines


# ---------- capa em braille ----------
def baixar_capa(url):
    if not url:
        return None
    os.makedirs(CAPAS, exist_ok=True)
    arq = os.path.join(CAPAS, hashlib.sha1(url.encode()).hexdigest() + ".img")
    if not os.path.exists(arq):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "termusic"}),
                                        timeout=15) as r:
                dados = r.read(8_000_000)
            with open(arq + ".tmp", "wb") as f:
                f.write(dados)
            os.replace(arq + ".tmp", arq)
        except Exception:  # noqa: BLE001
            return None
    return arq


def braille(arq, cols, linhas, densidade=0.20):
    """A imagem vira texto braille (cada caractere = 2x4 pontos): contornos (Sobel) acesos, cada
    caractere com a cor média daquele pedaço da capa (clareada pra aparecer no fundo escuro)."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    try:
        pix = GdkPixbuf.Pixbuf.new_from_file(arq)
    except Exception:  # noqa: BLE001
        return []
    w, h = pix.get_width(), pix.get_height()
    lado = min(w, h)                                   # recorta quadrado (capas de vídeo são 16:9)
    pix = pix.new_subpixbuf((w - lado) // 2, (h - lado) // 2, lado, lado)
    # célula do terminal ~ 1:2 -> quadrado ocupa cols = 2*linhas aprox.; cabe no espaço disponível
    lin = min(linhas, cols // 2)
    col = lin * 2
    pw, ph = col * 2, lin * 4
    pix = pix.scale_simple(pw, ph, GdkPixbuf.InterpType.BILINEAR)
    px, rs, nc = pix.get_pixels(), pix.get_rowstride(), pix.get_n_channels()
    cor = [[(px[y * rs + x * nc], px[y * rs + x * nc + 1], px[y * rs + x * nc + 2]) for x in range(pw)] for y in range(ph)]
    lum = [[0.299 * r + 0.587 * g + 0.114 * b for r, g, b in linha] for linha in cor]
    mag = [[0.0] * pw for _ in range(ph)]
    for y in range(1, ph - 1):
        for x in range(1, pw - 1):
            gx = (lum[y - 1][x + 1] + 2 * lum[y][x + 1] + lum[y + 1][x + 1]) - (lum[y - 1][x - 1] + 2 * lum[y][x - 1] + lum[y + 1][x - 1])
            gy = (lum[y + 1][x - 1] + 2 * lum[y + 1][x] + lum[y + 1][x + 1]) - (lum[y - 1][x - 1] + 2 * lum[y - 1][x] + lum[y - 1][x + 1])
            mag[y][x] = math.hypot(gx, gy)
    valores = sorted(v for linha in mag for v in linha)
    limite = valores[int(len(valores) * (1 - densidade))] if valores else 0
    bits = ((0, 0, 0x01), (0, 1, 0x02), (0, 2, 0x04), (1, 0, 0x08), (1, 1, 0x10), (1, 2, 0x20), (0, 3, 0x40), (1, 3, 0x80))
    saida = []
    for cy in range(lin):
        partes = []
        for cx in range(col):
            codigo, soma, n = 0, [0, 0, 0], 0
            for dx, dy, b in bits:
                x, y = cx * 2 + dx, cy * 4 + dy
                if mag[y][x] > limite and mag[y][x] > 18:
                    codigo |= b
                r, g, bb = cor[y][x]
                soma[0] += r
                soma[1] += g
                soma[2] += bb
                n += 1
            if codigo:
                c = tuple(s // n for s in soma)
                c = mistura(c, (255, 255, 255), 0.35)          # clareia: aparece no fundo escuro
                partes.append(fg(c) + chr(0x2800 + codigo))
            else:
                partes.append(" ")
        saida.append("".join(partes) + R)
    return saida


# ---------- teclado ----------
class Teclado:
    def __enter__(self):
        self.velho = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        # tela alternativa, sem cursor, sem quebra automática, mouse ligado (cliques e rodinha, formato SGR)
        sys.stdout.write("\033[?1049h\033[?25l\033[?7l\033[?1000h\033[?1006h")
        return self

    def __exit__(self, *a):
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self.velho)
        sys.stdout.write("\033[?1006l\033[?1000l\033[?7h\033[?25h\033[?1049l")
        sys.stdout.flush()

    def ler(self, espera):
        r, _, _ = select.select([sys.stdin], [], [], espera)
        if not r:
            return None
        ch = os.read(sys.stdin.fileno(), 64).decode(errors="ignore")
        m = re.match(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])", ch)
        if m:                                            # mouse: ("mouse", botão, coluna, linha, apertou?)
            return ("mouse", int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) == "M")
        return {"\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left", "\x1b[5~": "pgup",
                "\x1b[6~": "pgdn", "\x1b[3~": "del", "\x1b[Z": "btab", "\t": "tab", "\n": "enter", "\r": "enter",
                "\x7f": "bs", "\x1b": "esc"}.get(ch, ch)


# ---------- estado compartilhado (atualizado em segundo plano) ----------
class Estado:
    def __init__(self):
        self.e = {}
        self.ref = (0.0, time.time(), False)
        self.erro = ""
        self.lock = threading.Lock()

    def vigiar(self):
        while True:
            try:
                e = chamar("estado", timeout=5)
                with self.lock:
                    self.e = e
                    self.ref = (e["pos"], time.time(), e["tocando"])
                    self.erro = ""
            except Exception as ex:  # noqa: BLE001
                self.erro = str(ex)
            time.sleep(0.6)

    def pos(self):
        p, quando, tocando = self.ref
        return p + (time.time() - quando if tocando else 0)


def em_fundo(f):
    threading.Thread(target=f, daemon=True).start()


# ---------- visualizador (cava em modo texto) pro widget ----------
class Visual:
    def __init__(self, barras):
        self.niveis = [0] * barras
        self.barras = barras
        os.makedirs(os.path.dirname(CAVA_RAW), exist_ok=True)
        open(CAVA_RAW, "w").write(f"""[general]
bars = {barras}
framerate = 30
[input]
method = pulse
source = auto
[output]
method = raw
raw_target = /dev/stdout
data_format = ascii
ascii_max_range = 8
bar_delimiter = 59
""")
        em_fundo(self.rodar)

    def rodar(self):
        p = subprocess.Popen(["cava", "-p", CAVA_RAW], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        for linha in p.stdout:
            v = [int(x) for x in linha.strip().strip(";").split(";") if x.isdigit()]
            if len(v) == self.barras:
                self.niveis = v


# ============================== modo capa ==============================
def modo_capa():
    est = Estado()
    em_fundo(est.vigiar)
    ultimo, tam_ultimo = None, None
    sys.stdout.write("\033[?25l")
    signal.signal(signal.SIGWINCH, lambda *_: None)
    # A capa é só para ver: o terminal não ecoa nada e tudo que for digitado aqui é descartado
    # (sem ECHO/ICANON/ISIG: nem letras na arte, nem Ctrl+C fechando o painel)
    import select, termios
    fd = sys.stdin.fileno()
    try:
        antigo = termios.tcgetattr(fd)
        novo = termios.tcgetattr(fd)
        novo[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN)
        termios.tcsetattr(fd, termios.TCSANOW, novo)
    except termios.error:
        antigo = None

    def descartar(espera):
        fim = time.time() + espera
        while (resto := fim - time.time()) > 0:
            if select.select([fd], [], [], resto)[0]:
                try:
                    os.read(fd, 4096)
                except OSError:
                    time.sleep(resto)
    try:
        while True:
            a = (est.e or {}).get("atual") or {}
            url = a.get("capa")
            tam = tamanho_terminal()
            if (url, tam) != (ultimo, tam_ultimo):
                ultimo, tam_ultimo = url, tam
                cols, lin = tam
                linhas = braille(baixar_capa(url), cols - 2, lin - 1) if url else []
                sys.stdout.write("\033[2J\033[H")
                if not linhas:
                    msg = "nothing playing"
                    sys.stdout.write(f"\033[{lin // 2};{max(1, (cols - larg(msg)) // 2)}H{fg(APAGADO)}{msg}{R}")
                else:
                    topo = max(1, (lin - len(linhas)) // 2 + 1)
                    visivel = larg(re.sub(r"\033\[[0-9;]*m", "", linhas[0]))
                    esq = max(1, (cols - visivel) // 2 + 1)
                    for i, l in enumerate(linhas):
                        sys.stdout.write(f"\033[{topo + i};{esq}H{l}")
                sys.stdout.flush()
            descartar(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if antigo:
            termios.tcsetattr(fd, termios.TCSANOW, antigo)


# ============================== modo player (tui) ==============================
ABAS = [("fila", "Queue"), ("busca", "Search"), ("recentes", "Recent"), ("playlists", "Playlists"), ("curtidas", "Liked")]


class Tui:
    def __init__(self):
        self.est = Estado()
        em_fundo(self.est.vigiar)
        self.aba = "fila"
        self.listas = {"busca": [], "recentes": None, "playlists": None, "curtidas": None}
        self.playlist_aberta = None          # (nome, faixas) dentro da aba Playlists
        self.sel = {k: 0 for k, _ in ABAS}
        self.topo = {k: 0 for k, _ in ABAS}
        self.digitando = None                # texto da busca enquanto digita
        self.msg = ""
        self.msg_ate = 0
        self.ultima_busca = ""
        self.ajuda = False
        self.confirmar_limpar = 0          # 1º "C" arma (3 s); o 2º limpa a fila
        self.ultimo_clique = None
        self.y_lista = 8

    def aviso(self, t):
        self.msg, self.msg_ate = t, time.time() + 3

    def cmd(self, c, aviso=None, **p):
        def f():
            try:
                chamar(c, **p)
                if aviso:
                    self.aviso(aviso)
            except ErroMotor as e:
                self.aviso(f"error: {e}")
        em_fundo(f)

    # ---- dados de cada aba ----
    def itens(self):
        if self.aba == "fila":
            return (self.est.e or {}).get("fila") or []
        if self.aba == "playlists" and self.playlist_aberta:
            return self.playlist_aberta[1]
        return self.listas.get(self.aba) or []

    def carregar(self, aba, forcar=False):
        if self.listas.get(aba) is not None and not forcar:
            return
        self.listas[aba] = []

        def f():
            try:
                if aba == "recentes":
                    self.listas[aba] = chamar("recentes")
                elif aba == "playlists":
                    self.listas[aba] = chamar("playlists")
                elif aba == "curtidas":
                    self.listas[aba] = chamar("playlist", id="LM")
            except ErroMotor as e:
                self.aviso(f"error: {e}")
        self.aviso("loading…")
        em_fundo(f)

    def trocar_aba(self, k):
        self.aba = k
        if k in ("recentes", "playlists", "curtidas"):
            self.carregar(k)
        if k == "busca" and not self.listas["busca"]:
            self.digitando = ""

    # ---- desenho ----
    def desenhar(self):
        cols, lin = tamanho_terminal()
        e = self.est.e or {}
        a = e.get("atual") or {}
        L = []
        # linha 1: [Playing] título ............ Volume: ▁▂▃▅▇ 80%
        estado = "[Playing]" if e.get("tocando") else ("[Paused]" if a else "[Stopped]")
        cor_est = TEXTO if e.get("tocando") else APAGADO
        vol = int(e.get("volume") or 0)
        blocos = "▁▂▃▄▅▆▇█"
        barra_vol = "".join(fg(degrade(i / 7)) + blocos[i] if vol >= (i + 1) * 100 / 8 else fg(LINHA) + blocos[i]
                            for i in range(8))
        direita = f"{fg(APAGADO)}Volume: {barra_vol} {fg(TEXTO)}{vol:3d}%{R}"
        titulo = " — ".join(x for x in (a.get("artista"), a.get("titulo")) if x) or "nothing playing"
        espaco = cols - larg(estado) - 2 - 21           # 21 = "Volume: ▁▂▃▄▅▆▇█ 100%"
        L.append(f"{fg(cor_est)}{NEG}{estado}{R} {fg(TEXTO)}{NEG}{cortar(titulo, espaco)}{R} {direita}")
        # linha 2: 0:06 / 2:41 · álbum ........ Repeat / Radio / Shuffle
        pos, dur = self.est.pos(), e.get("duracao") or a.get("duracao") or 0
        flags = []
        for nome, ligado in (("Repeat one" if e.get("repetir_uma") else "Repeat", e.get("repetir") or e.get("repetir_uma")),
                             ("Radio", e.get("radio"))):
            flags.append(f"{fg(TEXTO if ligado else LINHA)}{nome}{R}")
        flag_txt = f" {fg(LINHA)}/{R} ".join(flags)
        esq = f"{fg(TEXTO)}{tempo(pos)} / {tempo(dur)}{R}  {fg(APAGADO)}{SEM_CONTROLE.sub(" ", a.get('album') or '')}{R}"
        L.append(esq + " " * max(1, cols - larg(re.sub(r'\033\[[0-9;]*m', '', esq)) - larg(re.sub(r'\033\[[0-9;]*m', '', flag_txt))) + flag_txt)
        # linha 3: progresso fino
        frac = min(1, pos / dur) if dur else 0
        cheio = int(frac * cols)
        L.append("".join(fg(degrade(i / max(cols - 1, 1))) + "━" for i in range(cheio)) + fg(LINHA) + "─" * (cols - cheio) + R)
        # linha 4: abas
        abas = ""
        larg_aba = max(10, cols // len(ABAS))
        for k, nome in ABAS:
            n = len((self.est.e or {}).get("fila") or []) if k == "fila" else None
            rot = f"{nome} {n}" if n else nome
            txt = rot.center(larg_aba)
            abas += (f"{bg(DESTAQUE)}{fg(DESTAQUE_TEXTO)}{NEG}{txt}{R}" if k == self.aba else f"{fg(TEXTO)}{txt}{R}")
        L.append(abas[:10000])
        L.append(fg(APAGADO) + "─" * cols + R)
        # tabela
        itens = self.itens()
        corpo = lin - len(L) - 3
        ca, ct, cal, cd = self.colunas(cols)
        if self.aba == "playlists" and not self.playlist_aberta:
            cab = f"{fg(TEXTO)}{NEG}{cortar('Playlist', cols - 10)}{'Songs':>10}{R}"
        else:
            cab = f"{fg(TEXTO)}{NEG}{cortar('Artist', ca)} {cortar('Title', ct)} {cortar('Album', cal)} {'Duration':>{cd}}{R}"
        L.append(cab)
        L.append(fg(APAGADO) + "─" * cols + R)
        if self.aba == "playlists" and self.playlist_aberta:
            L[-2] = f"{fg(TEXTO)}{NEG}{self.playlist_aberta[0]}{R}  {fg(APAGADO)}(Backspace to go back){R}"
        sel = self.sel[self.aba] = max(0, min(self.sel[self.aba], len(itens) - 1)) if itens else 0
        topo = self.topo[self.aba]
        if sel < topo:
            topo = sel
        if sel >= topo + corpo:
            topo = sel - corpo + 1
        self.topo[self.aba] = topo
        for i in range(topo, min(len(itens), topo + corpo)):
            it = itens[i]
            if self.aba == "playlists" and not self.playlist_aberta:
                linha = f"{cortar(it['titulo'], cols - 10)}{str(it.get('qtd') or ''):>10}"
            else:
                linha = (f"{cortar(it.get('artista'), ca)} {cortar(it.get('titulo'), ct)} {cortar(it.get('album'), cal)} "
                         f"{tempo(it.get('duracao')) if it.get('duracao') else '':>{cd}}")
            if i == sel:
                L.append(f"{bg(DESTAQUE)}{fg(DESTAQUE_TEXTO)}{linha}{R}")
            elif it.get("atual"):
                L.append(f"{fg(TEXTO)}{NEG}{linha}{R}")
            else:
                L.append(f"{fg(TEXTO) if i % 2 == 0 else fg(SUAVE)}{linha}{R}")
        if not itens:
            vazio = {"fila": "queue is empty — press / to search", "busca": "press / and type to search",
                     "recentes": "loading…", "playlists": "loading…", "curtidas": "loading…"}[self.aba]
            L.append(f"{fg(APAGADO)}  {vazio}{R}")
        while len(L) < lin - 1:
            L.append("")
        # rodapé: busca / aviso / dicas
        if self.digitando is not None:
            rodape = f"{fg(APAGADO)}[->{R} {self.digitando}{fg(APAGADO)}▏{R}"
        elif self.msg and time.time() < self.msg_ate:
            rodape = f"{fg(TEXTO)}{SEM_CONTROLE.sub(' ', self.msg)}{R}"
        else:
            rodape = f"{fg(LINHA)}[->{R} " + self.dicas(cols - 4)
        L.append(rodape)
        if self.ajuda:
            L = self.tela_ajuda(L, cols, lin)
        self.y_lista = 8                         # a 1ª música fica na linha 8 da tela (pro mouse)
        # cada linha na SUA posição e cortada na largura: nada quebra nem empurra a tela
        sys.stdout.write("".join(f"\033[{i + 1};1H\033[2K{caber(l, cols)}" for i, l in enumerate(L[:lin])))
        sys.stdout.flush()

    @staticmethod
    def colunas(cols):
        """Artist | Title | Album | Duration (8 fixas). Sobra dividida 27% / 40% / resto."""
        cd = 8
        resto = max(12, cols - cd - 3)
        ca, ct = int(resto * 0.27), int(resto * 0.40)
        return ca, ct, resto - ca - ct, cd

    def dicas(self, largura):
        """Os atalhos que valem NA ABA atual, quantos couberem; sempre termina em "? help"."""
        comuns = [("space", "play/pause"), ("n/p", "next/prev"), ("←→", "seek"), ("+/-", "volume")]
        por_aba = {"fila": [("enter", "play"), ("d", "remove"), ("J/K", "move"), ("s", "shuffle"), ("r", "radio"),
                            ("C", "clear all")],
                   "busca": [("/", "search"), ("enter", "play"), ("a", "queue"), ("A", "play next"), ("r", "radio")],
                   "playlists": ([("enter", "play"), ("a", "queue"), ("bksp", "back")] if self.playlist_aberta
                                 else [("enter", "open")]),
                   "recentes": [("enter", "play"), ("a", "queue"), ("A", "play next"), ("r", "radio")],
                   "curtidas": [("enter", "play"), ("a", "queue"), ("A", "play next"), ("r", "radio")]}
        itens = por_aba.get(self.aba, []) + comuns + [("tab", "tabs"), ("q", "close")]
        fim = f"{fg(TEXTO)}?{R} {fg(APAGADO)}help{R}"
        partes, usado = [], 7
        for k, d in itens:
            pedaco = f"{fg(TEXTO)}{k}{R} {fg(APAGADO)}{d}{R}"
            w = larg(k) + 1 + larg(d) + 3
            if usado + w > largura:
                break
            partes.append(pedaco)
            usado += w
        return f" {fg(LINHA)}·{R} ".join(partes + [fim])

    AJUDA = [("Navigation", [("Tab / Shift+Tab", "next / previous tab"), ("1 2 3 4 5", "Queue Search Recent Playlists Liked"),
                             ("↑ ↓  j k", "move"), ("PgUp PgDn  g G", "page / top / bottom"),
                             ("mouse", "click tab/row · double-click plays · wheel scrolls")]),
             ("Play", [("Enter", "play from here (Queue: jump; Playlists: open)"), ("space", "play / pause"),
                       ("n  p", "next / previous"), ("← →", "seek -5s / +5s (click the bar to jump)"),
                       ("+ -", "volume")]),
             ("Queue", [("a", "add to queue"), ("A", "play next"), ("r", "radio from this song"),
                        ("d  Del", "remove from queue"), ("J  K", "move down / up"), ("s", "shuffle queue"), ("C", "clear the whole queue (press twice)"),
                        ("R", "repeat: off / queue / one"), ("o", "autoplay similar songs when the queue ends")]),
             ("Other", [("/", "search YouTube Music"), ("Backspace", "back (inside a playlist)"),
                        ("?", "this help"), ("q", "close (music keeps playing)")])]

    def tela_ajuda(self, L, cols, lin):
        def bloco(grupos, larg_desc):
            out = []
            for grupo, itens in grupos:
                out.append(f"{fg(TEXTO)}{NEG}{grupo}{R}")
                for k, d in itens:
                    out.append(f"  {fg(TEXTO)}{cortar(k, 15)}{R} {fg(APAGADO)}{d}{R}")
                out.append("")
            return out
        linhas = bloco(self.AJUDA, 0)
        if len(linhas) + 3 > lin - 2:                      # não cabe na altura: duas colunas
            meio = len(self.AJUDA) // 2
            esq, dir_ = bloco(self.AJUDA[:meio], 0), bloco(self.AJUDA[meio:], 0)
            metade = max(20, (min(cols - 4, 120) - 6) // 2)
            linhas = [self.preencher(esq[i] if i < len(esq) else "", metade) + "  " +
                      (dir_[i] if i < len(dir_) else "") for i in range(max(len(esq), len(dir_)))]
        linhas.append(f"{fg(LINHA)}any key closes{R}")
        w = min(cols - 4, 120 if len(linhas) < 20 else 72)
        x0 = max(0, (cols - w) // 2)
        y0 = max(1, (lin - len(linhas)) // 2)
        caixa = [f"{fg(LINHA)}┌{'─' * (w - 2)}┐{R}"] + \
                [f"{fg(LINHA)}│{R} {self.preencher(l, w - 4)} {fg(LINHA)}│{R}" for l in linhas] + \
                [f"{fg(LINHA)}└{'─' * (w - 2)}┘{R}"]
        for i, l in enumerate(caixa):
            if 0 <= y0 + i < len(L):
                L[y0 + i] = " " * x0 + l
        return L

    @staticmethod
    def preencher(l, n):
        c = caber(l, n)
        return c + " " * max(0, n - larg(ESC.sub("", c)))

    def mouse(self, b, x, y, apertou):
        cols, lin = tamanho_terminal()
        e = self.est.e or {}
        if b in (64, 65):                                  # rodinha
            self.sel[self.aba] = max(0, self.sel[self.aba] + (3 if b == 65 else -3))
            return
        if b != 0 or not apertou:
            return
        if y == 1 and x <= 10:                             # [Playing] / [Paused]: tocar/pausar
            self.cmd("pausar")
        elif y == 3:                                       # barra de progresso: pula pro ponto clicado
            dur = e.get("duracao") or 0
            if dur:
                self.cmd("ir", s=dur * (x - 1) / max(cols - 1, 1))
        elif y == 4:                                       # abas
            larg_aba = max(10, cols // len(ABAS))
            i = min(len(ABAS) - 1, (x - 1) // larg_aba)
            self.trocar_aba(ABAS[i][0])
        elif y >= self.y_lista and y < lin:
            idx = self.topo[self.aba] + (y - self.y_lista)
            if idx < len(self.itens()):
                agora = time.time()
                duplo = self.ultimo_clique and self.ultimo_clique[0] == idx and agora - self.ultimo_clique[1] < 0.45
                self.sel[self.aba] = idx
                self.ultimo_clique = (idx, agora)
                if duplo:
                    self.ultimo_clique = None
                    self.tecla("enter")

    # ---- teclas ----
    def tecla(self, k):
        if isinstance(k, tuple):
            if self.ajuda:
                self.ajuda = False
            else:
                self.mouse(*k[1:])
            return True
        if self.ajuda:
            self.ajuda = False
            return True
        if k == "?" and self.digitando is None:
            self.ajuda = True
            return True
        if self.digitando is not None:
            if k == "enter":
                q, self.digitando = self.digitando.strip(), None
                if q:
                    self.buscar(q)
            elif k == "esc":
                self.digitando = None
            elif k == "bs":
                self.digitando = self.digitando[:-1]
            elif isinstance(k, str) and len(k) >= 1 and k.isprintable():
                self.digitando += k
            return True
        itens = self.itens()
        s = self.sel[self.aba]
        cols, lin = tamanho_terminal()
        pagina = max(1, lin - 10)
        if k in ("q", "Q"):
            return False
        if k == "tab":
            ks = [x for x, _ in ABAS]
            self.trocar_aba(ks[(ks.index(self.aba) + 1) % len(ks)])
        elif k == "btab":
            ks = [x for x, _ in ABAS]
            self.trocar_aba(ks[(ks.index(self.aba) - 1) % len(ks)])
        elif k in ("1", "2", "3", "4", "5"):
            self.trocar_aba(ABAS[int(k) - 1][0])
        elif k in ("down", "j"):
            self.sel[self.aba] = s + 1
        elif k in ("up", "k"):
            self.sel[self.aba] = max(0, s - 1)
        elif k == "pgdn":
            self.sel[self.aba] = s + pagina
        elif k == "pgup":
            self.sel[self.aba] = max(0, s - pagina)
        elif k == "g":
            self.sel[self.aba] = 0
        elif k == "G":
            self.sel[self.aba] = len(itens) - 1
        elif k == "/":
            self.aba, self.digitando = "busca", ""
        elif k == " ":
            self.cmd("pausar")
        elif k == "n":
            self.cmd("proxima")
        elif k == "p":
            self.cmd("anterior")
        elif k == "right":
            self.cmd("ir", s=self.est.pos() + 5)
        elif k == "left":
            self.cmd("ir", s=max(0, self.est.pos() - 5))
        elif k in ("+", "="):
            self.cmd("volume", v=(self.est.e.get("volume") or 0) + 5)
        elif k == "-":
            self.cmd("volume", v=(self.est.e.get("volume") or 0) - 5)
        elif k == "s":
            self.cmd("embaralhar", "shuffled")
        elif k == "R":
            self.cmd("repetir")
        elif k == "C" and self.aba == "fila":
            if time.time() < self.confirmar_limpar:
                self.confirmar_limpar = 0
                self.cmd("limpar", "queue cleared")
                self.sel["fila"] = 0
            elif (self.est.e or {}).get("fila"):
                self.confirmar_limpar = time.time() + 3
                self.aviso("clear the whole queue? press C again")
        elif k == "o":
            self.cmd("auto_radio")
        elif k == "bs" and self.aba == "playlists" and self.playlist_aberta:
            self.playlist_aberta = None
        elif not itens:
            return True
        elif k == "enter":
            if self.aba == "fila":
                self.cmd("pular_para", i=s)
            elif self.aba == "playlists" and not self.playlist_aberta:
                pl = itens[s]
                self.aviso(f"opening {pl['titulo']}…")

                def abrir(pl=pl):
                    try:
                        self.playlist_aberta = (pl["titulo"], chamar("playlist", id=pl["id"]))
                        self.sel["playlists"] = 0
                    except ErroMotor as e:
                        self.aviso(f"error: {e}")
                em_fundo(abrir)
            else:
                self.cmd("tocar", faixas=itens, inicio=s)
        elif self.aba == "playlists" and not self.playlist_aberta:
            return True
        elif k == "a" and self.aba != "fila":
            self.cmd("adicionar", "added to queue", faixas=[itens[s]])
        elif k == "A" and self.aba != "fila":
            self.cmd("proxima_na_fila", "plays next", faixas=[itens[s]])
        elif k == "r":
            self.cmd("radio", f"radio from {itens[s]['titulo']}", id=itens[s]["id"])
        elif k in ("d", "del") and self.aba == "fila":
            self.cmd("remover", i=s)
        elif k == "J" and self.aba == "fila" and s < len(itens) - 1:
            self.cmd("mover", de=s, para=s + 1)
            self.sel["fila"] = s + 1
        elif k == "K" and self.aba == "fila" and s > 0:
            self.cmd("mover", de=s, para=s - 1)
            self.sel["fila"] = s - 1
        return True

    def buscar(self, q):
        self.aba = "busca"
        self.aviso(f"searching “{q}”…")

        def f():
            try:
                self.listas["busca"] = chamar("buscar", q=q)
                self.sel["busca"] = 0
                self.aviso(f"{len(self.listas['busca'])} results for “{q}”")
            except ErroMotor as e:
                self.aviso(f"error: {e}")
        em_fundo(f)

    def rodar(self):
        with Teclado() as t:
            sys.stdout.write("\033[2J")
            while True:
                self.desenhar()
                k = t.ler(0.25)
                if k is not None and not self.tecla(k):
                    break


# ============================== modo widget ==============================
def modo_widget():
    est = Estado()
    em_fundo(est.vigiar)
    vis = Visual(24)
    capa_linhas, capa_url, capa_tam = [], None, None
    with Teclado() as t:
        sys.stdout.write("\033[2J")
        while True:
            cols, lin = tamanho_terminal()
            e = est.e or {}
            a = e.get("atual") or {}
            lado = max(6, lin - 1)                       # capa: altura toda, quadrada
            if (a.get("capa"), (cols, lin)) != (capa_url, capa_tam):
                capa_url, capa_tam = a.get("capa"), (cols, lin)
                capa_linhas = braille(baixar_capa(capa_url), lado * 2, lado, densidade=0.24) if capa_url else []
            esq = larg(re.sub(r"\033\[[0-9;]*m", "", capa_linhas[0])) + 1 if capa_linhas else 0
            x0 = esq + 3
            w = max(10, cols - x0)
            saida = ["\033[H"]
            for i in range(lin):
                saida.append(f"\033[{i + 1};1H\033[2K")
                if i < len(capa_linhas):
                    saida.append(" " + capa_linhas[i])
            # texto à direita
            pos, dur = est.pos(), e.get("duracao") or a.get("duracao") or 0
            linhas = [
                f"{fg(TEXTO if e.get('tocando') else APAGADO)}{NEG}{'playing' if e.get('tocando') else ('paused' if a else 'stopped')}{R}",
                f"{fg(TEXTO)}{NEG}{cortar(a.get('titulo') or 'nothing playing', w)}{R}",
                f"{fg(APAGADO)}{cortar(a.get('artista') or 'termusic', w)}{R}",
                f"{fg(APAGADO)}{cortar(a.get('album') or '', w)}{R}",
                "",
            ]
            # mini visualizador (2 linhas de altura, meia-altura com ▄▀)
            n = min(len(vis.niveis), w // 2)
            topo_l, baixo_l = "", ""
            for j in range(n):
                v = vis.niveis[j] if e.get("tocando") else 0
                c = fg(degrade(j / max(n - 1, 1)))
                baixo_l += c + (" ▁▂▃▄▅▆▇█"[min(v, 8)] if v <= 8 else "█") + " "
                topo_l += c + (" ▁▂▃▄▅▆▇█"[max(0, min(v - 8, 8))] if v > 8 else " ") + " "
            linhas += [topo_l + R, baixo_l + R]
            frac = min(1, pos / dur) if dur else 0
            barra_w = max(4, w - 12)
            cheio = int(frac * barra_w)
            linhas.append("".join(fg(degrade(i / barra_w)) + "━" for i in range(cheio)) + fg(LINHA) + "─" * (barra_w - cheio) +
                          f" {fg(TEXTO)}{tempo(pos)}/{tempo(dur)}{R}")
            linhas.append(f"{fg(LINHA)}space play  n/p skip  ←→ seek  q close{R}")
            inicio = max(0, (lin - len(linhas)) // 2)
            for i, l in enumerate(linhas):
                if inicio + i < lin:
                    saida.append(f"\033[{inicio + i + 1};{x0 + 1}H{caber(l, w)}")
            sys.stdout.write("".join(saida))
            sys.stdout.flush()
            k = t.ler(1 / 15)
            if k in ("q", "Q", "esc"):
                break
            acoes = {" ": ("pausar", {}), "n": ("proxima", {}), "p": ("anterior", {}),
                     "+": ("volume", {"v": (e.get("volume") or 0) + 5}), "-": ("volume", {"v": (e.get("volume") or 0) - 5}),
                     "right": ("ir", {"s": pos + 5}), "left": ("ir", {"s": max(0, pos - 5)})}
            if k in acoes:
                c, p = acoes[k]
                em_fundo(lambda c=c, p=p: chamar(c, **p))


if __name__ == "__main__":
    modo = sys.argv[1] if len(sys.argv) > 1 else "tui"
    try:
        chamar("estado", timeout=10)
    except ErroMotor as e:
        print(f"termusic: {e}")
        sys.exit(1)
    if modo == "capa":
        modo_capa()
    elif modo == "widget":
        modo_widget()
    else:
        Tui().rodar()
