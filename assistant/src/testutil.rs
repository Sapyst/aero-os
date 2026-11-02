//! Test helpers. Real files live only in a fresh folder under $HOME/.cache/aero-temp/,
//! and only that folder is removed afterwards.

use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicUsize, Ordering};

static COUNTER: AtomicUsize = AtomicUsize::new(0);

pub struct TempDir {
    pub path: PathBuf,
}

impl TempDir {
    pub fn new(tag: &str) -> TempDir {
        let home = std::env::var("HOME").expect("HOME");
        let base = Path::new(&home).join(".cache/aero-temp");
        let nanos = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.subsec_nanos()).unwrap_or(0);
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let path = base.join(format!("aero-test-{tag}-{}-{n}-{nanos}", std::process::id()));
        assert!(!path.exists(), "temporary folder already exists: {}", path.display());
        fs::create_dir_all(&path).expect("create temporary folder");
        TempDir { path }
    }

    pub fn file(&self, rel: &str, content: &[u8]) -> PathBuf {
        let p = self.path.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        fs::write(&p, content).unwrap();
        p
    }

    pub fn dir(&self, rel: &str) -> PathBuf {
        let p = self.path.join(rel);
        fs::create_dir_all(&p).unwrap();
        p
    }

    pub fn link(&self, target: &str, rel: &str) -> PathBuf {
        let p = self.path.join(rel);
        fs::create_dir_all(p.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink(target, &p).unwrap();
        p
    }

    pub fn s(&self) -> String {
        self.path.display().to_string()
    }
}

impl Drop for TempDir {
    fn drop(&mut self) {
        let home = std::env::var("HOME").unwrap_or_default();
        let prefix = format!("{home}/.cache/aero-temp/aero-test-");
        // Never remove anything except the folder this helper created.
        if !home.is_empty() && self.path.display().to_string().starts_with(&prefix) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }
}
