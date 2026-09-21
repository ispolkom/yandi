// src/managed_core.rs
//! The node as the owner of the production Core process (contract 1.0-rc1, P1b).
//!
//! Off by default: nothing consumes the Core over `/v1` yet (the web UI moves onto it in P3/P4), and a node on a machine without
//! Python must keep starting. Switch it on with `YANDI_MANAGED_CORE=1`. Then this is THE way a production Core runs: one node,
//! one Core process it spawned and can name, unlocked with a key derived from the node's master key, restarted when it dies.
//! The old `./start.sh` PET on :9010 is the legacy/development path, not a second production entrance to be relied on.
//!
//! Environment (read once at start):
//! - `YANDI_MANAGED_CORE=1`     enable
//! - `YANDI_CORE_PYTHON`        interpreter (default `python3`)
//! - `YANDI_CORE_ROOT`          the repository root that holds `pet/` (default: the current directory)
//! - `YANDI_CORE_STATE_DIR`     the Core's state directory (default `~/.local/share/yandi/core`)

#[cfg(unix)]
pub use unix::{start_from_env, ManagedCoreConfig};

#[cfg(unix)]
mod unix {
    use crate::web::auth::AuthState;
    use core_supervisor::{CoreSupervisor, KeyProvider, RuntimeBase, SupervisorConfig, Zeroizing};
    use std::path::PathBuf;
    use std::sync::Arc;

    /// What the environment asked for. Separate from the reading of the environment so that it can be tested.
    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct ManagedCoreConfig {
        pub python: PathBuf,
        pub root: PathBuf,
        pub state_dir: PathBuf,
    }

    impl ManagedCoreConfig {
        /// `None` unless `YANDI_MANAGED_CORE` is exactly `1`.
        pub fn from_lookup(
            get: impl Fn(&str) -> Option<String>,
            home: Option<PathBuf>,
            cwd: PathBuf,
        ) -> Option<ManagedCoreConfig> {
            if get("YANDI_MANAGED_CORE").as_deref() != Some("1") {
                return None;
            }
            let state_dir = get("YANDI_CORE_STATE_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    home.unwrap_or_else(|| PathBuf::from("."))
                        .join(".local/share/yandi/core")
                });
            Some(ManagedCoreConfig {
                python: get("YANDI_CORE_PYTHON")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| "python3".into()),
                root: get("YANDI_CORE_ROOT").map(PathBuf::from).unwrap_or(cwd),
                state_dir,
            })
        }
    }

    /// Start supervising the Core if the environment says so. The master key is taken from the node's own auth state the moment
    /// it exists (loaded automatically on this machine, or after first-time setup / rebind in the web UI); until then the Core is
    /// kept locked. Nothing here invents a key.
    pub fn start_from_env(auth: &AuthState) -> Option<CoreSupervisor> {
        let cfg = ManagedCoreConfig::from_lookup(
            |k| std::env::var(k).ok(),
            dirs::home_dir(),
            std::env::current_dir().unwrap_or_else(|_| ".".into()),
        )?;
        let runtime = RuntimeBase::default_path();
        let mut sup_cfg = SupervisorConfig::new(cfg.python, cfg.root, cfg.state_dir, runtime);
        sup_cfg.log_path = dirs::home_dir().map(|h| h.join(".local/share/yandi/core.log"));
        let auth = auth.clone();
        let provider: KeyProvider = Arc::new(move || auth.get_master_key().map(Zeroizing::new));
        match CoreSupervisor::start(sup_cfg, provider) {
            Ok((sup, _events)) => {
                println!("🧠 Core: supervised by this node (separate process, locked until unlocked with a key derived from the master key)");
                Some(sup)
            }
            Err(e) => {
                eprintln!("🧠 Core: not started — {e}");
                None
            }
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;
        use std::collections::HashMap;

        fn lookup(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
            let map: HashMap<String, String> = pairs
                .iter()
                .map(|(k, v)| (k.to_string(), v.to_string()))
                .collect();
            move |k| map.get(k).cloned()
        }

        #[test]
        fn it_is_off_unless_explicitly_switched_on() {
            for pairs in [
                vec![],
                vec![("YANDI_MANAGED_CORE", "0")],
                vec![("YANDI_MANAGED_CORE", "true")],
                vec![("YANDI_MANAGED_CORE", "")],
            ] {
                assert!(ManagedCoreConfig::from_lookup(
                    lookup(&pairs),
                    Some("/home/u".into()),
                    "/repo".into()
                )
                .is_none());
            }
        }

        #[test]
        fn defaults_and_overrides() {
            let c = ManagedCoreConfig::from_lookup(
                lookup(&[("YANDI_MANAGED_CORE", "1")]),
                Some("/home/u".into()),
                "/repo".into(),
            )
            .unwrap();
            assert_eq!(
                c,
                ManagedCoreConfig {
                    python: "python3".into(),
                    root: "/repo".into(),
                    state_dir: "/home/u/.local/share/yandi/core".into()
                }
            );
            let c = ManagedCoreConfig::from_lookup(
                lookup(&[
                    ("YANDI_MANAGED_CORE", "1"),
                    ("YANDI_CORE_PYTHON", "/v/bin/python"),
                    ("YANDI_CORE_ROOT", "/src"),
                    ("YANDI_CORE_STATE_DIR", "/s"),
                ]),
                None,
                "/repo".into(),
            )
            .unwrap();
            assert_eq!(
                c,
                ManagedCoreConfig {
                    python: "/v/bin/python".into(),
                    root: "/src".into(),
                    state_dir: "/s".into()
                }
            );
        }
    }
}
