//! yandi_state — встроенное хранилище состояния YANDI: замена внешнему Redis в едином бинарнике (кроссплатформенно: без сервера, без установки).
//! Семантика — Redis 7 для РЕАЛЬНО используемого подмножества команд (строки с TTL, списки, множества, ключи по шаблону, PUBLISH/SUBSCRIBE), включая
//! тексты ошибок (`WRONGTYPE …`, `ERR value is not an integer or out of range`, …) и граничные случаи (отрицательные индексы, пустой контейнер удаляет ключ, TTL при
//! переименовании…). Проверяется дифференциально против НАСТОЯЩЕГО redis-server (`llm_gateway`-стиль: `agent/state_store_parity_test.py`).
//! Один процесс: хранилище внутри процесса ядра (общий доступ нескольких процессов — через API ядра, см. docs/NODE_CORE_CONTRACT.md).

pub mod glob;
pub mod pubsub;
pub mod reply;
pub mod store;

#[cfg(feature = "python")]
mod bridge;

pub use reply::Reply;
pub use store::Store;
