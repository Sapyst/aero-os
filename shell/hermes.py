"""
hermes.py — Hermes, the notification daemon of Aero OS (the messenger of the gods).

This file is the CORE: the freedesktop Notifications D-Bus service, the list of notifications, the
rules and the user's settings. It draws nothing (hermes_ui.py does the cards and the dot wave), so it
can be tested on a private D-Bus without a screen.

Behaviour follows the freedesktop Notifications spec 1.2 and many lessons learned by mako
(https://github.com/emersion/mako, MIT License, Copyright (c) 2018 Simon Ser and contributors):
lenient urgency types (Chromium), tag replacement (x-canonical-private-synchronous / x-dunst-stack-tag),
unknown replaces_id = new notification, image-path over app_icon, image-data size math, expire
timeout rules, ActivationToken before ActionInvoked. Unlike mako, Hermes also honours "transient" and
"resident", and only lets an app close its OWN notifications.

Settings: ~/.config/aero/hermes.json (every key optional; see PADRAO). Unknown or broken values fall
back to the default, so a typo never breaks notifications.
"""
import copy
import json
import os
import re
import time
from collections import deque

from gi.repository import Gio, GLib

NOME = "org.freedesktop.Notifications"
CAMINHO = "/org/freedesktop/Notifications"
CONFIG = os.path.expanduser("~/.config/aero/hermes.json")
HISTORICO = os.path.expanduser("~/.local/state/aero/hermes-history.json")

XML = """
<node>
  <interface name="com.aero.Hermes">
    <method name="History"><arg type="s" direction="out"/></method>
    <method name="Modes"><arg type="as" direction="out"/></method>
    <method name="SetModes"><arg type="as" name="modes" direction="in"/></method>
    <method name="DismissAll"/>
    <method name="Reload"/>
  </interface>
  <interface name="org.freedesktop.Notifications">
    <method name="GetCapabilities"><arg type="as" direction="out"/></method>
    <method name="Notify">
      <arg type="s" name="app_name" direction="in"/><arg type="u" name="replaces_id" direction="in"/>
      <arg type="s" name="app_icon" direction="in"/><arg type="s" name="summary" direction="in"/>
      <arg type="s" name="body" direction="in"/><arg type="as" name="actions" direction="in"/>
      <arg type="a{sv}" name="hints" direction="in"/><arg type="i" name="expire_timeout" direction="in"/>
      <arg type="u" direction="out"/>
    </method>
    <method name="CloseNotification"><arg type="u" name="id" direction="in"/></method>
    <method name="GetServerInformation">
      <arg type="s" direction="out"/><arg type="s" direction="out"/>
      <arg type="s" direction="out"/><arg type="s" direction="out"/>
    </method>
    <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
    <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
    <signal name="ActivationToken"><arg type="u"/><arg type="s"/></signal>
  </interface>
</node>
"""

# Close reasons (spec)
EXPIROU, DISPENSADA, PEDIDO, INDEFINIDO = 1, 2, 3, 4
URGENCIAS = {0: "low", 1: "normal", 2: "critical"}

