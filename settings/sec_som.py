"""
sec_som.py — seção Sound.

Usa o pactl (a interface "PulseAudio" do PipeWire):
  pactl -f json list sinks      saídas (caixas, fones, HDMI)   -f json = resposta em JSON
  pactl -f json list sources    entradas (microfones)
  pactl set-default-sink NOME   escolhe a saída padrão
  pactl set-sink-volume @DEFAULT_SINK@ 50%   volume da saída padrão
  pactl set-sink-mute @DEFAULT_SINK@ 1       mudo (1) ou não (0)

"Fixed device volume" (só saídas): alguns fones USB têm um volume interno que se comporta mal
(um lado mais alto abaixo de 100%, ou voltam zerados ao reconectar). Ligado, o volume passa a ser
feito em software e o volume interno do aparelho fica preso em 100% — quem faz isso é o
componente do sistema `aero-audio` (~/.local/bin/aero-audio + aero-audio-guard.service).

"Switch to new devices": ao conectar um fone/caixa USB ou Bluetooth, o som passa para ele sozinho
(como no celular); ao desconectar, volta para o anterior. Também é o aero-audio que faz.
"""
import json
import re

from gi.repository import Adw, Gtk

import os
import time

from comum import Secao, em_segundo_plano, limpar_grupo, linha_app, pagina, rodar

AERO_AUDIO = os.path.expanduser("~/.local/bin/aero-audio")


def nome_bonito(desc, nome, entrada):
    """Os nomes que o sistema dá são longos e técnicos; deixamos legíveis."""
    m = re.search(r"\[(.+?)\]", desc)            # ex: "...Output [DELL G2725D] (Stereo)"
    if m:
        return f"{m.group(1)} (HDMI)"
    if "pci-0000_66" in nome:                    # 66:00 = controlador de áudio do próprio laptop
        return "Laptop microphone" if entrada else "Laptop speakers"
    for lixo in (" Analog Stereo", " Digital Stereo", " (Stereo)", " Mono", " Multichannel"):
        desc = desc.replace(lixo, "")
    return desc.strip()


def volume_de(dispositivo):
    """Média dos canais (esquerdo/direito), em %."""
    canais = dispositivo["volume"].values()
    return round(sum(int(c["value_percent"].rstrip("%")) for c in canais) / max(len(canais), 1))


def listar(tipo):
    """tipo = 'sinks' (saídas) ou 'sources' (entradas). Esconde o que não está ligado."""
    r = rodar(["pactl", "-f", "json", "list", tipo])
    if r.returncode != 0:
        return []
    padrao = rodar(["pactl", "get-default-sink" if tipo == "sinks" else "get-default-source"]).stdout.strip()
    itens = []
    for d in json.loads(r.stdout):
        if d["name"].endswith(".monitor"):       # "monitor" = cópia do som de uma saída; não é microfone
            continue
        porta = next((p for p in d.get("ports", []) if p["name"] == d.get("active_port")), None)
        if porta and porta["availability"] == "not available" and d["name"] != padrao:
            continue                             # ex: saídas HDMI sem nada plugado
        d["padrao"] = d["name"] == padrao
        itens.append(d)
    return itens


