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
"""
from gi.repository import Adw, Gtk

from comum import (Secao, botao, confirmar, em_segundo_plano, limpar_grupo, linha_app, linha_seta,
                   pagina, rodar)


def ctl(*args):
    return rodar(["bluetoothctl", *args])


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
        self.atualizando = False
        self.montar_meus()

    def ao_ligar(self, *_):
        if self.atualizando:
            return
        ctl("power", "on" if self.chave.get_active() else "off")
        self.montar_meus()

    def montar_meus(self):
        limpar_grupo(self.meus, self.linhas_meus)
        on = self.chave.get_active()
        self.outros.set_sensitive(on)
        for mac, nome in listar("Paired"):
            d = info(mac)
            status = "Connected" if d.get("Connected") == "yes" else "Not connected"
            linha = linha_seta(nome, status, icone(d), lambda m=mac: self.abrir_aparelho(m))
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
        if d.get("Battery Percentage"):
            # vem como "0x50 (80)": o número entre parênteses é a %
            estado.add(Adw.ActionRow(use_markup=False, title="Battery",
                                     subtitle=d["Battery Percentage"].split("(")[-1].rstrip(")") + "%"))

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

    def executar(self, acao, mac, nome):
        textos = {"connect": "Connected to", "disconnect": "Disconnected", "remove": "Forgot"}
        self.janela.avisar(f"Working on {nome}…")

        def depois(r):
            ok = getattr(r, "returncode", 1) == 0
            self.janela.avisar(f"{textos[acao]} {nome}" if ok else f"Could not {acao} {nome}")
            self.janela.voltar()
            self.montar_meus()
        em_segundo_plano(lambda: ctl(acao, mac), depois)

    # ----- procurar e parear -----

    def procurar(self):
        self.botao_procurar.set_sensitive(False)
        self.girando.set_visible(True)
        limpar_grupo(self.outros, self.linhas_outros)

        def depois(_):
            self.girando.set_visible(False)
            self.botao_procurar.set_sensitive(True)
            pareados = {mac for mac, _ in listar("Paired")}
            for mac, nome in listar():
                if mac in pareados:
                    continue
                linha = Adw.ActionRow(use_markup=False, title=nome, subtitle=mac)
                linha.add_prefix(Gtk.Image.new_from_icon_name(icone(info(mac))))
                linha.add_suffix(botao("Pair", lambda m=mac, n=nome: self.parear(m, n), "pill"))
                self.outros.add(linha)
                self.linhas_outros.append(linha)
            if not self.linhas_outros:
                vazio = Adw.ActionRow(use_markup=False, title="Nothing found", subtitle="Is the device in pairing mode?")
                self.outros.add(vazio)
                self.linhas_outros.append(vazio)
        em_segundo_plano(lambda: ctl("--timeout", "10", "scan", "on"), depois)

    def parear(self, mac, nome):
        self.janela.avisar(f"Pairing with {nome}…")

        def trabalho():
            for passo in ("pair", "trust", "connect"):   # trust = reconectar sozinho no futuro
                r = ctl("--timeout", "20", passo, mac)
                if passo == "pair" and r.returncode != 0 and "AlreadyExists" not in r.stdout:
                    return False
            return True

        def depois(ok):
            self.janela.avisar(f"Paired with {nome}" if ok is True else
                               f"Pairing with {nome} failed. Try the advanced settings.")
            limpar_grupo(self.outros, self.linhas_outros)
            self.montar_meus()
        em_segundo_plano(trabalho, depois)
