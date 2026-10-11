"""
sec_atualizacoes.py — seção Updates.

Ao abrir, checa tudo sozinho (repos, AUR, Flatpak, npm) e lista programa por programa,
separado em System / Desktop / Apps. Botões:
  Check for updates  = "update": só busca a lista nova (seguro, não instala nada)
  Upgrade system     = "upgrade": pacman -Syu (todos os pacotes oficiais juntos)
  Update everything  = upgrade + AUR + Flatpak + npm
  Update (por linha) = só aquele programa (para AUR, Flatpak e npm; ver atualizacoes.py)
Enquanto atualiza, uma sub-página mostra o log (a saída dos comandos) ao vivo.
"""
import subprocess
import threading
import time

from gi.repository import Adw, GLib, Gtk

import atualizacoes as at
from comum import Secao, ambiente_root, botao, em_segundo_plano, pagina

PEDEM_REINICIO = ("linux", "nvidia", "amd-ucode", "systemd", "mesa", "hyprland", "aquamarine")


class SecaoAtualizacoes(Secao):
    id = "atualizacoes"
    titulo = "Updates"
    icone = "software-update-available-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.programas, self.grupos = [], []
        self.checando = self.rodando = False
        self.conteudo = Adw.PreferencesPage()

        resumo = Adw.PreferencesGroup()
        self.status = Adw.ActionRow(use_markup=False, title="Checking for updates…")
        self.status_icone = Gtk.Image(pixel_size=32)
        self.status.add_prefix(self.status_icone)
        self.girando = Adw.Spinner(visible=False)
        self.status.add_suffix(self.girando)
        resumo.add(self.status)

        caixa = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, margin_top=12)
        self.b_checar = botao("Check for updates", self.checar, "pill", "view-refresh-symbolic")
        self.b_sistema = botao("Upgrade system", lambda: self.atualizar(so_sistema=True), "pill",
                               "system-software-install-symbolic")
        self.b_tudo = botao("Update everything", self.atualizar, "pill suggested-action",
                            "software-update-available-symbolic")
        self.b_checar.set_tooltip_text("Download the latest list of versions. Installs nothing.")
        self.b_sistema.set_tooltip_text("pacman -Syu: upgrade all official packages together")
        self.b_tudo.set_tooltip_text("System upgrade + AUR + Flatpak + npm")
        for b in (self.b_checar, self.b_sistema, self.b_tudo):
            caixa.append(b)
        resumo.add(caixa)
        self.conteudo.add(resumo)
        self.pagina = pagina(self.titulo, self.conteudo)

    def ao_mostrar(self):
        if not self.checando and not self.rodando:
            self.checar()   # checa sozinho toda vez que a página é aberta

    def botoes(self, ativos):
        for b in (self.b_checar, self.b_sistema, self.b_tudo):
            b.set_sensitive(ativos)

    # ----- checar -----

    def checar(self):
        self.checando = True
        self.botoes(False)
        self.girando.set_visible(True)
        self.status.set_title("Checking for updates…")
        self.status.set_subtitle("Official repos, AUR, Flatpak and npm")
        self.status_icone.set_from_icon_name("view-refresh-symbolic")
        em_segundo_plano(at.verificar, self.mostrar)

    def mostrar(self, resultado):
        self.checando = False
        self.girando.set_visible(False)
        self.botoes(True)
        for css in ("success", "accent", "warning"):
            self.status_icone.remove_css_class(css)
        if isinstance(resultado, Exception):
            self.status.set_title("Could not check for updates")
            self.status.set_subtitle(str(resultado))
            self.status_icone.set_from_icon_name("dialog-warning-symbolic")
            self.status_icone.add_css_class("warning")
            return

        self.programas, erros = resultado
        n = sum(1 for p in self.programas if p.nova)
        if n:
            self.status.set_title(f"{n} update{'s' if n > 1 else ''} available")
            self.status_icone.set_from_icon_name("software-update-available-symbolic")
            self.status_icone.add_css_class("accent")
        else:
            self.status.set_title("Everything is up to date")
            self.status_icone.set_from_icon_name("object-select-symbolic")
            self.status_icone.add_css_class("success")
        detalhe = f"Last checked at {time.strftime('%H:%M')}"
        if erros:
            detalhe += " · Problem: " + "; ".join(erros)
            self.status_icone.add_css_class("warning")
        self.status.set_subtitle(detalhe)
        self.b_tudo.set_sensitive(n > 0)
        self.montar_categorias()

    def montar_categorias(self):
        for g in self.grupos:
            self.conteudo.remove(g)
        self.grupos = []
        for cat, titulo, descricao in at.CATEGORIAS:
            todos = [p for p in self.programas if p.categoria == cat]
            if not todos:
                continue
            pendentes = [p for p in todos if p.nova and p.explicito]
            bibliotecas = [p for p in todos if p.nova and not p.explicito]
            em_dia = sorted((p for p in todos if not p.nova and p.explicito), key=lambda p: p.titulo.lower())

            grupo = Adw.PreferencesGroup(title=titulo, description=descricao)
            total = len(pendentes) + len(bibliotecas)
            contador = Gtk.Label(label=f"{total} update{'s' if total != 1 else ''}" if total else "Up to date")
            contador.add_css_class("accent" if total else "dim-label")
            grupo.set_header_suffix(contador)

            for p in pendentes:
                grupo.add(self.linha_pendente(p))
            if bibliotecas:
                exp = Adw.ExpanderRow(use_markup=False, title="Libraries and dependencies",
                                      subtitle=f"{len(bibliotecas)} updates · installed with the system upgrade")
                for p in bibliotecas:
                    exp.add_row(self.linha_pendente(p))
                grupo.add(exp)
            if em_dia:
                # Apps aparece aberto (são poucos); System e Desktop ficam recolhidos
                exp = Adw.ExpanderRow(use_markup=False, title="Up to date", subtitle=f"{len(em_dia)} programs",
                                      expanded=(cat == "extras"))
                for p in em_dia:
                    exp.add_row(self.linha_em_dia(p))
                grupo.add(exp)
            self.conteudo.add(grupo)
            self.grupos.append(grupo)

    def linha_pendente(self, p):
        linha = Adw.ActionRow(use_markup=False, title=p.titulo, subtitle=f"{p.atual} → {p.nova} · {at.FONTES[p.fonte]}")
        icone = Gtk.Image.new_from_icon_name("software-update-available-symbolic")
        icone.add_css_class("accent")
        linha.add_prefix(icone)
        if p.individual:
            linha.add_suffix(botao("Update", lambda: self.atualizar(unico=p), "pill"))
        else:
            rotulo = Gtk.Label(label="With system")
            rotulo.add_css_class("dim-label")
            rotulo.set_tooltip_text("Official packages are always upgraded together, so they never get "
                                    "out of sync with each other (Arch does not support partial upgrades).")
            linha.add_suffix(rotulo)
        return linha

    def linha_em_dia(self, p):
        linha = Adw.ActionRow(use_markup=False, title=p.titulo, subtitle=p.descricao, subtitle_lines=1)
        versao = Gtk.Label(label=p.atual, max_width_chars=18, ellipsize=3)  # 3 = cortar no fim com "…"
        versao.add_css_class("dim-label")
        versao.set_tooltip_text(f"{p.atual} · {at.FONTES[p.fonte]}")
        linha.add_suffix(versao)
        ok = Gtk.Image.new_from_icon_name("object-select-symbolic")
        ok.add_css_class("success")
        linha.add_suffix(ok)
        return linha

    # ----- atualizar -----

    def atualizar(self, so_sistema=False, unico=None):
        passos = at.passos_para(self.programas, so_sistema, unico)
        if not passos:
            self.janela.avisar("Nothing to update")
            return
        alvo = [unico] if unico else [p for p in self.programas if p.nova and (not so_sistema or p.fonte == "repo")]
        reiniciar = any(p.nome.startswith(PEDEM_REINICIO) for p in alvo)
        titulo = f"Updating {unico.titulo}" if unico else ("Upgrading system" if so_sistema else "Updating everything")
        self.rodando = True
        self.botoes(False)
        TelaExecucao(self.janela, titulo, passos, reiniciar, self.terminou)

    def terminou(self):
        self.rodando = False
        self.checar()   # confere de novo o que sobrou


