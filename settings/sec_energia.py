"""
sec_energia.py — seção Power (tampa, botão de energia, inatividade).

Inatividade (doc 25): gera ~/.config/hypr/hypridle.conf (travar, apagar a tela, dormir). Padrão: nunca.

Lê a config atual do systemd-logind pelo D-Bus (busctl) e grava pelo ajudante de root
/usr/local/bin/painel-energia, que só aceita valores de uma lista fechada (doc 10).
"""
import os
import shutil
import sys

from gi.repository import Adw, Gtk

from comum import (Secao, atualizar_painel, combo, em_segundo_plano, grupo_botao_aplicar, indice, ler_painel,
                   pagina, rodar, rodar_como_root, valor_de)

sys.path.insert(0, os.path.expanduser("~/.local/share/aero-shell"))
from servicos import energia, estado  # noqa: E402

AJUDANTE = "/usr/local/bin/painel-energia"
# Energia inteligente (daemon aero-energia = aero-shell/bateria.py; State: ~/.config/aero-shell/energia.json)
ENERGIA_PADRAO = {"modo": "smart", "limite": 80, "perfil_auto": True, "perfil_tomada": "balanced", "critico": 6,
                  "cheia_uma_vez": False, "segurar": None}
LIMITADOR = [("Smart", "smart"), ("Fixed limit", "fixo"), ("Direct power", "bypass"), ("Off", "desligado")]
EXPLICA_LIMITADOR = {
    "smart": "Stops at 80%, and at 60% after a full day on the charger (the healthiest level for a battery "
             "that lives plugged in). Then the laptop runs straight from the charger",
    "fixo": "Stops at the limit below. Then the laptop runs straight from the charger",
    "bypass": "Holds the battery at the level it had when you picked this (60% minimum): the laptop runs only "
              "from the charger and the battery rests",
    "desligado": "Charges to 100%"}
LIMITES = [("90%", "90"), ("80%", "80"), ("70%", "70"), ("60%", "60")]
NA_TOMADA = [("Balanced", "balanced"), ("Performance", "performance")]
CRITICO = [("Off", "0"), ("At 10%", "10"), ("At 6%", "6"), ("At 3%", "3")]
SCRIPT_DRIVER = os.path.expanduser("~/.local/bin/aero-bateria-driver")
TAMPA_COM_MONITOR = [("Just turn off the screen", "ignore"), ("Sleep", "suspend")]
TAMPA_SEM_MONITOR = [("Sleep", "suspend"), ("Do nothing", "ignore")]
# Na tomada, o padrão do Aero OS é seguir trabalhando: quem desliga o monitor (e ele "some" do
# USB-C/HDMI) com a tampa fechada não quer que o PC durma no meio de um download ou automação.
TAMPA_TOMADA = [("Keep working (screen off)", "ignore"), ("Sleep", "suspend")]
BOTAO_TOQUE = [("Sleep", "suspend"), ("Shut down", "poweroff"), ("Do nothing", "ignore")]
BOTAO_SEGURAR = [("Shut down", "poweroff"), ("Do nothing", "ignore")]

# Inatividade (doc 25): o hypridle conta o tempo parado e roda os comandos. Minutos; "0" = nunca.
TRAVAR = [("Never", "0"), ("5 minutes", "5"), ("10 minutes", "10"), ("15 minutes", "15"), ("30 minutes", "30")]
APAGAR = [("Never", "0"), ("5 minutes", "5"), ("10 minutes", "10"), ("15 minutes", "15"),
          ("30 minutes", "30"), ("1 hour", "60")]
DORMIR = [("Never", "0"), ("15 minutes", "15"), ("30 minutes", "30"), ("1 hour", "60"), ("2 hours", "120")]
HYPRIDLE = os.path.expanduser("~/.config/hypr/hypridle.conf")
# Modo de energia (doc 25): o power-profiles-daemon troca o "perfil" da placa (ventoinha, frequência)
MODOS = [("Power saver", "power-saver"), ("Balanced", "balanced"), ("Performance", "performance")]


