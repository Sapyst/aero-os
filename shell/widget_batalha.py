"""widget_batalha.py — Pokémon battle: lutas simuladas sem fim, só de assistir (doc 51).

Layout: o clássico dos jogos, em diagonal — o desafiante em cima à direita (caixa de HP à esquerda) e o
campeão embaixo à esquerda, espelhado pra olhar pro rival (caixa à direita); embaixo, a narração e o placar.
Regras e dados: batalha.py (nível 50, tabela de tipos, críticos, precisão...). Sprites: os do pokefetch
(pixel art, desenhada com filtro NEAREST em escala inteira: nada de borrão).
Quem vence FICA (rei da montanha) e enfrenta o próximo sorteado; o placar (vitórias/derrotas de cada um,
sequência atual, recorde) é guardado em ~/.cache/aero-shell/pokebattle.json.
Consumo: um evento a cada ~2 s; as animações (investida, tremor, barra de HP) só rodam enquanto duram.
"""
import math
import os
import random

from gi.repository import Gdk, GLib, Graphene, Gsk, Gtk

import batalha
from widgets import LARGURA_LARGA, MONO, PixelArt, Quadro, Widget, cor_texto, registrar, retangulo_redondo

SPRITES = os.path.expanduser("~/.local/share/pokefetch/sprites")
PLACAR = os.path.expanduser("~/.cache/aero-shell/pokebattle.json")
COR_TIPO = {"normal": "a8a77a", "fire": "ee8130", "water": "6390f0", "electric": "f7d02c", "grass": "7ac74c",
            "ice": "96d9d6", "fighting": "c22e28", "poison": "a33ea1", "ground": "e2bf65", "flying": "a98ff3",
            "psychic": "f95587", "bug": "a6b91a", "rock": "b6a136", "ghost": "735797", "dragon": "6f35fc",
            "dark": "705746", "steel": "b7b7ce", "fairy": "d685ad"}
VERDE, AMARELO, VERMELHO = (0.37, 0.75, 0.42), (0.89, 0.75, 0.29), (0.88, 0.34, 0.31)
VELOCIDADE = {"lenta": 1.6, "normal": 1.0, "rapida": 0.6}

_css = Gtk.CssProvider()
_css.load_from_string("\n".join(f".tipo-{t} {{ background-color: alpha(#{c}, 0.28); color: mix(#{c}, currentColor, 0.35);"
                                 f" box-shadow: inset 0 0 0 1px alpha(#{c}, 0.35); }}" for t, c in COR_TIPO.items())
                      + "\n.nome-poke { font-weight: 600; font-size: 13px; }"
                      + "\n.narracao { font-size: 12px; }"
                      + "\n.dano { font-family: " + MONO + "; color: #e0574f; font-weight: 800; font-size: 15px; text-shadow: 0 1px 3px rgba(0,0,0,0.6); }"
                      + "\n.dano-critico { font-size: 19px; color: #ffb347; }")
Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), _css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


Sprite = PixelArt            # o sprite em escala inteira mora em widgets.py (o System também usa)


