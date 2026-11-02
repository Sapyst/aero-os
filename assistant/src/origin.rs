//! find_origin: which configuration file creates, configures or mentions a path, unit or error term.
//! Read-only. It walks a fixed list of folders in Rust (no shell), with time, file, size and output
//! limits, and never reads or prints a file that guard::readable_path refuses.

use crate::fsinfo;
use crate::guard;
use std::collections::HashSet;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub struct Limits {
    pub time: Duration,
    pub max_files: usize,
    pub max_file_bytes: u64,
    pub max_hits: usize,
    pub max_hits_per_file: usize,
    pub max_depth: usize,
    pub max_line: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { time: Duration::from_secs(8), max_files: 40_000, max_file_bytes: 1_000_000, max_hits: 40, max_hits_per_file: 5, max_depth: 8, max_line: 200 }
    }
}

#[derive(Debug)]
pub struct Hit {
    pub file: String,
    /// 0 when the file or link name itself matches.
    pub line: usize,
    pub text: String,
}

pub struct Report {
    pub hits: Vec<Hit>,
    pub files: usize,
    pub refused: usize,
    pub unreadable: usize,
    pub stopped: Option<&'static str>,
    pub elapsed: Duration,
    pub variants: Vec<String>,
    pub roots: Vec<String>,
}

/// Folders and files searched, most specific first, so the likely origin is listed first.
pub fn default_roots(home: &str) -> Vec<PathBuf> {
    let h = |p: &str| PathBuf::from(format!("{home}/{p}"));
    let mut roots = vec![
        h(".config/user-tmpfiles.d"),
        h(".local/share/user-tmpfiles.d"),
        PathBuf::from("/etc/tmpfiles.d"),
        PathBuf::from("/run/tmpfiles.d"),
        PathBuf::from("/usr/lib/tmpfiles.d"),
        h(".config/systemd/user"),
        h(".local/share/systemd/user"),
        PathBuf::from("/etc/systemd/user"),
        PathBuf::from("/usr/lib/systemd/user"),
        PathBuf::from("/etc/systemd/system"),
        PathBuf::from("/run/systemd/system"),
        PathBuf::from("/usr/lib/systemd/system"),
        h(".config/environment.d"),
        PathBuf::from("/etc/environment.d"),
        PathBuf::from("/usr/lib/environment.d"),
        PathBuf::from("/etc/environment"),
    ];
    for f in [".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".zshenv", ".pam_environment", ".xprofile"] {
        roots.push(h(f));
    }
    roots.extend([h(".local/bin"), h(".config"), PathBuf::from("/etc")]);
    if let Ok(extra) = std::env::var("AERO_ORIGIN_EXTRA_ROOTS") {
        for p in extra.split(':') {
            if !p.is_empty() {
                let pb = PathBuf::from(p);
                if pb.exists() {
                    roots.push(pb);
                }
            }
        }
    }
    roots
}

/// Spellings a configuration file may use for the same path: ~, $HOME, ${HOME}, %h, %t, %U, $UID.
pub fn variants(term: &str, home: &str, uid: u32) -> Vec<String> {
    let mut base = vec![term.to_string()];
    let rest = if !home.is_empty() && term.starts_with(&format!("{home}/")) {
        Some(term[home.len() + 1..].to_string())
    } else {
        term.strip_prefix("~/").map(String::from)
    };
    if let Some(r) = rest {
        for p in [format!("{home}/{r}"), format!("~/{r}"), format!("$HOME/{r}"), format!("${{HOME}}/{r}"), format!("%h/{r}")] {
            base.push(p);
        }
    }
    let run = format!("/run/user/{uid}/");
    if let Some(r) = term.strip_prefix(&run) {
        for p in [format!("%t/{r}"), format!("$XDG_RUNTIME_DIR/{r}"), format!("${{XDG_RUNTIME_DIR}}/{r}")] {
            base.push(p);
        }
    }
    let id = uid.to_string();
    let mut all = vec![];
    for b in base {
        all.push(b.clone());
        // Replace the user id only where it stands alone as a number, as in claude-1000.
        let bytes = b.as_bytes();
        let mut i = 0;
        while let Some(pos) = b[i..].find(&id) {
            let s = i + pos;
            let e = s + id.len();
            let before = s == 0 || !bytes[s - 1].is_ascii_digit();
            let after = e == bytes.len() || !bytes[e].is_ascii_digit();
            if before && after {
                for spec in ["%U", "$UID", "${UID}", "$(id -u)"] {
                    all.push(format!("{}{spec}{}", &b[..s], &b[e..]));
                }
            }
            i = e;
        }
    }
    let mut seen = HashSet::new();
    all.retain(|v| seen.insert(v.clone()));
    all.truncate(24);
    all
}

