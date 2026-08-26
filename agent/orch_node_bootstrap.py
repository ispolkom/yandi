"""
assistant/orch_node_bootstrap.py — Node Bootstrap.
Новая нода входит в сеть → скачивает архив запросов кластера →
прогоняет каждый через оркестратор → накапливает трейсы → fine-tune.

Режимы:
  foreground  — ждёт завершения
  background  — daemon thread, не блокирует основной процесс

CLI:
  python3 assistant/orch_node_bootstrap.py run <tag> [max_queries]
  python3 assistant/orch_node_bootstrap.py status
  python3 assistant/orch_node_bootstrap.py list   — какие теги доступны для bootstrap
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

BOOTSTRAP_STATE_FILE = BASE / "registry" / "query_archive" / "bootstrap_state.json"

# Минимум трейсов для запуска fine-tuning
MIN_TRACES_FOR_FINETUNE = 100


class BootstrapState:
    """Персистентное состояние bootstrap-процесса."""

    def __init__(self):
        self._data: dict = self._load()

    def _load(self) -> dict:
        if BOOTSTRAP_STATE_FILE.exists():
            try:
                return json.loads(BOOTSTRAP_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save(self):
        BOOTSTRAP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOOTSTRAP_STATE_FILE.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def start(self, tag: str, total: int):
        self._data[tag] = {
            "status":    "running",
            "tag":       tag,
            "total":     total,
            "processed": 0,
            "traces":    0,
            "errors":    0,
            "started_at": time.time(),
            "updated_at": time.time(),
        }
        self._save()

    def update(self, tag: str, processed: int, traces: int, errors: int):
        if tag in self._data:
            self._data[tag].update({
                "processed":  processed,
                "traces":     traces,
                "errors":     errors,
                "updated_at": time.time(),
            })
            self._save()

    def finish(self, tag: str, status: str = "done"):
        if tag in self._data:
            self._data[tag]["status"]      = status
            self._data[tag]["finished_at"] = time.time()
            self._save()

    def get(self, tag: str) -> Optional[dict]:
        return self._data.get(tag)

    def all(self) -> dict:
        return self._data


_state = BootstrapState()


def bootstrap(
    tag: str,
    max_queries: int = 200,
    min_confidence: float = 0.3,
    verbose: bool = True,
    enable_web: bool = False,
) -> dict:
    """
    Загрузить архив запросов по тегу и прогнать через оркестратор.

    Args:
        tag:            тег кластера (авто:ремонт:тормоза)
        max_queries:    максимум запросов для обработки
        min_confidence: минимальная уверенность запроса в архиве
        verbose:        логировать прогресс
        enable_web:     разрешить веб-поиск при bootstrap

    Returns:
        dict со статистикой: processed, traces, errors
    """
    from agent.orch_query_archive import get_queries
    from agent.orch_schemas import OrchestratorRequest
    from agent.orchestrator_v2 import process
    from agent.orch_tracer import OrchestratorTracer

    def log(msg: str):
        if verbose:
            print(msg, flush=True)

    # Загрузить запросы из архива
    queries = get_queries(tag, limit=max_queries, min_confidence=min_confidence)
    if not queries:
        log(f"[Bootstrap:{tag}] Архив пуст — нечего обрабатывать")
        return {"processed": 0, "traces": 0, "errors": 0, "status": "empty"}

    log(f"[Bootstrap:{tag}] Загружено {len(queries)} запросов из архива")
    _state.start(tag, len(queries))

    processed = 0
    traces    = 0
    errors    = 0

    for i, item in enumerate(queries):
        query = item.get("query", "")
        if not query or len(query) < 5:
            continue

        log(f"  [{i+1}/{len(queries)}] {query[:60]}...")

        try:
            req  = OrchestratorRequest(query=query)
            resp = process(
                req,
                verbose=False,
                enable_web=enable_web,
                enable_validation=False,
            )
            processed += 1
            traces    += 1  # orch_tracer.py пишет трейс автоматически в process()
            log(f"    ✓ trust={resp.trust_level} conf≈{len(resp.answer)//10*10}chars")
        except Exception as e:
            errors += 1
            log(f"    ✗ Ошибка: {e}")

        _state.update(tag, processed, traces, errors)

        # Небольшая пауза чтобы не перегревать GPU
        time.sleep(1)

    # Проверить — достаточно ли трейсов для fine-tuning
    status = "done"
    if traces >= MIN_TRACES_FOR_FINETUNE:
        log(f"\n[Bootstrap:{tag}] ✓ {traces} трейсов — достаточно для fine-tuning!")
        log(f"  Запустите: python3 assistant/orch_finetune.py train")
        status = "done_ready_for_finetune"
    else:
        log(f"\n[Bootstrap:{tag}] {traces} трейсов (нужно {MIN_TRACES_FOR_FINETUNE} для fine-tuning)")

    _state.finish(tag, status)

    return {
        "tag":       tag,
        "processed": processed,
        "traces":    traces,
        "errors":    errors,
        "status":    status,
    }


def bootstrap_background(
    tag: str,
    max_queries: int = 200,
    **kwargs,
) -> threading.Thread:
    """Запустить bootstrap в фоновом треде."""
    t = threading.Thread(
        target=bootstrap,
        args=(tag, max_queries),
        kwargs={"verbose": True, **kwargs},
        daemon=False,
        name=f"bootstrap-{tag}",
    )
    t.start()
    return t


def get_status() -> dict:
    """Статус всех bootstrap-процессов."""
    return _state.all()


def suggest_bootstrap_targets() -> list[dict]:
    """
    Найти теги которые стоит бутстрапнуть:
    - Есть запросы в архиве
    - Мало трейсов в orch_traces для этого тега
    """
    from agent.orch_query_archive import get_all_tags, get_tag_stats
    from agent.orch_tracer import OrchestratorTracer

    tracer    = OrchestratorTracer()
    tr_stats  = tracer.stats()
    by_skill  = tr_stats.get("by_skill", {})

    targets = []
    for tag in get_all_tags():
        archive_stats = get_tag_stats(tag)
        archive_count = archive_stats.get("count", 0)
        if archive_count < 5:
            continue
        # Ищем соответствующий скилл в трейсах
        leaf = tag.split(":")[-1]
        trace_count = by_skill.get(tag, by_skill.get(leaf, 0))
        targets.append({
            "tag":          tag,
            "archive":      archive_count,
            "traces":       trace_count,
            "priority":     archive_count - trace_count * 2,
        })

    return sorted(targets, key=lambda x: -x["priority"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "run":
        tag         = sys.argv[2] if len(sys.argv) > 2 else "general"
        max_queries = int(sys.argv[3]) if len(sys.argv) > 3 else 50
        print(f"Bootstrap: тег={tag}, max={max_queries}")
        result = bootstrap(tag, max_queries=max_queries, verbose=True)
        print(f"\nРезультат: {result}")

    elif cmd == "status":
        statuses = get_status()
        if not statuses:
            print("Bootstrap не запускался")
        else:
            for tag, st in statuses.items():
                pct = int(st.get("processed", 0) / max(st.get("total", 1), 1) * 100)
                print(f"  {tag}: {st['status']} | {st.get('processed',0)}/{st.get('total',0)} ({pct}%) | traces={st.get('traces',0)}")

    elif cmd == "list":
        targets = suggest_bootstrap_targets()
        if not targets:
            print("Нет доступных тегов для bootstrap (архив пуст)")
        else:
            print("Рекомендуемые теги для bootstrap:")
            for t in targets[:10]:
                print(f"  {t['tag']}: archive={t['archive']} traces={t['traces']} priority={t['priority']}")

    else:
        print("Команды: run <tag> [max], status, list")
