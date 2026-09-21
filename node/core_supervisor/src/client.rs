//! The minimal client of the Core API: only the P1 lifecycle requests. It is not the egress client (P2).
use crate::keys::CoreKey;
use crate::runtime::LaunchSecret;
use rand::RngCore;
use std::time::Duration;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HealthState {
    Starting,
    Locked,
    Ready,
    Draining,
    Failed,
    Unknown,
}

impl HealthState {
    pub fn as_str(&self) -> &'static str {
        match self {
            HealthState::Starting => "starting",
            HealthState::Locked => "locked",
            HealthState::Ready => "ready",
            HealthState::Draining => "draining",
            HealthState::Failed => "failed",
            HealthState::Unknown => "unknown",
        }
    }
}

/// What can go wrong, without anything the Core said beyond its stable error code.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ClientError {
    Unreachable,
    Timeout,
    Refused { status: u16, code: String },
    BadAnswer,
}

impl std::fmt::Display for ClientError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ClientError::Unreachable => write!(f, "the core is not reachable"),
            ClientError::Timeout => write!(f, "the core did not answer in time"),
            ClientError::Refused { status, code } => {
                write!(f, "the core refused the request ({status} {code})")
            }
            ClientError::BadAnswer => write!(f, "the core's answer was not understood"),
        }
    }
}

pub struct CoreClient {
    http: reqwest::Client,
    base: String,
    secret: String,
}

impl CoreClient {
    /// `port` must be a port the node knows its own child bound. Talks to 127.0.0.1 only, never through a proxy.
    pub fn new(port: u16, secret: &LaunchSecret) -> Self {
        let http = reqwest::Client::builder()
            .no_proxy()
            .redirect(reqwest::redirect::Policy::none())
            .timeout(Duration::from_secs(5))
            .pool_max_idle_per_host(0)
            .build()
            .expect("a plain HTTP client can always be built");
        CoreClient {
            http,
            base: format!("http://127.0.0.1:{port}"),
            secret: secret.expose().to_owned(),
        }
    }

    fn idempotency_key() -> String {
        let mut b = [0u8; 12];
        rand::rngs::OsRng.fill_bytes(&mut b);
        format!("node-{}", hex::encode(b))
    }

    fn map_send(e: reqwest::Error) -> ClientError {
        if e.is_timeout() {
            ClientError::Timeout
        } else {
            ClientError::Unreachable
        }
    }

    async fn error_from(resp: reqwest::Response) -> ClientError {
        let status = resp.status().as_u16();
        let code = resp
            .json::<serde_json::Value>()
            .await
            .ok()
            .and_then(|v| v["error"]["code"].as_str().map(str::to_owned))
            .filter(|c| c.len() <= 40 && c.bytes().all(|b| b.is_ascii_lowercase() || b == b'_'))
            .unwrap_or_else(|| "unknown".to_owned());
        ClientError::Refused { status, code }
    }

    /// `GET /v1/health`: open, no credentials sent.
    pub async fn health(&self) -> Result<HealthState, ClientError> {
        let resp = self
            .http
            .get(format!("{}/v1/health", self.base))
            .send()
            .await
            .map_err(Self::map_send)?;
        if !resp.status().is_success() {
            return Err(Self::error_from(resp).await);
        }
        let v: serde_json::Value = resp.json().await.map_err(|_| ClientError::BadAnswer)?;
        Ok(match v["state"].as_str() {
            Some("starting") => HealthState::Starting,
            Some("locked") => HealthState::Locked,
            Some("ready") => HealthState::Ready,
            Some("draining") => HealthState::Draining,
            Some("failed") => HealthState::Failed,
            Some(_) => HealthState::Unknown,
            None => return Err(ClientError::BadAnswer),
        })
    }

    pub async fn capabilities(&self) -> Result<serde_json::Value, ClientError> {
        let resp = self
            .http
            .get(format!("{}/v1/capabilities", self.base))
            .bearer_auth(&self.secret)
            .send()
            .await
            .map_err(Self::map_send)?;
        if !resp.status().is_success() {
            return Err(Self::error_from(resp).await);
        }
        resp.json().await.map_err(|_| ClientError::BadAnswer)
    }

    async fn post(&self, path: &str, body: serde_json::Value) -> Result<(), ClientError> {
        let resp = self
            .http
            .post(format!("{}{}", self.base, path))
            .bearer_auth(&self.secret)
            .header("Idempotency-Key", Self::idempotency_key())
            .json(&body)
            .send()
            .await
            .map_err(Self::map_send)?;
        if resp.status().is_success() {
            Ok(())
        } else {
            Err(Self::error_from(resp).await)
        }
    }

    /// `POST /v1/unlock` with the derived key (never the master key).
    pub async fn unlock(&self, key: &CoreKey) -> Result<(), ClientError> {
        let encoded = key.to_base64();
        self.post(
            "/v1/unlock",
            serde_json::json!({"key": encoded.as_str(), "context": crate::keys::CORE_CONTEXT}),
        )
        .await
    }

    pub async fn lock(&self) -> Result<(), ClientError> {
        self.post("/v1/lock", serde_json::json!({})).await
    }

    pub async fn shutdown(&self, deadline: Duration) -> Result<(), ClientError> {
        self.post(
            "/v1/shutdown",
            serde_json::json!({"deadline_ms": deadline.as_millis().max(1) as u64}),
        )
        .await
    }
}
