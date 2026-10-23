"""
visitas.py — o que o ORBIT aprende com o uso, e onde ele abre (usado pelo ORBIT e pelo app Settings).

1) As visitas: cada pasta que o ORBIT mostra, e a pasta de cada arquivo que você abre por ele, ganha pontos
   em ~/.local/state/aero/orbit-visitas.json, no formato {pasta: {"n": visitas, "t": última vez (unix)}}.
   - A nota é "frecência" (frequência + recência, a mesma ideia da barra de endereço do Firefox):
         nota = n · e^(-idade / 21 dias)  +  1,5 · e^(-idade / 2 dias)
     A primeira parte é o hábito (muitas visitas), que vai murchando sozinho se você para de ir lá; a segunda
     é um bônus pequeno pra onde você esteve agora há pouco. Nada de regra fixa: a HOME se rearruma com o uso.
   - Passar por uma pasta a caminho de outra (menos de FICOU segundos, sem abrir nada) vale só PASSOU (0,25):
     assim ~ e Documents não ganham de tudo só por estarem no meio do caminho.
   - Guarda só as MAX (300) melhores. Grava de forma atômica (escreve um .tmp e troca com os.replace: o arquivo
     nunca fica pela metade, nem se a energia cair) numa thread, um tempinho depois da última mudança
     (debounce: 10 visitas seguidas = 1 gravação), nunca na thread que desenha.
   - Arquivo corrompido ou sumido = começa do zero, sem erro.
2) O modo da HOME: ~/.config/aero/orbit.json, {"home": "frequent" | "computer"}.
3) Leituras pro modo "computer": os armazenamentos de verdade (/proc/mounts), o espaço usado de cada um
   (statvfs) e as pastas padrão (Desktop, Downloads...). Funções puras: o ORBIT chama numa thread.
"""
import json
import math
import os
import re
import threading
import time

from gi.repository import GLib

ARQ = os.path.expanduser("~/.local/state/aero/orbit-visitas.json")
CONFIG = os.path.expanduser("~/.config/aero/orbit.json")
LIXO = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "Trash", "files")
MODOS = ("frequent", "computer")
MAX = 300                        # pastas guardadas (as de nota maior)
TAU = 21 * 86400                 # s: em quanto tempo o hábito cai pra ~37% (e^-1) sem visitas
FICOU = 2.5                      # s: ficar mais que isso numa pasta = visita inteira
PASSOU = 0.25                    # só passou por ela
ESPERA_GRAVAR = 1500             # ms depois da última mudança

# pastas que não são "suas": caches, temporárias, de sistema (e qualquer pasta escondida, com "." na frente)
CACHE = {"cache", "caches", "cacheddata", "gpucache", "shadercache", "code cache", "__pycache__", "node_modules",
         "tmp", "temp", "thumbnails"}
SISTEMA = ("/proc", "/sys", "/dev", "/tmp", "/run/user", "/var/tmp", "/var/cache")

_trava = threading.Lock()        # duas gravações nunca escrevem o mesmo .tmp ao mesmo tempo


# ---------- as visitas ----------

def nota(v, agora=None):
    """A frecência de uma pasta ({"n", "t"}) agora."""
    agora = time.time() if agora is None else agora
    idade = max(0.0, agora - v.get("t", 0.0))
    return v.get("n", 0.0) * math.exp(-idade / TAU) + 1.5 * math.exp(-idade / (2 * 86400))


def vale(pasta):
    """Essa pasta conta como visita? (a HOME virtual, pastas escondidas, caches e o sistema não contam)"""
    if not isinstance(pasta, str) or not pasta.startswith("/"):
        return False
    if any(pasta == s or pasta.startswith(s + "/") for s in SISTEMA):
        return False
    return not any(p.startswith(".") or p.casefold() in CACHE for p in pasta.strip("/").split("/") if p)


