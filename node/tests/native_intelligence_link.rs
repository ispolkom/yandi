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

/// Ядро на Python ходит в тот же движок узла через локальный HTTP `/api/gateway/call` (настоящий axum-маршрут узла).
#[tokio::test(flavor = "multi_thread")]
async fn gateway_route_serves_complete_embed_and_errors() {
    let dir = std::env::temp_dir().join(format!("yandi-gw-route-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("YANDI_KEK_PATH", dir.join("keys/kek.bin"));
    std::env::set_var("YANDI_NODE_DB", dir.join("node.sqlite"));
    std::env::set_var("YANDI_LLAMA_SERVER", concat!(env!("CARGO_MANIFEST_DIR"), "/../rustlib/yandi_llm/tests/fake_llama_server.py"));
    let gguf = dir.join("voice.gguf");
    std::fs::write(&gguf, b"x").unwrap();
    yandi_llm::secure_store::set_model_entry("yandi-voice", &serde_json::json!({"backend": "llamacpp", "path": gguf.to_string_lossy()})).unwrap();

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    tokio::spawn(async move { axum::serve(listener, yandi::web::gateway_router::<()>()).await.unwrap() });
    let http = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{port}/api/gateway/call");
    let call = |name: &str, args: serde_json::Value| {
        let (http, url, name) = (http.clone(), url.clone(), name.to_string());
        async move {
            let r = http.post(url).json(&serde_json::json!({"name": name, "args": args})).send().await.unwrap();
            (r.status().as_u16(), r.json::<serde_json::Value>().await.unwrap())
        }
    };
    let base = yandi_llm::client::DEFAULT_BASE_URL;

    let (st, r) = call("gw_complete", serde_json::json!({"prompt": "вопрос", "model": "yandi-voice", "base_url": base, "temperature": 0.25})).await;
    assert_eq!(st, 200, "{r}");
    let echoed: serde_json::Value = serde_json::from_str(r["ok"]["text"].as_str().unwrap()).unwrap();
    assert_eq!(echoed["messages"], serde_json::json!([{"role": "user", "content": "вопрос"}]));
    assert_eq!(echoed["temperature"], serde_json::json!(0.25));
    assert_eq!(r["ok"]["raw"]["_llm_gateway_trace"][0]["adapter_id"], serde_json::json!("llama_cpp"));

    let (st, r) = call("gw_complete_meta", serde_json::json!({"messages": [{"role": "user", "content": "x"}], "model": "yandi-voice", "base_url": base, "max_tokens": 1})).await;
    assert_eq!((st, &r["ok"]["truncated"], &r["ok"]["token_count"]), (200, &serde_json::json!(true), &serde_json::json!(7)), "{r}");

    // эмбеддинги: у подделки llamacpp-embed идёт отдельным процессом с --embedding
    let (st, r) = call("gw_embed", serde_json::json!({"texts": ["abc"], "model": "yandi-voice", "base_url": base})).await;
    assert_eq!(st, 200, "{r}");
    assert_eq!(r["ok"]["vectors"], serde_json::json!([[3.0, 0.5, 0.0]]));

    // не настроенная модель — честная ошибка шлюза (класс LLMError), а не Ollama
    let (st, r) = call("gw_complete", serde_json::json!({"prompt": "x", "model": "unknown-model", "base_url": base})).await;
    assert_eq!(st, 200);
    assert_eq!(r["error"]["class"], serde_json::json!("LLMError"));
    assert!(r["error"]["msg"].as_str().unwrap().contains("не настроен ни один backend"), "{r}");
    // неизвестная функция и неверные аргументы — 400
    let (st, r) = call("store_set", serde_json::json!({})).await;
    assert_eq!((st, r["error"]["class"].as_str()), (400, Some("BadRequest")));
    let (st, _) = call("gw_complete_semantic", serde_json::json!({"prompt": "x", "model": "m", "base_url": base})).await;
    assert_eq!(st, 400);
    let _ = std::fs::remove_dir_all(&dir);
}
