//! path_info: detailed read-only inspection of a filesystem path.
//! Reports: type, symlink target, owner, permissions, size, dates, and which processes use it.
//! Files refused by guard::readable_path are never inspected or printed.

use crate::fsinfo;
use crate::guard;
use std::fs;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};

fn format_mode(mode: u32, is_dir: bool, is_link: bool) -> String {
    let t = if is_link { 'l' } else if is_dir { 'd' } else { '-' };
    let rwx = |m: u32| {
        format!(
            "{}{}{}",
            if m & 4 != 0 { 'r' } else { '-' },
            if m & 2 != 0 { 'w' } else { '-' },
            if m & 1 != 0 { 'x' } else { '-' }
        )
    };
    format!(
        "{}{}{}{} ({:04o})",
        t,
        rwx((mode >> 6) & 7),
        rwx((mode >> 3) & 7),
        rwx(mode & 7),
        mode & 0o7777
    )
}

fn human_size(bytes: u64) -> String {
    if bytes < 1024 {
        format!("{bytes} B")
    } else if bytes < 1024 * 1024 {
        format!("{bytes} B ({:.1} KiB)", bytes as f64 / 1024.0)
    } else if bytes < 1024 * 1024 * 1024 {
        format!("{bytes} B ({:.1} MiB)", bytes as f64 / (1024.0 * 1024.0))
    } else {
        format!("{bytes} B ({:.2} GiB)", bytes as f64 / (1024.0 * 1024.0 * 1024.0))
    }
}

fn expand_tilde(path: &str) -> PathBuf {
    let home = fsinfo::home();
    if path == "~" {
        return PathBuf::from(&home);
    }
    if let Some(rest) = path.strip_prefix("~/") {
        return PathBuf::from(format!("{home}/{rest}"));
    }
    if path.starts_with("$HOME/") {
        return PathBuf::from(format!("{home}/{}", &path[6..]));
    }
    if path.starts_with("${HOME}/") {
        return PathBuf::from(format!("{home}/{}", &path[8..]));
    }
    PathBuf::from(path)
}