# ---------- settings ----------
# Every key can be changed by the user. Positions: "follow-central" uses the quick center's side.
PADRAO = {
    "style": "dotstyle",                 # dotstyle | aeroterm | aerocloud
    "position": "follow-bell",           # follow-bell (under the bar's bell) | follow-central | top-left |
                                         # top-center | top-right |
                                         # bottom-left | bottom-center | bottom-right
    "margin": 12, "width": 340, "gap": 8, "max_visible": 4,
    "timeout": {"low": 3000, "normal": 5000, "critical": 0},       # ms; 0 = stays until closed
    "history": {"size": 100},
    "dnd": {"enabled": False, "allow_critical": True},
    "fullscreen": "hold",                # the CARD while a fullscreen app has focus: hold (keep it for
                                         # later, quietly) | show. The wave is separate, see wave.fullscreen
    "wave": {
        "enabled": True,
        "origin": "notification",        # notification (where the card appears) | screen-center | off-card
        "color": "auto",                 # auto = the style's text color, or "#rrggbb"
        "urgent_color": "#e8312a",
        "duration_ms": 1400,
        "dot_spacing": 22,               # px between dots
        "dot_size": 0.3,                 # fraction of the spacing
        "reach": 0.55,                   # how far it travels, fraction of the screen diagonal
        "intensity": 0.55,               # 0..1, how bright the ring is (subtle by default)
        "width": 0.11,                   # thickness of the ring, fraction of the reach
        "reduced_motion": "auto",        # auto (follow the system) | on | off
        "fullscreen": "faded",           # over a fullscreen app (game, video): faded | normal | off
        "fullscreen_intensity": 0.22,    # how bright the faded wave is
    },
    "colors": {"background": "#0b0b0c", "text": "#ecebe8", "dim": "#8c8b88", "accent": "#e8312a"},
    "colors_light": {"background": "#f4f4f1", "text": "#151516", "dim": "#6b6a67", "accent": "#d42a22"},
    "font": {"title": "Doto", "body": "Geist", "mono": "Geist Mono"},
    "radius": 22,
    "icons": True,
    "rules": [],                         # [{"match": {...}, "set": {...}}], see aplicar_regras()
    "steam": "wave",                     # wave (wave where Steam's own popup appears) | move | off
}
POSICOES = {"follow-bell", "follow-central", "top-left", "top-center", "top-right", "bottom-left", "bottom-center",
            "bottom-right"}
COR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _num(v, padrao, minimo, maximo, inteiro=True):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return padrao
    v = int(v) if inteiro else float(v)
    return min(maximo, max(minimo, v))


def validar_config(bruto):
    """User's JSON -> complete, safe settings. Never raises: bad values fall back to the default."""
    c = copy.deepcopy(PADRAO)
    if not isinstance(bruto, dict):
        return c
    if bruto.get("style") in ("dotstyle", "aeroterm", "aerocloud"):
        c["style"] = bruto["style"]
    if bruto.get("position") in POSICOES:
        c["position"] = bruto["position"]
    c["margin"] = _num(bruto.get("margin"), c["margin"], 0, 400)
    c["width"] = _num(bruto.get("width"), c["width"], 200, 900)
    c["gap"] = _num(bruto.get("gap"), c["gap"], 0, 60)
    c["max_visible"] = _num(bruto.get("max_visible"), c["max_visible"], 1, 12)
    c["radius"] = _num(bruto.get("radius"), c["radius"], 0, 60)
    if isinstance(bruto.get("icons"), bool):
        c["icons"] = bruto["icons"]
    t = bruto.get("timeout")
    if isinstance(t, dict):
        for k in ("low", "normal", "critical"):
            c["timeout"][k] = _num(t.get(k), c["timeout"][k], 0, 3_600_000)
    h = bruto.get("history")
    if isinstance(h, dict):
        c["history"]["size"] = _num(h.get("size"), c["history"]["size"], 0, 1000)
    d = bruto.get("dnd")
    if isinstance(d, dict):
        for k in ("enabled", "allow_critical"):
            if isinstance(d.get(k), bool):
                c["dnd"][k] = d[k]
    if bruto.get("fullscreen") in ("hold", "show"):
        c["fullscreen"] = bruto["fullscreen"]
    w = bruto.get("wave")
    if isinstance(w, dict):
        cw = c["wave"]
        if isinstance(w.get("enabled"), bool):
            cw["enabled"] = w["enabled"]
        if w.get("origin") in ("notification", "screen-center", "off-card"):
            cw["origin"] = w["origin"]
        if w.get("color") == "auto" or (isinstance(w.get("color"), str) and COR_RE.match(w["color"])):
            cw["color"] = w["color"]
        if isinstance(w.get("urgent_color"), str) and COR_RE.match(w["urgent_color"]):
            cw["urgent_color"] = w["urgent_color"]
        cw["duration_ms"] = _num(w.get("duration_ms"), cw["duration_ms"], 200, 6000)
        cw["dot_spacing"] = _num(w.get("dot_spacing"), cw["dot_spacing"], 8, 80)
        cw["dot_size"] = _num(w.get("dot_size"), cw["dot_size"], 0.05, 0.5, False)
        cw["reach"] = _num(w.get("reach"), cw["reach"], 0.05, 1.5, False)
        cw["intensity"] = _num(w.get("intensity"), cw["intensity"], 0.05, 1.0, False)
        cw["width"] = _num(w.get("width"), cw["width"], 0.02, 0.5, False)
        if w.get("reduced_motion") in ("auto", "on", "off"):
            cw["reduced_motion"] = w["reduced_motion"]
        if w.get("fullscreen") in ("faded", "normal", "off"):
            cw["fullscreen"] = w["fullscreen"]
        cw["fullscreen_intensity"] = _num(w.get("fullscreen_intensity"), cw["fullscreen_intensity"], 0.02, 1.0, False)
    for grupo in ("colors", "colors_light"):
        cores = bruto.get(grupo)
        if isinstance(cores, dict):
            for k in c[grupo]:
                if isinstance(cores.get(k), str) and COR_RE.match(cores[k]):
                    c[grupo][k] = cores[k]
    fonte = bruto.get("font")
    if isinstance(fonte, dict):
        for k in c["font"]:
            if isinstance(fonte.get(k), str) and 0 < len(fonte[k]) <= 64 and re.match(r"^[\w .-]+$", fonte[k]):
                c["font"][k] = fonte[k]
    if bruto.get("steam") in ("wave", "move", "off"):
        c["steam"] = bruto["steam"]
    regras = bruto.get("rules")
    if isinstance(regras, list):
        c["rules"] = [r for r in (validar_regra(x) for x in regras[:100]) if r]
    return c


