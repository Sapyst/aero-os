#!/usr/bin/env python3
"""
taxa.py — taxa de atualização adaptativa (Settings → Display → Refresh rate). Daemon sem tela.

  na tomada            -> a maior taxa de cada monitor (o Dell vai a 180 Hz, a tela do notebook a 165)
  na bateria           -> 60 Hz (a tela gasta bem menos) — ou a maior, se o usuário escolher
  jogo aberto          -> a maior, mesmo na bateria (janela steam_app_*, gamescope... lista no State)

Escuta o Hyprland (janela abriu/fechou, monitor plugado, config recarregada: o "hyprctl reload" volta as
taxas do monitores.conf, então reaplica) e olha a tomada a cada 3 s (é só ler um arquivo do kernel).
Só mexe quando a taxa muda de verdade: trocar o modo do monitor pisca a tela uma vez.

State: ~/.config/aero-shell/taxa.json  {"ligado", "bateria": "60"|"max", "jogos", "classes_jogo"}
Uso: aero-hz            daemon (exec-once)
     aero-hz --status   o que ele faria agora
"""
import os
import re
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from servicos import energia, estado, hypr, telas  # noqa: E402

PADRAO = {"ligado": True, "bateria": "60", "jogos": True,
          "classes_jogo": ["^steam_app_", "^gamescope$", "^Minecraft", "\\.exe$"]}


class Taxa:
    def __init__(self):
        self.cfg, self.versao = PADRAO, -1
        self.jogos = set()          # endereços das janelas de jogo abertas
        self.pedido = threading.Event()
        self.recarregar()
        for c in hypr.json_de("clients", []):
            if self.eh_jogo(c.get("class", "")):
                self.jogos.add(c["address"].removeprefix("0x"))

    def recarregar(self):
        v = estado.versao("taxa")
        if v != self.versao:
            self.versao, self.cfg = v, estado.ler("taxa", PADRAO)
            return True
        return False

    def eh_jogo(self, classe):
        return any(re.search(p, classe or "") for p in self.cfg.get("classes_jogo") or [])

    def modo(self):
        tomada = energia.na_tomada()
        if tomada is None or tomada:
            return "max", "charger"
        if self.cfg.get("jogos") and self.jogos:
            return "max", "game"
        return ("max" if self.cfg.get("bateria") == "max" else "60"), "battery"

    def aplicar(self):
        if not self.cfg.get("ligado"):
            return
        modo, _ = self.modo()
        for m in telas.monitores():
            hz = telas.alvo(m, modo)
            if abs(hz - m["refreshRate"]) > 0.5:
                telas.definir_taxa(m, hz)

    def escutar(self):
        for nome, dados in hypr.eventos():
            if nome == "openwindow":
                end, _, resto = dados.partition(",")
                classe = resto.partition(",")[2].partition(",")[0]
                if self.eh_jogo(classe):
                    self.jogos.add(end)
                    self.pedido.set()
            elif nome == "closewindow" and dados in self.jogos:
                self.jogos.discard(dados)
                self.pedido.set()
            elif nome in ("configreloaded", "monitoradded", "monitoraddedv2"):
                time.sleep(1)                  # deixa o hypr-lid e o monitores.conf terminarem primeiro
                self.pedido.set()

    def rodar(self):
        threading.Thread(target=self.escutar, daemon=True).start()
        ultimo = None
        while True:
            agora = (energia.na_tomada(), bool(self.jogos))
            if self.pedido.is_set() or agora != ultimo or self.recarregar():
                self.pedido.clear()
                ultimo = agora
                try:
                    self.aplicar()
                except OSError:
                    pass
            self.pedido.wait(3)


if __name__ == "__main__":
    t = Taxa()
    if "--status" in sys.argv:
        modo, motivo = t.modo()
        print(f"{'on' if t.cfg.get('ligado') else 'off'} · mode {modo} ({motivo}) · games open: {len(t.jogos)}")
        for m in telas.monitores():
            print(f"  {m['name']}: {m['refreshRate']:.2f} Hz -> {telas.alvo(m, modo):.2f} Hz")
    else:
        t.rodar()
