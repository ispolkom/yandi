#!/usr/bin/env python3
"""
assistant/council_scribe.py — секретарь совета.

Читает весь Council chat из Redis (council:chat:messages),
извлекает паттерны оркестровки и записывает:
  1. registry/dataset/orch_traces/YYYYMMDD.jsonl — обучающие примеры оркестратора
  2. registry/decisions/decisions.jsonl — архитектурные решения совета

Формат обучающих примеров оркестратора:
  action → question → action
  то есть: system(оркестратор) | user(задача) | assistant(решение: какую модель вызвать)

CLI:
  python3 assistant/council_scribe.py run       — прогнать через весь чат
  python3 assistant/council_scribe.py decisions — только решения
  python3 assistant/council_scribe.py stats     — статистика
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    import redis
    REDIS_OK = True
except ImportError:
    REDIS_OK = False

BASE          = Path(__file__).parent.parent
TRACES_DIR    = BASE / "registry" / "dataset" / "orch_traces"
DECISIONS_DIR = BASE / "registry" / "decisions"
TRACES_DIR.mkdir(parents=True, exist_ok=True)
DECISIONS_DIR.mkdir(parents=True, exist_ok=True)

DECISIONS_FILE = DECISIONS_DIR / "decisions.jsonl"
TODAY = datetime.now().strftime("%Y%m%d")
TRACES_FILE = TRACES_DIR / f"{TODAY}_council.jsonl"

ORCH_SYSTEM = (
    "Ты оркестратор задач в AI-совете (Council). "
    "Получаешь запрос пользователя или вопрос из дискуссии. "
    "Анализируешь тип задачи и возвращаешь JSON-решение: "
    "какую модель вызвать (claude/gpt/deepseek), "
    "какой скилл использовать и почему."
)

# Классификация задач по ключевым словам
TASK_CLASSIFIERS = {
    "architecture": ["архитектур", "pipeline", "структур", "схем", "паттерн", "дизайн"],
    "analysis":     ["анализ", "оценить", "сравни", "проверь", "насколько", "имеет смысл"],
    "reasoning":    ["почему", "как", "зачем", "объясни", "для чего", "каким образом"],
    "decision":     ["решени", "выбрать", "использовать", "делаем", "стоит ли", "лучше"],
    "knowledge":    ["реестр", "датасет", "база данных", "записать", "сохранить", "знани"],
    "code":         ["код", "rust", "python", "реализац", "implement", "функц"],
}

# Сильные стороны моделей — для объяснения routing
MODEL_STRENGTHS = {
    "claude":   "глубокий анализ, архитектурные решения, код",
    "gpt":      "структурированный ответ, практические рекомендации",
    "deepseek": "техническая точность, JSON/форматы, краткость",
    "human":    "постановка задачи, уточнение требований",
}


def _load_redis_chat() -> list[dict]:
    """Загрузить весь чат из Redis."""
    if not REDIS_OK:
        print("[scribe] redis-py не установлен, пробую через subprocess...")
        import subprocess
        result = subprocess.run(
            ["redis-cli", "LRANGE", "council:chat:messages", "0", "-1"],
            capture_output=True, text=True
        )
        msgs = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line: continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
        return msgs

    r = redis.Redis(decode_responses=True)
    raw_list = r.lrange("council:chat:messages", 0, -1)
    msgs = []
    for raw in raw_list:
        try:
            msgs.append(json.loads(raw))
        except Exception:
            pass
    return msgs


def _classify_task(text: str) -> str:
    """Определить тип задачи по тексту."""
    text_lower = text.lower()
    scores = {}
    for task_type, keywords in TASK_CLASSIFIERS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[task_type] = score
    if not scores:
        return "reasoning"
    return max(scores, key=lambda k: scores[k])


def _choose_model(frm: str, task_type: str) -> str:
    """Определить какую модель вызвал оркестратор (из кто ответил)."""
    if frm in ("claude", "gpt", "deepseek"):
        return frm
    return "claude"  # default


def _make_orch_decision(task_type: str, model: str, task_text: str) -> str:
    """Сформировать JSON-решение оркестратора."""
    strengths = MODEL_STRENGTHS.get(model, "общий анализ")
    return json.dumps({
        "skill":  task_type,
        "model":  model,
        "args":   {"task": task_text[:150]},
        "reason": f"Тип задачи: {task_type}. Модель {model} подходит: {strengths}.",
    }, ensure_ascii=False)


def _make_trace(
    task: str,
    task_type: str,
    model: str,
    result: str,
    context: str = "",
    outcome: str = "success",
) -> dict:
    """Создать обучающий пример для оркестратора."""
    ts = datetime.now()
    quality = 0.8 if outcome == "success" and len(result) > 100 else 0.6

    user_content = task
    if context:
        user_content = f"{task}\n\n[Контекст: {context[:200]}]"

    orch_decision = _make_orch_decision(task_type, model, task)

    return {
        "ts":         ts.isoformat(),
        "date":       ts.strftime("%Y-%m-%d"),
        "task":       task[:500],
        "task_type":  task_type,
        "model":      model,
        "result":     result[:1000],
        "outcome":    outcome,
        "elapsed_ms": 0,
        "quality":    quality,
        "steps":      1,
        "skill":      task_type,
        "source":     "council_chat",
        "messages": [
            {"role": "system",    "content": ORCH_SYSTEM},
            {"role": "user",      "content": user_content},
            {"role": "assistant", "content": orch_decision},
        ],
    }


def _extract_decisions(msgs: list[dict]) -> list[dict]:
    """Извлечь архитектурные решения из чата."""
    decisions = []
    decision_keywords = [
        "решили", "используем", "делаем", "выбрали", "принято",
        "optimistic", "multi-tier", "NetworkX", "реестр 1", "реестр 2",
        "верифицированное знание", "оркестратор", "pipeline",
        "два типа нод", "epistemic", "провенанс", "provenance",
    ]

    seen_hashes: set[str] = set()

    for m in msgs:
        text = m.get("text", "")
        frm = m.get("from", "?")
        if frm == "human":
            continue

        # Ищем тексты с решениями
        matches = [kw for kw in decision_keywords if kw.lower() in text.lower()]
        if len(matches) < 1:
            continue
        if len(text) < 80:
            continue

        # Дедупликация по хешу первых 100 символов
        h = hashlib.md5(text[:100].encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)

        # Извлечь суть решения — первый абзац
        paragraphs = [p.strip() for p in text.split("\n\n") if len(p.strip()) > 30]
        summary = paragraphs[0][:300] if paragraphs else text[:300]

        ts = m.get("_ts") or m.get("ts") or datetime.now().timestamp()
        try:
            ts_str = datetime.fromtimestamp(float(ts)).isoformat() if isinstance(ts, (int, float)) else str(ts)
        except Exception:
            ts_str = datetime.now().isoformat()

        dec_id = f"dec_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{h[:6]}"
        decisions.append({
            "id":      dec_id,
            "text":    summary,
            "from":    frm,
            "tags":    matches[:3],
            "source":  "council_chat",
            "status":  "open",
            "created": ts_str,
            "closed":  None,
            "outcome": None,
        })

    return decisions


def _extract_orch_traces(msgs: list[dict]) -> list[dict]:
    """
    Извлечь обучающие примеры оркестратора из цепочек сообщений.

    Паттерн 1: human → AI (оркестратор маршрутизировал human-запрос к AI)
    Паттерн 2: AI-вопрос → AI-ответ (оркестратор маршрутизировал вопрос между моделями)
    """
    traces = []
    seen: set[str] = set()

    for i, m in enumerate(msgs):
        frm = m.get("from", "?")
        text = m.get("text", "")

        if len(text) < 30:
            continue

        # Паттерн 1: human-запрос → AI-ответ
        if frm == "human" and i + 1 < len(msgs):
            next_m = msgs[i + 1]
            responder = next_m.get("from", "?")
            if responder in ("claude", "gpt", "deepseek"):
                task = text[:400]
                result = next_m.get("text", "")[:600]
                task_type = _classify_task(task)

                h = hashlib.md5(task[:80].encode()).hexdigest()[:8]
                if h in seen:
                    continue
                seen.add(h)

                # Контекст — предыдущее сообщение
                ctx = msgs[i - 1].get("text", "")[:150] if i > 0 else ""

                trace = _make_trace(
                    task=task,
                    task_type=task_type,
                    model=responder,
                    result=result,
                    context=ctx,
                )
                traces.append(trace)

        # Паттерн 2: AI задаёт вопрос → другой AI отвечает
        if frm in ("claude", "gpt", "deepseek") and "?" in text and i + 1 < len(msgs):
            next_m = msgs[i + 1]
            responder = next_m.get("from", "?")
            if responder in ("claude", "gpt", "deepseek") and responder != frm:
                # Найти вопросительное предложение
                q_lines = [s.strip() for s in text.split("\n") if "?" in s and len(s.strip()) > 20]
                if not q_lines:
                    continue
                question = q_lines[0][:300]
                result = next_m.get("text", "")[:600]
                task_type = _classify_task(question)

                h = hashlib.md5(question[:80].encode()).hexdigest()[:8]
                if h in seen:
                    continue
                seen.add(h)

                ctx = f"Вопрос от {frm} к совету"
                trace = _make_trace(
                    task=question,
                    task_type=task_type,
                    model=responder,
                    result=result,
                    context=ctx,
                )
                traces.append(trace)

    return traces


def run_scribe() -> dict:
    """Основной метод: прогнать секретаря через весь чат."""
    print(f"[scribe] Загружаю чат из Redis...")
    msgs = _load_redis_chat()
    print(f"[scribe] Загружено: {len(msgs)} сообщений")

    # Извлечь трейсы оркестратора
    traces = _extract_orch_traces(msgs)
    print(f"[scribe] Извлечено трейсов оркестратора: {len(traces)}")

    # Записать трейсы
    with open(TRACES_FILE, "w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"[scribe] Трейсы записаны → {TRACES_FILE}")

    # Извлечь решения совета
    decisions = _extract_decisions(msgs)
    print(f"[scribe] Извлечено решений совета: {len(decisions)}")

    # Загрузить существующие решения (дедупликация)
    existing_decisions: list[dict] = []
    existing_texts: set[str] = set()
    if DECISIONS_FILE.exists():
        for line in DECISIONS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                d = json.loads(line)
                existing_decisions.append(d)
                existing_texts.add(d.get("text", "")[:80])
            except Exception:
                pass

    # Добавить только новые решения
    new_decisions = [d for d in decisions if d["text"][:80] not in existing_texts]
    print(f"[scribe] Новых решений (не дубликатов): {len(new_decisions)}")

    with open(DECISIONS_FILE, "w", encoding="utf-8") as f:
        for d in existing_decisions + new_decisions:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
    print(f"[scribe] Решения записаны → {DECISIONS_FILE}")

    return {
        "messages":      len(msgs),
        "traces":        len(traces),
        "decisions_new": len(new_decisions),
        "decisions_total": len(existing_decisions) + len(new_decisions),
        "traces_file":   str(TRACES_FILE),
        "decisions_file": str(DECISIONS_FILE),
    }


def show_stats() -> None:
    """Показать статистику."""
    traces_files = list(TRACES_DIR.glob("*.jsonl"))
    total_traces = 0
    by_model: dict[str, int] = {}
    by_type: dict[str, int] = {}

    for f in traces_files:
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            try:
                t = json.loads(line)
                total_traces += 1
                model = t.get("model", "?")
                task_type = t.get("task_type", "?")
                by_model[model] = by_model.get(model, 0) + 1
                by_type[task_type] = by_type.get(task_type, 0) + 1
            except Exception:
                pass

    decisions_count = 0
    if DECISIONS_FILE.exists():
        decisions_count = sum(1 for l in DECISIONS_FILE.read_text().splitlines() if l.strip())

    print(f"=== Статистика секретаря ===")
    print(f"Файлов трейсов: {len(traces_files)}")
    print(f"Трейсов оркестратора: {total_traces}")
    print(f"  по моделям:   {by_model}")
    print(f"  по типам:     {by_type}")
    print(f"Решений совета: {decisions_count}")


def show_decisions(n: int = 10) -> None:
    """Показать последние N решений."""
    if not DECISIONS_FILE.exists():
        print("Файл решений не найден")
        return

    decisions = []
    for line in DECISIONS_FILE.read_text(encoding="utf-8").splitlines():
        if not line.strip(): continue
        try:
            decisions.append(json.loads(line))
        except Exception:
            pass

    print(f"=== Решения совета ({len(decisions)} всего) ===\n")
    for d in decisions[-n:]:
        tags = ", ".join(d.get("tags", []))
        frm = d.get("from", "?")
        print(f"[{frm}] {d.get('text','')[:120]}")
        print(f"  теги: {tags}  статус: {d.get('status','?')}")
        print()


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sub = sys.argv[1] if len(sys.argv) > 1 else "run"

    if sub == "run":
        result = run_scribe()
        print(f"\n=== Итог ===")
        print(f"Сообщений в чате:    {result['messages']}")
        print(f"Трейсов создано:     {result['traces']}")
        print(f"Решений (новых):     {result['decisions_new']}")
        print(f"Решений (всего):     {result['decisions_total']}")
        print(f"Файл трейсов:        {result['traces_file']}")
        print(f"Файл решений:        {result['decisions_file']}")

    elif sub == "decisions":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        show_decisions(n)

    elif sub == "stats":
        show_stats()

    else:
        print("Команды: run, decisions [N], stats")
