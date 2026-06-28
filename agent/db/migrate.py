"""
agent/db/migrate.py — Миграция старых данных в новую схему.

Источники:
  - /media/iam/DATASET/claude/registry/dataset/orch_traces/*.json  → traces
  - /media/iam/DATASET/claude/registry/knowledge/graph.db          → knowledge (nodes)
  - /media/iam/DATASET/claude/registry/verified_knowledge/         → knowledge (verified)

CLI:
  python3 -m agent.db.migrate --dry-run   # показать что будет сделано
  python3 -m agent.db.migrate             # применить
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from agent.db.manager import KnowledgeDB, make_id

# ── Источники старых данных ───────────────────────────────────────────────────

OLD_TRACES_DIR   = Path("/media/iam/DATASET/claude/registry/dataset/orch_traces")
OLD_GRAPH_DB     = Path("/media/iam/DATASET/claude/registry/knowledge/graph.db")
OLD_VK_DIR       = Path("/media/iam/DATASET/claude/registry/verified_knowledge")


def _extract_model_chain(steps: list[dict]) -> str:
    models = []
    for s in steps:
        m = s.get("model", "")
        if m and m not in models:
            models.append(m)
    return "→".join(models)


def _guess_tag(question: str) -> str:
    """Грубая классификация вопроса по ключевым словам."""
    q = question.lower()
    rules = [
        (["физик", "квант", "энерги", "атом", "электрон", "магнит"],  "science:physics"),
        (["биолог", "клетк", "ген", "днк", "эволюц", "организм"],     "science:biology"),
        (["химия", "химич", "молекул", "реакц", "элемент"],           "science:chemistry"),
        (["астроном", "звезд", "планет", "луна", "галактик", "космос"],"science:astronomy"),
        (["медицин", "болезн", "лечен", "анатом", "организм", "врач"], "health:medicine"),
        (["психолог", "поведен", "мозг", "эмоци", "страх"],           "psychology:general"),
        (["экономик", "финанс", "инфляц", "рынок", "банк"],           "finance:general"),
        (["история", "историч", "войн", "цивилизац", "египет"],       "history:general"),
        (["программ", "алгоритм", "код", "python", "rust", "сеть"],   "tech:programming"),
        (["математик", "числ", "теорем", "вероятност"],               "math:general"),
        (["лингвист", "язык", "перевод", "грамматик"],                "linguistics:general"),
    ]
    for keywords, tag in rules:
        if any(k in q for k in keywords):
            return tag
    return "general:general"


def _build_answer_from_steps(steps: list[dict], verdict: str) -> tuple[str, float]:
    """
    Собрать финальный ответ из шагов трейса.
    Возвращает (answer, confidence).
    """
    initial  = ""
    correction  = ""
    supplement  = ""

    for s in steps:
        role = s.get("role", "")
        if role == "initial_answer":
            initial = s.get("text", "").strip()
        elif role == "critique":
            correction = (s.get("correction") or "").strip()
            supplement = (s.get("supplement") or "").strip()

    if verdict == "REJECTED":
        answer = correction or initial
        confidence = 0.4
    elif verdict == "VERIFIED":
        answer = initial
        confidence = 0.95
    else:
        # PARTIALLY_VERIFIED, SUPPLEMENTED — собираем всё вместе
        parts = [initial]
        if correction:
            parts.append(f"\n[Уточнение]: {correction}")
        if supplement:
            parts.append(f"\n[Дополнение]: {supplement}")
        answer = "".join(parts)
        confidence = 0.65

    return answer.strip(), confidence


def _verdict_to_trust(verdict: str) -> str:
    return "VERIFIED" if verdict == "VERIFIED" else "UNVERIFIED"


def migrate_traces(db: KnowledgeDB, dry_run: bool = False) -> int:
    if not OLD_TRACES_DIR.exists():
        print(f"  Пропуск traces: {OLD_TRACES_DIR} не найден")
        return 0

    files = list(OLD_TRACES_DIR.glob("*.json"))
    count = 0

    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ! Ошибка чтения {f.name}: {e}")
            continue

        question = data.get("question", "").strip()
        if not question:
            continue

        steps      = data.get("steps", [])
        verdict    = data.get("verdict", "UNVERIFIED")
        created_at = data.get("created_at", "")
        tag        = _guess_tag(question)
        entry_id   = make_id(question)
        chain      = _extract_model_chain(steps)

        answer, confidence = _build_answer_from_steps(steps, verdict)
        trust_level = _verdict_to_trust(verdict)

        if dry_run:
            print(f"  [trace+knowledge] {entry_id} | {tag} | {verdict} | {question[:55]}")
        else:
            db.save_trace(
                entry_id    = entry_id,
                question    = question,
                steps       = steps,
                verdict     = verdict,
                model_chain = chain,
                tag         = tag,
                meta        = {"migrated_from": f.name, "original_created_at": created_at},
            )
            if answer:
                db.save_knowledge(
                    query       = question,
                    answer      = answer,
                    tag         = tag,
                    trust_level = trust_level,
                    confidence  = confidence,
                    meta        = {"migrated_from": f.name, "verdict": verdict},
                    entry_id    = entry_id,
                )
        count += 1

    return count


def migrate_verified_knowledge(db: KnowledgeDB, dry_run: bool = False) -> int:
    count = 0
    for jsonl in OLD_VK_DIR.glob("*.jsonl") if OLD_VK_DIR.exists() else []:
        for line in jsonl.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue

            query  = d.get("query", "").strip()
            answer = d.get("answer", "").strip()
            if not query or not answer:
                continue

            tag        = d.get("tag", _guess_tag(query))
            entry_id   = make_id(query)
            sources    = d.get("sources", [])
            confidence = d.get("confidence", 1.0)

            if dry_run:
                print(f"  [knowledge] {entry_id} | {tag} | VERIFIED | {query[:60]}")
            else:
                db.save_knowledge(
                    query       = query,
                    answer      = answer,
                    tag         = tag,
                    trust_level = "VERIFIED",
                    confidence  = confidence,
                    sources     = sources,
                    meta        = {"migrated": True},
                    entry_id    = entry_id,
                )
            count += 1

    return count


def run(dry_run: bool = False):
    mode = "DRY RUN" if dry_run else "ПРИМЕНЯЕМ"
    print(f"\n{'='*50}")
    print(f"  Миграция данных — {mode}")
    print(f"{'='*50}\n")

    db = KnowledgeDB()

    print("→ Трейсы (orch_traces):")
    n = migrate_traces(db, dry_run)
    print(f"  Обработано: {n}\n")

    print("→ Верифицированные знания:")
    n = migrate_verified_knowledge(db, dry_run)
    print(f"  Обработано: {n}\n")

    if not dry_run:
        stats = db.stats()
        print("Итог:")
        print(f"  Категории:  {stats['categories']}")
        print(f"  Knowledge:  {stats['knowledge']}")
        print(f"  Verified:   {stats['verified']}")
        print(f"  Traces:     {stats['traces']}")
        print(f"  Gold pairs: {stats['gold_pairs']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
