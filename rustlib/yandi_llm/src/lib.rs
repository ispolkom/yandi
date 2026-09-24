//! yandi_llm — РОДНОЙ Rust-шлюз к языковым моделям: перенос `llm_gateway/` (client.py, types.py, vector_space.py, затем бэкенды, secure_store).
//! Это часть пути к единому бинарнику (см. ../../README.md): библиотека не зависит от Python; мост PyO3 (feature `python`) нужен ТОЛЬКО для
//! дифференциальных тестов «JSON внутрь → JSON наружу» против оригинала — Python остаётся эталоном, пока не переехали все вызывающие.
//!
//! Правила: (1) поведение = поведению Python-оригинала на обычных входах (расхождения на экзотике — NaN/огромные целые/одинокие суррогаты в JSON —
//! перечислены в README и в тестах); (2) тексты ошибок JSON — точные тексты Python (`yandi_rs::py_json`); (3) порядок ключей словарей сохраняется
//! (`serde_json` с `preserve_order`) — он виден вызывающим при `json.dumps`/сравнении.

pub mod messages;
pub mod pyfmt;
pub mod semantic;
pub mod types;
pub mod vector_space;

#[cfg(feature = "python")]
mod bridge;
