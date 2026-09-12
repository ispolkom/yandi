// tests/ai_rpc_no_ollama_test.rs
//! Node Intelligence RPC migration — integration proof.
//!
//! Central question from the mandate: "может ли две ноды YANDI
//! выполнить межнодовый AI-запрос, когда на обеих машинах Ollama
//! отсутствует полностью?" This test answers it by construction: it
//! spawns TWO real `llm_gateway.intelligence_bridge` Python processes
//! (each backed by a real local GGUF model via llama.cpp — genuinely no
//! Ollama anywhere in the loop), wires up two real `AiRpcService`
//! instances with real Ed25519 identities and the real signed-envelope
//! `TrustPolicy`, and drives a full A → B → B's llm_gateway → answer
//! round trip through `send_ai_infer_remote` / `RpcServer::handle` /
//! `resolve_pending` — the exact new code this migration added.
//!
//! What is intentionally NOT re-tested here: the real OS-level P2P
//! socket transport (NAT traversal, UDP/TCP dual path, discovery) — that
//! machinery is unchanged by this migration and already has its own
//! test coverage. Here, "the wire" is a pair of manually-connected
//! channels standing in for `NetworkTransport::send_encrypted`, exactly
//! matching how `main.rs` wires `gossip_tx` → `transport.send_encrypted`
//! → (on the peer) `ai_rpc_tx` → `RpcServer::handle` → `PKT_AI_RPC_RESPONSE`
//! → (back on the sender) `ai_rpc_response_tx` → `resolve_pending`. Every
//! byte that crosses this test's channels is the real signed/serialized
//! wire format (`RpcEnvelope`/`RpcResponse`), so envelope signing,
//! peer-allowlist validation, dispatch, and backend selection are all
//! exercised for real.
//!
//! Requires: llama-cpp-python importable AND the real GGUF file present
//! (same precondition as llm_gateway/llamacpp_backend_regression_test.py's
//! live smoke test). Skips (prints and returns) rather than failing when
//! that precondition isn't met, matching this repo's existing
//! "degrade gracefully for an absent optional dependency" test
//! discipline.
//!
//! Each test here runs a REAL local-model generation (CPU-bound,
//! seconds each) — run this file with `--test-threads=1`
//! (`cargo test --test ai_rpc_no_ollama_test -- --test-threads=1`) or two
//! concurrent llama.cpp instances will starve each other and can exceed
//! PEER_INFER_TIMEOUT for no architectural reason.

use std::process::{Child, Command, Stdio};
use std::time::Duration;

use ed25519_dalek::SigningKey;
use rand::rngs::OsRng;
use tokio::sync::mpsc;

use yandi::ai_rpc::policy::AllowedPeer;
use yandi::ai_rpc::types::{RpcResponse, PKT_AI_RPC_REQUEST};
use yandi::ai_rpc::{AiInferPayload, AiRpcService, ChatMessage};
use yandi::util::HashId;

/// Build one `trusted_ai_peers.json`-shaped entry for a test's own
/// `PeerDirectory`, so `send_ai_infer_remote` has a pinned key to verify
/// the eventual response against (Node Identity Binding Fix, Barrier 2).
fn test_peer(node_id: HashId, pubkey: [u8; 32], name: &str) -> yandi::ai_rpc::peer_directory::TrustedAiPeer {
    yandi::ai_rpc::peer_directory::TrustedAiPeer {
        node_id_hex: node_id.to_hex(),
        signing_pubkey_hex: hex::encode(pubkey),
        name: Some(name.to_string()),
    }
}

struct BridgeProc {
    child: Child,
}

