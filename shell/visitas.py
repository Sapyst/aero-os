"""
visitas.py — o que o ORBIT aprende com o uso, e onde ele abre (usado pelo ORBIT e pelo app Settings).

1) As visitas: cada pasta que o ORBIT mostra, e a pasta de cada arquivo que você abre por ele, ganha pontos
   em ~/.local/state/aero/orbit-visitas.json, no formato
       {pasta: {"n": visitas, "t": última vez (unix), "u": peso da última visita, "m": o disco onde ela fica}}
   - A nota é "frecência" (frequência + recência, a mesma ideia da barra de endereço do Firefox):
         nota = n · e^(-idade / 21 dias)  +  1,5 · u · e^(-idade / 2 dias)
     A primeira parte é o hábito (muitas visitas), que vai murchando sozinho se você para de ir lá; a segunda
     é um bônus pequeno pra onde você esteve agora há pouco. Nada de regra fixa: a HOME se rearruma com o uso.
   - Passar por uma pasta a caminho de outra (menos de FICOU segundos, sem abrir nada) vale só PASSOU (0,25):
     assim ~ e Documents não ganham de tudo só por estarem no meio do caminho. E o bônus de "agora há pouco"
     também é proporcional (o "u"): só passar ganha 1/4 do bônus de uma visita de verdade, então nunca empata
     com ela. (Arquivos antigos sem "u": vale min(1, n).)
   - "m" é o ponto de montagem onde a pasta foi vista existindo (/, /home, /mnt/e...). Se um dia ela some e
     esse disco não está montado, é só o disco que está desligado: a visita não é esquecida.
   - Números estragados no arquivo (NaN, infinito, negativo, data no futuro) são jogados fora na leitura: uma
     entrada ruim nunca derruba a HOME nem volta pro disco.
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
RECENTE = 2 * 86400              # s: o mesmo pro bônus de "agora há pouco"
N_MAX = 1e6                      # teto do n (um arquivo editado à mão com 1e308 não estoura as contas)
FICOU = 2.5                      # s: ficar mais que isso numa pasta = visita inteira
PASSOU = 0.25                    # só passou por ela
ESPERA_GRAVAR = 1500             # ms depois da última mudança

# pastas que não são "suas": caches, temporárias, de sistema (e qualquer pasta escondida, com "." na frente)
CACHE = {"cache", "caches", "cacheddata", "gpucache", "shadercache", "code cache", "__pycache__", "node_modules",
         "tmp", "temp", "thumbnails"}
SISTEMA = ("/proc", "/sys", "/dev", "/tmp", "/run/user", "/var/tmp", "/var/cache")

_trava = threading.Lock()        # duas gravações nunca escrevem o mesmo .tmp ao mesmo tempo


# ---------- as visitas ----------

def _finito(*xs):
    return all(isinstance(x, (int, float)) and math.isfinite(x) for x in xs)


def peso_recente(v):
    """O "u" de uma visita: o peso da última (1 = visita de verdade, PASSOU = só passou). Sem "u" (arquivo
    antigo): min(1, n), que é exatamente isso pra quem só passou uma vez."""
    n = v.get("n", 0.0)
    u = v.get("u", min(1.0, n) if _finito(n) else 0.0)
    return min(1.0, max(0.0, u)) if _finito(u) else 0.0


def nota(v, agora=None):
    """A frecência de uma pasta ({"n", "t", "u"}) agora. Sempre um número finito >= 0 (um valor estragado
    vale 0), então a HOME pode tirar raiz, ordenar e dividir sem medo."""
    agora = time.time() if agora is None else agora
    n, t = v.get("n", 0.0), v.get("t", 0.0)
    if not _finito(n, t):
        return 0.0
    idade = max(0.0, agora - t)
    s = max(0.0, n) * math.exp(-idade / TAU) + 1.5 * peso_recente(v) * math.exp(-idade / RECENTE)
    return s if math.isfinite(s) else 0.0


def vale(pasta):
    """Essa pasta conta como visita? (a HOME virtual, pastas escondidas, caches e o sistema não contam)"""
    if not isinstance(pasta, str) or not pasta.startswith("/"):
        return False
    if any(pasta == s or pasta.startswith(s + "/") for s in SISTEMA):
        return False
    return not any(p.startswith(".") or p.casefold() in CACHE for p in pasta.strip("/").split("/") if p)


def limpar(v, agora=None):
    """Uma entrada {"n", "t", "u", "m"} conferida, ou None se ela não presta. O json do Python aceita NaN e
    Infinity, e alguém pode editar o arquivo à mão: só passa n finito >= 0 e t finito entre 0 e agora (até um
    minuto no futuro é tolerado e vira "agora": o relógio pode ter sido acertado)."""
    agora = time.time() if agora is None else agora
    if not isinstance(v, dict):
        return None
    try:
        n, t = float(v.get("n", 0)), float(v.get("t", 0))
    except (TypeError, ValueError, OverflowError):
        return None
    if not (math.isfinite(n) and n >= 0 and math.isfinite(t) and 0 < t <= agora + 60):
        return None
    try:
        u = float(v["u"]) if "u" in v else min(1.0, n)
    except (TypeError, ValueError, OverflowError):
        u = min(1.0, n)
    limpo = {"n": min(n, N_MAX), "t": min(t, agora), "u": min(1.0, max(0.0, u)) if math.isfinite(u) else min(1.0, n)}
    m = v.get("m")
    if isinstance(m, str) and m.startswith("/"):
        limpo["m"] = m
    return limpo


def ler(arq=None):
    """As visitas gravadas; {} se o arquivo não existe ou veio estragado (entradas ruins são puladas)."""
    try:
        with open(arq or ARQ, encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    agora, limpo = time.time(), {}
    for k, v in (d.items() if isinstance(d, dict) else ()):
        if isinstance(k, str) and k.startswith("/"):
            bom = limpar(v, agora)
            if bom is not None:
                limpo[k] = bom
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
    """As MAX melhores pro disco. Passa tudo por limpar() de novo: um valor ruim nunca é escrito de volta."""
    agora = time.time()
    bons = {k: b for k, b in ((k, limpar(v, agora)) for k, v in dados.items()) if b is not None}
    saida = {}
    for k, v in sorted(bons.items(), key=lambda kv: -nota(kv[1], agora))[:MAX]:
        saida[k] = {"n": round(v["n"], 2), "t": int(v["t"]) or 1, "u": round(v["u"], 3)}
        if "m" in v:
            saida[k]["m"] = v["m"]
    _gravar_json(arq or ARQ, saida, separators=(",", ":"))


class Visitas:
    """As visitas em memória (um dict pequeno). registrar() é instantâneo; o disco fica pra depois, numa thread."""

    def __init__(self, arq=None):
        self.arq = arq or ARQ
        self.dados = ler(self.arq)
        self._fonte = None

    def registrar(self, pasta, peso=1.0):
        if not vale(pasta):
            return
        agora = time.time()
        v = self.dados.get(pasta) or {"n": 0.0, "t": 0.0, "u": 0.0}
        # o bônus de recência que ela já tinha, murchado até agora; a visita nova não pode diminuir ele (passar
        # por uma pasta um minuto depois de ficar nela não apaga a visita de verdade), só somar o peso dela
        antes = peso_recente(v) * math.exp(-max(0.0, agora - v.get("t", 0.0)) / RECENTE)
        # um dict NOVO a cada vez: uma thread lendo a cópia antiga nunca vê um valor pela metade
        novo = {"n": min(N_MAX, v.get("n", 0.0) + peso), "t": agora, "u": min(1.0, max(peso, antes))}
        if "m" in v:
            novo["m"] = v["m"]
        self.dados[pasta] = novo
        if len(self.dados) > MAX * 1.5:                       # poda de vez em quando (não a cada visita)
            agora = time.time()
            self.dados = dict(sorted(self.dados.items(), key=lambda kv: -nota(kv[1], agora))[:MAX])
        self._agendar()

    def esquecer(self, pastas):
        """Pastas que não existem mais saem da lista."""
        tirou = [p for p in pastas if self.dados.pop(p, None) is not None]     # (todas: any() pararia na 1ª)
        if tirou:
            self._agendar()

    def anotar_montagens(self, vistas):
        """{pasta: ponto de montagem}: onde cada pasta foi vista existindo (a thread da HOME descobre pelo
        /proc/mounts). Serve pra saber, se ela sumir, se foi apagada ou se só o disco dela está desligado."""
        mudou = False
        for pasta, m in vistas.items():
            v = self.dados.get(pasta)
            if v is not None and v.get("m") != m:
                self.dados[pasta] = {**v, "m": m}
                mudou = True
        if mudou:
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
# Discos que não estão nesta máquina (rede) ou que um programa finge ser (FUSE: sshfs, rclone...): um stat neles
# pode esperar a rede pra sempre. O autofs é um ponto que ainda vai montar (encostar nele dispara a montagem).
# (o fuseblk fica de fora: é o ntfs-3g, um disco de verdade aqui dentro)
REDE = {"nfs", "nfs4", "cifs", "smb3", "smbfs", "ncpfs", "afs", "ceph", "glusterfs", "9p", "virtiofs", "davfs",
        "sshfs", "lustre", "gfs2", "ocfs2", "beegfs", "fuse", "autofs"}


def _desescapar(s):
    """No /proc/mounts um espaço vem como \\040 (octal): "Meu HD" aparece "Meu\\040HD"."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def montagens():
    """[(aparelho, ponto, tipo)] do /proc/mounts, na ordem dele (montagens empilhadas no mesmo ponto: a última
    é a que vale). É uma leitura do kernel (procfs), não de disco: nunca trava, nem com um NFS caído."""
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            linhas = f.read().splitlines()
    except OSError:
        return []
    saida = []
    for linha in linhas:
        p = linha.split()
        if len(p) >= 3:
            saida.append((p[0], _desescapar(p[1]), p[2]))
    return saida


