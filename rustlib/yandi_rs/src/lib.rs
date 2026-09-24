//! yandi_rs — Rust-переносы отдельных модулей YANDI (agent/, pet/), собираются в один нативный
//! модуль для Python (через PyO3/maturin) и растут по одному куску за раз. См. ../README.md для
//! методологии и src/local_guard.rs для первого перенесённого куска.
//!
//! Структура НАВСЕГДА: каждый перенесённый Python-модуль `pet/xxx.py` получает здесь ровно один
//! файл `src/xxx.rs` и один подмодуль `yandi_rs.xxx` того же имени — так `from yandi_rs.xxx import
//! ...` в Python всегда зеркалит `from pet.xxx import ...`, и добавление следующего куска никогда
//! не требует переделывать то, что уже есть.

use pyo3::prelude::*;

pub mod boundaries;
pub mod claim_answer_linker;
pub mod claim_evidence_retriever;
pub mod claim_identity;
pub mod claim_semantic_identity_hardening;
pub mod claim_types;
pub mod claim_validator;
pub mod criticism_detector;
pub mod entity_resolver;
pub mod epistemic_router;
pub mod intent_router;
pub mod local_guard;
pub mod message_intensity;
pub mod object_resolver;
pub mod orch_risk;
pub mod personal_boundary;
pub mod scene_builder;
mod py_json;          // точный json.loads Python (для message_intensity)
mod py_printable_table; // данные: isprintable() для repr
mod py_case_table;    // данные: Lowercase/Uppercase/Titlecase для str.isupper(), см. gen_py_case_table.py
mod scene_builder_data; // данные: паттерны SceneBuilder, сгенерированы из Python
mod resolver_data;    // данные: таблицы object/entity resolver, сгенерированы из Python
mod py_icase_table;   // данные: группы IGNORECASE Python, см. gen_py_icase_table.py
mod py_regex;         // транслятор паттернов Python re -> крейт regex
mod py_decimal_table; // данные: цифры Unicode для py_float, см. gen_py_decimal_table.py
pub mod py_text;      // общие питоновские strip/split/\s/float — см. файл (+ подмодуль yandi_rs.py_text для проверки)
mod py_word_table; // данные (не подмодуль Python): точная копия Python-`\w`, см. gen_py_word_table.py
pub mod source_clustering;
pub mod source_quality;
pub mod target_router;
pub mod web_login;

