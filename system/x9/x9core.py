"""
x9core.py — the heart of X9: turns journal lines from sudo, pkexec and su into readable events,
puts each one in a category ("Packages", "Root shell"...), guesses what it was for, and keeps
the daily archive (/var/log/x9/YYYY-MM-DD.jsonl, one event per line).

Used by the root service (x9d), by the root helper (x9-admin) and, read-only, by the X9 app.
Nothing here runs a command or touches the network.
"""
import fcntl
import hashlib
import json
import os
import re
import shlex
import time

ARQUIVO = "/var/log/x9"                  # one file per day
ESTADO = "/var/lib/x9"                   # cursor, locks
RETENCAO = "/etc/x9/retention.json"      # auto-delete rules
TRAVA = os.path.join(ARQUIVO, ".lock")   # x9d and x9-admin never write at the same time

CATEGORIAS = ["Packages", "Services", "Files & config", "Disks & storage", "Network", "Users & security",
              "Kernel & boot", "Power", "Root shell", "Scripts & code", "Inspect (read-only)",
              "Aero system", "Authentication", "X9", "Other"]
RESULTADOS = {"allowed": "Allowed", "denied": "Denied", "wrong-password": "Wrong password",
              "cancelled": "Cancelled", "failed": "Failed"}
DIA_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ID_RE = re.compile(r"^[0-9a-f]{16}$")


# ---------- who asked ----------

APPS_CONHECIDOS = {"com.anthropic.claude": "Claude", "claude": "Claude", "kitty": "Terminal (kitty)",
                   "foot": "Terminal (foot)", "alacritty": "Terminal (alacritty)",
                   "org.wezfurlong.wezterm": "Terminal (wezterm)", "com.aero.settings": "Settings",
                   "aero-settings": "Settings", "x9": "X9", "steam": "Steam", "code": "VS Code"}


def app_da_unidade(unidade):
    """Name of the app from its systemd unit: the desktop puts every app in its own "scope",
    e.g. app-com.anthropic.Claude-2166.scope -> Claude, kitty-16031-0.scope -> Terminal (kitty)."""
    if not unidade:
        return "Unknown"
    if re.match(r"^session-\d+\.scope$", unidade):
        return "Desktop session"
    nome = re.sub(r"\.(scope|service)$", "", unidade)
    nome = re.sub(r"^app-", "", nome)
    nome = re.sub(r"^(Hyprland|flatpak|niri|gnome|kde)-", "", nome)
    nome = re.sub(r"(-[0-9a-f]{6,}|-\d+)+$", "", nome)       # the pid/random tail
    nome = nome.replace("\\x2d", "-")
    return APPS_CONHECIDOS.get(nome.lower(), nome or "Unknown")


# ---------- reading one journal line ----------

EXECUTAVEIS = {"sudo": ("/usr/bin/sudo", "/usr/sbin/sudo"), "pkexec": ("/usr/bin/pkexec",),
               "su": ("/usr/bin/su", "/usr/sbin/su")}

def _texto(v):
    if isinstance(v, list):                                    # journal sends non-UTF-8 text as bytes
        try:
            return bytes(v).decode("utf-8", "replace")
        except (TypeError, ValueError):
            return ""
    return v if isinstance(v, str) else ""


def _campos_sudo(resto):
    """ ' TTY=pts/1 ; PWD=/x ; USER=root ; COMMAND=/usr/bin/ls -l' -> (reason, {TTY:..., COMMAND:...})"""
    campos, motivo = {}, ""
    partes = resto.split(" ; ")
    for i, parte in enumerate(partes):
        parte = parte.strip()
        if parte.startswith("COMMAND="):
            campos["COMMAND"] = " ; ".join([parte[8:]] + partes[i + 1:]).strip()
            break
        chave, igual, valor = parte.partition("=")
        if igual and re.match(r"^[A-Z]+$", chave):
            campos[chave] = valor
        elif not motivo:
            motivo = parte
    return motivo, campos


