"""
agent/timeout_regression_test.py — P0-C regression.

Доказывает, что `step_timer()`/`run_with_timeout()` реально возвращают
управление вызывающему коду около заявленного timeout, а не ждут полного
завершения фоновой функции (баг: `with ThreadPoolExecutor(...) as ex:`
вызывал `shutdown(wait=True)` на выходе из блока, включая ветку
TimeoutError, из-за чего caller блокировался на реальное время работы
fn(), а не на объявленный timeout).

Запуск: /home/iam/venv/bin/python3 -m agent.timeout_regression_test
"""

import time

from agent.orch_timeout import step_timer, run_with_timeout

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def slow(seconds: float = 5.0):
    def _fn():
        time.sleep(seconds)
        return f"done after {seconds}s"
    return _fn


def fast():
    return "fast result"


# ── 1. step_timer: caller must NOT block for the full underlying duration ──

t_start = time.time()
result, dt, timed_out = step_timer("intent", slow(5.0), timeout=1)
wall = time.time() - t_start

check(
    "step_timer: caller wall-clock returns near declared timeout, not full fn duration",
    wall < 2.5,
    f"(wall={wall:.2f}s, declared timeout=1s, underlying fn=5s)",
)
check("step_timer: timed_out flag is True", timed_out is True)
check("step_timer: result is None on timeout", result is None)
check(
    "step_timer: reported dt is close to declared timeout, not full fn duration",
    dt < 2.5,
    f"(dt={dt:.2f}s)",
)

# ── 2. run_with_timeout: same contract, different call shape ──

t_start = time.time()
result2 = run_with_timeout("intent", slow(5.0), timeout=1, default="TIMEOUT_DEFAULT")
wall2 = time.time() - t_start

check(
    "run_with_timeout: caller wall-clock returns near declared timeout, not full fn duration",
    wall2 < 2.5,
    f"(wall={wall2:.2f}s)",
)
check("run_with_timeout: returns default value on timeout", result2 == "TIMEOUT_DEFAULT")

# ── 3. Non-regression: a fast function still returns its real result quickly ──

t_start = time.time()
result3, dt3, timed_out3 = step_timer("intent", fast, timeout=5)
wall3 = time.time() - t_start

check("step_timer: fast function returns its actual result", result3 == "fast result")
check("step_timer: fast function does not report timed_out", timed_out3 is False)
check("step_timer: fast function returns quickly", wall3 < 1.0, f"(wall={wall3:.2f}s)")

t_start = time.time()
result4 = run_with_timeout("intent", fast, timeout=5, default="SHOULD_NOT_APPEAR")
wall4 = time.time() - t_start

check("run_with_timeout: fast function returns its actual result", result4 == "fast result")
check("run_with_timeout: fast function returns quickly", wall4 < 1.0, f"(wall={wall4:.2f}s)")

# ── 4. Non-regression: a real exception (not a timeout) is still handled ──

def raises():
    raise ValueError("boom")

result5, dt5, timed_out5 = step_timer("intent", raises, timeout=5)
check("step_timer: real exception -> result is None", result5 is None)
check("step_timer: real exception -> timed_out is True (fail-safe default)", timed_out5 is True)

result6 = run_with_timeout("intent", raises, timeout=5, default="ERR_DEFAULT")
check("run_with_timeout: real exception -> default returned", result6 == "ERR_DEFAULT")

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