impl Drop for BridgeProc {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Spawns a real `llm_gateway.intelligence_bridge` process, isolated
/// from any real production secure_store state via env overrides (same
/// isolation technique used for the Python-side live test earlier in
/// this mandate), pre-configured so its `yandi:peer-default` alias
/// resolves to the real local GGUF file directly (no Ollama).
fn spawn_bridge(port: u16, kek_path: &str, db_path: &str, gguf_path: &str) -> Option<BridgeProc> {
    let python = "/home/iam/venv/bin/python3";
    let repo_root = std::env::current_dir().ok()?.parent()?.to_path_buf();

    let configure = Command::new(python)
        .current_dir(&repo_root)
        .env("YANDI_KEK_PATH", kek_path)
        .env("YANDI_NODE_DB", db_path)
        .arg("-c")
        .arg(format!(
            // CPU-only (n_gpu_layers: 0) deliberately, not the usual -1:
            // this test suite's own repeated process kills across many
            // mandates this session have left the GPU holding ~11GB of
            // orphaned VRAM with no live owning process (confirmed via
            // nvidia-smi) — a real but purely environmental problem,
            // unrelated to this fix, that a full machine reboot would
            // clear. CPU inference is slower but deterministic and
            // unaffected by that, so it's what makes this suite reliable
            // regardless of host GPU state.
            "from llm_gateway import config; config.set_model_entry('yandi:peer-default', {{'backend': 'llamacpp', 'path': {gguf_path:?}, 'n_ctx': 4096, 'n_gpu_layers': 0}})"
        ))
        .status()
        .ok()?;
    if !configure.success() {
        return None;
    }

    let child = Command::new(python)
        .current_dir(&repo_root)
        .env("YANDI_KEK_PATH", kek_path)
        .env("YANDI_NODE_DB", db_path)
        .args(["-u", "-m", "llm_gateway.intelligence_bridge", &port.to_string()])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    Some(BridgeProc { child })
}

/// Poll until the bridge's port actually accepts a TCP connection,
/// instead of a fixed sleep — process/interpreter startup time varies.
async fn wait_for_port(port: u16, timeout: Duration) {
    let deadline = tokio::time::Instant::now() + timeout;
    loop {
        if tokio::net::TcpStream::connect(("127.0.0.1", port)).await.is_ok() {
            return;
        }
        if tokio::time::Instant::now() >= deadline {
            panic!("bridge on port {port} did not start listening within {timeout:?}");
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
}

fn gguf_available() -> Option<String> {
    let python = "/home/iam/venv/bin/python3";
    let out = Command::new(python)
        .arg("-c")
        .arg(
            "from llm_gateway import llamacpp_backend as be
import os
p = be._MODEL_REGISTRY['heretic:q8'].path
print(p if be.Llama is not None and os.path.exists(p) else '')",
        )
        .current_dir(std::env::current_dir().ok()?.parent()?)
        .output()
        .ok()?;
    let path = String::from_utf8(out.stdout).ok()?.trim().to_string();
    if path.is_empty() { None } else { Some(path) }
}

#[tokio::test]
async fn peer_inference_without_ollama() {
    let Some(gguf_path) = gguf_available() else {
        println!("[skip] llama_cpp unavailable or GGUF missing — see llamacpp_backend_regression_test.py");
        return;
    };

    let tmp = std::env::temp_dir().join(format!("yandi_ai_rpc_test_{}", std::process::id()));
    // secure_store's tamper-evidence checkpoint (node_config_chain_tip.json)
    // lives next to the KEK file, keyed by DIRECTORY, not by db/key
    // filename — two nodes' configs MUST live in separate directories,
    // exactly like two real nodes would never share one kek folder.
    let dir_a = tmp.join("node_a");
    let dir_b = tmp.join("node_b");
    std::fs::create_dir_all(&dir_a).unwrap();
    std::fs::create_dir_all(&dir_b).unwrap();

    // Two independent local intelligence bridges = two independent
    // nodes' answering backends. Deliberately DIFFERENT ports, DIFFERENT
    // isolated secure_store state — nothing shared between "A" and "B".
    let bridge_a = spawn_bridge(
        18197,
        dir_a.join("kek.bin").to_str().unwrap(),
        dir_a.join("db.sqlite").to_str().unwrap(),
        &gguf_path,
    )
    .expect("failed to configure/spawn bridge A");
    let bridge_b = spawn_bridge(
        18198,
        dir_b.join("kek.bin").to_str().unwrap(),
        dir_b.join("db.sqlite").to_str().unwrap(),
        &gguf_path,
    )
    .expect("failed to configure/spawn bridge B");
    wait_for_port(18197, Duration::from_secs(20)).await;
    wait_for_port(18198, Duration::from_secs(20)).await;

    let sk_a = SigningKey::generate(&mut OsRng);
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let addr_b = sk_b.verifying_key().to_bytes();

    let svc_a = AiRpcService::new("http://127.0.0.1:18197", sk_a.clone(), addr_a).expect("AiRpcService A");
    let svc_b = AiRpcService::new("http://127.0.0.1:18198", sk_b.clone(), addr_b).expect("AiRpcService B");

    // B allows A; A allows B — mirrors main.rs's real paired-peer
    // allowlist population from PairedClientStore, just without the
    // on-disk pairing file for this test.
    svc_a
        .lock()
        .await
        .add_peer(AllowedPeer { address: addr_b, signing_pubkey: addr_b, name: Some("B".into()), rpm_limit: None })
        .await
        .unwrap();
    svc_b
        .lock()
        .await
        .add_peer(AllowedPeer { address: addr_a, signing_pubkey: addr_a, name: Some("A".into()), rpm_limit: None })
        .await
        .unwrap();
    // Node Identity Binding Fix (Barrier 2): send_ai_infer_remote() now
    // refuses to send unless the target peer is in OUR OWN trusted
    // directory (so its eventual response can be verified) — A must know
    // B's real pubkey, not just have B in its AllowedPeer allowlist.
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));

    // Wire A's outbound gossip channel to A's own signing identity —
    // real code path, real signing key.
    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    // Cloned out ONCE, used directly from then on — mirrors main.rs's
    // real wiring; going back through svc_a.lock() to resolve a reply
    // would deadlock against send_ai_infer_remote's own held lock (see
    // PendingRequests doc comment in ai_rpc/mod.rs).
    let pending_a = svc_a.lock().await.pending_requests();

    // The "wire": whatever A tries to send, hand it directly to B's
    // RpcServer::handle (this is exactly what NetworkTransport::send_encrypted
    // + the receiving node's PKT_AI_RPC_REQUEST dispatch arm do — see
    // transport.rs). Then feed B's reply back into A's resolve_pending
    // (exactly what the PKT_AI_RPC_RESPONSE dispatch arm + the new
    // ai_rpc_resp_rx task in main.rs do).
    let svc_b_for_wire = svc_b.clone();
    tokio::spawn(async move {
        while let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            assert_eq!(framed[0], PKT_AI_RPC_REQUEST, "wire frame must be tagged as an AI-RPC request");
            let raw = &framed[1..];
            let rpc_server = svc_b_for_wire.lock().await.server.clone();
            let resp: RpcResponse = rpc_server.handle(raw).await;
            pending_a.resolve(resp).await;
        }
    });