def _comando_do_cmdline(cmdline, ferramenta):
    """Command from the process's own command line (_CMDLINE), when the log line has none:
    'sudo -A bash x.sh --apply' -> 'bash x.sh --apply'."""
    try:
        partes = shlex.split(cmdline)
    except ValueError:
        partes = cmdline.split()
    if partes and os.path.basename(partes[0]) == ferramenta:
        partes = partes[1:]
    com_valor = {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T", "--user", "--group"}
    while partes and partes[0].startswith("-"):
        if partes.pop(0) in com_valor and partes:
            partes.pop(0)
    return " ".join(shlex.quote(p) for p in partes)


def ler_linha(e):
    """One journal entry (dict from journalctl -o json) -> a "piece" (dict) or None if it is noise.
    A piece is not yet an event: sudo can spread one command over several lines."""
    fonte = _texto(e.get("SYSLOG_IDENTIFIER"))
    msg = _texto(e.get("MESSAGE")).rstrip()
    if fonte not in ("sudo", "pkexec", "su") or not msg:
        return None
    # Anyone can write "sudo: ..." to the journal (logger -t sudo). _EXE is filled in by journald
    # from the real process, so it can't be faked: only the real programs count.
    # pkexec is special: it logs "Executing command" and turns into that command right away, so
    # journald often sees the new program. Its PAM line just before (same process) still shows the
    # real pkexec: the Juntador only accepts that "Executing" line if the PAM line proved the pid.
    confiavel = _texto(e.get("_EXE")) in EXECUTAVEIS[fonte]
    if not confiavel and fonte != "pkexec":
        return None
    try:
        ts = int(e.get("__REALTIME_TIMESTAMP", "0")) / 1e6
    except ValueError:
        return None
    base = {"tool": fonte, "pid": _texto(e.get("_PID")), "ts": ts, "cursor": _texto(e.get("__CURSOR")),
            "trusted": confiavel,
            "app": app_da_unidade(_texto(e.get("_SYSTEMD_USER_UNIT")) or _texto(e.get("_SYSTEMD_UNIT"))),
            "cmdline": _texto(e.get("_CMDLINE"))[:2000]}

    if "(sudo:session)" in msg or "(su:session)" in msg or "(polkit-1:session)" in msg:
        return {**base, "kind": "session"}
    if fonte == "sudo" and msg.startswith("pam_unix(sudo:auth)"):
        if "authentication failure" in msg:
            return {**base, "kind": "bad-password"}
        return {**base, "kind": "no-password"}                 # "conversation failed": prompt closed

    if fonte == "sudo":
        m = re.match(r"^\s*(\S+) : (.*)$", msg, re.S)
        if not m:
            return None
        usuario, resto = m.groups()
        if resto.startswith("(command continued) "):
            return {**base, "kind": "continued", "text": resto[len("(command continued) "):]}
        motivo, c = _campos_sudo(resto)
        return {**base, "kind": "main", "user": usuario, "as": c.get("USER", "root"), "tty": c.get("TTY", ""),
                "cwd": c.get("PWD", ""), "command": c.get("COMMAND", ""), "reason": motivo}

    if fonte == "pkexec":
        m = re.match(r"^(\S+): (.*?)\s*(\[USER=.*)$", msg, re.S)
        if not m:
            return None
        usuario, texto, colchetes = m.groups()
        c = dict(re.findall(r"\[(USER|TTY|CWD)=([^\]]*)\]", colchetes))
        cmd = colchetes.split("[COMMAND=", 1)[1][:-1] if "[COMMAND=" in colchetes and colchetes.endswith("]") else ""
        motivo = "" if texto.startswith("Executing command") else texto.replace("Error executing command as another user: ", "")
        return {**base, "kind": "main", "user": usuario, "as": c.get("USER", "root"), "tty": c.get("TTY", ""),
                "cwd": c.get("CWD", ""), "command": cmd, "reason": motivo}

    # su: "(to root) lucas on pts/7" / "FAILED SU (to root) lucas on pts/7"
    m = re.match(r"^(FAILED SU )?\(to (\S+)\) (\S+) on (\S+)", msg)
    if not m:
        return None
    falhou, alvo, usuario, tty = m.groups()
    return {**base, "kind": "main", "user": usuario, "as": alvo, "tty": tty, "cwd": "",
            "command": "su", "reason": "authentication failure" if falhou else ""}


class Juntador:
    """Joins the pieces of one sudo/pkexec/su call (same process) into a single event.
    sudo writes: [pam lines] -> main line -> [continued lines] -> session opened -> session closed."""

    def __init__(self):
        self.abertos = {}            # (tool, pid) -> event being built
        self.pkexec_reais = {}       # pid -> when its PAM line came from the real pkexec

    def receber(self, p, agora=None):
        """Returns the events that are finished now. `agora` = when the line ARRIVED (wall clock):
        an old journal imported at once must not look "finished" between two reads."""
        agora = time.time() if agora is None else agora
        prontos = []
        chave = (p["tool"], p["pid"])
        if p["tool"] == "pkexec":
            if p["kind"] == "session" and p["trusted"]:
                self.pkexec_reais[p["pid"]] = agora
                if len(self.pkexec_reais) > 1000:
                    for pid in sorted(self.pkexec_reais, key=self.pkexec_reais.get)[:500]:
                        del self.pkexec_reais[pid]
            if not p["trusted"] and not (p["kind"] == "main" and self.pkexec_reais.pop(p["pid"], None) is not None):
                return prontos                                  # not proven to come from pkexec
            if p["kind"] == "main" and p["trusted"]:
                self.pkexec_reais.pop(p["pid"], None)
        atual = self.abertos.get(chave)
        if p["kind"] == "continued":
            if atual is not None:
                atual["command"] = (atual["command"] + " " + p["text"])[:8000]
                atual["_visto"] = agora
            return prontos
        if p["kind"] == "session":
            if atual is not None and atual.get("_main"):
                prontos.append(self._fechar(chave))
            return prontos
        if p["kind"] in ("bad-password", "no-password"):
            if atual is not None and atual.get("_main"):
                prontos.append(self._fechar(chave))
                atual = None
            if atual is None:
                atual = self.abertos[chave] = {**p, "user": "", "as": "root", "tty": "", "cwd": "", "reason": "",
                                               "command": _comando_do_cmdline(p["cmdline"], p["tool"]),
                                               "_main": False, "_tentativas": 0}
            if p["kind"] == "bad-password":
                atual["_tentativas"] += 1
            else:
                atual["_sem_senha"] = True
            atual["_visto"] = agora
            return prontos
        # main line
        if atual is not None and atual.get("_main"):
            prontos.append(self._fechar(chave))
            atual = None
        novo = {**p, "_main": True, "_visto": agora}
        if atual is not None:                                   # pam lines came first
            novo["_tentativas"] = atual.get("_tentativas", 0)
            novo["_sem_senha"] = atual.get("_sem_senha", False)
            novo["cursor"] = atual["cursor"]
            novo["ts"] = atual["ts"]
            novo["command"] = novo["command"] or atual["command"]
        self.abertos[chave] = novo
        return prontos

    def vencidos(self, agora, espera=3.0):
        """Events with no new line for `espera` seconds (the call is over)."""
        return [self._fechar(k) for k, ev in list(self.abertos.items()) if agora - ev["_visto"] >= espera]

    def todos(self):
        return [self._fechar(k) for k in list(self.abertos)]

    def _fechar(self, chave):
        return finalizar(self.abertos.pop(chave))


def _resultado(ev):
    motivo = (ev.get("reason") or "").lower()
    if not ev.get("_main"):
        return ("wrong-password" if ev.get("_tentativas") else "cancelled"), \
               ("Wrong password" if ev.get("_tentativas") else "The password window was closed")
    if not motivo:
        return "allowed", ""
    if "incorrect password" in motivo or "authentication failure" in motivo:
        return "wrong-password", ev["reason"]
    if "dismissed" in motivo:
        return "cancelled", ev["reason"]
    if "password is required" in motivo:
        return "denied", "Tried to use sudo without asking for the password"
    if "not in sudoers" in motivo or "not allowed" in motivo or "not authorized" in motivo:
        return "denied", ev["reason"]
    return "failed", ev["reason"]


def finalizar(ev):
    """Builds the final event (what goes in the archive and what the app shows)."""
    resultado, motivo = _resultado(ev)
    categoria, para_que, risco = classificar(ev.get("command", ""), ev["tool"])
    sem_terminal = ev.get("tty", "") in ("", "unknown")
    if resultado == "denied" and "without asking" in motivo:
        risco = "high"                                          # an app tried a cached password, silently
    if resultado == "wrong-password":
        categoria, risco = "Authentication", "medium"
    return {"id": hashlib.sha256(ev["cursor"].encode()).hexdigest()[:16], "ts": round(ev["ts"], 3),
            "tool": ev["tool"], "user": ev.get("user") or "", "as": ev.get("as") or "root",
            "result": resultado, "reason": motivo[:300], "command": ev.get("command", "")[:8000],
            "cwd": ev.get("cwd", "")[:500], "tty": ev.get("tty", "") or "none", "terminal": not sem_terminal,
            "app": ev.get("app", "Unknown"), "category": categoria, "purpose": para_que, "risk": risco}


# ---------- what was it for ----------

PROGRAMAS = {
    "Packages": {"pacman", "yay", "paru", "makepkg", "flatpak", "pip", "pip3", "npm", "debtap", "snap"},
    "Services": {"systemctl", "service", "kill", "pkill", "killall"},
    "Files & config": {"cp", "mv", "install", "tee", "rm", "mkdir", "rmdir", "chmod", "chown", "chgrp", "ln",
                       "touch", "rsync", "truncate", "nano", "vim", "vi", "nvim", "sed", "unlink", "setfacl",
                       "tar", "unzip", "chattr"},
    "Disks & storage": {"mount", "umount", "fdisk", "gdisk", "sgdisk", "parted", "cryptsetup", "swapon",
                        "swapoff", "mkswap", "btrfs", "dd", "wipefs", "fsck", "e2fsck", "losetup", "fstrim",
                        "zramctl", "smartctl", "hdparm", "nvme", "snapper", "ntfsfix", "ntfs-3g", "ntfsresize"},
    "Network": {"ip", "nmcli", "iptables", "ip6tables", "nft", "ufw", "firewall-cmd", "wg", "wg-quick",
                "resolvectl", "tcpdump", "iw", "rfkill", "bluetoothctl", "tailscale", "openvpn"},
    "Users & security": {"passwd", "chpasswd", "useradd", "userdel", "usermod", "groupadd", "groupdel", "gpasswd",
                         "visudo", "chage", "faillock", "pwck", "sbctl", "aa-enforce", "setenforce"},
    "Kernel & boot": {"mkinitcpio", "bootctl", "grub-mkconfig", "grub-install", "efibootmgr", "sysctl", "modprobe",
                      "rmmod", "insmod", "dkms", "ukify", "kernel-install", "dracut", "plymouth-set-default-theme"},
    "Power": {"reboot", "shutdown", "poweroff", "halt", "cpupower", "tlp", "powerprofilesctl", "rtcwake"},
    "Inspect (read-only)": {"ls", "cat", "less", "more", "head", "tail", "stat", "find", "grep", "du", "df", "lsof",
                            "ss", "lsblk", "blkid", "dmesg", "journalctl", "ps", "top", "htop", "id", "whoami",
                            "file", "wc", "tree", "lspci", "lsusb", "free", "uptime", "pgrep", "fuser", "dmidecode",
                            "realpath", "readlink", "test", "true", "sensors", "nvidia-smi", "strings", "md5sum",
                            "sha256sum", "cmp", "diff", "getfacl", "lsattr", "findmnt", "swaplabel"},
}
SHELLS = {"bash", "sh", "zsh", "fish", "dash"}
CODIGO = {"python", "python3", "perl", "ruby", "node", "lua"}
INVOLUCROS = {"env", "nice", "ionice", "nohup", "stdbuf", "timeout", "time", "command", "exec", "chrt", "setsid"}
SENSIVEIS = ("/etc/sudoers", "/etc/polkit-1", "/etc/pam.d", "/etc/shadow", "/etc/passwd", "/etc/group",
             "/etc/gshadow", "/etc/security", "/root/.ssh", "/etc/ssh")
SO_LEITURA_SYSTEMCTL = {"status", "show", "cat", "is-active", "is-enabled", "is-failed", "list-units",
                        "list-unit-files", "list-timers", "list-dependencies", "list-sockets", "help"}
ENERGIA_SYSTEMCTL = {"reboot", "poweroff", "suspend", "hibernate", "halt", "hybrid-sleep", "suspend-then-hibernate"}


def _palavras(comando):
    try:
        return shlex.split(comando)
    except ValueError:
        return comando.split()


def _desembrulhar(p):
    """'env A=1 nice -n 5 /usr/bin/pacman -S x' -> ['pacman', '-S', 'x']"""
    p = list(p)
    while p:
        nome = os.path.basename(p[0])
        if nome == "env":
            # pkexec logs env values without quotes ("A=x --flag 4 /usr/bin/prog"): jump to the first
            # absolute path that is not a VAR=value
            absolutos = [i for i, t in enumerate(p[1:], 1) if t.startswith("/") and "=" not in t]
            if absolutos:
                del p[:absolutos[0]]
                continue
        if nome in INVOLUCROS:
            p.pop(0)
            while p and (p[0].startswith("-") or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p[0])
                         or (nome == "timeout" and re.match(r"^\d+[smhd]?$", p[0]))):
                opcao = p.pop(0)
                if nome in ("nice", "ionice", "chrt") and opcao in ("-n", "-c") and p:
                    p.pop(0)
            continue
        if nome == "sudo":
            p.pop(0)
            while p and p[0].startswith("-"):
                p.pop(0)
            continue
        break
    if p:
        p[0] = os.path.basename(p[0])
    return p