pub fn check_term(term: &str) -> Result<String, String> {
    let t = term.trim();
    if t.chars().count() < 3 {
        return Err("the search term needs at least 3 characters".into());
    }
    if t.len() > 200 || t.contains('\n') || t.contains('\0') {
        return Err("the search term must be one line of at most 200 characters".into());
    }
    Ok(t.to_string())
}

fn skip_dir(name: &str) -> bool {
    let lower = name.to_lowercase();
    if lower == "aero-temp" || lower.starts_with("aero-temp") {
        return false;
    }
    lower.contains("cache") || [".git", "node_modules", "indexeddb", "service worker", "blob_storage", "local storage", "session storage", "crashpad", "webstorage"].contains(&lower.as_str())
}

struct Walk<'a> {
    needles: Vec<String>,
    fold: bool,
    limits: &'a Limits,
    start: Instant,
    seen_files: HashSet<PathBuf>,
    seen_dirs: HashSet<PathBuf>,
    report: Report,
}

impl Walk<'_> {
    fn matches(&self, text: &str) -> bool {
        if self.fold {
            let t = text.to_lowercase();
            self.needles.iter().any(|n| t.contains(n.as_str()))
        } else {
            self.needles.iter().any(|n| text.contains(n.as_str()))
        }
    }

    fn over(&mut self) -> bool {
        if self.report.stopped.is_some() {
            return true;
        }
        if self.start.elapsed() > self.limits.time {
            self.report.stopped = Some("time limit");
        } else if self.report.files >= self.limits.max_files {
            self.report.stopped = Some("file limit");
        } else if self.report.hits.len() >= self.limits.max_hits {
            self.report.stopped = Some("output limit");
        }
        self.report.stopped.is_some()
    }

    fn hit(&mut self, file: &Path, line: usize, text: &str) {
        if self.report.hits.len() < self.limits.max_hits {
            self.report.hits.push(Hit { file: file.display().to_string(), line, text: fsinfo::clip(text.trim(), self.limits.max_line) });
        }
    }

    fn allowed(&mut self, shown: &Path, real: &Path) -> bool {
        let ok = guard::readable_path(&shown.display().to_string()).is_ok() && guard::readable_path(&real.display().to_string()).is_ok();
        if !ok {
            self.report.refused += 1;
        }
        ok
    }

    fn file(&mut self, path: &Path) {
        if self.over() {
            return;
        }
        let real = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        if !self.allowed(path, &real) {
            return;
        }
        let is_link = fs::symlink_metadata(path).map(|m| m.file_type().is_symlink()).unwrap_or(false);
        let name = path.file_name().map(|n| n.to_string_lossy().to_string()).unwrap_or_default();
        if is_link {
            let target = fs::read_link(path).map(|t| t.display().to_string()).unwrap_or_default();
            if self.matches(&name) || self.matches(&target) {
                self.hit(path, 0, &format!("symlink -> {target}"));
            }
        } else if self.matches(&name) {
            self.hit(path, 0, "(file name matches)");
        }
        if !self.seen_files.insert(real.clone()) {
            return;
        }
        let Ok(meta) = fs::metadata(&real) else {
            self.report.unreadable += 1;
            return;
        };
        if !meta.is_file() || meta.len() > self.limits.max_file_bytes {
            return;
        }
        let Ok(f) = fs::File::open(&real) else {
            self.report.unreadable += 1;
            return;
        };
        self.report.files += 1;
        let mut bytes = vec![];
        let _ = f.take(self.limits.max_file_bytes).read_to_end(&mut bytes);
        if bytes[..bytes.len().min(8192)].contains(&0) {
            return;
        }
        let text = String::from_utf8_lossy(&bytes);
        let mut n = 0;
        for (i, line) in text.lines().enumerate() {
            if self.matches(line) {
                self.hit(path, i + 1, line);
                n += 1;
                if n >= self.limits.max_hits_per_file || self.report.hits.len() >= self.limits.max_hits {
                    break;
                }
            }
        }
    }

    fn dir(&mut self, path: &Path, depth: usize) {
        if self.over() || depth > self.limits.max_depth {
            return;
        }
        let real = fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        if !self.seen_dirs.insert(real) {
            return;
        }
        let Ok(rd) = fs::read_dir(path) else {
            self.report.unreadable += 1;
            return;
        };
        let mut entries: Vec<_> = rd.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for e in entries {
            if self.over() {
                return;
            }
            let p = e.path();
            let Ok(ft) = e.file_type() else { continue };
            if ft.is_dir() {
                if !skip_dir(&e.file_name().to_string_lossy()) {
                    self.dir(&p, depth + 1);
                }
            } else if ft.is_symlink() {
                // Symlinked folders are not followed, so loops cannot happen; the link itself is still checked.
                if fs::metadata(&p).map(|m| m.is_dir()).unwrap_or(false) {
                    if !self.allowed(&p, &p) {
                        continue;
                    }
                    let target = fs::read_link(&p).map(|t| t.display().to_string()).unwrap_or_default();
                    let name = e.file_name().to_string_lossy().to_string();
                    if self.matches(&name) || self.matches(&target) {
                        self.hit(&p, 0, &format!("symlink -> {target}"));
                    }
                } else {
                    self.file(&p);
                }
            } else if ft.is_file() {
                self.file(&p);
            }
        }
    }
}

