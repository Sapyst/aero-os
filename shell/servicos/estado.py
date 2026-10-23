"""
estado.py — STATE: as escolhas do usuário das funções novas, um JSON por função.

API
  ler(nome, padrao) -> dict     ~/.config/aero-shell/<nome>.json por cima de `padrao` (chave que falta = padrão;
                                arquivo ausente ou quebrado = só o padrão, nunca exceção)
  gravar(nome, dados)           grava atômico (temporário + rename): quem lê ao mesmo tempo nunca vê metade
  versao(nome) -> float         mtime do arquivo (0 se não existe): daemons comparam pra recarregar sozinhos
"""
import json
import os

PASTA = os.path.expanduser("~/.config/aero-shell")


def caminho(nome):
    return os.path.join(PASTA, f"{nome}.json")


def ler(nome, padrao=None):
    dados = dict(padrao or {})
    try:
        with open(caminho(nome)) as f:
            lido = json.load(f)
        if isinstance(lido, dict):
            dados.update(lido)
    except (OSError, ValueError):
        pass
    return dados


def gravar(nome, dados):
    os.makedirs(PASTA, exist_ok=True)
    tmp = caminho(nome) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(dados, f, indent=2, ensure_ascii=False)
    os.replace(tmp, caminho(nome))


def versao(nome):
    try:
        return os.path.getmtime(caminho(nome))
    except OSError:
        return 0.0