def _lista(itens, n=4):
    itens = [i for i in itens if i]
    if len(itens) > n:
        return ", ".join(itens[:n]) + f" +{len(itens) - n}"
    return ", ".join(itens) or "?"


def _alvos(args):
    return [a for a in args if not a.startswith("-")]


def classificar(comando, ferramenta="sudo"):
    """command -> (category, purpose in plain English, risk low/medium/high).
    It is a best guess from the command line: X9 cannot see inside a script."""
    if ferramenta == "su" or comando == "su":
        return "Root shell", "Open a root shell (su)", "high"
    if comando in ("validate", "list", "-v", "-l"):
        return "Authentication", "Check the password only (sudo -v / -l)", "low"
    p = _desembrulhar(_palavras(comando))
    if not p:
        return "Other", "Unknown command", "medium"
    prog, args = p[0], p[1:]
    texto = " ".join(p)
    toca_sensivel = any(s in texto for s in SENSIVEIS)

    if prog == "x9-setlock":
        return "X9", "Set the X9 password", "low"
    if prog == "x9-admin":
        acao = args[0] if args else "?"
        return "X9", {"delete": "Delete X9 log entries", "delete-all": "Delete all X9 logs",
                      "set-retention": "Change X9 auto-delete rules",
                      "reset-lock": "Reset the X9 password"}.get(acao, f"X9: {acao}"), "medium"
    if prog == "aero-snap":
        if args[:1] in (["list"], ["undo"]):
            return "Disks & storage", ("Undo changes from a snapshot" if args[0] == "undo"
                                       else "Look at the snapshots"), "medium" if args[0] == "undo" else "low"
        dentro = classificar(" ".join(shlex.quote(a) for a in args[1:]), ferramenta) if len(args) > 1 else None
        if dentro:
            return dentro[0], f"{dentro[1]} (with snapshots: {args[0]})", dentro[2]
        return "Disks & storage", "Take a snapshot", "low"
    eh_aero = prog.startswith("aero-") or prog == "painel-energia" or \
        (prog in SHELLS and any("aero" in a for a in _alvos(args)[:1]))
    if eh_aero:
        if prog == "aero-memory-config":
            return "Aero system", "Change memory settings: " + " ".join(args[:3]), "low"
        if prog == "painel-energia":
            return "Aero system", "Apply power settings (lid and power button)", "low"
        if prog == "aero-swap-setup":
            return "Aero system", "Change the SSD swap file", "medium"
        if prog in SHELLS and args:
            return "Aero system", f"Run Aero installer {os.path.basename(_alvos(args)[0]) if _alvos(args) else ''}".strip(), "medium"
        return "Aero system", f"Aero tool: {prog} {' '.join(args[:3])}".strip(), "low"

    if prog == "pacman":
        flags = "".join(a.lstrip("-") for a in args if a.startswith("-") and not a.startswith("--"))
        longas = [a for a in args if a.startswith("--")]
        pacotes = [a for a in _alvos(args)]
        if "S" in flags and "u" in flags:
            return "Packages", "Upgrade the whole system", "medium"
        if "S" in flags and pacotes:
            return "Packages", f"Install packages: {_lista(pacotes)}", "medium"
        if "S" in flags and "y" in flags:
            return "Packages", "Refresh the package database", "low"
        if "R" in flags:
            return "Packages", f"Remove packages: {_lista(pacotes)}", "medium"
        if "U" in flags:
            return "Packages", f"Install a package file: {_lista([os.path.basename(x) for x in pacotes])}", "high"
        if "Q" in flags or "F" in flags or "--query" in longas:
            return "Inspect (read-only)", "Look at installed packages", "low"
        if "D" in flags:
            return "Packages", "Change package install reasons", "low"
        return "Packages", f"pacman {' '.join(args[:3])}", "medium"
    if prog in ("makepkg", "yay", "paru"):
        return "Packages", f"Build/install from the AUR or a recipe ({prog})", "high"
    if prog == "flatpak":
        verbo = args[0] if args else "?"
        return "Packages", f"Flatpak {verbo}: {_lista(_alvos(args[1:]))}", "medium" if verbo == "install" else "low"
    if prog in ("pip", "pip3", "npm"):
        return "Packages", f"{prog} {' '.join(args[:3])} (as root)", "high"

    if prog == "systemctl":
        resto = [a for a in args if not a.startswith("-")]
        verbo = resto[0] if resto else "status"
        unidades = resto[1:]
        if verbo in ENERGIA_SYSTEMCTL:
            return "Power", f"{verbo.capitalize()} the computer", "low"
        if verbo in SO_LEITURA_SYSTEMCTL or verbo.startswith("list-"):
            return "Inspect (read-only)", f"Look at services ({verbo})", "low"
        risco = "medium" if verbo in ("disable", "mask", "edit", "set-property", "revert") else "low"
        nomes = {"daemon-reload": "Reload service definitions", "start": "Start", "stop": "Stop",
                 "restart": "Restart", "reload": "Reload", "enable": "Turn on at boot", "disable": "Turn off at boot",
                 "mask": "Block", "unmask": "Unblock", "kill": "Stop by force", "edit": "Edit",
                 "try-restart": "Restart", "reload-or-restart": "Restart"}
        acao = nomes.get(verbo, verbo)
        return "Services", (f"{acao} service: {_lista(unidades)}" if unidades else acao), risco

    if prog in ("bash", "sh", "zsh", "fish", "dash"):
        if not args or args[0] in ("-i", "-l", "--login") or args == ["-"]:
            return "Root shell", f"Open a root shell ({prog})", "high"
        if args[0] == "-c":
            codigo = args[1] if len(args) > 1 else ""
            return "Scripts & code", f"Run shell commands: {codigo[:80]}", "high" if toca_sensivel else "medium"
        script = _alvos(args)
        return "Scripts & code", f"Run script {os.path.basename(script[0]) if script else '?'}", "medium"
    if prog.startswith("python") or prog in CODIGO:
        if "-c" in args or "-e" in args:
            return "Scripts & code", f"Run {prog} code", "medium"
        script = _alvos(args)
        return "Scripts & code", f"Run {prog} script {os.path.basename(script[0]) if script else ''}".strip(), "medium"

    for categoria, nomes in PROGRAMAS.items():
        if prog in nomes or (categoria == "Disks & storage" and prog.startswith(("mkfs", "fsck"))):
            return _detalhar(categoria, prog, args, toca_sensivel)
    if prog.startswith("mkfs"):
        return _detalhar("Disks & storage", prog, args, toca_sensivel)
    if prog.endswith(".sh"):
        return "Scripts & code", f"Run script {prog}", "medium"
    return ("Users & security" if toca_sensivel else "Other"), f"Run {prog} {' '.join(args[:2])}".strip(), \
        "high" if toca_sensivel else "medium"


