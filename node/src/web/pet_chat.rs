//! Личный чат Помощницы на РОДНОМ ядре Rust: `POST /api/local/chat` (тот же обмен, что был у Python-сервера PET — `{model, temperature, messages, turn_id}` → `{ok, content, model_used}`).
//! Весь ход (память об отношениях, личная память, извлечение событий и фактов, ответ, одна транзакция записи) выполняет `yandi_core::chat_turn`; модели вызываются через собственный шлюз узла
//! (тот же движок и те же настройки владельца, что у раздела «ИИ»). Маршрут монтируется ПОД проверкой входа.
use std::sync::{Mutex, OnceLock};

use axum::{response::{IntoResponse, Json}, routing::post, Router};
use serde_json::{json, Value};

use yandi_core::chat_turn as ct;
use yandi_core::{chat_prompts, self_model, Ctx};

const EXTRACTION_MODEL_ENV: &str = "YANDI_EXTRACTION_MODEL";
const VERIFIER_MODEL_ENV: &str = "YANDI_VERIFIER_MODEL";
const EXTRACTION_TIMEOUT_S: u64 = 60;

static DB: OnceLock<Result<Mutex<yandi_db::Db>, String>> = OnceLock::new();

/// Общая база пользователя (файл в каталоге данных; переживает переустановку).
fn db() -> Result<&'static Mutex<yandi_db::Db>, String> {
    DB.get_or_init(|| yandi_db::Db::open(&yandi_db::default_db_path()).map(Mutex::new).map_err(|e| format!("не удалось открыть базу: {}", e.0)))
        .as_ref()
        .map_err(|e| e.clone())
}

/// Какая логическая модель отвечает на СТРУКТУРНЫЕ вызовы: указанная владельцем через окружение, иначе сама модель чата. Скрытой подмены нет.
fn structured_target(chat_model: &str, envs: &[&str]) -> String {
    for name in envs {
        if let Ok(v) = std::env::var(name) {
            if !v.trim().is_empty() {
                return v.trim().to_string();
            }
        }
    }
    chat_model.to_string()
}

/// Детерминированный (температура 0) вызов модели в режиме JSON; ошибка — класс сбоя (протоколы трактуют её как «ничего не извлечено / не проверено»).
fn structured_call(target: &str, msgs: &[(String, String)]) -> Result<String, String> {
    let messages: Vec<Value> = msgs.iter().map(|(r, c)| json!({"role": r, "content": c})).collect();
    let args = json!({"model": target, "messages": messages, "base_url": yandi_llm::client::DEFAULT_BASE_URL, "temperature": 0.0, "max_tokens": 500, "response_format": "json", "timeout": EXTRACTION_TIMEOUT_S});
    let out = crate::ai_rpc::intelligence_bridge::gateway_call_blocking("gw_complete", &args).map_err(|_| "LLMError".to_string())?;
    match out.get("ok") {
        Some(ok) => Ok(ok["text"].as_str().unwrap_or("").to_string()),
        None => Err(out["error"]["class"].as_str().unwrap_or("LLMError").to_string()),
    }
}

/// Смысловой вызов: ответ и след попыток шлюза.
fn semantic_call(model: &str, system: &[Option<String>], messages: &[Value], temperature: f64) -> Result<ct::Semantic, String> {
    let args = json!({
        "model": model, "system": system, "messages": messages, "temperature": temperature, "stop": chat_prompts::stop_tokens(),
        "extra_options": {"repeat_penalty": 1.3, "repeat_last_n": 64}, "base_url": yandi_llm::client::DEFAULT_BASE_URL,
        "requirement": {"kind": "reply_state", "state_schema": {"type": "object", "properties": {}}, "reply_required": true, "state_required": false},
    });
    let out = crate::ai_rpc::intelligence_bridge::gateway_call_blocking("gw_complete_semantic", &args)?;
    match out.get("ok") {
        Some(ok) => Ok(ct::Semantic {
            reply: ok["reply"].as_str().unwrap_or("").to_string(),
            reply_ok: ok["reply_ok"].as_bool().unwrap_or(false),
            trace: ok["metadata"]["_llm_gateway_trace"].as_array().cloned().unwrap_or_default(),
        }),
        None => Err(out["error"]["msg"].as_str().unwrap_or("модель не ответила").to_string()),
    }
}

/// Один ход в блокирующем потоке (база и модель синхронные).
fn run_turn(model: String, messages: Vec<Value>, temperature: f64, turn_id: Option<String>) -> Result<String, String> {
    let guard = db()?.lock().map_err(|_| "база занята другим сбоем".to_string())?;
    let cx = Ctx::new(guard.conn());
    // «Кто Я» (метаданные характера); недоступно — просто без этого сообщения, ответ от него не зависит
    let character: Option<Value> = self_model::init(&cx).ok().and_then(|_| self_model::row(&cx).ok()).map(|r| r["metadata"].get("character").cloned().unwrap_or_else(|| json!({})));
    let extract_target = structured_target(&model, &[EXTRACTION_MODEL_ENV]);
    let verify_target = structured_target(&model, &[VERIFIER_MODEL_ENV, EXTRACTION_MODEL_ENV]);
    let extract = |m: &[(String, String)]| structured_call(&extract_target, m);
    let verify = |m: &[(String, String)]| structured_call(&verify_target, m);
    let semantic = |system: &[Option<String>], msgs: &[Value], t: f64| semantic_call(&model, system, msgs, t);
    let models = ct::Models { extract: &extract, verify: &verify, semantic: &semantic };
    let inp = ct::TurnInput { model: &model, messages: &messages, temperature, source_turn_id: turn_id.as_deref(), verified: None, character: character.as_ref() };
    ct::respond_with_character(&cx, &inp, &models).map(|o| o.reply)
}

async fn handle_chat(Json(p): Json<Value>) -> impl IntoResponse {
    let messages = p["messages"].as_array().cloned().unwrap_or_default();
    if messages.is_empty() {
        return Json(json!({"ok": false, "error": "empty messages"}));
    }
    let model = p["model"].as_str().map(|m| m.trim().to_string()).filter(|m| !m.is_empty()).or_else(super::ai_api::read_default);
    let Some(model) = model else {
        return Json(json!({"ok": false, "error": "Сначала выберите модель.", "content": "❌ Сначала выберите модель."}));
    };
    let temperature = p["temperature"].as_f64().unwrap_or(0.7);
    let turn_id = ct::valid_turn_id(p.get("turn_id"));
    let used = model.clone();
    match tokio::task::spawn_blocking(move || run_turn(model, messages, temperature, turn_id)).await {
        Ok(Ok(content)) => Json(json!({"ok": true, "content": content, "model_used": used})),
        Ok(Err(e)) => Json(json!({"ok": false, "error": e, "content": format!("❌ {e}")})),
        Err(e) => Json(json!({"ok": false, "error": e.to_string(), "content": format!("❌ {e}")})),
    }
}

pub fn router<S: Clone + Send + Sync + 'static>() -> Router<S> {
    Router::new().route("/api/local/chat", post(handle_chat).layer(axum::extract::DefaultBodyLimit::max(8 * 1024 * 1024)))
}
