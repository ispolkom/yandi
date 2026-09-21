//! The supervisor: one task that owns one Core child at a time and walks it through
//! spawn → locked → (key) → unlock → ready → watched → crash/exit → backoff → spawn …, or to a stop.
use crate::client::{ClientError, CoreClient, HealthState};
use crate::config::SupervisorConfig;
use crate::keys::{derive_core_key, CoreKey};
use crate::runtime::{LaunchDir, LaunchSecret, RuntimeBase, RuntimeError};
use std::collections::VecDeque;
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::AsyncWriteExt;
use tokio::process::{Child, Command};
use tokio::sync::{broadcast, watch};
use tokio::task::JoinHandle;
use zeroize::Zeroizing;

/// Where the master key comes from. `None` until the node has it (first run and rebind happen in the web UI); the supervisor
/// keeps the Core locked and waits. There is no default key: production never unlocks by itself with a made-up one.
pub type KeyProvider = Arc<dyn Fn() -> Option<Zeroizing<[u8; 32]>> + Send + Sync>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureCategory {
    CoreStartFailed,
    CoreExited,
    CoreRestartExhausted,
    CoreUnlockRefused,
}

impl FailureCategory {
    pub fn as_str(&self) -> &'static str {
        match self {
            FailureCategory::CoreStartFailed => "core_start_failed",
            FailureCategory::CoreExited => "core_exited",
            FailureCategory::CoreRestartExhausted => "core_restart_exhausted",
            FailureCategory::CoreUnlockRefused => "core_unlock_refused",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Phase {
    Starting,
    WaitingForKey,
    Unlocking,
    Ready,
    Backoff,
    Failed,
    Stopping,
    Stopped,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct CoreStatus {
    pub phase: Phase,
    pub failure: Option<FailureCategory>,
    pub launches: u32,
    pub restarts: u32,
}

/// What the supervisor tells the outside. Nothing here can carry a secret, a key, a path or anything the Core printed.
#[derive(Clone, Debug)]
pub enum Event {
    Spawned {
        launch: u32,
        pid: u32,
    },
    AwaitingState {
        state: &'static str,
    },
    HealthState {
        state: String,
    },
    UnlockSent,
    Ready,
    CoreExited {
        launch: u32,
        code: Option<i32>,
    },
    LaunchFailed {
        launch: u32,
        category: FailureCategory,
    },
    RestartScheduled {
        attempt: u32,
        delay: Duration,
    },
    RestartCeilingReached,
    Failure {
        category: FailureCategory,
        message: &'static str,
    },
    ShutdownRequested,
    Stopped {
        graceful: bool,
        terminated: bool,
        killed: bool,
    },
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ShutdownReport {
    /// The Core stopped itself after `POST /v1/shutdown`.
    pub graceful: bool,
    pub terminated: bool,
    pub killed: bool,
}

pub struct CoreSupervisor {
    status: watch::Receiver<CoreStatus>,
    events: broadcast::Sender<Event>,
    stop: watch::Sender<bool>,
    task: Option<JoinHandle<ShutdownReport>>,
}

impl CoreSupervisor {
    /// Prepare the runtime directory (removing only what a dead node provably left), then start supervising in the background.
    /// The returned receiver sees every event from the first one.
    pub fn start(
        cfg: SupervisorConfig,
        key: KeyProvider,
    ) -> Result<(CoreSupervisor, broadcast::Receiver<Event>), RuntimeError> {
        let base = RuntimeBase::open(&cfg.runtime_base)?;
        base.clean_stale();
        let (events, rx) = broadcast::channel(256);
        let (status_tx, status_rx) = watch::channel(CoreStatus {
            phase: Phase::Starting,
            failure: None,
            launches: 0,
            restarts: 0,
        });
        let (stop_tx, stop_rx) = watch::channel(false);
        let runner = Runner {
            cfg,
            key,
            base,
            status: status_tx,
            events: events.clone(),
            stop: stop_rx,
            restarts: VecDeque::new(),
            launches: 0,
        };
        let task = tokio::spawn(runner.run());
        Ok((
            CoreSupervisor {
                status: status_rx,
                events,
                stop: stop_tx,
                task: Some(task),
            },
            rx,
        ))
    }

    pub fn status(&self) -> CoreStatus {
        self.status.borrow().clone()
    }

    pub fn watch_status(&self) -> watch::Receiver<CoreStatus> {
        self.status.clone()
    }

    pub fn subscribe(&self) -> broadcast::Receiver<Event> {
        self.events.subscribe()
    }

    /// Stop the Core the way the node stops: ask it to shut down, wait, TERM, and only as a last resort KILL — only the child
    /// this supervisor spawned. The runtime artifacts are removed.
    pub async fn shutdown(mut self) -> ShutdownReport {
        self.stop.send_replace(true);
        match self.task.take() {
            Some(task) => task.await.unwrap_or_default(),
            None => ShutdownReport::default(),
        }
    }
}

impl Drop for CoreSupervisor {
    fn drop(&mut self) {
        self.stop.send_replace(true);
    }
}

// ── the runner ───────────────────────────────────────────────────────────────────────────────────────────────────

enum Tick {
    Stop,
    Exited(Option<i32>),
    Continue,
}

enum SupEnd {
    Exited { code: Option<i32>, was_ready: bool },
    StartFailed(&'static str),
    UnlockRefused,
    Stopped(ShutdownReport),
}

enum LaunchEnd {
    Failed(FailureCategory),
    Stopped(ShutdownReport),
}

struct Runner {
    cfg: SupervisorConfig,
    key: KeyProvider,
    base: RuntimeBase,
    status: watch::Sender<CoreStatus>,
    events: broadcast::Sender<Event>,
    stop: watch::Receiver<bool>,
    restarts: VecDeque<Instant>,
    launches: u32,
}

impl Runner {
    fn emit(&self, event: Event) {
        match &event {
            Event::Ready => eprintln!("[core-supervisor] core is ready"),
            Event::CoreExited { .. } => eprintln!("[core-supervisor] the core exited"),
            Event::RestartScheduled { attempt, delay } => eprintln!(
                "[core-supervisor] restart {attempt} in {} ms",
                delay.as_millis()
            ),
            Event::Failure { category, .. } => {
                eprintln!("[core-supervisor] failure: {}", category.as_str())
            }
            _ => {}
        }
        let _ = self.events.send(event);
    }

    fn set(&self, phase: Phase, failure: Option<FailureCategory>) {
        self.status.send_replace(CoreStatus {
            phase,
            failure,
            launches: self.launches,
            restarts: self.restarts.len() as u32,
        });
    }

    fn stopping(&self) -> bool {
        *self.stop.borrow()
    }

    async fn run(mut self) -> ShutdownReport {
        loop {
            if self.stopping() {
                return self.finish(ShutdownReport {
                    graceful: true,
                    ..Default::default()
                });
            }
            let category = match self.one_launch().await {
                LaunchEnd::Stopped(report) => return self.finish(report),
                LaunchEnd::Failed(category) => category,
            };
            self.emit(Event::LaunchFailed {
                launch: self.launches,
                category,
            });
            if category == FailureCategory::CoreUnlockRefused {
                // The key is wrong for this Core's check value; another process would be refused the same way.
                self.emit(Event::Failure {
                    category,
                    message: "The core did not accept the key it was given.",
                });
                self.set(Phase::Failed, Some(category));
                return self.wait_for_stop().await;
            }
            let now = Instant::now();
            while self
                .restarts
                .front()
                .map_or(false, |t| now.duration_since(*t) > self.cfg.failure_window)
            {
                self.restarts.pop_front();
            }
            if self.restarts.len() as u32 >= self.cfg.restart_limit {
                self.emit(Event::RestartCeilingReached);
                self.emit(Event::Failure {
                    category: FailureCategory::CoreRestartExhausted,
                    message: "The core failed repeatedly; automatic restarts have stopped.",
                });
                self.set(Phase::Failed, Some(FailureCategory::CoreRestartExhausted));
                return self.wait_for_stop().await;
            }
            let delay = self.cfg.backoff.delay(self.restarts.len() as u32);
            self.restarts.push_back(now);
            self.emit(Event::RestartScheduled {
                attempt: self.restarts.len() as u32,
                delay,
            });
            self.set(Phase::Backoff, None);
            let mut stop = self.stop.clone();
            tokio::select! {
                _ = tokio::time::sleep(delay) => {}
                _ = stop.wait_for(|v| *v) => return self.finish(ShutdownReport { graceful: true, ..Default::default() }),
            }
        }
    }

    fn finish(&self, report: ShutdownReport) -> ShutdownReport {
        self.emit(Event::Stopped {
            graceful: report.graceful,
            terminated: report.terminated,
            killed: report.killed,
        });
        self.set(Phase::Stopped, None);
        report
    }

    async fn wait_for_stop(&mut self) -> ShutdownReport {
        let mut stop = self.stop.clone();
        let _ = stop.wait_for(|v| *v).await;
        self.finish(ShutdownReport {
            graceful: true,
            ..Default::default()
        })
    }

    async fn tick(&self, child: &mut Child) -> Tick {
        let mut stop = self.stop.clone();
        tokio::select! {
            biased;
            _ = stop.wait_for(|v| *v) => Tick::Stop,
            r = child.wait() => Tick::Exited(r.ok().and_then(|s| s.code())),
            _ = tokio::time::sleep(self.cfg.poll_interval) => Tick::Continue,
        }
    }

    async fn one_launch(&mut self) -> LaunchEnd {
        self.launches += 1;
        let launch = self.launches;
        self.set(Phase::Starting, None);
        let (dir, secret) = match self.base.new_launch() {
            Ok(x) => x,
            Err(_) => return LaunchEnd::Failed(FailureCategory::CoreStartFailed),
        };
        let mut child = match self.spawn(&dir) {
            Ok(c) => c,
            Err(_) => {
                dir.cleanup();
                return LaunchEnd::Failed(FailureCategory::CoreStartFailed);
            }
        };
        let pid = child.id().unwrap_or(0);
        self.emit(Event::Spawned { launch, pid });
        let end = self.supervise(&mut child, &dir, &secret, pid).await;
        let result = match end {
            SupEnd::Exited { code, was_ready } => {
                self.emit(Event::CoreExited { launch, code });
                LaunchEnd::Failed(if was_ready {
                    FailureCategory::CoreExited
                } else {
                    FailureCategory::CoreStartFailed
                })
            }
            SupEnd::StartFailed(why) => {
                eprintln!("[core-supervisor] the core did not start: {why}");
                self.terminate(&mut child, pid).await;
                LaunchEnd::Failed(FailureCategory::CoreStartFailed)
            }
            SupEnd::UnlockRefused => {
                self.terminate(&mut child, pid).await;
                LaunchEnd::Failed(FailureCategory::CoreUnlockRefused)
            }
            SupEnd::Stopped(report) => LaunchEnd::Stopped(report),
        };
        dir.cleanup();
        result
    }

    fn spawn(&self, dir: &LaunchDir) -> std::io::Result<Child> {
        let mut cmd = Command::new(&self.cfg.program);
        cmd.args(&self.cfg.serve_args)
            .current_dir(&self.cfg.working_dir);
        self.apply_env(&mut cmd);
        // The only things that tell the Core about this launch: the path of the secret file, of the port file, of its state.
        cmd.env("YANDI_CORE_SECRET_FILE", dir.secret_path())
            .env("YANDI_CORE_PORT_FILE", dir.port_path());
        cmd.stdin(Stdio::null());
        if let Some(log) = &self.cfg.log_path {
            use std::os::unix::fs::OpenOptionsExt;
            let file = std::fs::OpenOptions::new()
                .create(true)
                .append(true)
                .mode(0o600)
                .open(log)?;
            cmd.stderr(Stdio::from(file.try_clone()?))
                .stdout(Stdio::from(file));
        }
        cmd.kill_on_drop(true);
        cmd.process_group(0); // its own group: signals go to what the node owns, and Ctrl+C in a terminal does not hit it
        #[cfg(target_os = "linux")]
        unsafe {
            let parent = std::process::id() as i32;
            cmd.pre_exec(move || {
                // If the node dies (even by SIGKILL) the Core is told to stop; no orphan outlives its owner.
                libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
                if libc::getppid() != parent {
                    libc::_exit(1);
                }
                Ok(())
            });
        }
        cmd.spawn()
    }

    fn apply_env(&self, cmd: &mut Command) {
        if !self.cfg.inherit_env {
            cmd.env_clear();
        }
        for (k, v) in &self.cfg.extra_env {
            cmd.env(k, v);
        }
        cmd.env("YANDI_CORE_STATE_DIR", &self.cfg.state_dir);
    }

    async fn supervise(
        &mut self,
        child: &mut Child,
        dir: &LaunchDir,
        secret: &LaunchSecret,
        pid: u32,
    ) -> SupEnd {
        // 1. the port: from the file the child wrote, and only if the child itself listens there, on loopback, and nowhere else
        self.emit(Event::AwaitingState { state: "locked" });
        let deadline = Instant::now() + self.cfg.startup_timeout;
        let port = loop {
            match self.tick(child).await {
                Tick::Stop => return self.stop_now(child, pid, None).await,
                Tick::Exited(code) => {
                    return SupEnd::Exited {
                        code,
                        was_ready: false,
                    }
                }
                Tick::Continue => {}
            }
            if let Some(p) = dir.read_port() {
                if owns_loopback_listener(pid, p) {
                    break p;
                }
            }
            if Instant::now() > deadline {
                return SupEnd::StartFailed("no port of its own");
            }
        };
        let client = CoreClient::new(port, secret);

        // 2. health must say `locked` — a core that starts ready has unlocked itself, which is not a core the node started
        let mut last: Option<HealthState> = None;
        loop {
            match self.tick(child).await {
                Tick::Stop => return self.stop_now(child, pid, Some(&client)).await,
                Tick::Exited(code) => {
                    return SupEnd::Exited {
                        code,
                        was_ready: false,
                    }
                }
                Tick::Continue => {}
            }
            if let Ok(state) = client.health().await {
                if last.as_ref() != Some(&state) {
                    self.emit(Event::HealthState {
                        state: state.as_str().to_owned(),
                    });
                    last = Some(state.clone());
                }
                match state {
                    HealthState::Locked => break,
                    HealthState::Starting => {}
                    _ => return SupEnd::StartFailed("did not start locked"),
                }
            }
            if Instant::now() > deadline {
                return SupEnd::StartFailed("never locked");
            }
        }

        // 3. the master key: wait for it, watching the child
        self.set(Phase::WaitingForKey, None);
        self.emit(Event::AwaitingState { state: "key" });
        let master = loop {
            if let Some(m) = (self.key)() {
                break m;
            }
            match self.tick(child).await {
                Tick::Stop => return self.stop_now(child, pid, Some(&client)).await,
                Tick::Exited(code) => {
                    return SupEnd::Exited {
                        code,
                        was_ready: false,
                    }
                }
                Tick::Continue => {}
            }
        };
        let derived = derive_core_key(&master);
        drop(master);

        // 4. first run: the Core has no check value for this key yet; it is created out of band, never over the API
        if !self.cfg.state_dir.join(&self.cfg.check_value_file).exists()
            && !self.provision(&derived).await
        {
            return SupEnd::StartFailed("provisioning failed");
        }

        // 5. unlock, then wait for ready
        self.set(Phase::Unlocking, None);
        self.emit(Event::UnlockSent);
        match client.unlock(&derived).await {
            Ok(()) => {}
            Err(ClientError::Refused { status: 403, .. }) => return SupEnd::UnlockRefused,
            Err(_) => return SupEnd::StartFailed("unlock failed"),
        }
        drop(derived);
        self.emit(Event::AwaitingState { state: "ready" });
        let ready_deadline = Instant::now() + self.cfg.ready_timeout;
        loop {
            match self.tick(child).await {
                Tick::Stop => return self.stop_now(child, pid, Some(&client)).await,
                Tick::Exited(code) => {
                    return SupEnd::Exited {
                        code,
                        was_ready: false,
                    }
                }
                Tick::Continue => {}
            }
            if let Ok(HealthState::Ready) = client.health().await {
                break;
            }
            if Instant::now() > ready_deadline {
                return SupEnd::StartFailed("never ready");
            }
        }

        // 6. watched: only the child's exit matters here
        self.set(Phase::Ready, None);
        self.emit(Event::Ready);
        loop {
            match self.tick(child).await {
                Tick::Stop => return self.stop_now(child, pid, Some(&client)).await,
                Tick::Exited(code) => {
                    return SupEnd::Exited {
                        code,
                        was_ready: true,
                    }
                }
                Tick::Continue => {}
            }
        }
    }

    async fn provision(&self, key: &CoreKey) -> bool {
        let mut cmd = Command::new(&self.cfg.program);
        cmd.args(&self.cfg.provision_args)
            .current_dir(&self.cfg.working_dir);
        self.apply_env(&mut cmd);
        cmd.stdin(Stdio::piped())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .kill_on_drop(true);
        let Ok(mut child) = cmd.spawn() else {
            return false;
        };
        if let Some(mut stdin) = child.stdin.take() {
            let encoded = key.to_base64();
            if stdin.write_all(encoded.as_bytes()).await.is_err()
                || stdin.write_all(b"\n").await.is_err()
            {
                return false;
            }
        }
        matches!(tokio::time::timeout(Duration::from_secs(30), child.wait()).await, Ok(Ok(status)) if status.success())
    }

    /// The node is stopping: ask, wait, TERM, KILL (only the owned child), report.
    async fn stop_now(
        &mut self,
        child: &mut Child,
        pid: u32,
        client: Option<&CoreClient>,
    ) -> SupEnd {
        self.set(Phase::Stopping, None);
        self.emit(Event::ShutdownRequested);
        let mut report = ShutdownReport::default();
        let mut waited_for_core = false;
        if let Some(c) = client {
            if c.shutdown(self.cfg.shutdown_deadline).await.is_ok() {
                waited_for_core = true;
            }
        }
        if waited_for_core {
            let patience = self.cfg.shutdown_deadline + Duration::from_secs(1);
            if let Ok(Ok(_)) = tokio::time::timeout(patience, child.wait()).await {
                report.graceful = true;
                return SupEnd::Stopped(report);
            }
        }
        let (terminated, killed) = self.terminate(child, pid).await;
        report.terminated = terminated;
        report.killed = killed;
        SupEnd::Stopped(report)
    }

    /// TERM the child's own process group, wait, and KILL it only if it ignored the TERM. Returns (terminated, killed).
    async fn terminate(&self, child: &mut Child, pid: u32) -> (bool, bool) {
        if let Ok(Some(_)) = child.try_wait() {
            return (false, false);
        }
        signal_group(pid, libc::SIGTERM);
        if let Ok(Ok(_)) = tokio::time::timeout(self.cfg.term_grace, child.wait()).await {
            return (true, false);
        }
        signal_group(pid, libc::SIGKILL);
        let _ = child.kill().await;
        let _ = tokio::time::timeout(Duration::from_secs(5), child.wait()).await;
        (true, true)
    }
}

/// A signal to the process group this node created for the child (`process_group(0)` makes the group id equal to the pid).
fn signal_group(pid: u32, sig: i32) {
    if pid > 1 {
        unsafe {
            libc::kill(-(pid as i32), sig);
        }
    }
}

/// Is `port` a loopback TCP listener of process `pid`, and does `pid` listen on loopback only?
/// The node never sends a request to a port it has not proven belongs to the child it spawned (an unknown or stale core, or a
/// port file pointing somewhere else, is refused). Linux reads /proc; elsewhere the port file in the node-created private
/// directory is all there is (documented limit).
#[cfg(target_os = "linux")]
pub(crate) fn owns_loopback_listener(pid: u32, port: u16) -> bool {
    use std::collections::HashSet;
    let Ok(fds) = std::fs::read_dir(format!("/proc/{pid}/fd")) else {
        return false;
    };
    let mut inodes: HashSet<String> = HashSet::new();
    for fd in fds.flatten() {
        if let Ok(target) = std::fs::read_link(fd.path()) {
            if let Some(rest) = target.to_string_lossy().strip_prefix("socket:[") {
                inodes.insert(rest.trim_end_matches(']').to_owned());
            }
        }
    }
    let mut ours = false;
    let mut elsewhere = false;
    for (table, loopbacks) in [
        ("tcp", &["0100007F"][..]),
        (
            "tcp6",
            &[
                "00000000000000000000000001000000",
                "0000000000000000FFFF00000100007F",
            ][..],
        ),
    ] {
        let Ok(text) = std::fs::read_to_string(format!("/proc/{pid}/net/{table}")) else {
            continue;
        };
        for line in text.lines().skip(1) {
            let f: Vec<&str> = line.split_whitespace().collect();
            if f.len() < 10 || f[3] != "0A" || !inodes.contains(f[9]) {
                continue;
            }
            let Some((addr, p)) = f[1].split_once(':') else {
                continue;
            };
            if loopbacks.contains(&addr) {
                if u16::from_str_radix(p, 16).ok() == Some(port) {
                    ours = true;
                }
            } else {
                elsewhere = true;
            }
        }
    }
    ours && !elsewhere
}

#[cfg(not(target_os = "linux"))]
pub(crate) fn owns_loopback_listener(_pid: u32, _port: u16) -> bool {
    true
}