def montagem_de(path, monts):
    """(ponto, tipo, aparelho) da montagem que guarda `path`: o ponto mais comprido que é começo dele. Só
    comparação de texto, sem encostar no disco."""
    melhor = ("/", "", "")
    for dev, ponto, fs in monts:
        if (path == ponto or path.startswith(ponto.rstrip("/") + "/")) and len(ponto) >= len(melhor[0]):
            melhor = (ponto, fs, dev)                     # ">=": no mesmo ponto vence a linha mais embaixo (a montagem de cima)
    return melhor


def remoto(fs):
    """Esse tipo de sistema de arquivos pode travar um stat (rede, FUSE, automount)?"""
    return fs in REDE or fs.startswith("fuse.")


def montados(monts):
    """Os pontos montados de verdade (um autofs sozinho é só a promessa de montar quando alguém encostar)."""
    return {ponto for _d, ponto, fs in monts if fs != "autofs"}


def pontos_fstab(arq="/etc/fstab"):
    """Os pontos de montagem do /etc/fstab (inclusive os "nofail"/automount, que podem estar desmontados)."""
    try:
        with open(arq, encoding="utf-8", errors="replace") as f:
            linhas = f.read().splitlines()
    except OSError:
        return set()
    pontos = set()
    for linha in linhas:
        p = linha.split("#", 1)[0].split()
        if len(p) >= 2 and p[1].startswith("/"):
            pontos.add(_desescapar(p[1]).rstrip("/") or "/")
    return pontos