pub fn search(term: &str, roots: &[PathBuf], home: &str, uid: u32, limits: &Limits) -> Result<Report, String> {
    let term = check_term(term)?;
    let vars = variants(&term, home, uid);
    let fold = !term.contains('/');
    let needles = vars.iter().map(|v| if fold { v.to_lowercase() } else { v.clone() }).collect();
    let mut w = Walk {
        needles,
        fold,
        limits,
        start: Instant::now(),
        seen_files: HashSet::new(),
        seen_dirs: HashSet::new(),
        report: Report { hits: vec![], files: 0, refused: 0, unreadable: 0, stopped: None, elapsed: Duration::ZERO, variants: vars, roots: vec![] },
    };
    for r in roots {
        if w.over() {
            break;
        }
        let Ok(meta) = fs::metadata(r) else { continue };
        w.report.roots.push(r.display().to_string());
        if meta.is_dir() {
            w.dir(r, 0);
        } else {
            w.file(r);
        }
    }
    w.report.elapsed = w.start.elapsed();
    Ok(w.report)
}

pub fn format(term: &str, r: &Report) -> String {
    let mut out = format!("find_origin {term:?}\nSpellings searched: {}\n", r.variants.join(" | "));
    out.push_str(&format!(
        "Read {} files in {:.1} s{}.{}{}\n",
        r.files,
        r.elapsed.as_secs_f64(),
        r.stopped.map(|s| format!(", stopped early at the {s}, so the list may be incomplete")).unwrap_or_default(),
        if r.refused > 0 { format!(" {} files were not read because they may hold secrets.", r.refused) } else { String::new() },
        if r.unreadable > 0 { format!(" {} files or folders could not be read (permissions).", r.unreadable) } else { String::new() },
    ));
    if r.hits.is_empty() {
        out.push_str(&format!("No match. Searched: {}\n", r.roots.join(", ")));
        out.push_str("Absence here does not prove nothing creates it: programs can also create files at run time.\n");
        return out;
    }
    out.push_str("Matches (file:line: text; line 0 means the name matches):\n");
    for h in &r.hits {
        out.push_str(&format!("{}:{}: {}\n", h.file, h.line, h.text));
    }
    out
}