#[pymodule]
fn yandi_rs(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let sys_modules = py.import_bound("sys")?.getattr("modules")?;

    let boundaries_mod = PyModule::new_bound(py, "boundaries")?;
    boundaries::register(py, &boundaries_mod)?;
    m.add_submodule(&boundaries_mod)?;

    let local_guard_mod = PyModule::new_bound(py, "local_guard")?;
    local_guard::register(py, &local_guard_mod)?;
    m.add_submodule(&local_guard_mod)?;

    let web_login_mod = PyModule::new_bound(py, "web_login")?;
    web_login::register(py, &web_login_mod)?;
    m.add_submodule(&web_login_mod)?;

    let claim_answer_linker_mod = PyModule::new_bound(py, "claim_answer_linker")?;
    claim_answer_linker::register(py, &claim_answer_linker_mod)?;
    m.add_submodule(&claim_answer_linker_mod)?;

    let claim_evidence_retriever_mod = PyModule::new_bound(py, "claim_evidence_retriever")?;
    claim_evidence_retriever::register(py, &claim_evidence_retriever_mod)?;
    m.add_submodule(&claim_evidence_retriever_mod)?;

    let claim_identity_mod = PyModule::new_bound(py, "claim_identity")?;
    claim_identity::register(py, &claim_identity_mod)?;
    m.add_submodule(&claim_identity_mod)?;

    let hardening_mod = PyModule::new_bound(py, "claim_semantic_identity_hardening")?;
    claim_semantic_identity_hardening::register(py, &hardening_mod)?;
    m.add_submodule(&hardening_mod)?;

    let source_quality_mod = PyModule::new_bound(py, "source_quality")?;
    source_quality::register(py, &source_quality_mod)?;
    m.add_submodule(&source_quality_mod)?;

    let claim_validator_mod = PyModule::new_bound(py, "claim_validator")?;
    claim_validator::register(py, &claim_validator_mod)?;
    m.add_submodule(&claim_validator_mod)?;

    let criticism_mod = PyModule::new_bound(py, "criticism_detector")?;
    criticism_detector::register(py, &criticism_mod)?;
    m.add_submodule(&criticism_mod)?;

    let claim_types_mod = PyModule::new_bound(py, "claim_types")?;
    claim_types::register(py, &claim_types_mod)?;
    m.add_submodule(&claim_types_mod)?;

    let epistemic_router_mod = PyModule::new_bound(py, "epistemic_router")?;
    epistemic_router::register(py, &epistemic_router_mod)?;
    m.add_submodule(&epistemic_router_mod)?;

    let message_intensity_mod = PyModule::new_bound(py, "message_intensity")?;
    message_intensity::register(py, &message_intensity_mod)?;
    m.add_submodule(&message_intensity_mod)?;

    let orch_risk_mod = PyModule::new_bound(py, "orch_risk")?;
    orch_risk::register(py, &orch_risk_mod)?;
    m.add_submodule(&orch_risk_mod)?;

    let source_clustering_mod = PyModule::new_bound(py, "source_clustering")?;
    source_clustering::register(py, &source_clustering_mod)?;
    m.add_submodule(&source_clustering_mod)?;

    let py_text_mod = PyModule::new_bound(py, "py_text")?;
    py_text::register(py, &py_text_mod)?;
    m.add_submodule(&py_text_mod)?;

    let intent_router_mod = PyModule::new_bound(py, "intent_router")?;
    intent_router::register(py, &intent_router_mod)?;
    m.add_submodule(&intent_router_mod)?;

    let target_router_mod = PyModule::new_bound(py, "target_router")?;
    target_router::register(py, &target_router_mod)?;
    m.add_submodule(&target_router_mod)?;

    let personal_boundary_mod = PyModule::new_bound(py, "personal_boundary")?;
    personal_boundary::register(py, &personal_boundary_mod)?;
    m.add_submodule(&personal_boundary_mod)?;

    let scene_builder_mod = PyModule::new_bound(py, "scene_builder")?;
    scene_builder::register(py, &scene_builder_mod)?;
    m.add_submodule(&scene_builder_mod)?;

    let object_resolver_mod = PyModule::new_bound(py, "object_resolver")?;
    object_resolver::register(py, &object_resolver_mod)?;
    m.add_submodule(&object_resolver_mod)?;

    let entity_resolver_mod = PyModule::new_bound(py, "entity_resolver")?;
    entity_resolver::register(py, &entity_resolver_mod)?;
    m.add_submodule(&entity_resolver_mod)?;

    // Чтобы `import yandi_rs.xxx` и `from yandi_rs.xxx import y` тоже работали (без этого
    // подмодуль виден только как атрибут yandi_rs.xxx, но не как отдельный элемент
    // sys.modules, что ломает некоторые формы импорта). Один и тот же шаг на каждый
    // будущий подмодуль — см. README.md.
    sys_modules.set_item("yandi_rs.boundaries", &boundaries_mod)?;
    sys_modules.set_item("yandi_rs.claim_answer_linker", &claim_answer_linker_mod)?;
    sys_modules.set_item("yandi_rs.local_guard", &local_guard_mod)?;
    sys_modules.set_item("yandi_rs.web_login", &web_login_mod)?;
    sys_modules.set_item("yandi_rs.claim_evidence_retriever", &claim_evidence_retriever_mod)?;
    sys_modules.set_item("yandi_rs.claim_identity", &claim_identity_mod)?;
    sys_modules.set_item("yandi_rs.claim_semantic_identity_hardening", &hardening_mod)?;
    sys_modules.set_item("yandi_rs.source_quality", &source_quality_mod)?;
    sys_modules.set_item("yandi_rs.claim_validator", &claim_validator_mod)?;
    sys_modules.set_item("yandi_rs.criticism_detector", &criticism_mod)?;
    sys_modules.set_item("yandi_rs.claim_types", &claim_types_mod)?;
    sys_modules.set_item("yandi_rs.epistemic_router", &epistemic_router_mod)?;
    sys_modules.set_item("yandi_rs.message_intensity", &message_intensity_mod)?;
    sys_modules.set_item("yandi_rs.orch_risk", &orch_risk_mod)?;
    sys_modules.set_item("yandi_rs.object_resolver", &object_resolver_mod)?;
    sys_modules.set_item("yandi_rs.entity_resolver", &entity_resolver_mod)?;
    sys_modules.set_item("yandi_rs.scene_builder", &scene_builder_mod)?;
    sys_modules.set_item("yandi_rs.personal_boundary", &personal_boundary_mod)?;
    sys_modules.set_item("yandi_rs.target_router", &target_router_mod)?;
    sys_modules.set_item("yandi_rs.intent_router", &intent_router_mod)?;
    sys_modules.set_item("yandi_rs.py_text", &py_text_mod)?;
    sys_modules.set_item("yandi_rs.source_clustering", &source_clustering_mod)?;
    Ok(())
}
