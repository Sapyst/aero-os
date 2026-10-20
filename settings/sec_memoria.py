"""
sec_memoria.py — seção Memory (RAM, memória comprimida, swap no SSD e o ComfyUI).

Quem faz o trabalho são dois serviços; esta página só mostra o estado e liga/desliga:
  aero-memory        serviço de root: vigia a pressão de memória (PSI), pausa trabalho de fundo
                     (ex: compilação de shaders da Steam) e, em último caso, fecha o app que mais come
                     memória antes do sistema travar. Config: /etc/aero/memory.conf, gravada pelo ajudante
                     de root /usr/local/bin/aero-memory-config (só aceita chaves e valores de uma lista
                     fechada). Estado: /run/aero-memory/state.json (só números e nomes de processo).
  aero-memory-agent  serviço do usuário: quando o ComfyUI fecha, encerra o servidor dele e libera RAM/VRAM;
                     descarrega os modelos parados. Config: ~/.config/aero/memory-agent.conf (ou
                     $XDG_CONFIG_HOME/aero/..., igual ao agente). É ele quem mostra as notificações.

As configs são lidas com as MESMAS regras dos serviços (yes/no/on/off valem como 1/0, número fora da faixa
= padrão), senão um switch podia mostrar o contrário do que o serviço está usando. Sem o aero-memory
rodando, o nível (Calm/Busy/Critical) é estimado aqui com as contas dele (o swap no SSD não conta como folga).
Nomes de processo: o comm vem cortado em 15 letras ("fossilize_repla") e vira o nome amigável do agente
("Steam shader compilation"); ação com processos de OUTRO usuário (uid no evento) aparece sem os nomes.

De onde vêm os números (ler isso é barato e não pede senha):
  /proc/meminfo             RAM total e disponível
  /proc/swaps               cada swap: zram (RAM comprimida, rápida) e arquivo/partição no SSD
  /sys/block/zramN/mm_stat  quanto o zram guarda e quanto ele ocupa de RAM de verdade
  /proc/pressure/memory     PSI: % do tempo em que os apps ficaram esperando memória
  /proc/self/mountinfo, /sys/dev/block/...   o disco da "/" é cifrado? (só pro aviso de privacidade
                            antes de criar o swap no SSD)

Os números se atualizam a cada 2 s SÓ enquanto a página está na tela: o relógio liga em ao_mostrar
(e quando a página aparece, sinal "map") e desliga quando ela some (sinal "unmap") ou quando a janela
fica escondida em outro workspace/minimizada (a janela fica "suspended"; volta sozinho quando ela aparece).
Sem os serviços instalados a página não quebra: mostra "not installed" e desliga os controles.
"""
import json
import math
import os
import re
import stat
import tempfile
import time

from gi.repository import Adw, GLib, Gtk

from comum import Secao, abrir, combo, em_segundo_plano, indice, pagina, rodar, rodar_como_root


def pasta_config(env=os.environ):
    """$XDG_CONFIG_HOME se existir, senão ~/.config: a mesma conta do config_path() do agente.
    Sem isso a página gravaria um arquivo que o agente nunca lê."""
    return env.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


AJUDANTE = "/usr/local/bin/aero-memory-config"       # ajudante de root (caminho absoluto, nada de PATH)
ESTADO = "/run/aero-memory/state.json"
CONF_GUARDA = "/etc/aero/memory.conf"
AGENTE = os.path.expanduser("~/.local/bin/aero-memory-agent")
UNIDADES_AGENTE = (os.path.expanduser("~/.config/systemd/user/aero-memory-agent.service"),
                   "/etc/systemd/user/aero-memory-agent.service", "/usr/lib/systemd/user/aero-memory-agent.service")
CONF_AGENTE = os.path.join(pasta_config(), "aero", "memory-agent.conf")
ESTADO_AGENTE = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                             "aero-memory-agent.json")
ARQUIVO_SSD = "/swap/swapfile"    # o único swap de disco que o aero-swap-setup cria e aceita mexer

PADRAO_GUARDA = {"enabled": "1", "pause_background": "1", "kill_on_critical": "1",
                 "warn_available": "15", "critical_available": "4"}
PADRAO_AGENTE = {"comfy_free_on_close": "1", "comfy_idle_unload_min": "15", "comfy_free_for_games": "1",
                 "notify": "1"}
OCIOSO = [("Never", "0"), ("After 5 minutes", "5"), ("After 15 minutes", "15"), ("After 30 minutes", "30"),
          ("After 1 hour", "60")]
TAMANHOS_SSD = (8, 16, 32)                          # GB; o ajudante aceita 4..64
NIVEIS = {"normal": ("Calm", "success"), "warning": ("Busy", "warning"), "critical": ("Critical", "error")}
VIVO_S = 15               # state.json mais velho que isso = o serviço não está escrevendo
LIMITE = 64 * 1024        # nenhum arquivo que a página lê passa disso
SEGUNDOS = 2              # intervalo do relógio

# Nomes com cara de gente: os mesmos do FRIENDLY do aero-memory-agent (a notificação e a página falam
# igual). O kernel corta o comm em 15 letras ("fossilize_repla"), então a comparação é pelo começo.
AMIGAVEIS = {"fossilize_replay": "Steam shader compilation"}
COMM_LEN = 15
NOME_PROCESSO = re.compile(r"[A-Za-z0-9._+-]{1,32}")     # o NAME_RE do aero-memory (background=, protect=)
# As contas do nível sem o serviço, iguais às do aero-memory (ZRAM_GAIN, THRASH_FULL, THRASH_FAST_PCT)
ZRAM_VALE = 2 / 3         # encher o zram custa ~1/3 em RAM: o espaço livre nele vale 2/3 de folga
TRAVANDO_FULL = 30.0      # PSI full acima disso com o swap rápido no fim = travando
TRAVANDO_RAPIDO = 5.0     # "no fim" = o swap rápido dá menos que 5% da RAM


# ---------- leitura segura (nunca trava a janela) ----------