    let payload = AiInferPayload {
        model: "some-model-A-would-like-B-to-use".to_string(), // must be IGNORED by B
        messages: vec![ChatMessage { role: "user".to_string(), content: "Ответь одним словом: сколько будет 2+2?".to_string() }],
        max_tokens: 20,
        stream: false,
        temperature: Some(0.0),
    };

    let peer_b = HashId(addr_b);
    let result = svc_a.lock().await.send_ai_infer_remote(peer_b, payload).await;

    match &result {
        Ok(resp) => println!("[ok] A received from B (via B's own llm_gateway, no Ollama): {:?}", resp.content),
        Err(e) => println!("[fail] A did not get an answer from B: {e}"),
    }

    assert!(result.is_ok(), "cross-node inference must succeed with Ollama absent on both nodes");
    assert!(!result.unwrap().content.trim().is_empty(), "B's answer must be non-empty real model output");

    drop(bridge_a);
    drop(bridge_b);
    let _ = std::fs::remove_dir_all(&tmp);

    println!("PEER INFERENCE WITHOUT OLLAMA: PASS");
}

/// Adversarial: A cannot dictate which physical model/backend B uses.
/// Two different (bogus, unconfigured) "model" strings must both be
/// silently ignored and answered by B's own single configured alias.
#[tokio::test]
async fn peer_cannot_choose_backend_model() {
    let Some(gguf_path) = gguf_available() else {
        println!("[skip] llama_cpp unavailable or GGUF missing");
        return;
    };

    let tmp = std::env::temp_dir().join(format!("yandi_ai_rpc_test2_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    let bridge_b = spawn_bridge(
        18199,
        tmp.join("b_kek.bin").to_str().unwrap(),
        tmp.join("b_db.sqlite").to_str().unwrap(),
        &gguf_path,
    )
    .expect("failed to configure/spawn bridge B");
    wait_for_port(18199, Duration::from_secs(20)).await;

    let sk_a = SigningKey::generate(&mut OsRng);
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let addr_b = sk_b.verifying_key().to_bytes();

    let svc_a = AiRpcService::new("http://127.0.0.1:18099", sk_a.clone(), addr_a).expect("AiRpcService A"); // A has no real bridge — irrelevant, A only sends
    let svc_b = AiRpcService::new("http://127.0.0.1:18199", sk_b.clone(), addr_b).expect("AiRpcService B");

    svc_a.lock().await.add_peer(AllowedPeer { address: addr_b, signing_pubkey: addr_b, name: None, rpm_limit: None }).await.unwrap();
    svc_b.lock().await.add_peer(AllowedPeer { address: addr_a, signing_pubkey: addr_a, name: None, rpm_limit: None }).await.unwrap();
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));

    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    let pending_a = svc_a.lock().await.pending_requests();

    let svc_b_for_wire = svc_b.clone();
    tokio::spawn(async move {
        while let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            let raw = &framed[1..];
            let rpc_server = svc_b_for_wire.lock().await.server.clone();
            let resp: RpcResponse = rpc_server.handle(raw).await;
            pending_a.resolve(resp).await;
        }
    });

    for bogus_model in ["claude-3-opus-DIRECT", "gpt-4-please", ""] {
        let payload = AiInferPayload {
            model: if bogus_model.is_empty() { "x".to_string() } else { bogus_model.to_string() },
            messages: vec![ChatMessage { role: "user".to_string(), content: "2+2?".to_string() }],
            max_tokens: 10,
            stream: false,
            temperature: Some(0.0),
        };
        let result = svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload).await;
        assert!(result.is_ok(), "B should still answer regardless of A's requested model string: {result:?}");
    }

    drop(bridge_b);
    let _ = std::fs::remove_dir_all(&tmp);
    println!("[ok] A could not steer B's backend via the model field, across 3 attempts");
}

