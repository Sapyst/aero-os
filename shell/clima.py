"""
clima.py — o clima de agora, pro Super Menu e pro Settings (doc 37).

De onde vem:
  - Onde você está: automático (pela conexão de internet, ipinfo.io) ou a cidade escolhida em
    Settings → Location (busca de cidades do Open-Meteo).
  - O clima: Open-Meteo (open-meteo.com) — grátis e sem cadastro/chave.
  - Fica guardado em ~/.cache/aero-shell/clima.json por 15 min (abrir o menu não espera a internet).

O desenho do clima (sol, lua, nuvem, chuva, neve, raio, névoa) é feito aqui mesmo, com formas
simples e arredondadas (cairo), no estilo do resto do sistema: nada de emoji.

Configuração (painel.conf):
  CLIMA_AUTO=1|0   CLIMA_LAT=..  CLIMA_LON=..  CLIMA_NOME="New York"   CLIMA_UNIDADE=c|f
"""
import json
import math
import os
import time
import urllib.parse

PAINEL = os.path.expanduser("~/.config/hypr/painel.conf")
CACHE = os.path.expanduser("~/.cache/aero-shell/clima.json")
VALIDADE = 15 * 60            # clima: 15 minutos
VALIDADE_LUGAR = 6 * 3600     # local automático: 6 horas

# Códigos do tempo (padrão WMO, o que o Open-Meteo usa) -> (texto, desenho)
CODIGOS = {
    0: ("Clear sky", "limpo"), 1: ("Mostly clear", "poucas-nuvens"), 2: ("Partly cloudy", "poucas-nuvens"),
    3: ("Overcast", "nublado"), 45: ("Fog", "nevoa"), 48: ("Freezing fog", "nevoa"),
    51: ("Light drizzle", "garoa"), 53: ("Drizzle", "garoa"), 55: ("Heavy drizzle", "garoa"),
    56: ("Freezing drizzle", "congelada"), 57: ("Freezing drizzle", "congelada"),
    61: ("Light rain", "chuva"), 63: ("Rain", "chuva"), 65: ("Heavy rain", "chuva-forte"),
    66: ("Freezing rain", "congelada"), 67: ("Freezing rain", "congelada"),
    71: ("Light snow", "neve"), 73: ("Snow", "neve"), 75: ("Heavy snow", "neve"), 77: ("Snow grains", "neve"),
    80: ("Light showers", "chuva"), 81: ("Showers", "chuva"), 82: ("Heavy showers", "chuva-forte"),
    85: ("Snow showers", "neve"), 86: ("Heavy snow showers", "neve"),
    95: ("Thunderstorm", "trovoada"), 96: ("Thunderstorm with hail", "trovoada"),
    97: ("Thunderstorm with hail", "trovoada"), 99: ("Thunderstorm with hail", "trovoada"),
}


# ---------- configuração ----------

def ler_config():
    cfg = {"CLIMA_AUTO": "1", "CLIMA_UNIDADE": "c"}
    try:
        for linha in open(PAINEL):
            if linha.startswith("CLIMA_") and "=" in linha:
                k, v = linha.strip().split("=", 1)
                cfg[k] = v.strip('"')
    except OSError:
        pass
    return cfg


def limpo(texto):
    """O painel.conf é lido pelo bash ("source"): nada de aspas, $, crase ou barra no valor."""
    return "".join(c for c in texto if c not in '"$`\\\n\r\u2028\u2029\x85' and c >= " ")


BAIXAR_FORA = False      # True: baixa num curl à parte (os widgets: sem ssl/http no processo, ~8 MB a menos)


def baixar(url, tempo=6):
    if BAIXAR_FORA:
        import shutil
        import subprocess
        if shutil.which("curl"):
            try:
                r = subprocess.run(["curl", "-fsSL", "--max-time", str(tempo), "-A", "aero-shell", url],
                                   capture_output=True, timeout=tempo + 2)
            except subprocess.SubprocessError as erro:
                raise OSError(str(erro)) from erro
            if r.returncode != 0:
                raise OSError(f"curl {r.returncode}")
            return json.loads(r.stdout.decode())
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "aero-shell"})
    with urllib.request.urlopen(req, timeout=tempo) as r:
        return json.loads(r.read().decode())


def ler_cache():
    try:
        return json.load(open(CACHE))
    except (OSError, ValueError):
        return {}


def gravar_cache(dados):
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    with open(CACHE + ".novo", "w") as f:
        json.dump(dados, f)
    os.replace(CACHE + ".novo", CACHE)


# ---------- onde ----------

