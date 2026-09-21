//! Supervision semantics, proven against a stand-in Core (tests/support/fake_core.py) that follows the real launch protocol and
//! misbehaves on cue. The real Python Core is exercised in tests/real_core.rs.
#![cfg(unix)]
use core_supervisor::*;
use sha2::{Digest, Sha256};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::broadcast;
use zeroize::Zeroizing;

const MASTER: [u8; 32] = [9u8; 32];

struct Env {
    _tmp: tempfile::TempDir,
    work: PathBuf,
    state: PathBuf,
    runtime: PathBuf,
    cfg: SupervisorConfig,
}

fn python() -> PathBuf {
    std::env::var_os("YANDI_TEST_PYTHON")
        .map(Into::into)
        .unwrap_or_else(|| "python3".into())
}

fn env_with(launches: &[&str], extra: serde_json::Value) -> Env {
    let tmp = tempfile::tempdir().unwrap();
    let work = tmp.path().join("work");
    let state = tmp.path().join("state");
    let runtime = tmp.path().join("run");
    std::fs::create_dir_all(&work).unwrap();
    let mut script =
        serde_json::json!({"work_dir": work, "launches": launches, "default": "normal"});
    if let Some(obj) = extra.as_object() {
        for (k, v) in obj {
            script[k] = v.clone();
        }
    }
    let script_path = tmp.path().join("script.json");
    std::fs::write(&script_path, script.to_string()).unwrap();
    let fake = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/support/fake_core.py");
    let mut cfg = SupervisorConfig::new(python(), tmp.path(), &state, &runtime);
    cfg.serve_args = vec![fake.into()];
    cfg.provision_args = vec![fake.into(), "provision".into()];
    cfg.inherit_env = false;
    cfg.extra_env = vec![
        (
            "PATH".into(),
            std::env::var_os("PATH").unwrap_or_else(|| "/usr/bin:/bin".into()),
        ),
        ("FAKE_CORE_SCRIPT".into(), script_path.into()),
    ];
    // the core's output goes to a file: an orphan must not be able to hold the test runner's pipes open
    cfg.log_path = Some(tmp.path().join("core.log"));
    cfg.poll_interval = Duration::from_millis(20);
    cfg.startup_timeout = Duration::from_secs(8);
    cfg.ready_timeout = Duration::from_secs(8);
    cfg.shutdown_deadline = Duration::from_secs(3);
    cfg.term_grace = Duration::from_secs(1);
    cfg.backoff = BackoffPolicy {
        initial: Duration::from_millis(50),
        max: Duration::from_millis(400),
        factor: 2,
    };
    cfg.restart_limit = 3;
    cfg.failure_window = Duration::from_secs(60);
    Env {
        _tmp: tmp,
        work,
        state,
        runtime,
        cfg,
    }
}

fn env(launches: &[&str]) -> Env {
    env_with(launches, serde_json::json!({}))
}

fn key() -> KeyProvider {
    Arc::new(|| Some(Zeroizing::new(MASTER)))
}

