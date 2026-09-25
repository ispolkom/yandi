//! Узел отвечает на AI-запросы СВОИМ движком, без Python-моста: настройки владельца (`yandi:peer-default`) → вшитый/указанный `llama-server`.
//! Вместо настоящего llama-server — подделка из rustlib/yandi_llm/tests. Тесты меняют переменные окружения процесса → запускать `-- --test-threads=1`.
use yandi::ai_rpc::intelligence_bridge::{use_native, IntelligenceBridgeClient};
use yandi::ai_rpc::types::{AiInferPayload, ChatMessage, RpcError};

fn payload(text: &str) -> AiInferPayload {
    AiInferPayload {
        model: "ignored-by-design".into(),
        messages: vec![ChatMessage { role: "user".into(), content: text.into() }],
        max_tokens: 32,
        stream: false,
        temperature: Some(0.5),
    }
}

#[tokio::test(flavor = "multi_thread")]
async fn native_bridge_answers_from_owner_config_via_own_engine() {
    let dir = std::env::temp_dir().join(format!("yandi-nat-intel-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("YANDI_KEK_PATH", dir.join("keys/kek.bin"));
    std::env::set_var("YANDI_NODE_DB", dir.join("node.sqlite"));
    std::env::set_var("YANDI_INTELLIGENCE_ENGINE", "native");
    std::env::set_var("YANDI_LLAMA_SERVER", concat!(env!("CARGO_MANIFEST_DIR"), "/../rustlib/yandi_llm/tests/fake_llama_server.py"));
    assert!(use_native());
    let gguf = dir.join("peer.gguf");
    std::fs::write(&gguf, b"x").unwrap();

    let client = IntelligenceBridgeClient::new("http://127.0.0.1:1").unwrap(); // Python-мост НЕ нужен: по этому адресу никто не слушает
    assert!(client.is_reachable().await, "движок должен быть найден");

    // владелец ничего не настроил → честная ошибка, наружу только категория, никакого Ollama
    match client.complete(&payload("привет")).await {
        Err(RpcError::BackendError(m)) => assert_eq!(m, "backend_error"),
        other => panic!("ожидалась ошибка backend_error, получено {other:?}"),
    }

    // владелец настроил алиас (так делает веб) → отвечает собственный движок
    yandi_llm::secure_store::set_model_entry("yandi:peer-default", &serde_json::json!({"backend": "llamacpp", "path": gguf.to_string_lossy()})).unwrap();
    let r = client.complete(&payload("привет, узел")).await.expect("ответ движка");
    let echoed: serde_json::Value = serde_json::from_str(&r.content).unwrap();
    assert_eq!(echoed["messages"], serde_json::json!([{"role": "user", "content": "привет, узел"}]));
    assert_eq!(echoed["max_tokens"], serde_json::json!(32));
    assert_eq!(r.tokens_used, Some(7));
    // поле model пира не влияет на выбор
    let mut p = payload("ещё раз");
    p.model = "hack:model".into();
    assert!(client.complete(&p).await.is_ok());
    let _ = std::fs::remove_dir_all(&dir);
}

#[tokio::test(flavor = "multi_thread")]
async fn explicit_python_mode_keeps_http_bridge() {
    std::env::set_var("YANDI_INTELLIGENCE_ENGINE", "python");
    assert!(!use_native());
    // по адресу никто не слушает → ошибка сети, а не родной путь
    let client = IntelligenceBridgeClient::new("http://127.0.0.1:1").unwrap();
    match client.complete(&payload("x")).await {
        Err(RpcError::BackendError(m)) => assert!(m.contains("unreachable"), "{m}"),
        other => panic!("{other:?}"),
    }
    assert!(!client.is_reachable().await);
}
