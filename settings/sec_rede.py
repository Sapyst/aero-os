"""
sec_rede.py — seção Network (Wi-Fi e cabo).

Usa o nmcli (linha de comando do NetworkManager, o serviço que cuida da rede):
  nmcli -t ...                  -t = saída "terse": campos separados por ":" (fácil de ler no código)
  nmcli radio wifi on|off       liga/desliga o Wi-Fi
  nmcli device wifi list        redes ao alcance
  nmcli device wifi connect SSID password SENHA
  nmcli connection up|down|delete id NOME   conectar/desconectar/esquecer uma rede salva
SSID = o nome da rede Wi-Fi.
"""
import json
import os

from gi.repository import Adw, Gtk

from comum import (Secao, botao, confirmar, em_segundo_plano, limpar_grupo, linha_app, linha_seta,
                   pagina, rodar, rodar_como_root)

PAIS = "/usr/local/lib/aero-geo/aero-wifi-country"     # root helper (pkexec): Wi-Fi country
ESTADO_PAIS = "/var/lib/aero-geo/state.json"
FONTES = {"routers": "from nearby networks", "timezone": "from the time zone", "ip": "from internet location",
          "manual": "set by you"}


def ler_paises():
    """[(name, code)] from the system's own table, sorted by name."""
    nomes = []
    try:
        with open("/usr/share/zoneinfo/iso3166.tab", encoding="utf-8") as f:
            for linha in f:
                partes = linha.rstrip("\n").split("\t")
                if len(partes) == 2 and len(partes[0]) == 2 and partes[0].isalpha():
                    nomes.append((partes[1], partes[0]))
    except OSError:
        pass
    return sorted(nomes)


def separar(linha):
    """No modo -t, um ":" dentro de um valor vem escapado como "\\:". Separa respeitando isso."""
    partes, atual, i = [], "", 0
    while i < len(linha):
        c = linha[i]
        if c == "\\" and i + 1 < len(linha):
            atual += linha[i + 1]
            i += 2
            continue
        if c == ":":
            partes.append(atual)
            atual = ""
        else:
            atual += c
        i += 1
    partes.append(atual)
    return partes


def nmcli(*args):
    return rodar(["nmcli", *args])


def tabela(*args):
    return [separar(l) for l in nmcli("-t", *args).stdout.splitlines() if l]


def icone_sinal(sinal):
    nivel = "excellent" if sinal >= 75 else "good" if sinal >= 50 else "ok" if sinal >= 25 else "weak"
    return f"network-wireless-signal-{nivel}-symbolic"


