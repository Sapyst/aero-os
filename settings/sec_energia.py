"""
sec_energia.py — seção Power (tampa, botão de energia, inatividade).

Inatividade (doc 25): gera ~/.config/hypr/hypridle.conf (travar, apagar a tela, dormir). Padrão: nunca.

Lê a config atual do systemd-logind pelo D-Bus (busctl) e grava pelo ajudante de root
/usr/local/bin/painel-energia, que só aceita valores de uma lista fechada (doc 10).
"""
import os
import shutil

from gi.repository import Adw

from comum import (Secao, atualizar_painel, combo, em_segundo_plano, grupo_botao_aplicar, indice, ler_painel,
                   pagina, rodar, rodar_como_root, valor_de)

AJUDANTE = "/usr/local/bin/painel-energia"
TAMPA_COM_MONITOR = [("Just turn off the screen", "ignore"), ("Sleep", "suspend")]
TAMPA_SEM_MONITOR = [("Sleep", "suspend"), ("Do nothing", "ignore")]
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
        self.tampa_sem = combo("Close without a monitor", TAMPA_SEM_MONITOR, 0,
                               "Sleep is safest: keeps the laptop from overheating in a bag")
        tampa.add(self.tampa_com)
        tampa.add(self.tampa_sem)

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

        modo = Adw.PreferencesGroup(title="Power mode")
        if shutil.which("powerprofilesctl"):
            self.modo = combo("Mode", MODOS, 1, "Power saver: quieter and longer battery. "
                                                "Performance: games and heavy apps")
            self.modo.connect("notify::selected", lambda *_: None if self.carregando_modo else
                              rodar(["powerprofilesctl", "set", valor_de(self.modo, MODOS)]))
            modo.add(self.modo)
        else:
            self.modo = None
            modo.add(Adw.ActionRow(use_markup=False, title="Mode",
                                   subtitle="Needs power-profiles-daemon. Install it in a terminal: "
                                            "sudo pacman -S power-profiles-daemon"))
        self.carregando_modo = False

        for g in (tampa, botao, modo, sono, grupo_botao_aplicar(self.aplicar)):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def ao_mostrar(self):
        self.tampa_com.set_selected(indice(TAMPA_COM_MONITOR, ler_logind("HandleLidSwitchDocked")))
        self.tampa_sem.set_selected(indice(TAMPA_SEM_MONITOR, ler_logind("HandleLidSwitch")))
        self.toque.set_selected(indice(BOTAO_TOQUE, ler_logind("HandlePowerKey")))
        self.segurar.set_selected(indice(BOTAO_SEGURAR, ler_logind("HandlePowerKeyLongPress")))
        if self.modo:                       # muda na hora (sem Apply); ao mostrar, só lê o atual
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
               valor_de(self.toque, BOTAO_TOQUE), valor_de(self.segurar, BOTAO_SEGURAR)]
        em_segundo_plano(
            lambda: rodar_como_root(cmd),
            lambda r: self.janela.avisar("Power settings applied" if getattr(r, "returncode", 1) == 0
                                         else "Not applied (password cancelled or error)"))