pub fn path_info(path_str: &str) -> String {
    let trimmed = path_str.trim();
    if trimmed.is_empty() {
        return "ERROR: empty path".into();
    }
    let expanded = expand_tilde(trimmed);
    let clean = fsinfo::clean_path(&expanded);
    let s = clean.to_string_lossy().to_string();

    if let Err(e) = guard::readable_path(&s) {
        return format!("REFUSED: {e}");
    }
    if let Ok(canon) = fs::canonicalize(&clean) {
        let cs = canon.to_string_lossy().to_string();
        if let Err(e) = guard::readable_path(&cs) {
            return format!("REFUSED: {e}");
        }
    }

    let meta = match fs::symlink_metadata(&clean) {
        Ok(m) => m,
        Err(_) => {
            let (uses, hidden) = fsinfo::process_uses();
            let matched: Vec<_> = uses.iter().filter(|u| u.path == s).collect();
            let mut out = format!("Path: {s}\nStatus: does not exist on disk\n");
            if !matched.is_empty() {
                out.push_str("Processes referencing this path:\n");
                for u in matched {
                    out.push_str(&format!("  PID {} ({}): {}\n", u.pid, u.name, u.how));
                }
            } else {
                out.push_str("Processes using this path: none\n");
            }
            if hidden > 0 {
                out.push_str(&format!("({hidden} system processes could not be inspected due to permissions)\n"));
            }
            return out;
        }
    };

    let ft = meta.file_type();
    let is_link = ft.is_symlink();
    let is_dir = ft.is_dir();
    let type_name = if is_link {
        "symlink"
    } else if is_dir {
        "directory"
    } else if ft.is_file() {
        "regular file"
    } else {
        let mode = meta.mode();
        if mode & 0o170000 == 0o010000 {
            "FIFO (named pipe)"
        } else if mode & 0o170000 == 0o140000 {
            "socket"
        } else if mode & 0o170000 == 0o060000 {
            "block device"
        } else if mode & 0o170000 == 0o020000 {
            "character device"
        } else {
            "other"
        }
    };

    let mut out = format!("Path: {s}\nType: {type_name}\n");

    if is_link {
        match fs::read_link(&clean) {
            Ok(target) => {
                let target_s = target.to_string_lossy().to_string();
                let full_target = if target.is_relative() {
                    fsinfo::clean_path(&clean.parent().unwrap_or(Path::new("/")).join(&target))
                } else {
                    fsinfo::clean_path(&target)
                };
                let status = if full_target.exists() {
                    "valid"
                } else {
                    "dangling (target does not exist)"
                };
                out.push_str(&format!("Symlink target: {target_s} [{status}]\n"));
            }
            Err(e) => out.push_str(&format!("Symlink target: could not read ({e})\n")),
        }
    }

    if let Ok(canon) = fs::canonicalize(&clean) {
        if canon != clean {
            out.push_str(&format!("Canonical path: {}\n", canon.display()));
        }
    }

    let uid = meta.uid();
    let gid = meta.gid();
    let user = fsinfo::user_name(uid);
    let group = fsinfo::group_name(gid);
    out.push_str(&format!("Owner: {user} (UID {uid})\nGroup: {group} (GID {gid})\n"));

    let perms = format_mode(meta.mode(), is_dir, is_link);
    out.push_str(&format!("Permissions: {perms}\n"));

    let size = meta.size();
    out.push_str(&format!("Size: {}\n", human_size(size)));

    if is_dir {
        if let Ok(entries) = fs::read_dir(&clean) {
            let count = entries.flatten().count();
            out.push_str(&format!("Entries: {count} items\n"));
        }
    }

    let mtime = meta.mtime();
    let atime = meta.atime();
    let ctime = meta.ctime();
    out.push_str(&format!("Modified: {} ({})\n", fsinfo::utc(mtime), fsinfo::age(mtime)));
    out.push_str(&format!("Accessed: {} ({})\n", fsinfo::utc(atime), fsinfo::age(atime)));
    out.push_str(&format!("Changed:  {} ({})\n", fsinfo::utc(ctime), fsinfo::age(ctime)));

    let (uses, hidden) = fsinfo::process_uses();
    let mut relevant_uses = vec![];
    for u in &uses {
        if u.path == s {
            relevant_uses.push(format!("PID {} ({}): {} ({})", u.pid, u.name, u.how, u.path));
        } else if is_dir && fsinfo::is_within(&u.path, &s) {
            relevant_uses.push(format!("PID {} ({}): {} ({})", u.pid, u.name, u.how, u.path));
        }
    }
    relevant_uses.sort();
    relevant_uses.dedup();

    if relevant_uses.is_empty() {
        out.push_str("Processes using this path: none\n");
    } else {
        out.push_str(&format!("Processes using this path ({}):\n", relevant_uses.len()));
        for u in relevant_uses.iter().take(20) {
            out.push_str(&format!("  {u}\n"));
        }
        if relevant_uses.len() > 20 {
            out.push_str(&format!("  [... {} more processes ...]\n", relevant_uses.len() - 20));
        }
    }

    if hidden > 0 {
        out.push_str(&format!("Note: {hidden} system processes could not be inspected for open files.\n"));
    }

    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::testutil::TempDir;
    use std::process::{Command, Stdio};

    #[test]
    fn path_info_reports_file_details() {
        let t = TempDir::new("pathinfo");
        let f = t.file("test.txt", b"Hello, world!\n");
        let res = path_info(&f.display().to_string());
        assert!(res.contains("Type: regular file"), "{res}");
        assert!(res.contains("Size: 14 B"), "{res}");
        assert!(res.contains("Owner:"), "{res}");
        assert!(res.contains("Permissions: -rw"), "{res}");
        assert!(res.contains("Modified:"), "{res}");
    }

    #[test]
    fn path_info_reports_symlink_and_dangling() {
        let t = TempDir::new("pathinfo-link");
        let _f = t.file("target.txt", b"data");
        let valid_link = t.link("target.txt", "valid.lnk");
        let dangling_link = t.link("/nonexistent/file/path", "dangling.lnk");

        let res_valid = path_info(&valid_link.display().to_string());
        assert!(res_valid.contains("Type: symlink"), "{res_valid}");
        assert!(res_valid.contains("target.txt [valid]"), "{res_valid}");

        let res_dangling = path_info(&dangling_link.display().to_string());
        assert!(res_dangling.contains("Type: symlink"), "{res_dangling}");
        assert!(res_dangling.contains("dangling (target does not exist)"), "{res_dangling}");
    }

    #[test]
    fn path_info_detects_processes_using_path() {
        let t = TempDir::new("pathinfo-proc");
        let dir = t.dir("active");
        let mut child = Command::new("cat")
            .stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .current_dir(&dir)
            .spawn()
            .expect("spawn test process");

        std::thread::sleep(std::time::Duration::from_millis(50));
        let res = path_info(&dir.display().to_string());
        let _ = child.kill();
        let _ = child.wait();

        assert!(res.contains("Processes using this path"), "{res}");
        assert!(res.contains("cat"), "{res}");
        assert!(res.contains("working directory"), "{res}");
    }

    #[test]
    fn path_info_refuses_secret_files() {
        assert!(path_info("/etc/shadow").starts_with("REFUSED:"));
        let home = fsinfo::home();
        assert!(path_info(&format!("{home}/.ssh/id_rsa")).starts_with("REFUSED:"));
    }
}
