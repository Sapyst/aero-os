"""
batalha.py — o motor da batalha Pokémon simulada do widget (doc 51). Só regras, sem desenho.

De onde vêm os dados (tudo local, nada de internet):
  ~/.local/share/pokefetch/catalogo.json   nome, tipos, status base (o mesmo do terminal)
  ~/.local/share/pokefetch/batalha.json    tabela de tipos, golpes com poder/precisão, golpes por Pokémon
                                           (gerado pelo pokefetch-golpes)
As regras (as dos jogos, nível 50, simplificadas pra ficar legível):
  - status no nível 50 com IVs 31 e natureza neutra; 4 golpes de DANO escolhidos entre os que ele aprende
    (dá preferência a golpes do próprio tipo — STAB — e à classe física/especial em que ele é melhor);
  - quem age primeiro: prioridade do golpe, depois Velocidade (empate: sorteio);
  - dano = fórmula oficial × STAB 1,5 × tabela de tipos × crítico (1 em 24, ×1,5) × sorte (85–100 %);
  - precisão: cada golpe pode errar; golpes de recarga (Hyper Beam…) custam o turno seguinte; golpes de
    carga (Solar Beam…) gastam um turno antes; Focus Punch falha se o usuário levou dano no turno;
  - golpes de recuo (Brave Bird, Flare Blitz…) machucam quem usa; sem golpe de dano (Ditto, Magikarp…):
    Struggle (50 de poder, 1/4 do HP de recuo);
  - HP ×2,5 (FATOR_HP): regra do widget, pra luta durar o bastante pra ser vista.
A batalha é um GERADOR de eventos (dicionários): quem desenha consome no ritmo que quiser.
"""
import json
import math
import os
import random

PASTA = os.path.expanduser("~/.local/share/pokefetch")
NIVEL = 50
MAX_TURNOS = 120
FATOR_HP = 2.5        # regra do WIDGET: HP ×2,5. Nos jogos cai em 1–2 golpes; aqui dá pra assistir (~6–10 turnos)

RECARGA = {"Hyper Beam", "Giga Impact", "Blast Burn", "Hydro Cannon", "Frenzy Plant", "Rock Wrecker",
           "Roar Of Time", "Prismatic Laser", "Eternabeam", "Meteor Assault"}
CARGA = {"Solar Beam", "Solar Blade", "Skull Bash", "Sky Attack", "Razor Wind", "Fly", "Dig", "Dive", "Bounce",
         "Phantom Force", "Shadow Force", "Freeze Shock", "Ice Burn", "Meteor Beam", "Electro Shot", "Sky Drop"}
SUICIDAS = {"Explosion", "Self Destruct", "Misty Explosion", "Mind Blown", "Steel Beam", "Final Gambit",
            "Memento", "Healing Wish", "Lunar Dance"}
RECUO = {"Head Smash": 1 / 2, "Brave Bird": 1 / 3, "Flare Blitz": 1 / 3, "Wood Hammer": 1 / 3, "Wave Crash": 1 / 3,
         "Volt Tackle": 1 / 3, "Double Edge": 1 / 3, "Wild Charge": 1 / 4, "Take Down": 1 / 4, "Submission": 1 / 4,
         "Head Charge": 1 / 4, "Light Of Ruin": 1 / 2, "Chloroblast": 1 / 2}      # fração do DANO causado
STRUGGLE = ["Struggle", None, 50, None, "fisico", 0]

_dados = None


def dados():
    """Catálogo + dados de batalha, carregados uma vez."""
    global _dados
    if _dados is None:
        with open(os.path.join(PASTA, "catalogo.json")) as f:
            cat = json.load(f)
        with open(os.path.join(PASTA, "batalha.json")) as f:
            bat = json.load(f)
        _dados = {"pokemon": cat["pokemon"], "tipos": bat["tipos"], "golpes": bat["golpes"],
                  "aprende": bat["pokemon"]}
    return _dados


