"""
executor.py — выполняет пошаговый план агента.

Использование:
    from agent.tools.executor import execute_plan, run_task

    # Выполнить готовый план
    result = execute_plan(steps=[
        {"step":1, "tool":"fs.ls", "args":{"path":"agent"}, "description":"смотрим структуру"},
        {"step":2, "tool":"shell.run", "args":{"cmd":"pytest agent/tools/test_tools.py"}, "description":"тест"},
    ])

    # Полный цикл: задача → план → выполнение → проверка
    result = run_task("Проверь что все tools работают и напиши отчёт в registry/decisions/check.md")
"""
import json
import time
from datetime import datetime
from pathlib import Path

from agent.tools import tool, list_tools

PROJECT_ROOT = Path(__file__).parent.parent.parent


def execute_plan(steps: list[dict], stop_on_fail: bool = True) -> dict:
    """Выполнить список шагов. Возвращает итог и историю выполнения."""
    log = []
    context = {}  # результаты шагов доступны следующим шагам

    for step in steps:
        n           = step.get("step", "?")
        tool_name   = step.get("tool", "")
        args        = step.get("args") or {}
        description = step.get("description", "")

        # Подстановка результатов предыдущих шагов: {{step_1_result}}
        args = _interpolate(args, context)

        print(f"  [{n}] {tool_name} — {description}")
        t0 = time.time()

        result = tool(tool_name, **args)
        elapsed = round(time.time() - t0, 2)

        ok = result.get("ok", True) if isinstance(result, dict) else True
        entry = {
            "step": n, "tool": tool_name, "description": description,
            "ok": ok, "elapsed": elapsed, "result": result,
        }
        log.append(entry)
        context[f"step_{n}_result"] = result

        if not ok and stop_on_fail:
            print(f"  ❌ Шаг {n} упал: {result.get('error','')}")
            return {"ok": False, "failed_step": n, "log": log, "context": context}

        print(f"  {'✅' if ok else '⚠️'} {elapsed}s")

    return {"ok": True, "steps_total": len(steps), "log": log, "context": context}


def run_task(task: str, context: str = "", model: str = "heretic:q8",
             max_retries: int = 2, save_result: bool = True) -> dict:
    """Полный agentic loop: задача → план → выполнение → проверка."""
    print(f"\n🤖 Задача: {task}")
    started = datetime.now()

    # 1. Строим план
    print("📋 Строим план...")
    plan_result = tool("ai.build_plan", task=task, context=context)
    if not plan_result.get("ok"):
        elapsed = round((datetime.now() - started).total_seconds(), 1)
        return {"ok": False, "stage": "planning", "elapsed": elapsed, "steps": 0,
                "verdict": f"Планировщик не смог построить план: {plan_result.get('error','')}",
                "error": plan_result.get("error"), "raw": plan_result.get("raw")}

    steps = plan_result["steps"]
    print(f"   {len(steps)} шагов")

    # 2. Выполняем план (с retry при ошибках)
    exec_result = None
    for attempt in range(1, max_retries + 1):
        print(f"\n⚙️  Выполнение (попытка {attempt}/{max_retries})...")
        exec_result = execute_plan(steps)
        if exec_result["ok"]:
            break
        # При ошибке просим AI пересмотреть план
        if attempt < max_retries:
            failed = exec_result.get("failed_step")
            error  = exec_result["log"][-1]["result"].get("error", "") if exec_result["log"] else ""
            print(f"   Пересматриваем план (шаг {failed} упал: {error})...")
            retry_ctx = f"{context}\n\nПопытка {attempt} провалилась на шаге {failed}: {error}"
            plan_result = tool("ai.build_plan", task=task, context=retry_ctx)
            if plan_result.get("ok"):
                steps = plan_result["steps"]

    # 3. Проверяем результат
    print("\n🔍 Проверка результата...")
    summary = _summarize(exec_result)
    review  = tool("ai.review", task=task, result=summary)
    verdict = review.get("content", "")
    passed  = "ok" in verdict.lower() and "fail" not in verdict.lower()
    print(f"   {'✅ OK' if passed else '❌ FAIL'}: {verdict[:100]}")

    # 4. Сохраняем в registry/decisions/
    elapsed = round((datetime.now() - started).total_seconds(), 1)
    final = {
        "ok": passed, "task": task, "elapsed": elapsed,
        "steps": len(steps), "verdict": verdict,
        "exec": exec_result, "plan_raw": plan_result.get("raw", ""),
    }
    if save_result:
        _save_decision(task, final, started)

    print(f"\n{'✅' if passed else '❌'} Готово за {elapsed}s")
    return final


def _interpolate(args: dict, context: dict) -> dict:
    """Подставляет {{step_N_result}} в строковые аргументы."""
    result = {}
    for k, v in args.items():
        if isinstance(v, str):
            for key, val in context.items():
                v = v.replace(f"{{{{{key}}}}}", json.dumps(val) if not isinstance(val, str) else val)
        result[k] = v
    return result


def _summarize(exec_result: dict) -> str:
    lines = []
    for entry in exec_result.get("log", []):
        r = entry["result"]
        if isinstance(r, str):
            lines.append(f"Шаг {entry['step']} ({entry['tool']}): {r[:200]}")
        elif isinstance(r, dict):
            lines.append(f"Шаг {entry['step']} ({entry['tool']}): ok={r.get('ok', True)}, "
                         f"{str(r)[:200]}")
        elif isinstance(r, list):
            lines.append(f"Шаг {entry['step']} ({entry['tool']}): [{len(r)} элементов]")
    return "\n".join(lines)


def _save_decision(task: str, result: dict, started: datetime):
    try:
        dec_dir = PROJECT_ROOT / "registry" / "decisions"
        dec_dir.mkdir(parents=True, exist_ok=True)
        fname = dec_dir / f"{started.strftime('%Y%m%d_%H%M%S')}_task.json"
        fname.write_text(json.dumps({
            "task": task, "started": started.isoformat(),
            "ok": result["ok"], "elapsed": result["elapsed"],
            "verdict": result["verdict"], "steps": result["steps"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"  [warn] Не удалось сохранить решение: {e}")
