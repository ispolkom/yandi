//! Проверка собственного движка узла на модели, которую ПОЛЬЗОВАТЕЛЬ выбрал в веб-интерфейсе. Никаких аргументов: настройки читаются из
//! хранилища узла (то же, что видит и пишет веб), запрос идёт тем же путём, что и в продукте: Gateway → настройка владельца → ServerEngine → llama-server.
//! Запуск: cargo run --release --no-default-features --example engine_check
use std::time::Instant;

use serde_json::Value;
use yandi_llm::client::{CompleteParams, Gateway, GatewayOptions, LocalEngine, SecureStoreConfig};
use yandi_llm::messages::SystemArg;
use yandi_llm::secure_store;
use yandi_llm::server_engine::{ServerEngine, ServerEngineConfig};
use yandi_llm::transport::ReqwestTransport;

fn main() {
    if !secure_store::db_path().exists() {
        println!("В настройках узла пока ничего нет ({}).\nОткройте веб-интерфейс YANDI, выберите там модель — и запустите проверку снова.", secure_store::db_path().display());
        return;
    }
    let models = match secure_store::list_models() {
        Ok(m) => m,
        Err(e) => {
            eprintln!("ПРОВАЛ: не удалось прочитать настройки узла: {}", e.message());
            std::process::exit(1);
        }
    };
    let local: Vec<(&String, &Value)> = models.iter().filter(|(_, v)| v.get("backend").and_then(|b| b.as_str()) == Some("llamacpp")).collect();
    println!("Модели в настройках узла: {} (из них локальных, через llama.cpp: {})", models.len(), local.len());
    if local.is_empty() {
        println!("Локальной модели, выбранной в веб-интерфейсе, нет — проверять нечего. Выберите локальную модель в вебе и повторите.");
        return;
    }
    let mut failed = false;
    for (name, entry) in local {
        println!("\n=== Модель «{}» (файл: {}) ===", name, entry.get("path").and_then(|p| p.as_str()).unwrap_or("?"));
        // отдельный движок на каждую модель: при выходе из итерации его процессы завершаются (память не копится)
        let engine = ServerEngine::new(ServerEngineConfig::new(vec![]));
        if let Some(e) = engine.registry_error() {
            eprintln!("ПРОВАЛ: {e}\nНужна программа llama-server (часть llama.cpp): поставьте её в PATH или задайте путь в переменной YANDI_LLAMA_SERVER.");
            std::process::exit(1);
        }
        let transport = ReqwestTransport::new();
        let gw = Gateway { transport: &transport, config: &SecureStoreConfig, engine: &engine, opts: GatewayOptions::from_env() };
        let opts = gw.opts.default_base_url.clone();
        println!("1/2 Запускаю модель и задаю вопрос (первый запуск может занять минуту-две)...");
        let t = Instant::now();
        let p = CompleteParams { temperature: Some(0.0), max_tokens: Some(64), timeout: 600, strip_think: true, ..Default::default() };
        match gw.complete(Some("Ответь одним словом: какая столица у Франции?"), name, &SystemArg::None, None, &opts, &p) {
            Ok(text) => println!("   ОТВЕТ МОДЕЛИ: {:?}\n   время: {:.1} с", text, t.elapsed().as_secs_f64()),
            Err(e) => {
                eprintln!("   ПРОВАЛ при генерации: {}", e.0);
                failed = true;
                continue;
            }
        }
        println!("2/2 Прошу эмбеддинг...");
        let t = Instant::now();
        match gw.embed(&["привет, мир".to_string()], name, &opts, 600) {
            Ok(r) => println!("   векторов: {}, размерность: {}, время: {:.1} с", r.vectors.len(), r.space.dimension, t.elapsed().as_secs_f64()),
            Err(e) => println!("   эмбеддинг недоступен: {}\n   (если эта модель не умеет эмбеддинги — это нормально, генерация выше работает)", e.0),
        }
    }
    if failed {
        std::process::exit(1);
    }
    println!("\nГОТОВО: собственный движок работает на модели из настроек узла.");
}
