"""widget_recursos.py — Resources: tudo curvo e colorido, em mono. Um sub-cartão com três ANÉIS (cpu, mem, temp)
com leitura numérica embaixo e os 24 núcleos em traços redondos; um com MEDIDORES de 270° (swap, discos, bateria);
e um com a rede em CURVAS suaves (download verde, upload azul) com preenchimento em degradê. Lê /proc e /sys a cada
2 s (sem abrir programa nenhum: microssegundos); discos a cada 60 s. Os valores DESLIZAM até o número novo
(400 ms, 12 fps: a área é grande). Tamanho fixo (depende só das opções)."""
import math
import os
import time

from widgets import LARGURA_ESTREITA, PASTEL, Widget, anel, cor_texto, painel_cairo, registrar, retangulo_redondo, texto, tracejado

VERMELHO = (0.88, 0.34, 0.31)
AZUL = (0.45, 0.66, 1.0)
HISTORICO = 60
MONO_FAM = "JetBrainsMono Nerd Font"


def ler(caminho):
    try:
        with open(caminho) as f:
            return f.read()
    except OSError:
        return ""


def gb(n):
    return f"{n / 2**30:.1f}" if n < 100 * 2**30 else f"{n / 2**30:.0f}"


def taxa(bps):
    for unidade, div in (("GB/s", 2**30), ("MB/s", 2**20), ("kB/s", 2**10)):
        if bps >= div:
            return f"{bps / div:.1f} {unidade}"
    return f"{int(bps)} B/s"


class Amostrador:
    def __init__(self):
        self.cpu_antes = {}
        self.cpu_total = 0.0
        self.nucleos = []
        self.hist_cpu = []
        self.rede_antes = None
        self.hist_rx, self.hist_tx = [], []
        self.rx = self.tx = 0.0
        self.sensores = {}
        for h in sorted(os.listdir("/sys/class/hwmon") if os.path.isdir("/sys/class/hwmon") else []):
            nome = ler(f"/sys/class/hwmon/{h}/name").strip()
            rotulo = {"k10temp": "cpu", "coretemp": "cpu", "zenpower": "cpu", "amdgpu": "gpu", "nvme": "ssd"}.get(nome)
            if rotulo and rotulo not in self.sensores and os.path.exists(f"/sys/class/hwmon/{h}/temp1_input"):
                self.sensores[rotulo] = f"/sys/class/hwmon/{h}/temp1_input"
        self.bateria = next((f"/sys/class/power_supply/{b}" for b in sorted(os.listdir("/sys/class/power_supply"))
                             if b.startswith("BAT")), None) if os.path.isdir("/sys/class/power_supply") else None
        self.discos = [("/", "/")]
        for m in (os.environ.get("AERO_DADOS", "/mnt/data"), "/home"):
            if os.path.ismount(m):
                self.discos.append((m, m))
        self.discos_info = []
        self.discos_quando = 0

    def cpu(self):
        for linha in ler("/proc/stat").splitlines():
            if not linha.startswith("cpu"):
                break
            partes = linha.split()
            nome, valores = partes[0], [int(v) for v in partes[1:9]]
            total, ocioso = sum(valores), valores[3] + valores[4]
            antes = self.cpu_antes.get(nome)
            self.cpu_antes[nome] = (total, ocioso)
            uso = 0.0
            if antes and total > antes[0]:
                uso = 1 - (ocioso - antes[1]) / (total - antes[0])
            if nome == "cpu":
                self.cpu_total = uso
            else:
                i = int(nome[3:])
                while len(self.nucleos) <= i:
                    self.nucleos.append(0.0)
                self.nucleos[i] = uso
        self.hist_cpu = (self.hist_cpu + [self.cpu_total])[-HISTORICO:]

    def memoria(self):
        info = {}
        for linha in ler("/proc/meminfo").splitlines():
            k, _, v = linha.partition(":")
            info[k] = int(v.split()[0]) * 1024 if v.strip() else 0
        t = info.get("MemTotal", 1)
        return (t - info.get("MemAvailable", 0), t, info.get("SwapTotal", 0) - info.get("SwapFree", 0), info.get("SwapTotal", 0))

    def rede(self):
        rx = tx = 0
        for linha in ler("/proc/net/dev").splitlines()[2:]:
            nome, _, resto = linha.partition(":")
            if nome.strip() == "lo":
                continue
            v = resto.split()
            rx += int(v[0])
            tx += int(v[8])
        agora = time.monotonic()
        if self.rede_antes:
            dt = max(0.001, agora - self.rede_antes[2])
            self.rx, self.tx = (rx - self.rede_antes[0]) / dt, (tx - self.rede_antes[1]) / dt
        self.rede_antes = (rx, tx, agora)
        self.hist_rx = (self.hist_rx + [self.rx])[-HISTORICO:]
        self.hist_tx = (self.hist_tx + [self.tx])[-HISTORICO:]

    def temperaturas(self):
        saida = []
        for rotulo in ("cpu", "gpu", "ssd"):
            caminho = self.sensores.get(rotulo)
            if caminho:
                try:
                    saida.append((rotulo, int(ler(caminho).strip()) / 1000))
                except ValueError:
                    pass
        return saida

    def discos_uso(self):
        if time.monotonic() - self.discos_quando > 60:
            self.discos_info = []
            for rotulo, caminho in self.discos:
                try:
                    st = os.statvfs(caminho)
                    total, livre = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
                    self.discos_info.append((rotulo, total - livre, total))
                except OSError:
                    pass
            self.discos_quando = time.monotonic()
        return self.discos_info

    def bateria_info(self):
        if not self.bateria:
            return None
        try:
            return int(ler(f"{self.bateria}/capacity")), ler(f"{self.bateria}/status").strip()
        except ValueError:
            return None


