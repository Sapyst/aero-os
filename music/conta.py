"""conta.py — login do YouTube Music a partir do Firefox (sem senha, sem arquivo de credenciais).

Lê os cookies do youtube.com do perfil padrão do Firefox (cópia temporária: o Firefox aberto trava o
arquivo) e monta os cabeçalhos que o ytmusicapi usa no modo "browser". Tudo fica só na memória."""
import configparser
import os
import shutil
import sqlite3
import tempfile

ESSENCIAIS = {"SID", "HSID", "SSID", "APISID", "SAPISID", "SIDCC", "LOGIN_INFO", "PREF", "YSC",
              "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA", "__Secure-1PSID", "__Secure-3PSID",
              "__Secure-1PAPISID", "__Secure-3PAPISID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
              "__Secure-1PSIDCC", "__Secure-3PSIDCC", "__Secure-ROLLOUT_TOKEN"}
BASES = [os.path.expanduser("~/.config/mozilla/firefox"), os.path.expanduser("~/.mozilla/firefox")]


def perfil_firefox():
    for base in BASES:
        ini = os.path.join(base, "profiles.ini")
        if not os.path.exists(ini):
            continue
        cp = configparser.ConfigParser()
        cp.read(ini)
        for sec in cp.sections():
            if sec.startswith("Install") and cp[sec].get("Default"):
                return os.path.join(base, cp[sec]["Default"])
        for sec in cp.sections():
            if cp[sec].get("Default") == "1" and cp[sec].get("Path"):
                return os.path.join(base, cp[sec]["Path"])
    return None


def cookies_youtube():
    p = perfil_firefox()
    if not p:
        return {}
    with tempfile.TemporaryDirectory() as tmp:
        os.chmod(tmp, 0o700)
        for suf in ("", "-wal"):
            orig = os.path.join(p, "cookies.sqlite" + suf)
            if os.path.exists(orig):
                shutil.copy(orig, os.path.join(tmp, "c.sqlite" + suf))
        db = sqlite3.connect(os.path.join(tmp, "c.sqlite"))
        linhas = db.execute("select name, value from moz_cookies where host in ('.youtube.com', 'music.youtube.com')").fetchall()
        db.close()
    # só os de login/preferência: mandar os ~115 cookies do YouTube dá "413 (cabeçalho grande demais)"
    return {k: v for k, v in linhas if k in ESSENCIAIS}


def cabecalhos():
    """Cabeçalhos pro YTMusic(auth=...). None se o Firefox não tiver login no YouTube."""
    c = cookies_youtube()
    if "SAPISID" not in c and "__Secure-3PAPISID" not in c:
        return None
    from ytmusicapi.helpers import get_authorization
    sapisid = c.get("SAPISID") or c.get("__Secure-3PAPISID")
    return {
        "authorization": get_authorization(sapisid + " https://music.youtube.com"),
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
        "accept": "*/*", "accept-language": "en-US,en;q=0.5", "content-type": "application/json",
        "x-goog-authuser": "0", "x-origin": "https://music.youtube.com", "origin": "https://music.youtube.com",
        "cookie": "; ".join(f"{k}={v}" for k, v in c.items()),
    }
