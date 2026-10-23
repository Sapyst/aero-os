#!/usr/bin/env python3
"""
bateria.py — Energia inteligente (Settings → Power → Battery · clique na bateria da barra). Daemon sem tela.

LIMITADOR (precisa do driver; sem ele, só avisa)
  smart    80%; depois de 24 h direto na tomada desce pra 60% (bateria que mora no carregador vive mais
           a ~60%). Tirou da tomada: volta a 80% na próxima carga.
  fixo     sempre `limite`% (60..100)
  bypass   "energia direto": segura a bateria no nível de AGORA (mín. 60%) — o notebook roda só da
           tomada e a bateria fica parada. É o que o controlador faz quando o limite é atingido.
  desligado  carrega até 100%
  "Carregar 100% uma vez" (viagem): ignora o limite até tirar da tomada; depois volta sozinho.

TOMADA SAIU
  a bateria assume sozinha (é o hardware); o daemon grava no disco o que estava pendente (sync), troca o
  modo de energia (automático) e arma o "salvar o momento".
SALVAR O MOMENTO (caso extremo)
  na bateria e chegou em `critico`%: avisa, espera 15 s (se voltar a tomada, cancela) e HIBERNA (a sessão
  vai pro disco e sobrevive à bateria zerar) — ou, se hibernar não está configurado, SUSPENDE (a sessão
  fica na memória, gastando quase nada).
MODO DE ENERGIA AUTOMÁTICO
  jogo aberto = Performance (mesma detecção do aero-hz) · tomada = `perfil_tomada` (Balanced) · bateria = Power saver.

State: ~/.config/aero-shell/energia.json · memória do daemon: ~/.cache/aero-shell/energia.json
Uso: aero-energia                 daemon (exec-once)
     aero-energia --waybar        uma linha JSON pro módulo da barra
     aero-energia --alternar      liga/desliga o limitador (smart <-> desligado) — clique na bateria
     aero-energia --cheia         carregar 100% uma vez
     aero-energia --status
"""
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from servicos import energia, estado, hypr  # noqa: E402

PADRAO = {"modo": "smart", "limite": 80, "perfil_auto": True, "perfil_tomada": "balanced",
          "critico": 6, "cheia_uma_vez": False, "segurar": None}
MEMORIA = os.path.expanduser("~/.cache/aero-shell/energia.json")
DIA = 24 * 3600
SINAL_WAYBAR = 12
ICONE = {"smart": "", "fixo": "", "bypass": ""}          # folha · tomada
NOME = {"smart": "Smart limiter", "fixo": "Charge limit", "bypass": "Direct power", "desligado": "No limit"}


def memoria():
    try:
        with open(MEMORIA) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def gravar_memoria(m):
    os.makedirs(os.path.dirname(MEMORIA), exist_ok=True)
    with open(MEMORIA + ".tmp", "w") as f:
        json.dump(m, f)
    os.replace(MEMORIA + ".tmp", MEMORIA)


def jogo_aberto():
    """Alguma janela de jogo aberta? (as mesmas classes do aero-hz, Settings → Display)"""
    import re
    classes = estado.ler("taxa", {}).get("classes_jogo") or ["^steam_app_", "^gamescope$", "^Minecraft", "\\.exe$"]
    return any(re.search(p, c.get("class") or "") for c in hypr.json_de("clients", []) or [] for p in classes)


