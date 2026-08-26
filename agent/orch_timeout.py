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
    """
    t = timeout or DEFAULT_TIMEOUTS.get(step, 60)
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            result = future.result(timeout=t)
            return result
        except concurrent.futures.TimeoutError:
            elapsed = time.time() - t0
            print(f"[timeout] step={step} timeout={t}s elapsed={elapsed:.1f}s")
            return default
        except Exception as e:
            elapsed = time.time() - t0
            print(f"[timeout] step={step} error={e} elapsed={elapsed:.1f}s")
            return default


def step_timer(step: StepName, fn: Callable[[], T], timeout: int | None = None) -> tuple[T | None, float, bool]:
    """
    Выполнить шаг и вернуть (результат, время, timed_out).
    """
    t = timeout or DEFAULT_TIMEOUTS.get(step, 60)
    t0 = time.time()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        future = ex.submit(fn)
        try:
            result = future.result(timeout=t)
            return result, time.time() - t0, False
        except concurrent.futures.TimeoutError:
            return None, time.time() - t0, True
        except Exception as e:
            print(f"[timeout] step={step} error={e}")
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
