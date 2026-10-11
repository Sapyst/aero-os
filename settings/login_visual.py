"""
Settings → Wallpapers → botão "Lock screen & menus" (docs 34 e 37).

Cada lugar pode ter a SUA imagem ou usar a mesma do desktop ("Same as desktop"):
  - Desktop     → é a galeria da própria página Wallpapers.
  - Lock screen → a tela de login. Ela roda como outro usuário e não lê a sua pasta, então a imagem vai
                  pra /etc/greetd/aero-login/visual/ (pasta sua, criada pelo instalar.sh do doc 34).
  - Super Menu  → a faixa do topo do menu (por enquanto o único menu com imagem).
E um interruptor geral: "Use the desktop wallpaper everywhere".
Também: como a imagem ocupa a tela de login (Crop, Fit...) e a foto do seu perfil.

painel.conf: FUNDO_LOGIN_SINC=1|0   FUNDO_MENU_SINC=1|0
"""
import os
import re
import shutil
import subprocess

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from comum import atualizar_painel, ler_painel  # noqa: E402

VISUAL = "/etc/greetd/aero-login/visual"
FUNDO_LOGIN = os.path.join(VISUAL, "fundo")
CONF_LOGIN = os.path.join(VISUAL, "login.conf")
FUNDO_MENU = os.path.expanduser("~/.local/share/aero-shell/fundos/menu")
SINCRONIZAR = os.path.expanduser("~/.local/bin/sincronizar-fundos")
FACE = os.path.expanduser("~/.face")
ENCAIXES = [("crop", "Crop (fill the screen)"), ("fit", "Fit (whole image)"), ("stretch", "Stretch"),
            ("tile", "Tile (repeat)"), ("center", "Center (original size)")]


def papel_desktop():
    try:
        texto = open(os.path.expanduser("~/.config/hypr/hyprpaper.conf")).read()
        m = re.search(r"^\s*path\s*=\s*(.+)$", texto, re.M)
        return os.path.expanduser(m.group(1).strip()) if m else None
    except OSError:
        return None


def ler_encaixe():
    try:
        for linha in open(CONF_LOGIN):
            if linha.startswith("ENCAIXE="):
                return linha.split("=", 1)[1].strip()
    except OSError:
        pass
    return "crop"


def copiar(origem, destino):
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    shutil.copyfile(origem, destino + ".novo")
    os.replace(destino + ".novo", destino)         # troca de uma vez (ninguém lê pela metade)


def escolher_imagem(janela, titulo, ao_escolher):
    filtro = Gtk.FileFilter(name="Images")
    filtro.add_mime_type("image/*")
    filtros = Gio.ListStore.new(Gtk.FileFilter)
    filtros.append(filtro)
    d = Gtk.FileDialog(title=titulo, filters=filtros,
                       initial_folder=Gio.File.new_for_path(os.path.expanduser("~/Pictures")))

    def fim(dialogo, resultado):
        try:
            ao_escolher(dialogo.open_finish(resultado).get_path())
        except GLib.Error:
            pass                      # cancelou
    d.open(janela, None, fim)


def definir_foto(caminho):
    """Foto do perfil: ~/.face + avisa o AccountsService (ele guarda uma cópia que a tela de login lê)."""
    if caminho:
        shutil.copyfile(caminho, FACE)
    elif os.path.exists(FACE):
        os.remove(FACE)
    subprocess.run(["busctl", "call", "org.freedesktop.Accounts", f"/org/freedesktop/Accounts/User{os.getuid()}",
                    "org.freedesktop.Accounts.User", "SetIconFile", "s", FACE if caminho else ""],
                   capture_output=True)


