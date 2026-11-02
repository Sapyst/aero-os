//! recent_changes: answers "what changed before this broke".
//! Reports:
//! 1. Recently changed configuration and script files in ~/.config, ~/.local/bin, /etc.
//! 2. Recent pacman package operations from /var/log/pacman.log.
//! 3. Git log of repositories named by the user.
//! Output is strictly limited. Files refused by guard::readable_path are never inspected.

use crate::fsinfo;
use crate::guard;
use serde_json::Value;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::Instant;

const MAX_CONFIG_FILES: usize = 25;
const MAX_PACMAN_LINES: usize = 30;
const MAX_GIT_COMMITS: usize = 10;

#[derive(Clone, Debug)]
struct FileHit {
    path: String,
    mtime: i64,
    size: u64,
}

fn skip_dir(name: &str) -> bool {
    let lower = name.to_lowercase();
    lower.contains("cache")
        || lower.contains("crash")
        || lower.contains("session")
        || [
            ".git",
            "node_modules",
            "google-chrome",
            "chromium",
            "slack",
            "discord",
            "brave",
            "microsoft-edge",
            "logs",
            "trash",
            "blob_storage",
            "local storage",
            "indexeddb",
        ]
        .contains(&lower.as_str())
}

fn walk_recent_files(root: &Path, min_time: i64, hits: &mut Vec<FileHit>, depth: usize, start: Instant) {
    if depth > 5 || start.elapsed().as_millis() > 3000 {
        return;
    }
    let Ok(entries) = fs::read_dir(root) else {
        return;
    };
    for e in entries.flatten() {
        let p = e.path();
        let name = e.file_name().to_string_lossy().to_string();
        if skip_dir(&name) {
            continue;
        }
        let p_str = p.to_string_lossy().to_string();
        if guard::readable_path(&p_str).is_err() {
            continue;
        }
        let Ok(ft) = e.file_type() else { continue };
        if ft.is_dir() {
            walk_recent_files(&p, min_time, hits, depth + 1, start);
        } else if ft.is_file() {
            if let Ok(meta) = e.metadata() {
                let mtime = meta.mtime();
                if mtime >= min_time {
                    hits.push(FileHit {
                        path: p_str,
                        mtime,
                        size: meta.size(),
                    });
                }
            }
        }
    }
}

pub fn recent_config_changes(roots: &[PathBuf], days: u32) -> String {
    let secs_back = (days as i64) * 86_400;
    let min_time = fsinfo::now() - secs_back;
    let mut hits = vec![];
    let start = Instant::now();

    for r in roots {
        if r.exists() {
            walk_recent_files(r, min_time, &mut hits, 0, start);
        }
    }

    hits.sort_by(|a, b| b.mtime.cmp(&a.mtime));
    hits.truncate(MAX_CONFIG_FILES);

    if hits.is_empty() {
        return format!("No configuration files changed in the last {days} days.\n");
    }

    let mut out = format!("Recently changed configuration files (last {days} days, up to {MAX_CONFIG_FILES}):\n");
    for h in hits {
        out.push_str(&format!(
            "  {} — modified {} ({})\n",
            h.path,
            fsinfo::utc(h.mtime),
            fsinfo::age(h.mtime)
        ));
    }
    out
}

pub fn recent_pacman_logs(log_path: &Path, count: usize) -> String {
    let Ok(content) = fs::read_to_string(log_path) else {
        return "Could not read /var/log/pacman.log\n".into();
    };

    let mut relevant = vec![];
    for line in content.lines().rev() {
        let l = line.trim();
        if l.is_empty() {
            continue;
        }
        let is_interesting = l.contains("[PACMAN] Running 'pacman")
            || l.contains("[ALPM] installed")
            || l.contains("[ALPM] upgraded")
            || l.contains("[ALPM] removed")
            || l.contains("[ALPM] transaction completed")
            || l.contains("[ALPM] transaction started")
            || l.contains("[ALPM-SCRIPTLET]");
        if is_interesting {
            relevant.push(l);
            if relevant.len() >= count {
                break;
            }
        }
    }

    if relevant.is_empty() {
        return "No recent package operations found in pacman log.\n".into();
    }

    relevant.reverse();
    let mut out = format!("Recent package operations from pacman log (last {}):\n", relevant.len());
    for l in relevant {
        out.push_str(&format!("  {l}\n"));
    }
    out
}