class TelaExecucao:
    """Sub-página que roda os passos em ordem e mostra a saída ao vivo."""

    def __init__(self, janela, titulo, passos, reiniciar, ao_terminar):
        self.janela, self.passos, self.reiniciar, self.ao_terminar = janela, passos, reiniciar, ao_terminar

        caixa = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                        margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        lista = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        lista.add_css_class("boxed-list")
        self.status = Adw.ActionRow(use_markup=False, title="Starting…", subtitle=f"{len(passos)} step(s)")
        self.girando = Adw.Spinner()
        self.status.add_prefix(self.girando)
        lista.append(self.status)
        caixa.append(lista)

        self.texto = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                  wrap_mode=Gtk.WrapMode.WORD_CHAR,
                                  top_margin=8, bottom_margin=8, left_margin=8, right_margin=8)
        self.buffer = self.texto.get_buffer()
        self.fim = self.buffer.create_mark("fim", self.buffer.get_end_iter(), False)
        rolagem = Gtk.ScrolledWindow(child=self.texto, vexpand=True)
        rolagem.add_css_class("card")
        caixa.append(rolagem)

        self.acoes = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER, visible=False)
        caixa.append(self.acoes)

        janela.empilhar(pagina(titulo, caixa))
        threading.Thread(target=self.rodar, daemon=True).start()

    def escrever(self, texto):
        self.buffer.insert(self.buffer.get_end_iter(), texto)
        self.texto.scroll_mark_onscreen(self.fim)   # acompanha o final, como um terminal

    def rodar(self):
        """Roda numa thread. Tudo que mexe na tela passa por GLib.idle_add."""
        for i, (descricao, cmd) in enumerate(self.passos, 1):
            GLib.idle_add(self.status.set_title, descricao)
            GLib.idle_add(self.status.set_subtitle, f"Step {i} of {len(self.passos)}")
            GLib.idle_add(self.escrever, f"\n══ {descricao}\n$ {' '.join(cmd[:4])}{' …' if len(cmd) > 4 else ''}\n")
            try:
                # stderr=STDOUT junta erros e saída normal no mesmo log; stdin=DEVNULL = ninguém digita nada
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, text=True, bufsize=1, env=ambiente_root())
                for linha in proc.stdout:
                    GLib.idle_add(self.escrever, linha)
                codigo = proc.wait()
            except OSError as erro:
                GLib.idle_add(self.escrever, f"{erro}\n")
                codigo = 1
            if codigo != 0:
                GLib.idle_add(self.terminar, False, descricao)
                return
        GLib.idle_add(self.terminar, True, None)

    def terminar(self, ok, passo_que_falhou):
        self.girando.set_visible(False)
        icone = Gtk.Image.new_from_icon_name("object-select-symbolic" if ok else "dialog-error-symbolic")
        icone.add_css_class("success" if ok else "error")
        self.status.add_prefix(icone)
        if ok:
            self.status.set_title("Done")
            self.status.set_subtitle("Restart your computer to finish: the kernel, drivers or desktop "
                                     "were updated" if self.reiniciar else "Everything finished successfully")
        else:
            self.status.set_title("Something went wrong")
            self.status.set_subtitle(f"Failed at: {passo_que_falhou}. See the log below.")
        self.escrever("\n══ " + ("Finished successfully" if ok else "Stopped because of an error") + "\n")
        self.acoes.append(botao("Back to updates", self.janela.voltar, "pill"))
        self.acoes.set_visible(True)
        self.ao_terminar()
