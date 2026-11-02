use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use crate::fsinfo;

fn has(argv: &[String], bad: &[&str]) -> Option<String> {
    argv.iter()
        .find(|a| bad.iter().any(|b| a.as_str() == *b || a.starts_with(&format!("{b}="))))
        .cloned()
}

fn first_word(argv: &[String]) -> Option<&str> {
    argv.iter().skip(1).find(|a| !a.starts_with('-')).map(|s| s.as_str())
}

fn sub_in(argv: &[String], allowed: &[&str], bare_ok: bool) -> Result<(), String> {
    match first_word(argv) {
        None if bare_ok => Ok(()),
        Some(s) if allowed.contains(&s) => Ok(()),
        other => Err(format!(
            "{} subcommand {:?} is not read-only; allowed: {}",
            argv[0],
            other.unwrap_or(""),
            allowed.join(", ")
        )),
    }
}

/// Checks a read-only command and returns the argv to run, with safe flags added.
pub fn readonly(argv: &[String]) -> Result<Vec<String>, String> {
    let Some(exe) = argv.first() else {
        return Err("empty command".into());
    };
    if exe.contains('/') {
        return Err("use the bare program name; to read a file use read_file".into());
    }
    if argv.iter().any(|a| ["|", ">", ">>", "&&", ";", "||"].contains(&a.as_str()) || a.contains("$(")) {
        return Err("no shell here: no pipes or redirects. Run one program; its whole output comes back, so count or filter it yourself".into());
    }
    let mut out = argv.to_vec();
    match exe.as_str() {
        "df" | "free" | "lsblk" | "findmnt" | "uname" | "uptime" | "lscpu" | "lspci" | "lsusb"
        | "ps" | "ss" | "sensors" | "which" | "whereis" | "whoami" | "id" | "date" | "stat" | "ls" | "file"
        | "wc" | "pgrep" | "zramctl" | "readlink" | "realpath" | "nproc" | "groups" | "printenv"
        | "fc-list" | "lsmod" | "modinfo" | "vulkaninfo" | "glxinfo" | "inxi" | "tty" | "getent" => {
            if exe == "ps" || exe == "ss" {
                if let Some(a) = has(argv, &["-K", "--kill"]) {
                    return Err(format!("{a} is not allowed"));
                }
            }
            if exe == "findmnt" || exe == "lsblk" {
                if let Some(a) = has(argv, &["--poll"]) {
                    return Err(format!("{a} is not allowed"));
                }
            }
            if exe == "date" {
                if let Some(a) = has(argv, &["-s", "--set"]) {
                    return Err(format!("{a} is not allowed"));
                }
            }
            if exe == "printenv" && argv.len() != 2 {
                return Err("printenv needs exactly one variable name".into());
            }
            if exe == "printenv" {
                let name = argv[1].to_uppercase();
                if ["KEY", "TOKEN", "SECRET", "PASS"].iter().any(|s| name.contains(s)) {
                    return Err("secret variables are not readable".into());
                }
            }
        }
        "grep" => {
            if let Some(a) = has(argv, &["-f", "--file"]) {
                return Err(format!("grep {a} is not allowed"));
            }
            let mut pos = vec![];
            let mut has_e = false;
            let mut skip = false;
            for a in argv.iter().skip(1) {
                if skip {
                    skip = false;
                    continue;
                }
                if a == "-e" || a == "--regexp" {
                    has_e = true;
                    skip = true;
                    continue;
                }
                if ["-m", "-A", "-B", "-C", "--max-count", "--after-context", "--before-context", "--context"].contains(&a.as_str()) {
                    skip = true;
                    continue;
                }
                if a.starts_with("-e") || a.starts_with("--regexp=") {
                    has_e = true;
                    continue;
                }
                if !a.starts_with('-') {
                    pos.push(a);
                }
            }
            let paths: Vec<&String> = if has_e { pos } else { pos.into_iter().skip(1).collect() };
            if paths.is_empty() {
                return Err("grep needs a pattern and at least one file or folder path".into());
            }
            for p in paths {
                readable_path(p)?;
            }
        }
        "head" => {
            let mut paths = vec![];
            let mut skip = false;
            for a in argv.iter().skip(1) {
                if skip {
                    skip = false;
                    continue;
                }
                if ["-n", "-c", "--lines", "--bytes"].contains(&a.as_str()) {
                    skip = true;
                    continue;
                }
                if !a.starts_with('-') {
                    paths.push(a);
                }
            }
            if paths.is_empty() {
                return Err("head needs at least one file path".into());
            }
            for p in paths {
                readable_path(p)?;
            }
        }
        "find" => {
            if let Some(a) = has(argv, &["-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"]) {
                return Err(format!("find {a} is not read-only"));
            }
        }
        "du" => {
            if !argv.iter().any(|a| a == "-x") {
                out.insert(1, "-x".into());
            }
        }
        "journalctl" => {
            if let Some(a) = has(argv, &["-f", "--follow", "--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var", "--setup-keys", "--update-catalog", "--vacuum-size", "--vacuum-time", "--vacuum-files"]) {
                return Err(format!("{a} is not allowed"));
            }
            out.insert(1, "--no-pager".into());
            if !argv.iter().any(|a| a == "-n" || a.starts_with("--lines") || a.starts_with("-n")) {
                out.insert(1, "--lines=200".into());
            }
        }
        "systemctl" => {
            sub_in(argv, &["status", "list-units", "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "is-active", "is-enabled", "is-failed", "show", "cat", "get-default", "is-system-running"], true)?;
            out.insert(1, "--no-pager".into());
        }
        "pacman" => {
            let flag = argv.get(1).map(String::as_str).unwrap_or("");
            let ok = flag.starts_with("-Q")
                || ["-Si", "-Sii", "-Ss", "-Sg", "-Sl", "-F", "-Fl", "-Fx", "-T"].contains(&flag);
            if !ok {
                return Err("pacman here only takes -Q*, -Si, -Ss, -Sg, -Sl, -F, -Fl, -Fx and -T".into());
            }
        }
        "paccache" => {
            if argv.get(1).map(String::as_str) != Some("-d") && argv.get(1).map(String::as_str) != Some("-dv") {
                return Err("paccache here only takes -d".into());
            }
        }
        "checkupdates" | "pactree" | "swapon" => {
            if exe == "swapon" && argv.get(1).map(String::as_str) != Some("--show") {
                return Err("swapon here only takes --show".into());
            }
        }
        "ip" => {
            if let Some(a) = has(argv, &["add", "del", "delete", "set", "flush", "change", "replace", "append", "exec"]) {
                return Err(format!("ip {a} is not read-only"));
            }
        }
        "nmcli" => {
            if let Some(a) = has(argv, &["up", "down", "add", "delete", "del", "modify", "mod", "connect", "disconnect", "reapply", "on", "off", "load", "reload", "rescan"]) {
                return Err(format!("nmcli {a} is not read-only"));
            }
        }
        "pactl" => {
            let sub = first_word(argv).unwrap_or("");
            if !(sub == "info" || sub == "list" || sub.starts_with("get-")) {
                return Err("pactl here only takes info, list and get-*".into());
            }
        }
        "wpctl" => sub_in(argv, &["status", "inspect", "get-volume"], true)?,
        "hyprctl" => sub_in(argv, &["monitors", "clients", "activewindow", "activeworkspace", "workspaces", "version", "devices", "binds", "getoption", "layers", "animations", "cursorpos", "configerrors", "decorations", "systeminfo", "globalshortcuts"], false)?,
        "bluetoothctl" => sub_in(argv, &["show", "devices", "info", "list"], false)?,
        "timedatectl" => sub_in(argv, &["status", "show", "timesync-status", "show-timesync", "list-timezones"], true)?,
        "hostnamectl" => sub_in(argv, &["status"], true)?,
        "resolvectl" => sub_in(argv, &["status", "dns", "domain", "query", "statistics"], true)?,
        "loginctl" => sub_in(argv, &["list-sessions", "list-users", "session-status", "show-session", "user-status", "show-user"], true)?,
        "coredumpctl" => sub_in(argv, &["list", "info"], true)?,
        "upower" => {
            let flag = argv.get(1).map(String::as_str).unwrap_or("");
            if !["-d", "-e", "-i", "--dump", "--enumerate"].contains(&flag) {
                return Err("upower here only takes -d, -e and -i".into());
            }
        }
        "powerprofilesctl" => sub_in(argv, &["get", "list"], true)?,
        "snapper" => sub_in(argv, &["list", "list-configs"], false)?,
        "btrfs" => {
            let words: Vec<&str> = argv.iter().skip(1).filter(|a| !a.starts_with('-')).map(|s| s.as_str()).collect();
            let ok = matches!(words.as_slice(), ["filesystem" | "fi", "df" | "usage" | "show" | "du", ..] | ["subvolume" | "sub", "list" | "show", ..]);
            if !ok {
                return Err("btrfs here only takes filesystem df/usage/show/du and subvolume list/show".into());
            }
        }
        "sysctl" => {
            if argv.iter().skip(1).any(|a| a.contains('=') || a == "-w" || a == "-p" || a.starts_with("--write") || a == "--system" || a.starts_with("--load")) {
                return Err("sysctl here is read-only".into());
            }
        }
        "dmesg" => {
            if let Some(a) = has(argv, &["-c", "-C", "-D", "-E", "-n", "--clear", "--read-clear", "--console-off", "--console-on", "--console-level", "-w", "--follow"]) {
                return Err(format!("{a} is not allowed"));
            }
        }
        "nvidia-smi" => {
            if !argv.iter().skip(1).all(|a| a == "-L" || a == "-q" || a == "--list-gpus" || a == "--query" || a.starts_with("--query-") || a.starts_with("--format=") || a == "-d") {
                return Err("nvidia-smi here only takes query options".into());
            }
        }
        "ping" => {
            out.insert(1, "3".into());
            out.insert(1, "-c".into());
        }
        "flatpak" => sub_in(argv, &["list", "info", "remotes", "history"], false)?,
        "brilho" => {
            if argv != ["brilho", "--ler"] {
                return Err("brilho here only takes --ler".into());
            }
        }
        "aero-energia" => {
            if argv != ["aero-energia", "--status"] && argv != ["aero-energia", "--waybar"] {
                return Err("aero-energia here only takes --status or --waybar".into());
            }
        }
        "aero-hz" => {
            if argv != ["aero-hz", "--status"] {
                return Err("aero-hz here only takes --status".into());
            }
        }
        "aero-visual" | "notificacoes-estado" => {
            if argv.len() != 1 {
                return Err(format!("{exe} here takes no arguments"));
            }
        }
        "aero-audio" => {
            if !matches!(argv.get(1).map(String::as_str), Some("list" | "-h" | "--help")) || argv.len() != 2 {
                return Err("aero-audio here only takes list or --help".into());
            }
        }
        "aero-bluetooth" => {
            if !matches!(argv.get(1).map(String::as_str), Some("-h" | "--help")) || argv.len() != 2 {
                return Err("aero-bluetooth here only takes --help".into());
            }
        }
        "hermesctl" => sub_in(argv, &["mode", "history"], false)?,
        _ => return Err(format!("{exe} is not on the read-only list; propose it with propose_command instead")),
    }
    Ok(out)
}

const SECRET_PARTS: &[&str] = &[
    ".ssh", ".gnupg", ".password-store", "shadow", "gshadow", "sudoers", ".env", "token", "secret", "credential",
    "private", ".key", "_key", "keyring", "cookies", "login data", ".netrc", ".git-credentials", ".pem", "password", "senha",
];

pub fn readable_path(path: &str) -> Result<(), String> {
    let lower = path.to_lowercase();
    if let Some(part) = SECRET_PARTS.iter().find(|p| lower.contains(*p)) {
        return Err(format!("{path} may hold secrets ({part}), so it is not readable"));
    }
    if ["/proc/kcore", "/dev/", "/sys/firmware"].iter().any(|p| lower.starts_with(p)) {
        return Err(format!("{path} is not readable here"));
    }
    Ok(())
}

pub enum Risk {
    Normal,
    High(String),
}

fn program_exists(name: &str) -> bool {
    Command::new("sh")
        .args(["-c", "command -v -- \"$0\"", name])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn quiet_ok(exe: &str, args: &[&str]) -> bool {
    Command::new(exe)
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn segments(command: &str) -> Vec<Vec<String>> {
    let mut parts = vec![];
    for chunk in command.replace("&&", ";").replace("||", ";").replace('|', ";").split(';') {
        let words: Vec<String> = chunk.split_whitespace().map(|w| w.trim_matches(|c| c == '"' || c == '\'').to_string()).collect();
        if !words.is_empty() {
            parts.push(words);
        }
    }
    parts
}

fn check_pacman(words: &[String]) -> Result<(), String> {
    let Some(flag) = words.get(1) else {
        return Ok(());
    };
    if flag.starts_with("-S") && !flag.starts_with("--") {
        let f = &flag[1..];
        if f.contains('y') && !f.contains('u') {
            return Err("partial upgrade: use pacman -Syu, never -Sy alone".into());
        }
        if f.contains('i') || f.contains('s') || f.contains('c') || f.contains('g') || f.contains('l') {
            return Ok(());
        }
        for pkg in words.iter().skip(2).filter(|w| !w.starts_with('-')) {
            if !quiet_ok("pacman", &["-Si", pkg]) && !quiet_ok("pacman", &["-Sg", pkg]) {
                return Err(format!("package {pkg} is not in the repositories; search with pacman -Ss"));
            }
        }
    }
    if flag.starts_with("-R") {
        for pkg in words.iter().skip(2).filter(|w| !w.starts_with('-') && !w.contains('$') && !w.contains(')')) {
            if !quiet_ok("pacman", &["-Q", pkg]) {
                return Err(format!("package {pkg} is not installed"));
            }
        }
    }
    Ok(())
}

fn check_systemctl(words: &[String]) -> Result<(), String> {
    let user = words.iter().any(|w| w == "--user");
    let args: Vec<&String> = words.iter().skip(1).filter(|w| !w.starts_with('-')).collect();
    let Some(sub) = args.first() else {
        return Ok(());
    };
    if ["start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "try-restart"].contains(&sub.as_str()) {
        for unit in args.iter().skip(1) {
            let mut check = vec![];
            if user {
                check.push("--user");
            }
            check.extend(["cat", unit.as_str()]);
            if !quiet_ok("systemctl", &check) {
                return Err(format!("unit {unit} does not exist{}; check with systemctl list-unit-files", if user { " for this user" } else { " in the system manager" }));
            }
        }
    }
    Ok(())
}

fn needs_root(words: &[&str]) -> bool {
    let Some((exe, rest)) = words.split_first() else {
        return false;
    };
    let first = rest.first().copied().unwrap_or("");
    match *exe {
        "pacman" => {
            let read_only = first.starts_with("-Q") || ["-Si", "-Sii", "-Ss", "-Sg", "-Sl", "-F", "-Fl", "-T"].contains(&first);
            first.starts_with('-') && !read_only
        }
        "systemctl" => {
            !rest.contains(&"--user")
                && rest.iter().find(|w| !w.starts_with('-')).is_some_and(|sub| ["start", "stop", "restart", "reload", "enable", "disable", "mask", "unmask", "try-restart", "daemon-reload"].contains(sub))
        }
        "paccache" => first.starts_with("-r") || first.starts_with("-u") || first.starts_with("-k"),
        _ => false,
    }
}

/// Fixes common slips: missing sudo, and flags that skip the user's review.
pub fn normalize(command: &str) -> String {
    command
        .split("&&")
        .map(|part| {
            let words: Vec<&str> = part.split_whitespace().filter(|w| *w != "--noconfirm").collect();
            let line = words.join(" ");
            if needs_root(&words) {
                format!("sudo {line}")
            } else {
                line
            }
        })
        .collect::<Vec<_>>()
        .join(" && ")
}

#[derive(Clone, Debug)]
enum ShToken {
    Word(String, bool),
    Op(String),
}

fn tokenize(cmd: &str) -> Result<Vec<ShToken>, String> {
    let mut tokens = vec![];
    let mut chars = cmd.chars().peekable();
    while let Some(&c) = chars.peek() {
        if c.is_whitespace() {
            chars.next();
            continue;
        }
        if c == ';' || c == '&' || c == '|' {
            chars.next();
            if (c == '&' && chars.peek() == Some(&'&')) || (c == '|' && chars.peek() == Some(&'|')) {
                chars.next();
                tokens.push(ShToken::Op(format!("{c}{c}")));
            } else {
                tokens.push(ShToken::Op(c.to_string()));
            }
            continue;
        }
        let mut word = String::new();
        let mut is_pure_single = false;
        let mut parts = 0;
        while let Some(&ch) = chars.peek() {
            if ch.is_whitespace() || ch == ';' || ch == '&' || ch == '|' {
                break;
            }
            chars.next();
            parts += 1;
            if ch == '\'' {
                let mut lit = String::new();
                let mut closed = false;
                for sc in chars.by_ref() {
                    if sc == '\'' {
                        closed = true;
                        break;
                    }
                    lit.push(sc);
                }
                if !closed {
                    return Err("unclosed single quote in command".into());
                }
                word.push_str(&lit);
                if parts == 1 && chars.peek().map_or(true, |nc| nc.is_whitespace() || *nc == ';' || *nc == '&' || *nc == '|') {
                    is_pure_single = true;
                }
            } else if ch == '"' {
                let mut closed = false;
                while let Some(dc) = chars.next() {
                    if dc == '"' {
                        closed = true;
                        break;
                    } else if dc == '\\' {
                        if let Some(esc) = chars.next() {
                            word.push(esc);
                        }
                    } else {
                        word.push(dc);
                    }
                }
                if !closed {
                    return Err("unclosed double quote in command".into());
                }
            } else if ch == '\\' {
                if let Some(esc) = chars.next() {
                    word.push(esc);
                }
            } else {
                word.push(ch);
            }
        }
        tokens.push(ShToken::Word(word, is_pure_single));
    }
    Ok(tokens)
}

fn pattern_matches(pat: &str, s: &str) -> bool {
    let (p_bytes, s_bytes) = (pat.as_bytes(), s.as_bytes());
    let (mut pi, mut si) = (0, 0);
    let (mut p_star, mut s_star) = (None, 0);
    while si < s_bytes.len() {
        if pi < p_bytes.len() && (p_bytes[pi] == b'?' || p_bytes[pi] == s_bytes[si]) {
            pi += 1;
            si += 1;
        } else if pi < p_bytes.len() && p_bytes[pi] == b'*' {
            p_star = Some(pi);
            pi += 1;
            s_star = si;
        } else if let Some(star) = p_star {
            pi = star + 1;
            s_star += 1;
            si = s_star;
        } else {
            return false;
        }
    }
    while pi < p_bytes.len() && p_bytes[pi] == b'*' {
        pi += 1;
    }
    pi == p_bytes.len()
}

fn expand_word(word: &str, is_literal: bool, cwd: &Path) -> Result<String, String> {
    if is_literal {
        return Ok(word.to_string());
    }
    if word.contains('`') || word.contains("$(") || word.contains("${(") {
        return Err(format!("command substitution in '{word}' cannot be resolved with certainty"));
    }
    let home = fsinfo::home();
    let mut out = String::new();
    let mut chars = word.chars().peekable();

    if chars.peek() == Some(&'~') {
        chars.next();
        let mut user = String::new();
        while let Some(&c) = chars.peek() {
            if c == '/' {
                break;
            }
            user.push(c);
            chars.next();
        }
        if user.is_empty() {
            out.push_str(&home);
        } else {
            match fsinfo::user_home(&user) {
                Some(uh) => out.push_str(&uh),
                None => return Err(format!("user '~{user}' cannot be resolved with certainty")),
            }
        }
    }

    while let Some(c) = chars.next() {
        if c == '$' {
            let mut var_name = String::new();
            if chars.peek() == Some(&'{') {
                chars.next();
                for vc in chars.by_ref() {
                    if vc == '}' {
                        break;
                    }
                    var_name.push(vc);
                }
            } else {
                while let Some(&vc) = chars.peek() {
                    if vc.is_alphanumeric() || vc == '_' {
                        var_name.push(vc);
                        chars.next();
                    } else {
                        break;
                    }
                }
            }
            if var_name.is_empty() {
                out.push('$');
            } else {
                match var_name.as_str() {
                    "HOME" => out.push_str(&home),
                    "USER" => out.push_str(&fsinfo::user_name(fsinfo::uid())),
                    "UID" => out.push_str(&fsinfo::uid().to_string()),
                    "PWD" => out.push_str(&cwd.to_string_lossy()),
                    other => {
                        if let Ok(val) = std::env::var(other) {
                            out.push_str(&val);
                        } else {
                            return Err(format!("environment variable '${other}' cannot be resolved with certainty"));
                        }
                    }
                }
            }
        } else {
            out.push(c);
        }
    }
    Ok(out)
}

fn resolve_targets(raw_path: &str, is_literal: bool, cwd: &Path) -> Result<Vec<PathBuf>, String> {
    let expanded = expand_word(raw_path, is_literal, cwd)?;
    let full = if Path::new(&expanded).is_relative() {
        cwd.join(&expanded)
    } else {
        PathBuf::from(&expanded)
    };
    let clean = fsinfo::clean_path(&full);
    let s = clean.to_string_lossy().to_string();
    let home = fsinfo::home();

    if !is_literal && (expanded.contains('*') || expanded.contains('?') || expanded.contains('[')) {
        if s.starts_with("/tmp/claude-") || s == "/tmp/claude*" || s == "/tmp/claude" {
            return Err(format!("glob '{s}' targets protected /tmp/claude-*"));
        }
        if !home.is_empty() && (s == format!("{home}/.claude*") || s.starts_with(&format!("{home}/.claude/")) || s == format!("{home}/.claude")) {
            return Err(format!("glob '{s}' targets protected ~/.claude"));
        }
        if s == "/*" || s == "/.*" || s == "/*/*" {
            return Err(format!("glob '{s}' targets root system directories"));
        }
        if s == "/home/*" || s == "/home/.*" || s == "/home/*/*" {
            return Err(format!("glob '{s}' targets home folders"));
        }
        if !home.is_empty() && (s == format!("{home}/*") || s == format!("{home}/.*") || s == format!("{home}/*/*")) {
            return Err(format!("glob '{s}' targets all top-level folders of $HOME"));
        }
        if s == "/mnt/d/*" || s == "/mnt/d/.*" || s == "/mnt/d/*/*" {
            return Err(format!("glob '{s}' targets all top-level folders of /mnt/d"));
        }
        if s == "/etc/*" || s == "/usr/*" || s == "/boot/*" || s == "/var/*" {
            return Err(format!("glob '{s}' targets protected system directories"));
        }

        let comps: Vec<&str> = expanded.split('/').collect();
        let mut candidates = if expanded.starts_with('/') {
            vec![PathBuf::from("/")]
        } else {
            vec![cwd.to_path_buf()]
        };
        for comp in comps {
            if comp.is_empty() {
                continue;
            }
            if !comp.contains('*') && !comp.contains('?') && !comp.contains('[') {
                candidates = candidates.into_iter().map(|c| c.join(comp)).collect();
            } else {
                let mut next = vec![];
                for cand in candidates {
                    if let Ok(entries) = fs::read_dir(&cand) {
                        for e in entries.flatten() {
                            let name = e.file_name().to_string_lossy().to_string();
                            if pattern_matches(comp, &name) {
                                next.push(cand.join(&name));
                            }
                        }
                    }
                }
                candidates = next;
            }
        }
        if candidates.is_empty() {
            return Err(format!("glob '{expanded}' matches no files and cannot be resolved with certainty"));
        }
        return Ok(candidates.into_iter().map(|c| fsinfo::clean_path(&c)).collect());
    }

    Ok(vec![clean])
}

fn check_target_safety(p: &Path, is_recursive: bool) -> Result<(), String> {
    let clean = fsinfo::clean_path(p);
    let s = clean.to_string_lossy().to_string();
    let home = fsinfo::home();

    // 1. /tmp/claude-* and ~/.claude
    if s.starts_with("/tmp/claude-") || s == "/tmp/claude" || s.starts_with("/tmp/claude/") {
        return Err(format!("{s} is protected (/tmp/claude-*)"));
    }
    if !home.is_empty() && (s == format!("{home}/.claude") || fsinfo::is_within(&s, &format!("{home}/.claude"))) {
        return Err(format!("{s} is protected (~/.claude)"));
    }

    // 2. Exact protected system directories and $HOME
    if s == "/" || s == "/home" || s == "/etc" || s == "/usr" || s == "/boot" || s == "/var" {
        return Err(format!("{s} is a protected system directory"));
    }
    if !home.is_empty() && s == home {
        return Err(format!("{s} is the user home directory"));
    }

    // 3. Top-level folder of $HOME or /mnt/d
    if !home.is_empty() && clean.parent() == Some(Path::new(&home)) && s != home {
        let is_dir = clean.is_dir() || is_recursive || !clean.exists();
        if is_dir {
            return Err(format!("{s} is a top-level folder of $HOME"));
        }
    }
    if clean.parent() == Some(Path::new("/mnt/d")) && s != "/mnt/d" {
        let is_dir = clean.is_dir() || is_recursive || !clean.exists();
        if is_dir {
            return Err(format!("{s} is a top-level folder of /mnt/d"));
        }
    }

    // 4. Mount points
    let mounts = fsinfo::mounts();
    if mounts.iter().any(|m| m.point == s) {
        return Err(format!("{s} is a mount point"));
    }

    // 5. Symlinks
    if let Ok(meta) = fs::symlink_metadata(&clean) {
        if meta.file_type().is_symlink() {
            if let Ok(target) = fs::read_link(&clean) {
                let resolved_target = if target.is_relative() {
                    fsinfo::clean_path(&clean.parent().unwrap_or(Path::new("/")).join(target))
                } else {
                    fsinfo::clean_path(&target)
                };
                check_target_safety(&resolved_target, is_recursive)?;
            }
        }
    }
    if let Ok(canon) = fs::canonicalize(&clean) {
        if canon != clean {
            check_target_safety(&canon, is_recursive)?;
        }
    }

    // 6. Running processes using the path
    let (uses, _) = fsinfo::process_uses();
    for u in &uses {
        if u.path == s {
            return Err(format!("running process '{}' (PID {}) uses {s} ({})", u.name, u.pid, u.how));
        }
        if (is_recursive || clean.is_dir()) && fsinfo::is_within(&u.path, &s) {
            return Err(format!("running process '{}' (PID {}) uses {} inside {s}", u.name, u.pid, u.path));
        }
    }

    Ok(())
}

fn check_single_command(words: &[(String, bool)], cwd: &mut PathBuf, cwd_uncertain: &mut bool) -> Result<(), String> {
    let mut w = words;
    while let Some((word, _)) = w.first() {
        if word == "sudo" || word == "env" || word.contains('=') {
            w = &w[1..];
        } else {
            break;
        }
    }
    while w.first().is_some_and(|(word, _)| word.starts_with('-')) {
        w = &w[1..];
    }
    let Some((exe, _)) = w.first() else {
        return Ok(());
    };

    if exe == "cd" {
        if let Some((target, lit)) = w.get(1) {
            match expand_word(target, *lit, cwd) {
                Ok(expanded) => {
                    let full = if Path::new(&expanded).is_relative() {
                        cwd.join(&expanded)
                    } else {
                        PathBuf::from(&expanded)
                    };
                    *cwd = fsinfo::clean_path(&full);
                }
                Err(_) => {
                    *cwd_uncertain = true;
                }
            }
        } else {
            *cwd = PathBuf::from(fsinfo::home());
        }
        return Ok(());
    }

    if ["sh", "bash", "zsh", "dash"].contains(&exe.as_str()) {
        for (i, (arg, _)) in w.iter().enumerate() {
            if arg == "-c" {
                if let Some((script, _)) = w.get(i + 1) {
                    check_command_chain(script, cwd)?;
                } else {
                    return Err("shell -c without command".into());
                }
            }
        }
        return Ok(());
    }

    if exe == "xargs" {
        let rest: Vec<&str> = w.iter().skip(1).filter(|(a, _)| !a.starts_with('-')).map(|(a, _)| a.as_str()).collect();
        if let Some(sub) = rest.first() {
            if ["rm", "rmdir", "unlink", "shred"].contains(sub) {
                return Err("xargs rm targets cannot be resolved with certainty".into());
            }
        }
        return Ok(());
    }

    if exe == "find" {
        let has_delete = w.iter().any(|(a, _)| a == "-delete" || a == "-exec" || a == "-execdir");
        let runs_rm = w.iter().any(|(a, _)| a == "rm" || a == "rmdir" || a == "unlink" || a == "shred");
        if has_delete && (w.iter().any(|(a, _)| a == "-delete") || runs_rm) {
            let roots: Vec<&(String, bool)> = w.iter().skip(1).take_while(|(a, _)| !a.starts_with('-') && !a.starts_with('(')).collect();
            if roots.is_empty() {
                if *cwd_uncertain {
                    return Err("find targets current working directory which is uncertain".into());
                }
                check_target_safety(cwd, true)?;
            } else {
                for (target, lit) in roots {
                    if *cwd_uncertain && Path::new(target).is_relative() {
                        return Err(format!("find target '{target}' has uncertain working directory"));
                    }
                    let paths = resolve_targets(target, *lit, cwd)?;
                    for p in paths {
                        check_target_safety(&p, true)?;
                    }
                }
            }
        }
        return Ok(());
    }

    if ["rm", "rmdir", "unlink", "shred"].contains(&exe.as_str()) {
        let is_recursive = exe == "rmdir" || w.iter().any(|(a, _)| a.starts_with('-') && (a.contains('r') || a.contains('R')));
        let mut operands = vec![];
        let mut past_double_dash = false;
        for (arg, lit) in w.iter().skip(1) {
            if past_double_dash {
                operands.push((arg.clone(), *lit));
            } else if arg == "--" {
                past_double_dash = true;
            } else if arg.starts_with('-') {
                // flags like -rf, -f, --recursive
            } else {
                operands.push((arg.clone(), *lit));
            }
        }
        if operands.is_empty() {
            return Err("destructive command has no target path".into());
        }
        for (operand, lit) in operands {
            if *cwd_uncertain && Path::new(&operand).is_relative() {
                return Err(format!("target '{operand}' has uncertain working directory"));
            }
            let targets = resolve_targets(&operand, lit, cwd)?;
            for t in targets {
                check_target_safety(&t, is_recursive)?;
            }
        }
        return Ok(());
    }

    Ok(())
}

fn check_command_chain(cmd: &str, initial_cwd: &Path) -> Result<(), String> {
    let tokens = tokenize(cmd)?;
    let mut cwd = initial_cwd.to_path_buf();
    let mut cwd_uncertain = false;

    let mut current_words: Vec<(String, bool)> = vec![];
    let mut idx = 0;
    while idx <= tokens.len() {
        let is_end = idx == tokens.len();
        let is_op = !is_end && matches!(tokens[idx], ShToken::Op(_));
        if is_end || is_op {
            if !current_words.is_empty() {
                check_single_command(&current_words, &mut cwd, &mut cwd_uncertain)?;
                current_words.clear();
            }
        } else if let ShToken::Word(ref w, lit) = tokens[idx] {
            current_words.push((w.clone(), lit));
        }
        idx += 1;
    }
    Ok(())
}

pub fn check_destructive_proposal(command: &str) -> Result<(), String> {
    let initial_cwd = std::env::current_dir().unwrap_or_else(|_| PathBuf::from(fsinfo::home()));
    check_command_chain(command, &initial_cwd)
}

/// Commands that replace or lose data without looking destructive.
fn overwrite_risk(compact: &str) -> Option<String> {
    let mut rest = compact.to_string();
    for harmless in ["2>&1", ">&2", "2>/dev/null", ">/dev/null", "> /dev/null", "echo 3 > /proc/sys/vm/drop_caches"] {
        rest = rest.replace(harmless, "");
    }
    if rest.contains('>') {
        return Some("this writes over a file with a redirect".into());
    }
    let words: Vec<&str> = rest.split_whitespace().collect();
    let has = |w: &str| words.contains(&w);
    let reason = if has("mv") && !words.iter().any(|w| *w == "-n" || *w == "--no-clobber") {
        Some("mv can replace an existing target or move a whole folder")
    } else if has("truncate") {
        Some("truncate erases file contents")
    } else if has("tee") {
        Some("tee writes over a file")
    } else if has("sed") && words.iter().any(|w| w.starts_with("-i") || *w == "--in-place") {
        Some("sed -i edits a file in place")
    } else if has("ln") && words.iter().any(|w| w.starts_with('-') && !w.starts_with("--") && w.contains('f') || *w == "--force") {
        Some("ln -f replaces an existing file")
    } else if has("cp") && (has("/dev/null") || !words.iter().any(|w| *w == "-n" || *w == "--no-clobber" || w.starts_with("--update"))) {
        Some("cp can write over an existing file")
    } else if has("git") && ((has("reset") && has("--hard")) || has("clean") || has("restore") || (has("checkout") && has("--"))) {
        Some("this discards git work")
    } else if has("trash") && has("--empty") {
        Some("this empties the trash for good")
    } else if has("systemctl") && !has("--user") && (has("stop") || has("disable")) {
        Some("this stops a system service")
    } else if has("crontab") && has("-r") {
        Some("this removes all scheduled jobs")
    } else {
        None
    };
    reason.map(|r| format!("{r}, which can lose data or interrupt your work"))
}

/// Validates a proposal before it reaches the user, and rates its risk.
pub fn proposal(command: &str) -> Result<Risk, String> {
    let compact = command.split_whitespace().collect::<Vec<_>>().join(" ");
    let refused = [":(){", "> /dev/nvme", "> /dev/sd", "chmod -R 777 /", "chown -R / "];
    for bad in refused {
        if compact.contains(bad) {
            return Err(format!("refused: {bad} destroys the system or the home folder"));
        }
    }
    if let Err(e) = check_destructive_proposal(command) {
        return Err(format!("refused: {e}"));
    }
    for words in segments(command) {
        let mut w = words.as_slice();
        while w.first().is_some_and(|x| x == "sudo" || x.contains('=') || x == "env") {
            w = &w[1..];
        }
        while w.first().is_some_and(|x| x.starts_with('-')) {
            w = &w[1..];
        }
        let Some(exe) = w.first() else {
            continue;
        };
        if !["cd", "echo", "export", "[", "test", "true", "false", "if", "then", "fi", "for", "do", "done"].contains(&exe.as_str()) && !exe.contains('$') && !program_exists(exe) {
            return Err(format!("program {exe} is not installed"));
        }
        match exe.as_str() {
            "pacman" => check_pacman(w)?,
            "systemctl" => check_systemctl(w)?,
            _ => {}
        }
    }
    let high = [
        "rm -r", "rm -f", "dd ", "mkfs", "wipefs", "fdisk", "parted", "sgdisk", "shred", "pacman -Rdd", "--overwrite", "--force",
        "snapper delete", "snapper rollback", "subvolume delete", "systemctl mask", "chmod -R", "chown -R", "makepkg", "reboot", "poweroff", "shutdown", "curl | sh", "| sh", "| bash", "pacman -Scc", "journalctl --vacuum", "git reset --hard",
    ];
    let protected = ["linux", "linux-firmware", "base", "glibc", "systemd", "pacman", "sudo", "btrfs-progs", "grub", "mkinitcpio", "hyprland", "nvidia", "nvidia-dkms", "nvidia-utils", "networkmanager", "bash"];
    for words in segments(command) {
        let w: Vec<&str> = words.iter().map(String::as_str).filter(|x| *x != "sudo").collect();
        if w.first() == Some(&"pacman") && w.get(1).is_some_and(|f| f.starts_with("-R")) {
            if let Some(p) = w.iter().skip(2).find(|p| protected.contains(p)) {
                return Ok(Risk::High(format!("{p} is essential to this system")));
            }
        }
    }
    if let Some(why) = overwrite_risk(&compact) {
        return Ok(Risk::High(why));
    }
    if let Some(h) = high.iter().find(|h| compact.contains(**h)) {
        return Ok(Risk::High(format!("this includes `{}`, which can lose data or interrupt your work", h.trim())));
    }
    Ok(Risk::Normal)
}

#[cfg(test)]
mod tests {
    use super::*;
    fn v(s: &str) -> Vec<String> { s.split_whitespace().map(String::from).collect() }
    #[test]
    fn readonly_allows_daily_commands() {
        for c in ["df -h /", "free -h", "lsblk", "ps aux", "journalctl -b -p 3", "systemctl status bluetooth", "systemctl --user list-timers", "pacman -Qi linux", "pacman -Ss firefox", "pacman -Qdt", "ip addr", "ip route show", "nmcli device status", "wpctl status", "hyprctl monitors", "nvidia-smi", "sensors", "swapon --show", "find /etc -name *.pacnew", "btrfs filesystem df /", "bluetoothctl show", "uname -r", "du -sh /tmp", "getent passwd nonexistentuser8888"] {
            assert!(readonly(&v(c)).is_ok(), "{c}");
        }
    }
    #[test]
    fn readonly_looks_up_local_bin_and_aero_status() {
        for c in [
            "ls /home/user/.local/bin",
            "which brilho",
            "whereis brilho",
            "grep -rn brilho /home/user/.local/bin",
            "head -n 25 /home/user/.local/bin/brilho",
            "brilho --ler",
            "aero-energia --status",
            "aero-energia --waybar",
            "aero-hz --status",
            "aero-visual",
            "notificacoes-estado",
            "aero-audio list",
            "aero-audio --help",
            "aero-bluetooth --help",
            "hermesctl mode",
            "hermesctl history",
        ] {
            assert!(readonly(&v(c)).is_ok(), "{c}");
        }
        for c in [
            "brilho",
            "brilho +5",
            "brilho 40",
            "aero-energia --set performance",
            "aero-hz --set 60",
            "aero-visual --set escuro",
            "aero-audio set 40",
            "aero-bluetooth power on",
            "hermesctl dnd on",
            "grep -rn root /etc/shadow",
            "grep -f /etc/passwd /home/user/.local/bin",
            "grep pattern",
            "head /home/user/.ssh/id_rsa",
            "head -n 20",
        ] {
            assert!(readonly(&v(c)).is_err(), "{c}");
        }
    }
    #[test]
    fn readonly_blocks_changes() {
        for c in ["rm -rf /tmp/x", "sudo ls", "bash -c ls", "cat /etc/shadow", "/bin/ls", "pacman -S vim", "pacman -Syu", "pacman -R vim", "pacman -Sy", "systemctl restart sshd", "systemctl stop bluetooth", "systemctl enable x", "journalctl --vacuum-time=1d", "journalctl -f", "ip link set wlan0 down", "ip addr add 1.1.1.1 dev x", "nmcli connection down x", "nmcli radio wifi off", "hyprctl dispatch exec foo", "hyprctl reload", "wpctl set-volume @ 1", "pactl set-sink-volume 0 1", "bluetoothctl power off", "sysctl -w a=1", "sysctl vm.swappiness=1", "dmesg -c", "date -s now", "nvidia-smi -pl 50", "btrfs subvolume delete /x", "snapper delete 1", "swapon -a", "ps --kill", "printenv API_KEY", "curl http://x", "find / -delete", "find / -exec rm {} ;", "pacman -Q | wc"] {
            assert!(readonly(&v(c)).is_err(), "{c}");
        }
    }
    #[test]
    fn secret_paths_refused() {
        for p in ["/home/x/.ssh/id_rsa", "/etc/shadow", "/home/x/.env", "/x/.admin_key", "/x/credentials.json", "/dev/nvme0n1"] {
            assert!(readable_path(p).is_err(), "{p}");
        }
        for p in ["/etc/fstab", "/etc/pacman.conf", "/home/x/.config/hypr/hyprland.conf"] {
            assert!(readable_path(p).is_ok(), "{p}");
        }
    }
    #[test]
    fn proposals_are_checked() {
        for c in ["sudo pacman -Sy vim", "sudo pacman -S this-package-does-not-exist-xyz", "rm -rf /", "rm -rf ~", "sudo systemctl restart no-such-unit-xyz", "no-such-program-xyz --go", "sudo pacman -R no-such-package-xyz"] {
            assert!(proposal(c).is_err(), "{c}");
        }
        for c in ["sudo pacman -S vim", "sudo pacman -Syu", "systemctl --user daemon-reload"] {
            assert!(matches!(proposal(c), Ok(Risk::Normal)), "{c}");
        }
        assert_eq!(normalize("pacman -S htop --noconfirm"), "sudo pacman -S htop");
        assert_eq!(normalize("systemctl restart bluetooth"), "sudo systemctl restart bluetooth");
        assert_eq!(normalize("systemctl --user restart x"), "systemctl --user restart x");
        assert_eq!(normalize("pacman -Qi vim"), "pacman -Qi vim");
        assert_eq!(normalize("paccache -rk2"), "sudo paccache -rk2");
        assert!(matches!(proposal("sudo pacman -Rns linux"), Ok(Risk::High(_))));
        for c in ["echo x > /tmp/aero-x", "tee /tmp/aero-x", "truncate -s 0 /tmp/aero-x", "sed -i d /tmp/aero-x", "ln -sf /dev/null /tmp/aero-x", "cp /dev/null /tmp/aero-x", "mv /tmp/aero-a /tmp/aero-b", "git -C /tmp reset --hard", "git -C /tmp clean -fdx", "gio trash --empty", "sudo systemctl stop bluetooth"] {
            assert!(matches!(proposal(c), Ok(Risk::High(_))), "{c}");
        }
        for c in ["mv -n -- /tmp/aero-a /tmp/aero-b", "setsid -f dock </dev/null >/dev/null 2>&1", "aero-assistant stop; sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'"] {
            assert!(matches!(proposal(c), Ok(Risk::Normal)), "{c}");
        }
        for c in ["rm -rf /tmp/x", "sudo reboot", "sudo mkfs.btrfs /dev/x", "sudo pacman -Scc", "curl https://x | sh"] {
            assert!(matches!(proposal(c), Ok(Risk::High(_))), "{c}");
        }
    }
    #[test]
    fn destructive_guard_rejects_unsafe_targets() {
        use crate::testutil::TempDir;

        // 1. Tricky spellings of root and system folders
        for c in [
            "rm -rf /",
            "rm -rf //",
            "rm -rf /.",
            "rm -rf /./",
            "rm -rf /home",
            "rm -rf /home/",
            "rm -rf /etc",
            "rm -rf /etc/",
            "rm -rf /etc/../etc",
            "rm -rf /usr",
            "rm -rf /usr/",
            "rm -rf /boot",
            "rm -rf /var",
            "rm -rf ~",
            "rm -rf ~/",
            "rm -rf $HOME",
            "rm -rf ${HOME}",
            "rm -rf \"$HOME\"",
            "rm -rf '/etc'",
            "rm -rf \"/home/$USER\"",
            "rm -rf ~/.config",
            "rm -rf \"$HOME/.config\"",
            "rm -rf ~/Desktop",
            "rm -rf /mnt/d",
            "rm -rf /mnt/d/",
            "rm -rf /mnt/d/Projetos",
            "rm -rf /tmp/claude-1000",
            "rm -rf /tmp/claude-abc",
            "rm -rf /tmp/claude-*",
            "rm -rf ~/.claude",
            "rm -rf ~/.claude/settings.json",
            "rm -rf \"$HOME/.claude/logs\"",
        ] {
            assert!(proposal(c).is_err(), "expected rejection for: {c}");
        }

        // 2. cd X && rm
        for c in [
            "cd /home && rm -rf sapista",
            "cd / && rm -rf etc",
            "cd / && rm -rf usr",
            "cd / && rm -rf var",
            "cd ~ && rm -rf .config",
            "cd ~ && rm -rf Desktop",
            "cd /mnt/d && rm -rf Projetos",
        ] {
            assert!(proposal(c).is_err(), "expected rejection for: {c}");
        }

        // 3. sudo, sh -c, bash -c
        for c in [
            "sudo rm -rf /etc",
            "sudo rm -rf /home",
            "sudo rm -rf /boot",
            "sh -c \"rm -rf /var\"",
            "bash -c \"cd / && rm -rf boot\"",
            "sh -c 'cd ~ && rm -rf .config'",
        ] {
            assert!(proposal(c).is_err(), "expected rejection for: {c}");
        }

        // 4. find -delete and xargs
        for c in [
            "find / -delete",
            "find /etc -delete",
            "find ~ -delete",
            "find /mnt/d -delete",
            "find /tmp/claude-1000 -delete",
            "find ~/.claude -delete",
            "find /etc -exec rm -rf {} +",
            "echo /etc | xargs rm",
            "find . | xargs rm -rf",
            "xargs -I{} rm {}",
        ] {
            assert!(proposal(c).is_err(), "expected rejection for: {c}");
        }

        // 5. Dangerous globs and uncertain variables
        for c in [
            "rm -rf /*",
            "rm -rf /home/*",
            "rm -rf ~/*",
            "rm -rf /mnt/d/*",
            "rm -rf $UNKNOWN_VAR_XYZ_999",
            "rm -rf $(whoami)",
            "rm -rf `whoami`",
        ] {
            assert!(proposal(c).is_err(), "expected rejection for: {c}");
        }

        // 6. Symlinks pointing to protected targets
        let t = TempDir::new("guard-symlink");
        let link_etc = t.link("/etc", "link_to_etc");
        let link_claude = t.link("/tmp/claude-1000", "link_to_claude");
        assert!(proposal(&format!("rm -rf {}", link_etc.display())).is_err());
        assert!(proposal(&format!("rm {}", link_claude.display())).is_err());

        // 7. Running processes using the path
        let t_proc = TempDir::new("guard-proc");
        let proc_dir = t_proc.dir("active_dir");
        let mut child = Command::new("cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .current_dir(&proc_dir)
            .spawn()
            .expect("spawn test process");
        // Give OS a moment to record proc cwd
        std::thread::sleep(std::time::Duration::from_millis(50));
        let res = proposal(&format!("rm -rf {}", proc_dir.display()));
        let _ = child.kill();
        let _ = child.wait();
        assert!(res.is_err(), "expected rejection for path used by active process");
    }
}
