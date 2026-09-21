use std::ffi::OsString;
use std::path::PathBuf;
use std::time::Duration;

/// Bounded exponential backoff: `initial · factor^n`, never above `max`.
#[derive(Clone, Debug)]
pub struct BackoffPolicy {
    pub initial: Duration,
    pub max: Duration,
    pub factor: u32,
}

impl BackoffPolicy {
    /// Delay before restart number `already_restarted + 1` inside the failure window.
    pub fn delay(&self, already_restarted: u32) -> Duration {
        let mut d = self.initial;
        for _ in 0..already_restarted {
            d = d.saturating_mul(self.factor.max(1));
            if d >= self.max {
                return self.max;
            }
        }
        d.min(self.max)
    }
}

/// Everything the supervisor needs. The numbers are implementation settings (the contract freezes none of them); the defaults
/// are documented in docs/CORE_SUPERVISION.md.
#[derive(Clone, Debug)]
pub struct SupervisorConfig {
    /// The interpreter that runs the Core (`python3`, or a venv's python).
    pub program: PathBuf,
    /// Arguments that start the Core, e.g. `-m pet.core_main`. No secret, no key, no address ever goes here.
    pub serve_args: Vec<OsString>,
    /// Arguments of the one-time provisioning step, e.g. `-m pet.core_main provision` (the key is written to its stdin).
    pub provision_args: Vec<OsString>,
    pub working_dir: PathBuf,
    /// `YANDI_CORE_STATE_DIR` for the Core; also where the Core keeps its check value.
    pub state_dir: PathBuf,
    /// File inside `state_dir` whose existence tells the node that the Core was provisioned (`check-value.json`).
    pub check_value_file: String,
    /// Parent of the per-launch runtime directories (0700). See `RuntimeBase::default_path`.
    pub runtime_base: PathBuf,
    /// Where the Core's output goes (appended, 0600). `None`: inherited from the node.
    pub log_path: Option<PathBuf>,
    /// Inherit the node's environment (production: yes). Tests turn it off and pass what they need in `extra_env`.
    pub inherit_env: bool,
    pub extra_env: Vec<(OsString, OsString)>,
    /// From spawn until health says `locked`.
    pub startup_timeout: Duration,
    /// From the unlock request until health says `ready`.
    pub ready_timeout: Duration,
    pub poll_interval: Duration,
    /// Sent to the Core as `deadline_ms`; the node then waits this long plus `term_grace` before it escalates.
    pub shutdown_deadline: Duration,
    pub term_grace: Duration,
    pub backoff: BackoffPolicy,
    /// Automatic restarts allowed inside `failure_window`; one more failure stops the restarting.
    pub restart_limit: u32,
    pub failure_window: Duration,
}

impl SupervisorConfig {
    pub fn new(
        program: impl Into<PathBuf>,
        working_dir: impl Into<PathBuf>,
        state_dir: impl Into<PathBuf>,
        runtime_base: impl Into<PathBuf>,
    ) -> Self {
        SupervisorConfig {
            program: program.into(),
            serve_args: vec!["-m".into(), "pet.core_main".into()],
            provision_args: vec!["-m".into(), "pet.core_main".into(), "provision".into()],
            working_dir: working_dir.into(),
            state_dir: state_dir.into(),
            check_value_file: "check-value.json".into(),
            runtime_base: runtime_base.into(),
            log_path: None,
            inherit_env: true,
            extra_env: Vec::new(),
            startup_timeout: Duration::from_secs(90),
            ready_timeout: Duration::from_secs(30),
            poll_interval: Duration::from_millis(100),
            shutdown_deadline: Duration::from_secs(10),
            term_grace: Duration::from_secs(5),
            backoff: BackoffPolicy {
                initial: Duration::from_millis(500),
                max: Duration::from_secs(30),
                factor: 2,
            },
            restart_limit: 5,
            failure_window: Duration::from_secs(300),
        }
    }
}