def ler_texto(caminho, limite=LIMITE):
    """Lê um arquivo pequeno. None se não existir, não for arquivo comum (FIFO, aparelho: O_NONBLOCK
    garante que abrir não trava) ou passar de `limite` bytes."""
    try:
        fd = os.open(caminho, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        partes, total = [], 0
        while total <= limite:
            pedaco = os.read(fd, 16384)
            if not pedaco:
                break
            partes.append(pedaco)
            total += len(pedaco)
        if total > limite:
            return None
        return b"".join(partes).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def ler_json(texto):
    """JSON -> dicionário, ou None (texto vazio, quebrado, fundo demais ou que não é um objeto)."""
    if not texto:
        return None
    try:
        dados = json.loads(texto)
    except (ValueError, RecursionError):
        return None
    return dados if isinstance(dados, dict) else None


def num(valor):
    """Só números de verdade (nada de bool, NaN, infinito, inteiro gigante ou texto)."""
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        return None
    try:
        valor = float(valor)
    except OverflowError:
        return None
    return valor if math.isfinite(valor) else None


def limpar(texto, maximo=32):
    """Texto vindo de fora: só caracteres visíveis (tira quebras, controles e truques de direção), curto."""
    if not isinstance(texto, str):
        return ""
    return "".join(c for c in texto if c.isprintable()).strip()[:maximo]


# ---------- /proc e /sys ----------

def ler_meminfo(texto):
    """/proc/meminfo -> {"MemTotal": kB, "MemAvailable": kB, ...}"""
    valores = {}
    for linha in (texto or "").splitlines():
        chave, _, resto = linha.partition(":")
        partes = resto.split()
        if partes and re.fullmatch(r"[0-9]{1,20}", partes[0]):
            valores[chave.strip()] = int(partes[0])
    return valores


def ler_psi(texto):
    """/proc/pressure/memory -> {"some": avg10, "full": avg10} (em %; None se o kernel não tiver PSI).
    Como o aero-memory: valor fora de 0..100 (ou nan) não é leitura de verdade e fica de fora."""
    psi = {"some": None, "full": None}
    for linha in (texto or "").splitlines():
        partes = linha.split()
        if partes and partes[0] in psi:
            for campo in partes[1:]:
                if campo.startswith("avg10="):
                    try:
                        valor = float(campo[6:])
                    except ValueError:
                        continue
                    if 0.0 <= valor <= 100.0:
                        psi[partes[0]] = valor
    return psi


def ler_swaps(texto):
    """/proc/swaps -> [{"nome", "tipo", "kb", "usado", "prioridade", "zram"}] (tamanhos em kB)."""
    itens = []
    for linha in (texto or "").splitlines()[1:]:          # a 1ª linha é o cabeçalho
        p = linha.split()
        if len(p) < 5:
            continue
        try:
            kb, usado, prioridade = int(p[-3]), int(p[-2]), int(p[-1])
        except ValueError:
            continue
        nome = " ".join(p[:-4])
        itens.append({"nome": nome, "tipo": p[-4], "kb": kb, "usado": usado, "prioridade": prioridade,
                      "zram": re.fullmatch(r"/dev/zram\d{1,3}", nome) is not None})
    return itens[:32]


def ler_mm_stat(texto):
    """mm_stat do zram -> (dados guardados, depois de comprimir, RAM usada de verdade) em bytes, ou None."""
    p = (texto or "").split()
    if len(p) < 3 or not all(x.isdigit() for x in p[:3]):
        return None
    return int(p[0]), int(p[1]), int(p[2])


def mm_stat_de(swap):
    """Lê o mm_stat do aparelho zram de uma linha de /proc/swaps (só /dev/zramN, nunca outro caminho)."""
    if not swap["zram"]:
        return None
    return ler_mm_stat(ler_texto(f"/sys/block/{swap['nome'][5:]}/mm_stat"))


def disco_da_raiz(mountinfo):
    """/proc/self/mountinfo -> o aparelho de onde vem a "/" ("/dev/nvme0n1p2"), ou None. Vale a última
    montagem em "/" (a de cima). O swap que o Aero cria (/swap/swapfile) fica nesse mesmo disco."""
    fonte = None
    for linha in (mountinfo or "").splitlines():
        c = linha.split()
        # id pai maj:min raiz PONTO opções [opcionais...] - tipo FONTE opções
        if len(c) < 10 or c[4] != "/" or "-" not in c[6:]:
            continue
        i = c.index("-", 6)
        if len(c) > i + 2:
            fonte = c[i + 2]
    return fonte if fonte and fonte.startswith("/dev/") else None


def cifrado(no, fundo=0):
    """O aparelho de bloco desta pasta do /sys só grava cifrado? Sim se ele for um dm-crypt (dm/uuid
    começa com "CRYPT-", o mesmo teste do lsblk) ou se TUDO embaixo dele (slaves: LVM, RAID) for."""
    uuid = ler_texto(os.path.join(no, "dm", "uuid"), 4096)
    if uuid is not None and uuid.startswith("CRYPT-"):
        return True
    if fundo >= 8:
        return False
    try:
        baixo = sorted(os.listdir(os.path.join(no, "slaves")))[:64]
    except OSError:
        return False                      # partição ou disco comum: nada embaixo
    return bool(baixo) and all(cifrado(os.path.join(no, "slaves", n), fundo + 1) for n in baixo)


def raiz_cifrada(mountinfo, sys_dir="/sys"):
    """O disco da "/" (onde fica o swap do SSD) é cifrado? True/False, ou None se não deu pra saber.
    Só lê arquivos (nada de lsblk): é rápido e pode rodar na hora de abrir o diálogo."""
    fonte = disco_da_raiz(mountinfo)
    if fonte is None:
        return None
    try:
        st = os.stat(fonte)
    except OSError:
        return None
    if not stat.S_ISBLK(st.st_mode):
        return None
    no = os.path.join(sys_dir, "dev", "block", f"{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}")
    return cifrado(no) if os.path.isdir(no) else None


# ---------- textos ----------

def tamanho(kb):
    """kB -> "512 MB" / "12.3 GB" (base 1024, como a memória é contada)."""
    if kb is None:
        return "?"
    mb = max(kb, 0) / 1024
    return f"{mb:.0f} MB" if mb < 1024 else f"{mb / 1024:.1f} GB"


def ha_quanto(t, agora):
    if t is None:
        return ""
    d = agora - t
    if d < -60:                     # relógio do arquivo no futuro: melhor não dizer nada
        return ""
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    dias = int(d // 86400)
    return "1 day ago" if dias == 1 else f"{dias} days ago"


def nome_bonito(nome, conhecidos=()):
    """Nome de processo (o comm, que o kernel corta em 15 letras) -> o nome amigável ("Steam shader
    compilation"), senão o nome inteiro da lista background=/protect= que começa igual (a mesma
    comparação do aero-memory), senão ele mesmo."""
    curto = nome[:COMM_LEN]
    for completo, amigavel in AMIGAVEIS.items():
        if completo[:COMM_LEN] == curto:
            return amigavel
    for completo in conhecidos:
        if completo[:COMM_LEN] == curto:
            return completo
    return nome


def nomes_de(acao, conhecidos=()):
    """'name' ou 'names' de uma ação -> "a, b, c" (no máximo 3, já limpos e com o nome bonito)."""
    nomes = acao.get("names")
    if not isinstance(nomes, list):
        nomes = [acao.get("name")]
    nomes = [n for n in (limpar(x) for x in nomes[:20]) if n]
    vistos = list(dict.fromkeys(nome_bonito(n, conhecidos) for n in nomes))
    texto = ", ".join(vistos[:3])
    return texto + (f" +{len(vistos) - 3}" if len(vistos) > 3 else "")


def de_outra_pessoa(acao, uid):
    """A ação foi com processos de outro usuário? O aero-memory põe o uid do dono em paused, resumed e
    killed (ele cuida de todos os usuários). Sem uid (guarda antigo, ação do sistema): vale como nossa."""
    dono = acao.get("uid")
    return uid is not None and isinstance(dono, int) and not isinstance(dono, bool) and dono != uid


def descrever_acao(acao, agora, conhecidos=(), uid=None):
    """last_action (do aero-memory ou do agente) -> "Paused Steam shader compilation (7 processes, 5.1 GB)
    · 2 min ago". uid = quem está olhando: ação com processos de outro usuário sai sem os nomes."""
    if isinstance(acao, str):
        return limpar(acao, 80)
    if not isinstance(acao, dict) or acao.get("dry_run") is True:     # --dry-run só finge: não conta
        return ""
    tipo = limpar(acao.get("kind") or acao.get("action") or "", 24).lower()
    outro = de_outra_pessoa(acao, uid)
    nomes = "" if outro else nomes_de(acao, conhecidos)
    mb = num(acao.get("mb"))
    gb = tamanho(mb * 1024) if mb else ""
    qtd = num(acao.get("count"))
    if tipo == "paused":
        extra = [f"{int(qtd)} processes"] if qtd and qtd > 1 else []
        extra += [gb] if gb else []
        texto = ("Paused another user's background work" if outro else f"Paused {nomes or 'background work'}")
        texto += f" ({', '.join(extra)})" if extra else ""
    elif tipo == "resumed":
        texto = ("Resumed another user's paused work" if outro else
                 f"Resumed {nomes}" if nomes else "Resumed paused work")
    elif tipo == "killed":
        quem = "another user's app" if outro else nomes or "an app"
        texto = f"Closed {quem}" + (f" (it was using {gb})" if gb else "") + " to prevent a freeze"
    elif tipo == "oom_kill":
        # o próprio kernel fechou (o OOM killer): o aero-memory só conta quantas vezes (no máximo 1000)
        texto = (f"The system closed {int(qtd)} apps (out of memory)" if qtd and 1 < qtd <= 1000 else
                 "The system closed an app (out of memory)")
    elif tipo == "pressure":
        texto = "Memory was running low"
    elif tipo in ("unload", "unloaded", "idle_unload", "free", "freed"):
        texto = "Unloaded idle ComfyUI models"
    elif tipo in ("games", "free_for_games", "freed_for_game", "gpu"):
        texto = "Freed GPU memory for another app"
    elif tipo in ("closed", "close", "stopped", "stop", "orphan"):
        texto = "Stopped the ComfyUI server" + (f" and freed {gb}" if gb else "")
    elif tipo:
        texto = tipo.replace("_", " ").capitalize()
    else:
        return ""
    quando = ha_quanto(num(acao.get("t")), agora)
    return f"{texto} · {quando}" if quando else texto


def valor_conf(v):
    """Valor de config vindo do state.json (bool, número ou texto) -> "1", "15"... ou None."""
    if isinstance(v, bool):
        return "1" if v else "0"
    if num(v) is not None and 0 <= v < 10000:
        return str(int(v))
    if isinstance(v, str) and re.fullmatch(r"\d{1,4}", v.strip()):
        return v.strip()
    return None


def ler_estado(texto, agora):
    """state.json do aero-memory -> só o que a página usa, já conferido. O arquivo é do root,
    mas mesmo assim é tratado como dado: só passam números, nomes curtos e níveis conhecidos."""
    est = {"existe": False, "vivo": False, "nivel": None, "config": {}, "conhecidos": [], "pausados": [],
           "uids_pausados": None, "ultima": None}
    d = ler_json(texto)
    if d is None:
        return est
    t = num(d.get("t"))
    est["existe"] = True
    est["vivo"] = t is not None and -5 <= agora - t <= VIVO_S
    nivel = d.get("level")
    est["nivel"] = nivel if isinstance(nivel, str) and nivel in NIVEIS else None
    cfg = d.get("config")
    if isinstance(cfg, dict):
        est["config"] = {k: v for k in PADRAO_GUARDA if (v := valor_conf(cfg.get(k))) is not None}
        # nomes inteiros de background=/protect=: o comm cortado em 15 letras volta a ser o nome todo
        conhecidos = []
        for k in ("background", "protect"):
            if isinstance(cfg.get(k), list):
                conhecidos += [x for x in cfg[k][:20] if isinstance(x, str) and NOME_PROCESSO.fullmatch(x)]
        est["conhecidos"] = list(dict.fromkeys(conhecidos))
    pausados = d.get("paused")
    if isinstance(pausados, list):
        nomes = [nome_bonito(n, est["conhecidos"]) for n in (limpar(x) for x in pausados[:50]) if n]
        est["pausados"] = list(dict.fromkeys(nomes))[:10]
    uids = d.get("paused_uids")           # de quem são os pausados (só com um aero-memory que escreve isso)
    if isinstance(uids, list) and all(isinstance(u, int) and not isinstance(u, bool) for u in uids[:256]):
        est["uids_pausados"] = uids[:256]
    if isinstance(d.get("last_action"), (dict, str)):
        est["ultima"] = d["last_action"]
    return est


def folga_swap(swaps):
    """Bytes de swap que contam como folga, com as contas do swap_headroom do aero-memory: só a camada
    mais rápida (a de maior prioridade, que o kernel enche primeiro: o zram), e o zram livre vale
    ZRAM_VALE dele. O swap no SSD (prioridade menor) não conta: quando ele está em uso, o sistema já
    troca páginas com o disco."""
    if not swaps:
        return 0
    topo = max(s["prioridade"] for s in swaps)
    folga = 0
    for s in swaps:
        if s["prioridade"] == topo:
            livre = (s["kb"] - min(s["usado"], s["kb"])) * 1024
            folga += int(livre * ZRAM_VALE) if s["nome"].startswith("/dev/zram") else livre
    return folga


def nivel_local(mem, psi, swaps=None, aviso=15, critico=4):
    """Sem o serviço rodando, estima o nível aqui mesmo: as contas do decide_level do aero-memory,
    partindo de "normal" (sem a histerese). swaps = ler_swaps(/proc/swaps), ou None se não deu pra ler
    (aí, como lá, a folga do swap é o SwapFree todo).
      critical: disponível < critico E (swap < 10% livre ou PSI full > 10),
                ou travando: PSI full > 30, swap rápido < 5% da RAM e disponível < aviso
      warning:  (disponível + swap rápido) < aviso, ou PSI some > 20"""
    total = mem.get("MemTotal")
    if not total or "MemAvailable" not in mem:
        return None
    total *= 1024                                   # tudo em bytes, como lá (as contas batem igualzinho)
    disp = min(mem["MemAvailable"] * 1024, total)
    swap_total = mem.get("SwapTotal", 0) * 1024
    swap_livre = min(mem.get("SwapFree", 0) * 1024, swap_total)
    validos = None if swaps is None else [s for s in swaps if s["kb"] >= 0 and s["usado"] >= 0]
    # tabela vazia com swap ligado = formato estranho: melhor o SwapFree todo do que folga zero
    rapida = folga_swap(validos) if validos is not None and (validos or swap_total == 0) else swap_livre
    some, full = psi.get("some") or 0.0, psi.get("full") or 0.0
    disp_pct = disp / total * 100
    fracao_livre = swap_livre / swap_total if swap_total > 0 else 0.0
    if disp_pct < critico and (fracao_livre < 0.10 or full > 10):
        return "critical"
    if full > TRAVANDO_FULL and rapida / total * 100 < TRAVANDO_RAPIDO and disp_pct < aviso:
        return "critical"
    if (disp + rapida) / total * 100 < aviso or some > 20:
        return "warning"
    return "normal"


def texto_guarda(instalado, est, servico, uid=None):
    """(título, subtítulo) da linha de estado do aero-memory. servico: True/False/None (ainda não sei).
    uid = quem está olhando: com algo de outro usuário pausado, não diz os nomes."""
    if not instalado:
        return ("Memory guard is not installed",
                "Install the system part of Aero OS memory management (system/memory/install.sh) "
                "to use these options")
    if est["vivo"]:
        if est["config"].get("enabled") == "0":
            return "Status", "Off: memory isn't being watched"
        texto = "Active · watching memory pressure"
        if est["pausados"]:
            uids = est.get("uids_pausados")
            if uid is not None and uids and any(u != uid for u in uids):
                texto += " · holding background work until memory frees up"
            else:
                texto += " · holding " + ", ".join(est["pausados"][:3]) + " until memory frees up"
        return "Status", texto
    if servico is True:
        return "Status", "Running"
    if servico is False:
        return "Status", "Not running: the aero-memory service is stopped"
    return "Status", "Checking…"


def texto_fechar(com_agente, servico_agente, agente):
    """Subtítulo de "Close the biggest app". O serviço de root não mostra nada na tela: a notificação vem
    do aero-memory-agent. Então só promete notificação com o agente instalado, não parado e com notify=1."""
    base = "Last resort, only when memory is about to run out. "
    if com_agente and servico_agente is not False and agente.get("notify") == "1":
        return base + "You get a notification saying what was closed"
    return base + "What was closed shows up under Last action"


def motivo_da_falha(r):
    """Por que o comando de root não deu certo, em texto curto (vai num aviso sem markup).
    sudo sai com 1 quando a senha é cancelada. O aero-memory-config sai com 2 (recusado), 3 (agora não)
    ou 4 (um passo falhou) e põe o motivo na última linha do stderr; os prefixos saem aqui:
    "not now: aero-swap-setup: error: not enough free space ..." -> "not enough free space ...".
    r também pode ser uma exceção (o sudo nem rodou)."""
    codigo = getattr(r, "returncode", None)
    if codigo == 1:
        return "password cancelled"
    texto = ""
    if codigo in (2, 3, 4):
        saida = getattr(r, "stderr", "")
        linhas = [x.strip() for x in (saida if isinstance(saida, str) else "").splitlines() if x.strip()]
        texto = linhas[-1] if linhas else ""
        for prefixo in ("refused:", "not now:", "failed:", "aero-swap-setup:", "error:"):
            if texto.lower().startswith(prefixo):
                texto = texto[len(prefixo):].strip()
        texto = limpar(texto, 200)
    return texto or "something went wrong"


def texto_comfy(instalado, est, servico, agora):
    """(título, subtítulo) da linha de estado do ComfyUI, do arquivo de estado do aero-memory-agent."""
    if not instalado:
        return "ComfyUI helper is not installed", "Install aero-memory-agent to free ComfyUI's memory on its own"
    if servico is False:
        return "ComfyUI", "The memory helper (aero-memory-agent) isn't running"
    comfy = est.get("comfy") if isinstance(est, dict) else None
    if not isinstance(comfy, dict):
        return "ComfyUI", "Not running"
    if comfy.get("running") is True:
        partes = ["Running"]
        if comfy.get("models_loaded") is True:
            partes.append("models loaded")
        elif comfy.get("models_loaded") is False:
            partes.append("no models loaded")
        ocioso = num(comfy.get("idle_s"))
        if ocioso is not None and ocioso >= 60:
            partes.append(f"idle {int(ocioso // 60)} min")
        linhas = [" · ".join(partes)]
    else:
        linhas = ["Not running"]
    acao = descrever_acao(comfy.get("last_action"), agora)
    if acao:
        linhas.append(f"Last: {acao}")
    return "ComfyUI", "\n".join(linhas)


def opcoes_ocioso(valor):
    """As opções de "Unload models when idle"; um valor fora da lista (editado à mão) ganha a sua."""
    if not re.fullmatch(r"\d{1,4}", valor or "") or any(v == str(int(valor)) for _, v in OCIOSO):
        return list(OCIOSO)
    n = int(valor)
    return sorted(OCIOSO + [(f"After {n} minutes", str(n))], key=lambda o: int(o[1]))


def ssd_gerenciavel(swaps):
    """(mostrar o tamanho?, tamanho atual em GB ou None). Só o swap que o Aero faz (/swap/swapfile).
    Com qualquer outro swap de disco ligado (partição, /swapfile feito à mão) o aero-swap-setup sempre
    recusa mexer, então a página nem oferece: pedir a senha pra algo que não vai dar certo não serve."""
    disco = [s for s in swaps if not s["zram"]]
    if any(s["nome"] != ARQUIVO_SSD for s in disco):
        return False, None
    if not disco:
        return True, None
    return True, max(1, round(disco[0]["kb"] / 1024 / 1024))     # /proc/swaps não repete um arquivo


def aviso_privacidade(cifrado_):
    """Frase de privacidade do diálogo que CRIA o swap no SSD: o que vai pra ele fica gravado no disco.
    cifrado_: o disco é cifrado (True/False) ou não deu pra saber (None)."""
    if cifrado_ is True:
        return ""
    if cifrado_ is False:
        return (" This disk isn't encrypted, so while things sit in the SSD swap, anyone who has the "
                "computer could read them from the disk.")
    return " If this disk isn't encrypted, anyone who has the computer could read what sits in the SSD swap."


def opcoes_ssd(atual):
    base = [(f"{n} GB", n) for n in TAMANHOS_SSD]
    if atual is None:
        return [("Not set up", None)] + base
    if atual not in TAMANHOS_SSD:
        base = sorted(base + [(f"{atual} GB", atual)], key=lambda o: o[1])
    return base


def camadas(swaps):
    """Linhas do grupo Swap: [(chave, título, subtítulo)], da mais rápida (prioridade maior) pra mais lenta."""
    if not swaps:
        return [(("nenhum",), "No swap is set up",
                 "When memory fills up there's nowhere to move things to, and the system can freeze")]
    ordem = sorted(swaps, key=lambda s: -s["prioridade"])
    linhas = []
    for i, s in enumerate(ordem):
        titulo = "Compressed RAM · fast" if s["zram"] else "SSD · for things not used in a while"
        partes = [tamanho(s["kb"]), f"priority {s['prioridade']}"]
        if i == 0 and len(ordem) > 1 and s["prioridade"] > ordem[1]["prioridade"]:
            partes.append("used first")
        if not s["zram"] and s["nome"] != ARQUIVO_SSD:
            partes.append("managed outside Aero OS")      # o Aero não muda o tamanho desse
        linhas.append(((s["nome"], s["kb"], s["prioridade"]), titulo, " · ".join(partes)))
    return linhas


def resumo_zram(swaps, stats):
    """(subtítulo, fração usada ou None) da linha "Compressed memory". stats = mm_stat de cada zram."""
    zram = [s for s in swaps if s["zram"]]
    if not zram:
        return "Not set up", None
    total, usado = sum(s["kb"] for s in zram), sum(s["usado"] for s in zram)
    texto = f"{tamanho(usado)} used of {tamanho(total)}"
    validos = [x for x in stats if x]
    guardado = sum(x[0] for x in validos)
    ram = sum(x[2] for x in validos)
    if guardado >= 1 << 20 and 0 < ram < guardado:      # com quase nada guardado a conta não diz nada
        texto += f" · takes {tamanho(ram / 1024)} of RAM ({guardado / ram:.1f}× smaller)"
    return texto, (usado / total if total else 0)


def resumo_ssd(swaps):
    disco = [s for s in swaps if not s["zram"]]
    if not disco:
        return "Not set up", None
    total, usado = sum(s["kb"] for s in disco), sum(s["usado"] for s in disco)
    return f"{tamanho(usado)} used of {tamanho(total)}", (usado / total if total else 0)


# ---------- config ----------

# As regras dos serviços (aero-memory: _bool/_pct; aero-memory-agent: BOOL_VALUES e o limite de minutos).
# Cada uma devolve o valor do jeito que a página usa ("1"/"0", número sem zero à esquerda) ou None (ruim).

def regra_sim_nao(v):
    v = v.strip().lower()
    return "1" if v in ("1", "yes", "true", "on") else "0" if v in ("0", "no", "false", "off") else None


def regra_porcento(v):
    v = v.strip()
    return str(int(v)) if re.fullmatch(r"[0-9]{1,2}", v) and 1 <= int(v) <= 50 else None


def regra_minutos(v):
    v = v.strip()
    return str(int(v)) if re.fullmatch(r"[0-9]{1,5}", v) and int(v) <= 1440 else None


REGRAS = {"enabled": regra_sim_nao, "pause_background": regra_sim_nao, "kill_on_critical": regra_sim_nao,
          "warn_available": regra_porcento, "critical_available": regra_porcento,
          "comfy_free_on_close": regra_sim_nao, "comfy_free_for_games": regra_sim_nao, "notify": regra_sim_nao,
          "comfy_idle_unload_min": regra_minutos}


def conf_valida(texto, padrao, ruim_vira_padrao=True):
    """Arquivo CHAVE=valor -> só as chaves de `padrao`, já conferidas por REGRAS (como os serviços leem:
    500 linhas no máximo, "#" começa comentário, vale a última linha de cada chave).
    Valor ruim: o aero-memory volta ao padrão (ruim_vira_padrao=True); o agente ignora a linha e fica
    com o que já tinha (False)."""
    valores = {}
    for linha in (texto or "").splitlines()[:500]:
        k, sep, v = linha.split("#", 1)[0].partition("=")
        k = k.strip()
        if not sep or k not in padrao:
            continue
        bom = REGRAS[k](v)
        if bom is not None:
            valores[k] = bom
        elif ruim_vira_padrao:
            valores.pop(k, None)
    return valores


def config_guarda(valores):
    """Padrões + valores conferidos. critical_available >= warn_available: os dois voltam ao padrão
    (o aero-memory faz igual; senão a estimativa de nível daqui usaria limites que ele não usa)."""
    cfg = dict(PADRAO_GUARDA)
    for k, v in valores.items():
        if k in cfg and (bom := REGRAS[k](v)) is not None:
            cfg[k] = bom
    if int(cfg["critical_available"]) >= int(cfg["warn_available"]):
        for k in ("warn_available", "critical_available"):
            cfg[k] = PADRAO_GUARDA[k]
    return cfg


def atualizar_conf(texto, chaves):
    """Muda SÓ as chaves pedidas e mantém o resto do arquivo (comentários e outras chaves).
    Chave/valor fora do formato -> ValueError (nunca grava nada estranho)."""
    for k, v in chaves.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", k) or not re.fullmatch(r"[A-Za-z0-9._,+-]{0,64}", str(v)):
            raise ValueError("bad key or value")
    faltando = {k: str(v) for k, v in chaves.items()}
    linhas, vistas = [], set()
    for linha in (texto or "").splitlines():
        k = linha.split("=", 1)[0].strip() if "=" in linha and not linha.lstrip().startswith("#") else None
        if k in chaves:
            if k in vistas:                 # chave repetida: fica só a primeira (sem ambiguidade)
                continue
            vistas.add(k)
            linha = f"{k}={faltando.pop(k)}"
        linhas.append(linha)
    if not linhas:
        linhas = ["# Gerado pelo Settings → Memory. Lido pelo aero-memory-agent."]
    linhas += [f"{k}={v}" for k, v in faltando.items()]
    return "\n".join(linhas) + "\n"


def gravar_conf(caminho, chaves):
    """Grava chaves no arquivo do usuário sem nunca deixar ele pela metade (temporário + rename).
    Se o arquivo for um link (dotfiles), grava no destino dele e o link continua."""
    alvo = os.path.realpath(caminho)
    pasta = os.path.dirname(alvo)
    os.makedirs(pasta, exist_ok=True)
    texto = ler_texto(alvo)
    if texto is None and os.path.lexists(alvo):
        raise OSError("config file is not readable")
    novo = atualizar_conf(texto, chaves)
    fd, tmp = tempfile.mkstemp(prefix=".memory-agent.", dir=pasta)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(novo)
        os.replace(tmp, alvo)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def agente_instalado():
    return os.path.exists(AGENTE) or any(os.path.exists(u) for u in UNIDADES_AGENTE)


def ajudante_tem(comando):
    """O ajudante de root sabe esse comando? (ele é um script pequeno; o tamanho do swap é opcional)."""
    texto = ler_texto(AJUDANTE, 512 * 1024)
    return texto is not None and comando in texto


# ---------- widgets ----------

def escolhido(linha, opcoes):
    """Valor escolhido num combo; None se a posição não existir (ex: no meio de uma troca de lista)."""
    i = linha.get_selected()
    return opcoes[i][1] if 0 <= i < len(opcoes) else None


def barra():
    """Barra de uso: cor normal até 85%, cor de alerta acima disso."""
    b = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
    b.set_size_request(160, -1)
    for nome in ("low", "high", "full"):
        b.remove_offset_value(nome)
    b.add_offset_value("high", 0.85)       # a classe é a do 1º limite >= valor: "high" = cor de destaque
    b.add_offset_value("low", 1.0)         # "low" = cor de alerta (laranja)
    return b


def fora_do_sinal(funcao):
    """Roda funcao() logo depois, fora do sinal atual. Desfazer um switch DENTRO do próprio notify::active
    faz o GTK entregar o aviso da volta depois que a trava `carregando` já saiu (e o handler roda de novo
    com o valor antigo). Fora do sinal, o aviso chega na hora, ainda com a trava."""
    GLib.idle_add(lambda: (funcao(), GLib.SOURCE_REMOVE)[1])


def suspensa(janela):
    """A janela está escondida (outro workspace, minimizada)? No Wayland ela continua "mapped", mas o
    compositor avisa que ela está suspensa (Gtk.Window.is_suspended, GTK 4.12+). Sem essa informação: não."""
    return janela is not None and hasattr(janela, "is_suspended") and janela.is_suspended()


def por_subtitulo(linha, texto):
    if linha.get_subtitle() != texto:      # só mexe se mudou (a cada 2 s, nada de redesenhar à toa)
        linha.set_subtitle(texto)


def por_barra(b, fracao):
    b.set_visible(fracao is not None)
    if fracao is not None and abs(b.get_value() - fracao) > 0.001:
        b.set_value(min(max(fracao, 0.0), 1.0))


class SecaoMemoria(Secao):
    id = "memoria"
    titulo = "Memory"
    icone = "drive-harddisk-system-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        # O construtor só monta widgets: nada de ler arquivos nem ligar relógio (a busca do Settings
        # cria todas as seções de uma vez, mesmo as que não estão na tela).
        self.relogio = None           # id do relógio de 2 s (só existe com a página na tela)
        self.raiz = self.id_raiz = None    # a janela vigiada (sinal notify::suspended) e o id da ligação
        self.recarga = None           # id do atraso antes de reiniciar o agente (junta cliques seguidos)
        self.carregando = False       # mudanças feitas pelo código não viram comandos
        self.ocupado = False          # um comando de root por vez = uma senha por vez
        self.checando = False         # systemctl is-active em andamento
        self.checado_em = 0.0         # quando foi a última checagem (no máximo 1 a cada 10 s)
        self.servico = self.servico_agente = None    # True/False; None = ainda não sei
        self.instalado = self.com_agente = False
        self.guarda = dict(PADRAO_GUARDA)
        self.agente = dict(PADRAO_AGENTE)
        self.opcoes_ocioso = list(OCIOSO)
        self.opcoes_ssd = opcoes_ssd(None)
        self.ssd_atual = None
        self.chaves_swap = None       # quais swaps existem (o grupo Swap só redesenha se mudar)
        self.linhas_swap = []
        conteudo = Adw.PreferencesPage()

        # 1. Agora
        agora = Adw.PreferencesGroup(title="Right now")
        self.ram, self.barra_ram = self.linha_barra("Memory (RAM)")
        self.zram, self.barra_zram = self.linha_barra("Compressed memory")
        self.ssd, self.barra_ssd = self.linha_barra("SSD swap")
        self.pressao = Adw.ActionRow(use_markup=False, title="Pressure")
        self.selo = Gtk.Label(valign=Gtk.Align.CENTER)
        self.selo.add_css_class("heading")
        self.pressao.add_suffix(self.selo)
        for r in (self.ram, self.zram, self.ssd, self.pressao):
            agora.add(r)

        # 2. Proteção contra travar (aero-memory, root)
        guarda = Adw.PreferencesGroup(
            title="Keep the system responsive",
            description="Aero OS watches memory pressure and steps in before the desktop can freeze.")
        self.status = Adw.ActionRow(use_markup=False, title="Status")
        guarda.add(self.status)
        self.chaves = {}
        for chave, titulo, sub in (
                ("enabled", "Memory guard", "Watches memory and acts before the system runs out"),
                ("pause_background", "Pause background work when memory is low",
                 "Jobs like Steam's shader compilation wait until there's room again, then continue"),
                ("kill_on_critical", "Close the biggest app before a freeze",
                 texto_fechar(False, None, PADRAO_AGENTE))):     # o certo vem em atualizar()
            s = Adw.SwitchRow(use_markup=False, title=titulo, subtitle=sub)
            s.connect("notify::active", self.ao_trocar_guarda, chave)
            self.chaves[chave] = s
            guarda.add(s)
        self.ultima = Adw.ActionRow(use_markup=False, title="Last action", subtitle="Nothing yet")
        guarda.add(self.ultima)

        # 3. ComfyUI (aero-memory-agent, do usuário: sem senha)
        comfy = Adw.PreferencesGroup(
            title="ComfyUI",
            description="ComfyUI keeps its models in memory. These options free RAM and GPU memory "
                        "when you're not using it.")
        self.comfy = Adw.ActionRow(use_markup=False, title="ComfyUI")
        self.ao_fechar = Adw.SwitchRow(
            use_markup=False, title="Free everything when ComfyUI closes",
            subtitle="Ends its background server and frees its RAM and GPU memory when the last window closes")
        self.ocioso = combo("Unload models when idle", OCIOSO, indice(OCIOSO, "15"),
                            "Frees memory while ComfyUI stays open. Models load again on the next run")
        self.ocioso.set_use_markup(False)
        self.jogos = Adw.SwitchRow(
            use_markup=False, title="Free GPU memory for games",
            subtitle="Unloads idle models when another app starts using the NVIDIA graphics card")
        self.ao_fechar.connect("notify::active", lambda r, *_: self.ao_trocar_agente(
            "comfy_free_on_close", "1" if r.get_active() else "0"))
        self.jogos.connect("notify::active", lambda r, *_: self.ao_trocar_agente(
            "comfy_free_for_games", "1" if r.get_active() else "0"))
        self.ocioso.connect("notify::selected", lambda r, *_: self.carregando or self.ao_trocar_agente(
            "comfy_idle_unload_min", escolhido(r, self.opcoes_ocioso)))
        for r in (self.comfy, self.ao_fechar, self.ocioso, self.jogos):
            comfy.add(r)

        # 4. Swap: as camadas (rápida -> lenta) e o tamanho do swap no SSD
        self.swap = Adw.PreferencesGroup(
            title="Swap",
            description="When memory fills up, things you haven't used in a while move to swap. "
                        "The fast compressed tier fills first; the SSD holds the rest.")
        self.tamanho_ssd = combo("SSD swap size", self.opcoes_ssd, 0,
                                 "More room for things not used in a while. Uses that much disk space")
        self.tamanho_ssd.set_use_markup(False)
        self.tamanho_ssd.set_visible(False)
        self.tamanho_ssd.connect("notify::selected", self.ao_trocar_ssd)
        self.swap.add(self.tamanho_ssd)

        for g in (agora, guarda, comfy, self.swap):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)
        # Relógio só com a página na tela: "map" = apareceu, "unmap" = sumiu (outra seção, janela fechada).
        # Janela em outro workspace ou minimizada não dá "unmap": isso vem pelo notify::suspended dela.
        self.pagina.connect("map", lambda *_: self.ligar_relogio())
        self.pagina.connect("unmap", lambda *_: self.ao_sumir())

    @staticmethod
    def linha_barra(titulo):
        linha = Adw.ActionRow(use_markup=False, title=titulo)
        b = barra()
        linha.add_suffix(b)
        return linha, b

    # ----- relógio -----

    def ao_mostrar(self):
        self.ler_configs()
        self.checar_servicos()
        self.atualizar()
        self.ligar_relogio()

    def ao_sumir(self):
        self.parar_relogio()
        if self.recarga is not None:                 # reinício do agente ainda esperando: faz agora
            GLib.source_remove(self.recarga)
            self.recarregar_agente()

    def ligar_relogio(self):
        raiz = self.vigiar_janela()
        if self.relogio is None and not suspensa(raiz):     # janela escondida: só quando ela voltar
            self.relogio = GLib.timeout_add_seconds(SEGUNDOS, self.tique)

    def parar_relogio(self):
        if self.relogio is not None:
            GLib.source_remove(self.relogio)
            self.relogio = None

    def vigiar_janela(self):
        """Liga UMA vez o aviso de "suspensa" da janela onde a página está (se ela trocar de janela,
        solta a antiga). Devolve a janela (ou None, se a página ainda não está em nenhuma)."""
        raiz = self.pagina.get_root()
        if raiz is not self.raiz and hasattr(raiz, "is_suspended"):
            if self.raiz is not None:
                self.raiz.disconnect(self.id_raiz)
            self.raiz, self.id_raiz = raiz, raiz.connect("notify::suspended", self.ao_suspender)
        return raiz

    def ao_suspender(self, raiz, _param):
        if raiz.is_suspended():
            self.parar_relogio()
        elif self.pagina.get_mapped() and self.pagina.get_root() is raiz:    # voltou com a página na tela
            self.atualizar()
            self.ligar_relogio()

    def tique(self):
        # sumiu sem "unmap" (ex: nunca apareceu) ou a janela ficou escondida: para (o aviso religa)
        if not self.pagina.get_mapped() or suspensa(self.pagina.get_root()):
            self.relogio = None
            return GLib.SOURCE_REMOVE
        self.atualizar()
        return GLib.SOURCE_CONTINUE

    # ----- leitura -----

    def ler_guarda(self, est):
        """Config do aero-memory: o arquivo manda (lido com as regras do serviço); sem ele, o que o serviço
        diz que está usando; sem os dois, o padrão."""
        texto = ler_texto(CONF_GUARDA)
        return config_guarda(conf_valida(texto, PADRAO_GUARDA) if texto is not None else est["config"])

    def ler_configs(self):
        self.instalado = os.access(AJUDANTE, os.X_OK)
        self.com_agente = agente_instalado()
        swaps = ler_swaps(ler_texto("/proc/swaps"))
        if not self.ocupado:              # com um comando de root no meio, não desfaz o que a pessoa clicou
            self.guarda = self.ler_guarda(ler_estado(ler_texto(ESTADO), time.time()))
            self.mostrar_guarda()
            self.montar_ssd(swaps)
        self.agente = dict(PADRAO_AGENTE) | conf_valida(ler_texto(CONF_AGENTE), PADRAO_AGENTE,
                                                        ruim_vira_padrao=False)
        self.mostrar_agente()
        self.sensibilidade()

    def mostrar_guarda(self):
        self.carregando = True
        for chave, s in self.chaves.items():
            s.set_active(self.instalado and self.guarda.get(chave) == "1")
        self.carregando = False

    def mostrar_agente(self):
        self.carregando = True
        ag = self.com_agente
        self.ao_fechar.set_active(ag and self.agente["comfy_free_on_close"] == "1")
        self.jogos.set_active(ag and self.agente["comfy_free_for_games"] == "1")
        opcoes = opcoes_ocioso(self.agente["comfy_idle_unload_min"])
        if opcoes != self.opcoes_ocioso:
            self.opcoes_ocioso = opcoes
            self.ocioso.set_model(Gtk.StringList.new([t for t, _ in opcoes]))
        self.ocioso.set_selected(indice(opcoes, self.agente["comfy_idle_unload_min"] if ag else "0"))
        self.carregando = False

    def montar_ssd(self, swaps):
        mostrar, atual = ssd_gerenciavel(swaps)
        self.ssd_atual = atual
        self.carregando = True
        opcoes = opcoes_ssd(atual)
        if opcoes != self.opcoes_ssd:
            self.opcoes_ssd = opcoes
            self.tamanho_ssd.set_model(Gtk.StringList.new([t for t, _ in opcoes]))
        self.tamanho_ssd.set_selected(indice(opcoes, atual))
        self.carregando = False
        self.tamanho_ssd.set_visible(mostrar and self.instalado and ajudante_tem("swap-size"))

    def sensibilidade(self):
        livre = self.instalado and not self.ocupado
        self.chaves["enabled"].set_sensitive(livre)
        for chave in ("pause_background", "kill_on_critical"):
            self.chaves[chave].set_sensitive(livre and self.guarda.get("enabled") == "1")
        self.tamanho_ssd.set_sensitive(livre)
        for r in (self.ao_fechar, self.ocioso, self.jogos):
            r.set_sensitive(self.com_agente)

    def checar_servicos(self):
        """Os serviços estão rodando? systemctl numa thread (com limite de tempo), uma checagem por vez."""
        if self.checando or not (self.instalado or self.com_agente):
            return
        self.checando = True
        self.checado_em = time.monotonic()

        def ativo(cmd):
            try:
                return rodar(cmd, timeout=3).stdout.strip() == "active"
            except Exception:  # noqa: BLE001 — sem systemctl ou demorou: "não sei"
                return None

        def trabalho():
            return (ativo(["systemctl", "is-active", "aero-memory.service"]) if self.instalado else None,
                    ativo(["systemctl", "--user", "is-active", "aero-memory-agent.service"])
                    if self.com_agente else None)

        def depois(r):
            self.checando = False
            if isinstance(r, tuple):
                # aero-memory: sem resposta do systemctl = tratado como parado (melhor que "Checking…" pra
                # sempre). Agente: None = não sei, vale o arquivo de estado dele.
                self.servico = r[0] is True if self.instalado else None
                self.servico_agente = r[1]
                self.atualizar()
        em_segundo_plano(trabalho, depois)

    def atualizar(self):
        """Lê os números (arquivos pequenos do kernel, menos de 1 ms) e atualiza as linhas."""
        agora = time.time()
        uid = os.getuid()
        mem = ler_meminfo(ler_texto("/proc/meminfo"))
        texto_swaps = ler_texto("/proc/swaps")
        swaps = ler_swaps(texto_swaps)
        psi = ler_psi(ler_texto("/proc/pressure/memory"))
        est = ler_estado(ler_texto(ESTADO), agora)
        if self.servico is True and not est["vivo"] and time.monotonic() - self.checado_em > 10:
            self.checar_servicos()                   # parou de dar notícias: ainda está rodando?

        total, disp = mem.get("MemTotal"), mem.get("MemAvailable")
        if total and disp is not None:
            por_subtitulo(self.ram, f"{tamanho(total - disp)} used of {tamanho(total)} · "
                                    f"{tamanho(disp)} available")
            por_barra(self.barra_ram, (total - disp) / total)
        else:
            por_subtitulo(self.ram, "Not available")
            por_barra(self.barra_ram, None)
        texto, fracao = resumo_zram(swaps, [mm_stat_de(s) for s in swaps if s["zram"]])
        por_subtitulo(self.zram, texto)
        por_barra(self.barra_zram, fracao)
        texto, fracao = resumo_ssd(swaps)
        por_subtitulo(self.ssd, texto)
        por_barra(self.barra_ssd, fracao)

        aviso = int(self.guarda.get("warn_available", "15"))
        critico = int(self.guarda.get("critical_available", "4"))
        nivel = (est["nivel"] if est["vivo"] and est["nivel"] else
                 nivel_local(mem, psi, swaps if texto_swaps is not None else None, aviso, critico))
        rotulo, classe = NIVEIS.get(nivel, ("Unknown", "dim-label"))
        if self.selo.get_label() != rotulo:
            self.selo.set_label(rotulo)
            self.selo.set_css_classes(["heading", classe])
        por_subtitulo(self.pressao, "Pressure information isn't available on this system" if psi["some"] is None
                      else f"Apps waited for memory {psi['some']:.1f}% of the last 10 seconds")

        titulo, sub = texto_guarda(self.instalado, est, self.servico, uid)
        if self.status.get_title() != titulo:
            self.status.set_title(titulo)
        por_subtitulo(self.status, sub)
        self.ultima.set_visible(self.instalado)
        por_subtitulo(self.ultima, descrever_acao(est["ultima"], agora, est["conhecidos"], uid) or "Nothing yet")
        por_subtitulo(self.chaves["kill_on_critical"], texto_fechar(self.com_agente, self.servico_agente,
                                                                    self.agente))

        titulo, sub = texto_comfy(self.com_agente, ler_json(ler_texto(ESTADO_AGENTE)), self.servico_agente, agora)
        if self.comfy.get_title() != titulo:
            self.comfy.set_title(titulo)
        por_subtitulo(self.comfy, sub)

        linhas = camadas(swaps)
        chaves = [c for c, _, _ in linhas]
        if chaves != self.chaves_swap:
            self.chaves_swap = chaves
            for linha in self.linhas_swap:
                self.swap.remove(linha)
            self.linhas_swap = []
            self.swap.remove(self.tamanho_ssd)          # o tamanho fica sempre embaixo das camadas
            for _, titulo, sub in linhas:
                linha = Adw.ActionRow(use_markup=False, title=titulo, subtitle=sub)
                self.swap.add(linha)
                self.linhas_swap.append(linha)
            self.swap.add(self.tamanho_ssd)
            if not self.ocupado:
                self.montar_ssd(swaps)

    # ----- gravar: aero-memory (root) -----

    def ao_trocar_guarda(self, linha, _param, chave):
        if self.carregando:
            return
        valor = "1" if linha.get_active() else "0"
        if self.ocupado or not os.access(AJUDANTE, os.X_OK):
            fora_do_sinal(self.mostrar_guarda)      # não dá pra gravar agora: volta como estava
            return
        self.ocupado = True
        self.sensibilidade()
        cmd = [AJUDANTE, "set", chave, valor]
        em_segundo_plano(lambda: rodar_como_root(cmd), lambda r: self.depois_guarda(r, chave, valor))

    def depois_guarda(self, r, chave, valor):
        self.ocupado = False
        if getattr(r, "returncode", 1) == 0:
            self.guarda[chave] = valor
        else:
            # Senha cancelada, recusado ou erro: mostra o que está gravado de verdade (o switch volta) e o porquê
            self.guarda = self.ler_guarda(ler_estado(ler_texto(ESTADO), time.time()))
            self.janela.avisar(f"Not saved: {motivo_da_falha(r)}")
        self.mostrar_guarda()
        self.sensibilidade()

    def ao_trocar_ssd(self, linha, _param):
        if self.carregando:
            return
        novo = escolhido(linha, self.opcoes_ssd)
        if self.ocupado or not isinstance(novo, int) or novo == self.ssd_atual or not 4 <= novo <= 64:
            if novo != self.ssd_atual:
                fora_do_sinal(self.voltar_ssd)
            return
        if self.ssd_atual:
            texto = (f"Aero OS moves what's in the SSD swap back into memory, makes a new {novo} GB swap file "
                     f"and turns it on. It uses {novo} GB of disk space, can take a minute and asks for your "
                     f"password.")
        else:
            # criar o swap no SSD = memória gravada no disco: diz isso antes (só lê /proc e /sys, é rápido)
            texto = (f"Aero OS makes a {novo} GB swap file on the SSD and turns it on. It uses {novo} GB of "
                     f"disk space and asks for your password."
                     + aviso_privacidade(raiz_cifrada(ler_texto("/proc/self/mountinfo", 1024 * 1024))))
        d = Adw.AlertDialog(heading=f"Change the SSD swap to {novo} GB?", body=texto)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Change")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("cancel")
        d.set_close_response("cancel")              # Esc ou fechar = cancelar (a lista volta)
        d.connect("response", lambda _d, resp: self.trocar_ssd(novo) if resp == "ok" else self.voltar_ssd())
        d.present(self.janela)

    def voltar_ssd(self):
        self.carregando = True
        self.tamanho_ssd.set_selected(indice(self.opcoes_ssd, self.ssd_atual))
        self.carregando = False

    def trocar_ssd(self, novo):
        if self.ocupado:
            self.voltar_ssd()
            return
        self.ocupado = True
        self.sensibilidade()
        sub = self.tamanho_ssd.get_subtitle()
        self.tamanho_ssd.set_subtitle("Changing… this can take a minute")
        cmd = [AJUDANTE, "swap-size", str(int(novo))]

        def depois(r):
            self.ocupado = False
            self.tamanho_ssd.set_subtitle(sub)
            codigo = getattr(r, "returncode", None)
            if codigo == 0:
                self.janela.avisar(f"SSD swap is now {novo} GB")
            elif codigo in (2, 3, 4):
                # recusado ("pouco espaço no disco", "RAM apertada pra desligar o swap"...): o motivo
                # costuma ser longo, então vai numa caixa (um aviso de 3 s não dá tempo de ler)
                motivo = motivo_da_falha(r)
                self.explicar("Swap not changed", motivo[:1].upper() + motivo[1:])
            else:
                self.janela.avisar(f"Swap not changed: {motivo_da_falha(r)}")
            self.montar_ssd(ler_swaps(ler_texto("/proc/swaps")))
            self.sensibilidade()
            self.atualizar()
        # Sem limite de tempo de propósito: matar o sudo no meio de um swapoff deixaria o swap pela metade
        em_segundo_plano(lambda: rodar_como_root(cmd), depois)

    def explicar(self, titulo, texto):
        """Caixa com uma explicação e um OK. Título e texto sem markup (o padrão do Adw.AlertDialog)."""
        d = Adw.AlertDialog(heading=titulo, body=texto, heading_use_markup=False, body_use_markup=False)
        d.add_response("ok", "OK")
        d.present(self.janela)

    # ----- gravar: aero-memory-agent (usuário) -----

    def ao_trocar_agente(self, chave, valor):
        if self.carregando:
            return
        if valor is None or not self.com_agente:
            fora_do_sinal(self.mostrar_agente)
            return
        try:
            gravar_conf(CONF_AGENTE, {chave: valor})
        except (OSError, ValueError):
            fora_do_sinal(self.mostrar_agente)       # volta como estava
            self.janela.avisar("Couldn't save the ComfyUI settings")
            return
        self.agente[chave] = valor
        self.agendar_recarga()

    def agendar_recarga(self):
        """O agente relê a config. Espera 0,6 s: vários cliques seguidos = um reinício só."""
        if self.recarga is not None:
            GLib.source_remove(self.recarga)
        self.recarga = GLib.timeout_add(600, self.recarregar_agente)

    def recarregar_agente(self):
        self.recarga = None
        # try-: só reinicia se já estiver rodando (se a pessoa parou o agente, ele continua parado).
        # --no-block: o systemctl só pede e sai na hora; abrir() = a GLib recolhe o processo (sem zumbi)
        # e ele termina mesmo se o Settings fechar logo depois.
        try:
            abrir(["systemctl", "--user", "--no-block", "try-reload-or-restart", "aero-memory-agent.service"])
        except GLib.Error:
            pass
        return GLib.SOURCE_REMOVE