class SecaoSom(Secao):
    id = "som"
    titulo = "Sound"
    icone = "audio-volume-high-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()
        self.saida = Adw.PreferencesGroup(title="Output")
        self.entrada = Adw.PreferencesGroup(title="Input")
        avancado = Adw.PreferencesGroup()
        avancado.add(linha_app("Advanced sound settings", "Per-app volume, profiles, all devices",
                               ["pavucontrol"], "preferences-system-symbolic"))
        for g in (self.saida, self.entrada, avancado):
            conteudo.add(g)
        self.linhas = {"sinks": [], "sources": []}
        self.atualizando = False  # trava pra mudanças feitas pelo código não virarem comandos
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.montar("sinks", self.saida)
        self.montar("sources", self.entrada)

    def montar(self, tipo, grupo):
        self.atualizando = True
        limpar_grupo(grupo, self.linhas[tipo])
        entrada = tipo == "sources"
        sink_ou_source = "source" if entrada else "sink"
        itens = listar(tipo)

        # Botões de rádio: só um marcado por vez (set_group liga todos no mesmo grupo)
        primeiro_radio = None
        padrao = None
        for d in itens:
            radio = Gtk.CheckButton(active=d["padrao"], valign=Gtk.Align.CENTER)
            if primeiro_radio:
                radio.set_group(primeiro_radio)
            else:
                primeiro_radio = radio
            linha = Adw.ActionRow(use_markup=False, title=nome_bonito(d["description"], d["name"], entrada),
                                  activatable_widget=radio)
            linha.add_prefix(radio)
            radio.connect("toggled", self.ao_escolher, tipo, grupo, d["name"])
            grupo.add(linha)
            self.linhas[tipo].append(linha)
            if d["padrao"]:
                padrao = d

        if padrao:
            # Linha de volume: botão de mudo + controle deslizante
            vol = Adw.ActionRow(use_markup=False, title="Volume")
            mudo = Gtk.ToggleButton(active=padrao["mute"], valign=Gtk.Align.CENTER,
                                    tooltip_text="Mute")
            mudo.add_css_class("flat")
            self.icone_mudo(mudo, entrada)
            mudo.connect("toggled", self.ao_mudo, sink_ou_source, entrada)
            barra = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
            barra.set_hexpand(True)
            barra.set_size_request(260, -1)
            barra.set_value(volume_de(padrao))
            barra.add_mark(100, Gtk.PositionType.BOTTOM, None)
            barra.connect("value-changed", self.ao_volume, sink_ou_source)
            vol.add_prefix(mudo)
            vol.add_suffix(barra)
            grupo.add(vol)
            self.linhas[tipo].append(vol)
            # Volume fixo do aparelho: só faz sentido em placas ALSA (USB, HDMI, interna).
            cartao = padrao.get("properties", {}).get("device.name", "")
            if not entrada and cartao.startswith("alsa_card.") and os.path.exists(AERO_AUDIO):
                fixos = rodar([AERO_AUDIO, "list"]).stdout.split()
                fixo = Adw.SwitchRow(use_markup=False, title="Fixed device volume",
                                     subtitle="The system does the volume and keeps the device's own at full. "
                                              "Fixes headsets that go silent or play louder on one side.",
                                     active=cartao in fixos)
                fixo.connect("notify::active", self.ao_fixo, cartao, tipo, grupo)
                grupo.add(fixo)
                self.linhas[tipo].append(fixo)
        if not entrada and os.path.exists(AERO_AUDIO):
            try:
                ligado = "switch_to_new=off" not in open(os.path.expanduser("~/.config/aero/audio.conf")).read()
            except FileNotFoundError:
                ligado = True
            troca = Adw.SwitchRow(title="Switch to new devices",
                                  subtitle="When a headset or speaker connects (USB or Bluetooth), sound moves to it",
                                  active=ligado)
            troca.connect("notify::active", lambda r, *_: self.atualizando or rodar([AERO_AUDIO, "switch", "on" if r.get_active() else "off"]))
            grupo.add(troca)
            self.linhas[tipo].append(troca)
        if not itens:
            vazio = Adw.ActionRow(use_markup=False, title="No devices found")
            grupo.add(vazio)
            self.linhas[tipo].append(vazio)
        self.atualizando = False

    def icone_mudo(self, botao, entrada):
        if entrada:
            botao.set_icon_name("microphone-disabled-symbolic" if botao.get_active()
                                else "audio-input-microphone-symbolic")
        else:
            botao.set_icon_name("audio-volume-muted-symbolic" if botao.get_active()
                                else "audio-volume-high-symbolic")

    def ao_escolher(self, radio, tipo, grupo, nome):
        if self.atualizando or not radio.get_active():
            return
        rodar(["pactl", "set-default-sink" if tipo == "sinks" else "set-default-source", nome])
        self.montar(tipo, grupo)  # redesenha pra mostrar o volume do novo padrão

    def ao_volume(self, barra, sink_ou_source):
        if not self.atualizando:
            rodar(["pactl", f"set-{sink_ou_source}-volume", f"@DEFAULT_{sink_ou_source.upper()}@",
                   f"{int(barra.get_value())}%"])

    def ao_fixo(self, linha, _param, cartao, tipo, grupo):
        if self.atualizando:
            return
        ligado = linha.get_active()
        linha.set_sensitive(False)
        # O aero-audio reinicia o WirePlumber (o som pisca por um instante): fora da janela.
        def trocar():
            r = rodar([AERO_AUDIO, "set", cartao, "on" if ligado else "off"])
            time.sleep(2)  # espera o PipeWire mostrar as saídas de novo
            return r
        em_segundo_plano(trocar, lambda _r: self.montar(tipo, grupo))

    def ao_mudo(self, botao, sink_ou_source, entrada):
        self.icone_mudo(botao, entrada)
        if not self.atualizando:
            rodar(["pactl", f"set-{sink_ou_source}-mute", f"@DEFAULT_{sink_ou_source.upper()}@",
                   "1" if botao.get_active() else "0"])
