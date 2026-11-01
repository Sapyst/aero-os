//! Decides which commands may run: read-only tools without asking, proposals after checks.

use std::process::{Command, Stdio};

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
        | "fc-list" | "lsmod" | "modinfo" | "vulkaninfo" | "glxinfo" | "inxi" | "tty" => {
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

/// Validates a proposal before it reaches the user, and rates its risk.
pub fn proposal(command: &str) -> Result<Risk, String> {
    let compact = command.split_whitespace().collect::<Vec<_>>().join(" ");
    let refused = ["rm -rf /", "rm -rf ~", "rm -fr /", ":(){", "> /dev/nvme", "> /dev/sd", "chmod -R 777 /", "chown -R / "];
    for bad in refused {
        let hit = compact.contains(bad);
        let only_subpath = bad == "rm -rf /" && compact.split("rm -rf /").nth(1).is_some_and(|rest| rest.chars().next().is_some_and(|c| c.is_alphanumeric()));
        let home_subpath = bad == "rm -rf ~" && compact.split("rm -rf ~").nth(1).is_some_and(|rest| rest.starts_with('/') && rest.len() > 1);
        if hit && !only_subpath && !home_subpath {
            return Err(format!("refused: {bad} destroys the system or the home folder"));
        }
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
        for c in ["df -h /", "free -h", "lsblk", "ps aux", "journalctl -b -p 3", "systemctl status bluetooth", "systemctl --user list-timers", "pacman -Qi linux", "pacman -Ss firefox", "pacman -Qdt", "ip addr", "ip route show", "nmcli device status", "wpctl status", "hyprctl monitors", "nvidia-smi", "sensors", "swapon --show", "find /etc -name *.pacnew", "btrfs filesystem df /", "bluetoothctl show", "uname -r", "du -sh /tmp"] {
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
        for c in ["rm -rf /tmp/x", "sudo reboot", "sudo mkfs.btrfs /dev/x", "sudo pacman -Scc", "curl https://x | sh"] {
            assert!(matches!(proposal(c), Ok(Risk::High(_))), "{c}");
        }
    }
}
