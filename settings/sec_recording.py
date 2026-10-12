"""
sec_recording.py — seção Screen recording.

Grava chaves REC_* no painel.conf; o ~/.local/bin/screen-record lê na hora de cada gravação
(não tem "Apply"). Atalhos: Ctrl+Print (área) e Ctrl+Shift+Print (monitor inteiro); de novo = para.
"""
import json
import os
import subprocess

from gi.repository import Adw, Gio, GLib, Gtk

from comum import Secao, abrir, atualizar_painel, botao, combo, indice, ler_painel, pagina

SCRIPT = os.path.expanduser("~/.local/bin/screen-record")
QUALIDADES = [("Small file", "small"), ("Balanced", "balanced"), ("High", "high")]
FPS = [("24 fps", "24"), ("30 fps", "30"), ("60 fps", "60")]
ENCODERS = [("Automatic", "auto"), ("Graphics card (NVENC)", "gpu"), ("Processor (x264)", "cpu")]
FORMATOS = [("MP4", "mp4"), ("MKV", "mkv")]
AUDIOS = [("Off", "off"), ("Computer sound", "desktop"), ("Microphone", "mic"),
          ("Computer sound + microphone", "both")]
CONTAGENS = [("Off", "0"), ("3 seconds", "3"), ("5 seconds", "5")]
DEPOIS = [("Show a notification", "notify"), ("Open the video", "video"), ("Open the folder", "folder"),
          ("Copy the video (paste in a chat)", "copy")]


def pasta_padrao():
    try:
        base = subprocess.run(["xdg-user-dir", "VIDEOS"], capture_output=True, text=True).stdout.strip()
    except OSError:
        base = ""
    return os.path.join(base or os.path.expanduser("~/Videos"), "Recordings")


def microfones():
    """[(nome mostrado, nome do PipeWire)], começando por "Default"."""
    lista = [("Default", "default")]
    try:
        saida = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, text=True).stdout
        for s in json.loads(saida or "[]"):
            if s.get("monitor_source") or s["name"].endswith(".monitor"):
                continue
            lista.append((s.get("description") or s["name"], s["name"]))
    except (OSError, ValueError, KeyError):
        pass
    return lista


