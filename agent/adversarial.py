#!/usr/bin/env python3
"""
assistant/adversarial.py — adversarial probing через совет.

Схема:
  Берём решение/утверждение из Decision Tracker или вручную.
  Отправляем в совет:
    - Одна модель (defender) защищает позицию
    - Другие (attackers) ищут уязвимости, контраргументы, edge cases
  Результат дебата → высокоценные записи в датасет (failures + synthetic).

Почему ценно (по Claude):
  "Датасет из adversarial дискуссий особенно ценен — модель учится
  защищать позиции и признавать ошибки."

Режимы:
  defend_decision <decision_id>   — дебат по конкретному решению
  defend_claim <утверждение>      — дебат по произвольному тезису
  stress_test <тема>              — найти слабые места в теме

Хранение:
  registry/dataset/adversarial/   — результаты дебатов в JSONL

Команды:
  python3 assistant/adversarial.py defend_decision <id>
  python3 assistant/adversarial.py defend_claim "тезис"
  python3 assistant/adversarial.py stress_test "тема"
  python3 assistant/adversarial.py status
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE          = Path(__file__).parent.parent
ADVERSARIAL_DIR = BASE / "registry" / "dataset" / "adversarial"
REPORT_KEY    = "council:skill:reports"
REPORT_CH     = "council:skill:report"
CHAT_CH       = "council:chat:pubsub"

ADVERSARIAL_DIR.mkdir(parents=True, exist_ok=True)

PROXIES = {"http": None, "https": None}
COUNCIL_API = "http://127.0.0.1:9010"

# Роли для дебата
DEFENDER_MODEL = "claude"
ATTACKER_MODELS = ["gpt", "deepseek"]

WAIT_REPLY_TIMEOUT = 120  # сек


def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    r.lpush(REPORT_KEY, data)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, data)


def _api_post(path: str, payload: dict) -> dict:
    import sys as _sys; _sys.path.insert(0, str(Path(__file__).parent.parent))
    from agent.local_http import local_post
    r = local_post(f"{COUNCIL_API}{path}", json=payload, timeout=10)
    return r.json() if r.ok else {"error": r.text}


def _set_state(**kwargs):
    _api_post("/api/council/state", kwargs)


def _inject(text: str) -> str:
    result = _api_post("/api/council/inject", {"text": text})
    return result.get("task_id", "")


def _wait_reply(model: str, r: redis.Redis, timeout: int = WAIT_REPLY_TIMEOUT) -> str:
    pubsub   = r.pubsub()
    pubsub.subscribe(CHAT_CH)
    deadline = time.time() + timeout

    try:
        for msg in pubsub.listen():
            if time.time() > deadline:
                break
            if msg["type"] != "message":
                continue
            try:
                data = json.loads(msg["data"])
                if data.get("from") == model and len(data.get("text", "")) > 20:
                    return data["text"]
            except Exception:
                continue
    finally:
        pubsub.unsubscribe()
        pubsub.close()
    return ""


class AdversarialProber:
    """Организует adversarial дебаты через совет."""

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    def _run_debate(self, claim: str, context: str = "", topic: str = "general") -> dict:
        """
        Основной цикл дебата:
        1. Defender объясняет и защищает тезис
        2. Attacker 1 атакует
        3. Attacker 2 атакует с другой стороны
        4. Defender даёт финальный ответ
        """
        stamp    = datetime.now().strftime("%Y%m%d_%H%M%S")
        debate_id = f"debate_{stamp}"
        rounds   = []

        print(f"\n[adversarial] Дебат: {claim[:60]}")

        # ── Раунд 1: Defender объясняет позицию ──────────────────────────────
        defender_prompt = (
            f"[Adversarial Debate — роль: ЗАЩИТНИК]\n\n"
            f"Тезис: «{claim}»\n"
            + (f"Контекст: {context}\n" if context else "")
            + "\nОбъясни и защити этот тезис как можно убедительнее. "
            "Приведи 3 сильных аргумента в его поддержку. "
            "Будь конкретен, используй примеры из нашего проекта."
        )

        print(f"  [R1] {DEFENDER_MODEL} защищает...")
        _set_state(**{f"{m}_blocked": (m != DEFENDER_MODEL) for m in ["claude", "gpt", "deepseek"]})
        time.sleep(1)
        _inject(defender_prompt)
        defense = _wait_reply(DEFENDER_MODEL, self.r)
        rounds.append({"role": "defender", "model": DEFENDER_MODEL,
                       "type": "defense", "content": defense})
        print(f"  ✓ defense: {len(defense)} симв.")
        time.sleep(5)

        # ── Раунд 2: Attackers атакуют ────────────────────────────────────────
        for attacker in ATTACKER_MODELS:
            attack_prompt = (
                f"[Adversarial Debate — роль: АТАКУЮЩИЙ]\n\n"
                f"Тезис под защитой: «{claim}»\n\n"
                f"Защитник ({DEFENDER_MODEL}) сказал:\n{defense[:800]}\n\n"
                "Найди уязвимости в этом тезисе. Предложи контрпримеры, edge cases, "
                "скрытые допущения, которые делают тезис ошибочным или ограниченным. "
                "Будь конкретен и аргументирован. Не соглашайся ради вежливости."
            )

            print(f"  [R2] {attacker} атакует...")
            _set_state(**{f"{m}_blocked": (m != attacker) for m in ["claude", "gpt", "deepseek"]})
            time.sleep(1)
            _inject(attack_prompt)
            attack = _wait_reply(attacker, self.r)
            rounds.append({"role": "attacker", "model": attacker,
                           "type": "attack", "content": attack})
            print(f"  ✓ attack ({attacker}): {len(attack)} симв.")
            time.sleep(5)

        # ── Раунд 3: Defender финальный ответ ────────────────────────────────
        attacks_summary = "\n\n".join(
            f"[{r['model']}]: {r['content'][:400]}"
            for r in rounds if r["type"] == "attack"
        )
        final_prompt = (
            f"[Adversarial Debate — финальная защита]\n\n"
            f"Тезис: «{claim}»\n\n"
            f"Атакующие выдвинули следующие возражения:\n{attacks_summary}\n\n"
            "Ответь на каждое возражение. Признай, если в тезисе есть ограничения, "
            "но объясни почему он всё равно верен (или скорректируй его). "
            "Итог: уточнённая версия тезиса с учётом критики."
        )

        print(f"  [R3] {DEFENDER_MODEL} финальный ответ...")
        _set_state(**{f"{m}_blocked": (m != DEFENDER_MODEL) for m in ["claude", "gpt", "deepseek"]})
        time.sleep(1)
        _inject(final_prompt)
        final_defense = _wait_reply(DEFENDER_MODEL, self.r)
        rounds.append({"role": "defender", "model": DEFENDER_MODEL,
                       "type": "final_defense", "content": final_defense})
        print(f"  ✓ final_defense: {len(final_defense)} симв.")

        # Разблокируем всех
        _set_state(claude_blocked=False, gpt_blocked=False, deepseek_blocked=False)

        # ── Сохранение ────────────────────────────────────────────────────────
        result = {
            "debate_id" : debate_id,
            "claim"     : claim,
            "context"   : context,
            "topic"     : topic,
            "rounds"    : rounds,
            "timestamp" : datetime.now().isoformat(),
        }

        out_path = ADVERSARIAL_DIR / f"{debate_id}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))

        # Конвертируем в датасет-записи
        dataset_rows = self._to_dataset_rows(result)
        if dataset_rows:
            ds_path = ADVERSARIAL_DIR / f"{debate_id}_hf.jsonl"
            with open(ds_path, "w", encoding="utf-8") as f:
                for row in dataset_rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            result["dataset_path"] = str(ds_path)
            result["dataset_rows"] = len(dataset_rows)

        _publish(self.r, {
            "skill"       : "adversarial",
            "debate_id"   : debate_id,
            "claim"       : claim[:60],
            "rounds"      : len(rounds),
            "dataset_rows": result.get("dataset_rows", 0),
            "timestamp"   : result["timestamp"],
        })

        return result

    def _to_dataset_rows(self, result: dict) -> list[dict]:
        """Конвертирует дебат в HF-совместимые записи."""
        rows    = []
        sid     = result["debate_id"]
        topic   = result.get("topic", "general")
        ts      = result["timestamp"][:19]

        for round_data in result["rounds"]:
            content = round_data.get("content", "").strip()
            if len(content) < 30:
                continue
            role_map = {
                "defense"      : "assistant",
                "attack"       : "assistant",
                "final_defense": "assistant",
            }
            rows.append({
                "session_id": sid,
                "topic"     : topic,
                "time_start": ts,
                "role"      : role_map.get(round_data["type"], "assistant"),
                "content"   : content,
                "score"     : 90,  # adversarial примеры высокоценные
                "source"    : f"adversarial_{round_data['type']}",
                "debate_model": round_data["model"],
            })

        # Добавляем claim как human-сообщение
        rows.insert(0, {
            "session_id": sid,
            "topic"     : topic,
            "time_start": ts,
            "role"      : "human",
            "content"   : f"[Adversarial] Тезис: {result['claim']}",
            "score"     : 90,
            "source"    : "adversarial_claim",
        })

        return rows

    # ── публичные методы ──────────────────────────────────────────────────────

    def defend_decision(self, decision_id: str) -> dict:
        """Дебат по решению из Decision Tracker."""
        try:
            from agent.decision_tracker import DecisionTracker
            dt  = DecisionTracker(r=self.r)
            rec = dt.get(decision_id)
            if not rec:
                # частичный поиск
                records = [r for r in dt.list("open") if decision_id.lower() in r["id"].lower()
                           or decision_id.lower() in r["text"].lower()]
                if not records:
                    return {"error": f"решение не найдено: {decision_id}"}
                rec = records[0]
        except Exception as e:
            return {"error": str(e)}

        claim   = rec["text"]
        context = f"Причина: {rec.get('reason', '')}. Ожидание: {rec.get('expected', '')}"
        return self._run_debate(claim, context=context, topic="decision")

    def defend_claim(self, claim: str, topic: str = "general") -> dict:
        """Дебат по произвольному тезису."""
        return self._run_debate(claim, topic=topic)

    def stress_test(self, topic: str) -> dict:
        """Стресс-тест темы — найти слабые места."""
        claim = (
            f"Текущая реализация {topic} в системе PET/Council является "
            f"оптимальной и не имеет существенных уязвимостей."
        )
        return self._run_debate(claim, topic=topic)

    def status(self) -> list[dict]:
        debates = []
        for f in sorted(ADVERSARIAL_DIR.glob("debate_*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
                debates.append({
                    "debate_id"    : d["debate_id"],
                    "claim"        : d["claim"][:60],
                    "rounds"       : len(d.get("rounds", [])),
                    "dataset_rows" : d.get("dataset_rows", 0),
                    "timestamp"    : d.get("timestamp", ""),
                })
            except Exception:
                pass
        return sorted(debates, key=lambda x: x["timestamp"], reverse=True)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    ap  = AdversarialProber()

    if cmd == "defend_decision":
        did    = sys.argv[2] if len(sys.argv) > 2 else ""
        result = ap.defend_decision(did)
        print(f"Дебат завершён: {result.get('debate_id')} ({result.get('dataset_rows',0)} записей)")

    elif cmd == "defend_claim":
        claim  = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else ""
        result = ap.defend_claim(claim)
        print(f"Дебат завершён: {result.get('debate_id')} ({result.get('dataset_rows',0)} записей)")

    elif cmd == "stress_test":
        topic  = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Knowledge Graph"
        result = ap.stress_test(topic)
        print(f"Стресс-тест завершён: {result.get('debate_id')}")

    elif cmd == "status":
        debates = ap.status()
        if not debates:
            print("Нет дебатов. Запусти: defend_claim 'тезис'")
        for d in debates:
            print(f"  [{d['debate_id']}]  rounds={d['rounds']}  "
                  f"rows={d['dataset_rows']}  {d['claim']}")

    else:
        print(f"Неизвестная команда: {cmd}")
        print("Доступно: defend_decision <id> | defend_claim <тезис> | stress_test <тема> | status")
        sys.exit(1)
