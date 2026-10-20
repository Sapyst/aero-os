#!/usr/bin/env python3
"""
aero-login — a tela de login do sistema (doc 34).

Quem cuida do login "de verdade" é o greetd (um serviço bem pequeno). Ele abre um Hyprland só pra
tela de login e, dentro dele, este programa. A conversa com o greetd é por um socket ($GREETD_SOCK):
cada mensagem é um JSON precedido do tamanho dele (4 bytes).
  1) create_session {usuário}             → o greetd pede a senha
  2) post_auth_message_response {senha}   → "success" (senha certa) ou "error"
  3) start_session {comando}              → o greetd abre o Hyprland do usuário quando sairmos daqui

Teste sem greetd (só pra ver a cara):  aero-login.py --teste
"""
import json
import os
import pwd
import socket
import struct
import subprocess
import sys

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402

TESTE = "--teste" in sys.argv
PASTA = os.path.dirname(os.path.abspath(__file__))
# A pasta "visual" é SUA (o instalar.sh dá ela pro seu usuário): o Settings troca o fundo e o encaixe
# sem precisar de senha. A tela de login só lê.
VISUAL = os.path.join(PASTA, "visual")
FUNDO = next((f for f in (os.path.join(VISUAL, "fundo"), os.path.join(PASTA, "fundo.jpg")) if os.path.exists(f)),
             os.path.join(PASTA, "fundo.jpg"))
# Como a imagem ocupa a tela (o mesmo jeito de falar de papel de parede)
ENCAIXES = {
    "crop":    "background-size: cover; background-repeat: no-repeat; background-position: center;",
    "fit":     "background-size: contain; background-repeat: no-repeat; background-position: center;",
    "stretch": "background-size: 100% 100%; background-repeat: no-repeat;",
    "tile":    "background-size: auto; background-repeat: repeat; background-position: top left;",
    "center":  "background-size: auto; background-repeat: no-repeat; background-position: center;",
}


def encaixe():
    try:
        for linha in open(os.path.join(VISUAL, "login.conf")):
            if linha.startswith("ENCAIXE="):
                return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    return "crop"
# O mesmo que digitar "Hyprland" no terminal (com o vigia dele), mas sem mostrar NADA no terminal
# (ver aero-session). Tem de ser UM programa: o greetd não repassa "sh -c ..." do jeito esperado,
# e a sessão abria e fechava na hora (o login voltava sozinho).
SESSAO = ["/etc/greetd/aero-login/aero-session"]

# A animação de início do Aero OS (a mesma do boot e do desktop), se estiver instalada.
sys.path.insert(0, "/usr/share/aero/venus")
try:
    from venus import Venus
except (ImportError, OSError):
    Venus = None

CSS = """
window { background: black; }
.escurecer { background: alpha(black, 0.35); }
.relogio { font-size: 84px; font-weight: 300; color: white; }
.data { font-size: 18px; color: alpha(white, 0.85); }
.cartao { background: alpha(@window_bg_color, 0.80); border-radius: 22px; padding: 28px 32px;
          box-shadow: inset 0 0 0 1px alpha(white, 0.10); }
.nome { font-size: 22px; font-weight: 700; }
.senha { border-radius: 22px; min-height: 44px; padding: 0 14px; font-size: 15px; }
.erro { color: #ff7b72; font-weight: 600; }
button.entrar { border-radius: 99px; min-width: 44px; min-height: 44px; padding: 0; }
button.energia { border-radius: 99px; min-width: 48px; min-height: 48px; padding: 0;
                 background: alpha(black, 0.35); color: white; }
button.energia:hover { background: alpha(black, 0.55); }
.tremer { animation: tremer 0.35s; }
@keyframes tremer { 0%, 100% { margin-left: 0; } 20%, 60% { margin-left: -16px; } 40%, 80% { margin-left: 16px; } }
"""


