//! Read-only facts about files, users, mounts and processes, shared by the tools and the guard.

use std::collections::HashSet;
use std::fs;
use std::path::Path;

unsafe extern "C" {
    fn getuid() -> u32;
}

pub fn home() -> String {
    std::env::var("HOME").unwrap_or_default()
}

pub fn uid() -> u32 {
    unsafe { getuid() }
}

fn colon_file(path: &str, key_col: usize, key: &str, want: usize) -> Option<String> {
    fs::read_to_string(path).ok()?.lines().find_map(|l| {
        let cols: Vec<&str> = l.split(':').collect();
        (cols.get(key_col) == Some(&key)).then(|| cols.get(want).map(|s| s.to_string())).flatten()
    })
}

pub fn user_name(uid: u32) -> String {
    colon_file("/etc/passwd", 2, &uid.to_string(), 0).unwrap_or_else(|| uid.to_string())
}

pub fn group_name(gid: u32) -> String {
    colon_file("/etc/group", 2, &gid.to_string(), 0).unwrap_or_else(|| gid.to_string())
}

/// Home folder of a named user, for `~name`.
pub fn user_home(name: &str) -> Option<String> {
    colon_file("/etc/passwd", 0, name, 5)
}

/// Undoes the octal escapes the kernel uses in /proc mount tables (\040 for a space).
fn unescape(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = vec![];
    let mut i = 0;
    while i < b.len() {
        if b[i] == b'\\' && i + 3 < b.len() && b[i + 1..i + 4].iter().all(|c| (b'0'..=b'7').contains(c)) {
            out.push(((b[i + 1] - b'0') << 6) | ((b[i + 2] - b'0') << 3) | (b[i + 3] - b'0'));
            i += 4;
        } else {
            out.push(b[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).to_string()
}

#[derive(Clone, Debug)]
pub struct Mount {
    pub point: String,
    pub source: String,
    pub fstype: String,
}

pub fn mounts() -> Vec<Mount> {
    let text = fs::read_to_string("/proc/self/mountinfo").unwrap_or_default();
    text.lines()
        .filter_map(|l| {
            let (left, right) = l.split_once(" - ")?;
            let point = unescape(left.split_whitespace().nth(4)?);
            let mut r = right.split_whitespace();
            let fstype = r.next()?.to_string();
            let source = unescape(r.next().unwrap_or(""));
            Some(Mount { point, source, fstype })
        })
        .collect()
}

/// Active swap devices and files from /proc/swaps.
pub fn swaps() -> Vec<String> {
    fs::read_to_string("/proc/swaps").unwrap_or_default().lines().skip(1).filter_map(|l| l.split_whitespace().next().map(unescape)).collect()
}

#[derive(Clone, Debug)]
pub struct ProcUse {
    pub pid: u32,
    pub name: String,
    pub how: &'static str,
    pub path: String,
}

/// Every file, folder and mapping that visible processes use. The second value counts
/// processes whose open files could not be read (other users, usually root).
pub fn process_uses() -> (Vec<ProcUse>, usize) {
    let me = std::process::id();
    let mut uses = vec![];
    let mut hidden = 0;
    for e in fs::read_dir("/proc").into_iter().flatten().flatten() {
        let Some(pid) = e.file_name().to_str().and_then(|s| s.parse::<u32>().ok()) else {
            continue;
        };
        if pid == me {
            continue;
        }
        let dir = e.path();
        let name = fs::read_to_string(dir.join("comm")).unwrap_or_default().trim().to_string();
        let mut seen = HashSet::new();
        let mut add = |how: &'static str, p: String, uses: &mut Vec<ProcUse>| {
            let p = p.strip_suffix(" (deleted)").map(String::from).unwrap_or(p);
            if p.starts_with('/') && seen.insert((how, p.clone())) {
                uses.push(ProcUse { pid, name: name.clone(), how, path: p });
            }
        };
        if let Ok(p) = fs::read_link(dir.join("cwd")) {
            add("working directory", p.display().to_string(), &mut uses);
        }
        if let Ok(p) = fs::read_link(dir.join("root")) {
            if p != Path::new("/") {
                add("root directory", p.display().to_string(), &mut uses);
            }
        }
        if let Ok(p) = fs::read_link(dir.join("exe")) {
            add("program", p.display().to_string(), &mut uses);
        }
        match fs::read_dir(dir.join("fd")) {
            Ok(fds) => {
                for fd in fds.flatten() {
                    if let Ok(p) = fs::read_link(fd.path()) {
                        add("open file", p.display().to_string(), &mut uses);
                    }
                }
            }
            Err(_) => hidden += 1,
        }
        for line in fs::read_to_string(dir.join("maps")).unwrap_or_default().lines() {
            if let Some(i) = line.find(" /") {
                add("mapped file", line[i + 1..].to_string(), &mut uses);
            }
        }
    }
    (uses, hidden)
}

/// True when `p` is `base` or lies inside it.
pub fn is_within(p: &str, base: &str) -> bool {
    if base == "/" {
        return p.starts_with('/');
    }
    p == base || p.strip_prefix(base).is_some_and(|r| r.starts_with('/'))
}

/// Uses of `path` itself, or of anything inside it when `inside` is true.
pub fn uses_of<'a>(uses: &'a [ProcUse], path: &str, inside: bool) -> Vec<&'a ProcUse> {
    uses.iter().filter(|u| if inside { is_within(&u.path, path) } else { u.path == path }).collect()
}

