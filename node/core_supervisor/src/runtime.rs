//! The per-user runtime directory and the per-launch artifacts in it.
//!
//! Layout (all owned by the current user):
//! ```text
//! <runtime_base>/            0700
//!   core-<node pid>-<rand>/  0700   one per launch, created fresh, removed afterwards
//!     launch-secret          0600   32 random bytes, hex; the Core reads it once and removes it
//!     port                   0600   written by the Core
//! ```
use rand::RngCore;
use std::fs::{self, DirBuilder, OpenOptions};
use std::io::Write;
use std::os::unix::fs::{DirBuilderExt, MetadataExt, OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use zeroize::Zeroizing;

#[derive(Debug)]
pub enum RuntimeError {
    Unsafe(&'static str),
    Io(std::io::Error),
}

impl std::fmt::Display for RuntimeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            RuntimeError::Unsafe(why) => write!(f, "the runtime directory is not safe: {why}"),
            RuntimeError::Io(_) => write!(f, "the runtime directory could not be prepared"),
        }
    }
}

impl From<std::io::Error> for RuntimeError {
    fn from(e: std::io::Error) -> Self {
        RuntimeError::Io(e)
    }
}

fn euid() -> u32 {
    unsafe { libc::geteuid() }
}

/// 32 random bytes, hex. A new one for every launch; it is never derived from anything and never reused.
pub struct LaunchSecret(Zeroizing<String>);

impl LaunchSecret {
    pub fn generate() -> Self {
        let mut raw = Zeroizing::new([0u8; 32]);
        rand::rngs::OsRng.fill_bytes(raw.as_mut_slice());
        LaunchSecret(Zeroizing::new(hex::encode(raw.as_slice())))
    }

    pub fn expose(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for LaunchSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("LaunchSecret(<redacted>)")
    }
}

pub struct RuntimeBase {
    path: PathBuf,
}

impl RuntimeBase {
    /// `$XDG_RUNTIME_DIR/yandi-core` where the system provides a per-user runtime directory, else `/tmp/yandi-core-<uid>`.
    pub fn default_path() -> PathBuf {
        match std::env::var_os("XDG_RUNTIME_DIR") {
            Some(dir) if !dir.is_empty() => PathBuf::from(dir).join("yandi-core"),
            _ => PathBuf::from(format!("/tmp/yandi-core-{}", euid())),
        }
    }

    /// Create the base (0700) or accept an existing one only if it is a real directory of this user that nobody else can enter.
    pub fn open(path: impl Into<PathBuf>) -> Result<Self, RuntimeError> {
        let path = path.into();
        match fs::symlink_metadata(&path) {
            Ok(meta) => {
                if meta.file_type().is_symlink() {
                    return Err(RuntimeError::Unsafe("it is a symbolic link"));
                }
                if !meta.is_dir() {
                    return Err(RuntimeError::Unsafe("it is not a directory"));
                }
                if meta.uid() != euid() {
                    return Err(RuntimeError::Unsafe("it belongs to another user"));
                }
                if meta.mode() & 0o077 != 0 {
                    return Err(RuntimeError::Unsafe("it can be entered by others"));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)?;
                }
                DirBuilder::new().mode(0o700).create(&path)?;
            }
            Err(e) => return Err(e.into()),
        }
        Ok(RuntimeBase { path })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    /// A fresh directory and a fresh secret file for one launch. The file is created exclusively (`create_new`), without
    /// following links, with mode 0600 from the first byte.
    pub fn new_launch(&self) -> Result<(LaunchDir, LaunchSecret), RuntimeError> {
        let mut tag = [0u8; 6];
        rand::rngs::OsRng.fill_bytes(&mut tag);
        let dir = self
            .path
            .join(format!("core-{}-{}", std::process::id(), hex::encode(tag)));
        DirBuilder::new().mode(0o700).create(&dir)?;
        let launch = LaunchDir {
            secret_path: dir.join("launch-secret"),
            port_path: dir.join("port"),
            dir,
        };
        let secret = LaunchSecret::generate();
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .custom_flags(libc::O_NOFOLLOW)
            .open(&launch.secret_path)?;
        file.write_all(secret.expose().as_bytes())?;
        file.write_all(b"\n")?;
        file.sync_all()?;
        Ok((launch, secret))
    }

    /// Remove launch directories that a node which is no longer running left behind. Only what is provably ours: a real
    /// directory of this user, mode 0700, named `core-<pid>-…` where `<pid>` is a process that does not exist. It never kills or
    /// signals anything and never touches any other name.
    pub fn clean_stale(&self) -> usize {
        let mut removed = 0;
        let Ok(entries) = fs::read_dir(&self.path) else {
            return 0;
        };
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let Some(rest) = name.strip_prefix("core-") else {
                continue;
            };
            let Some((pid_text, _)) = rest.split_once('-') else {
                continue;
            };
            let Ok(pid) = pid_text.parse::<i32>() else {
                continue;
            };
            if pid <= 0 || pid as u32 == std::process::id() {
                continue;
            }
            let Ok(meta) = fs::symlink_metadata(entry.path()) else {
                continue;
            };
            if meta.file_type().is_symlink()
                || !meta.is_dir()
                || meta.uid() != euid()
                || meta.mode() & 0o077 != 0
            {
                continue;
            }
            let alive = unsafe { libc::kill(pid, 0) } == 0
                || std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM);
            if !alive && fs::remove_dir_all(entry.path()).is_ok() {
                removed += 1;
            }
        }
        removed
    }
}

