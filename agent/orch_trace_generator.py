"""
assistant/orch_trace_generator.py — Synthetic Trace Generator.
Генерирует обучающие трейсы для оркестратора из существующих данных реестра.

Источники:
  - registry/dataset/model_sessions/  — ответы Claude/GPT/DeepSeek
  - registry/verified_knowledge/      — верифицированные знания
  - registry/council/                 — Council сессии

Выход:
  - registry/dataset/orch_traces/synthetic_YYYYMMDD.jsonl

CLI:
  python3 assistant/orch_trace_generator.py generate [N]   — сгенерировать N трейсов
  python3 assistant/orch_trace_generator.py stats          — статистика
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime
from pathlib import Path

BASE       = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

SESSIONS_DIR  = BASE / "registry" / "dataset" / "model_sessions"
KNOWLEDGE_DIR = BASE / "registry" / "verified_knowledge"
COUNCIL_DIR   = BASE / "registry" / "council"
TRACES_DIR    = BASE / "registry" / "dataset" / "orch_traces"
TRACES_DIR.mkdir(parents=True, exist_ok=True)

SYSTEM_PROMPT = (
    "Ты оркестратор задач. Получаешь запрос пользователя и контекст сессии. "
    "Анализируешь и возвращаешь JSON-решение: какой скилл использовать, "
    "с какими параметрами и почему."
)

# Шаблоны вопросов по доменам
QUESTION_TEMPLATES = {
    "tech": [
        "Как работает {concept}?",
        "Объясни принцип {concept}",
        "Что такое {concept} в контексте {context}?",
        "Чем отличается {concept} от {alt}?",
        "Как реализовать {concept} на практике?",
    ],
    "ai_ml": [
        "Как обучить {concept}?",
        "Что такое {concept} в машинном обучении?",
        "Как работает {concept}?",
        "Когда применять {concept}?",
        "Сравни {concept} и {alt}",
    ],
    "coding": [
        "Как написать {concept} на Python?",
        "Реализуй {concept}",
        "Исправь ошибку в коде: {concept}",
        "Объясни паттерн {concept}",
    ],
    "general": [
        "Расскажи о {concept}",
        "Что такое {concept}?",
        "Как {concept} используется в {context}?",
    ],
}

# Концепции для генерации вопросов
CONCEPTS = {
    "tech": [
        ("DHT", "P2P-сетях"), ("Kademlia", "маршрутизации"), ("XOR-метрика", "Kademlia"),
        ("FAISS", "векторном поиске"), ("репутация нод", "P2P"), ("оркестратор", "AI-системах"),
        ("federated learning", "приватности"), ("RAG", "языковых моделях"),
        ("embedding", "семантическом поиске"), ("consensus", "распределённых системах"),
    ],
    "ai_ml": [
        ("LoRA", "fine-tuning"), ("SFT", "обучении LLM"), ("RLHF", "выравнивании"),
        ("CoT", "рассуждении"), ("attention", "трансформерах"), ("quantization", "моделях"),
        ("RAG", "retrieval"), ("multi-agent", "системах"),
    ],
    "coding": [
        ("async/await", "Python"), ("WebSocket", "FastAPI"), ("Redis pubsub", "очередях"),
        ("FAISS index", "Python"), ("pydantic", "валидации"), ("ThreadPoolExecutor", "параллелизме"),
    ],
    "general": [
        ("P2P-сеть",), ("распределённые системы",), ("AI-совет",), ("оркестрация задач",),
    ],
}

RISK_LEVELS  = ["low", "medium", "high", "critical"]
RISK_WEIGHTS = [0.6, 0.25, 0.1, 0.05]
TRUST_LEVELS = ["VERIFIED", "HYPOTHESIS", "UNVERIFIED", "PARTIALLY_VERIFIED"]
TRUST_WEIGHTS= [0.3, 0.3, 0.25, 0.15]


def _load_from_sessions() -> list[dict]:
    """Загрузить реальные Q&A из model_sessions."""
    items = []
    for f in SESSIONS_DIR.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                q = d.get("question") or d.get("query") or d.get("task", "")
                a = d.get("answer") or d.get("response") or d.get("result", "")
                topic = d.get("topic", "general")
                if q and a and len(a) > 50:
                    items.append({"question": q, "answer": a, "topic": topic, "source": str(f.name)})
            except Exception:
                pass
    return items


def _load_from_knowledge() -> list[dict]:
    """Загрузить верифицированные знания."""
    items = []
    kf = KNOWLEDGE_DIR / "knowledge.jsonl"
    if not kf.exists():
        return items
    for line in kf.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            q = d.get("question", "")
            a = d.get("answer", "")
            if q and a and len(a) > 50:
                items.append({
                    "question": q, "answer": a,
                    "topic":    d.get("topic", "general"),
                    "source":   "verified_knowledge",
                    "verified": True,
                })
        except Exception:
            pass
    return items


def _generate_synthetic(n: int, domain: str = "tech") -> list[dict]:
    """Сгенерировать синтетические Q&A пары для домена."""
    items = []
    templates = QUESTION_TEMPLATES.get(domain, QUESTION_TEMPLATES["general"])
    concepts  = CONCEPTS.get(domain, CONCEPTS["general"])

    for _ in range(n):
        concept_data = random.choice(concepts)
        concept = concept_data[0]
        context = concept_data[1] if len(concept_data) > 1 else "системах"
        alt     = random.choice([c[0] for c in concepts if c[0] != concept])

        tpl = random.choice(templates)
        q   = tpl.format(concept=concept, context=context, alt=alt)

        answer = f"{concept} — это важный компонент {context}. Понимание {concept} необходимо для эффективной работы с {context}."
        items.append({"question": q, "answer": answer, "topic": domain, "source": "synthetic"})

    return items


def _make_trace(item: dict, idx: int) -> dict:
    """Преобразовать Q&A пару в обучающий трейс."""
    question = item["question"]
    answer   = item["answer"]
    topic    = item.get("topic", "general")
    verified = item.get("verified", False)
    source   = item.get("source", "unknown")

    # Симулировать решение оркестратора
    risk_level = random.choices(RISK_LEVELS, RISK_WEIGHTS)[0]
    trust_level = "VERIFIED" if verified else random.choices(TRUST_LEVELS, TRUST_WEIGHTS)[0]
    confidence = round(random.uniform(0.5, 0.95) if verified else random.uniform(0.3, 0.8), 2)
    steps = ["cache_check", "risk_assess", "plan", "intent", "enrich", "local_search", "synthesize", "optimistic_respond"]
    if risk_level in ("high", "critical"):
        steps += ["validate"]

    orch_decision = json.dumps({
        "skill":      topic,
        "model":      "qwen3:14b",
        "args":       {"task": question[:200]},
        "reason":     f"intent={topic}, risk={risk_level}, steps={len(steps)}",
        "plan":       steps,
        "trust":      trust_level,
        "confidence": confidence,
    }, ensure_ascii=False)

    quality = 0.8 if verified else 0.7 if len(answer) > 200 else 0.6
    outcome = "success" if confidence > 0.4 else "partial"

    return {
        "ts":         datetime.now().isoformat(),
        "date":       datetime.now().strftime("%Y-%m-%d"),
        "task":       question[:500],
        "task_type":  topic,
        "model":      "qwen3:14b",
        "result":     answer[:1000],
        "outcome":    outcome,
        "elapsed_ms": random.randint(5000, 60000),
        "quality":    quality,
        "steps":      len(steps),
        "skill":      topic,
        "source":     source,
        "verified":   verified,
        "messages": [
            {"role": "system",    "content": SYSTEM_PROMPT},
            {"role": "user",      "content": question},
            {"role": "assistant", "content": orch_decision},
        ],
    }


def generate(target: int = 500, verbose: bool = True) -> dict:
    """
    Сгенерировать обучающие трейсы.

    Args:
        target:  целевое количество трейсов
        verbose: печатать прогресс

    Returns:
        dict со статистикой
    """
    if verbose:
        print(f"Генерация трейсов: цель={target}")

    # Загружаем реальные данные
    real_items = _load_from_sessions() + _load_from_knowledge()
    if verbose:
        print(f"  Реальных Q&A: {len(real_items)}")

    # Дополняем синтетическими если нужно
    need_synthetic = max(0, target - len(real_items))
    synthetic: list[dict] = []
    if need_synthetic > 0:
        per_domain = max(1, need_synthetic // 4)
        for domain in ["tech", "ai_ml", "coding", "general"]:
            synthetic += _generate_synthetic(per_domain, domain)
        if verbose:
            print(f"  Синтетических: {len(synthetic)}")

    all_items = real_items + synthetic
    random.shuffle(all_items)
    all_items = all_items[:target]

    # Строим трейсы
    traces = [_make_trace(item, i) for i, item in enumerate(all_items)]

    # Сохраняем
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = TRACES_DIR / f"synthetic_{ts}.jsonl"
    with out_file.open("w", encoding="utf-8") as f:
        for t in traces:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")

    if verbose:
        by_domain: dict[str, int] = {}
        for t in traces:
            d = t["task_type"]
            by_domain[d] = by_domain.get(d, 0) + 1
        print(f"\n✓ Записано {len(traces)} трейсов → {out_file.name}")
        print(f"  По доменам: {by_domain}")

    return {
        "total":     len(traces),
        "real":      len(real_items),
        "synthetic": len(synthetic),
        "file":      str(out_file),
    }


def stats() -> dict:
    """Статистика всех трейсов."""
    total = 0
    sources: dict[str, int] = {}
    for f in TRACES_DIR.glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                total += 1
                src = d.get("source", "unknown")
                sources[src] = sources.get(src, 0) + 1
            except Exception:
                pass
    return {"total": total, "by_source": sources}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "generate":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 500
        result = generate(n, verbose=True)
        print(f"\nРезультат: {result}")
    elif cmd == "stats":
        s = stats()
        print(f"Всего трейсов: {s['total']}")
        print(f"По источникам: {s['by_source']}")
    else:
        print("Команды: generate [N], stats")