class SecaoGravacao(Secao):
    id = "recording"
    titulo = "Screen recording"
    icone = "media-record-symbolic"

    def __init__(self, janela):
        super().__init__(janela)
        self.carregando = False
        self.mics = microfones()
        conteudo = Adw.PreferencesPage()

        gravar = Adw.PreferencesGroup(
            title="Screen recording",
            description="Ctrl+Shift+Print asks what to record: 1 for the whole screen, 2 for an area you drag. "
                        "Ctrl+Print goes straight to the area. Press either again to stop. "
                        "Changes here apply to the next recording.")
        agora = Adw.ActionRow(use_markup=False, title="Record now", subtitle="Starts right away")
        agora.add_prefix(Gtk.Image(icon_name="media-record-symbolic"))
        agora.add_suffix(botao("Area", lambda: self.iniciar()))
        agora.add_suffix(botao("Whole screen", lambda: self.iniciar("screen")))
        gravar.add(agora)

        video = Adw.PreferencesGroup(title="Video")
        self.qualidade = combo("Quality", QUALIDADES, 1, "Higher quality makes bigger files")
        self.fps = combo("Frame rate", FPS, 2, "24 looks like film, 30 is lighter, 60 is the smoothest")
        self.encoder = combo("Encoder", ENCODERS, 0,
                             "Automatic uses the graphics card when it can, so the computer stays fast")
        self.formato = combo("Format", FORMATOS, 0, "MP4 plays and uploads anywhere; MKV survives a crash")
        for r in (self.qualidade, self.fps, self.encoder, self.formato):
            video.add(r)

        som = Adw.PreferencesGroup(title="Sound")
        self.audio = combo("Record", AUDIOS, 1)
        self.mic = combo("Microphone", self.mics, 0)
        for r in (self.audio, self.mic):
            som.add(r)

        fluxo = Adw.PreferencesGroup(title="Before and after")
        self.contagem = combo("Countdown", CONTAGENS, 0, "Time to get ready before it starts")
        self.depois = combo("When it's saved", DEPOIS, 0)
        for r in (self.contagem, self.depois):
            fluxo.add(r)

        destino = Adw.PreferencesGroup(title="Save to")
        self.pasta = Adw.ActionRow(use_markup=False, title="Folder")
        self.pasta.add_prefix(Gtk.Image(icon_name="folder-videos-symbolic"))
        self.pasta.add_suffix(botao("Open", lambda: abrir(["xdg-open", self.pasta_atual()])))
        self.pasta.add_suffix(botao("Change…", self.escolher_pasta))
        padrao = Adw.ActionRow(use_markup=False, title="Use the default folder", subtitle=pasta_padrao())
        padrao.add_suffix(botao("Reset", self.pasta_resetar))
        destino.add(self.pasta)
        destino.add(padrao)

        for linha in (self.qualidade, self.fps, self.encoder, self.formato, self.audio, self.mic,
                      self.contagem, self.depois):
            linha.connect("notify::selected", lambda *_: self.gravar())
        for g in (gravar, video, som, fluxo, destino):
            conteudo.add(g)
        self.pagina = pagina(self.titulo, conteudo)

    def pasta_atual(self):
        p = ler_painel().get("REC_DIR", "")
        return os.path.expanduser(p) if p else pasta_padrao()

    def ao_mostrar(self):
        self.carregando = True
        p = ler_painel()
        self.qualidade.set_selected(indice(QUALIDADES, p.get("REC_QUALITY", "balanced"), 1))
        self.fps.set_selected(indice(FPS, p.get("REC_FPS", "60"), 2))
        self.encoder.set_selected(indice(ENCODERS, p.get("REC_ENCODER", "auto")))
        self.formato.set_selected(indice(FORMATOS, p.get("REC_FORMAT", "mp4")))
        self.audio.set_selected(indice(AUDIOS, p.get("REC_AUDIO", "desktop"), 1))
        self.mic.set_selected(indice(self.mics, p.get("REC_MIC", "default")))
        self.contagem.set_selected(indice(CONTAGENS, p.get("REC_COUNTDOWN", "0")))
        self.depois.set_selected(indice(DEPOIS, p.get("REC_AFTER", "notify")))
        self.pasta.set_subtitle(self.pasta_atual())
        self.mic.set_sensitive(AUDIOS[self.audio.get_selected()][1] in ("mic", "both"))
        self.carregando = False

    def gravar(self):
        self.mic.set_sensitive(AUDIOS[self.audio.get_selected()][1] in ("mic", "both"))
        if self.carregando:
            return
        atualizar_painel(REC_QUALITY=QUALIDADES[self.qualidade.get_selected()][1],
                         REC_FPS=FPS[self.fps.get_selected()][1],
                         REC_ENCODER=ENCODERS[self.encoder.get_selected()][1],
                         REC_FORMAT=FORMATOS[self.formato.get_selected()][1],
                         REC_AUDIO=AUDIOS[self.audio.get_selected()][1],
                         REC_MIC=self.mics[self.mic.get_selected()][1],
                         REC_COUNTDOWN=CONTAGENS[self.contagem.get_selected()][1],
                         REC_AFTER=DEPOIS[self.depois.get_selected()][1])

    def escolher_pasta(self):
        dialogo = Gtk.FileDialog(title="Save recordings to")
        dialogo.set_initial_folder(Gio.File.new_for_path(self.pasta_atual()))

        def pronto(d, resultado):
            try:
                pasta = d.select_folder_finish(resultado)
            except GLib.Error:
                return                         # cancelado
            atualizar_painel(REC_DIR=pasta.get_path())
            self.pasta.set_subtitle(pasta.get_path())
            self.janela.avisar("Recordings will be saved there")
        dialogo.select_folder(self.janela, None, pronto)

    def pasta_resetar(self):
        atualizar_painel(REC_DIR="")
        self.pasta.set_subtitle(pasta_padrao())
        self.janela.avisar("Back to the default folder")

    def iniciar(self, modo=None):
        cmd = [SCRIPT] + ([modo] if modo else [])
        # a janela das Settings sai da frente pra não aparecer na gravação (o Hyprland ignora o
        # "minimizar" do GTK; o hypr-minimizar manda a janela ativa pro special:minimized)
        subprocess.Popen([os.path.expanduser("~/.local/bin/hypr-minimizar")])
        GLib.timeout_add(350, lambda: (subprocess.Popen(cmd, start_new_session=True), False)[1])
