//! Чат Помощницы на родном ядре: `POST /api/local/chat` проводит ЦЕЛЫЙ ход через собственный шлюз узла. Вместо настоящей модели — локальный «сервис» со сценарием ответов,
//! который записывает все запросы: по ним видно, что извлечение событий видит только текущее сообщение, а память об отношениях идёт только в ответ. Меняет переменные окружения → `-- --test-threads=1`.
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

use axum::{extract::State, routing::post, Json, Router};
use serde_json::{json, Value};

struct Script {
    answers: Mutex<VecDeque<String>>,
    seen: Mutex<Vec<Value>>,
}

async fn fake_service(State(s): State<Arc<Script>>, Json(body): Json<Value>) -> Json<Value> {
    s.seen.lock().unwrap().push(body);
    let content = s.answers.lock().unwrap().pop_front().unwrap_or_default();
    Json(json!({"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {"completion_tokens": 3}}))
}

async fn serve(app: Router) -> String {
    let l = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let p = l.local_addr().unwrap().port();
    tokio::spawn(async move { axum::serve(l, app).await.unwrap() });
    format!("http://127.0.0.1:{p}")
}

fn all_text(body: &Value) -> String {
    body["messages"].as_array().map(|m| m.iter().map(|x| x["content"].as_str().unwrap_or("").to_string()).collect::<Vec<_>>().join("\n")).unwrap_or_default()
}

#[tokio::test(flavor = "multi_thread")]
async fn whole_turn_runs_through_native_core_and_gateway() {
    let dir = std::env::temp_dir().join(format!("yandi-pet-chat-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    std::env::set_var("YANDI_KEK_PATH", dir.join("keys/kek.bin"));
    std::env::set_var("YANDI_NODE_DB", dir.join("node.sqlite"));
    std::env::set_var("XDG_DATA_HOME", dir.join("data"));
    std::env::set_var("YANDI_DB", dir.join("yandi.sqlite"));
    std::env::remove_var("YANDI_EXTRACTION_MODEL");
    std::env::remove_var("YANDI_VERIFIER_MODEL");

    // главный ключ узла (после входа): личные слова должны лечь в базу запечатанными
    yandi::web::pet_chat::set_master_key_provider(Box::new(|| Some([7u8; 32])));

    let script = Arc::new(Script { answers: Mutex::new(VecDeque::new()), seen: Mutex::new(vec![]) });
    let svc = serve(Router::new().route("/v1/chat/completions", post(fake_service)).with_state(script.clone())).await;
    let base = serve(yandi::web::ai_api::router::<()>().merge(yandi::web::pet_chat::router::<()>())).await;
    let http = reqwest::Client::new();
    let post_json = |p: &str, b: Value| http.post(format!("{base}{p}")).json(&b).send();

    let r = post_json("/api/ai/models", json!({"name": "Голос", "kind": "remote", "base_url": format!("{svc}/v1"), "model": "srv"})).await.unwrap();
    assert_eq!(r.status(), 200);

    // ход 1: оскорбление с номером реплики: события → проверка → факты → ответ
    let turn = "turn-000000a1";
    for a in [
        json!({"events": [{"span": [1, 2], "type": "insult", "severity": 0.7}]}).to_string(),
        json!({"act": "insult", "frame": "current"}).to_string(),
        json!({"facts": [], "memory_query": "none"}).to_string(),
        json!({"reply": "Мне это неприятно.", "state": {}}).to_string(),
    ] {
        script.answers.lock().unwrap().push_back(a);
    }
    let r: Value = post_json("/api/local/chat", json!({"model": "Голос", "turn_id": turn, "messages": [{"role": "user", "content": "Ты тупая железка"}]})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(true), "{r}");
    assert_eq!(r["content"], json!("Мне это неприятно."));
    assert_eq!(r["model_used"], json!("Голос"));
    {
        let seen = script.seen.lock().unwrap();
        assert_eq!(seen.len(), 4, "события, проверка, факты, ответ");
        // извлечение событий видит только текущее сообщение
        assert!(all_text(&seen[0]).contains("Ты тупая железка"));
        assert!(!all_text(&seen[0]).contains("Память об отношениях"));
        // ответ получает характер и память об отношениях (на этот момент обид ещё нет)
        let reply_req = all_text(&seen[3]);
        assert!(reply_req.contains("открытых обид на пользователя нет"), "{reply_req}");
        assert!(reply_req.contains("твой код открыт здесь"), "«кто Я» из модели себя");
    }
    // ход записан одной транзакцией: реплика + обида
    let db = yandi_db::Db::open(&dir.join("yandi.sqlite")).unwrap();
    let stored: String = db.conn().query_row("SELECT user_text FROM interaction_turn WHERE source_turn_id=?", [turn], |r| r.get(0)).unwrap();
    assert!(stored.starts_with("yp1:") && !stored.contains("тупая"), "слова человека лежат в базе запечатанными: {stored}");
    let g_text: String = db.conn().query_row("SELECT description FROM grievance", [], |r| r.get(0)).unwrap();
    assert!(g_text.starts_with("yp1:"), "описание обиды запечатано");
    let n: i64 = db.conn().query_row("SELECT COUNT(*) FROM interaction_turn WHERE source_turn_id=?", [turn], |r| r.get(0)).unwrap();
    assert_eq!(n, 1);
    let g: i64 = db.conn().query_row("SELECT COUNT(*) FROM grievance", [], |r| r.get(0)).unwrap();
    assert_eq!(g, 1);

    // ход 2: следующее сообщение — теперь ответ ЗНАЕТ про обиду (как факт из памяти, не как новое событие)
    script.seen.lock().unwrap().clear();
    for a in [json!({"events": []}).to_string(), json!({"facts": [], "memory_query": "none"}).to_string(), json!({"reply": "Слушаю.", "state": {}}).to_string()] {
        script.answers.lock().unwrap().push_back(a);
    }
    let r: Value = post_json("/api/local/chat", json!({"model": "Голос", "turn_id": "turn-000000a2", "messages": [{"role": "user", "content": "Ты тупая железка"}, {"role": "assistant", "content": "Мне это неприятно."}, {"role": "user", "content": "Привет"}]})).await.unwrap().json().await.unwrap();
    assert_eq!(r["content"], json!("Слушаю."), "{r}");
    {
        let seen = script.seen.lock().unwrap();
        assert!(!all_text(&seen[0]).contains("тупая"), "в извлечение событий уходит только последнее сообщение");
        let reply_req = all_text(seen.last().unwrap());
        assert!(reply_req.contains("Историческая память об отношениях"), "{reply_req}");
    }

    // повтор той же доставки ничего не применяет второй раз
    for a in [json!({"events": [{"span": [1, 2], "type": "insult", "severity": 0.7}]}).to_string(), json!({"act": "insult", "frame": "current"}).to_string(), json!({"facts": [], "memory_query": "none"}).to_string(), json!({"reply": "Опять.", "state": {}}).to_string()] {
        script.answers.lock().unwrap().push_back(a);
    }
    post_json("/api/local/chat", json!({"model": "Голос", "turn_id": turn, "messages": [{"role": "user", "content": "Ты тупая железка"}]})).await.unwrap();
    let g: i64 = db.conn().query_row("SELECT COUNT(*) FROM grievance", [], |r| r.get(0)).unwrap();
    assert_eq!(g, 1, "повтор доставки не создаёт вторую обиду");

    // без номера реплики личная память не читается и не пишется
    let before: i64 = db.conn().query_row("SELECT COUNT(*) FROM interaction_turn", [], |r| r.get(0)).unwrap();
    for a in [json!({"events": []}).to_string(), json!({"reply": "Без номера.", "state": {}}).to_string()] {
        script.answers.lock().unwrap().push_back(a);
    }
    let r: Value = post_json("/api/local/chat", json!({"model": "Голос", "messages": [{"role": "user", "content": "Привет"}]})).await.unwrap().json().await.unwrap();
    assert_eq!(r["content"], json!("Без номера."), "{r}");
    let after: i64 = db.conn().query_row("SELECT COUNT(*) FROM interaction_turn", [], |r| r.get(0)).unwrap();
    assert_eq!(before, after);

    // ошибки понятным языком
    let r: Value = post_json("/api/local/chat", json!({"model": "Голос", "messages": []})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(false));
    let r: Value = post_json("/api/local/chat", json!({"model": "нет-такой", "messages": [{"role": "user", "content": "x"}]})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(false), "{r}");
    assert!(r["content"].as_str().unwrap().starts_with("❌"));

    // история окна разговора: добавить, прочитать, стереть; чужой вид отвергается
    let r: Value = post_json("/api/local/clear", json!({})).await.unwrap().json().await.unwrap();
    assert_eq!(r["ok"], json!(true));
    for m in [json!({"role": "user", "content": "привет"}), json!({"role": "assistant", "content": "здравствуй"})] {
        let r: Value = post_json("/api/local/message", m).await.unwrap().json().await.unwrap();
        assert_eq!(r["ok"], json!(true));
    }
    let bad: Value = post_json("/api/local/message", json!([1, 2])).await.unwrap().json().await.unwrap();
    assert_eq!(bad["ok"], json!(false));
    let h: Value = http.get(format!("{base}/api/local/history")).send().await.unwrap().json().await.unwrap();
    assert_eq!(h["messages"], json!([{"role": "user", "content": "привет"}, {"role": "assistant", "content": "здравствуй"}]));
    for i in 0..305 {
        post_json("/api/local/message", json!({"role": "user", "content": format!("м{i}")})).await.unwrap();
    }
    let h: Value = http.get(format!("{base}/api/local/history")).send().await.unwrap().json().await.unwrap();
    assert_eq!(h["messages"].as_array().unwrap().len(), 300, "хранится не больше 300 последних");
    assert_eq!(h["messages"][299]["content"], json!("м304"));
    post_json("/api/local/clear", json!({})).await.unwrap();
    let h: Value = http.get(format!("{base}/api/local/history")).send().await.unwrap().json().await.unwrap();
    assert_eq!(h["messages"], json!([]));
}

#[test]
fn assistant_page_is_self_contained_and_linked_from_navigation() {
    let html = include_str!("../src/web/ui/assistant.html");
    assert!(!html.contains("<script src=\"http") && !html.contains("<link rel=\"stylesheet\" href=\"http"), "никаких внешних скриптов и стилей");
    assert!(html.contains("/api/local/chat") && html.contains("/api/local/history"));
    for page in ["index", "chat", "settings", "groups", "ai", "gateways", "relay", "group-chat"] {
        let t = std::fs::read_to_string(format!("{}/src/web/ui/{page}.html", env!("CARGO_MANIFEST_DIR"))).unwrap();
        assert!(t.contains("href=\"/assistant\""), "{page}: нет ссылки на «Помощницу»");
    }
}
