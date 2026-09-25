//! Проверка собственного движка узла на НАСТОЯЩЕЙ модели: запускает `llama-server`, задаёт один вопрос и просит эмбеддинг.
//! Запуск: cargo run --release --no-default-features --example engine_check -- /путь/к/модели.gguf [/путь/к/llama-server]
use std::time::Instant;

use serde_json::json;
use yandi_llm::client::{LocalEngine, LocalParams, LocalTarget, ModelSpec};
use yandi_llm::server_engine::{ServerEngine, ServerEngineConfig};

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let Some(model) = args.first() else {
        eprintln!("Укажите путь к файлу модели (.gguf) первым аргументом.");
        std::process::exit(2);
    };
    let mut cfg = ServerEngineConfig::new(vec![]);
    if let Some(b) = args.get(1) {
        cfg.binary = b.clone();
    }
    let engine = ServerEngine::new(cfg);
    if let Some(e) = engine.registry_error() {
        eprintln!("ПРОВАЛ: {e}\nУстановите llama-server (часть llama.cpp) и/или укажите путь вторым аргументом.");
        std::process::exit(1);
    }
    let spec = ModelSpec { path: model.clone(), n_ctx: 4096, n_gpu_layers: -1 };
    println!("1/2 Запускаю модель и задаю вопрос (первый запуск может занять минуту-две)...");
    let t = Instant::now();
    let msgs = [json!({"role": "user", "content": "Ответь одним словом: какая столица у Франции?"})];
    let lp = LocalParams { temperature: Some(0.0), max_tokens: Some(64), ..Default::default() };
    match engine.generate(&LocalTarget::Spec(spec.clone()), &msgs, &lp) {
        Ok((text, meta)) => println!("   ОТВЕТ МОДЕЛИ: {:?}\n   служебное: {}\n   время: {:.1} с", text, serde_json::Value::Object(meta), t.elapsed().as_secs_f64()),
        Err(e) => {
            eprintln!("ПРОВАЛ при генерации: {e}");
            std::process::exit(1);
        }
    }
    println!("2/2 Прошу эмбеддинг (второй экземпляр модели)...");
    let t = Instant::now();
    match engine.embed(&spec, &["привет, мир".to_string()]) {
        Ok((v, meta)) => println!("   векторов: {}, размерность: {}, время: {:.1} с", v.len(), meta["dimension"], t.elapsed().as_secs_f64()),
        Err(e) => {
            eprintln!("ПРОВАЛ при эмбеддинге: {e}\n(если модель не умеет эмбеддинги — это не ошибка движка)");
            std::process::exit(1);
        }
    }
    println!("ГОТОВО: собственный движок работает. При выходе дочерние процессы завершаются.");
}
