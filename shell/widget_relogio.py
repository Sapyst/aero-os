"""widget_relogio.py — Clock no estilo do dashboard: um sub-cartão com a hora grande em mono (as horas em
destaque, os minutos mais apagados) e a data em números embaixo; outro sub-cartão com o dia da semana e o mês.
Acorda 1x por minuto (ou por segundo, com os segundos ligados). Tamanho fixo."""
import time

from gi.repository import GLib, Gtk

from widgets import LARGURA_LARGA, Quadro, Widget, registrar


@registrar
class Relogio(Widget):
    tipo = "relogio"
    nome = "Clock"
    icone = "alarm-symbolic"
    opcoes_padrao = {"segundos": False, "formato": "24h", "data": True}
    tamanho = (LARGURA_LARGA, 104)

    def montar(self):
        e = self.e
        self.cartao.set_orientation(Gtk.Orientation.HORIZONTAL)
        self.cartao.set_spacing(10 * e)
        painel = Quadro("clock", e, espaco=0)
        painel.set_size_request(int(236 * e), -1)
        linha = Gtk.Box(spacing=8 * e, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True)
        self.horas = self.rotulo("", "hora")
        self.minutos = self.rotulo("", "hora hora-dim")
        linha.append(self.horas)
        linha.append(self.minutos)
        if self.opcoes["segundos"] or self.opcoes["formato"] == "12h":
            self.extra = self.rotulo("", "mono dim pequeno", valign=Gtk.Align.END, margin_bottom=9 * e)
            linha.append(self.extra)
        else:
            self.extra = None
        painel.append(linha)
        self.cartao.append(painel)
        caixa_data = Quadro("date", e, espaco=2)
        caixa_data.set_hexpand(True)
        self.dia_semana = self.rotulo("", "mono medio")
        self.data = self.rotulo("", "mono dim pequeno")
        self.semana = self.rotulo("", "mono apagado pequeno")
        for r in (self.dia_semana, self.data, self.semana):
            caixa_data.append(r)
        caixa_data.set_visible(bool(self.opcoes["data"]))
        self.cartao.append(caixa_data)
        self.atualizar()
        self.agendar()

    def agendar(self):
        passo = 1 if self.opcoes["segundos"] else 60
        espera = passo - (time.time() % passo)
        self.temporizadores.append(GLib.timeout_add(int(espera * 1000) + 5, self.tique))

    def tique(self):
        self.temporizadores.clear()
        self.atualizar()
        self.agendar()
        return GLib.SOURCE_REMOVE

    def atualizar(self):
        t = time.localtime()
        h = t.tm_hour if self.opcoes["formato"] == "24h" else (t.tm_hour % 12 or 12)
        self.horas.set_label(f"{h:02d}")
        self.minutos.set_label(f"{t.tm_min:02d}")
        if self.extra is not None:
            partes = []
            if self.opcoes["segundos"]:
                partes.append(f"{t.tm_sec:02d}")
            if self.opcoes["formato"] == "12h":
                partes.append("am" if t.tm_hour < 12 else "pm")
            self.extra.set_label(" ".join(partes))
        self.dia_semana.set_label(time.strftime("%A").lower())
        self.data.set_label(time.strftime("%d %B %Y").lower())
        self.semana.set_label(time.strftime("week %V · day %j").lower())
