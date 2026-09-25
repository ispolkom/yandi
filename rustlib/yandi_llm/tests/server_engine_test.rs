//! Родной локальный движок (присматриваемый llama-server) против подделки с теми же путями и аргументами.
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{json, Value};
use yandi_llm::client::{LocalEngine, LocalParams, LocalTarget, ModelSpec};
use yandi_llm::server_engine::{ServerEngine, ServerEngineConfig};

fn fake() -> String {
    format!("{}/tests/fake_llama_server.py", env!("CARGO_MANIFEST_DIR"))
}

fn gguf(dir: &PathBuf, name: &str) -> ModelSpec {
    let p = dir.join(name);
    std::fs::write(&p, b"x").unwrap();
    ModelSpec { path: p.to_string_lossy().into_owned(), n_ctx: 4096, n_gpu_layers: -1 }
}

fn tmp(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!("yandi_se_{}_{}", tag, std::process::id()));
    let _ = std::fs::remove_dir_all(&d);
    std::fs::create_dir_all(&d).unwrap();
    d
}

fn engine(reg: Vec<(String, ModelSpec)>, log: Option<&PathBuf>) -> ServerEngine {
    match log {
        Some(l) => std::env::set_var("FAKE_LOG", l),
        None => std::env::remove_var("FAKE_LOG"),
    }
    let mut cfg = ServerEngineConfig::new(reg);
    cfg.binary = fake();
    cfg.startup_timeout = Duration::from_secs(10);
    ServerEngine::new(cfg)
}

fn lines(log: &PathBuf) -> Vec<Value> {
    std::fs::read_to_string(log).unwrap_or_default().lines().map(|l| serde_json::from_str(l).unwrap()).collect()
}

fn alive(pid: i64) -> bool {
    std::path::Path::new(&format!("/proc/{pid}")).exists() && std::fs::read_to_string(format!("/proc/{pid}/stat")).map(|s| !s.contains(") Z")).unwrap_or(false)
}