fn civil(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + if m <= 2 { 1 } else { 0 }, m, d)
}

/// Formats seconds since 1970 as an exact UTC time.
pub fn utc(secs: i64) -> String {
    let (y, m, d) = civil(secs.div_euclid(86_400));
    let t = secs.rem_euclid(86_400);
    format!("{y:04}-{m:02}-{d:02} {:02}:{:02}:{:02} UTC", t / 3600, t / 60 % 60, t % 60)
}

pub fn now() -> i64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0)
}

pub fn age(secs: i64) -> String {
    let d = now() - secs;
    match d {
        d if d < 0 => "in the future".into(),
        d if d < 120 => format!("{d} s ago"),
        d if d < 7200 => format!("{} min ago", d / 60),
        d if d < 172_800 => format!("{} h ago", d / 3600),
        d => format!("{} days ago", d / 86_400),
    }
}

/// Cuts text to `max` characters on a character boundary.
pub fn clip(s: &str, max: usize) -> String {
    match s.char_indices().nth(max) {
        Some((i, _)) => format!("{}...", &s[..i]),
        None => s.to_string(),
    }
}

/// Normalizes a path syntactically, resolving `.` and `..` without touching disk.
pub fn clean_path(path: &Path) -> std::path::PathBuf {
    use std::path::Component;
    let mut out = std::path::PathBuf::new();
    for comp in path.components() {
        match comp {
            Component::Prefix(p) => out.push(p.as_os_str()),
            Component::RootDir => out.push("/"),
            Component::CurDir => {}
            Component::ParentDir => {
                if out.file_name().is_some() {
                    out.pop();
                } else if out != Path::new("/") {
                    out.push("..");
                }
            }
            Component::Normal(c) => out.push(c),
        }
    }
    if out.as_os_str().is_empty() {
        std::path::PathBuf::from(".")
    } else {
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn utc_dates_are_exact() {
        assert_eq!(utc(0), "1970-01-01 00:00:00 UTC");
        assert_eq!(utc(951_782_400), "2000-02-29 00:00:00 UTC");
        assert_eq!(utc(1_793_536_245), "2026-11-01 12:30:45 UTC");
    }
    #[test]
    fn within_and_unescape() {
        assert!(is_within("/home/a/b", "/home/a"));
        assert!(is_within("/home/a", "/home/a"));
        assert!(!is_within("/home/ab", "/home/a"));
        assert!(is_within("/x", "/"));
        assert_eq!(unescape("/mnt/my\\040disk"), "/mnt/my disk");
        assert_eq!(clip("abcdef", 3), "abc...");
    }
    #[test]
    fn mounts_include_root() {
        assert!(mounts().iter().any(|m| m.point == "/"));
    }
    #[test]
    fn clean_path_resolves_dots() {
        assert_eq!(clean_path(Path::new("/home/user/../../etc")), Path::new("/etc"));
        assert_eq!(clean_path(Path::new("/home/user/..")), Path::new("/home"));
        assert_eq!(clean_path(Path::new("/")), Path::new("/"));
        assert_eq!(clean_path(Path::new("/../../..")), Path::new("/"));
        assert_eq!(clean_path(Path::new("//etc//")), Path::new("/etc"));
        assert_eq!(clean_path(Path::new("/tmp/./claude-1000")), Path::new("/tmp/claude-1000"));
    }
}