def avisar(titulo, texto, urgente=False, etiqueta=None):
    # etiqueta: notificações com a mesma etiqueta SUBSTITUEM a anterior (o hermesd entende as duas dicas),
    # então clicar várias vezes na bateria não empilha avisos
    dicas = [f"-h", f"string:x-canonical-private-synchronous:{etiqueta}",
             f"-h", f"string:x-dunst-stack-tag:{etiqueta}"] if etiqueta else []
    subprocess.Popen(["notify-send", "-a", "Power", "-i", "battery-symbolic"] + (["-u", "critical"] if urgente else [])
                     + dicas + [titulo, texto], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def atualizar_barra():
    subprocess.run(["pkill", f"-RTMIN+{SINAL_WAYBAR}", "-x", "waybar"], capture_output=True)


def alvo(cfg, mem, tomada, nivel):
    """O limite que deveria estar valendo agora (100 = sem limite)."""
    modo = cfg.get("modo")
    if cfg.get("cheia_uma_vez") or modo == "desligado":
        return 100
    if modo == "fixo":
        return max(60, min(100, int(cfg.get("limite", 80))))
    if modo == "bypass":
        return max(60, min(100, int(cfg.get("segurar") or nivel or 80)))
    # smart
    desde = mem.get("tomada_desde")
    if tomada and desde and time.time() - desde >= DIA:
        return 60
    return 80


def descrever(cfg, mem, b, tomada):
    modo = cfg.get("modo")
    linhas = [f"{b['nivel']}%  ·  " + ("on the charger" if tomada else "on battery")] if b else []
    if cfg.get("cheia_uma_vez"):
        linhas.append("Charging to 100% once (back to the limiter when unplugged)")
    elif modo != "desligado":
        lim = alvo(cfg, mem, tomada, b["nivel"] if b else 80)
        extra = {"smart": " (desk mode: a day on the charger)" if lim == 60 else "",
                 "bypass": ": the laptop runs from the charger, the battery rests"}.get(modo, "")
        linhas.append(f"{NOME[modo]}: stops at {lim}%{extra}")
    if modo != "desligado" and not energia.driver():
        linhas.append("Needs the battery driver (Settings → Power)")
    if b and b.get("saude"):
        linhas.append(f"Health {b['saude']}%  ·  {b.get('ciclos') or 0} cycles")
    linhas.append("Click: limiter on/off  ·  Right click: Power settings")
    return "\n".join(linhas)


def waybar():
    cfg, mem = estado.ler("energia", PADRAO), memoria()
    b, tomada = energia.bateria(), energia.na_tomada()
    modo = "desligado" if not b else ("cheia" if cfg.get("cheia_uma_vez") else cfg.get("modo"))
    texto = ICONE.get(cfg.get("modo"), "") if modo not in ("desligado", "cheia") else ("" if modo == "cheia" else "")
    classe = modo + ("" if energia.driver() or modo == "desligado" else " sem-driver")
    print(json.dumps({"text": texto, "tooltip": descrever(cfg, mem, b, tomada) if b else "", "class": classe}))


def alternar():
    cfg = estado.ler("energia", PADRAO)
    if cfg.get("modo") == "desligado":
        cfg["modo"] = cfg.get("modo_antes") or "smart"
    else:
        cfg["modo_antes"], cfg["modo"] = cfg.get("modo"), "desligado"
    cfg["cheia_uma_vez"] = False
    if cfg["modo"] == "bypass":
        cfg["segurar"] = (energia.bateria() or {}).get("nivel")
    estado.gravar("energia", cfg)
    if not energia.driver() and cfg["modo"] != "desligado":
        avisar(NOME[cfg["modo"]] + " on", "It starts working once the battery driver is installed (Settings → Power).",
               etiqueta="aero-power-mode")
    else:
        avisar(NOME[cfg["modo"]] + (" on" if cfg["modo"] != "desligado" else ""),
               "Charging to 100% again" if cfg["modo"] == "desligado" else "Stops before full to keep the battery healthy",
               etiqueta="aero-power-mode")
    atualizar_barra()


class Daemon:
    def __init__(self):
        self.mem = memoria()
        self.tomada = None
        self.salvando = None            # hora em que o aviso de "salvar o momento" saiu

    def passo(self):
        cfg = estado.ler("energia", PADRAO)
        b, tomada = energia.bateria(), energia.na_tomada()
        if b is None:
            return
        if tomada != self.tomada:
            if tomada:
                # o relógio das 24 h só zera numa ligação de verdade (não quando o daemon reinicia)
                if self.tomada is False or not self.mem.get("tomada_desde"):
                    self.mem["tomada_desde"] = time.time()
                self.salvando = None
            else:
                self.mem["tomada_desde"] = None
                if self.tomada is not None:                # TIROU da tomada (não é a primeira leitura)
                    os.sync()                              # o que estava pendente vai pro disco agora
                    if cfg.get("cheia_uma_vez"):
                        cfg = cfg | {"cheia_uma_vez": False}
                        estado.gravar("energia", cfg)
            self.tomada = tomada
            gravar_memoria(self.mem)
            atualizar_barra()
        # modo de energia automático
        if cfg.get("perfil_auto"):
            energia.definir_perfil("performance" if jogo_aberto() else
                                   cfg.get("perfil_tomada", "balanced") if tomada else "power-saver")
        # limitador
        if energia.driver() and energia.pode_escrever():
            energia.definir_limite(alvo(cfg, self.mem, tomada, b["nivel"]))
        # salvar o momento
        critico = int(cfg.get("critico", 6) or 0)
        if not tomada and critico and b["nivel"] <= critico:
            if self.salvando is None:
                self.salvando = time.time()
                acao = "Hibernating" if energia.pode_hibernar() else "Suspending"
                avisar("Battery critical: saving the moment",
                       f"{acao} in 15 s to keep your session. Plug in the charger to cancel.", urgente=True)
            elif time.time() - self.salvando >= 15:
                os.sync()
                subprocess.run(["systemctl", "hibernate" if energia.pode_hibernar() else "suspend"])
                self.salvando = None
        elif self.salvando and (tomada or b["nivel"] > critico):
            self.salvando = None

    def rodar(self):
        ultimo = 0.0
        while True:
            try:
                tomada = energia.na_tomada()
                # tomada: a cada 2 s (é ler um arquivo). O resto: a cada 30 s ou quando a tomada muda
                if tomada != self.tomada or time.time() - ultimo >= 30 or estado.versao("energia") > ultimo:
                    ultimo = time.time()
                    self.passo()
                    atualizar_barra()
            except Exception as e:  # noqa: BLE001 — o daemon de energia não pode morrer
                print("bateria:", e, file=sys.stderr, flush=True)
            time.sleep(2)


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--waybar":
        waybar()
    elif arg == "--alternar":
        alternar()
    elif arg == "--cheia":
        estado.gravar("energia", estado.ler("energia", PADRAO) | {"cheia_uma_vez": True})
        avisar("Charging to 100% once", "Back to the limiter when you unplug.")
        atualizar_barra()
    elif arg == "--status":
        cfg, mem = estado.ler("energia", PADRAO), memoria()
        b, t = energia.bateria(), energia.na_tomada()
        print(descrever(cfg, mem, b, t) if b else "no battery")
        print("driver:", energia.driver() or "none", "| write access:", energia.pode_escrever(),
              "| target:", alvo(cfg, mem, t, b["nivel"] if b else 80), "| profile:", energia.perfil(),
              "| hibernate:", energia.pode_hibernar())
    else:
        Daemon().rodar()