def gerar_hypridle(travar, apagar, dormir, travar_ao_dormir):
    """Escreve o hypridle.conf. Cada "listener" = depois de X segundos parado, roda on-timeout;
    quando você mexe de novo, roda on-resume."""
    linhas = ["# Gerado pelo Settings → Power → Inactivity (sec_energia.py). Não editar à mão.",
              "general {",
              "    lock_cmd = pidof hyprlock || hyprlock          # o que \"travar\" significa",
              "    after_sleep_cmd = hyprctl dispatch dpms on      # ao acordar, liga as telas"]
    if travar_ao_dormir:
        linhas.append("    before_sleep_cmd = loginctl lock-session      # trava antes de dormir")
    linhas.append("}")
    for minutos, ligar, desligar in ((travar, "loginctl lock-session", None),
                                     (apagar, "hyprctl dispatch dpms off", "hyprctl dispatch dpms on"),
                                     (dormir, "systemctl suspend", None)):
        if minutos != "0":
            linhas += ["listener {", f"    timeout = {int(minutos) * 60}", f"    on-timeout = {ligar}"]
            if desligar:
                linhas.append(f"    on-resume = {desligar}")
            linhas.append("}")
    with open(HYPRIDLE, "w") as f:
        f.write("\n".join(linhas) + "\n")


def ler_logind(prop):
    r = rodar(["busctl", "get-property", "org.freedesktop.login1", "/org/freedesktop/login1",
               "org.freedesktop.login1.Manager", prop])
    # a saída vem assim: s "suspend"
    return r.stdout.strip().split(" ", 1)[-1].strip('"') if r.returncode == 0 else ""