/// TEST8 / adversarial §14: an unpaired peer gets no inference. No live
/// bridge needed at all — the request must be rejected by
/// `TrustPolicy::validate` before dispatch, so no backend is ever touched.
#[tokio::test]
async fn unpaired_peer_gets_no_inference() {
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();
    let svc_b = AiRpcService::new("http://127.0.0.1:1", sk_b, addr_b).expect("AiRpcService B"); // bogus bridge URL — must never be reached

    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    // Deliberately NOT calling svc_b.add_peer(A) — A is a stranger to B.

    let payload = AiInferPayload {
        model: "whatever".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let payload_bytes = bincode::serialize(&payload).unwrap();
    let mut env = yandi::ai_rpc::RpcEnvelope {
        version: yandi::ai_rpc::AI_RPC_VERSION,
        request_id: 1,
        nonce: rand::random(),
        timestamp_ms: yandi::ai_rpc::now_ms(),
        sender: addr_a,
        method: yandi::ai_rpc::RpcMethod::AiInfer,
        payload: payload_bytes,
        signature: vec![],
    };
    yandi::ai_rpc::sign_envelope(&mut env, &sk_a);
    let raw = env.to_bytes().unwrap();

    let resp = svc_b.lock().await.server.handle(&raw).await;
    match resp.status {
        yandi::ai_rpc::RpcStatus::Err(yandi::ai_rpc::RpcError::Unauthorized) => {
            println!("[ok] unpaired peer correctly rejected before any backend call");
        }
        other => panic!("expected Unauthorized for an unpaired sender, got {other:?}"),
    }
}

/// TEST9 / adversarial §14: a malformed request never reaches a backend.
/// Garbage bytes, an empty buffer, and a validly-signed-but-empty-model
/// payload must all be rejected by decode/sanitisation, not by the
/// backend call itself (bogus bridge URL that would error loudly if hit).
#[tokio::test]
async fn malformed_requests_never_reach_a_backend() {
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();
    let svc_b = AiRpcService::new("http://127.0.0.1:1", sk_b, addr_b).expect("AiRpcService B");

    let resp = svc_b.lock().await.server.handle(b"not a valid bincode envelope at all").await;
    assert!(matches!(resp.status, yandi::ai_rpc::RpcStatus::Err(_)), "garbage bytes must produce an error response, not a panic or a backend call");

    let resp = svc_b.lock().await.server.handle(&[]).await;
    assert!(matches!(resp.status, yandi::ai_rpc::RpcStatus::Err(_)), "empty buffer must produce an error response");

    // Signed, valid envelope, but sender not paired AND model empty —
    // must fail on the allowlist step (or, if somehow paired, on the
    // empty-model guard) — never on a backend HTTP call to :1.
    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    svc_b.lock().await.add_peer(AllowedPeer { address: addr_a, signing_pubkey: addr_a, name: None, rpm_limit: None }).await.unwrap();
    let payload = AiInferPayload {
        model: "".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let payload_bytes = bincode::serialize(&payload).unwrap();
    let mut env = yandi::ai_rpc::RpcEnvelope {
        version: yandi::ai_rpc::AI_RPC_VERSION,
        request_id: 1,
        nonce: rand::random(),
        timestamp_ms: yandi::ai_rpc::now_ms(),
        sender: addr_a,
        method: yandi::ai_rpc::RpcMethod::AiInfer,
        payload: payload_bytes,
        signature: vec![],
    };
    yandi::ai_rpc::sign_envelope(&mut env, &sk_a);
    let resp = svc_b.lock().await.server.handle(&env.to_bytes().unwrap()).await;
    match resp.status {
        yandi::ai_rpc::RpcStatus::Err(yandi::ai_rpc::RpcError::InvalidPayload(_)) => {
            println!("[ok] empty-model payload rejected before reaching the (bogus, unreachable) backend");
        }
        other => panic!("expected InvalidPayload for an empty model name, got {other:?}"),
    }
}

/// TEST5 / adversarial §14 ("force fallback after an explicit backend
/// failure"): B's bridge is reachable but its configured backend fails
/// (points at a nonexistent GGUF file) — A must get an honest error,
/// never a silently substituted answer from a different backend.
#[tokio::test]
async fn explicit_backend_failure_is_honest_not_silently_substituted() {
    let tmp = std::env::temp_dir().join(format!("yandi_ai_rpc_test_fail_{}", std::process::id()));
    std::fs::create_dir_all(&tmp).unwrap();
    // Deliberately a nonexistent path — B's owner "configured" a backend
    // that will fail at generation time.
    let bridge_b = spawn_bridge(
        18296,
        tmp.join("kek.bin").to_str().unwrap(),
        tmp.join("db.sqlite").to_str().unwrap(),
        "/nonexistent/path/does-not-exist.gguf",
    )
    .expect("failed to configure/spawn bridge B");
    wait_for_port(18296, Duration::from_secs(20)).await;

    let sk_a = SigningKey::generate(&mut OsRng);
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let addr_b = sk_b.verifying_key().to_bytes();
    let svc_a = AiRpcService::new("http://127.0.0.1:1", sk_a.clone(), addr_a).expect("AiRpcService A");
    let svc_b = AiRpcService::new("http://127.0.0.1:18296", sk_b, addr_b).expect("AiRpcService B");
    svc_a.lock().await.add_peer(AllowedPeer { address: addr_b, signing_pubkey: addr_b, name: None, rpm_limit: None }).await.unwrap();
    svc_b.lock().await.add_peer(AllowedPeer { address: addr_a, signing_pubkey: addr_a, name: None, rpm_limit: None }).await.unwrap();
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));

    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    let pending_a = svc_a.lock().await.pending_requests();
    let svc_b_for_wire = svc_b.clone();
    tokio::spawn(async move {
        while let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            let raw = &framed[1..];
            let rpc_server = svc_b_for_wire.lock().await.server.clone();
            let resp: RpcResponse = rpc_server.handle(raw).await;
            pending_a.resolve(resp).await;
        }
    });

    let payload = AiInferPayload {
        model: "irrelevant".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let result = svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload).await;

    assert!(result.is_err(), "B's explicit (broken) backend must surface as an honest error to A, never a masked success: {result:?}");
    println!("[ok] B's broken explicit backend produced an honest error to A: {result:?}");

    drop(bridge_b);
    let _ = std::fs::remove_dir_all(&tmp);
}