/// One launch's private directory. Removing it (also on drop) removes the secret file if the Core has not.
pub struct LaunchDir {
    dir: PathBuf,
    secret_path: PathBuf,
    port_path: PathBuf,
}

impl LaunchDir {
    pub fn dir(&self) -> &Path {
        &self.dir
    }
    pub fn secret_path(&self) -> &Path {
        &self.secret_path
    }
    pub fn port_path(&self) -> &Path {
        &self.port_path
    }

    /// The port the Core wrote, if it has.
    pub fn read_port(&self) -> Option<u16> {
        fs::read_to_string(&self.port_path)
            .ok()?
            .trim()
            .parse()
            .ok()
            .filter(|p| *p != 0)
    }

    pub fn cleanup(&self) {
        let _ = fs::remove_dir_all(&self.dir);
    }

    pub fn dir_mode(&self) -> Option<u32> {
        fs::metadata(&self.dir)
            .ok()
            .map(|m| m.permissions().mode() & 0o777)
    }
}

impl Drop for LaunchDir {
    fn drop(&mut self) {
        self.cleanup();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_new_launch_has_a_private_directory_and_a_private_secret_file() {
        let tmp = tempfile::tempdir().unwrap();
        let base = RuntimeBase::open(tmp.path().join("base")).unwrap();
        assert_eq!(
            fs::metadata(base.path()).unwrap().permissions().mode() & 0o777,
            0o700
        );
        let (launch, secret) = base.new_launch().unwrap();
        assert_eq!(launch.dir_mode(), Some(0o700));
        let meta = fs::symlink_metadata(launch.secret_path()).unwrap();
        assert!(meta.is_file());
        assert_eq!(meta.permissions().mode() & 0o777, 0o600);
        assert_eq!(meta.uid(), euid());
        let text = fs::read_to_string(launch.secret_path()).unwrap();
        assert_eq!(text.trim(), secret.expose());
        assert_eq!(secret.expose().len(), 64);
        assert!(secret.expose().bytes().all(|b| b.is_ascii_hexdigit()));
    }

    #[test]
    fn every_launch_gets_a_different_secret_and_directory() {
        let tmp = tempfile::tempdir().unwrap();
        let base = RuntimeBase::open(tmp.path().join("base")).unwrap();
        let (a, sa) = base.new_launch().unwrap();
        let (b, sb) = base.new_launch().unwrap();
        assert_ne!(sa.expose(), sb.expose());
        assert_ne!(a.dir(), b.dir());
    }

    #[test]
    fn cleanup_removes_the_directory_and_drop_does_too() {
        let tmp = tempfile::tempdir().unwrap();
        let base = RuntimeBase::open(tmp.path().join("base")).unwrap();
        let (launch, _s) = base.new_launch().unwrap();
        let dir = launch.dir().to_path_buf();
        launch.cleanup();
        assert!(!dir.exists());
        let (launch2, _s2) = base.new_launch().unwrap();
        let dir2 = launch2.dir().to_path_buf();
        drop(launch2);
        assert!(!dir2.exists());
    }

    #[test]
    fn a_base_that_others_can_enter_or_that_is_a_link_is_refused() {
        let tmp = tempfile::tempdir().unwrap();
        let open_dir = tmp.path().join("open");
        DirBuilder::new().mode(0o755).create(&open_dir).unwrap();
        assert!(matches!(
            RuntimeBase::open(&open_dir),
            Err(RuntimeError::Unsafe(_))
        ));
        let target = tmp.path().join("target");
        DirBuilder::new().mode(0o700).create(&target).unwrap();
        std::os::unix::fs::symlink(&target, tmp.path().join("link")).unwrap();
        assert!(matches!(
            RuntimeBase::open(tmp.path().join("link")),
            Err(RuntimeError::Unsafe(_))
        ));
        let file = tmp.path().join("file");
        fs::write(&file, "x").unwrap();
        assert!(matches!(
            RuntimeBase::open(&file),
            Err(RuntimeError::Unsafe(_))
        ));
    }

    #[test]
    fn stale_cleanup_removes_only_what_is_provably_ours_and_dead() {
        let tmp = tempfile::tempdir().unwrap();
        let base = RuntimeBase::open(tmp.path().join("base")).unwrap();
        let dead_pid = 4_000_000; // above pid_max on any default Linux configuration
        let stale = base.path().join(format!("core-{dead_pid}-abc"));
        DirBuilder::new().mode(0o700).create(&stale).unwrap();
        let alive = base
            .path()
            .join(format!("core-{}-abc", unsafe { libc::getppid() }));
        DirBuilder::new().mode(0o700).create(&alive).unwrap();
        let open_perm = base.path().join(format!("core-{dead_pid}-open"));
        DirBuilder::new().mode(0o755).create(&open_perm).unwrap();
        let other = base.path().join("something-else");
        DirBuilder::new().mode(0o700).create(&other).unwrap();
        let (mine, _s) = base.new_launch().unwrap();
        assert_eq!(base.clean_stale(), 1);
        assert!(!stale.exists());
        assert!(alive.exists() && open_perm.exists() && other.exists() && mine.dir().exists());
    }
}