class SecaoEnergia(Secao):
    id = "energia"
    titulo = "Power"
    icone = "system-shutdown-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        conteudo = Adw.PreferencesPage()

        tampa = Adw.PreferencesGroup(title="Lid")
        self.tampa_com = combo("Close with a monitor connected", TAMPA_COM_MONITOR, 0)
        self.tampa_sem = combo("Close without a monitor, on battery", TAMPA_SEM_MONITOR, 0,
                               "Sleep is safest: keeps the laptop from overheating in a bag")
        self.tampa_tomada = combo("Close without a monitor, plugged in", TAMPA_TOMADA, 0,
                                  "Also when you turn the monitor off. Keep working lets downloads and tasks finish")
        tampa.add(self.tampa_com)
        tampa.add(self.tampa_sem)
        tampa.add(self.tampa_tomada)

        botao = Adw.PreferencesGroup(
            title="Power button",
            description="Holding it for ~10 seconds always forces a hardware shutdown, "
                        "even if the system is frozen.")
        self.toque = combo("Press once", BOTAO_TOQUE, 0)
        self.segurar = combo("Hold", BOTAO_SEGURAR, 0)
        botao.add(self.toque)
        botao.add(self.segurar)

        instalado = shutil.which("hyprlock") and shutil.which("hypridle")
        sono = Adw.PreferencesGroup(
            title="Inactivity", description=None if instalado else
            "Needs the screen locker. Install it in a terminal: sudo pacman -S hyprlock hypridle")
        self.travar = combo("Lock the screen after", TRAVAR, 0, "Super+L locks it anytime")
        self.apagar = combo("Turn off the screen after", APAGAR, 0)
        self.dormir = combo("Sleep after", DORMIR, 0)
        self.travar_ao_dormir = Adw.SwitchRow(title="Lock before sleeping",
                                              subtitle="Asks for your password when the laptop wakes up")
        for r in (self.travar, self.apagar, self.dormir, self.travar_ao_dormir):
            sono.add(r)

        cfg = estado.ler("energia", ENERGIA_PADRAO)
        self.carregando = True
        modo = Adw.PreferencesGroup(title="Power mode")
        self.perfil_auto = Adw.SwitchRow(title="Automatic", active=cfg["perfil_auto"],
                                         subtitle="Performance while a game is open, power saver on battery")
        self.perfil_tomada = combo("On the charger", NA_TOMADA, indice(NA_TOMADA, cfg["perfil_tomada"]))
        if shutil.which("powerprofilesctl"):
            self.modo = combo("Mode", MODOS, 1, "Power saver: quieter and longer battery. "
                                                "Performance: games and heavy apps")
            self.modo.connect("notify::selected", lambda *_: None if self.carregando_modo else
                              rodar(["powerprofilesctl", "set", valor_de(self.modo, MODOS)]))
            for r in (self.perfil_auto, self.perfil_tomada, self.modo):
                modo.add(r)
        else:
            self.modo = None
            modo.add(Adw.ActionRow(use_markup=False, title="Mode",
                                   subtitle="Needs power-profiles-daemon. Install it in a terminal: "
                                            "sudo pacman -S power-profiles-daemon"))
        self.carregando_modo = False

        # Bateria: estado + limitador inteligente + salvar o momento (Service: servicos/energia.py)
        self.bat = Adw.PreferencesGroup(title="Battery",
                                        description="Applies on its own. Clicking the battery in the taskbar "
                                                    "turns the limiter on and off.")
        self.bat_estado = Adw.ActionRow(use_markup=False, title="Battery")
        self.limitador = combo("Charge limiter", LIMITADOR, indice(LIMITADOR, cfg["modo"]),
                               EXPLICA_LIMITADOR.get(cfg["modo"], ""))
        self.limite_fixo = combo("Limit", LIMITES, indice(LIMITES, str(cfg["limite"]), 1))
        self.cheia = Adw.ActionRow(use_markup=False, title="Charge to 100% once",
                                   subtitle="For a trip: ignores the limiter until you unplug")
        self.botao_cheia = Gtk.Button(label="Charge to full", valign=Gtk.Align.CENTER)
        self.botao_cheia.connect("clicked", lambda *_: self.carregar_cheia())
        self.cheia.add_suffix(self.botao_cheia)
        self.critico = combo("Save the moment", CRITICO, indice(CRITICO, str(cfg["critico"]), 2))
        self.driver = Adw.ActionRow(use_markup=False, title="Battery driver")
        self.botao_driver = Gtk.Button(label="How to install", valign=Gtk.Align.CENTER)
        self.botao_driver.connect("clicked", lambda *_: self.explicar_driver())
        self.driver.add_suffix(self.botao_driver)
        for r in (self.bat_estado, self.limitador, self.limite_fixo, self.cheia, self.critico, self.driver):
            self.bat.add(r)
        for r in (self.perfil_auto, self.limitador, self.limite_fixo, self.critico, self.perfil_tomada):
            r.connect("notify::active" if isinstance(r, Adw.SwitchRow) else "notify::selected",
                      lambda *_: self.gravar_energia())
        self.carregando = False

        for g in (tampa, botao, modo, self.bat, sono, grupo_botao_aplicar(self.aplicar)):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def mostrar_bateria(self):
        b = energia.bateria()
        self.bat.set_visible(b is not None)
        if b is None:
            return
        cfg = estado.ler("energia", ENERGIA_PADRAO)
        tomada = energia.na_tomada()
        partes = [f"{b['nivel']}%", {"Charging": "charging", "Discharging": "on battery", "Full": "full",
                                     "Not charging": "plugged in, not charging"}.get(b["estado"], b["estado"].lower())]
        if b["saude"]:
            partes.append(f"health {b['saude']}% of new")
        if b["ciclos"]:
            partes.append(f"{b['ciclos']} cycles")
        lim = energia.limite()
        if lim and lim < 100:
            partes.append(f"stops at {lim}%")
        self.bat_estado.set_title("On the charger" if tomada else "On battery")
        self.bat_estado.set_subtitle("  ·  ".join(partes))
        self.carregando = True
        self.limitador.set_selected(indice(LIMITADOR, cfg["modo"]))
        self.limite_fixo.set_selected(indice(LIMITES, str(cfg["limite"]), 1))
        self.critico.set_selected(indice(CRITICO, str(cfg["critico"]), 2))
        self.perfil_auto.set_active(cfg["perfil_auto"])
        self.perfil_tomada.set_selected(indice(NA_TOMADA, cfg["perfil_tomada"]))
        self.carregando = False
        self.limite_fixo.set_visible(cfg["modo"] == "fixo")
        self.limitador.set_subtitle(EXPLICA_LIMITADOR.get(cfg["modo"], ""))
        self.cheia.set_sensitive(cfg["modo"] != "desligado")
        self.botao_cheia.set_label("Charging to full…" if cfg.get("cheia_uma_vez") else "Charge to full")
        self.perfil_tomada.set_sensitive(cfg["perfil_auto"])
        if self.modo:
            self.modo.set_sensitive(not cfg["perfil_auto"])
        hib = energia.pode_hibernar()
        self.critico.set_subtitle("When the battery is almost empty: " + (
            "hibernates, so the session survives even if the battery dies" if hib else
            "suspends and keeps your session in memory (hibernation isn't set up on this system)"))
        d = energia.driver()
        self.driver.set_visible(not (d and energia.pode_escrever()))
        if not d:
            self.driver.set_subtitle("Not installed: the limiter is set up but can't stop the charge yet. "
                                     "This laptop needs the third-party gigabyte-laptop-wmi driver")
        else:
            self.driver.set_subtitle("Installed, but the limiter can't write to it yet (permission rule missing)")

    def gravar_energia(self):
        if self.carregando:
            return
        cfg = estado.ler("energia", ENERGIA_PADRAO)
        novo = valor_de(self.limitador, LIMITADOR)
        if novo == "bypass" and cfg.get("modo") != "bypass":
            cfg["segurar"] = (energia.bateria() or {}).get("nivel")       # segura no nível de agora
        cfg |= {"modo": novo, "limite": int(valor_de(self.limite_fixo, LIMITES)),
                "critico": int(valor_de(self.critico, CRITICO)), "perfil_auto": self.perfil_auto.get_active(),
                "perfil_tomada": valor_de(self.perfil_tomada, NA_TOMADA)}
        estado.gravar("energia", cfg)
        rodar(["pkill", "-RTMIN+12", "-x", "waybar"])
        self.mostrar_bateria()

    def carregar_cheia(self):
        rodar([os.path.expanduser("~/.local/bin/aero-energia"), "--cheia"])
        self.mostrar_bateria()

    def explicar_driver(self):
        d = Adw.AlertDialog(heading="Battery driver", body=(
            "The charge limiter needs gigabyte-laptop-wmi, a community driver that talks to this laptop's "
            "controller. It isn't official and hasn't been tested on this exact model.\n\n"
            "Try it for this session only (gone after a reboot), in a terminal:\n"
            "sudo ~/.local/bin/aero-bateria-driver --test\n\n"
            "Undo: sudo ~/.local/bin/aero-bateria-driver --remove"))
        d.add_response("ok", "OK")
        d.present(self.janela)

    def ao_mostrar(self):
        self.mostrar_bateria()
        self.tampa_com.set_selected(indice(TAMPA_COM_MONITOR, ler_logind("HandleLidSwitchDocked")))
        self.tampa_sem.set_selected(indice(TAMPA_SEM_MONITOR, ler_logind("HandleLidSwitch")))
        self.tampa_tomada.set_selected(indice(TAMPA_TOMADA, ler_logind("HandleLidSwitchExternalPower")))
        self.toque.set_selected(indice(BOTAO_TOQUE, ler_logind("HandlePowerKey")))
        self.segurar.set_selected(indice(BOTAO_SEGURAR, ler_logind("HandlePowerKeyLongPress")))
        if self.modo:                       # muda na hora (sem Apply); ao mostrar, só lê o atual (com o
                                            # automático ligado, quem manda é o aero-energia)
            self.carregando_modo = True
            self.modo.set_selected(indice(MODOS, rodar(["powerprofilesctl", "get"]).stdout.strip(), 1))
            self.carregando_modo = False
        p = ler_painel()
        self.travar.set_selected(indice(TRAVAR, p.get("OCIO_TRAVAR", "0")))
        self.apagar.set_selected(indice(APAGAR, p.get("OCIO_APAGAR", "0")))
        self.dormir.set_selected(indice(DORMIR, p.get("OCIO_DORMIR", "0")))
        self.travar_ao_dormir.set_active(p.get("OCIO_TRAVAR_DORMIR", "0") == "1")

    def aplicar(self):
        # Inatividade: arquivo do usuário, não precisa de senha. Reinicia o hypridle pra ler de novo.
        chaves = dict(OCIO_TRAVAR=valor_de(self.travar, TRAVAR), OCIO_APAGAR=valor_de(self.apagar, APAGAR),
                      OCIO_DORMIR=valor_de(self.dormir, DORMIR),
                      OCIO_TRAVAR_DORMIR="1" if self.travar_ao_dormir.get_active() else "0")
        atualizar_painel(**chaves)
        gerar_hypridle(chaves["OCIO_TRAVAR"], chaves["OCIO_APAGAR"], chaves["OCIO_DORMIR"],
                       chaves["OCIO_TRAVAR_DORMIR"] == "1")
        if shutil.which("hypridle"):
            rodar(["pkill", "-x", "hypridle"])
            rodar(["hyprctl", "dispatch", "exec", "hypridle"])
        cmd = [AJUDANTE, valor_de(self.tampa_com, TAMPA_COM_MONITOR),
               valor_de(self.tampa_sem, TAMPA_SEM_MONITOR),
               valor_de(self.toque, BOTAO_TOQUE), valor_de(self.segurar, BOTAO_SEGURAR),
               valor_de(self.tampa_tomada, TAMPA_TOMADA)]
        em_segundo_plano(
            lambda: rodar_como_root(cmd),
            lambda r: self.janela.avisar("Power settings applied" if getattr(r, "returncode", 1) == 0
                                         else "Not applied (password cancelled or error)"))