#[test]
fn generate_speaks_openai_to_own_process_and_passes_args() {
    let d = tmp("gen");
    let log = d.join("log");
    let spec = gguf(&d, "model-a.gguf");
    let e = engine(vec![("alias".into(), spec.clone())], Some(&log));
    assert!(e.registry_error().is_none());
    assert!(e.has_model("alias"));
    assert!(!e.has_model("zzz"));
    let msgs = vec![json!({"role": "user", "content": "привет"})];
    let lp = LocalParams {
        temperature: Some(0.5),
        max_tokens: Some(1),
        response_format: Some(json!("json")),
        stop: Some(vec!["z".into()]),
        extra_options: json!({"seed": 7, "repeat_last_n": 64, "temperature": 9, "max_tokens": 99}).as_object().cloned(),
    };
    let (text, meta) = e.generate(&LocalTarget::Registry("alias".into()), &msgs, &lp).unwrap();
    let echoed: Value = serde_json::from_str(&text).unwrap();
    assert_eq!(echoed["messages"], json!(msgs));
    assert_eq!(echoed["temperature"], json!(0.5)); // явный параметр сильнее extra_options
    assert_eq!(echoed["max_tokens"], json!(1));
    assert_eq!(echoed["seed"], json!(7));
    assert!(echoed.get("repeat_last_n").is_none());
    assert_eq!(echoed["response_format"], json!({"type": "json_object"}));
    assert_eq!(echoed["stop"], json!(["z"]));
    assert_eq!(meta["done_reason"], json!("length"));
    assert_eq!(meta["eval_count"], json!(7));
    // командная строка процесса
    let l = lines(&log);
    assert_eq!(l.len(), 1);
    let a: Vec<String> = l[0]["args"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
    assert_eq!(&a[0..2], &["-m".to_string(), spec.path.clone()]);
    assert!(a.windows(2).any(|w| w == ["--host", "127.0.0.1"]));
    assert!(a.windows(2).any(|w| w == ["-c", "4096"]));
    assert!(a.windows(2).any(|w| w == ["-ngl", "999"]));
    assert!(!a.contains(&"--embedding".to_string()));
    // второй вызов — тот же процесс
    e.generate(&LocalTarget::Spec(spec), &msgs, &LocalParams::default()).unwrap();
    assert_eq!(lines(&log).len(), 1);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn plain_generation_sends_no_optional_fields() {
    let d = tmp("plain");
    let spec = gguf(&d, "m.gguf");
    let e = engine(vec![], None);
    let (text, meta) = e.generate(&LocalTarget::Spec(spec), &[json!({"role": "user", "content": "x"})], &LocalParams { response_format: Some(json!({"type": "object"})), stop: Some(vec![]), ..Default::default() }).unwrap();
    let echoed: Value = serde_json::from_str(&text).unwrap();
    assert!(echoed.get("response_format").is_none(), "схема не передаётся, как и в Python-движке: {echoed}");
    assert!(echoed.get("stop").is_none() && echoed.get("temperature").is_none() && echoed.get("max_tokens").is_none());
    assert_eq!(meta["done_reason"], json!("stop"));
    // строка, отличная от "json", тоже не включает json-режим; n_gpu_layers=0 передаётся как есть (CPU), а не «все слои»
    let log = d.join("log2");
    std::env::set_var("FAKE_LOG", &log);
    let cpu = ModelSpec { n_gpu_layers: 0, ..gguf(&d, "cpu.gguf") };
    let (text, _) = e.generate(&LocalTarget::Spec(cpu), &[json!({"role": "user", "content": "x"})], &LocalParams { response_format: Some(json!("text")), ..Default::default() }).unwrap();
    assert!(serde_json::from_str::<Value>(&text).unwrap().get("response_format").is_none());
    let a: Vec<String> = lines(&log)[0]["args"].as_array().unwrap().iter().map(|v| v.as_str().unwrap().to_string()).collect();
    assert!(a.windows(2).any(|w| w == ["-ngl", "0"]), "{a:?}");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn embeddings_use_separate_embedding_process_and_sorted_by_index() {
    let d = tmp("emb");
    let log = d.join("log");
    let spec = gguf(&d, "emb.gguf");
    let e = engine(vec![], Some(&log));
    let (v, meta) = e.embed(&spec, &["ab".into(), "abcd".into()]).unwrap();
    assert_eq!(v, vec![json!([2.0, 0.5, 0.0]), json!([4.0, 0.5, 1.0])]);
    assert_eq!(meta["dimension"], json!(3));
    assert_eq!(meta["normalized"], json!(false));
    // эмбеддинги — отдельный экземпляр с --embedding; генерация по той же модели поднимает второй процесс без него
    e.generate(&LocalTarget::Spec(spec.clone()), &[json!({"role": "user", "content": "x"})], &LocalParams::default()).unwrap();
    let l = lines(&log);
    assert_eq!(l.len(), 2);
    let has_emb = |v: &Value| v["args"].as_array().unwrap().iter().any(|a| a == "--embedding");
    assert!(has_emb(&l[0]) && !has_emb(&l[1]));
    assert_ne!(l[0]["pid"], l[1]["pid"]);
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn honest_errors_and_no_process_for_missing_things() {
    let d = tmp("err");
    let log = d.join("log");
    let e = engine(vec![("a".into(), ModelSpec { path: d.join("absent.gguf").to_string_lossy().into_owned(), n_ctx: 1, n_gpu_layers: 0 })], Some(&log));
    let msgs = [json!({"role": "user", "content": "x"})];
    assert!(!e.has_model("a"));
    let err = e.generate(&LocalTarget::Registry("nope".into()), &msgs, &LocalParams::default()).unwrap_err();
    assert_eq!(err, "нет встроенной GGUF-записи для модели 'nope'");
    let err = e.generate(&LocalTarget::Registry("a".into()), &msgs, &LocalParams::default()).unwrap_err();
    assert!(err.starts_with("GGUF-файл не найден: ") && err.ends_with("absent.gguf"), "{err}");
    let spec = gguf(&d, "x.gguf");
    let err = e.embed(&spec, &[]).unwrap_err();
    assert_eq!(err, "embed_at_spec() вызван с пустым списком текстов");
    let err = e.embed(&ModelSpec { path: d.join("absent2.gguf").to_string_lossy().into_owned(), n_ctx: 1, n_gpu_layers: 0 }, &["a".into()]).unwrap_err();
    assert!(err.starts_with("GGUF-файл не найден: "), "{err}");
    assert!(lines(&log).is_empty(), "процесс не должен запускаться");
    // сервер вернул 500 → ошибка с текстом статуса
    let bad = gguf(&d, "http500.gguf");
    let err = e.generate(&LocalTarget::Spec(bad), &msgs, &LocalParams::default()).unwrap_err();
    assert!(err.contains("500 Server Error"), "{err}");
    // режим embedding у подделки не отвечает на chat, но здесь важно: ошибка запуска
    let crash = gguf(&d, "crash.gguf");
    let err = e.generate(&LocalTarget::Spec(crash), &msgs, &LocalParams::default()).unwrap_err();
    assert!(err.starts_with("llama-server завершился при запуске"), "{err}");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn missing_binary_is_reported_and_startup_timeout_kills_child() {
    let d = tmp("bin");
    let spec = gguf(&d, "m.gguf");
    let mut cfg = ServerEngineConfig::new(vec![]);
    cfg.binary = "/nonexistent/llama-server".into();
    let e = ServerEngine::new(cfg);
    assert_eq!(e.registry_error().as_deref(), Some("llama-server недоступен: /nonexistent/llama-server: файл не найден"));
    let err = e.generate(&LocalTarget::Spec(spec), &[json!({"role": "user", "content": "x"})], &LocalParams::default()).unwrap_err();
    assert!(err.starts_with("llama-server недоступен: "), "{err}");
    let mut cfg = ServerEngineConfig::new(vec![]);
    cfg.binary = "definitely-not-in-path-xyz".into();
    assert_eq!(ServerEngine::new(cfg).registry_error().as_deref(), Some("llama-server недоступен: definitely-not-in-path-xyz: не найден в PATH"));
    // сервер не становится готов вовремя
    let log = d.join("log");
    let never = gguf(&d, "never.gguf");
    let mut e = engine(vec![], Some(&log));
    e = {
        let mut cfg = ServerEngineConfig::new(vec![]);
        cfg.binary = fake();
        cfg.startup_timeout = Duration::from_millis(600);
        drop(e);
        ServerEngine::new(cfg)
    };
    let err = e.generate(&LocalTarget::Spec(never), &[json!({"role": "user", "content": "x"})], &LocalParams::default()).unwrap_err();
    assert_eq!(err, "llama-server не стал готов за 0 с");
    let pid = lines(&log)[0]["pid"].as_i64().unwrap();
    std::thread::sleep(Duration::from_millis(200));
    assert!(!alive(pid), "процесс должен быть убит после тайм-аута");
    let _ = std::fs::remove_dir_all(&d);
}

#[test]
fn waits_for_loading_then_restarts_dead_process_and_kills_on_drop() {
    let d = tmp("life");
    let log = d.join("log");
    let spec = gguf(&d, "loading.gguf");
    let e = engine(vec![], Some(&log));
    let msgs = [json!({"role": "user", "content": "x"})];
    let t0 = std::time::Instant::now();
    e.generate(&LocalTarget::Spec(spec.clone()), &msgs, &LocalParams::default()).unwrap();
    assert!(t0.elapsed() >= Duration::from_millis(350), "должен был дождаться готовности");
    let pid1 = lines(&log)[0]["pid"].as_i64().unwrap();
    assert!(alive(pid1));
    // убили снаружи → следующий вызов поднимает новый процесс
    unsafe_kill(pid1);
    std::thread::sleep(Duration::from_millis(300));
    e.generate(&LocalTarget::Spec(spec), &msgs, &LocalParams::default()).unwrap();
    let l = lines(&log);
    assert_eq!(l.len(), 2);
    let pid2 = l[1]["pid"].as_i64().unwrap();
    assert_ne!(pid1, pid2);
    assert!(alive(pid2));
    drop(e);
    std::thread::sleep(Duration::from_millis(200));
    assert!(!alive(pid2), "выход узла убивает дочерний процесс");
    let _ = std::fs::remove_dir_all(&d);
}

fn unsafe_kill(pid: i64) {
    let _ = std::process::Command::new("kill").arg("-9").arg(pid.to_string()).status();
}

#[test]
fn gateway_end_to_end_with_own_engine_no_ollama_no_network_config() {
    use yandi_llm::client::{CompleteParams, ConfigSource, Gateway, GatewayOptions, DEFAULT_BASE_URL};
    use yandi_llm::messages::SystemArg;
    use yandi_llm::transport::{HttpRequest, HttpResponse, Transport, TransportError};
    struct NoNet;
    impl Transport for NoNet {
        fn post_json(&self, r: &HttpRequest, _t: u64) -> Result<HttpResponse, TransportError> {
            panic!("внешний запрос: {}", r.url)
        }
    }
    struct NoCfg;
    impl ConfigSource for NoCfg {
        fn get_model_entry(&self, _m: &str) -> Result<Option<Value>, String> {
            Ok(None)
        }
    }
    let d = tmp("e2e");
    let spec = gguf(&d, "brain.gguf");
    let e = engine(vec![("brain".into(), spec)], None);
    let (t, c) = (NoNet, NoCfg);
    let gw = Gateway { transport: &t, config: &c, engine: &e, opts: GatewayOptions { default_base_url: DEFAULT_BASE_URL.into(), local_enabled: true } };
    let p = CompleteParams { timeout: 5, strip_think: true, ..Default::default() };
    let out = gw.complete(Some("вопрос"), "brain", &SystemArg::None, None, DEFAULT_BASE_URL, &p).unwrap();
    let echoed: Value = serde_json::from_str(&out).unwrap();
    assert_eq!(echoed["messages"], json!([{"role": "user", "content": "вопрос"}]));
    let r = gw.embed(&["abc".into()], "brain", DEFAULT_BASE_URL, 5).unwrap();
    assert_eq!(r.vectors, vec![json!([3.0, 0.5, 0.0])]);
    assert_eq!(r.space.backend, "llamacpp");
    // локальный движок выключен → своё не запускается, честная ошибка
    let gw_off = Gateway { transport: &t, config: &c, engine: &e, opts: GatewayOptions { default_base_url: DEFAULT_BASE_URL.into(), local_enabled: false } };
    assert!(gw_off.complete(Some("q"), "brain", &SystemArg::None, None, DEFAULT_BASE_URL, &p).unwrap_err().0.contains("не настроен ни один backend"));
    let _ = std::fs::remove_dir_all(&d);
}