@registrar
class Recursos(Widget):
    tipo = "recursos"
    nome = "Resources"
    icone = "power-profile-balanced-symbolic"
    opcoes_padrao = {"nucleos": True, "temperaturas": True, "discos": True, "rede": True, "bateria": True}
    tamanho = (LARGURA_ESTREITA, 336)
    LARG = LARGURA_ESTREITA

    def montar(self):
        self.am = Amostrador()
        self.am.cpu()
        self.am.rede()
        self.v = {"cpu": 0.0, "nuc": list(self.am.nucleos), "mem": 0.0, "swap": 0.0, "rx": 0.0, "tx": 0.0}
        h = self.altura()
        self.redimensionar(self.LARG, h)
        self.tela = self.area(self.LARG, h, self.desenhar)
        self.cartao.append(self.tela)
        self.a_cada(2, self.tique)

    def grupos(self):
        """(nome, altura) de cada sub-cartão, sem escala."""
        o, am = self.opcoes, self.am
        g = [("aneis", 118 + (22 if o["nucleos"] else 0))]
        n = (1 if am.memoria()[3] else 0) + (len(am.discos) if o["discos"] else 0) + (1 if o["bateria"] and am.bateria else 0)
        if n:
            g.append(("medidores", 92))
        if o["rede"]:
            g.append(("rede", 84))
        return g

    def altura(self):
        g = self.grupos()
        return sum(h for _, h in g) + 10 * (len(g) - 1)

    def tique(self):
        self.am.cpu()
        if self.opcoes["rede"]:
            self.am.rede()
        usado, total, su, st = self.am.memoria()
        de = dict(self.v, nuc=list(self.v["nuc"]))
        para = {"cpu": self.am.cpu_total, "nuc": list(self.am.nucleos), "mem": usado / max(1, total),
                "swap": su / st if st else 0.0, "rx": self.am.rx, "tx": self.am.tx}
        while len(de["nuc"]) < len(para["nuc"]):
            de["nuc"].append(0.0)
        def passo(t):
            self.v = {"cpu": de["cpu"] + (para["cpu"] - de["cpu"]) * t,
                      "nuc": [a + (b - a) * t for a, b in zip(de["nuc"], para["nuc"])],
                      "mem": de["mem"] + (para["mem"] - de["mem"]) * t,
                      "swap": de["swap"] + (para["swap"] - de["swap"]) * t,
                      "rx": de["rx"] + (para["rx"] - de["rx"]) * t, "tx": de["tx"] + (para["tx"] - de["tx"]) * t}
            self.tela.queue_draw()
        self.animar(400, passo, fps=12)

    # ---- peças ----
    @staticmethod
    def medidor(cr, cx, cy, r, fracao, cor, esp):
        """Medidor de 270° (abertura embaixo), pontas redondas: trilho apagado + arco na cor."""
        a0, a1 = math.pi * 0.75, math.pi * 2.25
        cr.set_line_width(esp)
        cr.set_line_cap(1)
        cr.new_sub_path()
        cr.arc(cx, cy, r, a0, a1)
        cr.set_source_rgba(*cor[:3], 0.12)
        cr.stroke()
        f = max(0.0, min(1.0, fracao))
        if f > 0.005:
            cr.new_sub_path()
            cr.arc(cx, cy, r, a0, a0 + (a1 - a0) * f)
            cr.set_source_rgba(*cor[:3], 1.0)
            cr.stroke()

    def curva(self, cr, pontos, x, y, w, h, cor, maximo=None, preencher=True):
        """Linha SUAVE (Catmull-Rom → Bézier) pelo histórico, com preenchimento em degradê embaixo."""
        if len(pontos) < 2:
            return
        maximo = maximo or max(max(pontos), 1e-9)
        passo = w / (HISTORICO - 1)
        x0 = x + w - passo * (len(pontos) - 1)
        P = [(x0 + passo * i, y + h - h * min(1, p / maximo)) for i, p in enumerate(pontos)]
        cr.new_sub_path()
        cr.move_to(*P[0])
        for i in range(len(P) - 1):
            p0 = P[i - 1] if i > 0 else P[i]
            p1, p2 = P[i], P[i + 1]
            p3 = P[i + 2] if i + 2 < len(P) else p2
            c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
            cr.curve_to(c1[0], c1[1], c2[0], c2[1], p2[0], p2[1])
        cr.set_source_rgba(*cor[:3], 0.95)
        cr.set_line_width(1.6)
        cr.set_line_cap(1)
        cr.set_line_join(1)
        cr.stroke_preserve()
        if preencher:
            cr.line_to(x + w, y + h)
            cr.line_to(x0, y + h)
            cr.close_path()
            grad = __import__("cairo").LinearGradient(0, y, 0, y + h)
            grad.add_color_stop_rgba(0, *cor[:3], 0.28)
            grad.add_color_stop_rgba(1, *cor[:3], 0.0)
            cr.set_source(grad)
            cr.fill()
        else:
            cr.new_path()

    def desenhar(self, cr, w, h):
        area, e = self.tela, self.e
        cor = cor_texto(area)
        am, o, v = self.am, self.opcoes, self.v
        y = 0
        for nome, alt in self.grupos():
            alt *= e
            titulo = {"aneis": "cpu · mem · temp", "medidores": "storage · battery", "rede": "net"}[nome]
            painel_cairo(cr, 0, y + 6 * e, w, alt - 6 * e, 10 * e, titulo, area, e)
            y += 6 * e
            alt -= 6 * e
            if nome == "aneis":
                usado, total, su, st = am.memoria()
                temps = dict(am.temperaturas()) if o["temperaturas"] else {}
                itens = [("cpu", v["cpu"], f"{round(v['cpu'] * 100)}%", f"{len(am.nucleos)} thr", PASTEL["rosa"]),
                         ("mem", v["mem"], f"{round(v['mem'] * 100)}%", f"{gb(usado)}/{gb(total)}G", PASTEL["azul"])]
                if "cpu" in temps:
                    sub = f"gpu {temps['gpu']:.0f}°" if "gpu" in temps else (f"ssd {temps['ssd']:.0f}°" if "ssd" in temps else "")
                    itens.append(("temp", min(1.0, temps["cpu"] / 100), f"{temps['cpu']:.0f}°", sub, PASTEL["pessego"]))
                elif "gpu" in temps:
                    itens.append(("gpu", min(1.0, temps["gpu"] / 100), f"{temps['gpu']:.0f}°", "", PASTEL["pessego"]))
                else:
                    itens.append(("swap", v["swap"], f"{round(v['swap'] * 100)}%", f"{gb(su)}/{gb(st)}G", PASTEL["pessego"]))
                n = len(itens)
                r = 29 * e
                for i, (rotulo, frac, valor, sub, c) in enumerate(itens):
                    cx = w * (i + 0.5) / n
                    cy = y + 12 * e + r
                    anel(cr, cx, cy, r, frac, c, 6.5 * e)
                    texto(cr, area, valor, cx, cy - 8 * e, 12 * e, "bold", 0.95, "centro", MONO_FAM)
                    texto(cr, area, rotulo, cx, cy + r + 8 * e, 10 * e, "bold", 0.55, "centro", MONO_FAM)
                    if sub:
                        texto(cr, area, sub, cx, cy + r + 21 * e, 9 * e, "normal", 0.4, "centro", MONO_FAM)
                if o["nucleos"] and v["nuc"]:
                    k = len(v["nuc"])
                    folga = 2.5 * e
                    x0, wi = 12 * e, w - 24 * e
                    lw = (wi - folga * (k - 1)) / k
                    tracejado(cr, x0, y + alt - 30 * e, wi, e)
                    yb = y + alt - 20 * e
                    for i, u in enumerate(v["nuc"]):
                        x = x0 + i * (lw + folga)
                        retangulo_redondo(cr, x, yb, lw, 10 * e, 2.5 * e)
                        cr.set_source_rgba(*cor[:3], 0.08)
                        cr.fill()
                        a = max(3 * e, 10 * e * min(1, u))           # traço de 3 px no mínimo (não vira bolinha)
                        retangulo_redondo(cr, x, yb + 10 * e - a, lw, a, 2.5 * e)
                        cr.set_source_rgba(*PASTEL["rosa"], 0.9)
                        cr.fill()
            elif nome == "medidores":
                usado, total, su, st = am.memoria()
                itens = []
                if st:
                    itens.append(("swp", v["swap"], f"{round(v['swap'] * 100)}%", PASTEL["lilas"]))
                if o["discos"]:
                    cores = [PASTEL["azul"], PASTEL["verde"], PASTEL["amarelo"]]
                    for i, (rotulo, du, dt) in enumerate(am.discos_uso()):
                        nome_d = "root" if rotulo == "/" else rotulo.rsplit("/", 1)[-1][:4]
                        itens.append((nome_d, du / max(1, dt), f"{round(100 * du / max(1, dt))}%", cores[i % 3]))
                if o["bateria"] and am.bateria:
                    b = am.bateria_info()
                    if b:
                        itens.append(("bat", b[0] / 100, f"{b[0]}%", PASTEL["verde"] if b[0] > 15 else VERMELHO))
                n = max(1, len(itens))
                r = 21 * e
                for i, (rotulo, frac, valor, c) in enumerate(itens):
                    cx = w * (i + 0.5) / n
                    cy = y + 12 * e + r
                    self.medidor(cr, cx, cy, r, frac, c, 5 * e)
                    texto(cr, area, valor, cx, cy - 6 * e, 10 * e, "bold", 0.95, "centro", MONO_FAM)
                    texto(cr, area, rotulo, cx, cy + r + 4 * e, 10 * e, "bold", 0.55, "centro", MONO_FAM)
            elif nome == "rede":
                texto(cr, area, f"↓ {taxa(v['rx'])}   ↑ {taxa(v['tx'])}", w - 12 * e, y + 9 * e, 11 * e, "normal", 0.8, "direita", MONO_FAM)
                maximo = max(max(am.hist_rx + am.hist_tx, default=1), 1)
                cr.save()
                retangulo_redondo(cr, 12 * e, y + 28 * e, w - 24 * e, alt - 38 * e, 8 * e)
                cr.clip()
                self.curva(cr, am.hist_rx, 12 * e, y + 30 * e, w - 24 * e, alt - 42 * e, PASTEL["verde"], maximo)
                self.curva(cr, am.hist_tx, 12 * e, y + 30 * e, w - 24 * e, alt - 42 * e, PASTEL["azul"], maximo, preencher=False)
                cr.restore()
            y += alt + 10 * e
