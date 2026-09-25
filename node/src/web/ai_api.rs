//! Раздел «ИИ» веб-интерфейса узла: выбор модели человеком и разговор с ней. Настройки моделей хранятся в зашифрованном хранилище узла (`yandi_llm::secure_store`,
//! ключи сервисов — там же, наружу не отдаются никогда), ответы даёт собственный движок узла или выбранный человеком сервер/сервис. Маршруты монтируются ПОД проверкой входа.
use std::path::{Path, PathBuf};

use axum::{
    extract::{Path as UrlPath, Query},
    http::StatusCode,
    response::{IntoResponse, Json},
    routing::{delete, get, post},
    Router,
};
use serde::Deserialize;
use serde_json::{json, Map, Value};

use yandi_llm::secure_store;

const MAX_NAME: usize = 64;
const MAX_BROWSE: usize = 500;

fn bad(msg: impl Into<String>) -> (StatusCode, Json<Value>) {
    (StatusCode::BAD_REQUEST, Json(json!({"error": msg.into()})))
}

fn ui_state_path() -> PathBuf {
    yandi_db::data_dir().join("ai_ui.json")
}

pub(crate) fn read_default() -> Option<String> {
    let t = std::fs::read_to_string(ui_state_path()).ok()?;
    serde_json::from_str::<Value>(&t).ok()?.get("default")?.as_str().map(String::from)
}

fn write_default(name: Option<&str>) -> Result<(), String> {
    let p = ui_state_path();
    if let Some(dir) = p.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    std::fs::write(p, json!({"default": name}).to_string()).map_err(|e| e.to_string())
}

/// Сервисы с готовыми адресами: человеку достаточно выбрать из списка и вставить ключ.
fn service_preset(id: &str) -> Option<(&'static str, &'static str)> {
    Some(match id {
        "openai" => ("https://api.openai.com/v1", "openai"),
        "anthropic" => ("https://api.anthropic.com/v1", "anthropic"),
        "deepseek" => ("https://api.deepseek.com/v1", "openai"),
        "openrouter" => ("https://openrouter.ai/api/v1", "openai"),
        _ => return None,
    })
}

fn valid_name(n: &str) -> Result<(), String> {
    if n.trim().is_empty() || n.chars().count() > MAX_NAME || n.chars().any(|c| c.is_control()) {
        return Err(format!("Имя модели: от 1 до {MAX_NAME} символов, без служебных знаков."));
    }
    Ok(())
}

/// Строка описания для списка — БЕЗ секретов.
fn describe(name: &str, e: &Value, default: &Option<String>) -> Value {
    let backend = e.get("backend").and_then(|v| v.as_str()).unwrap_or("");
    let has_key = e.get("api_key").and_then(|v| v.as_str()).map(|k| !k.is_empty()).unwrap_or(false) || e.get("api_key_env").is_some();
    let (kind, summary, problem) = match backend {
        "llamacpp" => {
            let path = e.get("path").and_then(|v| v.as_str()).unwrap_or("");
            let exists = Path::new(path).is_file();
            ("local", path.to_string(), if exists { Value::Null } else { json!("Файл модели не найден") })
        }
        "remote" => {
            let base = e.get("base_url").and_then(|v| v.as_str()).unwrap_or("");
            let model = e.get("model").and_then(|v| v.as_str()).unwrap_or("");
            (if has_key { "service" } else { "remote" }, format!("{base} · {model}"), Value::Null)
        }
        other => ("other", other.to_string(), json!("Неизвестный тип настройки")),
    };
    json!({"name": name, "kind": kind, "summary": summary, "has_key": has_key, "problem": problem, "is_default": default.as_deref() == Some(name)})
}

