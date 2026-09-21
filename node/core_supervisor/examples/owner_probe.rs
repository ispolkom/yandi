//! A tiny "node" for the test that kills its owner: starts the supervisor against the stand-in core, prints the core's pid,
//! and sleeps. The test then SIGKILLs this process and checks that the core goes away with it.
use core_supervisor::*;
use std::sync::Arc;
use zeroize::Zeroizing;

#[cfg(unix)]
#[tokio::main]
async fn main() {
    let get = |k: &str| std::env::var_os(k).expect(k);
    let mut cfg = SupervisorConfig::new(
        get("PROBE_PYTHON"),
        std::env::temp_dir(),
        get("PROBE_STATE"),
        get("PROBE_RUNTIME"),
    );
    cfg.serve_args = vec![get("PROBE_FAKE")];
    cfg.provision_args = vec![get("PROBE_FAKE"), "provision".into()];
    cfg.inherit_env = false;
    cfg.extra_env = vec![
        ("PATH".into(), get("PATH")),
        ("FAKE_CORE_SCRIPT".into(), get("PROBE_SCRIPT")),
    ];
    let (_sup, mut rx) =
        CoreSupervisor::start(cfg, Arc::new(|| Some(Zeroizing::new([9u8; 32])))).unwrap();
    let mut pid = 0;
    loop {
        match rx.recv().await {
            Ok(Event::Spawned { pid: p, .. }) => pid = p,
            Ok(Event::Ready) => break,
            _ => {}
        }
    }
    println!("core_pid={pid}");
    loop {
        tokio::time::sleep(std::time::Duration::from_secs(3600)).await;
    }
}

#[cfg(not(unix))]
fn main() {
    eprintln!("the core supervisor is Unix-only for now");
}
