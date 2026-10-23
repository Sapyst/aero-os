"""
energia.py — SERVICE: tomada, bateria, limite de carga e modo de energia.

API
  na_tomada() -> bool | None       carregador ligado? (None = máquina sem bateria/adaptador)
  bateria() -> dict | None         {"nivel": 87, "estado": "Charging", "saude": 94, "ciclos": 24}
  driver() -> str                  "padrao" (kernel), "gigabyte" (gigabyte-laptop-wmi), "" (nenhum)
  limite_arquivo() -> str|None     o arquivo do kernel que limita a carga (None = não dá neste notebook)
  limite() -> int | None           % onde a carga para agora (100 = sem limite)
  pode_escrever() -> bool          o usuário consegue mudar o limite sem senha (regra udev instalada)
  definir_limite(pct) -> bool      60..100 (100 = desliga o limite). Só escreve se mudou (cada escrita vai
                                   pro controlador do notebook, o EC)
  comando_limite(pct) -> list      o mesmo, como comando pra rodar como root (quando não há permissão)
  perfil() / definir_perfil(p)     power-profiles-daemon: "performance" | "balanced" | "power-saver"
  pode_hibernar() -> bool          logind diz que hibernar funciona (precisa swap em disco + resume)

Limite: o kernel padrão expõe BAT*/charge_control_end_threshold (ThinkPad, ASUS, Dell...). Neste Gigabyte
só o driver de terceiros gigabyte-laptop-wmi (módulo aorus_laptop) dá: /sys/devices/platform/aorus_laptop/
charge_mode (0 normal, 1 custom) + charge_limit (60..100). Com o limite atingido, o notebook roda direto
da tomada e a bateria fica parada (é o "bypass": energia passa direto, a bateria não trabalha).
"""
import glob
import os
import subprocess

FONTES = "/sys/class/power_supply"
AORUS = "/sys/devices/platform/aorus_laptop"


def _ler(caminho, padrao=None):
    try:
        with open(caminho) as f:
            return f.read().strip()
    except OSError:
        return padrao


def _escrever(caminho, valor):
    try:
        with open(caminho, "w") as f:
            f.write(str(valor))
        return True
    except OSError:
        return False


def _do_tipo(tipo):
    return [p for p in glob.glob(f"{FONTES}/*") if _ler(f"{p}/type") == tipo]


def na_tomada():
    adaptadores = [p for p in _do_tipo("Mains") if _ler(f"{p}/online") is not None]
    if not adaptadores:
        return None
    return any(_ler(f"{p}/online") == "1" for p in adaptadores)


def _bateria():
    b = _do_tipo("Battery")
    return b[0] if b else None


def bateria():
    b = _bateria()
    if not b:
        return None
    cheio = int(_ler(f"{b}/charge_full", 0) or _ler(f"{b}/energy_full", 0) or 0)
    projeto = int(_ler(f"{b}/charge_full_design", 0) or _ler(f"{b}/energy_full_design", 0) or 0)
    return {"nivel": int(_ler(f"{b}/capacity", 0) or 0), "estado": _ler(f"{b}/status", "?"),
            "saude": round(cheio * 100 / projeto) if cheio and projeto else None,
            "ciclos": int(_ler(f"{b}/cycle_count", 0) or 0) or None}


def driver():
    b = _bateria()
    if b and os.path.exists(f"{b}/charge_control_end_threshold"):
        return "padrao"
    if os.path.exists(f"{AORUS}/charge_limit"):
        return "gigabyte"
    return ""


def limite_arquivo():
    d = driver()
    return {"padrao": f"{_bateria()}/charge_control_end_threshold", "gigabyte": f"{AORUS}/charge_limit"}.get(d)


def limite():
    d = driver()
    if d == "gigabyte" and _ler(f"{AORUS}/charge_mode") != "1":
        return 100                       # modo normal: o limite gravado não vale
    v = _ler(limite_arquivo()) if d else None
    return int(v) if v and v.isdigit() else None


def pode_escrever():
    f = limite_arquivo()
    return bool(f) and os.access(f, os.W_OK) and (driver() != "gigabyte" or os.access(f"{AORUS}/charge_mode", os.W_OK))


def definir_limite(pct):
    pct = max(60, min(100, int(pct)))
    if limite() == pct:
        return True
    d = driver()
    if d == "gigabyte":
        modo = "0" if pct == 100 else "1"
        ok = True
        if _ler(f"{AORUS}/charge_mode") != modo:
            ok = _escrever(f"{AORUS}/charge_mode", modo)
        if ok and pct < 100 and _ler(f"{AORUS}/charge_limit") != str(pct):
            ok = _escrever(f"{AORUS}/charge_limit", pct)
        return ok
    if d == "padrao":
        return _escrever(limite_arquivo(), pct)
    return False


def comando_limite(pct):
    pct = int(pct)
    if not 60 <= pct <= 100:
        raise ValueError(pct)
    d = driver()
    if d == "gigabyte":
        modo = 0 if pct == 100 else 1
        return ["sh", "-c", f"echo {modo} > {AORUS}/charge_mode && echo {pct} > {AORUS}/charge_limit"]
    if d == "padrao":
        return ["sh", "-c", f"echo {pct} > {limite_arquivo()}"]
    return None


def perfil():
    try:
        return subprocess.run(["powerprofilesctl", "get"], capture_output=True, text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def definir_perfil(p):
    if p in ("performance", "balanced", "power-saver") and perfil() != p:
        subprocess.run(["powerprofilesctl", "set", p], capture_output=True, timeout=5)


def pode_hibernar():
    try:
        r = subprocess.run(["busctl", "call", "org.freedesktop.login1", "/org/freedesktop/login1",
                            "org.freedesktop.login1.Manager", "CanHibernate"], capture_output=True, text=True, timeout=3)
        return '"yes"' in r.stdout
    except (OSError, subprocess.SubprocessError):
        return False


if __name__ == "__main__":
    print("na tomada:", na_tomada(), "| bateria:", bateria(), "| driver:", driver() or "nenhum",
          "| limite:", limite(), "| escrever:", pode_escrever(), "| perfil:", perfil(), "| hibernar:", pode_hibernar())
