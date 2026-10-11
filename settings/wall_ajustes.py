"""
Settings → Wallpapers → botão "Adjust" (doc 45): brilho, contraste e saturação do papel de parede.

O original nunca é mexido: o ~/.local/bin/ajustar-wallpaper gera uma cópia ajustada (ffmpeg) e põe na tela.
Os ajustes ficam no painel.conf e valem pra todo wallpaper novo.
A prévia aqui é ao vivo (uma versão pequena, gerada a cada mudança, ~0,3 s).
"""
import os
import subprocess

from gi.repository import Adw, GLib, Gtk

from comum import atualizar_painel, em_segundo_plano, ler_painel
from login_visual import Previa

AJUSTAR = os.path.expanduser("~/.local/bin/ajustar-wallpaper")
PREVIA = os.path.join(GLib.get_user_cache_dir(), "aero-settings", "previa-ajuste.jpg")


def valor(p, chave, padrao):
    try:
        return float(str(p.get(chave, padrao)).strip('"'))
    except ValueError:
        return padrao


def abrir(janela):
    p = ler_painel()
    dialogo = Adw.PreferencesDialog(title="Adjust wallpaper", content_width=560, content_height=640)
    pag = Adw.PreferencesPage()
    dialogo.add(pag)

    g_previa = Adw.PreferencesGroup(description="The original file is never changed. "
                                                "These settings also apply to wallpapers you pick later.")
    pag.add(g_previa)
    previa = Previa(200)
    g_previa.add(previa)

    g = Adw.PreferencesGroup()
    pag.add(g)
    pb = Adw.SwitchRow(title="Black & white", use_markup=False)   # "&" puro (sem markup)
    g.add(pb)

    def deslizante(titulo, minimo, maximo, inicial):
        linha = Adw.ActionRow(title=titulo)
        esc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimo, maximo, 0.01)
        esc.set_value(inicial)
        esc.set_hexpand(True)
        esc.set_size_request(260, -1)
        esc.set_draw_value(False)
        meio = (minimo + maximo) / 2
        esc.add_mark(meio, Gtk.PositionType.BOTTOM, None)    # marquinha no "normal"
        linha.add_suffix(esc)
        g.add(linha)
        return esc

    brilho = deslizante("Brightness", -0.5, 0.5, valor(p, "WALL_BRILHO", 0))
    contraste = deslizante("Contrast", 0.5, 1.5, valor(p, "WALL_CONTRASTE", 1))
    saturacao = deslizante("Saturation", 0.0, 2.0, valor(p, "WALL_SATURACAO", 1))
    mexendo = [False]
    pb.set_active(saturacao.get_value() == 0)

    pendente = [0]

    def atualizar_previa(*_):
        """Espera o controle parar um instante (250 ms) e gera a prévia em segundo plano."""
        if pendente[0]:
            GLib.source_remove(pendente[0])

        def gerar():
            pendente[0] = 0
            b, c, s = brilho.get_value(), contraste.get_value(), saturacao.get_value()
            os.makedirs(os.path.dirname(PREVIA), exist_ok=True)
            em_segundo_plano(
                lambda: subprocess.run([AJUSTAR, "--previa", PREVIA, f"{b:.2f}", f"{c:.2f}", f"{s:.2f}"],
                                       capture_output=True).returncode,
                lambda r: previa.mostrar(PREVIA) if r == 0 else None)
            return False
        pendente[0] = GLib.timeout_add(250, gerar)

    def ao_pb(*_):
        if mexendo[0]:
            return
        mexendo[0] = True
        saturacao.set_value(0 if pb.get_active() else 1)
        mexendo[0] = False
        atualizar_previa()

    def ao_saturacao(*_):
        if not mexendo[0]:
            mexendo[0] = True
            pb.set_active(saturacao.get_value() == 0)
            mexendo[0] = False
        atualizar_previa()

    pb.connect("notify::active", ao_pb)
    brilho.connect("value-changed", atualizar_previa)
    contraste.connect("value-changed", atualizar_previa)
    saturacao.connect("value-changed", ao_saturacao)

    # botões: voltar ao normal / aplicar
    g_botoes = Adw.PreferencesGroup()
    pag.add(g_botoes)
    caixa = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=6)
    normal = Gtk.Button(label="Reset")
    normal.add_css_class("pill")
    aplicar = Gtk.Button(label="Apply")
    aplicar.add_css_class("pill")
    aplicar.add_css_class("suggested-action")
    caixa.append(normal)
    caixa.append(aplicar)
    g_botoes.add(caixa)

    def ao_normal(*_):
        mexendo[0] = True
        brilho.set_value(0)
        contraste.set_value(1)
        saturacao.set_value(1)
        pb.set_active(False)
        mexendo[0] = False
        atualizar_previa()

    def ao_aplicar(*_):
        b, c, s = (round(x.get_value(), 2) for x in (brilho, contraste, saturacao))
        atualizar_painel(WALL_BRILHO=f"{b:g}", WALL_CONTRASTE=f"{c:g}", WALL_SATURACAO=f"{s:g}")
        aplicar.set_sensitive(False)

        def fim(r):
            aplicar.set_sensitive(True)
            dialogo.add_toast(Adw.Toast(title="Wallpaper adjusted" if r == 0 else "Couldn't adjust the wallpaper"))
        em_segundo_plano(lambda: subprocess.run([AJUSTAR], capture_output=True).returncode, fim)

    normal.connect("clicked", ao_normal)
    aplicar.connect("clicked", ao_aplicar)

    atualizar_previa()
    dialogo.present(janela)