/// Real Node Directory Integration: proves the specific bug found during
/// this mandate's audit is fixed. A real `main.rs` node signs its own
/// outbound envelopes with `sender = identity.node_id()` — a value that
/// has NO relationship to its Ed25519 signing public key (confirmed by
/// reading `NodeIdentity::new()`: `address` is independently random).
/// The earlier AI-RPC allowlist wiring set `AllowedPeer::address` from a
/// peer's PUBKEY, which would never match a real peer's actual `sender`
/// (their node_id) — meaning trust validation would always fail
/// (Unauthorized) for a real different physical machine, even though
/// every previous test here happened to use identical node_id/pubkey
/// values and so never caught it. This test deliberately uses TWO
/// DIFFERENT, unrelated values for A's node_id and A's signing pubkey —
/// exactly the realistic shape — and confirms B still authorizes and
/// answers A correctly when its allowlist entry is built the FIXED way
/// (`address = node_id`, `signing_pubkey = <actual pubkey>`, independently).
#[tokio::test]
async fn realistic_distinct_node_id_and_pubkey_still_authorize() {
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();
    let svc_b = AiRpcService::new("http://127.0.0.1:1", sk_b, addr_b).expect("AiRpcService B"); // unreachable bridge, but request must get past auth first

    let sk_a = SigningKey::generate(&mut OsRng);
    let real_pubkey_a = sk_a.verifying_key().to_bytes();
    // A's routing identity is a SEPARATE random value, exactly like
    // `NodeIdentity::new()`'s `address: HashId::new_random()` — NOT
    // derived from the signing key at all.
    let node_id_a = HashId::new_random();
    assert_ne!(node_id_a.0, real_pubkey_a, "test setup must use genuinely distinct values, matching production reality");

    // The FIXED allowlist shape: address = node_id, signing_pubkey = real pubkey.
    svc_b
        .lock()
        .await
        .add_peer(AllowedPeer { address: node_id_a.0, signing_pubkey: real_pubkey_a, name: Some("A".into()), rpm_limit: None })
        .await
        .unwrap();

    let payload = AiInferPayload {
        model: "irrelevant".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let payload_bytes = bincode::serialize(&payload).unwrap();
    let mut env = yandi::ai_rpc::RpcEnvelope {
        version: yandi::ai_rpc::AI_RPC_VERSION,
        request_id: 1,
        nonce: rand::random(),
        timestamp_ms: yandi::ai_rpc::now_ms(),
        sender: node_id_a.0, // A claims to be its node_id, NOT its pubkey
        method: yandi::ai_rpc::RpcMethod::AiInfer,
        payload: payload_bytes,
        signature: vec![],
    };
    yandi::ai_rpc::sign_envelope(&mut env, &sk_a); // but signs with its REAL key

    let resp = svc_b.lock().await.server.handle(&env.to_bytes().unwrap()).await;
    match resp.status {
        yandi::ai_rpc::RpcStatus::Err(yandi::ai_rpc::RpcError::Unauthorized) => {
            panic!("A was rejected as Unauthorized despite a correctly-shaped allowlist entry — the node_id/pubkey bug is NOT fixed");
        }
        yandi::ai_rpc::RpcStatus::Err(yandi::ai_rpc::RpcError::BackendError(_)) => {
            // Expected: auth passed, request reached the (deliberately
            // unreachable) bridge and failed there — proves the
            // signature/allowlist check itself succeeded.
            println!("[ok] distinct node_id/pubkey correctly authorized (failed only at the unreachable-bridge step, as expected)");
        }
        other => panic!("unexpected outcome: {other:?}"),
    }
}

/// Real Two-Node P2P E2E test mandate — regression test for a real bug
/// found only by actually running two live node processes and doing a
/// genuine end-to-end request: `dispatch_decrypted_wagon()` (the real
/// P2P transport's inbound packet dispatcher, see
/// `netlayer/transport.rs`) hands every handler the FULL plaintext
/// INCLUDING its own leading packet-type tag byte — it never strips it.
/// Every other handler in that match (e.g. `handle_resume_packet` /
/// `decode_resume`, whose own wire format doc comment is literally
/// `[C0][node_id:32]...`) already expects and skips that leading byte
/// itself. `main.rs`'s AI-RPC inbound request/response tasks did NOT —
/// they fed the full framed bytes (tag byte still attached) straight
/// into `RpcEnvelope::from_bytes()` / `RpcResponse::from_bytes()`,
/// silently misaligning every field after the first byte. This was
/// invisible to every prior test in this file because they all drove
/// `RpcServer::handle()` / `PendingRequests::resolve()` directly with
/// already-correctly-stripped bytes or in-memory values, never through
/// the real dispatcher. A genuine two-node run surfaced it immediately
/// as "failed to decode envelope: invalid value: integer 325..." /
/// "peer did not answer in time". Fixed in main.rs by slicing `&raw[1..]`
/// before decoding in both the inbound-request and inbound-response
/// tasks. This test pins the wire contract so it cannot silently regress.
#[test]
fn ai_rpc_frame_requires_stripping_the_leading_tag_byte_before_decode() {
    let sk = SigningKey::generate(&mut OsRng);
    let sender = sk.verifying_key().to_bytes();
    let payload = AiInferPayload {
        model: "x".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let payload_bytes = bincode::serialize(&payload).unwrap();
    let mut env = yandi::ai_rpc::RpcEnvelope {
        version: yandi::ai_rpc::AI_RPC_VERSION,
        request_id: 1,
        nonce: rand::random(),
        timestamp_ms: yandi::ai_rpc::now_ms(),
        sender,
        method: yandi::ai_rpc::RpcMethod::AiInfer,
        payload: payload_bytes,
        signature: vec![],
    };
    yandi::ai_rpc::sign_envelope(&mut env, &sk);
    let env_bytes = env.to_bytes().unwrap();

    // Exactly what send_ai_infer_remote()/_gossip_kb() hand to send_encrypted(),
    // and exactly what dispatch_decrypted_wagon() hands onward, untouched.
    let mut framed = Vec::with_capacity(1 + env_bytes.len());
    framed.push(PKT_AI_RPC_REQUEST);
    framed.extend_from_slice(&env_bytes);

    assert!(
        yandi::ai_rpc::RpcEnvelope::from_bytes(&framed).is_err()
            || yandi::ai_rpc::RpcEnvelope::from_bytes(&framed).unwrap().sender != sender,
        "decoding WITHOUT stripping the tag byte must fail or misdecode — \
         if this ever starts succeeding cleanly, the wire framing convention changed \
         and main.rs's raw[1..] slicing must be revisited",
    );

    let decoded = yandi::ai_rpc::RpcEnvelope::from_bytes(&framed[1..])
        .expect("decoding WITH the tag byte stripped must succeed — this is the real fix");
    assert_eq!(decoded.sender, sender);
    assert_eq!(decoded.method, yandi::ai_rpc::RpcMethod::AiInfer);
}

// ── Node Identity Binding Fix — Barrier 2 tests ─────────────────────────────
//
// The live exploit's second half: even with routing hijacked to X, a
// signed, verified response is the independent second barrier that must
// still refuse X's answer. These tests intercept the OUTGOING request on
// the wire (real signing, real framing, real request_id) and, instead of
// routing it to a real RpcServer, hand a hand-crafted "forged" RpcResponse
// straight to `PendingRequests::resolve()` — exactly the shape a hijacked
// routing slot would let an attacker deliver. No live model/bridge needed.

fn sign_response(resp: &mut RpcResponse, key: &SigningKey) {
    use ed25519_dalek::Signer;
    resp.signature = key.sign(&resp.canonical_bytes()).to_bytes().to_vec();
}

/// Shared setup: A trusts B (real key), sends exactly one AiInfer request,
/// and the caller's `forge` closure gets to see the real, decoded outgoing
/// envelope (so it knows the real `request_id`) and must produce the
/// `RpcResponse` to feed back through `resolve()` instead of a real one.
/// Returns what `send_ai_infer_remote` resolved to.
async fn run_forgery_scenario(
    forge: impl FnOnce(&yandi::ai_rpc::RpcEnvelope, [u8; 32]) -> RpcResponse + Send + 'static,
) -> Result<yandi::ai_rpc::AiInferResponse, yandi::ai_rpc::RpcError> {
    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();

    let svc_a = AiRpcService::new("http://127.0.0.1:1", sk_a, addr_a).expect("AiRpcService A");
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));

    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    let pending_a = svc_a.lock().await.pending_requests();

    tokio::spawn(async move {
        if let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            let env = yandi::ai_rpc::RpcEnvelope::from_bytes(&framed[1..])
                .expect("A's own outgoing envelope must decode");
            // addr_b is what A ACTUALLY expects the responder to be — pass
            // it to `forge` so scenarios can correctly claim to be B while
            // getting some OTHER aspect (key/payload/etc.) wrong.
            let forged = forge(&env, addr_b);
            pending_a.resolve(forged).await;
        }
    });

    let payload = AiInferPayload {
        model: "irrelevant".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10,
        stream: false,
        temperature: None,
    };
    let result = svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload).await;
    result
}