class CaixaHP(Quadro):
    """Nome, tipos, barra de HP (animada), números e o retrospecto — numa moldura de terminal com o título
    ("challenger"/"champion") embutido na borda. Os rótulos têm max_width_chars: dentro do palco cada
    filho recebe a largura natural, e sem isso um nome comprido alargaria a caixa."""

    def __init__(self, widget, largura, titulo="", direita=False):
        super().__init__(titulo, widget.e, espaco=3)
        self.w = widget
        self.fracao = 1.0
        self.rastro = 1.0                          # barra clara que fica pra trás e desce depois (dá a "sensação" do golpe)
        self.anim = self.anim_rastro = None
        self.nome = Gtk.Label(xalign=0, ellipsize=3, max_width_chars=18)
        self.nome.add_css_class("nome-poke")
        self.tipos = Gtk.Box(spacing=4)
        self.append(self.nome)
        self.append(self.tipos)
        self.barra = Gtk.DrawingArea(content_height=int(8 * widget.e), hexpand=True)
        self.barra.set_draw_func(self.desenhar_barra)
        self.append(self.barra)
        base = Gtk.Box(spacing=6)
        self.hp = Gtk.Label(xalign=0, hexpand=True, ellipsize=3)
        self.hp.add_css_class("mono")
        self.hp.add_css_class("pequeno")
        self.retro = Gtk.Label(xalign=1, ellipsize=3)
        self.retro.add_css_class("mono")
        self.retro.add_css_class("apagado")
        self.retro.add_css_class("pequeno")
        base.append(self.hp)
        base.append(self.retro)
        self.append(base)

    def mostrar(self, lut, placar, campeao=False):
        self.lut = lut
        self.nome.set_label(f"{lut.nome}")
        while (c := self.tipos.get_first_child()) is not None:
            self.tipos.remove(c)
        for t in lut.tipos:
            chip = Gtk.Label(label=t.upper())
            chip.add_css_class("chip")
            chip.add_css_class(f"tipo-{t}")
            self.tipos.append(chip)
        v, d = placar.registro(lut.id)
        seq = placar.d["campeao"]["sequencia"] if campeao and placar.d["campeao"] and placar.d["campeao"]["id"] == lut.id else 0
        self.retro.set_label(f"{v}w {d}l" + (f" · ×{seq}" if seq > 1 else ""))
        self.fracao = lut.fracao
        self.atualizar_hp(animar=False)

    def atualizar_hp(self, animar=True):
        alvo = self.lut.fracao
        self.hp.set_label(f"{self.lut.hp} / {self.lut.hp_max}")
        for a in (self.anim, self.anim_rastro):
            if a:
                a.cancelar()
        if not animar or self.w.menos_movimento:
            self.fracao = self.rastro = alvo
            self.barra.queue_draw()
            return
        inicio, inicio_rastro = self.fracao, self.rastro
        def passo(v):
            self.fracao = inicio + (alvo - inicio) * v
            self.barra.queue_draw()
        def passo_rastro(v):
            self.rastro = inicio_rastro + (alvo - inicio_rastro) * v
            self.barra.queue_draw()
        self.anim = self.w.animar(450, passo)
        self.anim_rastro = self.w.animar(1100, passo_rastro, "inout")

    def desenhar_barra(self, area, cr, w, h):
        cor = cor_texto(area)
        retangulo_redondo(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(*cor[:3], 0.12)
        cr.fill()
        f = max(0.0, min(1.0, self.fracao))
        r = max(0.0, min(1.0, self.rastro))
        if r > f:                                           # o rastro: a parte que "acabou de sumir"
            retangulo_redondo(cr, 0, 0, max(h, w * r), h, h / 2)
            cr.set_source_rgba(*cor[:3], 0.35)
            cr.fill()
        if f > 0:
            retangulo_redondo(cr, 0, 0, max(h, w * f), h, h / 2)
            cr.set_source_rgba(*(VERDE if f > 0.5 else AMARELO if f > 0.2 else VERMELHO), 0.95)
            cr.fill()
        cr.set_source_rgba(*cor[:3], 0.18)                   # marquinhas a cada 10 %: barra "segmentada"
        cr.set_line_width(1)
        for i in range(1, 10):
            cr.move_to(round(w * i / 10) + 0.5, 1)
            cr.line_to(round(w * i / 10) + 0.5, h - 1)
        cr.stroke()


@registrar
class Batalha(Widget):
    tipo = "batalha"
    nome = "Pokémon battle"
    icone = "input-gaming-symbolic"
    opcoes_padrao = {"velocidade": "normal", "cura": "metade", "justo": False, "placar": True}

    LARG, ALT = LARGURA_LARGA, 264
    tamanho = (LARGURA_LARGA, 264)
    fps_animacao = 24            # o cartão todo é re-renderizado a cada quadro: 24 fps (cinema) bastam

    def montar(self):
        e = self.e
        self.rng = random.Random()
        self.placar = batalha.Placar(PLACAR)
        self.gerador = None
        self.campeao = None                  # Lutador que ficou da batalha anterior
        self.a, self.b = None, None
        self.maquina = None                  # temporizador do "máquina de escrever" da narração
        P = self.palco                       # tamanho fixo: nada aqui dentro mexe no cartão
        # ORDEM = camadas: os sprites entram PRIMEIRO (ficam atrás de painéis e textos). E as posições não se
        # sobrepõem: desafiante em cima à direita (y -4..108), campeão embaixo à esquerda (y 72..184).
        caixa = int(116 * e)
        self.sprite_b = Sprite(caixa)                      # desafiante: olhando pra esquerda
        self.sprite_a = Sprite(caixa, espelhar=True)       # campeão: olhando pra direita
        P.put(self.sprite_b, (self.LARG - 150) * e, -10 * e)      # caixa de 116: vai até y 106
        P.put(self.sprite_a, 30 * e, 70 * e)                       # até y 186 (o log começa em 186)
        self.caixa_b = CaixaHP(self, int(224 * e), "challenger")
        self.caixa_a = CaixaHP(self, int(224 * e), "champion")
        P.put(self.caixa_b, 0, 0, 224 * e, None)
        P.put(self.caixa_a, (self.LARG - 224) * e, 108 * e, 224 * e, None)   # começa depois do sprite (106)
        # a caixa de texto, como nos jogos: moldura "log" com "›" de prompt e a narração
        caixa_texto = Quadro("log", e, espaco=0)
        linha_txt = Gtk.Box(spacing=8 * e)
        prompt = Gtk.Label(label="›", valign=Gtk.Align.START)
        prompt.add_css_class("mono")
        prompt.add_css_class("apagado")
        linha_txt.append(prompt)
        self.narracao = Gtk.Label(xalign=0, yalign=0, wrap=True, lines=2, ellipsize=3, hexpand=True, valign=Gtk.Align.START)
        self.narracao.add_css_class("narracao")
        linha_txt.append(self.narracao)
        caixa_texto.append(linha_txt)
        P.put(caixa_texto, 0, 186 * e, self.LARG * e, 54 * e)
        self.rodape = Gtk.Label(xalign=0, ellipsize=3)
        self.rodape.add_css_class("mono")
        self.rodape.add_css_class("apagado")
        self.rodape.add_css_class("pequeno")
        self.rodape.set_visible(bool(self.opcoes["placar"]))
        P.put(self.rodape, 2 * e, 246 * e, (self.LARG - 4) * e, None)
        self.atualizar_rodape()
        self.bob = 0
        if not self.menos_movimento:
            self.a_cada(2, self.balancar)
        self.agendar(self.proxima_batalha, 600)

    # ---- ritmo ----
    def agendar(self, funcao, ms):
        fator = VELOCIDADE.get(self.opcoes["velocidade"], 1.0)
        def chamar():
            self.temporizadores = [t for t in self.temporizadores if t != ident[0]]
            funcao()
            return GLib.SOURCE_REMOVE
        ident = [GLib.timeout_add(int(ms * fator), chamar)]
        self.temporizadores.append(ident[0])

    def balancar(self):
        """Respiração: os sprites sobem/descem 1 px a cada segundo, alternados."""
        self.bob ^= 1
        self.sprite_a.dy = -1 if self.bob else 0
        self.sprite_b.dy = 0 if self.bob else -1
        self.sprite_a.queue_draw()
        self.sprite_b.queue_draw()

    # ---- a luta ----
    def sortear_desafiante(self):
        ids = list(batalha.dados()["pokemon"].keys())
        if self.campeao and self.opcoes["justo"]:
            alvo = self.campeao.total_base
            perto = [i for i in ids if abs(sum(batalha.dados()["pokemon"][i]["status"].values()) - alvo) <= 100
                     and int(i) != self.campeao.id]
            if len(perto) >= 10:
                ids = perto
        escolha = self.rng.choice(ids)
        while self.campeao and int(escolha) == self.campeao.id:
            escolha = self.rng.choice(ids)
        return escolha

    def proxima_batalha(self):
        if self.campeao is None:
            c = self.placar.d.get("campeao")
            id_a = str(c["id"]) if c else self.rng.choice(list(batalha.dados()["pokemon"].keys()))
            self.campeao = batalha.Lutador(id_a, self.rng)
        else:
            cura = self.opcoes["cura"]
            if cura == "total":
                self.campeao.hp = self.campeao.hp_max
            elif cura == "metade":
                self.campeao.hp = min(self.campeao.hp_max, self.campeao.hp + self.campeao.hp_max // 2)
        id_b = self.sortear_desafiante()
        self.gerador = batalha.batalha(self.campeao.id, id_b, self.rng, campeao=self.campeao)
        self.passo()

    def passo(self):
        try:
            ev = next(self.gerador)
        except StopIteration:
            self.agendar(self.proxima_batalha, 1500)
            return
        t = ev["tipo"]
        if t == "inicio":
            self.a, self.b = ev["a"], ev["b"]
            self.sprite_a.carregar(self.a.id)
            self.sprite_b.carregar(self.b.id)
            self.caixa_a.mostrar(self.a, self.placar, campeao=True)
            self.caixa_b.mostrar(self.b, self.placar)
            self.sprite_a.alfa = 1.0
            self.sprite_a.dx = 0
            # o desafiante entra deslizando da direita
            def entrar(v):
                self.sprite_b.dx = (1 - v) * 70
                self.sprite_b.alfa = v
                self.sprite_b.queue_draw()
            self.animar(480, entrar)
            self.narrar(f"{self.b.nome} challenges {self.a.nome}!")
            self.agendar(self.passo, 1800)
        elif t == "turno":
            self.passo()
        elif t == "golpe":
            quem, alvo = ev["quem"], ev["alvo"]
            self.investida(quem)
            self.agendar(lambda: self.acertar(ev), 220)
            extra = []
            if ev["critico"]:
                extra.append("A critical hit!")
            if ev["mult"] >= 2:
                extra.append("It's super effective!")
            elif 0 < ev["mult"] < 1:
                extra.append("It's not very effective…")
            if ev["recuo"]:
                extra.append(f"{quem.nome} is hurt by the recoil.")
            self.narrar(f"{quem.nome} used {ev['golpe']}!" + ("\n" + " ".join(extra) if extra else ""))
            self.agendar(self.passo, 2000 if extra else 1600)
        elif t == "errou":
            self.investida(ev["quem"])
            self.narrar(f"{ev['quem'].nome} used {ev['golpe']}!\nIt missed.")
            self.agendar(self.passo, 1500)
        elif t == "carga":
            self.narrar(f"{ev['quem'].nome} is charging {ev['golpe']}…")
            self.agendar(self.passo, 1300)
        elif t == "recarga":
            self.narrar(f"{ev['quem'].nome} must recharge.")
            self.agendar(self.passo, 1300)
        elif t == "falhou":
            self.narrar(f"{ev['quem'].nome} lost its focus and couldn't move!")
            self.agendar(self.passo, 1400)
        elif t == "imune":
            self.investida(ev["quem"])
            self.narrar(f"{ev['quem'].nome} used {ev['golpe']}!\nIt doesn't affect {ev['alvo'].nome}…")
            self.agendar(self.passo, 1600)
        elif t == "desmaiou":
            sp = self.sprite_de(ev["quem"])
            def cair(v):
                sp.dy = v * 26
                sp.alfa = 1 - v
                sp.queue_draw()
            self.animar(520, cair, "in")
            self.narrar(f"{ev['quem'].nome} fainted!")
            self.agendar(self.passo, 1500)
        elif t == "fim":
            self.terminar(ev)

    def sprite_de(self, lut):
        return self.sprite_a if lut is self.a else self.sprite_b

    def caixa_de(self, lut):
        return self.caixa_a if lut is self.a else self.caixa_b

    def investida(self, quem):
        sp = self.sprite_de(quem)
        sentido = 1 if quem is self.a else -1          # o campeão investe pra direita, o desafiante pra esquerda
        def ir(v):
            sp.dx = math.sin(v * math.pi) * 16 * sentido
            sp.queue_draw()
        self.animar(380, ir, "inout")

    def acertar(self, ev):
        alvo = ev["alvo"]
        sp = self.sprite_de(alvo)
        critico = ev["critico"]
        def tremer(v):
            sp.dx = math.sin(v * math.pi * 5) * (7 if critico else 5) * (1 - v)
            sp.brilho = max(0.0, 1 - v * 1.6)            # flash branco que some rápido
            sp.queue_draw()
        self.animar(420, tremer, "linear")
        self.caixa_de(alvo).atualizar_hp()
        if ev["recuo"]:
            self.caixa_de(ev["quem"]).atualizar_hp()
        self.numero_dano(alvo, ev["dano"], critico)

    def numero_dano(self, alvo, dano, critico):
        """O número do dano sobe e some, em cima de quem apanhou."""
        e = self.e
        lb = Gtk.Label(label=f"−{dano}" + ("!" if critico else ""))
        lb.add_css_class("dano")
        if critico:
            lb.add_css_class("dano-critico")
        sp = self.sprite_de(alvo)
        x = ((self.LARG - 150) * e + 62 * e) if sp is self.sprite_b else 92 * e
        y0 = (6 * e) if sp is self.sprite_b else 80 * e
        self.palco.put(lb, x, y0)
        def subir(v):
            self.palco.move(lb, x, y0 - 30 * e * v)
            lb.set_opacity(1.0 if v < 0.55 else 1 - (v - 0.55) / 0.45)
        self.animar(950, subir, "out", ao_terminar=lambda: self.palco.remove(lb))

    def terminar(self, ev):
        v, p = ev["vencedor"], ev["perdedor"]
        self.placar.registrar(v, p, ev["turnos"])
        if v is None:
            self.narrar("Both Pokémon fainted. No winner this time.")
            self.campeao = None
        else:
            seq = self.placar.d["campeao"]["sequencia"]
            if ev["tempo"]:
                self.narrar(f"Time's up: {v.nome} wins on HP!" + (f"  Streak: {seq}." if seq > 1 else ""))
            else:
                self.narrar(f"{v.nome} wins!" + (f"  Streak: {seq}." if seq > 1 else "  New champion!" if v is self.b else ""))
            self.campeao = v
        self.atualizar_rodape()
        self.caixa_a.mostrar(self.a, self.placar, campeao=(v is self.a))
        self.caixa_b.mostrar(self.b, self.placar, campeao=(v is self.b))
        self.agendar(self.trocar_lados if v is self.b else self.proxima_batalha, 3600)

    def trocar_lados(self):
        """O desafiante venceu: vira o campeão e passa pro lado de baixo (some de um lado, surge do outro)."""
        def sumir(val):
            self.sprite_b.alfa = 1 - val
            self.sprite_b.queue_draw()
        self.animar(300, sumir)
        self.agendar(self.proxima_batalha, 350)

    def narrar(self, s):
        """Máquina de escrever (como nos jogos): ~45 letras por segundo. Com "Reduce motion", de uma vez."""
        if self.maquina:
            GLib.source_remove(self.maquina)
            self.maquina = None
        if self.menos_movimento or len(s) < 4:
            self.narracao.set_label(s)
            return
        estado = {"i": 0}
        def letra():
            estado["i"] += 4
            self.narracao.set_label(s[:estado["i"]])
            if estado["i"] >= len(s):
                self.maquina = None
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE
        self.narracao.set_label("")
        self.maquina = GLib.timeout_add(80, letra)

    def atualizar_rodape(self):
        d = self.placar.d
        partes = [f"Battle #{d['batalhas'] + 1}"]
        if d.get("recorde"):
            partes.append(f"Record streak: {self.nome_de(d['recorde']['id'])} ×{d['recorde']['sequencia']}")
        top = self.placar.top(3)
        if top:
            partes.append("Top: " + ", ".join(f"{self.nome_de(i)} {w}" for i, w, _ in top))
        self.rodape.set_label("[→ " + "  ·  ".join(p.lower() for p in partes) + "]")

    @staticmethod
    def nome_de(pid):
        return batalha.dados()["pokemon"].get(str(pid), {}).get("nome", "?")

    def parar(self):
        super().parar()
        if self.maquina:
            GLib.source_remove(self.maquina)
            self.maquina = None