def discos(dados=None, monts=None):
    """Os armazenamentos: [(ponto de montagem, removível?)]: o sistema (/) primeiro, depois `dados` (o D:),
    os outros, e por último os removíveis (pendrive, HD externo, em /run/media/você/...).
    Um aparelho aparece uma vez só: o btrfs monta o MESMO disco em /, /home, /var/log...
    monts: o montagens() já lido (senão lê agora)."""
    linhas = list(montagens() if monts is None else monts)
    linhas.sort(key=lambda p: p[1] != "/")              # a linha do / primeiro (é ela que fica)
    vistos, achados = set(), []
    for dev, ponto, fs in linhas:
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


def lugares(checar=True):
    """As pastas padrão que existem, na ordem do "Meu computador": Desktop, Downloads, Documents, Pictures,
    Videos, Music (os nomes de verdade vêm do ~/.config/user-dirs.dirs, então funcionam em qualquer idioma).
    checar=False: sem conferir se existem (quem chama confere do seu jeito, com prazo)."""
    casa = os.path.expanduser("~")
    U = GLib.UserDirectory
    saida = []
    for d in (U.DIRECTORY_DESKTOP, U.DIRECTORY_DOWNLOAD, U.DIRECTORY_DOCUMENTS, U.DIRECTORY_PICTURES,
              U.DIRECTORY_VIDEOS, U.DIRECTORY_MUSIC):
        p = GLib.get_user_special_dir(d)
        if p and p != casa and p not in saida and (not checar or os.path.isdir(p)):
            saida.append(p)
    return saida