def lugar(cfg, cache):
    """(lat, lon, nome). Automático = pela conexão (guardado por 6 h); senão a cidade escolhida."""
    if cfg.get("CLIMA_AUTO", "1") != "1" and cfg.get("CLIMA_LAT") and cfg.get("CLIMA_LON"):
        return float(cfg["CLIMA_LAT"]), float(cfg["CLIMA_LON"]), cfg.get("CLIMA_NOME", "")
    auto = cache.get("auto")
    if auto and time.time() - auto.get("quando", 0) < VALIDADE_LUGAR:
        return auto["lat"], auto["lon"], auto["nome"]
    info = baixar("https://ipinfo.io/json")
    lat, lon = (float(x) for x in info["loc"].split(","))
    cache["auto"] = {"lat": lat, "lon": lon, "nome": info.get("city", ""), "quando": time.time()}
    return lat, lon, cache["auto"]["nome"]


def buscar_cidades(texto):
    """Pra caixa de busca do Settings: [(nome bonito, lat, lon), ...]"""
    url = ("https://geocoding-api.open-meteo.com/v1/search?count=8&language=en&format=json&name="
           + urllib.parse.quote(texto))
    r = baixar(url)
    saida = []
    for c in r.get("results") or []:
        partes = [c.get("name"), c.get("admin1"), c.get("country")]
        saida.append((", ".join(p for p in partes if p), c["latitude"], c["longitude"]))
    return saida


# ---------- o clima ----------

def obter(forcar=False):
    """O clima de agora (dicionário) — do cache se for recente. Pode demorar (internet): use numa thread.
    Sem internet: devolve o último que deu certo (ou None)."""
    cfg = ler_config()
    cache = ler_cache()
    chave = f"{cfg.get('CLIMA_AUTO')}|{cfg.get('CLIMA_LAT')}|{cfg.get('CLIMA_LON')}|{cfg.get('CLIMA_UNIDADE')}"
    atual = cache.get("clima")
    if not forcar and atual and atual.get("chave") == chave and time.time() - atual.get("quando", 0) < VALIDADE:
        return atual
    try:
        lat, lon, nome = lugar(cfg, cache)
        unidade = "fahrenheit" if cfg.get("CLIMA_UNIDADE") == "f" else "celsius"
        url = ("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 1,
            "temperature_unit": unidade, "wind_speed_unit": "kmh",
            "current": "temperature_2m,apparent_temperature,weather_code,is_day,precipitation,"
                       "relative_humidity_2m,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max"}))
        r = baixar(url)
        c, d = r["current"], r["daily"]
        texto, desenho = CODIGOS.get(c["weather_code"], ("Unknown", "nublado"))
        atual = {"chave": chave, "quando": time.time(), "lugar": nome,
                 "temp": round(c["temperature_2m"]), "sensacao": round(c["apparent_temperature"]),
                 "codigo": c["weather_code"], "texto": texto, "desenho": desenho, "dia": bool(c["is_day"]),
                 "umidade": c.get("relative_humidity_2m"), "vento": round(c.get("wind_speed_10m") or 0),
                 "max": round(d["temperature_2m_max"][0]), "min": round(d["temperature_2m_min"][0]),
                 "chance_chuva": (d.get("precipitation_probability_max") or [None])[0]}
        cache["clima"] = atual
        gravar_cache(cache)
        return atual
    except (OSError, ValueError, KeyError, TypeError):
        return cache.get("clima")