/// TEST9: a response from X's key under B's node_id must be rejected —
/// the exact second half of the original live exploit.
#[tokio::test]
async fn forged_response_from_wrong_key_is_rejected() {
    let sk_x = SigningKey::generate(&mut OsRng); // attacker's OWN, unrelated, otherwise-valid key

    let result = run_forgery_scenario(move |env, addr_b| {
        // X correctly claims to BE B (responder = addr_b, exactly what A
        // expects) — the ONLY thing wrong is the signing key underneath.
        let mut resp = RpcResponse {
            request_id: env.request_id,
            requester: env.sender,
            responder: addr_b,
            status: yandi::ai_rpc::RpcStatus::Ok,
            is_chunk: false,
            chunk_done: true,
            payload: bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "FORGED BY X".into(), tokens_used: None }).unwrap(),
            signature: vec![],
        };
        sign_response(&mut resp, &sk_x); // signed with X's key, NOT B's pinned key
        resp
    }).await;

    match result {
        Err(yandi::ai_rpc::RpcError::ResponseAuthFailed(msg)) => {
            println!("[ok] forged response from the wrong key rejected: {msg}");
        }
        other => panic!("FORGED RESPONSE FROM WRONG KEY ACCEPTED: YES — got {other:?}"),
    }
}

/// TEST10: a response with no signature at all must be rejected.
#[tokio::test]
async fn unsigned_response_is_rejected() {
    let result = run_forgery_scenario(|env, addr_b| RpcResponse {
        request_id: env.request_id,
        requester: env.sender,
        responder: addr_b,
        status: yandi::ai_rpc::RpcStatus::Ok,
        is_chunk: false,
        chunk_done: true,
        payload: bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "no sig".into(), tokens_used: None }).unwrap(),
        signature: vec![], // never signed
    }).await;

    match result {
        Err(yandi::ai_rpc::RpcError::ResponseAuthFailed(msg)) => println!("[ok] unsigned response rejected: {msg}"),
        other => panic!("an unsigned response was accepted: {other:?}"),
    }
}

