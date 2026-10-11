"""cliente.py — como o YTMusic (app) e o TerMusic falam com o motor (motor.py).

    from cliente import chamar
    chamar("buscar", q="freya ridings")      # -> lista de faixas
    chamar("pausar")
Se o motor não estiver rodando, ele é iniciado (fica rodando mesmo depois de fechar o app: a música
continua; ele sai sozinho depois de 15 min parado)."""
import json
import os
import socket
import subprocess
import time

PASTA = os.path.dirname(os.path.abspath(__file__))
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "ytmotor.sock")


class ErroMotor(Exception):
    pass


def _conectar():
    s = socket.socket(socket.AF_UNIX)
    s.connect(SOCK)
    return s


def iniciar_motor():
    subprocess.Popen(["python3", os.path.join(PASTA, "motor.py")], cwd=PASTA, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(100):
        try:
            _conectar().close()
            return
        except OSError:
            time.sleep(0.1)
    raise ErroMotor("o motor não iniciou (veja ~/.cache/ytmotor/motor.log)")


def chamar(cmd, timeout=60, **params):
    try:
        s = _conectar()
    except OSError:
        iniciar_motor()
        s = _conectar()
    with s:
        s.settimeout(timeout)
        s.sendall((json.dumps({"cmd": cmd, **params}) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            dados = s.recv(1 << 20)
            if not dados:
                break
            buf += dados
    r = json.loads(buf or b"{}")
    if not r.get("ok"):
        raise ErroMotor(r.get("erro", "sem resposta"))
    return r.get("r")
