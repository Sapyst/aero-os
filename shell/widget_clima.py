"""widget_clima.py — Weather: agora e os próximos dias (5 ou 7), do mesmo lugar do Settings → Location.
Dados e desenhos vêm do clima.py (Open-Meteo, cache de 30 min). A busca roda numa thread, a cada 30 min e
quando a localização/unidade muda no Settings (vigia o painel.conf).
Visual: temperatura em números finos, detalhes em mono; os dias em colunas sem caixas (o de hoje marcado
por um ponto), com um fio de 2 px da faixa de temperatura (azul frio → laranja quente, na escala da semana).
Dados novos surgem num fade. Tamanho fixo."""
import datetime
import threading

import cairo
from gi.repository import Gio, GLib

import clima
from widgets import LARGURA_LARGA, PASTEL, SOLIDO, Widget, cor_texto, painel_cairo, registrar, texto, tracejado

FRIO, QUENTE = PASTEL["azul"], PASTEL["pessego"]
clima.BAIXAR_FORA = True       # a previsão vem por um curl à parte: nada de ssl/http dentro do daemon dos widgets
MONO_FAM = "JetBrainsMono Nerd Font"


@registrar
class Clima(Widget):
    tipo = "clima"
    nome = "Weather"
    icone = "weather-clear-symbolic"
    opcoes_padrao = {"privado": False, "dias": 7, "detalhes": True}
    tamanho = (LARGURA_LARGA, 206)

    def montar(self):
        self.dados = None
        self.alfa = 1.0
        n = int(self.opcoes["dias"])
        w = LARGURA_LARGA
        h = 206
        self.redimensionar(w, h)
        self.tela = self.area(w, h, self.desenhar)
        self.cartao.append(self.tela)
        self.buscar()
        self.a_cada(30 * 60, self.buscar)
        self.vigia = Gio.File.new_for_path(clima.PAINEL).monitor_file(Gio.FileMonitorFlags.NONE, None)
        self._debounce = None
        self.vigia.connect("changed", self.ao_mudar_painel)

    def ao_mudar_painel(self, *_):
        if self._debounce:
            GLib.source_remove(self._debounce)
        self._debounce = GLib.timeout_add(1500, lambda: (setattr(self, "_debounce", None), self.buscar(True), False)[2])

    def parar(self):
        super().parar()
        if getattr(self, "vigia", None):
            self.vigia.cancel()

    def buscar(self, forcar=False):
        n = int(self.opcoes["dias"])
        def tarefa():
            d = clima.obter_semana(forcar=forcar, dias=n)
            GLib.idle_add(self.receber, d)
        threading.Thread(target=tarefa, daemon=True).start()

    def receber(self, d):
        if d and d is not self.dados:
            self.dados = d
            self.alfa = 0.0
            self.animar(450, lambda v: (setattr(self, "alfa", v), self.tela.queue_draw()))
        else:
            self.tela.queue_draw()
        return GLib.SOURCE_REMOVE

    def icone_clima(self, cr, x, y, s, desenho, dia=True):
        cr.save()
        cr.translate(x, y)
        clima.desenhar(cr, s, desenho, dia, fundo_claro=not self.escuro)
        cr.restore()

    def desenhar(self, cr, w, h):
        area, e = self.tela, self.e
        cor = cor_texto(area)
        if not self.dados:
            painel_cairo(cr, 0, 0, w, h, 14 * e)
            texto(cr, area, "weather", 12 * e, 10 * e, 10 * e, "bold", 0.5, familia=MONO_FAM)
            texto(cr, area, "waiting for the forecast…", 12 * e, 30 * e, 12 * e, "normal", 0.5, familia=MONO_FAM)
            return
        cr.push_group()
        agora, dias = self.dados["agora"], self.dados["dias"]
        alt_agora = 80 * e
        # ---- agora: moldura "now" ----
        painel_cairo(cr, 0, 6 * e, w, alt_agora - 6 * e, 10 * e, "now", area, e)
        self.icone_clima(cr, 12 * e, 17 * e, 52 * e, agora["desenho"], agora["dia"])
        texto(cr, area, f"{agora['temp']}°", 78 * e, 10 * e, 34 * e, "bold", 0.95, familia=MONO_FAM)
        texto(cr, area, agora["texto"].lower(), 80 * e, 52 * e, 11 * e, "normal", 0.6, familia=MONO_FAM)
        if self.opcoes["detalhes"]:
            # privado: sem o nome do lugar nem nascer/pôr do sol (dão a latitude) — para prints
            privado = self.opcoes.get("privado")
            lugar = "" if privado else (self.dados.get("lugar") or "").lower()
            linhas = []
            if lugar:
                linhas.append(lugar)
            linhas.append(f"feels {agora['sensacao']}°  ·  wind {agora['vento']} km/h")
            if agora.get("umidade") is not None:
                hoje = dias[0] if dias else {}
                extra = f"  ·  {hoje['nascer']} – {hoje['por']}" if hoje.get("nascer") and hoje.get("por") and not privado else ""
                linhas.append(f"humidity {agora['umidade']}%" + extra)
            for i, s in enumerate(linhas):
                texto(cr, area, s, w - 12 * e, 18 * e + i * 16 * e, 11 * e, "normal", 0.55, "direita", MONO_FAM)
        # ---- os dias: sub-cartão com colunas ----
        n = len(dias)
        y0 = alt_agora + 14 * e
        painel_cairo(cr, 0, y0, w, h - y0, 10 * e, "forecast", area, e)
        y0 += 2 * e
        tw = w / n
        mins = [d["min"] for d in dias] or [0]
        maxs = [d["max"] for d in dias] or [1]
        wmin, wmax = min(mins), max(maxs)
        faixa = max(1, wmax - wmin)
        for i, d in enumerate(dias):
            x = i * tw
            cx = x + tw / 2
            try:
                nome = "today" if i == 0 else datetime.date.fromisoformat(d["data"]).strftime("%a").lower()
            except ValueError:
                nome = d["data"][-2:]
            cr.set_source_rgba(*(PASTEL["rosa"] if i == 0 else cor[:3]), 1.0 if i == 0 else 0.5)
            texto(cr, area, nome, cx, y0 + 10 * e, 10 * e, "bold", 0.0, "centro", MONO_FAM)   # mede (invisível)
            self._texto_cor(cr, area, nome, cx, y0 + 10 * e, 10 * e, PASTEL["rosa"] if i == 0 else None, 1.0 if i == 0 else 0.5)
            s = 30 * e
            self.icone_clima(cr, cx - s / 2, y0 + 26 * e, s, d["desenho"], True)
            self._texto_cor(cr, area, f"{d['max']}°", cx - 2 * e, y0 + 62 * e, 13 * e, PASTEL["pessego"], 1.0, "direita")
            self._texto_cor(cr, area, f"{d['min']}°", cx + 2 * e, y0 + 64 * e, 11 * e, PASTEL["azul"], 0.9, "esquerda")
            bx, bw, by, bh = x + 12 * e, tw - 24 * e, y0 + 84 * e, 3 * e
            if not SOLIDO:
                cr.rectangle(bx, by, bw, bh)
                cr.set_source_rgba(*cor[:3], 0.10)
                cr.fill()
            f0, f1 = (d["min"] - wmin) / faixa, (d["max"] - wmin) / faixa
            frio, quente = (PASTEL["azul"], PASTEL["rosa"]) if SOLIDO else (FRIO, QUENTE)
            grad = cairo.LinearGradient(bx, 0, bx + bw, 0)
            grad.add_color_stop_rgb(0, *frio)
            grad.add_color_stop_rgb(1, *quente)
            if SOLIDO:                             # Solid: pílulas (trilho tonal + faixa da semana)
                from widgets import retangulo_redondo
                import solido
                bh = 4 * e
                retangulo_redondo(cr, bx, by, bw, bh, bh / 2)
                cr.set_source_rgb(*solido.rgb("surface_container_highest"))
                cr.fill()
                retangulo_redondo(cr, bx + bw * f0, by, max(bh, bw * (f1 - f0)), bh, bh / 2)
            else:
                cr.rectangle(bx + bw * f0, by, max(bh, bw * (f1 - f0)), bh)
            cr.set_source(grad)
            cr.fill()
            chuva = d.get("chance_chuva")
            if chuva is not None and chuva >= 20:
                self._texto_cor(cr, area, f"{chuva}%", cx, y0 + 92 * e, 9.5 * e, PASTEL["azul"], 0.8)
        cr.pop_group_to_source()
        cr.paint_with_alpha(self.alfa)

    def _texto_cor(self, cr, area, s, x, y, tamanho, cor, alfa=1.0, alinhar="centro"):
        """texto() numa cor pastel (texto() usa a cor do tema): pinta por cima com a cor pedida."""
        from widgets import _layouts  # noqa: F401 (o cache de layouts é interno ao texto())
        import widgets
        cor_w = area.get_color()
        if cor is None:
            widgets.texto(cr, area, s, x, y, tamanho, "bold" if tamanho <= 10 * self.e else "medium", alfa, alinhar, MONO_FAM)
            return
        # mesma máquina do texto(): layout em cache, mas com a cor pedida
        chave = (id(area), s, round(tamanho, 2), "bold" if tamanho <= 10 * self.e else "medium", MONO_FAM)
        layout = widgets._layouts.get(chave)
        if layout is None:
            widgets.texto(cr, area, s, x, y, tamanho, "bold" if tamanho <= 10 * self.e else "medium", 0.0, alinhar, MONO_FAM)
            layout = widgets._layouts[chave]
        widgets.PangoCairo.update_layout(cr, layout)
        _, log = layout.get_pixel_extents()
        dx = {"esquerda": 0, "centro": -log.width / 2, "direita": -log.width}[alinhar]
        cr.set_source_rgba(*cor[:3], alfa)
        cr.move_to(x + dx, y)
        widgets.PangoCairo.show_layout(cr, layout)