class Previa(Gtk.DrawingArea):
    """Miniatura arredondada da imagem, cortada pra caber ("cover"), DESENHADA aqui (cairo).
    Antes era fundo CSS: cada troca de imagem mexia num estilo do app inteiro e o GTK reestilizava
    TUDO (inclusive as centenas de miniaturas da galeria) — o Settings travava (doc 45)."""

    def __init__(self, altura):
        super().__init__(height_request=altura, hexpand=True, margin_bottom=6)
        self.pixbuf = None
        self.pedido = 0
        self.set_draw_func(self.desenhar)

    def mostrar(self, caminho):
        """Carrega a imagem (reduzida) numa thread e redesenha quando chegar."""
        import threading
        from gi.repository import GdkPixbuf
        self.pedido += 1
        meu = self.pedido
        if not caminho or not os.path.exists(caminho):
            self.pixbuf = None
            self.queue_draw()
            return

        def carregar():
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(caminho, 900, -1, True)
            except GLib.Error:
                pb = None

            def pronto():
                if meu == self.pedido:                 # só a imagem mais recente vale
                    self.pixbuf = pb
                    self.queue_draw()
                return False
            GLib.idle_add(pronto)
        threading.Thread(target=carregar, daemon=True).start()

    def desenhar(self, _area, cr, w, h):
        import math
        import cairo
        r = 14
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        pb = self.pixbuf
        if pb is None:
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.12)
            cr.paint()
            return
        pw, ph = pb.get_width(), pb.get_height()
        esc = max(w / pw, h / ph)
        cr.translate((w - pw * esc) / 2, (h - ph * esc) / 2)
        cr.scale(esc, esc)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_GOOD)
        cr.paint()