/// The tool entry point: searches the default folders for this user.
pub fn find_origin(term: &str) -> String {
    let home = fsinfo::home();
    match search(term, &default_roots(&home), &home, fsinfo::uid(), &Limits::default()) {
        Ok(r) => format(term.trim(), &r),
        Err(e) => format!("REFUSED: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TempDir;

    fn run(t: &TempDir, term: &str, limits: &Limits) -> Report {
        search(term, &[t.path.clone()], &fsinfo::home(), 1000, limits).unwrap()
    }

    #[test]
    fn finds_rule_by_line_and_alternate_spellings() {
        let t = TempDir::new("origin");
        let home = fsinfo::home();
        t.file("user-tmpfiles.d/stand-in.conf", b"# comment\nL /tmp/stand-in-tool-%U - - - - %h/.cache/stand-in-target\n");
        t.file("environment.d/x.conf", b"STANDIN_DIR=${HOME}/.cache/stand-in-target\n");
        let r = run(&t, "/tmp/stand-in-tool-1000", &Limits::default());
        assert_eq!(r.hits.len(), 1, "{:?}", r.hits);
        assert_eq!(r.hits[0].line, 2);
        assert!(r.hits[0].file.ends_with("user-tmpfiles.d/stand-in.conf"));
        let r = run(&t, &format!("{home}/.cache/stand-in-target"), &Limits::default());
        assert_eq!(r.hits.len(), 2, "{:?}", r.hits);
        let text = format("x", &r);
        assert!(text.contains("stand-in.conf:2:"), "{text}");
    }

    #[test]
    fn never_reads_secret_files_or_binaries() {
        let t = TempDir::new("origin");
        t.file("conf/token.conf", b"standin-secret-term\n");
        t.file("conf/id_rsa.pem", b"standin-secret-term\n");
        t.file("conf/blob.bin", b"standin-secret-term\0\0\0");
        t.file("conf/ok.conf", b"nothing here\n");
        t.link("token.conf", "conf/alias.conf");
        let r = run(&t, "standin-secret-term", &Limits::default());
        assert!(r.hits.is_empty(), "{:?}", r.hits);
        assert!(r.refused >= 3, "refused {}", r.refused);
        assert!(!format("standin-secret-term", &r).contains("token"));
    }

    #[test]
    fn names_links_and_loops() {
        let t = TempDir::new("origin");
        t.file("units/standin-tool.service", b"[Service]\nExecStart=/usr/bin/true\n");
        t.link("../units/standin-tool.service", "wants/standin-tool.service");
        t.link(".", "units/loop");
        t.link("/nonexistent/standin-tool", "units/dangling");
        let r = run(&t, "standin-tool", &Limits::default());
        let lines: Vec<String> = r.hits.iter().map(|h| format!("{}:{}:{}", h.file, h.line, h.text)).collect();
        assert!(lines.iter().any(|l| l.contains("units/standin-tool.service:0:(file name matches)")), "{lines:?}");
        assert!(lines.iter().any(|l| l.contains("wants/standin-tool.service:0:symlink ->")), "{lines:?}");
        assert!(lines.iter().any(|l| l.contains("units/dangling:0:symlink -> /nonexistent/standin-tool")), "{lines:?}");
        assert!(r.stopped.is_none());
    }

    #[test]
    fn limits_are_enforced() {
        let t = TempDir::new("origin");
        let body: String = (0..50).map(|i| format!("standin-many {i}\n")).collect();
        for i in 0..20 {
            t.file(&format!("many/f{i:02}.conf"), body.as_bytes());
        }
        let r = run(&t, "standin-many", &Limits::default());
        assert_eq!(r.hits.len(), 40);
        assert_eq!(r.stopped, Some("output limit"));
        assert!(r.hits.iter().filter(|h| h.file.ends_with("f00.conf")).count() <= 5);
        let r = run(&t, "standin-many", &Limits { time: Duration::ZERO, ..Limits::default() });
        assert_eq!(r.stopped, Some("time limit"));
        let long = format!("standin-long {}\n", "x".repeat(5000));
        t.file("long/l.conf", long.as_bytes());
        let r = run(&t, "standin-long", &Limits::default());
        assert!(r.hits[0].text.chars().count() <= 203);
        assert!(check_term("ab").is_err());
        assert!(check_term("a\nb c").is_err());
    }

    #[test]
    fn spellings() {
        let v = variants("/home/u/.cache/x", "/home/u", 1000);
        for want in ["~/.cache/x", "$HOME/.cache/x", "${HOME}/.cache/x", "%h/.cache/x"] {
            assert!(v.contains(&want.to_string()), "{v:?}");
        }
        let v = variants("/tmp/tool-1000", "/home/u", 1000);
        assert!(v.contains(&"/tmp/tool-%U".to_string()), "{v:?}");
        assert!(v.contains(&"/tmp/tool-$UID".to_string()), "{v:?}");
        let v = variants("/tmp/tool-10000", "/home/u", 1000);
        assert_eq!(v, vec!["/tmp/tool-10000".to_string()]);
        let v = variants("/run/user/1000/app.sock", "/home/u", 1000);
        assert!(v.contains(&"%t/app.sock".to_string()), "{v:?}");
    }
}
