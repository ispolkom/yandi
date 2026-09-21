//! The supervisor against the REAL Python Core (pet/core_main.py), as separate processes.
//!
//! Needs a Python with the core's requirements (uvicorn, cryptography, fastapi …): set YANDI_TEST_PYTHON. These tests are
//! `#[ignore]`d so that a plain `cargo test` never pretends to have run them; run them with
//!     YANDI_TEST_PYTHON=/path/to/python cargo test --offline -p yandi-core-supervisor --test real_core -- --ignored
#![cfg(unix)]
use core_supervisor::*;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;
use tokio::sync::broadcast;
use zeroize::Zeroizing;

fn repo() -> PathBuf {
    PathBuf::from(concat!(env!("CARGO_MANIFEST_DIR"), "/../.."))
}

fn python() -> PathBuf {
    std::env::var_os("YANDI_TEST_PYTHON")
        .expect("set YANDI_TEST_PYTHON to a python with the core's requirements")
        .into()
}

fn config(tmp: &std::path::Path, shell: bool) -> SupervisorConfig {
    let mut cfg = SupervisorConfig::new(python(), repo(), tmp.join("state"), tmp.join("run"));
    cfg.serve_args = if shell {
        vec!["-m".into(), "pet.core_main".into(), "--shell".into()]
    } else {
        vec!["-m".into(), "pet.core_main".into()]
    };
    cfg.inherit_env = false;
    cfg.extra_env = vec![
        ("PATH".into(), std::env::var_os("PATH").unwrap()),
        ("HOME".into(), tmp.join("home").into()),
        ("XDG_DATA_HOME".into(), tmp.join("data").into()),
        ("YANDI_TEST_MODE".into(), "1".into()),
        ("PYTHONDONTWRITEBYTECODE".into(), "1".into()),
        (
            "YANDI_WEB_SETTINGS".into(),
            tmp.join("web_settings.json").into(),
        ),
    ];
    std::fs::create_dir_all(tmp.join("home")).unwrap();
    cfg.log_path = Some(tmp.join("core.log"));
    cfg.startup_timeout = Duration::from_secs(120);
    cfg.backoff = BackoffPolicy {
        initial: Duration::from_millis(100),
        max: Duration::from_secs(1),
        factor: 2,
    };
    cfg.shutdown_deadline = Duration::from_secs(5);
    cfg
}

fn key() -> KeyProvider {
    Arc::new(|| Some(Zeroizing::new([0x42u8; 32])))
}

async fn until(
    rx: &mut broadcast::Receiver<Event>,
    secs: u64,
    pred: impl Fn(&Event) -> bool,
) -> Vec<Event> {
    let mut seen = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
    loop {
        match tokio::time::timeout_at(deadline, rx.recv()).await {
            Ok(Ok(ev)) => {
                let hit = pred(&ev);
                seen.push(ev);
                if hit {
                    return seen;
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            other => panic!("event not seen in {secs}s: {seen:?} ({other:?})"),
        }
    }
}

fn pid_of(events: &[Event]) -> u32 {
    events
        .iter()
        .rev()
        .find_map(|e| {
            if let Event::Spawned { pid, .. } = e {
                Some(*pid)
            } else {
                None
            }
        })
        .expect("no Spawned event")
}

fn alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

async fn full_lifecycle(shell: bool) {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path(), shell);
    let runtime = cfg.runtime_base.clone();
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    let seen = until(&mut rx, 180, |e| matches!(e, Event::Ready)).await;
    let first_pid = pid_of(&seen);
    assert_eq!(sup.status().phase, Phase::Ready);
    // the real core removed its secret file after reading it: only its port file is left in the launch directory
    let launch_dirs: Vec<_> = std::fs::read_dir(&runtime)
        .unwrap()
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with("core-"))
        .collect();
    assert_eq!(launch_dirs.len(), 1);
    assert!(
        !launch_dirs[0].path().join("launch-secret").exists(),
        "the core left its launch secret on disk"
    );
    assert!(launch_dirs[0].path().join("port").exists());

    // the core dies (someone SIGKILLs it): the supervisor survives, restarts it, and unlocks the new process again
    unsafe { libc::kill(first_pid as i32, libc::SIGKILL) };
    let seen = until(&mut rx, 180, |e| matches!(e, Event::Ready)).await;
    assert!(seen.iter().any(|e| matches!(e, Event::CoreExited { .. })));
    let second_pid = pid_of(&seen);
    assert_ne!(first_pid, second_pid);
    assert_eq!(sup.status().phase, Phase::Ready);

    let report = sup.shutdown().await;
    assert!(
        report.graceful && !report.terminated && !report.killed,
        "the real core must stop by itself on /v1/shutdown: {report:?}"
    );
    for _ in 0..40 {
        if !alive(second_pid) {
            break;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    assert!(
        !alive(second_pid) && !alive(first_pid),
        "an orphan core is left"
    );
    assert!(std::fs::read_dir(&runtime)
        .unwrap()
        .flatten()
        .all(|e| !e.file_name().to_string_lossy().starts_with("core-")));
    let log = std::fs::read_to_string(tmp.path().join("core.log")).unwrap_or_default();
    let key_b64 = derive_core_key(&[0x42u8; 32]).to_base64();
    assert!(
        !log.contains(key_b64.as_str()),
        "the derived key reached the core's log"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs YANDI_TEST_PYTHON"]
async fn the_real_core_lifecycle_shell_spawn_unlock_crash_restart_shutdown() {
    full_lifecycle(true).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs YANDI_TEST_PYTHON"]
async fn the_real_core_lifecycle_with_the_real_application_behind_the_gate() {
    full_lifecycle(false).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
#[ignore = "needs YANDI_TEST_PYTHON"]
async fn a_core_provisioned_for_another_key_refuses_and_is_not_restarted() {
    let tmp = tempfile::tempdir().unwrap();
    let cfg = config(tmp.path(), true);
    // provision the state with a different key, exactly as a node whose master key later changed would find it
    let mut prov = std::process::Command::new(python())
        .args(["-m", "pet.core_main", "provision"])
        .current_dir(repo())
        .env_clear()
        .env("PATH", std::env::var_os("PATH").unwrap())
        .env("YANDI_CORE_STATE_DIR", tmp.path().join("state"))
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .stdin(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    use std::io::Write;
    prov.stdin
        .take()
        .unwrap()
        .write_all(format!("{}\n", derive_core_key(&[0x01u8; 32]).to_base64().as_str()).as_bytes())
        .unwrap();
    assert!(prov.wait().unwrap().success());
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    let seen = until(&mut rx, 120, |e| matches!(e, Event::Failure { .. })).await;
    assert!(
        matches!(
            seen.last(),
            Some(Event::Failure {
                category: FailureCategory::CoreUnlockRefused,
                ..
            })
        ),
        "{seen:?}"
    );
    let pid = pid_of(&seen);
    tokio::time::sleep(Duration::from_millis(500)).await;
    assert!(!alive(pid), "the refused core was left running");
    assert_eq!(
        sup.status().failure,
        Some(FailureCategory::CoreUnlockRefused)
    );
    sup.shutdown().await;
}