def multiplicador(tipo_golpe, tipos_alvo):
    if tipo_golpe is None:                       # Struggle: sem tipo, acerta tudo igual
        return 1.0
    m = 1.0
    for t in tipos_alvo:
        m *= dados()["tipos"][tipo_golpe][t]
    return m


class Lutador:
    def __init__(self, pid, rng=random):
        d = dados()
        p = d["pokemon"][str(pid)]
        self.id = int(pid)
        self.nome = p["nome"]
        self.tipos = list(p["tipos"])
        b = p["status"]
        # nível 50, IV 31, EV 0, natureza neutra (fórmula dos jogos)
        self.hp_max = int(((2 * b["hp"] + 31) * NIVEL // 100 + NIVEL + 10) * FATOR_HP)
        self.atk = (2 * b["attack"] + 31) * NIVEL // 100 + 5
        self.dfs = (2 * b["defense"] + 31) * NIVEL // 100 + 5
        self.spa = (2 * b["special-attack"] + 31) * NIVEL // 100 + 5
        self.spd = (2 * b["special-defense"] + 31) * NIVEL // 100 + 5
        self.spe = (2 * b["speed"] + 31) * NIVEL // 100 + 5
        self.total_base = sum(b.values())
        self.hp = self.hp_max
        self.golpes = self.escolher_golpes(rng)
        self.recarregando = False
        self.carregando = None                   # golpe de carga iniciado no turno anterior
        self.levou_dano_no_turno = False

    # ---- golpes ----
    def escolher_golpes(self, rng):
        """4 golpes: até 2 do próprio tipo (STAB), o resto por força, dando peso à classe em que é melhor."""
        d = dados()
        candidatos = []
        for mid in d["aprende"].get(str(self.id), []):
            g = d["golpes"][str(mid)]
            if g[0] in SUICIDAS:
                continue
            nome, tipo, poder, prec, classe, _ = g
            forca = poder * ((prec or 100) / 100)
            forca *= 1.5 if tipo in self.tipos else 1.0
            forca *= 0.7 if (nome in RECARGA or nome in CARGA) else 1.0
            forca *= (self.atk if classe == "fisico" else self.spa) / max(self.atk, self.spa)
            candidatos.append((forca * rng.uniform(0.85, 1.15), g))   # um pouco de sorte na escolha
        candidatos.sort(key=lambda c: -c[0])
        escolhidos, tipos_usados = [], set()
        for _, g in candidatos:                  # 1ª passada: o melhor de cada tipo (variedade)
            if g[1] not in tipos_usados and len(escolhidos) < 4:
                escolhidos.append(g)
                tipos_usados.add(g[1])
        for _, g in candidatos:                  # 2ª passada: completa com os mais fortes
            if len(escolhidos) >= 4:
                break
            if g not in escolhidos:
                escolhidos.append(g)
        return escolhidos or [STRUGGLE]

    def escolher_golpe(self, alvo, rng):
        """A "IA": 75 % das vezes o golpe que mais dói contra o alvo; o resto, sorteio (sorte ou azar)."""
        if self.carregando:
            return self.carregando
        def estimativa(g):
            nome, tipo, poder, prec, classe, _ = g
            a, dfs = (self.atk, alvo.dfs) if classe == "fisico" else (self.spa, alvo.spd)
            return poder * a / dfs * multiplicador(tipo, alvo.tipos) * ((prec or 100) / 100) * \
                (1.5 if tipo in self.tipos else 1.0)
        if rng.random() < 0.75:
            return max(self.golpes, key=estimativa)
        return rng.choice(self.golpes)

    @property
    def vivo(self):
        return self.hp > 0

    @property
    def fracao(self):
        return max(0.0, self.hp / self.hp_max)


def dano(atacante, alvo, golpe, rng):
    """(dano, multiplicador de tipo, foi crítico?) pela fórmula dos jogos."""
    nome, tipo, poder, _, classe, _ = golpe
    a, dfs = (atacante.atk, alvo.dfs) if classe == "fisico" else (atacante.spa, alvo.spd)
    mult = multiplicador(tipo, alvo.tipos)
    if mult == 0:
        return 0, 0.0, False
    base = math.floor(math.floor(math.floor(2 * NIVEL / 5 + 2) * poder * a / dfs) / 50) + 2
    critico = rng.random() < 1 / 24
    valor = base * (1.5 if critico else 1.0)
    valor *= 1.5 if tipo in atacante.tipos else 1.0
    valor *= mult
    valor *= rng.uniform(0.85, 1.0)
    return max(1, int(valor)), mult, critico


def batalha(id_a, id_b, rng=None, campeao=None):
    """Gera os eventos de uma batalha entre os Pokémon id_a e id_b.
    `campeao`: um Lutador já existente (quem venceu a anterior) no lugar de id_a, com o HP que tiver."""
    rng = rng or random.Random()
    a = campeao or Lutador(id_a, rng)
    b = Lutador(id_b, rng)
    a.recarregando = a.carregando = None
    yield {"tipo": "inicio", "a": a, "b": b}
    for turno in range(1, MAX_TURNOS + 1):
        yield {"tipo": "turno", "n": turno}
        a.levou_dano_no_turno = b.levou_dano_no_turno = False
        acoes = []
        for quem, outro in ((a, b), (b, a)):
            golpe = None if quem.recarregando else quem.escolher_golpe(outro, rng)
            prio = golpe[5] if golpe else 0
            acoes.append((prio, quem.spe + rng.random(), quem, outro, golpe))
        acoes.sort(key=lambda x: (-x[0], -x[1]))
        for _, _, quem, outro, golpe in acoes:
            if not quem.vivo or not outro.vivo:
                break
            if quem.recarregando:
                quem.recarregando = False
                yield {"tipo": "recarga", "quem": quem}
                continue
            nome = golpe[0]
            if nome in CARGA and quem.carregando is None:
                quem.carregando = golpe
                yield {"tipo": "carga", "quem": quem, "golpe": nome}
                continue
            quem.carregando = None
            if nome == "Focus Punch" and quem.levou_dano_no_turno:
                yield {"tipo": "falhou", "quem": quem, "golpe": nome, "motivo": "lost focus"}
                continue
            prec = golpe[3]
            if prec is not None and rng.random() * 100 >= prec:
                yield {"tipo": "errou", "quem": quem, "alvo": outro, "golpe": nome}
                continue
            valor, mult, critico = dano(quem, outro, golpe, rng)
            if mult == 0:
                yield {"tipo": "imune", "quem": quem, "alvo": outro, "golpe": nome}
                continue
            outro.hp = max(0, outro.hp - valor)
            outro.levou_dano_no_turno = True
            recuo = 0
            if nome == "Struggle":
                recuo = max(1, quem.hp_max // 4)
            elif nome in RECUO:
                recuo = max(1, int(valor * RECUO[nome]))
            if recuo:
                quem.hp = max(0, quem.hp - recuo)
            if nome in RECARGA:
                quem.recarregando = True
            yield {"tipo": "golpe", "quem": quem, "alvo": outro, "golpe": nome, "tipo_golpe": golpe[1],
                   "dano": valor, "mult": mult, "critico": critico, "recuo": recuo}
            if not outro.vivo:
                yield {"tipo": "desmaiou", "quem": outro}
            if not quem.vivo:
                yield {"tipo": "desmaiou", "quem": quem}
        if not a.vivo or not b.vivo:
            break
    if a.vivo and b.vivo:                        # empate técnico (limite de turnos): quem tem mais HP %
        vencedor, perdedor = (a, b) if a.fracao >= b.fracao else (b, a)
        yield {"tipo": "fim", "vencedor": vencedor, "perdedor": perdedor, "turnos": turno, "tempo": True}
    elif a.vivo or b.vivo:
        vencedor, perdedor = (a, b) if a.vivo else (b, a)
        yield {"tipo": "fim", "vencedor": vencedor, "perdedor": perdedor, "turnos": turno, "tempo": False}
    else:                                        # os dois caíram (recuo do Struggle): sem vencedor
        yield {"tipo": "fim", "vencedor": None, "perdedor": None, "turnos": turno, "tempo": False}


# ---------- placar (persistente) ----------

class Placar:
    """Vitórias/derrotas de cada Pokémon, campeão atual e sequência, recordes. Gravado em JSON."""

    def __init__(self, caminho):
        self.caminho = caminho
        try:
            self.d = json.load(open(caminho))
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("batalhas", 0)
        self.d.setdefault("registro", {})          # id -> [vitórias, derrotas]
        self.d.setdefault("campeao", None)         # {"id", "sequencia"}
        self.d.setdefault("recorde", None)         # {"id", "sequencia"}
        self.d.setdefault("ultimas", [])           # últimas 8: [vencedor, perdedor, turnos]

    def registrar(self, vencedor, perdedor, turnos):
        self.d["batalhas"] += 1
        for lut, i in ((vencedor, 0), (perdedor, 1)):
            if lut is None:
                continue
            r = self.d["registro"].setdefault(str(lut.id), [0, 0])
            r[i] += 1
        if vencedor is None:
            self.d["campeao"] = None
        else:
            c = self.d["campeao"]
            if c and c["id"] == vencedor.id:
                c["sequencia"] += 1
            else:
                self.d["campeao"] = c = {"id": vencedor.id, "sequencia": 1}
            rec = self.d["recorde"]
            if not rec or c["sequencia"] > rec["sequencia"]:
                self.d["recorde"] = {"id": vencedor.id, "sequencia": c["sequencia"]}
        self.d["ultimas"] = ([[vencedor.id if vencedor else None, perdedor.id if perdedor else None, turnos]]
                             + self.d["ultimas"])[:8]
        self.gravar()

    def gravar(self):
        os.makedirs(os.path.dirname(self.caminho), exist_ok=True)
        with open(self.caminho + ".novo", "w") as f:
            json.dump(self.d, f)
        os.replace(self.caminho + ".novo", self.caminho)

    def registro(self, pid):
        return self.d["registro"].get(str(pid), [0, 0])

    def top(self, n=3):
        """Os maiores vencedores: (id, vitórias, derrotas), por vitórias e depois aproveitamento."""
        itens = [(int(k), v[0], v[1]) for k, v in self.d["registro"].items() if v[0]]
        itens.sort(key=lambda x: (-x[1], -(x[1] / (x[1] + x[2]))))
        return itens[:n]


# ---------- teste rápido: python3 batalha.py --simular 2000 ----------

if __name__ == "__main__":
    import sys
    from collections import Counter
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
    rng = random.Random(1)
    ids = list(dados()["pokemon"].keys())
    turnos, vit = [], Counter()
    forte_venceu = 0
    for _ in range(n):
        x, y = rng.sample(ids, 2)
        for ev in batalha(x, y, rng):
            if ev["tipo"] == "fim":
                turnos.append(ev["turnos"])
                if ev["vencedor"]:
                    vit[ev["vencedor"].nome] += 1
                    forte_venceu += ev["vencedor"].total_base >= ev["perdedor"].total_base
    print(f"{n} batalhas: média {sum(turnos)/len(turnos):.1f} turnos (máx {max(turnos)}), "
          f"o de maior soma de status venceu {100*forte_venceu/n:.0f}% das vezes")
    print("mais vitórias:", vit.most_common(6))
    for ev in batalha("6", "9", random.Random(7)):      # Charizard x Blastoise, narrado
        t = ev["tipo"]
        if t == "golpe":
            print(f"  {ev['quem'].nome} used {ev['golpe']}: {ev['dano']} dmg x{ev['mult']}{' CRIT' if ev['critico'] else ''}"
                  f"  -> {ev['alvo'].nome} {ev['alvo'].hp}/{ev['alvo'].hp_max}")
        elif t in ("errou", "carga", "recarga", "falhou", "imune", "desmaiou"):
            print(f"  [{t}] {ev.get('quem').nome} {ev.get('golpe', '')}")
        elif t == "fim":
            print(f"  FIM: {ev['vencedor'] and ev['vencedor'].nome} em {ev['turnos']} turnos")
