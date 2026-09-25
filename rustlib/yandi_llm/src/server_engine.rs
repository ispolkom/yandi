//! Собственный локальный движок узла: присматриваемый процесс `llama-server` (llama.cpp) + OpenAI-протокол по localhost.
//! Замена `llamacpp_backend` (Python-библиотека `llama_cpp` внутри процесса) для единого бинарника: библиотек Python нет, модель живёт в
//! дочернем процессе, которым владеет узел (запуск лениво, при первом обращении; смерть процесса → перезапуск при следующем; выход узла → процесс убит).
//! Ollama НЕ используется — это СВОЙ инструмент узла. Семантика — как у Python-движка: «GGUF-файл не найден», отдельный экземпляр для эмбеддингов,
//! json-режим только для `response_format == "json"`, `repeat_last_n` отбрасывается, один запрос на модель за раз.
use std::collections::HashMap;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde_json::{Map, Value};

use crate::client::{LocalEngine, LocalParams, LocalTarget, ModelSpec};
use crate::remote::{openai_embed_parse, openai_embed_request, openai_generate_parse, openai_generate_request, GenerateParams};
use crate::transport::{HttpRequest, ReqwestTransport, Transport};
use yandi_rs::py_text::py_repr_str;

pub const DEFAULT_STARTUP_TIMEOUT_SECS: u64 = 120;

#[derive(Debug, Clone)]
pub struct ServerEngineConfig {
    /// Путь к `llama-server`: вшитый в бинарник узла (распаковывается сам) → рядом с узлом → каталог данных → PATH; `YANDI_LLAMA_SERVER` — явное переопределение (см. `engine_binary`).
    pub binary: String,
    pub registry: Vec<(String, ModelSpec)>,
    pub startup_timeout: Duration,
    /// Дополнительные аргументы командной строки (например `--threads 4`), добавляются после обязательных.
    pub extra_args: Vec<String>,
}

impl ServerEngineConfig {
    pub fn new(registry: Vec<(String, ModelSpec)>) -> Self {
        let binary = crate::engine_binary::resolve();
        ServerEngineConfig { binary, registry, startup_timeout: Duration::from_secs(DEFAULT_STARTUP_TIMEOUT_SECS), extra_args: vec![] }
    }
}

struct Proc {
    child: Child,
    port: u16,
}

pub struct ServerEngine {
    cfg: ServerEngineConfig,
    /// создаётся при первом запросе: блокирующий HTTP-клиент нельзя строить внутри async-контекста узла
    transport: std::sync::OnceLock<ReqwestTransport>,
    /// ключ: (путь GGUF, режим эмбеддингов) — llama.cpp не переключает режим у запущенного контекста
    procs: Mutex<HashMap<(String, bool), Proc>>,
    /// один запрос за раз (как `_lock` у Python-движка): контекст модели не потокобезопасен
    gen_lock: Mutex<()>,
}

impl ServerEngine {
    pub fn new(cfg: ServerEngineConfig) -> Self {
        ServerEngine { cfg, transport: std::sync::OnceLock::new(), procs: Mutex::new(HashMap::new()), gen_lock: Mutex::new(()) }
    }

    /// Найден ли исполняемый файл (абсолютный/относительный путь или поиск в PATH).
    fn binary_found(&self) -> Result<(), String> {
        let b = &self.cfg.binary;
        if b.contains('/') || b.contains(std::path::MAIN_SEPARATOR) {
            return if Path::new(b).is_file() { Ok(()) } else { Err(format!("{b}: файл не найден")) };
        }
        let path = std::env::var_os("PATH").unwrap_or_default();
        for dir in std::env::split_paths(&path) {
            let cand = dir.join(b);
            if cand.is_file() {
                return Ok(());
            }
            #[cfg(windows)]
            if dir.join(format!("{b}.exe")).is_file() {
                return Ok(());
            }
        }
        Err(format!("{b}: не найден в PATH"))
    }

    fn resolve_spec(&self, target: &LocalTarget) -> Result<ModelSpec, String> {
        match target {
            LocalTarget::Registry(alias) => self.registry_spec(alias).ok_or_else(|| format!("нет встроенной GGUF-записи для модели {}", py_repr_str(alias))),
            LocalTarget::Spec(s) => Ok(s.clone()),
        }
    }

    /// Порт localhost процесса для (модель, режим); при необходимости запускает и ждёт готовности.
    fn ensure(&self, spec: &ModelSpec, embedding: bool) -> Result<u16, String> {
        self.binary_found().map_err(|e| format!("llama-server недоступен: {e}"))?;
        if !Path::new(&spec.path).exists() {
            return Err(format!("GGUF-файл не найден: {}", spec.path));
        }
        let mut procs = self.procs.lock().unwrap_or_else(|e| e.into_inner());
        let key = (spec.path.clone(), embedding);
        if let Some(p) = procs.get_mut(&key) {
            match p.child.try_wait() {
                Ok(None) => return Ok(p.port),
                _ => {
                    procs.remove(&key);
                }
            }
        }
        let port = free_port()?;
        let mut cmd = Command::new(&self.cfg.binary);
        cmd.arg("-m").arg(&spec.path).arg("--host").arg("127.0.0.1").arg("--port").arg(port.to_string());
        cmd.arg("-c").arg(spec.n_ctx.to_string());
        // -1 у Python = «все слои на GPU»; llama-server принимает достаточно большое число
        cmd.arg("-ngl").arg(if spec.n_gpu_layers < 0 { 999 } else { spec.n_gpu_layers }.to_string());
        if embedding {
            cmd.arg("--embedding");
        }
        cmd.args(&self.cfg.extra_args);
        cmd.stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null());
        let mut child = cmd.spawn().map_err(|e| format!("llama-server не запустился: {e}"))?;
        let deadline = Instant::now() + self.cfg.startup_timeout;
        loop {
            if let Ok(Some(status)) = child.try_wait() {
                return Err(format!("llama-server завершился при запуске ({status})"));
            }
            if http_get_status(port, "/health", Duration::from_millis(500)) == Some(200) {
                break;
            }
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!("llama-server не стал готов за {} с", self.cfg.startup_timeout.as_secs()));
            }
            std::thread::sleep(Duration::from_millis(50));
        }
        procs.insert(key, Proc { child, port });
        Ok(port)
    }

    fn post(&self, req: &HttpRequest, timeout: u64) -> Result<Vec<u8>, String> {
        let resp = self.transport.get_or_init(ReqwestTransport::new).post_json(req, timeout).map_err(|e| e.0)?;
        if let Some(d) = crate::remote::status_error(resp.status, &resp.reason, &req.url) {
            return Err(d);
        }
        Ok(resp.body)
    }
}