def _detalhar(categoria, prog, args, sensivel):
    alvos = _alvos(args)
    if categoria == "Files & config":
        if sensivel:
            return "Users & security", f"Change security files ({prog} {_lista(alvos, 2)})", "high"
        if prog in ("rm", "rmdir", "unlink"):
            recursivo = any(a.startswith("-") and ("r" in a or "R" in a) for a in args) or "--recursive" in args
            sistema = any(a in ("/", "/etc", "/usr", "/boot", "/var", "/home") or a.rstrip("/") in
                          ("", "/etc", "/usr", "/boot", "/var", "/home") for a in alvos)
            return categoria, f"Delete {_lista(alvos, 3)}", "high" if recursivo and sistema else "medium"
        if prog in ("nano", "vim", "vi", "nvim"):
            return categoria, f"Edit {_lista(alvos, 2)}", "medium"
        if prog == "tee":
            return categoria, f"Write {_lista(alvos, 2)}", "medium"
        if prog in ("chmod", "chown", "chgrp", "setfacl", "chattr"):
            return categoria, f"Change permissions of {_lista(alvos[1:] or alvos, 2)}", "medium"
        if prog == "sed":
            return categoria, f"Edit {_lista(alvos[-1:], 1)} in place" if any(a.startswith("-i") for a in args) \
                else "Filter text with sed", "medium"
        if prog in ("cp", "mv", "install", "rsync", "ln"):
            return categoria, f"Put files in {_lista(alvos[-1:], 1)}", "medium"
        return categoria, f"{prog} {_lista(alvos, 2)}", "medium"
    if categoria == "Disks & storage":
        if prog.startswith("mkfs") or prog in ("wipefs", "dd", "fdisk", "gdisk", "sgdisk", "parted", "cryptsetup"):
            return categoria, f"Format or partition a disk ({prog} {_lista(alvos, 2)})", "high"
        if prog == "btrfs" and args[:2] in (["subvolume", "list"], ["filesystem", "show"], ["filesystem", "usage"],
                                            ["filesystem", "df"], ["device", "stats"]):
            return "Inspect (read-only)", f"Look at the disk (btrfs {' '.join(args[:2])})", "low"
        if prog in ("swapon", "swapoff", "mkswap", "zramctl"):
            return categoria, f"Change swap ({prog} {_lista(alvos, 2)})", "medium"
        if prog in ("mount", "umount"):
            return categoria, f"{'Mount' if prog == 'mount' else 'Unmount'} {_lista(alvos, 2)}", "medium"
        return categoria, f"{prog} {' '.join(args[:3])}", "medium"
    if categoria == "Kernel & boot":
        if prog == "sysctl" and (not alvos or all("=" not in a for a in alvos)) and "-w" not in args \
                and "--system" not in args and "-p" not in args:
            return "Inspect (read-only)", "Read kernel settings (sysctl)", "low"
        return categoria, f"{ {'mkinitcpio': 'Rebuild the boot image', 'bootctl': 'Change the boot loader', 'sysctl': 'Change kernel settings', 'modprobe': 'Load a kernel driver', 'rmmod': 'Unload a kernel driver'}.get(prog, prog)} {_lista(alvos, 2) if prog in ('modprobe', 'rmmod', 'sysctl') else ''}".strip(), "medium"
    if categoria == "Users & security":
        return categoria, f"{ {'passwd': 'Change a password', 'visudo': 'Edit sudo rules', 'useradd': 'Create a user', 'userdel': 'Delete a user', 'usermod': 'Change a user', 'gpasswd': 'Change a group'}.get(prog, prog)} {_lista(alvos, 2) if alvos else ''}".strip(), "high"
    if categoria == "Services" and prog in ("kill", "pkill", "killall"):
        return categoria, f"Stop processes: {_lista(alvos, 3)}", "medium"
    if categoria == "Power":
        return categoria, f"Power: {prog} {' '.join(args[:2])}".strip(), "low"
    if categoria == "Network":
        return categoria, f"Network: {prog} {' '.join(args[:3])}".strip(), "medium"
    if categoria == "Inspect (read-only)":
        return categoria, f"Look at {_lista(alvos, 2)}" if alvos else f"Run {prog}", "low"
    return categoria, f"{prog} {' '.join(args[:3])}".strip(), "medium"