/// TEST11: a legitimately-signed response whose payload is altered
/// AFTER signing must be rejected (signature no longer matches).
#[tokio::test]
async fn payload_altered_after_signing_is_rejected() {
    let sk_b = SigningKey::generate(&mut OsRng); // this scenario needs B's real key baked in below
    let real_pubkey_b = sk_b.verifying_key().to_bytes();

    // Build the scenario manually (not via run_forgery_scenario) so we can
    // pin A's directory to THIS SPECIFIC sk_b/addr_b pair.
    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let addr_b = real_pubkey_b;

    let svc_a = AiRpcService::new("http://127.0.0.1:1", sk_a, addr_a).expect("AiRpcService A");
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));
    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    let pending_a = svc_a.lock().await.pending_requests();

    tokio::spawn(async move {
        if let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            let env = yandi::ai_rpc::RpcEnvelope::from_bytes(&framed[1..]).unwrap();
            let mut resp = RpcResponse {
                request_id: env.request_id,
                requester: env.sender,
                responder: addr_b,
                status: yandi::ai_rpc::RpcStatus::Ok,
                is_chunk: false,
                chunk_done: true,
                payload: bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "honest answer".into(), tokens_used: None }).unwrap(),
                signature: vec![],
            };
            sign_response(&mut resp, &sk_b); // signed HONESTLY, with B's real key
            resp.payload = bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "TAMPERED answer".into(), tokens_used: None }).unwrap(); // tampered AFTER signing
            pending_a.resolve(resp).await;
        }
    });

    let payload = AiInferPayload {
        model: "irrelevant".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10, stream: false, temperature: None,
    };
    let result = svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload).await;
    match result {
        Err(yandi::ai_rpc::RpcError::ResponseAuthFailed(msg)) => println!("[ok] tampered payload rejected: {msg}"),
        other => panic!("a response tampered after signing was accepted: {other:?}"),
    }
}

/// TEST13: responder field altered after signing (claims a different
/// node_id than the one actually signed for) must be rejected.
#[tokio::test]
async fn altered_responder_field_is_rejected() {
    let sk_x = SigningKey::generate(&mut OsRng);
    let result = run_forgery_scenario(move |env, _addr_b| {
        let mut resp = RpcResponse {
            request_id: env.request_id,
            requester: env.sender,
            responder: sk_x.verifying_key().to_bytes(), // X honestly signs AS ITSELF...
            status: yandi::ai_rpc::RpcStatus::Ok,
            is_chunk: false,
            chunk_done: true,
            payload: bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "hi".into(), tokens_used: None }).unwrap(),
            signature: vec![],
        };
        sign_response(&mut resp, &sk_x);
        resp.responder = [0xBBu8; 32]; // ...then relabels itself as B AFTER signing
        resp
    }).await;

    match result {
        Err(yandi::ai_rpc::RpcError::ResponseAuthFailed(msg)) => println!("[ok] responder altered post-signature rejected: {msg}"),
        other => panic!("a response with an altered responder field was accepted: {other:?}"),
    }
}

