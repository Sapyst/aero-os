"""
sec_bluetooth.py — seção Bluetooth.

Usa o bluetoothctl (ferramenta do bluez, o serviço de Bluetooth do Linux):
  bluetoothctl show                 estado do adaptador (Powered: yes/no)
  bluetoothctl power on|off         liga/desliga
  bluetoothctl devices Paired       aparelhos pareados ("Device AA:BB:.. Nome")
  bluetoothctl info MAC             detalhes (Connected, Icon...)
  bluetoothctl --timeout 10 scan on procura aparelhos por 10 s
  bluetoothctl pair|trust|connect|disconnect|remove MAC
MAC = o "endereço" único do aparelho, tipo AA:BB:CC:DD:EE:FF.

Conectar e parear passam pelo componente do sistema `aero-bluetooth` (~/.local/bin/aero-bluetooth,
serviço aero-bluetooth.service): ele mantém o agente de pareamento rodando (quem responde quando o
aparelho pede código/confirmação), liga o Bluetooth no login se "Turn on at login" estiver ligado,
marca os aparelhos como confiáveis (assim reconectam sozinhos) e tenta de novo quando a conexão falha.
"""
import os
from gi.repository import Adw, GLib, Gtk

from comum import (Secao, botao, confirmar, em_segundo_plano, limpar_grupo, linha_app, linha_seta,
                   pagina, rodar)


AERO_BT = os.path.expanduser("~/.local/bin/aero-bluetooth")
PREFS = os.path.expanduser("~/.config/aero/bluetooth.conf")


def ctl(*args):
    return rodar(["bluetoothctl", *args])


def liga_no_login():
    try:
        return "startup=off" not in open(PREFS).read()
    except FileNotFoundError:
        return True


def bateria(dados):
    """"0x50 (80)" -> "80%"."""
    b = dados.get("Battery Percentage")
    return b.split("(")[-1].rstrip(")") + "%" if b else ""


def status(dados):
    partes = ["Connected" if dados.get("Connected") == "yes" else "Not connected"]
    if bateria(dados):
        partes.append(bateria(dados))
    return "  ·  ".join(partes)


def ligado():
    return "Powered: yes" in ctl("show").stdout


def listar(filtro=None):
    """[(mac, nome)] de `bluetoothctl devices [filtro]`."""
    r = ctl("devices", *([filtro] if filtro else []))
    itens = []
    for linha in r.stdout.splitlines():
        partes = linha.split(" ", 2)
        if len(partes) == 3 and partes[0] == "Device":
            mac, nome = partes[1], partes[2]
            if nome.replace("-", ":") != mac:        # sem nome de verdade = ignora
                itens.append((mac, nome))
    return itens


def info(mac):
    """Lê `bluetoothctl info` como dicionário: {"Connected": "yes", "Icon": "audio-headset", ...}"""
    dados = {}
    for linha in ctl("info", mac).stdout.splitlines():
        if ":" in linha:
            k, v = linha.strip().split(":", 1)
            dados.setdefault(k.strip(), v.strip())
    return dados


def icone(dados):
    return (dados.get("Icon") or "bluetooth") + "-symbolic"


