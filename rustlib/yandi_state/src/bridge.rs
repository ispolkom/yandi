//! Мост для дифференциальных тестов против настоящего redis-server: `Store` как Python-объект, `execute(*argv) -> ответ` (bytes / int / None / list /
//! кортеж ("err", текст) / ("ok", текст) для простых строк), подписчики с `get_message`. Не для боевого использования.

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyList, PyTuple};
use std::sync::Arc;

use crate::pubsub::Subscription;
use crate::reply::Reply;
use crate::store::Store;

fn to_py(py: Python<'_>, r: &Reply) -> PyObject {
    match r {
        Reply::Nil => py.None(),
        Reply::Int(i) => i.into_py(py),
        Reply::Bulk(b) => PyBytes::new_bound(py, b).into_py(py),
        Reply::Status(s) => PyTuple::new_bound(py, ["ok".into_py(py), s.into_py(py)]).into_py(py),
        Reply::Error(e) => PyTuple::new_bound(py, ["err".into_py(py), e.into_py(py)]).into_py(py),
        Reply::Array(a) => PyList::new_bound(py, a.iter().map(|x| to_py(py, x))).into_py(py),
    }
}

#[pyclass]
struct PyStore {
    inner: Arc<Store>,
}

#[pymethods]
impl PyStore {
    #[new]
    fn new() -> Self {
        PyStore { inner: Arc::new(Store::new()) }
    }

    #[pyo3(signature = (*argv))]
    fn execute(&self, py: Python<'_>, argv: Vec<Vec<u8>>) -> PyObject {
        let r = py.allow_threads(|| self.inner.execute(&argv));
        to_py(py, &r)
    }

    fn pubsub(&self) -> PySub {
        PySub { inner: self.inner.new_subscription() }
    }
}

#[pyclass(unsendable)]
struct PySub {
    inner: Subscription,
}

#[pymethods]
impl PySub {
    fn subscribe(&self, channel: Vec<u8>) {
        self.inner.subscribe(&channel);
    }
    fn psubscribe(&self, pattern: Vec<u8>) {
        self.inner.psubscribe(&pattern);
    }
    fn unsubscribe(&self, channel: Vec<u8>) {
        self.inner.unsubscribe(&channel);
    }
    fn punsubscribe(&self, pattern: Vec<u8>) {
        self.inner.punsubscribe(&pattern);
    }
    /// (вид, шаблон|None, канал, данные) или None
    fn get_message(&self, py: Python<'_>) -> PyObject {
        match self.inner.try_recv() {
            None => py.None(),
            Some(m) => PyTuple::new_bound(
                py,
                [
                    m.kind.into_py(py),
                    m.pattern.map(|p| PyBytes::new_bound(py, &p).into_py(py)).unwrap_or_else(|| py.None()),
                    PyBytes::new_bound(py, &m.channel).into_py(py),
                    PyBytes::new_bound(py, &m.data).into_py(py),
                ],
            )
            .into_py(py),
        }
    }
}

#[pymodule]
fn yandi_state(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_class::<PyStore>()?;
    m.add_class::<PySub>()?;
    Ok(())
}