/// TEST12: a response for a DIFFERENT (unrelated) request_id must simply
/// never match this pending request at all — proving `resolve()` can't be
/// tricked into completing the wrong waiter via an unrelated valid response.
#[tokio::test]
async fn mismatched_request_id_never_resolves_the_wrong_waiter() {
    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();

    let svc_a = AiRpcService::new("http://127.0.0.1:1", sk_a, addr_a).expect("AiRpcService A");
    svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
        yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
    ));
    let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
    svc_a.lock().await.set_gossip_channel(gossip_tx_a);
    let pending_a = svc_a.lock().await.pending_requests();

    tokio::spawn(async move {
        if let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
            let env = yandi::ai_rpc::RpcEnvelope::from_bytes(&framed[1..]).unwrap();
            let mut resp = RpcResponse {
                request_id: env.request_id.wrapping_add(12345), // deliberately WRONG request_id
                requester: env.sender,
                responder: addr_b,
                status: yandi::ai_rpc::RpcStatus::Ok,
                is_chunk: false,
                chunk_done: true,
                payload: bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "for someone else".into(), tokens_used: None }).unwrap(),
                signature: vec![],
            };
            sign_response(&mut resp, &sk_b); // even honestly, correctly signed by the REAL B
            pending_a.resolve(resp).await; // must be silently dropped — no matching pending entry
        }
    });

    let payload = AiInferPayload {
        model: "irrelevant".to_string(),
        messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
        max_tokens: 10, stream: false, temperature: None,
    };
    // Shrink the wait so this test doesn't sit for the full 90s timeout.
    let result = tokio::time::timeout(
        Duration::from_secs(3),
        svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload),
    ).await;
    assert!(result.is_err(), "a response for an unrelated request_id must never resolve THIS pending request — it must simply time out, not succeed with someone else's answer");
    println!("[ok] mismatched request_id response never resolved this waiter (timed out as expected)");
}

/// TEST7/TEST8: the honest path still works — a correctly-signed success
/// AND a correctly-signed error response are both accepted as authenticated.
#[tokio::test]
async fn correctly_signed_success_and_error_responses_are_accepted() {
    let sk_a = SigningKey::generate(&mut OsRng);
    let addr_a = sk_a.verifying_key().to_bytes();
    let sk_b = SigningKey::generate(&mut OsRng);
    let addr_b = sk_b.verifying_key().to_bytes();

    for (send_error, expect_ok) in [(false, true), (true, false)] {
        let svc_a = AiRpcService::new("http://127.0.0.1:1", sk_a.clone(), addr_a).expect("AiRpcService A");
        svc_a.lock().await.set_peer_directory(std::sync::Arc::new(
            yandi::ai_rpc::PeerDirectory { peers: vec![test_peer(HashId(addr_b), addr_b, "B")] },
        ));
        let (gossip_tx_a, mut gossip_rx_a) = mpsc::channel::<(HashId, Vec<u8>)>(8);
        svc_a.lock().await.set_gossip_channel(gossip_tx_a);
        let pending_a = svc_a.lock().await.pending_requests();
        let sk_b = sk_b.clone();

        tokio::spawn(async move {
            if let Some((_peer_id, framed)) = gossip_rx_a.recv().await {
                let env = yandi::ai_rpc::RpcEnvelope::from_bytes(&framed[1..]).unwrap();
                let status = if send_error {
                    yandi::ai_rpc::RpcStatus::Err(yandi::ai_rpc::RpcError::BackendError("B's backend really is down".into()))
                } else {
                    yandi::ai_rpc::RpcStatus::Ok
                };
                let payload = if send_error { vec![] } else {
                    bincode::serialize(&yandi::ai_rpc::AiInferResponse { content: "honest answer".into(), tokens_used: Some(3) }).unwrap()
                };
                let mut resp = RpcResponse {
                    request_id: env.request_id, requester: env.sender, responder: addr_b,
                    status, is_chunk: false, chunk_done: true, payload, signature: vec![],
                };
                sign_response(&mut resp, &sk_b);
                pending_a.resolve(resp).await;
            }
        });

        let payload = AiInferPayload {
            model: "irrelevant".to_string(),
            messages: vec![ChatMessage { role: "user".to_string(), content: "hi".to_string() }],
            max_tokens: 10, stream: false, temperature: None,
        };
        let result = svc_a.lock().await.send_ai_infer_remote(HashId(addr_b), payload).await;
        assert_eq!(result.is_ok(), expect_ok, "send_error={send_error} result={result:?}");
        match &result {
            Ok(r) => println!("[ok] correctly-signed SUCCESS response accepted: {:?}", r.content),
            Err(yandi::ai_rpc::RpcError::BackendError(m)) => println!("[ok] correctly-signed ERROR response accepted as an authenticated error: {m}"),
            Err(e) => panic!("expected an authenticated BackendError, got a different error: {e}"),
        }
    }
}
