#!/usr/bin/env python3
"""
ytmotor — o motor de música do YTMusic (app) e do TerMusic (terminal). Os dois controlam o MESMO player.

  - Toca com o mpv (áudio do YouTube Music via yt-dlp). A fila inteira fica na playlist do mpv, então
    próxima/anterior também funcionam pelo MPRIS (central, TaskTube, teclas de mídia).
  - Fala com a sua conta do YouTube Music pelo ytmusicapi, com o login do Firefox (conta.py):
    busca, recentes (histórico), playlists, curtidas, "a seguir"/rádio.
  - Letras sincronizadas: LRCLIB (lrclib.net); se não tiver, a letra simples do YouTube.
  - Rádio automático: quando chega na última música da fila, completa com parecidas.

Os apps falam com ele por um socket ($XDG_RUNTIME_DIR/ytmotor.sock): uma linha JSON {"cmd":..., ...}
e a resposta numa linha JSON. Ver COMANDOS em Motor.atender. Uso: motor.py (fica rodando).
"""
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request

import conta

RUN = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
SOCK = os.path.join(RUN, "ytmotor.sock")
MPV_SOCK = os.path.join(RUN, "ytmotor-mpv.sock")
CACHE = os.path.expanduser("~/.cache/ytmotor")
LOG = os.path.join(CACHE, "motor.log")


def log(*a):
    with open(LOG, "a") as f:
        f.write(time.strftime("%H:%M:%S ") + " ".join(str(x) for x in a) + "\n")


def segundos(texto):
    """'3:06' -> 186"""
    try:
        partes = [int(x) for x in str(texto).split(":")]
    except ValueError:
        return 0
    s = 0
    for p in partes:
        s = s * 60 + p
    return s


def capa(thumbs, tamanho=544):
    """A maior miniatura, pedida no tamanho que a gente quer (as do googleusercontent aceitam =wN-hN)."""
    if not thumbs:
        return ""
    url = thumbs[-1]["url"]
    return re.sub(r"=w\d+-h\d+.*$", f"=w{tamanho}-h{tamanho}-l90-rj", url) if "googleusercontent" in url else url


# Emojis (e o "seletor de emoji" invisível) saem dos nomes que vêm do YouTube: playlists, títulos...
EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0E\uFE0F\u200D\u20E3]+")


def limpo(t):
    return re.sub(r"\s{2,}", " ", EMOJI.sub("", t or "")).strip()


def faixa(x):
    """Resultado do ytmusicapi -> o formato de faixa que os apps usam."""
    vid = x.get("videoId")
    if not vid:
        return None
    artistas = ", ".join(a["name"] for a in (x.get("artists") or []) if a.get("name"))
    album = (x.get("album") or {}).get("name") if isinstance(x.get("album"), dict) else (x.get("album") or "")
    dur = x.get("duration_seconds") or segundos(x.get("duration") or x.get("length") or 0)
    return {"id": vid, "titulo": limpo(x.get("title")), "artista": limpo(artistas), "album": limpo(album),
            "duracao": dur, "capa": capa(x.get("thumbnails") or x.get("thumbnail") or [])}