class SecaoRede(Secao):
    id = "rede"
    titulo = "Network"
    icone = "network-wireless-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()

        self.status = Adw.PreferencesGroup(title="Status")
        wifi = Adw.PreferencesGroup()
        self.chave = Adw.SwitchRow(title="Wi-Fi")
        self.chave.connect("notify::active", self.ao_ligar)
        wifi.add(self.chave)

        self.redes = Adw.PreferencesGroup(title="Networks")
        self.girando = Adw.Spinner(visible=False)
        self.botao_buscar = botao("Scan again", lambda: self.montar_redes(buscar=True), "flat",
                                  "view-refresh-symbolic")
        sufixo = Gtk.Box(spacing=6)
        sufixo.append(self.girando)
        sufixo.append(self.botao_buscar)
        self.redes.set_header_suffix(sufixo)

        self.salvas = Adw.PreferencesGroup(title="Saved networks")
        self.grupo_pais = Adw.PreferencesGroup(
            title="Wi-Fi country", description="Decides which channels and bands are allowed (6 GHz needs it). "
                                               "Checked only when you join a new network.")
        self.pais_atual = Adw.ActionRow(title="Now")
        self.pais_atual.set_use_markup(False)
        self.paises = ler_paises()
        self.pais_combo = Adw.ComboRow(title="Country", model=Gtk.StringList.new(
            ["Automatic"] + [f"{n} ({c})" for n, c in self.paises]))
        self.pais_combo.set_enable_search(True)
        self.pais_combo.connect("notify::selected", self.ao_mudar_pais)
        self.pais_ip = Adw.SwitchRow(title="Use internet location",
                                     subtitle="Only when nearby networks and the time zone can't tell. "
                                              "Sends your IP address to ipinfo.io")
        self.pais_ip.connect("notify::active", self.ao_mudar_ip)
        for linha in (self.pais_atual, self.pais_combo, self.pais_ip):
            self.grupo_pais.add(linha)

        avancado = Adw.PreferencesGroup()
        avancado.add(linha_app("Advanced network settings", "IP, DNS, VPN, wired connections",
                               ["nm-connection-editor"], "preferences-system-symbolic"))

        for g in (self.status, wifi, self.redes, self.salvas, self.grupo_pais, avancado):
            conteudo.add(g)
        self.linhas = {"status": [], "redes": [], "salvas": []}
        self.atualizando = False
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.atualizando = True
        self.chave.set_active(nmcli("radio", "wifi").stdout.strip() == "enabled")
        self.atualizando = False
        self.montar_status()
        self.montar_redes()
        self.montar_salvas()
        self.montar_pais()

    # ---------- Wi-Fi country (aero-wifi-country) ----------

    def montar_pais(self):
        instalado = os.access(PAIS, os.X_OK)
        self.grupo_pais.set_visible(instalado)
        if not instalado:
            return
        try:
            with open(ESTADO_PAIS, encoding="utf-8") as f:
                estado = json.load(f)
        except (OSError, ValueError):
            estado = {}
        try:
            conf = dict(linha.strip().partition("=")[::2] for linha in open("/etc/aero/wifi-country.conf")
                        if "=" in linha and not linha.startswith("#"))
        except OSError:
            conf = {}
        codigo = estado.get("country", "")
        nome = next((n for n, c in self.paises if c == codigo), codigo)
        self.pais_atual.set_subtitle(f"{nome} ({codigo}) · {FONTES.get(estado.get('source'), '')}"
                                     if codigo else "Not decided yet")
        self.atualizando = True
        manual = conf.get("mode") == "manual"
        indice_pais = next((i for i, (_, c) in enumerate(self.paises) if c == conf.get("country")), None)
        self.pais_combo.set_selected(indice_pais + 1 if manual and indice_pais is not None else 0)
        self.pais_ip.set_active(conf.get("ip_lookup") == "1")
        self.atualizando = False

    def mudar_pais(self, args):
        def depois(r):
            ok = getattr(r, "returncode", 1) == 0
            self.janela.avisar("Wi-Fi country updated" if ok else "Not changed (password cancelled or error)")
            self.montar_pais()
        em_segundo_plano(lambda: rodar_como_root([PAIS, "set"] + args), depois)

    def ao_mudar_pais(self, *_):
        if self.atualizando:
            return
        i = self.pais_combo.get_selected()
        self.mudar_pais(["mode", "auto"] if i == 0 else ["country", self.paises[i - 1][1]])

    def ao_mudar_ip(self, *_):
        if not self.atualizando:
            self.mudar_pais(["ip_lookup", "1" if self.pais_ip.get_active() else "0"])

    def ao_ligar(self, *_):
        if not self.atualizando:
            nmcli("radio", "wifi", "on" if self.chave.get_active() else "off")
            self.ao_mostrar()

    def montar_status(self):
        limpar_grupo(self.status, self.linhas["status"])
        for dispositivo, tipo, estado, conexao in tabela("-f", "DEVICE,TYPE,STATE,CONNECTION", "device"):
            if tipo not in ("wifi", "ethernet"):
                continue
            nome = "Wi-Fi" if tipo == "wifi" else "Ethernet (cable)"
            if estado == "connected":
                sub, ic = f"Connected to {conexao}", "network-transmit-receive-symbolic"
            elif estado == "unavailable":
                sub, ic = "Cable unplugged" if tipo == "ethernet" else "Off", "network-offline-symbolic"
            else:
                sub, ic = "Not connected", "network-offline-symbolic"
            linha = Adw.ActionRow(use_markup=False, title=nome, subtitle=sub)
            linha.add_prefix(Gtk.Image.new_from_icon_name(ic))
            self.status.add(linha)
            self.linhas["status"].append(linha)

    def montar_redes(self, buscar=False):
        limpar_grupo(self.redes, self.linhas["redes"])
        if not self.chave.get_active():
            return
        self.girando.set_visible(True)
        self.botao_buscar.set_sensitive(False)

        def ler():
            # --rescan yes = procurar de novo agora (demora alguns segundos)
            return tabela("-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list",
                          "--rescan", "yes" if buscar else "auto")

        def depois(resultado):
            self.girando.set_visible(False)
            self.botao_buscar.set_sensitive(True)
            if isinstance(resultado, Exception):
                return
            melhores = {}  # a mesma rede aparece várias vezes (um por antena): fica a de sinal mais forte
            for em_uso, ssid, sinal, seguranca in resultado:
                if not ssid:
                    continue                     # rede oculta
                sinal = int(sinal or 0)
                atual = melhores.get(ssid)
                if atual is None or em_uso == "*" or (atual[0] != "*" and sinal > atual[1]):
                    melhores[ssid] = (em_uso, sinal, seguranca)
            for ssid, (em_uso, sinal, seguranca) in sorted(
                    melhores.items(), key=lambda kv: (kv[1][0] != "*", -kv[1][1])):
                protegida = seguranca not in ("", "--")
                sub = ("Connected · " if em_uso == "*" else "") + (seguranca if protegida else "Open")
                linha = Adw.ActionRow(use_markup=False, title=ssid, subtitle=sub, activatable=True)
                linha.add_prefix(Gtk.Image.new_from_icon_name(icone_sinal(sinal)))
                if protegida:
                    linha.add_suffix(Gtk.Image.new_from_icon_name("system-lock-screen-symbolic"))
                if em_uso == "*":
                    linha.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                    linha.connect("activated", lambda *_, s=ssid: self.abrir_rede(s))
                else:
                    linha.connect("activated", lambda *_, s=ssid, p=protegida: self.conectar(s, p))
                self.redes.add(linha)
                self.linhas["redes"].append(linha)
        em_segundo_plano(ler, depois)

    def nomes_salvos(self):
        return [nome for nome, tipo in tabela("-f", "NAME,TYPE", "connection", "show")
                if tipo == "802-11-wireless"]

    def montar_salvas(self):
        limpar_grupo(self.salvas, self.linhas["salvas"])
        for nome in self.nomes_salvos():
            linha = Adw.ActionRow(use_markup=False, title=nome)
            linha.add_suffix(botao("Forget", lambda n=nome: self.esquecer(n), "flat", "user-trash-symbolic"))
            self.salvas.add(linha)
            self.linhas["salvas"].append(linha)
        if not self.linhas["salvas"]:
            vazio = Adw.ActionRow(use_markup=False, title="No saved networks")
            self.salvas.add(vazio)
            self.linhas["salvas"].append(vazio)

    # ----- conectar -----

    def conectar(self, ssid, protegida):
        if ssid in self.nomes_salvos():
            self.executar(["connection", "up", "id", ssid], f"Connected to {ssid}")
        elif not protegida:
            self.executar(["device", "wifi", "connect", ssid], f"Connected to {ssid}")
        else:
            self.pedir_senha(ssid)

    def pedir_senha(self, ssid):
        d = Adw.AlertDialog(heading=f"Connect to {ssid}", body="Enter the Wi-Fi password.")
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lista.add_css_class("boxed-list")
        senha = Adw.PasswordEntryRow(title="Password")
        lista.append(senha)
        d.set_extra_child(lista)
        d.add_response("cancel", "Cancel")
        d.add_response("ok", "Connect")
        d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("ok")

        def resposta(_d, r):
            if r == "ok" and senha.get_text():
                self.executar(["device", "wifi", "connect", ssid, "password", senha.get_text()],
                              f"Connected to {ssid}")
        d.connect("response", resposta)
        d.present(self.janela)

    def executar(self, args, sucesso):
        self.janela.avisar("Connecting…" if "connect" in args or "up" in args else "Working…")

        def depois(r):
            ok = getattr(r, "returncode", 1) == 0
            self.janela.avisar(sucesso if ok else "Failed. Check the password and try again.")
            self.ao_mostrar()
        em_segundo_plano(lambda: nmcli(*args), depois)

    # ----- sub-página da rede conectada -----

    def abrir_rede(self, ssid):
        dispositivo = next((d for d, t, *_ in tabela("-f", "DEVICE,TYPE", "device") if t == "wifi"), "")
        dados = dict(tuple(l[:2]) for l in tabela("-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS",
                                                    "device", "show", dispositivo) if len(l) >= 2)
        conteudo = Adw.PreferencesPage()
        detalhes = Adw.PreferencesGroup()
        for chave, titulo in (("IP4.ADDRESS[1]", "IP address"), ("IP4.GATEWAY", "Router"),
                              ("IP4.DNS[1]", "DNS")):
            if dados.get(chave):
                detalhes.add(Adw.ActionRow(use_markup=False, title=titulo, subtitle=dados[chave]))
        acoes = Adw.PreferencesGroup()
        caixa = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        caixa.append(botao("Disconnect", lambda: (self.janela.voltar(), self.executar(
            ["connection", "down", "id", ssid], f"Disconnected from {ssid}")), "pill"))
        caixa.append(botao("Forget", lambda: (self.janela.voltar(), self.esquecer(ssid)),
                           "pill destructive-action"))
        acoes.add(caixa)
        conteudo.add(detalhes)
        conteudo.add(acoes)
        self.janela.empilhar(pagina(ssid, conteudo))

    def esquecer(self, nome):
        confirmar(self.janela, f"Forget {nome}?", "The saved password will be deleted.", "Forget",
                  lambda: self.executar(["connection", "delete", "id", nome], f"Forgot {nome}"),
                  destrutivo=True)