def abrir(janela):
    import pwd
    eu = pwd.getpwuid(os.getuid())
    nome = eu.pw_gecos.split(",")[0] or eu.pw_name
    p = ler_painel()
    estado = {"login": p.get("FUNDO_LOGIN_SINC", "1") == "1", "menu": p.get("FUNDO_MENU_SINC", "1") == "1"}
    mexendo = [False]                     # True enquanto o próprio código mexe nos interruptores
    login_ok = os.access(VISUAL, os.W_OK)

    dialogo = Adw.PreferencesDialog(title="Lock screen & menus", content_width=560, content_height=720)
    pag = Adw.PreferencesPage()
    dialogo.add(pag)
    raiz = janela

    def avisar(texto):
        dialogo.add_toast(Adw.Toast(title=texto, use_markup=False))

    # ---- tudo junto ----
    g_tudo = Adw.PreferencesGroup(description="Each place can have its own picture, or use the same one as the desktop.")
    pag.add(g_tudo)
    tudo = Adw.SwitchRow(title="Use the desktop wallpaper everywhere",
                         subtitle="Lock screen and Super Menu follow the desktop")
    g_tudo.add(tudo)

    # ---- desktop ----
    g_desk = Adw.PreferencesGroup(title="Desktop", description="Pick it in the gallery on the Wallpapers page")
    pag.add(g_desk)
    previa_desk = Previa(120)
    g_desk.add(previa_desk)

    # ---- tela de login ----
    # (a prévia num grupo e as opções em outro: dentro do mesmo grupo o GTK põe a imagem DEPOIS das linhas)
    g_login_previa = Adw.PreferencesGroup(title="Lock screen")
    if not login_ok:
        g_login_previa.set_description("Run once in a terminal to enable this:\n"
                                       "sudo bash ~/.local/share/aero-shell/login/instalar.sh")
    pag.add(g_login_previa)
    previa_login = Previa(120)
    g_login_previa.add(previa_login)
    g_login = Adw.PreferencesGroup()
    pag.add(g_login)
    login_sinc = Adw.SwitchRow(title="Same as desktop", sensitive=login_ok)
    g_login.add(login_sinc)
    login_img = Adw.ActionRow(title="Picture")
    b_login = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
    login_img.add_suffix(b_login)
    g_login.add(login_img)
    modos = Adw.ComboRow(title="Picture fit", sensitive=login_ok, model=Gtk.StringList.new([t for _, t in ENCAIXES]))
    chaves = [c for c, _ in ENCAIXES]
    modos.set_selected(chaves.index(ler_encaixe()) if ler_encaixe() in chaves else 0)
    g_login.add(modos)
    teste = Adw.ActionRow(title="Preview the login screen", subtitle="Opens it in a window (it won't log you in)",
                          activatable=True)
    teste.add_suffix(Gtk.Image(icon_name="view-reveal-symbolic"))
    g_login.add(teste)

    # ---- Super Menu ----
    g_menu_previa = Adw.PreferencesGroup(title="Super Menu", description="The picture at the top of the menu")
    pag.add(g_menu_previa)
    previa_menu = Previa(90)
    g_menu_previa.add(previa_menu)
    g_menu = Adw.PreferencesGroup()
    pag.add(g_menu)
    menu_sinc = Adw.SwitchRow(title="Same as desktop")
    g_menu.add(menu_sinc)
    menu_img = Adw.ActionRow(title="Picture")
    b_menu = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
    menu_img.add_suffix(b_menu)
    g_menu.add(menu_img)

    def mostrar():
        mexendo[0] = True
        desk = papel_desktop()
        login_sinc.set_active(estado["login"])
        menu_sinc.set_active(estado["menu"])
        tudo.set_active(estado["login"] and estado["menu"])
        login_img.set_sensitive(login_ok and not estado["login"])
        menu_img.set_sensitive(not estado["menu"])
        previa_desk.mostrar(desk)
        previa_login.mostrar(FUNDO_LOGIN if os.path.exists(FUNDO_LOGIN) else "/etc/greetd/aero-login/fundo.jpg")
        previa_menu.mostrar(desk if estado["menu"] or not os.path.exists(FUNDO_MENU) else FUNDO_MENU)
        mexendo[0] = False

    def gravar():
        atualizar_painel(FUNDO_LOGIN_SINC="1" if estado["login"] else "0",
                         FUNDO_MENU_SINC="1" if estado["menu"] else "0")
        if estado["login"]:
            subprocess.run([SINCRONIZAR])          # a tela de login recebe o wallpaper do desktop agora
        desk = papel_desktop()
        if not estado["menu"] and not os.path.exists(FUNDO_MENU) and desk:
            copiar(desk, FUNDO_MENU)               # desligou: começa com a imagem atual, até você escolher outra
        mostrar()

    def ao_tudo(*_):
        if mexendo[0]:
            return
        estado["login"] = estado["menu"] = tudo.get_active()
        if not login_ok:
            estado["login"] = False
        gravar()

    def ao_login(*_):
        if not mexendo[0]:
            estado["login"] = login_sinc.get_active()
            gravar()

    def ao_menu(*_):
        if not mexendo[0]:
            estado["menu"] = menu_sinc.get_active()
            gravar()

    tudo.connect("notify::active", ao_tudo)
    login_sinc.connect("notify::active", ao_login)
    menu_sinc.connect("notify::active", ao_menu)

    def usar_login(caminho):
        copiar(caminho, FUNDO_LOGIN)
        mostrar()
        avisar("Lock screen picture updated")

    def usar_menu(caminho):
        copiar(caminho, FUNDO_MENU)
        mostrar()
        avisar("Super Menu picture updated")

    b_login.connect("clicked", lambda *_: escolher_imagem(raiz, "Lock screen picture", usar_login))
    b_menu.connect("clicked", lambda *_: escolher_imagem(raiz, "Super Menu picture", usar_menu))

    def ao_encaixe(*_):
        with open(CONF_LOGIN, "w") as f:
            f.write(f"ENCAIXE={chaves[modos.get_selected()]}\n")
    modos.connect("notify::selected", ao_encaixe)
    teste.connect("activated", lambda *_: subprocess.Popen(
        ["python3", "/etc/greetd/aero-login/aero-login.py", "--teste"]))

    # ---- perfil ----
    g_perfil = Adw.PreferencesGroup(title="Profile picture")
    pag.add(g_perfil)
    avatar = Adw.Avatar(size=64, text=nome, show_initials=True)

    def mostrar_foto():
        try:
            avatar.set_custom_image(Gdk.Texture.new_from_filename(FACE))
        except GLib.Error:
            avatar.set_custom_image(None)
    mostrar_foto()
    perfil = Adw.ActionRow(title=nome, subtitle="Shown on the login screen, the Super Menu and the quick settings")
    perfil.add_prefix(avatar)

    def trocar(caminho):
        definir_foto(caminho)
        mostrar_foto()
        avisar("Profile picture updated" if caminho else "Profile picture removed")
    escolher = Gtk.Button(label="Choose…", valign=Gtk.Align.CENTER)
    escolher.connect("clicked", lambda *_: escolher_imagem(raiz, "Profile picture", trocar))
    tirar = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove picture", valign=Gtk.Align.CENTER)
    tirar.add_css_class("flat")
    tirar.connect("clicked", lambda *_: trocar(None))
    perfil.add_suffix(escolher)
    perfil.add_suffix(tirar)
    g_perfil.add(perfil)

    mostrar()
    dialogo.present(janela)
