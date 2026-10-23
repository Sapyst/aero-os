"""
hypr.py — SERVICE: conversa com o Hyprland direto pelo socket (sem abrir um processo hyprctl a cada pergunta).

API
  pedir(cmd) -> str              comando cru no socket 1 ("dispatch workspace 2", "keyword ...")
  json_de(cmd, padrao) -> obj    "monitors all", "clients", "activeworkspace"... já em JSON
  despachar(*args) -> str        atalho: despachar("workspace", 3)
  eventos() -> iterador          (nome, dados) do socket 2, pra sempre (reconecta se o Hyprland reiniciar)
"""
import json
import os
import socket
import time

PASTA = os.path.join(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"), "hypr",
                     os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", ""))


def pedir(cmd, tempo=3):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(tempo)
        s.connect(os.path.join(PASTA, ".socket.sock"))
        s.sendall(cmd.encode())
        partes = []
        while True:
            bloco = s.recv(65536)
            if not bloco:
                break
            partes.append(bloco)
    return b"".join(partes).decode(errors="replace")


def json_de(cmd, padrao=None):
    try:
        return json.loads(pedir(f"j/{cmd}"))
    except (OSError, ValueError):
        return padrao


def despachar(*args):
    return pedir("dispatch " + " ".join(str(a) for a in args))


def eventos():
    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(os.path.join(PASTA, ".socket2.sock"))
                resto = b""
                while True:
                    bloco = s.recv(4096)
                    if not bloco:
                        break
                    resto += bloco
                    *linhas, resto = resto.split(b"\n")
                    for l in linhas:
                        nome, _, dados = l.decode(errors="replace").partition(">>")
                        yield nome, dados
        except OSError:
            pass
        time.sleep(2)