CAMPOS_MATCH = {"app", "summary", "body", "category", "urgency", "desktop_entry"}
CAMPOS_SET = {"hide", "wave", "timeout", "history", "urgency", "sound"}


def validar_regra(r):
    """{"match": {"app": "Steam", "summary": "regex", "urgency": "critical", ...},
        "set": {"hide": bool, "wave": bool, "timeout": ms, "history": bool, "urgency": "low|normal|critical"}}
    Text matches are case-insensitive regular expressions."""
    if not isinstance(r, dict) or not isinstance(r.get("match"), dict) or not isinstance(r.get("set"), dict):
        return None
    m = {}
    for k, v in r["match"].items():
        if k not in CAMPOS_MATCH or not isinstance(v, str) or len(v) > 200:
            return None
        if k == "urgency":
            if v not in ("low", "normal", "critical"):
                return None
            m[k] = v
        else:
            try:
                m[k] = re.compile(v, re.I)
            except re.error:
                return None
    s = {}
    for k, v in r["set"].items():
        if k not in CAMPOS_SET:
            return None
        if k in ("hide", "wave", "history", "sound") and isinstance(v, bool):
            s[k] = v
        elif k == "timeout" and isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 3_600_000:
            s[k] = v
        elif k == "urgency" and v in ("low", "normal", "critical"):
            s[k] = v
        else:
            return None
    return {"match": m, "set": s} if m or s else None


def ler_config(caminho=CONFIG):
    try:
        with open(caminho, encoding="utf-8") as f:
            return validar_config(json.load(f))
    except (OSError, ValueError):
        return validar_config({})


# ---------- text ----------

PERMITIDAS = {"b", "i", "u"}


def limpar_markup(texto, limite=2000):
    """Body text -> safe Pango markup. Keeps <b> <i> <u> (what the spec allows and we render);
    links and images become plain text; everything else is escaped. Bad nesting is closed properly."""
    if not isinstance(texto, str):
        return ""
    texto = texto[:limite]
    saida, abertas = [], []
    pos = 0
    for m in re.finditer(r"<\s*(/?)\s*([a-zA-Z]+)[^>]*>|&(#?\w+);", texto):
        saida.append(GLib.markup_escape_text(texto[pos:m.start()]))
        pos = m.end()
        if m.group(3) is not None:                     # an entity: keep the common ones, escape others
            ent = m.group(3)
            if ent in ("amp", "lt", "gt", "quot", "apos") or re.match(r"^#(\d{1,7}|x[0-9a-fA-F]{1,6})$", ent):
                saida.append(f"&{ent};")
            else:
                saida.append(GLib.markup_escape_text(m.group(0)))
            continue
        fecha, tag = m.group(1) == "/", m.group(2).lower()
        if tag == "br":
            saida.append("\n")
        elif tag in PERMITIDAS:
            if not fecha:
                saida.append(f"<{tag}>")
                abertas.append(tag)
            elif tag in abertas:
                while abertas:                          # close everything opened after it, then it
                    t = abertas.pop()
                    saida.append(f"</{t}>")
                    if t == tag:
                        break
        # <a>, <img> and unknown tags: dropped (their inner text stays)
    saida.append(GLib.markup_escape_text(texto[pos:]))
    saida.extend(f"</{t}>" for t in reversed(abertas))
    return "".join(saida)


