"""
assistant/orch_timeout.py — Timeout Manager.
Враппер с таймаутом для каждого шага оркестратора.
"""
from __future__ import annotations

import concurrent.futures
import time
from typing import Any, Callable, TypeVar

from agent.orch_schemas import StepError, StepName

T = TypeVar("T")

# Таймауты по умолчанию для каждого шага (секунды)
DEFAULT_TIMEOUTS: dict[str, int] = {
    "cache_check":        5,
    "risk_assess":        2,
    "plan":              30,
    "intent":            60,
    "clarify":           30,
    "enrich":            60,
    "local_search":      15,
    "web_query":         60,
    "web_scrape":        30,
    "synthesize":       200,
    "optimistic_respond": 2,
    "validate":          90,
    "arbitrate":         90,
}



# P0-C (YANDI autonomous fix pass): `with ThreadPoolExecutor(...) as ex:`
# calls `ex.__exit__` -> `shutdown(wait=True)` on the way out of the
# `with` block — including on the `TimeoutError` branch below, whose
# `return` statement still has to pass through that `__exit__` before
# control reaches the caller. `shutdown(wait=True)` blocks until the
# already-running worker thread actually finishes, so a declared
# `timeout=N` never bounded real wall-clock latency: the caller got
# back control only after the underlying `fn()` call itself completed,
# however long that took. Reproduced deterministically (declared
# timeout=2s, fn sleeps 5s -> caller unblocked only after ~5s, not ~2s).
#
# Fix: build the executor without `with`, `shutdown(wait=False)`
# explicitly on every exit path. The orphaned thread is NOT killed
# (Python cannot safely kill a running thread) — it keeps running
# in the background and is only reclaimed by CPython's atexit
# thread-join machinery when the whole process actually exits. For a
# per-query process (this orchestrator's CLI usage) that means: the
# *response* is no longer held hostage by a slow step, but the
# process itself may still take a bit longer to exit if a background
# call is still in flight when the query finishes — an acceptable,
# bounded trade-off, not a hang on the user-facing path.
#
# Danger this does NOT solve (documented, not silently hidden):
# an orphaned background call that later acquires GENERATION_SEMAPHORE,
# writes to a shared registry file, or mutates global state can still
# do so *after* the caller has moved on — soft timeout only stops the
# caller from waiting, it does not stop the background work. Hard
# cancellation would require a process/subprocess boundary (so the OS
# can actually kill it), which is a materially bigger change than this
# pass — not done here. The affected callers here (`build_plan`,
# `analyze_intent`, and generally anything wrapped by `step_timer`/
# `run_with_timeout`) are I/O-bound HTTP calls to a local Ollama, not
# uncooperative CPU-bound loops, so a soft timeout is the right level
# for this pass; a genuinely runaway CPU-bound step would need the
# subprocess boundary instead.


def run_with_timeout(
    step: StepName,
    fn: Callable[[], T],
    timeout: int | None = None,
    default: Any = None,
) -> T | Any:
    """
    Выполнить функцию с таймаутом.

    Args:
        step:    имя шага (для логирования и дефолтного таймаута)
        fn:      функция без аргументов
        timeout: таймаут в секундах (None = дефолт из DEFAULT_TIMEOUTS)
        default: значение при таймауте или ошибке

    Returns:
        Результат fn() или default при таймауте/ошибке

    ВАЖНО: при таймауте фоновый поток НЕ убивается (Python не может
    безопасно убить поток) — он может продолжить работать после того,
    как эта функция уже вернула `default` вызывающему коду.
    """
    t = timeout or DEFAULT_TIMEOUTS.get(step, 60)
    t0 = time.time()

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = ex.submit(fn)
    try:
        result = future.result(timeout=t)
        ex.shutdown(wait=False)
        return result
    except concurrent.futures.TimeoutError:
        elapsed = time.time() - t0
        print(
            f"[Timeout] step={step} limit={t}s "
            f"returned_after={elapsed:.2f}s background_may_continue=True"
        )
        ex.shutdown(wait=False)
        return default
    except Exception as e:
        elapsed = time.time() - t0
        print(f"[timeout] step={step} error={e} elapsed={elapsed:.1f}s")
        ex.shutdown(wait=False)
        return default


def step_timer(step: StepName, fn: Callable[[], T], timeout: int | None = None) -> tuple[T | None, float, bool]:
    """
    Выполнить шаг и вернуть (результат, время, timed_out).

    ВАЖНО: при таймауте фоновый поток НЕ убивается (Python не может
    безопасно убить поток) — он может продолжить работать после того,
    как эта функция уже вернула управление вызывающему коду.
    """
    t = timeout or DEFAULT_TIMEOUTS.get(step, 60)
    t0 = time.time()

    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = ex.submit(fn)
    try:
        result = future.result(timeout=t)
        ex.shutdown(wait=False)
        return result, time.time() - t0, False
    except concurrent.futures.TimeoutError:
        elapsed = time.time() - t0
        print(
            f"[Timeout] step={step} limit={t}s "
            f"returned_after={elapsed:.2f}s background_may_continue=True"
        )
        ex.shutdown(wait=False)
        return None, elapsed, True
    except Exception as e:
        print(f"[timeout] step={step} error={e}")
        ex.shutdown(wait=False)
        return None, time.time() - t0, True


if __name__ == "__main__":
    import time as _time

    def slow():
        _time.sleep(5)
        return "done"

    def fast():
        return "fast result"

    r1 = run_with_timeout("intent", fast, timeout=3, default="TIMEOUT")
    print(f"fast: {r1}")

    r2 = run_with_timeout("intent", slow, timeout=2, default="TIMEOUT")
    print(f"slow: {r2}")
