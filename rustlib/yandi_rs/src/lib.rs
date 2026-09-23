//! yandi_rs — Rust-переносы отдельных модулей YANDI (agent/, pet/), собираются в один нативный
//! модуль для Python (через PyO3/maturin) и растут по одному куску за раз. См. ../README.md для
//! методологии и src/local_guard.rs для первого перенесённого куска.
//!
//! Структура НАВСЕГДА: каждый перенесённый Python-модуль `pet/xxx.py` получает здесь ровно один
//! файл `src/xxx.rs` и один подмодуль `yandi_rs.xxx` того же имени — так `from yandi_rs.xxx import
//! ...` в Python всегда зеркалит `from pet.xxx import ...`, и добавление следующего куска никогда
//! не требует переделывать то, что уже есть.

use pyo3::prelude::*;

pub mod local_guard;

#[pymodule]
fn yandi_rs(py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    let local_guard_mod = PyModule::new_bound(py, "local_guard")?;
    local_guard::register(py, &local_guard_mod)?;
    m.add_submodule(&local_guard_mod)?;
    // Чтобы `import yandi_rs.local_guard` и `from yandi_rs.local_guard import x` тоже работали
    // (без этого подмодуль виден только как атрибут yandi_rs.local_guard, но не как отдельный
    // элемент sys.modules, что ломает некоторые формы импорта).
    py.import_bound("sys")?
        .getattr("modules")?
        .set_item("yandi_rs.local_guard", &local_guard_mod)?;
    Ok(())
}
