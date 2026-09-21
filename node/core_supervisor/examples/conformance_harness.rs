//! The Rust side of the supervisor conformance protocol (contract/README.md, "Supervisor scenarios").
//!
//! stdin : one JSON object  {"config": {"restart_limit", "window_s", "backoff_min_ms", "backoff_max_ms"},
//!                           "launches": ["normal", "crash_at_start", ...], "startup_timeout_ms": 4000}
//!         `launches` is what the stand-in core does on launch 1, 2, …; every later launch behaves normally.
//! stdout: one JSON object per line, in order: what the supervisor did, and `transport_tick` from a stand-in for the node's
//!         own transport, which must keep running whatever the core does. The run ends by itself (with `harness_done`).
//! It never touches anything but a temporary directory and its own children.
use core_supervisor::*;
use serde::Deserialize;
use std::sync::Arc;
use std::time::{Duration, Instant};
use zeroize::Zeroizing;

#[derive(Deserialize, Default)]
struct Cfg {
    restart_limit: Option<u32>,
    window_s: Option<u64>,
    backoff_min_ms: Option<u64>,
    backoff_max_ms: Option<u64>,
}

#[derive(Deserialize)]
struct Input {
    #[serde(default)]
    config: Cfg,
    launches: Vec<String>,
    startup_timeout_ms: Option<u64>,
}

fn say(t0: Instant, event: &str, extra: serde_json::Value) {
    let mut v = serde_json::json!({"t_ms": t0.elapsed().as_millis() as u64, "event": event});
    if let Some(o) = extra.as_object() {
        for (k, x) in o {
            v[k] = x.clone();
        }
    }
    println!("{v}");
}

#[cfg(unix)]
#[tokio::main]
async fn main() {
    let input: Input = serde_json::from_reader(std::io::stdin()).expect("a JSON scenario on stdin");
    let tmp = tempfile::tempdir().unwrap();
    let work = tmp.path().join("work");
    std::fs::create_dir_all(&work).unwrap();
    let script = tmp.path().join("script.json");
    std::fs::write(
        &script,
        serde_json::json!({"work_dir": work, "launches": input.launches, "default": "normal"})
            .to_string(),
    )
    .unwrap();
    let fake = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/support/fake_core.py");
    let python = std::env::var_os("YANDI_TEST_PYTHON").unwrap_or_else(|| "python3".into());

    let mut cfg = SupervisorConfig::new(
        python,
        tmp.path(),
        tmp.path().join("state"),
        tmp.path().join("run"),
    );
    cfg.serve_args = vec![fake.into()];
    cfg.provision_args = vec![fake.into(), "provision".into()];
    cfg.inherit_env = false;
    cfg.extra_env = vec![
        (
            "PATH".into(),
            std::env::var_os("PATH").unwrap_or_else(|| "/usr/bin:/bin".into()),
        ),
        ("FAKE_CORE_SCRIPT".into(), script.into()),
    ];
    cfg.log_path = Some(tmp.path().join("core.log"));
    cfg.poll_interval = Duration::from_millis(20);
    cfg.startup_timeout = Duration::from_millis(input.startup_timeout_ms.unwrap_or(4000));
    cfg.ready_timeout = Duration::from_secs(8);
    cfg.shutdown_deadline = Duration::from_secs(3);
    cfg.term_grace = Duration::from_secs(1);
    cfg.restart_limit = input.config.restart_limit.unwrap_or(5);
    cfg.failure_window = Duration::from_secs(input.config.window_s.unwrap_or(60));
    cfg.backoff = BackoffPolicy {
        initial: Duration::from_millis(input.config.backoff_min_ms.unwrap_or(50)),
        max: Duration::from_millis(input.config.backoff_max_ms.unwrap_or(800)),
        factor: 2,
    };

    let t0 = Instant::now();
    // the node's own transport: it must go on ticking through every crash of the core
    tokio::spawn(async move {
        loop {
            say(t0, "transport_tick", serde_json::json!({}));
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    });

    let (sup, mut rx) = CoreSupervisor::start(cfg, Arc::new(|| Some(Zeroizing::new([9u8; 32]))))
        .expect("runtime directory");
    let scripted = input.launches.len() as u32;
    let mut spawned = 0u32;
    let mut done_at: Option<Instant> = None;
    let deadline = Instant::now() + Duration::from_secs(60);
    while Instant::now() < deadline {
        if let Some(at) = done_at {
            if Instant::now() >= at {
                break;
            }
        }
        let Ok(Ok(event)) = tokio::time::timeout(Duration::from_millis(50), rx.recv()).await else {
            continue;
        };
        match event {
            Event::Spawned { launch, pid } => {
                spawned = launch;
                say(
                    t0,
                    "core_spawned",
                    serde_json::json!({"launch": launch, "pid": pid}),
                );
            }
            Event::AwaitingState { state } => {
                say(t0, "awaiting_state", serde_json::json!({"state": state}))
            }
            Event::HealthState { state } => {
                say(t0, "health_state", serde_json::json!({"state": state}))
            }
            Event::UnlockSent => say(t0, "unlock_sent", serde_json::json!({})),
            Event::Ready => {
                say(t0, "ready", serde_json::json!({"launch": spawned}));
                if spawned >= scripted {
                    done_at = Some(Instant::now() + Duration::from_millis(400));
                }
            }
            Event::CoreExited { launch, code } => say(
                t0,
                "core_exited",
                serde_json::json!({"launch": launch, "code": code}),
            ),
            Event::LaunchFailed { launch, category } => say(
                t0,
                "launch_failed",
                serde_json::json!({"launch": launch, "category": category.as_str()}),
            ),
            Event::RestartScheduled { attempt, delay } => say(
                t0,
                "restart_scheduled",
                serde_json::json!({"attempt": attempt, "delay_ms": delay.as_millis() as u64}),
            ),
            Event::RestartCeilingReached => {
                say(t0, "restart_ceiling_reached", serde_json::json!({}))
            }
            Event::Failure { category, message } => {
                say(
                    t0,
                    "failure_reported",
                    serde_json::json!({"category": category.as_str(), "message": message}),
                );
                done_at = Some(Instant::now() + Duration::from_millis(400));
            }
            Event::ShutdownRequested | Event::Stopped { .. } => {}
        }
    }
    let report = sup.shutdown().await;
    say(
        t0,
        "shutdown_done",
        serde_json::json!({"graceful": report.graceful, "terminated": report.terminated, "killed": report.killed}),
    );
    say(t0, "harness_done", serde_json::json!({}));
}

#[cfg(not(unix))]
fn main() {
    eprintln!("the core supervisor is Unix-only for now");
}