# ---------- archive ----------

def dia_de(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def arquivo_do_dia(dia, pasta=None):
    pasta = pasta or ARQUIVO
    if not DIA_RE.match(dia):
        raise ValueError(f"bad day: {dia!r}")
    return os.path.join(pasta, dia + ".jsonl")


def dias(pasta=None):
    pasta = pasta or ARQUIVO
    try:
        return sorted((n[:-6] for n in os.listdir(pasta) if n.endswith(".jsonl") and DIA_RE.match(n[:-6])),
                      reverse=True)
    except OSError:
        return []


def ler_dia(dia, pasta=None):
    pasta = pasta or ARQUIVO
    eventos = []
    try:
        with open(arquivo_do_dia(dia, pasta), encoding="utf-8", errors="replace") as f:
            for linha in f:
                try:
                    ev = json.loads(linha)
                except ValueError:
                    continue
                if isinstance(ev, dict) and ID_RE.match(str(ev.get("id", ""))):
                    eventos.append(ev)
    except OSError:
        pass
    return eventos


class Trava:
    """flock on the archive: one writer at a time (x9d appending, x9-admin deleting)."""

    def __init__(self, pasta=None):
        pasta = pasta or ARQUIVO
        self.caminho = os.path.join(pasta, ".lock")

    def __enter__(self):
        self.fd = os.open(self.caminho, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        os.close(self.fd)


def _abrir_arquivo(caminho, modo, gid):
    fd = os.open(caminho, modo | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    try:
        if gid is not None and os.fstat(fd).st_gid != gid:
            os.fchown(fd, 0, gid)
        os.fchmod(fd, 0o640)
    except OSError:
        pass
    return fd


def acrescentar(eventos, pasta=None, gid=None):
    """Appends events to their day files. Call it inside Trava."""
    pasta = pasta or ARQUIVO
    por_dia = {}
    for ev in eventos:
        por_dia.setdefault(dia_de(ev["ts"]), []).append(ev)
    for dia, lista in por_dia.items():
        fd = _abrir_arquivo(arquivo_do_dia(dia, pasta), os.O_WRONLY | os.O_CREAT | os.O_APPEND, gid)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            for ev in lista:
                f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")


def regravar_dia(dia, eventos, pasta=None, gid=None):
    """Rewrites one day (after deleting entries). Empty day = file removed. Call it inside Trava."""
    pasta = pasta or ARQUIVO
    caminho = arquivo_do_dia(dia, pasta)
    if not eventos:
        try:
            os.unlink(caminho)
        except FileNotFoundError:
            pass
        return
    tmp = caminho + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = _abrir_arquivo(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, gid)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for ev in eventos:
            f.write(json.dumps(ev, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, caminho)


# ---------- auto-delete ----------

RETENCAO_PADRAO = {"keep_days": 90, "rules": []}
UNIDADES = {"hours": 3600, "days": 86400}


def validar_retencao(dados):
    """Checks auto-delete settings strictly (they come from the app through a root helper).
    {"keep_days": 0..3650 (0 = forever), "rules": [{"category": "...", "result": "...", "after": N,
    "unit": "hours"|"days"}]}. A rule's category/result may be "any"."""
    if not isinstance(dados, dict) or set(dados) - {"keep_days", "rules"}:
        raise ValueError("unknown fields")
    dias_ = dados.get("keep_days", 90)
    if not isinstance(dias_, int) or isinstance(dias_, bool) or not 0 <= dias_ <= 3650:
        raise ValueError("keep_days must be 0..3650")
    regras = dados.get("rules", [])
    if not isinstance(regras, list) or len(regras) > 50:
        raise ValueError("rules must be a list (max 50)")
    limpas = []
    for r in regras:
        if not isinstance(r, dict) or set(r) - {"category", "result", "after", "unit"}:
            raise ValueError("bad rule")
        cat, res = r.get("category", "any"), r.get("result", "any")
        depois, unidade = r.get("after"), r.get("unit", "days")
        if cat != "any" and cat not in CATEGORIAS:
            raise ValueError(f"unknown category: {cat}")
        if res != "any" and res not in RESULTADOS:
            raise ValueError(f"unknown result: {res}")
        if unidade not in UNIDADES:
            raise ValueError("unit must be hours or days")
        if not isinstance(depois, int) or isinstance(depois, bool) or not 1 <= depois <= 87600:
            raise ValueError("after must be 1..87600")
        limpas.append({"category": cat, "result": res, "after": depois, "unit": unidade})
    return {"keep_days": dias_, "rules": limpas}


def ler_retencao(caminho=None):
    caminho = caminho or RETENCAO
    try:
        with open(caminho, encoding="utf-8") as f:
            return validar_retencao(json.load(f))
    except (OSError, ValueError):
        return dict(RETENCAO_PADRAO)


def vence(ev, retencao, agora):
    """True if the event should be deleted now."""
    idade = agora - float(ev.get("ts", agora))
    if retencao["keep_days"] and idade > retencao["keep_days"] * 86400:
        return True
    for r in retencao["rules"]:
        if (r["category"] in ("any", ev.get("category"))) and (r["result"] in ("any", ev.get("result"))) \
                and idade > r["after"] * UNIDADES[r["unit"]]:
            return True
    return False


def aplicar_retencao(retencao, agora=None, pasta=None, gid=None):
    """Deletes what expired. Returns how many events went away. Call it inside Trava."""
    pasta = pasta or ARQUIVO
    agora = agora if agora is not None else time.time()
    removidos = 0
    for dia in dias(pasta):
        eventos = ler_dia(dia, pasta)
        ficam = [e for e in eventos if not vence(e, retencao, agora)]
        if len(ficam) != len(eventos):
            removidos += len(eventos) - len(ficam)
            regravar_dia(dia, ficam, pasta, gid)
    return removidos


# ---------- app password (a lock for the screen, not encryption) ----------

TRAVAS = os.path.join(ESTADO, "locks")
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1, "maxmem": 64 * 1024 * 1024, "dklen": 32}


def hash_senha(senha, sal=None):
    sal = sal or os.urandom(16)
    h = hashlib.scrypt(senha.encode("utf-8"), salt=sal, **SCRYPT)
    return {"v": 1, "salt": sal.hex(), "hash": h.hex()}


def confere_senha(senha, registro):
    import hmac
    try:
        sal = bytes.fromhex(registro["salt"])
        esperado = bytes.fromhex(registro["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    return hmac.compare_digest(hashlib.scrypt(senha.encode("utf-8"), salt=sal, **SCRYPT), esperado)


def arquivo_trava(uid, pasta=None):
    pasta = pasta or TRAVAS
    return os.path.join(pasta, f"{int(uid)}.json")


def ler_trava(uid, pasta=None):
    pasta = pasta or TRAVAS
    try:
        with open(arquivo_trava(uid, pasta), encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, dict) and "hash" in dados else None
    except (OSError, ValueError):
        return None


def gravar_trava(uid, gid, registro, pasta=None):
    pasta = pasta or TRAVAS
    caminho = arquivo_trava(uid, pasta)
    tmp = caminho + ".tmp"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o640)
    with os.fdopen(fd, "w") as f:
        json.dump(registro, f)
        try:
            os.fchown(f.fileno(), 0, gid)
        except OSError:
            pass
    os.replace(tmp, caminho)


def senha_boa(senha):
    """Rules for a new password: 4..256 chars, no control characters."""
    if not isinstance(senha, str) or not 4 <= len(senha) <= 256:
        return "Use 4 to 256 characters"
    if any(ord(c) < 32 for c in senha):
        return "No control characters"
    return ""


# ---------- filters (used by the app) ----------

HORA_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def minutos(hhmm):
    """'07:30' -> 450; '' -> None; invalid -> ValueError."""
    if not hhmm:
        return None
    m = HORA_RE.match(hhmm.strip())
    if not m:
        raise ValueError(f"bad time: {hhmm!r} (use HH:MM)")
    return int(m.group(1)) * 60 + int(m.group(2))


def passa(ev, f):
    """True if the event matches every filter that is set. Filters combine (AND):
    day_from/day_to = 'YYYY-MM-DD' (inclusive), time_from/time_to = 'HH:MM' on any day
    (22:00 -> 06:00 crosses midnight), category/result/risk/app = exact value, text = in any field."""
    ts = float(ev.get("ts", 0))
    lt = time.localtime(ts)
    dia = time.strftime("%Y-%m-%d", lt)
    if f.get("day_from") and dia < f["day_from"]:
        return False
    if f.get("day_to") and dia > f["day_to"]:
        return False
    de, ate = minutos(f.get("time_from", "")), minutos(f.get("time_to", ""))
    if de is not None or ate is not None:
        agora = lt.tm_hour * 60 + lt.tm_min
        de = 0 if de is None else de
        ate = 24 * 60 - 1 if ate is None else ate
        dentro = de <= agora <= ate if de <= ate else (agora >= de or agora <= ate)
        if not dentro:
            return False
    for chave in ("category", "result", "risk", "app"):
        if f.get(chave) and ev.get(chave) != f[chave]:
            return False
    texto = (f.get("text") or "").strip().lower()
    if texto:
        alvo = " ".join(str(ev.get(k, "")) for k in ("purpose", "command", "app", "user", "cwd", "reason",
                                                    "category", "tool")).lower()
        if not all(p in alvo for p in texto.split()):
            return False
    return True