class SecaoBluetooth(Secao):
    id = "bluetooth"
    titulo = "Bluetooth"
    icone = "bluetooth-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()

        topo = Adw.PreferencesGroup()
        self.chave = Adw.SwitchRow(title="Bluetooth")
        self.chave.connect("notify::active", self.ao_ligar)
        topo.add(self.chave)
        self.no_login = Adw.SwitchRow(title="Turn on at login", subtitle="Devices you trust reconnect by themselves")
        self.no_login.connect("notify::active", self.ao_no_login)
        topo.add(self.no_login)

        self.meus = Adw.PreferencesGroup(title="My devices")
        self.outros = Adw.PreferencesGroup(title="Other devices",
                                           description="Put the device in pairing mode, then search.")
        self.botao_procurar = botao("Search", self.procurar, "flat", "system-search-symbolic")
        self.girando = Adw.Spinner(visible=False)
        sufixo = Gtk.Box(spacing=6)
        sufixo.append(self.girando)
        sufixo.append(self.botao_procurar)
        self.outros.set_header_suffix(sufixo)

        avancado = Adw.PreferencesGroup()
        avancado.add(linha_app("Advanced Bluetooth settings", "Blueman: file transfer, PIN pairing",
                               ["blueman-manager"], "preferences-system-symbolic"))

        for g in (topo, self.meus, self.outros, avancado):
            conteudo.add(g)
        self.linhas_meus, self.linhas_outros = [], []
        self.atualizando = False
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.atualizando = True
        self.chave.set_active(ligado())
        self.no_login.set_active(liga_no_login())
        self.atualizando = False
        self.montar_meus()

    def ao_ligar(self, *_):
        if self.atualizando:
            return
        ctl("power", "on" if self.chave.get_active() else "off")
        self.montar_meus()

    def ao_no_login(self, *_):
        if not self.atualizando:
            rodar([AERO_BT, "startup", "on" if self.no_login.get_active() else "off"])

    def montar_meus(self):
        limpar_grupo(self.meus, self.linhas_meus)
        on = self.chave.get_active()
        self.outros.set_sensitive(on)
        for mac, nome in listar("Paired"):
            d = info(mac)
            conectado = d.get("Connected") == "yes"
            linha = linha_seta(nome, status(d), icone(d), lambda m=mac: self.abrir_aparelho(m))
            # Conectar/desconectar direto da lista, sem abrir a página do aparelho.
            rapido = botao("Disconnect" if conectado else "Connect",
                           lambda m=mac, n=nome, c=conectado: self.executar("disconnect" if c else "connect", m, n, voltar=False),
                           "flat")
            rapido.set_valign(Gtk.Align.CENTER)
            linha.add_suffix(rapido)
            linha.set_sensitive(on)
            self.meus.add(linha)
            self.linhas_meus.append(linha)
        if not self.linhas_meus:
            vazio = Adw.ActionRow(use_markup=False, title="No paired devices")
            self.meus.add(vazio)
            self.linhas_meus.append(vazio)

    # ----- sub-página de um aparelho -----

    def abrir_aparelho(self, mac):
        d = info(mac)
        nome = d.get("Alias") or d.get("Name") or mac
        conteudo = Adw.PreferencesPage()

        estado = Adw.PreferencesGroup()
        conectado = d.get("Connected") == "yes"
        estado.add(Adw.ActionRow(use_markup=False, title="Status", subtitle="Connected" if conectado else "Not connected"))
        estado.add(Adw.ActionRow(use_markup=False, title="Address", subtitle=mac))
        if bateria(d):
            estado.add(Adw.ActionRow(use_markup=False, title="Battery", subtitle=bateria(d)))
        # Confiável = o aparelho pode se conectar sozinho quando é ligado.
        auto = Adw.SwitchRow(title="Connect automatically", subtitle="Reconnect when the device is turned on",
                             active=d.get("Trusted") == "yes")
        auto.connect("notify::active", lambda r, *_: ctl("trust" if r.get_active() else "untrust", mac))
        estado.add(auto)

        acoes = Adw.PreferencesGroup()
        caixa = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        acao = "disconnect" if conectado else "connect"
        caixa.append(botao("Disconnect" if conectado else "Connect",
                           lambda: self.executar(acao, mac, nome), "pill suggested-action"))
        caixa.append(botao("Forget", lambda: confirmar(
            self.janela, f"Forget {nome}?", "You will need to pair it again to use it.",
            "Forget", lambda: self.executar("remove", mac, nome), destrutivo=True), "pill destructive-action"))
        acoes.add(caixa)

        conteudo.add(estado)
        conteudo.add(acoes)
        self.janela.empilhar(pagina(nome, conteudo))

    def executar(self, acao, mac, nome, voltar=True):
        textos = {"connect": "Connected to", "disconnect": "Disconnected", "remove": "Forgot"}
        self.janela.avisar(f"Connecting to {nome}…" if acao == "connect" else f"Working on {nome}…")

        def depois(r):
            ok = getattr(r, "returncode", 1) == 0
            self.janela.avisar(f"{textos[acao]} {nome}" if ok else
                               f"Could not {acao} {nome}" + (". Is it on and nearby?" if acao == "connect" else ""))
            if voltar:
                self.janela.voltar()
            self.montar_meus()
        # Conectar passa pelo aero-bluetooth: liga o Bluetooth, confia no aparelho e tenta de novo.
        cmd = [AERO_BT, "connect", mac] if acao == "connect" and os.path.exists(AERO_BT) else ["bluetoothctl", acao, mac]
        em_segundo_plano(lambda: rodar(cmd), depois)

    # ----- procurar e parear -----

    def procurar(self):
        self.botao_procurar.set_sensitive(False)
        self.girando.set_visible(True)
        limpar_grupo(self.outros, self.linhas_outros)

        def depois(_):
            self.girando.set_visible(False)
            self.botao_procurar.set_sensitive(True)
            self.mostrar_achados(final=True)
        # Enquanto procura, a lista vai aparecendo (a cada 1,5 s), em vez de só no fim.
        self.procurando = True

        def mostrar_parcial():
            if not self.procurando:
                return False
            self.mostrar_achados(final=False)
            return True
        GLib.timeout_add(1500, mostrar_parcial)

        def fim(r):
            self.procurando = False
            depois(r)
        ctl("pairable", "on")
        em_segundo_plano(lambda: ctl("--timeout", "15", "scan", "on"), fim)

    def mostrar_achados(self, final):
        limpar_grupo(self.outros, self.linhas_outros)
        pareados = {mac for mac, _ in listar("Paired")}
        for mac, nome in listar():
            if mac in pareados:
                continue
            linha = Adw.ActionRow(use_markup=False, title=nome, subtitle=mac)
            linha.add_prefix(Gtk.Image.new_from_icon_name(icone(info(mac))))
            linha.add_suffix(botao("Pair", lambda m=mac, n=nome: self.parear(m, n), "pill"))
            self.outros.add(linha)
            self.linhas_outros.append(linha)
        if final and not self.linhas_outros:
            vazio = Adw.ActionRow(use_markup=False, title="Nothing found", subtitle="Is the device in pairing mode?")
            self.outros.add(vazio)
            self.linhas_outros.append(vazio)

    def parear(self, mac, nome):
        self.janela.avisar(f"Pairing with {nome}…")

        def trabalho():
            # aero-bluetooth pair = parear + confiar (reconectar sozinho) + conectar, com novas tentativas.
            # O agente dele responde aos pedidos de código/confirmação do aparelho.
            return rodar([AERO_BT, "pair", mac]).returncode == 0

        def depois(ok):
            self.janela.avisar(f"Paired with {nome}" if ok is True else
                               f"Pairing with {nome} failed. Try the advanced settings.")
            limpar_grupo(self.outros, self.linhas_outros)
            self.montar_meus()
        em_segundo_plano(trabalho, depois)