impl Drop for ServerEngine {
    fn drop(&mut self) {
        let procs = self.procs.get_mut().unwrap_or_else(|e| e.into_inner());
        for (_, p) in procs.iter_mut() {
            let _ = p.child.kill();
            let _ = p.child.wait();
        }
    }
}

impl LocalEngine for ServerEngine {
    fn registry_error(&self) -> Option<String> {
        self.binary_found().err().map(|e| format!("llama-server недоступен: {e}"))
    }
    fn has_model(&self, model: &str) -> bool {
        self.cfg.registry.iter().any(|(n, s)| n == model && Path::new(&s.path).exists())
    }
    fn registry_spec(&self, model: &str) -> Option<ModelSpec> {
        self.cfg.registry.iter().find(|(n, _)| n == model).map(|(_, s)| s.clone())
    }
    fn generate(&self, target: &LocalTarget, messages: &[Value], p: &LocalParams) -> Result<(String, Map<String, Value>), String> {
        let spec = self.resolve_spec(target)?;
        let port = self.ensure(&spec, false)?;
        let base = format!("http://127.0.0.1:{port}/v1");
        let gp = GenerateParams {
            temperature: p.temperature,
            max_tokens: p.max_tokens,
            response_format: match &p.response_format {
                Some(Value::String(s)) if s == "json" => Some("json".into()),
                _ => None,
            },
            stop: p.stop.clone(),
            timeout: crate::client::DEFAULT_TIMEOUT,
        };
        let mut req = openai_generate_request(messages, &base, None, &spec.path, &gp);
        if let (Some(extra), Value::Object(body)) = (&p.extra_options, &mut req.body) {
            for (k, v) in extra {
                if k == "repeat_last_n" {
                    continue; // как у Python-движка: не параметр вызова, только конструктора
                }
                body.insert(k.clone(), v.clone());
            }
            // явные параметры вызова сильнее extra_options (как `kwargs[...] = ...` после `dict(extra_options)`)
            if let Some(t) = p.temperature {
                body.insert("temperature".into(), serde_json::json!(t));
            }
            if let Some(m) = p.max_tokens {
                body.insert("max_tokens".into(), serde_json::json!(m));
            }
        }
        let _g = self.gen_lock.lock().unwrap_or_else(|e| e.into_inner());
        let body = self.post(&req, gp.timeout)?;
        openai_generate_parse(&body, &spec.path, &base).map_err(|e| e.0)
    }
    fn embed(&self, spec: &ModelSpec, texts: &[String]) -> Result<(Vec<Value>, Map<String, Value>), String> {
        if !Path::new(&spec.path).exists() {
            return Err(format!("GGUF-файл не найден: {}", spec.path));
        }
        if texts.is_empty() {
            return Err("embed_at_spec() вызван с пустым списком текстов".to_string());
        }
        let port = self.ensure(spec, true)?;
        let base = format!("http://127.0.0.1:{port}/v1");
        let req = openai_embed_request(texts, &base, None, &spec.path);
        let _g = self.gen_lock.lock().unwrap_or_else(|e| e.into_inner());
        let body = self.post(&req, crate::client::DEFAULT_TIMEOUT)?;
        openai_embed_parse(&body, &spec.path, &base).map_err(|e| e.0)
    }
}

fn free_port() -> Result<u16, String> {
    let l = TcpListener::bind("127.0.0.1:0").map_err(|e| format!("нет свободного порта: {e}"))?;
    l.local_addr().map(|a| a.port()).map_err(|e| e.to_string())
}

/// Минимальный `GET` по localhost без внешних зависимостей: код статуса или None (не отвечает).
fn http_get_status(port: u16, path: &str, timeout: Duration) -> Option<u16> {
    let mut s = TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), timeout).ok()?;
    s.set_read_timeout(Some(timeout)).ok()?;
    s.set_write_timeout(Some(timeout)).ok()?;
    s.write_all(format!("GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n").as_bytes()).ok()?;
    let mut buf = Vec::new();
    let mut chunk = [0u8; 256];
    while buf.len() < 64 {
        match s.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(_) => break,
        }
        if buf.windows(2).any(|w| w == b"\r\n") {
            break;
        }
    }
    let line = String::from_utf8_lossy(&buf);
    line.split_whitespace().nth(1)?.parse().ok()
}
