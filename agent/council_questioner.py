#!/usr/bin/env python3
"""
assistant/council_questioner.py — реле-режим вопросника для Council chat.

Для каждого вопроса:
  1. Отправить через /api/council/relay (human → claude → gpt → deepseek)
  2. Ждать ответа deepseek (конец цепочки)
  3. Запустить council_scribe (сборщик датасета)
  4. Очистить чат
  5. Следующий вопрос

CLI:
  python3 assistant/council_questioner.py run
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

import redis as _redis

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))
from agent.local_http import local_post

COUNCIL_API  = "http://127.0.0.1:9010"
REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
PUBSUB_CH    = "council:chat:pubsub"
WAIT_TIMEOUT = 360   # 6 минут на весь цикл claude→gpt→deepseek
AFTER_CLEAR  = 5     # пауза после очистки перед следующим вопросом

# Префикс — просить модели отвечать только текстом
PLAIN_PREFIX = (
    "Отвечай ТОЛЬКО простым текстом. "
    "Без markdown, без таблиц, без блоков кода, без заголовков (#). "
    "Только связный текст.\n\n"
)

QUESTIONS = [
    # ── 1. Архитектура DHT ───────────────────────────────────────────────────
    {
        "topic": "distributed_architecture",
        "q": [
            "Как правильно организовать DHT (distributed hash table) для хранения знаний в P2P-сети, "
            "где каждая нода — локальная LLM? Какой алгоритм выбрать: Kademlia, Chord или что-то другое?",

            "Как обеспечить консистентность знаний в P2P-сети если ноды приходят и уходят? "
            "Нужен ли механизм версионирования для каждой записи в distributed knowledge base?",
        ]
    },
    # ── 2. Оркестрация моделей ───────────────────────────────────────────────
    {
        "topic": "model_orchestration",
        "q": [
            "Как обучить локальную модель-оркестратор (7B) маршрутизировать задачи между "
            "специалистами (reasoning, search, code, analysis)? Какой формат датасета нужен?",

            "Какой порог качества достаточен чтобы не эскалировать задачу к более крупной модели? "
            "Как автоматически определить что 7B справилась, а не галлюцинировала?",
        ]
    },
    # ── 3. Репутация нод ────────────────────────────────────────────────────
    {
        "topic": "node_reputation",
        "q": [
            "Как строить систему репутации для нод в P2P AI-сети? "
            "Репутация domain-specific (хорош в коде, слаб в медицине) или глобальная?",

            "Что делать с нодой которая систематически даёт неверные ответы — "
            "автоматически понижать доверие или требовать ручной верификации?",
        ]
    },
    # ── 4. Privacy и шифрование ──────────────────────────────────────────────
    {
        "topic": "privacy_encryption",
        "q": [
            "Нужно ли шифровать запросы между нодами в P2P AI-сети или достаточно TLS? "
            "Как балансировать между приватностью и возможностью верификации ответов?",

            "Как реализовать PRIVATE knowledge layer — данные хранятся только локально, "
            "не индексируются, но нода участвует в federated inference?",
        ]
    },
    # ── 5. Fine-tuning стратегия ─────────────────────────────────────────────
    {
        "topic": "finetuning_strategy",
        "q": [
            "Сколько примеров нужно для эффективного LoRA fine-tuning оркестратора на 7B? "
            "Наши 46 трейсов из council — это уже что-то или ещё мало?",

            "Как избежать catastrophic forgetting при fine-tuning — "
            "когда модель хорошо учится на новом датасете но теряет базовые способности? Нужен replay buffer?",
        ]
    },
    # ── 6. Optimistic UI ─────────────────────────────────────────────────────
    {
        "topic": "optimistic_ui",
        "q": [
            "Как реализовать optimistic answer + background validation? "
            "Как откатить ответ если фоновая верификация показала что он неверный?",

            "Как объяснить пользователю разницу между VERIFIED, HYPOTHESIS и PERSONAL записями? "
            "Нужна ли визуализация уровней доверия или достаточно текстовой метки?",
        ]
    },
    # ── 7. Knowledge Graph ───────────────────────────────────────────────────
    {
        "topic": "knowledge_graph",
        "q": [
            "Как автоматически связывать новые знания с существующими узлами в knowledge graph? "
            "Нужен LLM для определения типа связи или достаточно правил?",

            "Какой формат для хранения provenance (происхождения) знаний: "
            "JSON поля в узле, рёбра типа source_of, или отдельный audit log?",
        ]
    },
    # ── 8. Экономика сети ────────────────────────────────────────────────────
    {
        "topic": "network_economics",
        "q": [
            "Как стимулировать ноды участвовать в валидации чужих знаний если это требует GPU? "
            "Токены, репутация, или что-то другое?",

            "Нужна ли коммерческая модель для collective nodes (серверные ноды с большими моделями) "
            "или сеть должна быть полностью бесплатной на доверии?",
        ]
    },
    # ── 9. Hallucination detection ───────────────────────────────────────────
    {
        "topic": "hallucination_detection",
        "q": [
            "Как обнаруживать галлюцинации в ответах локальной LLM без обращения к интернету? "
            "Можно ли использовать consistency check через другую модель на той же ноде?",

            "Если несколько нод дали разные ответы на один вопрос — как агрегировать? "
            "Мажоритарное голосование, взвешенное по репутации, или другое?",
        ]
    },
    # ── 10. Безопасность ─────────────────────────────────────────────────────
    {
        "topic": "security_abuse",
        "q": [
            "Как защитить P2P AI-сеть от poison attack — "
            "когда злоумышленник намеренно добавляет неверные знания чтобы снизить качество сети?",

            "Нужна ли идентификация пользователей в P2P сети или полная анонимность лучше? "
            "Как баланс между анонимностью и ответственностью за качество знаний?",
        ]
    },
    # ── 11. Federated Inference ──────────────────────────────────────────────
    {
        "topic": "federated_inference",
        "q": [
            "Как распределить inference большой задачи между несколькими малыми моделями на разных нодах? "
            "Это реалистично или лучше просто выбрать одну мощную ноду?",

            "Как синхронизировать контекст между нодами при federated inference? "
            "Какой максимальный размер контекста можно передавать по P2P без деградации?",
        ]
    },
    # ── 12. Рост сети ────────────────────────────────────────────────────────
    {
        "topic": "network_growth",
        "q": [
            "Как новая нода должна bootstrapping в P2P сети — "
            "с чего начать, каким нодам доверять, как набрать репутацию?",

            "Какой минимальный технический порог для запуска ноды? "
            "Raspberry Pi 4 с Qwen 0.6B или нужно хотя бы 8GB VRAM?",
        ]
    },
]


def _r() -> _redis.Redis:
    return _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _send_relay(text: str) -> bool:
    """Отправить вопрос через relay (human → claude → gpt → deepseek)."""
    try:
        resp = local_post(
            f"{COUNCIL_API}/api/council/relay",
            json={"text": PLAIN_PREFIX + text},
            timeout=10,
        )
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"[questioner] relay error: {e}", flush=True)
        return False


def _wait_deepseek(r: _redis.Redis, sent_at: float, timeout: int = WAIT_TIMEOUT) -> bool:
    """Ждать ответа deepseek через pubsub — он всегда последний в цепочке."""
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(PUBSUB_CH)
    deadline = time.time() + timeout
    print(f"[questioner] ожидаю claude → gpt → deepseek (max {timeout}s)...", flush=True)

    responded = set()
    while time.time() < deadline:
        msg = ps.get_message(timeout=2)
        if not msg:
            continue
        try:
            data = json.loads(msg["data"])
        except Exception:
            continue

        frm = data.get("from", "")
        ts  = data.get("_ts", 0.0)

        if frm in ("claude", "gpt", "deepseek") and ts >= sent_at:
            responded.add(frm)
            waiting = {"claude", "gpt", "deepseek"} - responded
            print(
                f"[questioner] ✓ {frm} ответил ({len(responded)}/3)"
                + (f"  ждём: {sorted(waiting)}" if waiting else ""),
                flush=True,
            )
            if frm == "deepseek":
                ps.unsubscribe()
                return True

    ps.unsubscribe()
    print(f"[questioner] ⚠️ таймаут {timeout}s  ответили: {sorted(responded)}", flush=True)
    return False


def _clear_chat() -> None:
    try:
        local_post(f"{COUNCIL_API}/api/council/clear", json={}, timeout=5)
        print(f"[questioner] 🗑  чат очищен", flush=True)
    except Exception as e:
        print(f"[questioner] clear error: {e}", flush=True)


def run_session():
    r = _r()
    total_q = sum(len(t["q"]) for t in QUESTIONS)
    print(f"[questioner] Реле-режим: {len(QUESTIONS)} тем, {total_q} вопросов", flush=True)
    print(f"[questioner] Порядок: human → claude → gpt → deepseek → сборщик → очистка", flush=True)
    print(flush=True)

    q_num = 0
    for t_idx, topic_data in enumerate(QUESTIONS):
        topic = topic_data["topic"]
        print(f"═══ Тема {t_idx+1}/{len(QUESTIONS)}: {topic} ═══", flush=True)

        for q_idx, question in enumerate(topic_data["q"]):
            q_num += 1
            print(f"\n[{q_num}/{total_q}] Вопрос {q_idx+1}/2:", flush=True)
            print(f"  {question[:100]}...", flush=True)

            sent_at = time.time()
            ok = _send_relay(question)
            if not ok:
                print(f"[questioner] ❌ не удалось отправить", flush=True)
                continue
            print(f"[questioner] 📤 отправлено {datetime.now().strftime('%H:%M:%S')}", flush=True)

            # Ждём deepseek — конец цепочки
            done = _wait_deepseek(r, sent_at)

            # Запускаем сборщик
            print(f"[questioner] 📊 запускаю council_scribe...", flush=True)
            try:
                from agent.council_scribe import run_scribe
                result = run_scribe()
                print(
                    f"[questioner] ✅ трейсов: {result['traces']}  "
                    f"решений: {result['decisions_new']} новых",
                    flush=True,
                )
            except Exception as e:
                print(f"[questioner] scribe error: {e}", flush=True)

            # Очищаем чат
            _clear_chat()
            time.sleep(AFTER_CLEAR)

        print(f"\n✓ Тема '{topic}' завершена\n", flush=True)

    # Финальный экспорт в SFT
    print(f"\n[questioner] === СЕССИЯ ЗАВЕРШЕНА ===", flush=True)
    print(f"[questioner] Всего вопросов: {q_num}", flush=True)
    print(f"[questioner] Запускаю финальный экспорт SFT датасета...", flush=True)
    try:
        import subprocess
        res = subprocess.run(
            ["python3", "assistant/orch_dataset.py", "export"],
            capture_output=True, text=True, cwd=str(BASE),
        )
        print(res.stdout.strip(), flush=True)
    except Exception as e:
        print(f"[questioner] export error: {e}", flush=True)


if __name__ == "__main__":
    sub = sys.argv[1] if len(sys.argv) > 1 else "run"
    if sub == "run":
        run_session()
    else:
        print("Команды: run")
