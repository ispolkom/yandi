//! yandi_rs — Rust-переносы отдельных модулей YANDI (agent/, pet/), собираются в один нативный
//! модуль для Python (через PyO3/maturin) и растут по одному куску за раз. См. ../README.md для
//! методологии и src/local_guard.rs для первого перенесённого куска.
//!
//! Структура НАВСЕГДА: каждый перенесённый Python-модуль `pet/xxx.py` получает здесь ровно один
//! файл `src/xxx.rs` и один подмодуль `yandi_rs.xxx` того же имени — так `from yandi_rs.xxx import
//! ...` в Python всегда зеркалит `from pet.xxx import ...`, и добавление следующего куска никогда
//! не требует переделывать то, что уже есть.

use pyo3::prelude::*;

pub mod claim_identity;
pub mod claim_semantic_identity_hardening;
pub mod local_guard;
pub mod web_login;

#[pymodule]
fn yandi_rs(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let sys_modules = py.import_bound("sys")?.getattr("modules")?;

    let local_guard_mod = PyModule::new_bound(py, "local_guard")?;
    local_guard::register(py, &local_guard_mod)?;
    m.add_submodule(&local_guard_mod)?;

    let web_login_mod = PyModule::new_bound(py, "web_login")?;
    web_login::register(py, &web_login_mod)?;
    m.add_submodule(&web_login_mod)?;

    let claim_identity_mod = PyModule::new_bound(py, "claim_identity")?;
    claim_identity::register(py, &claim_identity_mod)?;
    m.add_submodule(&claim_identity_mod)?;

    let hardening_mod = PyModule::new_bound(py, "claim_semantic_identity_hardening")?;
    claim_semantic_identity_hardening::register(py, &hardening_mod)?;
    m.add_submodule(&hardening_mod)?;

    // Чтобы `import yandi_rs.xxx` и `from yandi_rs.xxx import y` тоже работали (без этого
    // подмодуль виден только как атрибут yandi_rs.xxx, но не как отдельный элемент
    // sys.modules, что ломает некоторые формы импорта). Один и тот же шаг на каждый
    // будущий подмодуль — см. README.md.
    sys_modules.set_item("yandi_rs.local_guard", &local_guard_mod)?;
    sys_modules.set_item("yandi_rs.web_login", &web_login_mod)?;
    sys_modules.set_item("yandi_rs.claim_identity", &claim_identity_mod)?;
    sys_modules.set_item("yandi_rs.claim_semantic_identity_hardening", &hardening_mod)?;
    Ok(())
}