pub fn git_log_for_repo(repo_path: &str, commits_count: usize) -> String {
    let home = fsinfo::home();
    let expanded = if repo_path == "~" {
        home
    } else if let Some(rest) = repo_path.strip_prefix("~/") {
        format!("{home}/{rest}")
    } else {
        repo_path.to_string()
    };
    let clean = fsinfo::clean_path(Path::new(&expanded));
    let s = clean.to_string_lossy().to_string();

    if let Err(e) = guard::readable_path(&s) {
        return format!("Repository {s}: REFUSED: {e}\n");
    }

    if !clean.exists() || !clean.join(".git").exists() {
        return format!("Repository {s}: does not exist or is not a git repository\n");
    }

    let out = Command::new("git")
        .args([
            "-C",
            &s,
            "log",
            &format!("-n{commits_count}"),
            "--format=%h %ad %an: %s",
            "--date=short",
        ])
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output();

    match out {
        Ok(output) if output.status.success() => {
            let text = String::from_utf8_lossy(&output.stdout);
            let lines: Vec<&str> = text.lines().collect();
            if lines.is_empty() {
                format!("Repository {s}: git log is empty\n")
            } else {
                let mut res = format!("Repository {s} (recent commits):\n");
                for l in lines {
                    res.push_str(&format!("  {l}\n"));
                }
                res
            }
        }
        Ok(output) => {
            format!("Repository {s}: git log exited with {}\n", output.status)
        }
        Err(e) => {
            format!("Repository {s}: could not run git: {e}\n")
        }
    }
}

pub fn recent_changes(args: &Value) -> String {
    let days = args["days"].as_u64().unwrap_or(7).clamp(1, 30) as u32;

    let home = fsinfo::home();
    let default_roots = vec![
        PathBuf::from(format!("{home}/.config")),
        PathBuf::from(format!("{home}/.local/bin")),
        PathBuf::from("/etc"),
    ];

    let mut out = String::from("=== Recent Changes ===\n\n");

    // 1. Config changes
    out.push_str(&recent_config_changes(&default_roots, days));
    out.push('\n');

    // 2. Pacman log
    out.push_str(&recent_pacman_logs(Path::new("/var/log/pacman.log"), MAX_PACMAN_LINES));
    out.push('\n');

    // 3. Git repositories
    out.push_str("=== Git Repositories ===\n");
    let mut repos = vec![];
    if let Some(arr) = args["repositories"].as_array() {
        for v in arr {
            if let Some(r) = v.as_str() {
                repos.push(r.to_string());
            }
        }
    } else if let Some(r) = args["repositories"].as_str() {
        for part in r.split(',') {
            let t = part.trim();
            if !t.is_empty() {
                repos.push(t.to_string());
            }
        }
    } else if let Some(r) = args["repository"].as_str() {
        repos.push(r.to_string());
    }

    if repos.is_empty() {
        out.push_str("No repositories specified. Pass repository paths under 'repositories' to include their git commit log.\n");
    } else {
        for r in repos {
            out.push_str(&git_log_for_repo(&r, MAX_GIT_COMMITS));
        }
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TempDir;

    #[test]
    fn recent_config_detects_new_files() {
        let t = TempDir::new("recent-cfg");
        t.file("app.conf", b"setting = 1\n");
        let roots = vec![t.path.clone()];
        let res = recent_config_changes(&roots, 1);
        assert!(res.contains("app.conf"), "{res}");
    }

    #[test]
    fn recent_pacman_parses_entries() {
        let t = TempDir::new("recent-pacman");
        let log = t.file(
            "pacman.log",
            b"[2026-10-31T10:00:00] [ALPM] installed linux (6.17.1-1)\n[2026-10-31T10:01:00] [PACMAN] Running 'pacman -Syu'\n",
        );
        let res = recent_pacman_logs(&log, 10);
        assert!(res.contains("installed linux"), "{res}");
        assert!(res.contains("Running 'pacman -Syu'"), "{res}");
    }

    #[test]
    fn git_log_reports_for_repo() {
        let t = TempDir::new("recent-git");
        let _ = Command::new("git").args(["init", &t.path.display().to_string()]).output();
        let _ = Command::new("git").args(["-C", &t.path.display().to_string(), "config", "user.name", "Test"]).output();
        let _ = Command::new("git").args(["-C", &t.path.display().to_string(), "config", "user.email", "test@test.local"]).output();
        t.file("README.md", b"# Test Repo\n");
        let _ = Command::new("git").args(["-C", &t.path.display().to_string(), "add", "."]).output();
        let _ = Command::new("git").args(["-C", &t.path.display().to_string(), "commit", "-m", "Initial commit"]).output();

        let res = git_log_for_repo(&t.path.display().to_string(), 5);
        assert!(res.contains("Initial commit"), "{res}");
    }
}
