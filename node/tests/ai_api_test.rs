//! Раздел «ИИ» веб-интерфейса: добавление модели человеком, список без секретов, «Голос», проверка, разговор, «Обзор». Вместо настоящего llama-server — подделка;
//! «сервис с ключом» — локальный сервер, записывающий заголовки. Меняет переменные окружения процесса → `-- --test-threads=1`.
use std::sync::{Arc, Mutex};

use axum::{extract::State, http::HeaderMap, routing::post, Json, Router};
use serde_json::{json, Value};

struct Rec(Mutex<Vec<(String, Value)>>);

async fn fake_service(State(r): State<Arc<Rec>>, headers: HeaderMap, Json(body): Json<Value>) -> Json<Value> {
    let auth = headers.get("authorization").and_then(|v| v.to_str().ok()).unwrap_or("").to_string();
    r.0.lock().unwrap().push((auth, body));
    Json(json!({"choices": [{"message": {"content": "ответ сервиса"}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}}))
}

async fn serve(app: Router) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let p = l.local_addr().unwrap().port();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    format!("http://127.0.0.1:{p}")
}

#[tokio::test(flavor = "multi_thread")]
async fn person_adds_models_chooses_voice_and_talks() {
    let dir = std::env::temp_dir().join(format!("yandi-ai-api-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(dir.join("models/sub")).unwrap();
    std::env::set_var("YANDI_KEK_PATH", dir.join("keys/kek.bin"));
    std::env::set_var("YANDI_NODE_DB", dir.join("node.sqlite"));
    std::env::set_var("XDG_DATA_HOME", dir.join("data"));
    std::env::set_var("YANDI_LLAMA_SERVER", concat!(env!("CARGO_MANIFEST_DIR"), "/../rustlib/yandi_llm/tests/fake_llama_server.py"));
    std::env::remove_var("YANDI_INTELLIGENCE_ENGINE");
    let gguf = dir.join("models/мой.gguf");
    std::fs::write(&gguf, b"x").unwrap();
    std::fs::write(dir.join("models/notes.txt"), b"x").unwrap();
    std::fs::write(dir.join("models/.hidden.gguf"), b"x").unwrap();

    let rec = Arc::new(Rec(Mutex::new(vec![])));
    let svc = serve(Router::new().route("/v1/chat/completions", post(fake_service)).with_state(rec.clone())).await;
    let base = serve(yandi::web::ai_api::router::<()>()).await;
    let http = reqwest::Client::new();
    let get = |p: &str| http.get(format!("{base}{p}")).send();
    let post_json = |p: &str, b: Value| http.post(format!("{base}{p}")).json(&b).send();

    // пусто
    let s: Value = get("/api/ai/state").await.unwrap().json().await.unwrap();
    assert_eq!(s["models"], json!([]));
    assert_eq!(s["engine"]["ready"], json!(true));

    // ошибки понятным языком
    let r = post_json("/api/ai/models", json!({"name": "", "kind": "local", "path": "x"})).await.unwrap();
    assert_eq!(r.status(), 400);
    let r = post_json("/api/ai/models", json!({"name": "м", "kind": "local", "path": "/нет/такого.gguf"})).await.unwrap();
    assert_eq!(r.status(), 400);
    assert!(r.json::<Value>().await.unwrap()["error"].as_str().unwrap().contains("Файл модели не найден"));
    let r = post_json("/api/ai/models", json!({"name": "с", "kind": "service", "service": "openai", "model": "m", "api_key": ""})).await.unwrap();
    assert!(r.json::<Value>().await.unwrap()["error"].as_str().unwrap().contains("нужен ключ"));
    let r = post_json("/api/ai/models", json!({"name": "с", "kind": "remote", "base_url": "ftp://x", "model": "m"})).await.unwrap();
    assert_eq!(r.status(), 400);

    // локальная модель, свой сервер (с ключом — как «сервис»)
    let r = post_json("/api/ai/models", json!({"name": "Голосовая", "kind": "local", "path": gguf.to_string_lossy()})).await.unwrap();
    assert_eq!(r.status(), 200, "{:?}", r.text().await);
    let r = post_json("/api/ai/models", json!({"name": "Сервер", "kind": "remote", "base_url": format!("{svc}/v1"), "model": "srv-model", "api_key": "sk-SECRET-123"})).await.unwrap();
    assert_eq!(r.status(), 200);

    let raw = get("/api/ai/state").await.unwrap().text().await.unwrap();
    assert!(!raw.contains("sk-SECRET-123"), "ключ не должен уходить в браузер: {raw}");
    let s: Value = serde_json::from_str(&raw).unwrap();
    let names: Vec<&str> = s["models"].as_array().unwrap().iter().map(|m| m["name"].as_str().unwrap()).collect();
    assert_eq!(names, vec!["Голосовая", "Сервер"]);
    assert_eq!(s["models"][1]["kind"], json!("service"));
    assert_eq!(s["models"][1]["has_key"], json!(true));
    assert_eq!(s["default"], json!("Голосовая"), "по умолчанию — первая");

    // «Голос»
    assert_eq!(post_json("/api/ai/default", json!({"name": "Сервер"})).await.unwrap().status(), 200);
    assert_eq!(post_json("/api/ai/default", json!({"name": "нет"})).await.unwrap().status(), 400);
    let s: Value = get("/api/ai/state").await.unwrap().json().await.unwrap();
    assert_eq!(s["default"], json!("Сервер"));

    // разговор с сервисом: ключ ушёл в заголовок, модель — как указана человеком
    let r: Value = post_json("/api/ai/chat", json!({"messages": [{"role": "user", "content": "привет"}]})).await.unwrap().json().await.unwrap();
    assert_eq!(r["text"], json!("ответ сервиса"));
    {
        let seen = rec.0.lock().unwrap();
        assert_eq!(seen.len(), 1);
        assert_eq!(seen[0].0, "Bearer sk-SECRET-123");
        assert_eq!(seen[0].1["model"], json!("srv-model"));
        assert_eq!(seen[0].1["messages"], json!([{"role": "user", "content": "привет"}]));
    }
    // правка без ввода ключа сохраняет прежний
    let r = post_json("/api/ai/models", json!({"name": "Сервер", "kind": "remote", "base_url": format!("{svc}/v1"), "model": "srv-model-2", "api_key": ""})).await.unwrap();
    assert_eq!(r.status(), 200);
    post_json("/api/ai/chat", json!({"messages": [{"role": "user", "content": "ещё"}]})).await.unwrap();
    assert_eq!(rec.0.lock().unwrap()[1].0, "Bearer sk-SECRET-123");
    assert_eq!(rec.0.lock().unwrap()[1].1["model"], json!("srv-model-2"));

    // разговор с локальной моделью через собственный движок (подделка llama-server)
    let r: Value = post_json("/api/ai/chat", json!({"model": "Голосовая", "messages": [{"role": "user", "content": "вопрос локальной"}]})).await.unwrap().json().await.unwrap();
    let echoed: Value = serde_json::from_str(r["text"].as_str().unwrap()).unwrap();
    assert_eq!(echoed["messages"][0]["content"], json!("вопрос локальной"));
    assert_eq!(post_json("/api/ai/chat", json!({"messages": []})).await.unwrap().status(), 400);

    // проверка
    let r: Value = post_json("/api/ai/check", json!({"name": "Голосовая"})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(true));
    assert!(r["message"].as_str().unwrap().contains("Файл модели на месте"));
    let r: Value = post_json("/api/ai/check", json!({"name": "Голосовая", "deep": true})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(true), "{r}");
    let r: Value = post_json("/api/ai/check", json!({"name": "Сервер", "deep": true})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(true));
    std::fs::remove_file(&gguf).unwrap();
    let r: Value = post_json("/api/ai/check", json!({"name": "Голосовая"})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(false));
    let s: Value = get("/api/ai/state").await.unwrap().json().await.unwrap();
    assert_eq!(s["models"][0]["problem"], json!("Файл модели не найден"));

    // модель без настройки: понятная ошибка, без Ollama
    let r = post_json("/api/ai/chat", json!({"model": "нету", "messages": [{"role": "user", "content": "x"}]})).await.unwrap();
    assert_eq!(r.status(), 502);
    assert!(r.json::<Value>().await.unwrap()["error"].as_str().unwrap().contains("нет настройки"));

    // обзор: папки и .gguf, без скрытых и посторонних
    let b: Value = get(&format!("/api/ai/browse?path={}", dir.join("models").to_string_lossy())).await.unwrap().json().await.unwrap();
    let ents: Vec<String> = b["entries"].as_array().unwrap().iter().map(|e| format!("{}:{}", e["kind"].as_str().unwrap(), e["name"].as_str().unwrap())).collect();
    assert_eq!(ents, vec!["dir:sub"], "мой.gguf удалён выше; notes.txt и .hidden.gguf не показываются: {ents:?}");
    assert!(b["parent"].is_string());
    assert_eq!(get("/api/ai/browse?path=/нет/такого").await.unwrap().status(), 400);

    // удаление
    let r: Value = http.delete(format!("{base}/api/ai/models/Сервер")).send().await.unwrap().json().await.unwrap();
    assert_eq!(r["removed"], json!(true));
    let s: Value = get("/api/ai/state").await.unwrap().json().await.unwrap();
    assert_eq!(s["models"].as_array().unwrap().len(), 1);
    assert_eq!(s["default"], json!("Голосовая"), "Голос переехал на оставшуюся модель");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn page_is_self_contained_and_linked_from_navigation() {
    let html = include_str!("../src/web/ui/ai.html");
    assert!(!html.contains("http://") && !html.contains("https://") || !html.contains("<script src=\"http"), "никаких внешних скриптов");
    for page in ["index", "chat", "settings", "groups"] {
        let t = std::fs::read_to_string(format!("{}/src/web/ui/{page}.html", env!("CARGO_MANIFEST_DIR"))).unwrap();
        assert!(t.contains("href=\"/ai\""), "{page}: нет ссылки на раздел ИИ");
    }
}