async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> Result<T, (StatusCode, Json<Value>)> {
    tokio::task::spawn_blocking(f).await.map_err(|e| (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": format!("внутренняя ошибка: {e}")}))))
}

async fn handle_state() -> impl IntoResponse {
    let listed = blocking(|| secure_store::list_models().map_err(|e| e.message().to_string())).await;
    let models = match listed {
        Ok(Ok(m)) => m,
        Ok(Err(e)) => return (StatusCode::INTERNAL_SERVER_ERROR, Json(json!({"error": format!("Не удалось прочитать настройки моделей: {e}")}))),
        Err(e) => return e,
    };
    let mut default = read_default();
    if default.as_ref().map(|d| !models.contains_key(d)).unwrap_or(true) {
        default = models.keys().find(|k| !k.starts_with("yandi:")).cloned();
    }
    let list: Vec<Value> = models.iter().map(|(n, e)| describe(n, e, &default)).collect();
    let engine = crate::ai_rpc::intelligence_bridge::engine_problem();
    (StatusCode::OK, Json(json!({"models": list, "default": default, "engine": {"ready": engine.is_none(), "problem": engine}})))
}

#[derive(Deserialize)]
struct ModelBody {
    name: String,
    kind: String,
    path: Option<String>,
    n_ctx: Option<i64>,
    n_gpu_layers: Option<i64>,
    base_url: Option<String>,
    model: Option<String>,
    protocol: Option<String>,
    service: Option<String>,
    api_key: Option<String>,
}

fn build_entry(b: &ModelBody, previous: Option<&Value>) -> Result<Value, String> {
    let mut e = Map::new();
    match b.kind.as_str() {
        "local" => {
            let path = b.path.as_deref().unwrap_or("").trim();
            if path.is_empty() {
                return Err("Укажите файл модели (.gguf).".into());
            }
            if !Path::new(path).is_file() {
                return Err("Файл модели не найден. Проверьте путь или выберите файл кнопкой «Обзор».".into());
            }
            e.insert("backend".into(), json!("llamacpp"));
            e.insert("path".into(), json!(path));
            if let Some(n) = b.n_ctx.filter(|n| *n > 0) {
                e.insert("n_ctx".into(), json!(n));
            }
            if let Some(n) = b.n_gpu_layers {
                e.insert("n_gpu_layers".into(), json!(n));
            }
        }
        "remote" | "service" => {
            let (base, protocol) = if b.kind == "service" {
                let id = b.service.as_deref().unwrap_or("");
                let (u, p) = service_preset(id).ok_or("Выберите сервис из списка.")?;
                (u.to_string(), p.to_string())
            } else {
                (b.base_url.as_deref().unwrap_or("").trim().to_string(), b.protocol.clone().filter(|p| p == "anthropic").unwrap_or_else(|| "openai".into()))
            };
            if !(base.starts_with("http://") || base.starts_with("https://")) {
                return Err("Адрес сервера должен начинаться с http:// или https://".into());
            }
            let model = b.model.as_deref().unwrap_or("").trim();
            if model.is_empty() {
                return Err("Укажите название модели на этом сервере или сервисе.".into());
            }
            e.insert("backend".into(), json!("remote"));
            e.insert("protocol".into(), json!(protocol));
            e.insert("base_url".into(), json!(base));
            e.insert("model".into(), json!(model));
            // пустое поле ключа при правке = оставить прежний
            let key = b.api_key.as_deref().map(str::trim).filter(|k| !k.is_empty()).map(String::from).or_else(|| previous.and_then(|p| p.get("api_key")).and_then(|k| k.as_str()).map(String::from));
            if let Some(k) = key {
                e.insert("api_key".into(), json!(k));
            } else if b.kind == "service" {
                return Err("Для сервиса нужен ключ доступа.".into());
            }
        }
        _ => return Err("Неизвестный тип модели.".into()),
    }
    Ok(Value::Object(e))
}

async fn handle_save(Json(b): Json<ModelBody>) -> impl IntoResponse {
    if let Err(m) = valid_name(&b.name) {
        return bad(m);
    }
    let name = b.name.trim().to_string();
    let r = blocking(move || -> Result<(), String> {
        let previous = secure_store::get_model_entry(&name).map_err(|e| e.message().to_string())?;
        let entry = build_entry(&b, previous.as_ref())?;
        // хранилище «вставить или стереть, но не изменить»: правка = стереть старую запись и вставить новую
        if previous.is_some() {
            secure_store::remove_model_entry(&name).map_err(|e| e.message().to_string())?;
        }
        secure_store::set_model_entry(&name, &entry).map_err(|e| e.message().to_string())
    })
    .await;
    match r {
        Ok(Ok(())) => (StatusCode::OK, Json(json!({"ok": true}))),
        Ok(Err(m)) => bad(m),
        Err(e) => e,
    }
}

async fn handle_delete(UrlPath(name): UrlPath<String>) -> impl IntoResponse {
    let n = name.clone();
    match blocking(move || secure_store::remove_model_entry(&n).map_err(|e| e.message().to_string())).await {
        Ok(Ok(found)) => {
            if read_default().as_deref() == Some(name.as_str()) {
                let _ = write_default(None);
            }
            (StatusCode::OK, Json(json!({"ok": true, "removed": found})))
        }
        Ok(Err(m)) => bad(m),
        Err(e) => e,
    }
}

#[derive(Deserialize)]
struct NameBody {
    name: String,
}

async fn handle_default(Json(b): Json<NameBody>) -> impl IntoResponse {
    let n = b.name.clone();
    let exists = blocking(move || secure_store::get_model_entry(&n).map(|e| e.is_some()).map_err(|e| e.message().to_string())).await;
    match exists {
        Ok(Ok(true)) => match write_default(Some(&b.name)) {
            Ok(()) => (StatusCode::OK, Json(json!({"ok": true}))),
            Err(m) => bad(m),
        },
        Ok(Ok(false)) => bad("Такой модели нет."),
        Ok(Err(m)) => bad(m),
        Err(e) => e,
    }
}

#[derive(Deserialize)]
struct CheckBody {
    name: String,
    #[serde(default)]
    deep: bool,
}

/// «Проверить»: быстрая — файл на месте и движок найден; глубокая — реальный короткий вопрос модели (первый запуск локальной модели идёт долго).
async fn handle_check(Json(b): Json<CheckBody>) -> impl IntoResponse {
    let name = b.name.clone();
    let entry = match blocking(move || secure_store::get_model_entry(&name).map_err(|e| e.message().to_string())).await {
        Ok(Ok(Some(e))) => e,
        Ok(Ok(None)) => return bad("Такой модели нет."),
        Ok(Err(m)) => return bad(m),
        Err(e) => return e,
    };
    if entry.get("backend").and_then(|v| v.as_str()) == Some("llamacpp") {
        let path = entry.get("path").and_then(|v| v.as_str()).unwrap_or("");
        if !Path::new(path).is_file() {
            return (StatusCode::OK, Json(json!({"ok": false, "message": "Файл модели не найден по сохранённому пути."})));
        }
        if let Some(p) = crate::ai_rpc::intelligence_bridge::engine_problem() {
            return (StatusCode::OK, Json(json!({"ok": false, "message": format!("Файл модели на месте, но движок не найден: {p}")})));
        }
        if !b.deep {
            return (StatusCode::OK, Json(json!({"ok": true, "message": "Файл модели на месте, движок готов. Нажмите «Проверить ответом», чтобы задать модели вопрос."})));
        }
    }
    let (status, out) = ask(&b.name, vec![json!({"role": "user", "content": "Ответь одним словом: да"})], Some(8), Some(0.0)).await;
    if status == StatusCode::OK {
        (StatusCode::OK, Json(json!({"ok": true, "message": format!("Модель отвечает: «{}»", out["text"].as_str().unwrap_or("").trim())})))
    } else {
        (StatusCode::OK, Json(json!({"ok": false, "message": out["error"].clone()})))
    }
}

#[derive(Deserialize)]
struct BrowseQuery {
    path: Option<String>,
}

/// «Обзор»: папки и файлы `.gguf` любого диска. Работает под входом в узел; ничего не читает, кроме имён и размеров.
async fn handle_browse(Query(q): Query<BrowseQuery>) -> impl IntoResponse {
    let want = q.path.unwrap_or_default();
    match blocking(move || browse(&want)).await {
        Ok(Ok(v)) => (StatusCode::OK, Json(v)),
        Ok(Err(m)) => bad(m),
        Err(e) => e,
    }
}

fn browse(want: &str) -> Result<Value, String> {
    if want.is_empty() && cfg!(windows) {
        let drives: Vec<Value> = (b'A'..=b'Z').filter(|c| Path::new(&format!("{}:\\", *c as char)).exists()).map(|c| json!({"name": format!("{}:\\", c as char), "kind": "dir", "size": null})).collect();
        return Ok(json!({"path": "", "parent": null, "entries": drives}));
    }
    let start: PathBuf = if want.is_empty() {
        std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")).map(PathBuf::from).unwrap_or_else(|| PathBuf::from("/"))
    } else {
        PathBuf::from(want)
    };
    let dir = std::fs::canonicalize(&start).map_err(|_| "Такой папки нет.".to_string())?;
    if !dir.is_dir() {
        return Err("Это не папка.".into());
    }
    let mut entries: Vec<Value> = Vec::new();
    for e in std::fs::read_dir(&dir).map_err(|_| "Нет доступа к этой папке.".to_string())?.flatten() {
        let name = e.file_name().to_string_lossy().into_owned();
        if name.starts_with('.') {
            continue;
        }
        let Ok(md) = e.metadata() else { continue };
        if md.is_dir() {
            entries.push(json!({"name": name, "kind": "dir", "size": null}));
        } else if name.to_lowercase().ends_with(".gguf") {
            entries.push(json!({"name": name, "kind": "file", "size": md.len()}));
        }
    }
    entries.sort_by(|a, b| (b["kind"] == "dir").cmp(&(a["kind"] == "dir")).then_with(|| a["name"].as_str().unwrap_or("").to_lowercase().cmp(&b["name"].as_str().unwrap_or("").to_lowercase())));
    entries.truncate(MAX_BROWSE);
    Ok(json!({"path": dir.to_string_lossy(), "parent": dir.parent().map(|p| p.to_string_lossy().into_owned()), "entries": entries}))
}

#[derive(Deserialize)]
struct ChatBody {
    model: Option<String>,
    messages: Vec<Value>,
    temperature: Option<f64>,
    max_tokens: Option<i64>,
}

/// Один вызов модели через собственный шлюз узла; ошибки — простым языком.
async fn ask(model: &str, messages: Vec<Value>, max_tokens: Option<i64>, temperature: Option<f64>) -> (StatusCode, Value) {
    let args = json!({"model": model, "messages": messages, "base_url": yandi_llm::client::DEFAULT_BASE_URL, "max_tokens": max_tokens, "temperature": temperature, "timeout": 900});
    let (status, out) = crate::ai_rpc::intelligence_bridge::gateway_call("gw_complete".into(), args).await;
    if status != 200 {
        return (StatusCode::BAD_GATEWAY, json!({"error": out["error"]["msg"].as_str().unwrap_or("Не удалось обратиться к модели.")}));
    }
    match out.get("ok") {
        Some(ok) => (StatusCode::OK, json!({"text": ok["text"], "model": model})),
        None => (StatusCode::BAD_GATEWAY, json!({"error": friendly(out["error"]["msg"].as_str().unwrap_or(""))})),
    }
}

/// Технический текст ошибки шлюза → понятное сообщение (подробности остаются в журнале узла).
fn friendly(raw: &str) -> String {
    if raw.contains("не настроен ни один backend") {
        "Для этой модели нет настройки. Добавьте её в списке моделей.".into()
    } else if raw.contains("llama-server недоступен") {
        "Собственный движок модели не найден. Эта сборка узла собрана без вшитого движка.".into()
    } else if raw.contains("GGUF-файл не найден") {
        "Файл модели не найден. Проверьте путь в настройках модели.".into()
    } else if raw.contains("не стал готов") || raw.contains("завершился при запуске") {
        "Модель не запустилась (не хватает памяти или файл повреждён).".into()
    } else if raw.contains("401") || raw.contains("403") {
        "Сервис отклонил запрос: проверьте ключ доступа.".into()
    } else {
        format!("Модель не ответила. {}", raw.chars().take(300).collect::<String>())
    }
}

async fn handle_chat(Json(b): Json<ChatBody>) -> impl IntoResponse {
    if b.messages.is_empty() {
        return bad("Пустой разговор.");
    }
    let model = match b.model.filter(|m| !m.is_empty()).or_else(read_default) {
        Some(m) => m,
        None => return bad("Сначала выберите модель."),
    };
    let (status, body) = ask(&model, b.messages, b.max_tokens, b.temperature).await;
    (status, Json(body))
}

pub fn router<S: Clone + Send + Sync + 'static>() -> Router<S> {
    Router::new()
        .route("/api/ai/state", get(handle_state))
        .route("/api/ai/models", post(handle_save))
        .route("/api/ai/models/:name", delete(handle_delete))
        .route("/api/ai/default", post(handle_default))
        .route("/api/ai/check", post(handle_check))
        .route("/api/ai/browse", get(handle_browse))
        .route("/api/ai/chat", post(handle_chat).layer(axum::extract::DefaultBodyLimit::max(8 * 1024 * 1024)))
}