fn records(work: &PathBuf) -> Vec<serde_json::Value> {
    std::fs::read_to_string(work.join("records.jsonl"))
        .unwrap_or_default()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn of_kind(work: &PathBuf, kind: &str) -> Vec<serde_json::Value> {
    records(work)
        .into_iter()
        .filter(|r| r["event"] == kind)
        .collect()
}

fn alive(pid: u64) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

async fn until(
    rx: &mut broadcast::Receiver<Event>,
    secs: u64,
    pred: impl Fn(&Event) -> bool,
) -> Vec<(Instant, Event)> {
    let mut seen = Vec::new();
    let deadline = tokio::time::Instant::now() + Duration::from_secs(secs);
    loop {
        match tokio::time::timeout_at(deadline, rx.recv()).await {
            Ok(Ok(ev)) => {
                let hit = pred(&ev);
                seen.push((Instant::now(), ev));
                if hit {
                    return seen;
                }
            }
            Ok(Err(broadcast::error::RecvError::Lagged(_))) => continue,
            other => panic!(
                "no such event within {secs}s; saw {:?} ({other:?})",
                seen.iter().map(|(_, e)| e).collect::<Vec<_>>()
            ),
        }
    }
}

fn kinds(events: &[(Instant, Event)]) -> Vec<String> {
    events
        .iter()
        .map(|(_, e)| match e {
            Event::Spawned { .. } => "spawned".to_owned(),
            Event::AwaitingState { state } => format!("awaiting:{state}"),
            Event::HealthState { state } => format!("health:{state}"),
            Event::UnlockSent => "unlock".to_owned(),
            Event::Ready => "ready".to_owned(),
            Event::CoreExited { .. } => "exited".to_owned(),
            Event::LaunchFailed { category, .. } => format!("launch_failed:{}", category.as_str()),
            Event::RestartScheduled { .. } => "restart".to_owned(),
            Event::RestartCeilingReached => "ceiling".to_owned(),
            Event::Failure { category, .. } => format!("failure:{}", category.as_str()),
            Event::ShutdownRequested => "shutdown_requested".to_owned(),
            Event::Stopped { .. } => "stopped".to_owned(),
        })
        .collect()
}

fn sha_hex(text: &str) -> String {
    hex::encode(Sha256::digest(text.as_bytes()))
}

fn no_core_dirs_left(runtime: &PathBuf) -> bool {
    std::fs::read_dir(runtime)
        .map(|d| {
            d.flatten()
                .all(|e| !e.file_name().to_string_lossy().starts_with("core-"))
        })
        .unwrap_or(true)
}

async fn all_dead(work: &PathBuf) -> bool {
    let pids: Vec<u64> = of_kind(work, "start")
        .iter()
        .map(|r| r["pid"].as_u64().unwrap())
        .collect();
    for _ in 0..40 {
        if pids.iter().all(|p| !alive(*p)) {
            return true;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    false
}

// ── spawn → locked → unlock → ready ─────────────────────────────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn spawn_waits_for_locked_unlocks_with_the_derived_key_and_waits_for_ready() {
    let e = env(&["normal"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    let k = kinds(&seen);
    let pos = |s: &str| {
        k.iter()
            .position(|x| x == s)
            .unwrap_or_else(|| panic!("{s} not in {k:?}"))
    };
    assert!(
        pos("spawned") < pos("awaiting:locked") && pos("awaiting:locked") < pos("health:locked")
    );
    assert!(
        pos("health:locked") < pos("awaiting:key")
            && pos("awaiting:key") < pos("unlock")
            && pos("unlock") < pos("awaiting:ready")
    );
    assert!(pos("awaiting:ready") < pos("ready"));
    assert_eq!(sup.status().phase, Phase::Ready);

    let unlocks = of_kind(&e.work, "unlock");
    assert_eq!(unlocks.len(), 1);
    let derived = derive_core_key(&MASTER);
    assert_eq!(unlocks[0]["key_b64"], derived.to_base64().as_str());
    assert_eq!(unlocks[0]["context"], "yandi/core/v1");
    // the master key itself never reached the core, in any encoding
    let master_b64 = base64_of(&MASTER);
    assert_ne!(unlocks[0]["key_b64"].as_str().unwrap(), master_b64);
    let everything = std::fs::read_to_string(e.work.join("records.jsonl")).unwrap();
    for form in [hex::encode(MASTER), master_b64] {
        assert!(
            !everything.contains(&form),
            "the master key appeared in what the core saw"
        );
    }
    let report = sup.shutdown().await;
    assert!(
        report.graceful && !report.terminated && !report.killed,
        "{report:?}"
    );
    assert!(all_dead(&e.work).await);
    assert!(no_core_dirs_left(&e.runtime));
}

fn base64_of(bytes: &[u8]) -> String {
    use base64::Engine;
    base64::engine::general_purpose::STANDARD.encode(bytes)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_launch_secret_is_in_a_private_file_only() {
    let e = env(&["normal"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    let start = &of_kind(&e.work, "start")[0];
    assert_eq!(
        start["secret_file_mode"], "0o600",
        "the secret file must be 0600"
    );
    assert_eq!(
        start["dir_mode"], "0o700",
        "the launch directory must be 0700"
    );
    assert_eq!(
        start["secret_file_removed"], true,
        "the core removes the file after reading it"
    );
    let secret = start["secret"].as_str().unwrap();
    assert_eq!(secret.len(), 64);
    // never on the command line, never in the environment, in any form
    let argv = start["argv"].to_string() + &start["cmdline"].to_string();
    assert!(!argv.contains(secret), "the secret is on the command line");
    let env_dump = start["env"].to_string();
    assert!(
        !env_dump.contains(secret),
        "the secret is in the environment"
    );
    assert!(start["env"]["YANDI_CORE_SECRET_FILE"]
        .as_str()
        .unwrap()
        .ends_with("launch-secret"));
    let live = std::fs::read(format!("/proc/{}/cmdline", start["pid"])).unwrap_or_default();
    assert!(!String::from_utf8_lossy(&live).contains(secret));
    sup.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_node_talks_to_the_core_with_its_secret() {
    // the stand-in refuses (401) anything but health without the bearer secret: unlock succeeding proves the client sent it
    let e = env(&["normal"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    assert_eq!(of_kind(&e.work, "unlock").len(), 1);
    sup.shutdown().await;
}

// ── crash, restart, fresh secret ─────────────────────────────────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_crash_is_survived_and_the_restart_gets_a_fresh_secret_and_is_unlocked_again() {
    let e = env(&["crash_after_ready", "normal"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    assert!(!kinds(&seen).contains(&"exited".to_owned()));
    let seen = until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    let k = kinds(&seen);
    assert!(
        k.iter().any(|x| x == "exited")
            && k.iter().any(|x| x == "launch_failed:core_exited")
            && k.iter().any(|x| x == "restart"),
        "{k:?}"
    );
    assert_eq!(
        sup.status().phase,
        Phase::Ready,
        "the supervisor (and so the node) is still up after the core died"
    );
    let starts = of_kind(&e.work, "start");
    assert_eq!(starts.len(), 2);
    assert_ne!(
        starts[0]["secret"], starts[1]["secret"],
        "a restart must not reuse the launch secret"
    );
    assert_ne!(starts[0]["pid"], starts[1]["pid"]);
    assert_eq!(
        of_kind(&e.work, "unlock").len(),
        2,
        "the restarted core is unlocked again"
    );
    sup.shutdown().await;
    assert!(all_dead(&e.work).await);
    assert!(no_core_dirs_left(&e.runtime));
}

#[test]
fn backoff_grows_by_the_factor_and_never_exceeds_its_maximum() {
    let p = BackoffPolicy {
        initial: Duration::from_millis(500),
        max: Duration::from_secs(30),
        factor: 2,
    };
    let delays: Vec<u64> = (0..9).map(|n| p.delay(n).as_millis() as u64).collect();
    assert_eq!(
        delays,
        vec![500, 1000, 2000, 4000, 8000, 16000, 30000, 30000, 30000]
    );
    assert!(p.delay(500) <= Duration::from_secs(30));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn restarts_wait_exponentially_and_never_spin() {
    let e = env(&[
        "crash_at_start",
        "crash_at_start",
        "crash_at_start",
        "normal",
    ]);
    let mut cfg = e.cfg.clone();
    cfg.restart_limit = 5;
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    let seen = until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    let delays: Vec<u64> = seen
        .iter()
        .filter_map(|(_, ev)| {
            if let Event::RestartScheduled { delay, .. } = ev {
                Some(delay.as_millis() as u64)
            } else {
                None
            }
        })
        .collect();
    assert_eq!(delays, vec![50, 100, 200]);
    // the waiting is real: each spawn is at least its delay after the previous exit
    let mut last_exit: Option<Instant> = None;
    let mut i = 0;
    for (at, ev) in &seen {
        match ev {
            Event::LaunchFailed { .. } => last_exit = Some(*at),
            Event::Spawned { .. } if last_exit.is_some() => {
                assert!(
                    at.duration_since(last_exit.unwrap()) + Duration::from_millis(15)
                        >= Duration::from_millis(delays[i]),
                    "restart {i} came too soon"
                );
                i += 1;
                last_exit = None;
            }
            _ => {}
        }
    }
    assert_eq!(i, 3);
    sup.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn after_the_limit_restarting_stops_and_the_node_stays_up() {
    let e = env(&["crash_at_start"; 10]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 30, |ev| matches!(ev, Event::Failure { .. })).await;
    let k = kinds(&seen);
    assert_eq!(k.iter().filter(|x| *x == "restart").count(), 3, "{k:?}");
    assert_eq!(
        k.iter().filter(|x| *x == "spawned").count(),
        4,
        "one launch and three restarts, no more: {k:?}"
    );
    assert!(
        k.contains(&"ceiling".to_owned()) && k.last().unwrap() == "failure:core_restart_exhausted"
    );
    let status = sup.status();
    assert_eq!(
        (status.phase, status.failure),
        (Phase::Failed, Some(FailureCategory::CoreRestartExhausted))
    );
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(
        of_kind(&e.work, "start").len(),
        4,
        "it must not go on spawning once it has given up"
    );
    assert!(sup.shutdown().await.graceful);
    assert!(no_core_dirs_left(&e.runtime));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_core_that_never_answers_is_given_up_on_and_leaves_no_process() {
    let mut e = env(&["never_answers"; 5]);
    e.cfg.startup_timeout = Duration::from_millis(700);
    e.cfg.restart_limit = 2;
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 30, |ev| matches!(ev, Event::Failure { .. })).await;
    let k = kinds(&seen);
    assert!(
        k.iter()
            .filter(|x| *x == "launch_failed:core_start_failed")
            .count()
            == 3
            && k.contains(&"ceiling".to_owned()),
        "{k:?}"
    );
    sup.shutdown().await;
    assert!(
        all_dead(&e.work).await,
        "a core that never answered was left running"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_refused_key_is_final_safe_and_leaves_no_process() {
    let e = env(&["refuse_key"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 20, |ev| matches!(ev, Event::Failure { .. })).await;
    assert_eq!(kinds(&seen).last().unwrap(), "failure:core_unlock_refused");
    if let Some((_, Event::Failure { message, .. })) = seen.last() {
        assert!(!message.contains('/') && !message.to_lowercase().contains("traceback"));
    }
    assert_eq!(
        sup.status().failure,
        Some(FailureCategory::CoreUnlockRefused)
    );
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(
        of_kind(&e.work, "start").len(),
        1,
        "a wrong key is not cured by restarting"
    );
    sup.shutdown().await;
    assert!(all_dead(&e.work).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_core_that_starts_unlocked_is_not_accepted() {
    let mut e = env(&["starts_ready"]);
    e.cfg.restart_limit = 0;
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 20, |ev| matches!(ev, Event::Failure { .. })).await;
    let k = kinds(&seen);
    assert!(
        k.contains(&"launch_failed:core_start_failed".to_owned())
            && !k.contains(&"unlock".to_owned()),
        "{k:?}"
    );
    sup.shutdown().await;
    assert!(all_dead(&e.work).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn without_a_master_key_the_core_stays_locked_and_nothing_is_invented() {
    let e = env(&["normal"]);
    let have = Arc::new(AtomicBool::new(false));
    let h = have.clone();
    let provider: KeyProvider = Arc::new(move || {
        if h.load(Ordering::SeqCst) {
            Some(Zeroizing::new(MASTER))
        } else {
            None
        }
    });
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), provider).unwrap();
    until(&mut rx, 20, |ev| {
        matches!(ev, Event::AwaitingState { state: "key" })
    })
    .await;
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert_eq!(sup.status().phase, Phase::WaitingForKey);
    assert!(
        of_kind(&e.work, "unlock").is_empty(),
        "the core was unlocked without a key"
    );
    have.store(true, Ordering::SeqCst);
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    assert_eq!(of_kind(&e.work, "unlock").len(), 1);
    sup.shutdown().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn first_run_provisions_the_check_value_before_the_unlock_and_only_once() {
    let e = env(&["crash_after_ready", "normal"]);
    assert!(!e.state.join("check-value.json").exists());
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    let all = records(&e.work);
    let order: Vec<&str> = all.iter().map(|r| r["event"].as_str().unwrap()).collect();
    let first_provision = order
        .iter()
        .position(|x| *x == "provision")
        .expect("never provisioned");
    let first_unlock = order.iter().position(|x| *x == "unlock").unwrap();
    assert!(first_provision < first_unlock);
    assert_eq!(
        order.iter().filter(|x| **x == "provision").count(),
        1,
        "already provisioned: the restart must not do it again"
    );
    let provisioned = all[first_provision]["key_sha256"].as_str().unwrap();
    assert_eq!(
        provisioned,
        sha_hex(derive_core_key(&MASTER).to_base64().as_str())
    );
    assert!(all[first_provision]["argv"]
        .to_string()
        .find(&hex::encode(MASTER))
        .is_none());
    sup.shutdown().await;
}

// ── ownership ────────────────────────────────────────────────────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_port_the_child_does_not_own_is_never_contacted() {
    let rogue = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    rogue.set_nonblocking(true).unwrap();
    let rogue_port = rogue.local_addr().unwrap().port();
    let mut e = env_with(
        &["rogue_port"],
        serde_json::json!({"rogue_port": rogue_port}),
    );
    e.cfg.startup_timeout = Duration::from_millis(900);
    e.cfg.restart_limit = 0;
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Failure { .. })).await;
    match rogue.accept() {
        Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {}
        other => panic!("the node connected to a core it did not spawn: {other:?}"),
    }
    assert!(of_kind(&e.work, "unlock").is_empty());
    sup.shutdown().await;
    assert!(all_dead(&e.work).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_core_that_listens_beyond_loopback_is_refused() {
    let mut e = env(&["bind_all"]);
    e.cfg.startup_timeout = Duration::from_millis(900);
    e.cfg.restart_limit = 0;
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    let seen = until(&mut rx, 20, |ev| matches!(ev, Event::Failure { .. })).await;
    assert!(!kinds(&seen).contains(&"unlock".to_owned()));
    sup.shutdown().await;
    assert!(all_dead(&e.work).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn only_the_owned_child_is_ever_signalled() {
    let mut bystander = std::process::Command::new("sleep")
        .arg("60")
        .spawn()
        .unwrap();
    let e = env(&["crash_after_ready", "ignore_shutdown_and_term"]);
    let mut cfg = e.cfg.clone();
    cfg.shutdown_deadline = Duration::from_millis(300);
    cfg.term_grace = Duration::from_millis(400);
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    until(&mut rx, 30, |ev| matches!(ev, Event::Ready)).await;
    let report = sup.shutdown().await;
    assert!(report.killed, "{report:?}");
    assert!(
        alive(bystander.id() as u64),
        "a process the supervisor did not spawn was killed"
    );
    bystander.kill().unwrap();
    let _ = bystander.wait();
}

// ── shutdown ─────────────────────────────────────────────────────────────────────────────────────────────────────

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_escalates_from_the_request_to_term_and_leaves_no_orphan() {
    let e = env(&["hang_on_shutdown"]);
    let mut cfg = e.cfg.clone();
    cfg.shutdown_deadline = Duration::from_millis(300);
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    let report = sup.shutdown().await;
    assert!(
        !report.graceful && report.terminated && !report.killed,
        "{report:?}"
    );
    assert!(all_dead(&e.work).await);
    assert!(no_core_dirs_left(&e.runtime));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn kill_is_the_last_resort_and_still_leaves_no_orphan() {
    let e = env(&["ignore_shutdown_and_term"]);
    let mut cfg = e.cfg.clone();
    cfg.shutdown_deadline = Duration::from_millis(300);
    cfg.term_grace = Duration::from_millis(400);
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    let report = sup.shutdown().await;
    assert!(report.terminated && report.killed, "{report:?}");
    assert!(all_dead(&e.work).await);
    assert!(no_core_dirs_left(&e.runtime));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_while_waiting_between_restarts_or_for_a_key_is_clean() {
    let e = env(&["crash_at_start"; 3]);
    let mut cfg = e.cfg.clone();
    cfg.backoff = BackoffPolicy {
        initial: Duration::from_secs(30),
        max: Duration::from_secs(30),
        factor: 2,
    };
    let (sup, mut rx) = CoreSupervisor::start(cfg, key()).unwrap();
    until(&mut rx, 20, |ev| {
        matches!(ev, Event::RestartScheduled { .. })
    })
    .await;
    let began = Instant::now();
    sup.shutdown().await;
    assert!(
        began.elapsed() < Duration::from_secs(3),
        "shutdown had to wait out the backoff"
    );
    assert!(no_core_dirs_left(&e.runtime));
    let e = env(&["normal"]);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), Arc::new(|| None)).unwrap();
    until(&mut rx, 20, |ev| {
        matches!(ev, Event::AwaitingState { state: "key" })
    })
    .await;
    let report = sup.shutdown().await;
    assert!(report.terminated || report.graceful, "{report:?}");
    assert!(all_dead(&e.work).await);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_core_does_not_outlive_a_node_that_is_killed() {
    let e = env(&["normal"]);
    let exe = std::env::current_exe().unwrap();
    let probe = exe
        .parent()
        .unwrap()
        .parent()
        .unwrap()
        .join("examples")
        .join("owner_probe");
    assert!(probe.exists(), "build the example first: {probe:?}");
    let mut child = std::process::Command::new(&probe)
        .env("PROBE_PYTHON", python())
        .env(
            "PROBE_FAKE",
            concat!(env!("CARGO_MANIFEST_DIR"), "/tests/support/fake_core.py"),
        )
        .env(
            "PROBE_SCRIPT",
            e.cfg
                .extra_env
                .iter()
                .find(|(k, _)| k == "FAKE_CORE_SCRIPT")
                .unwrap()
                .1
                .clone(),
        )
        .env("PROBE_STATE", &e.state)
        .env("PROBE_RUNTIME", &e.runtime)
        .stdout(std::process::Stdio::piped())
        .spawn()
        .unwrap();
    let mut line = String::new();
    std::io::BufRead::read_line(
        &mut std::io::BufReader::new(child.stdout.take().unwrap()),
        &mut line,
    )
    .unwrap();
    let core_pid: u64 = line
        .trim()
        .strip_prefix("core_pid=")
        .expect(&line)
        .parse()
        .unwrap();
    assert!(alive(core_pid));
    unsafe { libc::kill(child.id() as i32, libc::SIGKILL) };
    let _ = child.wait();
    for _ in 0..60 {
        if !alive(core_pid) {
            return;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    unsafe { libc::kill(core_pid as i32, libc::SIGKILL) };
    panic!("the core outlived its node");
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn starting_removes_only_a_dead_nodes_leftovers() {
    use std::os::unix::fs::DirBuilderExt;
    let e = env(&["normal"]);
    let base = RuntimeBase::open(&e.runtime).unwrap();
    let stale = base.path().join("core-4000000-abc");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&stale)
        .unwrap();
    let foreign = base.path().join("not-ours-4000000");
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&foreign)
        .unwrap();
    drop(base);
    let (sup, mut rx) = CoreSupervisor::start(e.cfg.clone(), key()).unwrap();
    until(&mut rx, 20, |ev| matches!(ev, Event::Ready)).await;
    assert!(
        !stale.exists(),
        "a dead node's launch directory was left behind"
    );
    assert!(
        foreign.exists(),
        "something that is not a launch directory was removed"
    );
    sup.shutdown().await;
}
