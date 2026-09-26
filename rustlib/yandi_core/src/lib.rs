//! yandi_core — ядро YANDI на Rust: перенос `agent/` слой за слоем поверх `yandi_db`. Каждый модуль сверяется с Python-оригиналом на одних и тех же последовательностях вызовов
//! (агент на Python + MySQL против ядра на Rust + SQLite). Время и случайные идентификаторы подаются через `Ctx`, поэтому поведение воспроизводимо в тестах.
pub mod belief_manager;
pub mod causal_events;
pub mod chat_prompts;
pub mod chat_turn;
pub mod commitment_verification;
pub mod curiosity;
pub mod hypothesis_builder;
pub mod core_loop;
pub mod event_extraction;
pub mod fact_extraction;
pub mod inner_state;
pub mod memory_episodic;
pub mod motivation;
pub mod ctx;
pub mod personal_facts;
pub mod personal_memory;
pub mod personality_graph;
pub mod reflection_loop;
pub mod relationship_commitments;
pub mod relationship_memory;
pub mod relationship_state;
pub mod self_model;

#[cfg(feature = "python")]
mod bridge;

pub use ctx::Ctx;
pub type R<T> = Result<T, String>;