# ---------- conversa com o greetd ----------
class Greetd:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.connect(os.environ["GREETD_SOCK"])

    def pedir(self, msg):
        dados = json.dumps(msg).encode()
        self.s.sendall(struct.pack("=I", len(dados)) + dados)
        tamanho = struct.unpack("=I", self.s.recv(4))[0]
        resposta = b""
        while len(resposta) < tamanho:
            resposta += self.s.recv(tamanho - len(resposta))
        return json.loads(resposta)


def usuario_padrao():
    """A primeira pessoa "de verdade" (UID 1000+, com shell de login) — no seu PC, você."""
    for u in sorted(pwd.getpwall(), key=lambda u: u.pw_uid):
        if 1000 <= u.pw_uid < 60000 and not u.pw_shell.endswith(("nologin", "false")):
            return u
    return pwd.getpwuid(os.getuid())


def foto_de(nome):
    """A foto do perfil: o AccountsService guarda uma cópia que qualquer um pode ler."""
    for caminho in (f"/var/lib/AccountsService/icons/{nome}", os.path.expanduser(f"~{nome}/.face")):
        try:
            return Gdk.Texture.new_from_filename(caminho)
        except GLib.Error:
            continue
    return None


class Login(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.aero.Login")

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        css = Gtk.CssProvider()
        fundo = ""
        if os.path.exists(FUNDO):
            arquivo = GLib.filename_to_uri(FUNDO, None)
            fundo = f".fundo {{ background-image: url('{arquivo}'); {ENCAIXES.get(encaixe(), ENCAIXES['crop'])} }}"
        css.load_from_string(CSS + fundo)
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css, 800)

        self.usuario = usuario_padrao()
        self.janela = Gtk.ApplicationWindow(application=self, title="Login")
        camadas = Gtk.Overlay()
        fundo = Gtk.Box()                  # o papel de parede é desenhado pelo CSS (.fundo), com o encaixe escolhido
        fundo.add_css_class("fundo")
        camadas.set_child(fundo)
        escuro = Gtk.Box()
        escuro.add_css_class("escurecer")
        camadas.add_overlay(escuro)

        # relógio grande em cima
        topo = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, halign=Gtk.Align.CENTER, valign=Gtk.Align.START,
                       margin_top=90)
        self.relogio = Gtk.Label()
        self.relogio.add_css_class("relogio")
        self.data = Gtk.Label()
        self.data.add_css_class("data")
        topo.append(self.relogio)
        topo.append(self.data)
        camadas.add_overlay(topo)
        self.tique()
        GLib.timeout_add_seconds(1, self.tique)

        # o cartão: foto, nome, senha
        cartao = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, halign=Gtk.Align.CENTER,
                         valign=Gtk.Align.CENTER, width_request=360)
        cartao.add_css_class("cartao")
        nome_visivel = (self.usuario.pw_gecos.split(",")[0] or self.usuario.pw_name)
        avatar = Adw.Avatar(size=112, text=nome_visivel, show_initials=True, halign=Gtk.Align.CENTER)
        foto = foto_de(self.usuario.pw_name)
        if foto:
            avatar.set_custom_image(foto)
        cartao.append(avatar)
        nome = Gtk.Label(label=nome_visivel)
        nome.add_css_class("nome")
        cartao.append(nome)
        linha = Gtk.Box(spacing=8)
        self.senha = Gtk.PasswordEntry(show_peek_icon=True, hexpand=True, placeholder_text="Password")
        self.senha.add_css_class("senha")
        self.senha.connect("activate", lambda *_: self.entrar())
        linha.append(self.senha)
        entrar = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Log in")
        entrar.add_css_class("entrar")
        entrar.add_css_class("suggested-action")
        entrar.connect("clicked", lambda *_: self.entrar())
        linha.append(entrar)
        cartao.append(linha)
        self.erro = Gtk.Label(label="", visible=False)
        self.erro.add_css_class("erro")
        cartao.append(self.erro)
        self.cartao = cartao
        camadas.add_overlay(cartao)

        # desligar / reiniciar, no canto
        energia = Gtk.Box(spacing=12, halign=Gtk.Align.END, valign=Gtk.Align.END, margin_end=28, margin_bottom=28)
        for icone, dica, cmd in (("system-reboot-symbolic", "Restart", "reboot"),
                                 ("system-shutdown-symbolic", "Shut down", "poweroff")):
            b = Gtk.Button(icon_name=icone, tooltip_text=dica)
            b.add_css_class("energia")
            b.connect("clicked", lambda *_, c=cmd: None if TESTE else subprocess.Popen(["systemctl", c]))
            energia.append(b)
        camadas.add_overlay(energia)

        # A Vênus do boot continua aqui por um instante e se desfaz revelando a tela de login.
        self.camadas = camadas
        self.venus = None
        if Venus and not TESTE:
            try:
                self.venus = Venus()
                self.venus.loop()
                camadas.add_overlay(self.venus)
                self.venus.set_can_target(False)          # nunca pega clique nem foco
                GLib.timeout_add(900, lambda: self.venus.leave(0.8, self.sem_venus) and False)
            except Exception:  # noqa: BLE001 — sem animação, o login funciona igual
                self.venus = None

        self.janela.set_child(camadas)
        if not TESTE:
            self.janela.fullscreen()
        else:
            self.janela.set_default_size(1280, 720)
        self.janela.present()
        self.senha.grab_focus()            # o perfil já vem escolhido: é só digitar a senha

    def sem_venus(self):
        if self.venus:
            self.camadas.remove_overlay(self.venus)
            self.venus = None

    def tique(self):
        agora = GLib.DateTime.new_now_local()
        self.relogio.set_label(agora.format("%H:%M"))
        self.data.set_label(agora.format("%A, %d %B"))
        return GLib.SOURCE_CONTINUE

    def falhou(self, texto):
        self.erro.set_label(texto)
        self.erro.set_visible(True)
        self.senha.set_text("")
        self.senha.set_sensitive(True)
        self.senha.grab_focus()
        self.cartao.remove_css_class("tremer")
        GLib.idle_add(lambda: self.cartao.add_css_class("tremer"))

    def entrar(self):
        senha = self.senha.get_text()
        if not senha:
            return
        self.senha.set_sensitive(False)
        self.erro.set_visible(False)
        if TESTE:
            GLib.timeout_add(400, lambda: self.falhou("Test mode: no login"))
            return
        try:
            g = Greetd()
            r = g.pedir({"type": "create_session", "username": self.usuario.pw_name})
            enviada = False
            while r.get("type") == "auth_message":
                tipo = r.get("auth_message_type")
                if tipo == "secret" and not enviada:
                    r = g.pedir({"type": "post_auth_message_response", "response": senha})
                    enviada = True          # a senha vai UMA vez, só para o pedido secreto
                elif tipo in ("secret", "visible"):
                    # outra pergunta (código, nova senha, nome...): esta tela não sabe responder,
                    # e responder com a senha a vazaria. Cancela com uma mensagem clara.
                    g.pedir({"type": "cancel_session"})
                    self.falhou("This login needs an extra step (use a text console: Ctrl+Alt+F2)")
                    return
                else:                       # mensagens informativas do PAM: só confirma
                    r = g.pedir({"type": "post_auth_message_response", "response": None})
            if r.get("type") == "success":
                r = g.pedir({"type": "start_session", "cmd": SESSAO, "env": []})
                if r.get("type") == "success":
                    # A Vênus volta com glitch e para; o desktop começa com essa mesma imagem.
                    if Venus and not TESTE:
                        try:
                            v = Venus()
                            v.set_can_target(False)
                            self.camadas.add_overlay(v)
                            v.enter(0.7, self.quit, fade_backdrop=True)
                            return
                        except Exception:  # noqa: BLE001
                            pass
                    self.quit()             # o greetd abre o Hyprland do usuário
                    return
            g.pedir({"type": "cancel_session"})
            self.falhou("Wrong password" if r.get("error_type") == "auth_error" else r.get("description", "Login failed"))
        except (OSError, KeyError, ValueError, struct.error) as erro:
            self.falhou(f"Login failed: {erro}")


if __name__ == "__main__":
    sys.exit(Login().run([sys.argv[0]]))