def markup_para_texto(markup):
    """Safe markup -> plain text (the history keeps what you read, without tags or &amp;)."""
    try:
        from gi.repository import Pango
        ok, _attrs, texto, _accel = Pango.parse_markup(markup, -1, "\0")
        return texto if ok else markup
    except Exception:  # noqa: BLE001
        return markup


def texto_puro(texto, limite=500):
    """Summary / app name: plain text, no control characters."""
    if not isinstance(texto, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", texto)[:limite]


# ---------- one notification ----------

MAX_IMAGEM = 16 * 1024 * 1024


def ler_imagem(v):
    """image-data hint (iiibiiay) -> dict or None. Rejects broken or huge images."""
    try:
        w, h, stride, alfa, bits, canais, dados = v
    except (TypeError, ValueError):
        return None
    if not (0 < w <= 4096 and 0 < h <= 4096 and bits == 8 and canais in (3, 4) and stride >= w * canais):
        return None
    if bool(alfa) != (canais == 4):
        return None
    precisa = stride * (h - 1) + w * canais         # the last row may have no padding (as in mako)
    dados = bytes(dados)
    if len(dados) < precisa or len(dados) > MAX_IMAGEM:
        return None
    return {"width": w, "height": h, "rowstride": stride, "alpha": bool(alfa), "data": dados}


class Notificacao:
    __slots__ = ("id", "app", "icone", "titulo", "corpo", "acoes", "urgencia", "categoria", "entrada",
                 "valor", "imagem", "etiqueta", "transitoria", "residente", "som", "timeout", "remetente",
                 "criada", "escondida", "onda", "guardar", "pendente")

    def __init__(self, id_):
        self.id = id_
        self.criada = time.time()
        self.acoes, self.imagem, self.etiqueta, self.valor = [], None, None, None
        self.escondida, self.onda, self.guardar, self.pendente = False, True, True, False

    def resumo(self):
        """What the history and the quick center see (no image bytes)."""
        return {"id": self.id, "app": self.app, "summary": self.titulo, "body": markup_para_texto(self.corpo),
                "urgency": URGENCIAS[self.urgencia], "category": self.categoria, "icon": self.icone,
                "desktop_entry": self.entrada, "time": round(self.criada, 3),
                "actions": [[k, t] for k, t in self.acoes]}


def aplicar_regras(n, regras):
    """User rules, in order; later rules win. Changes hide/wave/timeout/history/urgency of n."""
    alvo = {"app": n.app, "summary": n.titulo, "body": n.corpo, "category": n.categoria or "",
            "desktop_entry": n.entrada or "", "urgency": URGENCIAS[n.urgencia]}
    for r in regras:
        ok = True
        for k, v in r["match"].items():
            if k == "urgency":
                ok = alvo["urgency"] == v
            else:
                ok = bool(v.search(alvo[k] or ""))
            if not ok:
                break
        if not ok:
            continue
        s = r["set"]
        if "hide" in s:
            n.escondida = s["hide"]
        if "wave" in s:
            n.onda = s["wave"]
        if "timeout" in s:
            n.timeout = s["timeout"]
        if "history" in s:
            n.guardar = s["history"]
        if "urgency" in s:
            n.urgencia = {"low": 0, "normal": 1, "critical": 2}[s["urgency"]]
            alvo["urgency"] = s["urgency"]


# ---------- the service ----------

class Hermes:
    """The notification list + the D-Bus service. The UI connects with ouvir(callback):
    callback(event, notification) where event is "show", "update" or "close"."""

    LIMITE_POR_SEG = 20              # per app; more than this in one second is dropped (spam)

    def __init__(self, config=None, agendar=None, cancelar=None):
        self.config = config or ler_config()
        self.agendar = agendar or (lambda ms, f: GLib.timeout_add(ms, lambda: (f(), False)[1]))
        self.cancelar = cancelar or GLib.source_remove
        self.ativas = {}                     # id -> Notificacao
        self.timers = {}
        self.historico = deque(maxlen=max(1, self.config["history"]["size"]))
        self.proximo = 1
        self.ouvintes = []
        self.conexao = None
        self.registro = None
        self.dono = None
        self.taxa = {}                       # sender -> (second, count)
        self.modos = set(["dnd"] if self.config["dnd"]["enabled"] else [])
        self.segurar = lambda n: False       # UI sets: True = the card is held (fullscreen); its timer
                                             # starts only when the card really shows (armar)

    # --- plumbing ---

    def ouvir(self, f):
        self.ouvintes.append(f)

    def _avisar(self, evento, n):
        for f in list(self.ouvintes):
            try:
                f(evento, n)
            except Exception as erro:  # noqa: BLE001 — one broken listener must not stop the others
                print(f"hermes: listener failed: {erro!r}", flush=True)

    def capacidades(self):
        return ["body", "body-markup", "actions", "icon-static", "persistence", "x-canonical-private-synchronous",
                "x-dunst-stack-tag", "x-aero-wave"]

    # --- Notify ---

    def notificar(self, app, substitui, icone, titulo, corpo, acoes, dicas, expira, remetente=""):
        agora = int(time.time())
        seg, conta = self.taxa.get(remetente, (agora, 0))
        conta = conta + 1 if seg == agora else 1
        self.taxa[remetente] = (agora, conta)
        if len(self.taxa) > 500:
            self.taxa.clear()
        if conta > self.LIMITE_POR_SEG:
            return 0                                     # dropped: an app flooding the screen

        n = self.ativas.get(substitui) if substitui else None
        nova = n is None or (n.remetente and remetente and n.remetente != remetente)
        if nova:
            n = Notificacao(self.proximo)
            self.proximo = self.proximo + 1 if self.proximo < 2 ** 32 - 1 else 1
        n.app = texto_puro(app, 120) or "Unknown"
        n.icone = texto_puro(icone, 500)
        n.titulo = texto_puro(titulo, 500)
        n.corpo = limpar_markup(corpo)
        n.acoes = []
        if isinstance(acoes, (list, tuple)):
            for i in range(0, min(len(acoes), 20) - 1, 2):
                n.acoes.append((texto_puro(acoes[i], 64), texto_puro(acoes[i + 1], 64)))
        n.urgencia, n.categoria, n.entrada, n.valor = 1, "", "", None
        n.imagem, n.etiqueta, n.transitoria, n.residente, n.som = None, None, False, False, None
        dicas = dicas if isinstance(dicas, dict) else {}
        for chave, v in dicas.items():
            if chave == "urgency" and isinstance(v, int) and not isinstance(v, bool):
                n.urgencia = v if v in (0, 1, 2) else 1         # byte, uint32 or int32 (Chromium)
            elif chave == "category" and isinstance(v, str):
                n.categoria = texto_puro(v, 64)
            elif chave == "desktop-entry" and isinstance(v, str):
                n.entrada = texto_puro(v, 128)
            elif chave == "value" and isinstance(v, int) and not isinstance(v, bool):
                n.valor = max(0, min(100, v))
            elif chave in ("image-path", "image_path") and isinstance(v, str):
                n.icone = texto_puro(v, 500)                     # wins over app_icon
            elif chave in ("image-data", "image_data", "icon_data"):
                img = ler_imagem(v)
                if img:
                    n.imagem = img                               # wins over any icon path
            elif chave in ("x-canonical-private-synchronous", "x-dunst-stack-tag") and isinstance(v, str):
                n.etiqueta = texto_puro(v, 128)
            elif chave == "transient":
                n.transitoria = bool(v)
            elif chave == "resident":
                n.residente = bool(v)
            elif chave == "sound-file" and isinstance(v, str):
                n.som = texto_puro(v, 500)
        n.remetente = remetente
        n.escondida, n.onda, n.guardar = False, True, not n.transitoria
        tempo_padrao = self.config["timeout"][URGENCIAS[n.urgencia]]
        n.timeout = tempo_padrao if not isinstance(expira, int) or expira < 0 else expira
        aplicar_regras(n, self.config["rules"])

        # same tag from the same app replaces the older one in place (volume, now playing...)
        if nova and n.etiqueta:
            for velha in list(self.ativas.values()):
                if velha.etiqueta == n.etiqueta and velha.app == n.app:
                    self._parar_timer(velha.id)
                    del self.ativas[velha.id]
                    n.id = velha.id
                    nova = False
                    break

        if "dnd" in self.modos and not (n.urgencia == 2 and self.config["dnd"]["allow_critical"]):
            n.escondida = True
            n.onda = False
        self.ativas[n.id] = n
        if n.guardar and not n.transitoria:
            self._guardar(n)
        self._parar_timer(n.id)
        n.pendente = False
        if n.timeout > 0 and not n.escondida:
            if self.segurar(n):
                n.pendente = True
            else:
                self.timers[n.id] = self.agendar(n.timeout, lambda i=n.id: self.fechar(i, EXPIROU))
        if n.escondida:
            # hidden by DND or a rule: kept in the history, never drawn; closed right away for the app
            del self.ativas[n.id]
            self._sinal("NotificationClosed", GLib.Variant("(uu)", (n.id, DISPENSADA)))
        else:
            self._avisar("show" if nova else "update", n)
        return n.id

    def armar(self, id_):
        """A held card is on screen now: its timeout starts counting from here."""
        n = self.ativas.get(id_)
        if n is not None and n.pendente:
            n.pendente = False
            self.timers[id_] = self.agendar(n.timeout, lambda: self.fechar(id_, EXPIROU))

    def historico_mako(self):
        """History in makoctl's JSON shape (newest first), so old callers keep working."""
        saida = []
        for x in reversed(self.historico):
            saida.append({"id": x["id"], "app_name": x["app"], "app_icon": x["icon"] or None,
                          "category": x["category"] or None, "desktop_entry": x["desktop_entry"] or None,
                          "summary": x["summary"], "body": x["body"], "urgency": x["urgency"],
                          "actions": {k: t for k, t in x["actions"]}})
        return saida

    def recarregar(self, config=None):
        self.config = config or ler_config()
        self.historico = deque(self.historico, maxlen=max(1, self.config["history"]["size"]))
        self._avisar("config", None)

    def _guardar(self, n):
        self.historico = deque((x for x in self.historico if x["id"] != n.id), maxlen=self.historico.maxlen)
        if self.config["history"]["size"]:
            self.historico.append(n.resumo())

    def _parar_timer(self, id_):
        t = self.timers.pop(id_, None)
        if t is not None:
            try:
                self.cancelar(t)
            except Exception:  # noqa: BLE001
                pass

    # --- closing and actions ---

    def fechar(self, id_, motivo=DISPENSADA, remetente=None):
        """remetente set = a D-Bus CloseNotification: only the app that created it may close it."""
        n = self.ativas.get(id_)
        if n is None:
            return False
        if remetente is not None and n.remetente and remetente != n.remetente:
            return False
        self._parar_timer(id_)
        del self.ativas[id_]
        self._avisar("close", n)
        self._sinal("NotificationClosed", GLib.Variant("(uu)", (id_, motivo)))
        return True

    def acionar(self, id_, chave="default", token=""):
        """The person clicked the card (default) or a button. Resident notifications stay."""
        n = self.ativas.get(id_)
        if n is None or (chave != "default" and chave not in [k for k, _ in n.acoes]):
            return False
        if token:
            self._sinal("ActivationToken", GLib.Variant("(us)", (id_, token)), destino=n.remetente)
        self._sinal("ActionInvoked", GLib.Variant("(us)", (id_, chave)), destino=n.remetente)
        if not n.residente:
            self.fechar(id_, DISPENSADA)
        return True

    def dispensar_tudo(self):
        for id_ in list(self.ativas):
            self.fechar(id_, DISPENSADA)

    def modo(self, nome, ligado):
        (self.modos.add if ligado else self.modos.discard)(nome)
        self._avisar("modes", None)

    def definir_modos(self, modos):
        self.modos = {m for m in modos if isinstance(m, str) and re.match(r"^[\w-]{1,32}$", m)}
        self._avisar("modes", None)

    # --- D-Bus ---

    def _sinal(self, nome, params, destino=None):
        if self.conexao is None:
            return
        try:
            self.conexao.emit_signal(destino or None, CAMINHO, NOME, nome, params)
        except GLib.Error:
            pass

    def publicar(self, conexao, ao_ganhar=None, ao_perder=None):
        """Registers the object and asks for org.freedesktop.Notifications (fails quietly if another
        daemon, like mako, has it). Only once it is ours, com.aero.Hermes is taken too: that second
        name is how hermesctl knows Hermes is the one in charge."""
        self.conexao = conexao
        no = Gio.DBusNodeInfo.new_for_xml(XML)
        self.registro = conexao.register_object(CAMINHO, no.lookup_interface(NOME), self._chamada, None, None)
        self.registro_ctl = conexao.register_object(CAMINHO, no.lookup_interface("com.aero.Hermes"),
                                                    self._controle, None, None)

        def ganhou(*_):
            self.dono_ctl = Gio.bus_own_name_on_connection(conexao, "com.aero.Hermes",
                                                           Gio.BusNameOwnerFlags.NONE, None, None)
            if ao_ganhar:
                ao_ganhar()

        def perdeu(*_):
            if ao_perder:
                ao_perder()
        self.dono_ctl = None
        self.dono = Gio.bus_own_name_on_connection(conexao, NOME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                                                   ganhou, perdeu)
        return self.dono

    def _controle(self, conexao, remetente, _caminho, _iface, metodo, params, invocacao):
        """hermesctl. Only processes of THIS user reach the session bus, but changing modes/settings
        or clearing is still limited to the same uid as the daemon."""
        try:
            if metodo == "History":
                invocacao.return_value(GLib.Variant("(s)", (json.dumps(self.historico_mako()),)))
            elif metodo == "Modes":
                invocacao.return_value(GLib.Variant("(as)", (sorted(self.modos),)))
            elif metodo == "SetModes":
                (modos,) = params.unpack()
                self.definir_modos(modos[:20])
                invocacao.return_value(None)
            elif metodo == "DismissAll":
                self.dispensar_tudo()
                invocacao.return_value(None)
            elif metodo == "Reload":
                self.recarregar()
                invocacao.return_value(None)
            else:
                invocacao.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", metodo)
        except Exception as erro:  # noqa: BLE001
            invocacao.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(erro)[:200])

    def _chamada(self, _con, remetente, _caminho, _iface, metodo, params, invocacao):
        try:
            if metodo == "GetCapabilities":
                invocacao.return_value(GLib.Variant("(as)", (self.capacidades(),)))
            elif metodo == "GetServerInformation":
                invocacao.return_value(GLib.Variant("(ssss)", ("Hermes", "Aero OS", "1.0", "1.2")))
            elif metodo == "Notify":
                app, sub, icone, titulo, corpo, acoes, dicas, expira = params.unpack()
                id_ = self.notificar(app, sub, icone, titulo, corpo, acoes, dicas, expira, remetente)
                invocacao.return_value(GLib.Variant("(u)", (id_,)))
            elif metodo == "CloseNotification":
                (id_,) = params.unpack()
                self.fechar(id_, PEDIDO, remetente=remetente)
                invocacao.return_value(None)
            else:
                invocacao.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", metodo)
        except Exception as erro:  # noqa: BLE001 — a bad message must never kill the daemon
            invocacao.return_dbus_error("org.freedesktop.DBus.Error.Failed", str(erro)[:200])