class Mpv:
    """Conversa com o mpv pelo socket JSON dele."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.req = 0
        self.respostas = {}
        self.eventos = []
        self.sock = None

    def iniciar(self):
        if os.path.exists(MPV_SOCK):
            os.remove(MPV_SOCK)
        self.proc = subprocess.Popen(
            ["mpv", "--idle=yes", "--no-video", "--no-terminal", f"--input-ipc-server={MPV_SOCK}",
             "--ytdl-format=bestaudio/best", "--ytdl-raw-options=cookies-from-browser=firefox",
             "--cache=yes", "--demuxer-max-bytes=50M", "--volume=80", "--title=YTMusic",
             "--audio-display=no", "--replaygain=track"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(MPV_SOCK):
                break
            time.sleep(0.05)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(MPV_SOCK)
        threading.Thread(target=self._ler, daemon=True).start()

    def _ler(self):
        buf = b""
        while True:
            dados = self.sock.recv(65536)
            if not dados:
                break
            buf += dados
            *linhas, buf = buf.split(b"\n")
            for l in linhas:
                try:
                    m = json.loads(l)
                except ValueError:
                    continue
                if "request_id" in m:
                    ev = self.respostas.get(m["request_id"])
                    if ev:
                        ev[1] = m
                        ev[0].set()
                elif "event" in m:
                    self.eventos.append(m)

    def cmd(self, *args, timeout=3):
        with self.lock:
            self.req += 1
            rid = self.req
            ev = [threading.Event(), None]
            self.respostas[rid] = ev
            self.sock.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        ev[0].wait(timeout)
        self.respostas.pop(rid, None)
        r = ev[1] or {}
        return r.get("data") if r.get("error") == "success" else None

    def get(self, prop, padrao=None):
        v = self.cmd("get_property", prop)
        return padrao if v is None else v

    def set(self, prop, valor):
        return self.cmd("set_property", prop, valor)


class Motor:
    def __init__(self):
        self.mpv = Mpv()
        self.yt = None
        self.meta = {}           # id -> faixa (metadados de tudo que já passou pela fila)
        self.letras = {}         # id -> letra
        self.auto_radio = True
        self.estendendo = False
        self.ultimo_id = None
        self.ultimo_contato = time.time()

    # ---------- conta ----------
    def conta(self):
        if self.yt is None:
            from ytmusicapi import YTMusic
            cab = conta.cabecalhos()
            self.yt = YTMusic(auth=cab) if cab else YTMusic()
        return self.yt

    # ---------- fila (= playlist do mpv) ----------
    @staticmethod
    def url(fid):
        return f"https://music.youtube.com/watch?v={fid}"

    @staticmethod
    def id_da_url(u):
        m = re.search(r"[?&]v=([\w-]{11})", u or "")
        return m.group(1) if m else None

    def fila(self):
        itens = self.mpv.get("playlist", []) or []
        saida = []
        for it in itens:
            fid = self.id_da_url(it.get("filename"))
            f = dict(self.meta.get(fid) or {"id": fid, "titulo": it.get("title") or fid, "artista": "", "album": "",
                                             "duracao": 0, "capa": ""})
            f["atual"] = bool(it.get("current") or it.get("playing"))
            saida.append(f)
        return saida

    def carregar(self, faixas, modo):
        """modo: 'replace' (troca a fila e toca), 'append', 'next' (logo depois da atual)."""
        faixas = [f for f in faixas if f and f.get("id")]
        if not faixas:
            return
        for f in faixas:
            self.meta[f["id"]] = f
        if modo == "replace":
            self.mpv.cmd("playlist-clear")
            self.mpv.cmd("stop")
        pos = self.mpv.get("playlist-pos", -1)
        n0 = self.mpv.get("playlist-count", 0)
        for i, f in enumerate(faixas):
            # só o título: o artista já vai separado pro MPRIS (central, teclas de mídia)
            self.mpv.cmd("loadfile", self.url(f["id"]), "append", -1, {"force-media-title": f["titulo"]})
            if modo == "next" and pos >= 0:
                self.mpv.cmd("playlist-move", n0 + i, pos + 1 + i)
        if modo == "replace":
            self.mpv.set("playlist-pos", 0)
            self.mpv.set("pause", False)
        elif self.mpv.get("idle-active", False):
            self.mpv.set("playlist-pos", n0 if modo == "append" else max(pos + 1, 0))
            self.mpv.set("pause", False)

    # ---------- rádio automático e histórico ----------
    def vigia(self):
        while True:
            time.sleep(1)
            try:
                pos = self.mpv.get("playlist-pos", -1)
                n = self.mpv.get("playlist-count", 0)
                fid = self.id_da_url(self.mpv.get("path", ""))
                if fid and fid != self.ultimo_id:
                    self.ultimo_id = fid
                    threading.Thread(target=self.registrar_historico, args=(fid,), daemon=True).start()
                if self.auto_radio and n and pos == n - 1 and fid and not self.estendendo:
                    self.estendendo = True
                    threading.Thread(target=self.estender, args=(fid,), daemon=True).start()
                # sem ninguém usando, parado, há 15 min: encerra (o app/terminal reabre quando precisar)
                parado = self.mpv.get("pause", True) or self.mpv.get("idle-active", True)
                if parado and time.time() - self.ultimo_contato > 900:
                    log("ocioso: saindo")
                    self.mpv.cmd("quit")
                    os._exit(0)
                if not parado:
                    self.ultimo_contato = time.time()
            except Exception as e:  # noqa: BLE001
                log("vigia:", e)

    def estender(self, fid):
        try:
            atuais = {f["id"] for f in self.fila()}
            w = self.conta().get_watch_playlist(videoId=fid, limit=25)
            novas = [f for f in (faixa(x) for x in w.get("tracks", [])) if f and f["id"] not in atuais][:20]
            self.carregar(novas, "append")
            log("rádio: +", len(novas))
        except Exception as e:  # noqa: BLE001
            log("rádio:", e)
        finally:
            self.estendendo = False

    def registrar_historico(self, fid):
        """Conta a reprodução no seu histórico do YouTube Music (aparece em Recent, aqui e no site)."""
        try:
            yt = self.conta()
            yt.add_history_item(yt.get_song(fid))
        except Exception as e:  # noqa: BLE001
            log("histórico:", e)

    # ---------- letras ----------
    def letra(self, fid):
        if fid in self.letras:
            return self.letras[fid]
        f = self.meta.get(fid) or {}
        res = {"sincronizada": [], "texto": "", "fonte": ""}
        artista = (f.get("artista") or "").split(",")[0].strip()
        # "I Can't Hear It Now (from the series Arcane...)" -> "I Can't Hear It Now" (o LRCLIB usa o nome limpo)
        titulo = re.sub(r"\s*[\(\[](from|feat|ft\.|with|prod|official|lyric|audio|video|remaster)[^\)\]]*[\)\]]", "",
                        f.get("titulo") or "", flags=re.I).strip()
        if titulo and artista:
            f = dict(f, titulo=titulo)
            q = {"track_name": titulo, "artist_name": artista}
            if f.get("duracao"):
                q["duration"] = f["duracao"]
            for url in (f"https://lrclib.net/api/get?{urllib.parse.urlencode(q)}",
                        f"https://lrclib.net/api/search?{urllib.parse.urlencode({'q': artista + ' ' + f['titulo']})}"):
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "ytmotor (personal)"}),
                                                timeout=8) as r:
                        d = json.load(r)
                    if isinstance(d, list):
                        d = next((x for x in d if x.get("syncedLyrics")), d[0] if d else {})
                    if d.get("syncedLyrics"):
                        linhas = []
                        for l in d["syncedLyrics"].splitlines():
                            m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", l)
                            if m:
                                linhas.append([int(m.group(1)) * 60 + float(m.group(2)), m.group(3)])
                        res = {"sincronizada": linhas, "texto": "", "fonte": "LRCLIB"}
                        break
                    if d.get("plainLyrics"):
                        res = {"sincronizada": [], "texto": d["plainLyrics"], "fonte": "LRCLIB"}
                        break
                except Exception:  # noqa: BLE001
                    continue
        if not res["sincronizada"] and not res["texto"]:
            try:
                w = self.conta().get_watch_playlist(videoId=fid, limit=1)
                if w.get("lyrics"):
                    ly = self.conta().get_lyrics(w["lyrics"])
                    res = {"sincronizada": [], "texto": ly.get("lyrics") or "", "fonte": "YouTube Music"}
            except Exception:  # noqa: BLE001
                pass
        self.letras[fid] = res
        return res

    # ---------- os comandos dos apps ----------
    def estado(self):
        fila = self.fila()
        idx = next((i for i, f in enumerate(fila) if f["atual"]), -1)
        return {"tocando": not self.mpv.get("pause", True) and not self.mpv.get("idle-active", True),
                "parado": self.mpv.get("idle-active", True), "pos": self.mpv.get("time-pos", 0) or 0,
                "duracao": self.mpv.get("duration", 0) or (fila[idx]["duracao"] if idx >= 0 else 0),
                "volume": self.mpv.get("volume", 80), "indice": idx, "atual": fila[idx] if idx >= 0 else None,
                "fila": fila, "repetir": self.mpv.get("loop-playlist", "no") not in ("no", False),
                "repetir_uma": self.mpv.get("loop-file", "no") not in ("no", False),
                "carregando": bool(self.mpv.get("seeking", False)) or (idx >= 0 and not self.mpv.get("duration")),
                "radio": self.auto_radio}

    def atender(self, p):
        r = self._atender(p)
        if isinstance(r, list):                       # guarda os dados de toda faixa que passa pelos apps
            for f in r:
                if isinstance(f, dict) and f.get("id") and "titulo" in f and "artista" in f:
                    self.meta.setdefault(f["id"], f)
        return r

    def _atender(self, p):
        """COMANDOS: estado, buscar, recentes, playlists, playlist, tocar, adicionar, proxima_na_fila,
        pular_para, remover, mover, pausar, proxima, anterior, ir, volume, repetir, embaralhar, radio,
        auto_radio, letra, limpar."""
        self.ultimo_contato = time.time()
        c = p.get("cmd")
        yt = None
        if c == "estado":
            return self.estado()
        if c in ("buscar", "recentes", "playlists", "playlist", "radio"):
            yt = self.conta()
        if c == "buscar":
            r = yt.search(p["q"], filter=p.get("filtro", "songs"), limit=40)
            return [f for f in (faixa(x) for x in r) if f]
        if c == "recentes":
            return [f for f in (faixa(x) for x in yt.get_history()) if f][:100]
        if c == "playlists":
            return [{"id": x["playlistId"], "titulo": limpo(x["title"]), "qtd": x.get("count") or "",
                     "capa": capa(x.get("thumbnails") or [], 226)} for x in yt.get_library_playlists(limit=None)]
        if c == "playlist":
            if p["id"] in ("LM", "curtidas"):
                d = yt.get_liked_songs(limit=None)
            else:
                d = yt.get_playlist(p["id"], limit=None)
            return [f for f in (faixa(x) for x in d.get("tracks", [])) if f]
        if c == "tocar":
            fs = p["faixas"]
            i = p.get("inicio", 0)
            self.carregar(fs[i:] + fs[:i] if p.get("girar") else fs, "replace")
            if not p.get("girar") and i:
                self.mpv.set("playlist-pos", i)
            return True
        if c == "adicionar":
            self.carregar(p["faixas"], "append")
            return True
        if c == "proxima_na_fila":
            self.carregar(p["faixas"], "next")
            return True
        if c == "pular_para":
            self.mpv.set("playlist-pos", p["i"])
            self.mpv.set("pause", False)
            return True
        if c == "remover":
            self.mpv.cmd("playlist-remove", p["i"])
            return True
        if c == "mover":
            de, para = p["de"], p["para"]
            self.mpv.cmd("playlist-move", de, para + 1 if para > de else para)
            return True
        if c == "limpar":
            # a fila INTEIRA: o playlist-clear do mpv mantém a música atual; tirar ela também para de tocar
            self.mpv.cmd("playlist-clear")
            if self.mpv.get("playlist-count", 0):
                self.mpv.cmd("playlist-remove", "current")
            self.mpv.cmd("stop")
            return True
        if c == "pausar":
            if self.mpv.get("idle-active", False) and self.mpv.get("playlist-count", 0):
                self.mpv.set("playlist-pos", max(self.mpv.get("playlist-pos", 0), 0))
            self.mpv.cmd("cycle", "pause")
            return True
        if c == "proxima":
            self.mpv.cmd("playlist-next")
            return True
        if c == "anterior":
            if (self.mpv.get("time-pos", 0) or 0) > 4:
                self.mpv.cmd("seek", 0, "absolute")
            else:
                self.mpv.cmd("playlist-prev")
            return True
        if c == "ir":
            self.mpv.cmd("seek", float(p["s"]), "absolute")
            return True
        if c == "volume":
            self.mpv.set("volume", max(0, min(130, float(p["v"]))))
            return True
        if c == "repetir":                 # desligado -> fila -> uma música -> desligado
            fila_on = self.mpv.get("loop-playlist", "no") not in ("no", False)
            uma_on = self.mpv.get("loop-file", "no") not in ("no", False)
            if not fila_on and not uma_on:
                self.mpv.set("loop-playlist", "inf")
            elif fila_on:
                self.mpv.set("loop-playlist", "no")
                self.mpv.set("loop-file", "inf")
            else:
                self.mpv.set("loop-file", "no")
            return True
        if c == "embaralhar":
            self.mpv.cmd("playlist-shuffle")
            return True
        if c == "radio":
            w = yt.get_watch_playlist(videoId=p["id"], limit=30)
            fs = [f for f in (faixa(x) for x in w.get("tracks", [])) if f]
            self.carregar(fs, "replace")
            return True
        if c == "auto_radio":
            self.auto_radio = bool(p.get("ligado", not self.auto_radio))
            return self.auto_radio
        if c == "letra":
            if p.get("faixa"):
                self.meta.setdefault(p["id"], p["faixa"])
            return self.letra(p["id"])
        raise ValueError(f"comando desconhecido: {c}")


def servir(motor):
    if os.path.exists(SOCK):
        os.remove(SOCK)
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(SOCK)
    os.chmod(SOCK, 0o600)              # só o seu usuário fala com o motor
    srv.listen(16)

    def cliente(c):
        with c:
            buf = b""
            while True:
                dados = c.recv(65536)
                if not dados:
                    return
                buf += dados
                while b"\n" in buf:
                    linha, buf = buf.split(b"\n", 1)
                    try:
                        r = {"ok": True, "r": motor.atender(json.loads(linha))}
                    except Exception as e:  # noqa: BLE001
                        log("erro:", repr(e))
                        r = {"ok": False, "erro": str(e)}
                    c.sendall((json.dumps(r, ensure_ascii=False) + "\n").encode())

    while True:
        c, _ = srv.accept()
        threading.Thread(target=cliente, args=(c,), daemon=True).start()


def main():
    os.makedirs(CACHE, exist_ok=True)
    # Só UM motor: os painéis do TerMusic e o app podem pedir o motor ao mesmo tempo. Quem pega a trava
    # fica; os outros saem na hora (e os apps usam o que ficou).
    import fcntl
    global _trava
    _trava = open(os.path.join(CACHE, "motor.lock"), "w")
    try:
        fcntl.flock(_trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return
    m = Motor()
    m.mpv.iniciar()
    threading.Thread(target=m.vigia, daemon=True).start()
    log("motor no ar")
    servir(m)


if __name__ == "__main__":
    main()