def obter_semana(forcar=False, dias=7):
    """Previsão de `dias` dias (pro widget Weather): lista de dicionários, um por dia, do de hoje em diante.
    Mesmo lugar/unidade do Settings → Location; cache de 30 min em clima.json (chave "semana")."""
    cfg = ler_config()
    cache = ler_cache()
    chave = f"{cfg.get('CLIMA_AUTO')}|{cfg.get('CLIMA_LAT')}|{cfg.get('CLIMA_LON')}|{cfg.get('CLIMA_UNIDADE')}|{dias}"
    atual = cache.get("semana")
    if not forcar and atual and atual.get("chave") == chave and time.time() - atual.get("quando", 0) < 30 * 60:
        return atual
    try:
        lat, lon, nome = lugar(cfg, cache)
        unidade = "fahrenheit" if cfg.get("CLIMA_UNIDADE") == "f" else "celsius"
        r = baixar("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": dias,
            "temperature_unit": unidade, "wind_speed_unit": "kmh",
            "current": "temperature_2m,apparent_temperature,weather_code,is_day,relative_humidity_2m,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "sunrise,sunset,wind_speed_10m_max"}))
        c, d = r["current"], r["daily"]
        lista = []
        for i, data in enumerate(d["time"]):
            texto, desenho = CODIGOS.get(d["weather_code"][i], ("Unknown", "nublado"))
            lista.append({"data": data, "codigo": d["weather_code"][i], "texto": texto, "desenho": desenho,
                          "max": round(d["temperature_2m_max"][i]), "min": round(d["temperature_2m_min"][i]),
                          "chance_chuva": (d.get("precipitation_probability_max") or [None] * 99)[i],
                          "vento": round((d.get("wind_speed_10m_max") or [0] * 99)[i] or 0),
                          "nascer": (d.get("sunrise") or [""] * 99)[i][-5:], "por": (d.get("sunset") or [""] * 99)[i][-5:]})
        texto, desenho = CODIGOS.get(c["weather_code"], ("Unknown", "nublado"))
        atual = {"chave": chave, "quando": time.time(), "lugar": nome, "dias": lista,
                 "agora": {"temp": round(c["temperature_2m"]), "sensacao": round(c["apparent_temperature"]),
                           "texto": texto, "desenho": desenho, "dia": bool(c["is_day"]),
                           "umidade": c.get("relative_humidity_2m"), "vento": round(c.get("wind_speed_10m") or 0)},
                 "unidade": "°F" if unidade == "fahrenheit" else "°C"}
        cache["semana"] = atual
        gravar_cache(cache)
        return atual
    except (OSError, ValueError, KeyError, TypeError, IndexError):
        return cache.get("semana")


# ---------- o desenho ----------
# Tudo num quadrado de lado "s" (o tamanho do ícone). Cores suaves, pensadas pra ficar sobre a foto
# escurecida da faixa e também sobre o fundo claro/escuro do Settings.

AMARELO = (1.00, 0.80, 0.30)
LUA = (0.97, 0.93, 0.78)
NUVEM = (1.00, 1.00, 1.00)
NUVEM_ATRAS = (0.80, 0.84, 0.90)
NUVEM_ESCURA = (0.62, 0.66, 0.74)
GOTA = (0.55, 0.78, 1.00)
RAIO = (1.00, 0.78, 0.20)


def _sol(cr, cx, cy, r):
    cr.set_source_rgb(*AMARELO)
    cr.set_line_width(r * 0.24)
    cr.set_line_cap(1)                           # pontas redondas
    for i in range(8):
        a = i * math.pi / 4
        cr.move_to(cx + math.cos(a) * r * 1.38, cy + math.sin(a) * r * 1.38)
        cr.line_to(cx + math.cos(a) * r * 1.78, cy + math.sin(a) * r * 1.78)
        cr.stroke()
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.fill()


def _lua(cr, cx, cy, r):
    cr.push_group()
    cr.set_source_rgb(*LUA)
    cr.arc(cx, cy, r, 0, 2 * math.pi)
    cr.fill()
    cr.set_operator(0)                           # CLEAR: "morde" a lua e vira meia-lua
    cr.arc(cx + r * 0.55, cy - r * 0.35, r * 0.85, 0, 2 * math.pi)
    cr.fill()
    cr.set_operator(2)                           # OVER (normal) de novo
    cr.pop_group_to_source()
    cr.paint()


def _nuvem(cr, x, y, w, cor, alfa=1.0):
    """Nuvem: base arredondada + 2 "bolotas" em cima. (x, y) = canto de baixo à esquerda."""
    h = w * 0.36
    cr.push_group()
    cr.set_source_rgb(*cor)
    raio = h / 2
    cr.new_sub_path()                            # base: retângulo de pontas redondas
    cr.arc(x + raio, y - raio, raio, math.pi / 2, 3 * math.pi / 2)
    cr.arc(x + w - raio, y - raio, raio, -math.pi / 2, math.pi / 2)
    cr.close_path()
    cr.fill()
    cr.arc(x + w * 0.36, y - h * 0.95, w * 0.22, 0, 2 * math.pi)
    cr.fill()
    cr.arc(x + w * 0.62, y - h * 1.05, w * 0.27, 0, 2 * math.pi)
    cr.fill()
    cr.pop_group_to_source()
    cr.paint_with_alpha(alfa)


def _gotas(cr, s, n, forte=False, xs=None):
    cr.set_source_rgb(*GOTA)
    cr.set_line_width(s * (0.065 if forte else 0.055))
    cr.set_line_cap(1)
    xs = xs or ([0.30, 0.50, 0.70] if n == 3 else [0.40, 0.60] if n == 2 else [0.22, 0.38, 0.54, 0.70, 0.86][:n])
    for i, fx in enumerate(xs):
        y0 = s * (0.76 + (0.05 if i % 2 else 0))
        comp = s * (0.14 if forte else 0.10)
        cr.move_to(s * fx, y0)
        cr.line_to(s * fx - comp * 0.35, y0 + comp)
        cr.stroke()


FLOCO = [(1, 1, 1)]


def _flocos(cr, s, xs):
    cr.set_source_rgb(*FLOCO[0])
    cr.set_line_width(s * 0.035)
    cr.set_line_cap(1)
    for i, fx in enumerate(xs):
        cx, cy, r = s * fx, s * (0.82 + (0.06 if i % 2 else 0)), s * 0.055
        for k in range(3):
            a = k * math.pi / 3
            cr.move_to(cx - math.cos(a) * r, cy - math.sin(a) * r)
            cr.line_to(cx + math.cos(a) * r, cy + math.sin(a) * r)
            cr.stroke()


def _raio(cr, s):
    cr.set_source_rgb(*RAIO)
    pontos = [(0.52, 0.62), (0.40, 0.82), (0.50, 0.82), (0.44, 0.98), (0.64, 0.74), (0.53, 0.74), (0.60, 0.62)]
    cr.move_to(s * pontos[0][0], s * pontos[0][1])
    for px, py in pontos[1:]:
        cr.line_to(s * px, s * py)
    cr.close_path()
    cr.fill()


def _nevoa(cr, s):
    cr.set_source_rgba(*FLOCO[0], 0.85)
    cr.set_line_width(s * 0.06)
    cr.set_line_cap(1)
    for fy, x0, x1 in ((0.76, 0.18, 0.74), (0.87, 0.30, 0.86)):
        cr.move_to(s * x0, s * fy)
        cr.line_to(s * x1, s * fy)
        cr.stroke()


def desenhar(cr, s, desenho, dia=True, fundo_claro=False):
    """Desenha o clima num quadrado de lado s (cr = contexto cairo).
    fundo_claro=True (tema claro, cartão quase branco): nuvens e flocos em cinza, senão somem."""
    global NUVEM, NUVEM_ATRAS, NUVEM_ESCURA, LUA
    guardadas = (NUVEM, NUVEM_ATRAS, NUVEM_ESCURA, LUA)
    if fundo_claro:
        NUVEM, NUVEM_ATRAS, NUVEM_ESCURA, LUA = (0.72, 0.75, 0.80), (0.58, 0.62, 0.68), (0.42, 0.45, 0.52), (0.55, 0.52, 0.42)
    FLOCO[0] = (0.55, 0.60, 0.68) if fundo_claro else (1, 1, 1)
    try:
        _desenhar(cr, s, desenho, dia, fundo_claro)
    finally:
        FLOCO[0] = (1, 1, 1)
        NUVEM, NUVEM_ATRAS, NUVEM_ESCURA, LUA = guardadas


def _desenhar(cr, s, desenho, dia, fundo_claro):
    astro = (lambda cx, cy, r: _sol(cr, cx, cy, r)) if dia else (lambda cx, cy, r: _lua(cr, cx, cy, r))
    if desenho == "limpo":
        astro(s * 0.5, s * 0.5, s * (0.22 if dia else 0.30))
        return
    if desenho == "poucas-nuvens":
        astro(s * 0.62, s * 0.36, s * (0.15 if dia else 0.20))
        _nuvem(cr, s * 0.10, s * 0.82, s * 0.64, NUVEM)
        return
    # com nuvem cheia: o "corpo" fica em cima e a chuva/neve/raio embaixo
    tem_algo_embaixo = desenho not in ("nublado",)
    base = s * (0.66 if tem_algo_embaixo else 0.78)
    escura = desenho in ("chuva-forte", "trovoada")
    _nuvem(cr, s * 0.30, base - s * 0.10, s * 0.62, NUVEM_ESCURA if escura else NUVEM_ATRAS, 0.95)
    _nuvem(cr, s * 0.08, base, s * 0.70, NUVEM_ESCURA if escura else NUVEM)
    if desenho == "garoa":
        _gotas(cr, s, 2)
    elif desenho == "chuva":
        _gotas(cr, s, 3)
    elif desenho == "chuva-forte":
        _gotas(cr, s, 5, forte=True)
    elif desenho == "neve":
        _flocos(cr, s, [0.30, 0.50, 0.70])
    elif desenho == "congelada":                 # chuva congelada: gotas e floco lado a lado
        _gotas(cr, s, 2, xs=[0.30, 0.72])
        _flocos(cr, s, [0.51])
    elif desenho == "trovoada":                  # raio no meio, gotas dos lados
        _gotas(cr, s, 2, xs=[0.30, 0.78])
        _raio(cr, s)
    elif desenho == "nevoa":
        _nevoa(cr, s)