def ler(arq=None):
    """As visitas gravadas; {} se o arquivo não existe ou veio estragado (entradas ruins são puladas)."""
    try:
        with open(arq or ARQ, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    limpo = {}
    for k, v in (d.items() if isinstance(d, dict) else ()):
        try:
            if isinstance(k, str) and isinstance(v, dict):
                limpo[k] = {"n": float(v.get("n", 0)), "t": float(v.get("t", 0))}
        except (TypeError, ValueError):
            continue
    return limpo


def _gravar_json(arq, dados, **kw):
    """Escrita atômica: o .tmp inteiro primeiro, depois troca de nome (os.replace é instantâneo)."""
    with _trava:
        try:
            os.makedirs(os.path.dirname(arq), exist_ok=True)
            tmp = f"{arq}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dados, f, **kw)
            os.replace(tmp, arq)
        except OSError:
            pass


def gravar(dados, arq=None):
    agora = time.time()
    melhores = sorted(dados.items(), key=lambda kv: -nota(kv[1], agora))[:MAX]
    _gravar_json(arq or ARQ, {k: {"n": round(v["n"], 2), "t": int(v["t"])} for k, v in melhores},
                 separators=(",", ":"))


class Visitas:
    """As visitas em memória (um dict pequeno). registrar() é instantâneo; o disco fica pra depois, numa thread."""

    def __init__(self, arq=None):
        self.arq = arq or ARQ
        self.dados = ler(self.arq)
        self._fonte = None

    def registrar(self, pasta, peso=1.0):
        if not vale(pasta):
            return
        v = self.dados.get(pasta, {"n": 0.0, "t": 0.0})
        # um dict NOVO a cada vez: uma thread lendo a cópia antiga nunca vê um valor pela metade
        self.dados[pasta] = {"n": v["n"] + peso, "t": time.time()}
        if len(self.dados) > MAX * 1.5:                       # poda de vez em quando (não a cada visita)
            agora = time.time()
            self.dados = dict(sorted(self.dados.items(), key=lambda kv: -nota(kv[1], agora))[:MAX])
        self._agendar()

    def esquecer(self, pastas):
        """Pastas que não existem mais saem da lista."""
        tirou = [p for p in pastas if self.dados.pop(p, None) is not None]     # (todas: any() pararia na 1ª)
        if tirou:
            self._agendar()

    def copia(self):
        return dict(self.dados)

    def _agendar(self):
        if self._fonte:
            GLib.source_remove(self._fonte)
        self._fonte = GLib.timeout_add(ESPERA_GRAVAR, self._gravar)

    def _gravar(self):
        self._fonte = None
        threading.Thread(target=gravar, args=(dict(self.dados), self.arq), daemon=True).start()
        return False


# ---------- o modo da HOME ----------

def ler_modo(arq=None):
    try:
        with open(arq or CONFIG, encoding="utf-8") as f:
            d = json.load(f)
        modo = d.get("home") if isinstance(d, dict) else None
    except (OSError, ValueError):
        modo = None
    return modo if modo in MODOS else MODOS[0]


def gravar_modo(modo, arq=None):
    """Muda só a chave "home" (o resto do arquivo fica como estava)."""
    if modo not in MODOS:
        return
    arq = arq or CONFIG
    try:
        with open(arq, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict):
            d = {}
    except (OSError, ValueError):
        d = {}
    d["home"] = modo
    _gravar_json(arq, d, indent=2)


# ---------- o computador: discos e pastas padrão ----------

# Só aparelhos de verdade (/dev/...). Pontos de montagem do sistema ficam de fora: /boot, /var...
PSEUDO = {"squashfs", "overlay", "tmpfs", "devtmpfs", "ramfs", "autofs", "efivarfs"}
FORA = ("/boot", "/efi", "/var", "/snap", "/proc", "/sys", "/dev", "/run", "/tmp", "/nix", "/usr", "/recovery",
        "/swap")
REMOVIVEL = ("/run/media/", "/media/")


def _desescapar(s):
    """No /proc/mounts um espaço vem como \\040 (octal): "Meu HD" aparece "Meu\\040HD"."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def discos(dados=None):
    """Os armazenamentos: [(ponto de montagem, removível?)]: o sistema (/) primeiro, depois `dados` (o D:),
    os outros, e por último os removíveis (pendrive, HD externo, em /run/media/você/...).
    Um aparelho aparece uma vez só: o btrfs monta o MESMO disco em /, /home, /var/log..."""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            linhas = [l.split() for l in f.read().splitlines()]
    except OSError:
        linhas = []
    linhas.sort(key=lambda p: p[1:2] != ["/"])          # a linha do / primeiro (é ela que fica)
    vistos, achados = set(), []
    for partes in linhas:
        if len(partes) < 3:
            continue
        dev, ponto, fs = partes[0], _desescapar(partes[1]), partes[2]
        if not dev.startswith("/dev/") or dev.startswith("/dev/loop") or fs in PSEUDO:
            continue
        removivel = ponto.startswith(REMOVIVEL)
        if ponto != "/" and not removivel and any(ponto == p or ponto.startswith(p + "/") for p in FORA):
            continue
        if dev in vistos or any(ponto == a for a, _ in achados):
            continue
        vistos.add(dev)
        achados.append((ponto, removivel))
    if not any(p == "/" for p, _ in achados):
        achados.insert(0, ("/", False))
    return sorted(achados, key=lambda it: (it[1], it[0] != "/", it[0] != dados, it[0]))


def espaco(ponto):
    """(usado, tamanho, livre) em bytes, como o df mostra; None se não deu pra ler. Pode demorar num disco
    que está acordando: chame numa thread."""
    try:
        st = os.statvfs(ponto)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    if total <= 0:
        return None
    return (st.f_blocks - st.f_bfree) * st.f_frsize, total, st.f_bavail * st.f_frsize


def _gb(n):
    g = n / 1024 ** 3
    if g >= 1000:
        return f"{g / 1024:.1f}", "TB"
    return (f"{g:.0f}" if g >= 10 else f"{g:.1f}"), "GB"


def uso_txt(usado, total):
    """"881 / 954 GB" (ou "888 GB / 1.9 TB" quando as unidades são diferentes)."""
    (a, ua), (b, ub) = _gb(usado), _gb(total)
    return f"{a} / {b} {ub}" if ua == ub else f"{a} {ua} / {b} {ub}"


def livre_txt(livre):
    v, u = _gb(livre)
    return f"{v} {u}"


def lugares():
    """As pastas padrão que existem, na ordem do "Meu computador": Desktop, Downloads, Documents, Pictures,
    Videos, Music (os nomes de verdade vêm do ~/.config/user-dirs.dirs, então funcionam em qualquer idioma)."""
    casa = os.path.expanduser("~")
    U = GLib.UserDirectory
    saida = []
    for d in (U.DIRECTORY_DESKTOP, U.DIRECTORY_DOWNLOAD, U.DIRECTORY_DOCUMENTS, U.DIRECTORY_PICTURES,
              U.DIRECTORY_VIDEOS, U.DIRECTORY_MUSIC):
        p = GLib.get_user_special_dir(d)
        if p and p != casa and p not in saida and os.path.isdir(p):
            saida.append(p)
    return saida
