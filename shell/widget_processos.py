"""widget_processos.py — Processes: um "top" ao vivo, no estilo do Resources (fica embaixo dele). Carga média e
número de tarefas no topo; depois os processos que mais consomem CPU (ou memória), agrupados pelo nome do
programa — e, pra interpretadores (python3, bash, node...), pelo nome do SCRIPT: assim dá pra ver quanto cada
peça do próprio shell (dock.py, widgets.py...) gasta. Lê /proc/*/stat e statm a cada 3 s (uns 10 ms de CPU).
CPU% é como no top: % de UM núcleo (um programa com 3 threads cheias passa de 100)."""
import os
import time

from widgets import LARGURA_ESTREITA, PASTEL, Widget, cor_texto, painel_cairo, registrar, texto

MONO_FAM = "Adwaita Mono"
CLK = os.sysconf("SC_CLK_TCK")
PAGINA = os.sysconf("SC_PAGE_SIZE")
INTERPRETADORES = {"python3", "python", "bash", "sh", "zsh", "fish", "node", "perl", "ruby", "electron"}


def mem_txt(b):
    return f"{b / 2**30:.1f}G" if b >= 2**30 else f"{b / 2**20:.0f}M"


class Topo:
    def __init__(self):
        self.antes = {}          # pid -> (ticks, quando)
        self.nomes = {}          # pid -> nome mostrado (cache: comm/cmdline só na 1ª vez)

    def nome(self, pid, comm):
        n = self.nomes.get(pid)
        if n:
            return n
        n = comm
        if comm in INTERPRETADORES:
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
                for a in args[1:]:
                    if not a.startswith("-"):
                        n = os.path.basename(a) or comm
                        break
            except OSError:
                pass
        self.nomes[pid] = n
        return n

    def amostrar(self):
        """[(nome, cpu%, bytes, quantos)] agrupado por nome. Também guarda carga média e nº de tarefas."""
        agora = time.monotonic()
        grupos = {}
        vivos = set()
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/stat") as f:
                    st = f.read()
                with open(f"/proc/{pid}/statm") as f:
                    res = int(f.read().split()[1]) * PAGINA
            except (OSError, ValueError, IndexError):
                continue
            comm = st[st.index("(") + 1:st.rindex(")")]
            campos = st[st.rindex(")") + 2:].split()
            ticks = int(campos[11]) + int(campos[12])          # utime + stime
            vivos.add(pid)
            antes = self.antes.get(pid)
            self.antes[pid] = (ticks, agora)
            cpu = 0.0
            if antes and agora > antes[1]:
                cpu = (ticks - antes[0]) / CLK / (agora - antes[1]) * 100
            if not res and not cpu:
                continue                                        # thread do kernel ou zumbi
            nome = self.nome(pid, comm)
            g = grupos.setdefault(nome, [0.0, 0, 0])
            g[0] += cpu
            g[1] += res
            g[2] += 1
        for pid in list(self.antes):
            if pid not in vivos:
                del self.antes[pid]
                self.nomes.pop(pid, None)
        try:
            with open("/proc/loadavg") as f:
                l = f.read().split()
            self.carga = f"{l[0]} {l[1]} {l[2]}"
            self.tarefas = l[3].split("/")[1]
        except (OSError, IndexError):
            self.carga, self.tarefas = "?", "?"
        return [(n, c, m, q) for n, (c, m, q) in grupos.items()]


@registrar
class Processos(Widget):
    tipo = "processos"
    nome = "Processes"
    icone = "view-list-symbolic"
    opcoes_padrao = {"ordenar": "cpu", "linhas": 8}
    tamanho = (LARGURA_ESTREITA, 200)

    def montar(self):
        self.topo = Topo()
        self.topo.amostrar()
        self.linhas = []
        n = int(self.opcoes["linhas"])
        h = 56 + 18 * n
        self.redimensionar(LARGURA_ESTREITA, h)
        self.tela = self.area(LARGURA_ESTREITA, h, self.desenhar)
        self.cartao.append(self.tela)
        self.temporizadores.append(__import__("gi").repository.GLib.timeout_add(900, self._primeira))
        self.a_cada(3, self.tique)

    def _primeira(self):
        self.tique()
        return False

    def tique(self):
        itens = self.topo.amostrar()
        chave = (lambda i: -i[1]) if self.opcoes["ordenar"] == "cpu" else (lambda i: -i[2])
        itens.sort(key=chave)
        self.linhas = itens[:int(self.opcoes["linhas"])]
        self.tela.queue_draw()

    def desenhar(self, cr, w, h):
        area, e = self.tela, self.e
        cor = cor_texto(area)
        por = self.opcoes["ordenar"]
        painel_cairo(cr, 0, 6 * e, w, h - 6 * e, 10 * e, f"processes · by {por}", area, e)
        x0, xr = 12 * e, w - 12 * e
        texto(cr, area, f"load {getattr(self.topo, 'carga', '?')}", x0, 16 * e, 10 * e, "normal", 0.55, familia=MONO_FAM)
        texto(cr, area, f"{getattr(self.topo, 'tarefas', '?')} tasks", xr, 16 * e, 10 * e, "normal", 0.55, "direita", MONO_FAM)
        # cabeçalho da tabela
        y = 36 * e
        xc, xm = xr - 54 * e, xr
        texto(cr, area, "name", x0, y, 9.5 * e, "bold", 0.4, familia=MONO_FAM)
        texto(cr, area, "cpu%", xc, y, 9.5 * e, "bold", 0.4, "direita", MONO_FAM)
        texto(cr, area, "mem", xm, y, 9.5 * e, "bold", 0.4, "direita", MONO_FAM)
        y += 16 * e
        maximo = max([i[1] if por == "cpu" else i[2] for i in self.linhas] or [1]) or 1
        cores = [PASTEL["rosa"], PASTEL["pessego"], PASTEL["amarelo"], PASTEL["verde"], PASTEL["azul"], PASTEL["lilas"]]
        for i, (nome, cpu, mem, q) in enumerate(self.linhas):
            c = cores[min(i, len(cores) - 1)]
            rot = nome if q == 1 else f"{nome} ×{q}"
            if len(rot) > 22:
                rot = rot[:21] + "…"
            texto(cr, area, rot, x0, y, 11 * e, "normal", 0.9, familia=MONO_FAM)
            texto(cr, area, f"{cpu:.1f}", xc, y, 11 * e, "normal", 0.9, "direita", MONO_FAM)
            texto(cr, area, mem_txt(mem), xm, y, 11 * e, "normal", 0.7, "direita", MONO_FAM)
            frac = (cpu if por == "cpu" else mem) / maximo
            bw = (xc - 70 * e) - x0
            cr.rectangle(x0, y + 14.5 * e, bw, 1.5 * e)
            cr.set_source_rgba(*cor[:3], 0.08)
            cr.fill()
            if frac > 0.01:
                cr.rectangle(x0, y + 14.5 * e, bw * min(1.0, frac), 1.5 * e)
                cr.set_source_rgba(*c, 0.9)
                cr.fill()
            y += 18 * e
