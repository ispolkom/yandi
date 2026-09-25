//! Единый бинарник: родной шлюз к моделям (`yandi_llm`) и встроенное хранилище состояния (`yandi_state`, замена Redis) подключены к узлу как обычные
//! библиотеки — без Python, без внешних серверов. Тест ничего не меняет в поведении узла; он доказывает, что зависимости собираются и работают в его рабочем пространстве.

use yandi_llm::client::{Gateway, GatewayOptions, SecureStoreConfig, UnavailableEngine};
use yandi_llm::messages::SystemArg;
use yandi_llm::secure_store;
use yandi_llm::transport::ReqwestTransport;
use yandi_state::Store;

fn argv(parts: &[&str]) -> Vec<Vec<u8>> {
    parts.iter().map(|p| p.as_bytes().to_vec()).collect()
}

#[test]
fn state_store_speaks_redis_semantics() {
    let s = Store::new();
    assert_eq!(s.execute(&argv(&["SET", "k", "v"])), yandi_state::Reply::ok());
    assert_eq!(s.execute(&argv(&["GET", "k"])), yandi_state::Reply::Bulk(b"v".to_vec()));
    assert_eq!(s.execute(&argv(&["LPUSH", "l", "a", "b"])), yandi_state::Reply::Int(2));
    assert!(matches!(s.execute(&argv(&["LPUSH", "k", "x"])), yandi_state::Reply::Error(e) if e.starts_with("WRONGTYPE")));
}

#[test]
fn secure_store_and_gateway_resolve_natively() {
    let dir = std::env::temp_dir().join(format!("yandi-link-{}", std::process::id()));
    let _ = std::fs::remove_dir_all(&dir);
    std::env::set_var("YANDI_KEK_PATH", dir.join("keys/node_kek.bin"));
    std::env::set_var("YANDI_NODE_DB", dir.join("node_config.sqlite"));
    secure_store::set_model_entry("m", &serde_json::json!({"backend": "remote", "protocol": "openai", "base_url": "http://127.0.0.1:1"})).expect("запись");
    assert_eq!(secure_store::get_model_entry("m").unwrap().unwrap()["protocol"], "openai");
    // явная настройка владельца: недоступный сервер → честная ошибка именно этого backend'а, БЕЗ перехода на другой источник
    let t = ReqwestTransport::new();
    let engine = UnavailableEngine::new("нет");
    let gw = Gateway { transport: &t, config: &SecureStoreConfig, engine: &engine, opts: GatewayOptions::from_env() };
    let p = yandi_llm::client::CompleteParams::new();
    let err = gw.complete(Some("привет"), "m", &SystemArg::None, None, yandi_llm::client::DEFAULT_BASE_URL, &p).unwrap_err();
    assert!(err.0.contains("автоматический переход на другой источник интеллекта запрещён"), "{}", err.0);
    let _ = std::fs::remove_dir_all(&dir);
}
