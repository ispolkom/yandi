#!/usr/bin/env python3
"""
assistant/model_runner.py — полный опросник для одной модели.

Блокирует остальные две, гонит все 24 вопроса по очереди,
ждёт ответа до 300с, сохраняет в отдельный файл.
При остановке — возобновляет с места остановки.

CLI:
  python3 assistant/model_runner.py claude
  python3 assistant/model_runner.py gpt
  python3 assistant/model_runner.py deepseek
  python3 assistant/model_runner.py claude --resume   # продолжить с места
  python3 assistant/model_runner.py stats             # статистика всех файлов
"""
from __future__ import annotations

import json
import signal
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
WAIT_TIMEOUT = 300
AFTER_Q      = 6      # пауза после очистки перед следующим вопросом

MODELS = ("claude", "gpt", "deepseek")
OUT_DIR = BASE / "registry" / "dataset" / "model_sessions"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PLAIN_PREFIX = (
    "Отвечай ТОЛЬКО простым текстом. "
    "Без markdown, без таблиц, без блоков кода, без заголовков. "
    "Только связный текст.\n\n"
)

QUESTIONS = [
    {"topic": "distributed_architecture", "q": [
        "Как правильно организовать DHT (distributed hash table) для хранения знаний в P2P-сети, "
        "где каждая нода — локальная LLM? Какой алгоритм выбрать: Kademlia, Chord или что-то другое?",
        "Как обеспечить консистентность знаний в P2P-сети если ноды приходят и уходят? "
        "Нужен ли механизм версионирования для каждой записи в distributed knowledge base?",
    ]},
    {"topic": "model_orchestration", "q": [
        "Как обучить локальную модель-оркестратор (7B) маршрутизировать задачи между "
        "специалистами (reasoning, search, code, analysis)? Какой формат датасета нужен?",
        "Какой порог качества достаточен чтобы не эскалировать задачу к более крупной модели? "
        "Как автоматически определить что 7B справилась, а не галлюцинировала?",
    ]},
    {"topic": "node_reputation", "q": [
        "Как строить систему репутации для нод в P2P AI-сети? "
        "Репутация domain-specific (хорош в коде, слаб в медицине) или глобальная?",
        "Что делать с нодой которая систематически даёт неверные ответы — "
        "автоматически понижать доверие или требовать ручной верификации?",
    ]},
    {"topic": "privacy_encryption", "q": [
        "Нужно ли шифровать запросы между нодами в P2P AI-сети или достаточно TLS? "
        "Как балансировать между приватностью и возможностью верификации ответов?",
        "Как реализовать PRIVATE knowledge layer — данные хранятся только локально, "
        "не индексируются, но нода участвует в federated inference?",
    ]},
    {"topic": "finetuning_strategy", "q": [
        "Сколько примеров нужно для эффективного LoRA fine-tuning оркестратора на 7B? "
        "Наши 46 трейсов из council — это уже что-то или ещё мало?",
        "Как избежать catastrophic forgetting при fine-tuning — "
        "когда модель учится на новом датасете но теряет базовые способности? Нужен replay buffer?",
    ]},
    {"topic": "optimistic_ui", "q": [
        "Как реализовать optimistic answer + background validation? "
        "Как откатить ответ если фоновая верификация показала что он неверный?",
        "Как объяснить пользователю разницу между VERIFIED, HYPOTHESIS и PERSONAL записями? "
        "Нужна ли визуализация уровней доверия или достаточно текстовой метки?",
    ]},
    {"topic": "knowledge_graph", "q": [
        "Как автоматически связывать новые знания с существующими узлами в knowledge graph? "
        "Нужен LLM для определения типа связи или достаточно правил?",
        "Какой формат для хранения provenance (происхождения) знаний: "
        "JSON поля в узле, рёбра типа source_of, или отдельный audit log?",
    ]},
    {"topic": "network_economics", "q": [
        "Как стимулировать ноды участвовать в валидации чужих знаний если это требует GPU? "
        "Токены, репутация, или что-то другое?",
        "Нужна ли коммерческая модель для collective nodes (серверные ноды с большими моделями) "
        "или сеть должна быть полностью бесплатной на доверии?",
    ]},
    {"topic": "hallucination_detection", "q": [
        "Как обнаруживать галлюцинации в ответах локальной LLM без обращения к интернету? "
        "Можно ли использовать consistency check через другую модель на той же ноде?",
        "Если несколько нод дали разные ответы на один вопрос — как агрегировать? "
        "Мажоритарное голосование, взвешенное по репутации, или другое?",
    ]},
    {"topic": "security_abuse", "q": [
        "Как защитить P2P AI-сеть от poison attack — "
        "когда злоумышленник намеренно добавляет неверные знания чтобы снизить качество сети?",
        "Нужна ли идентификация пользователей в P2P сети или полная анонимность лучше? "
        "Как баланс между анонимностью и ответственностью за качество знаний?",
    ]},
    {"topic": "federated_inference", "q": [
        "Как распределить inference большой задачи между несколькими малыми моделями на разных нодах? "
        "Это реалистично или лучше просто выбрать одну мощную ноду?",
        "Как синхронизировать контекст между нодами при federated inference? "
        "Какой максимальный размер контекста можно передавать по P2P без деградации?",
    ]},
    {"topic": "network_growth", "q": [
        "Как новая нода должна bootstrapping в P2P сети — "
        "с чего начать, каким нодам доверять, как набрать репутацию?",
        "Какой минимальный технический порог для запуска ноды? "
        "Raspberry Pi 4 с Qwen 0.6B или нужно хотя бы 8GB VRAM?",
    ]},
]

# Плоский список всех вопросов
ALL_QUESTIONS = []
idx = 0
for t in QUESTIONS:
    for q in t["q"]:
        ALL_QUESTIONS.append({"idx": idx, "topic": t["topic"], "text": q})
        idx += 1


def _r() -> _redis.Redis:
    return _redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def _out_file(model: str) -> Path:
    today = datetime.now().strftime("%Y%m%d")
    return OUT_DIR / f"{model}_{today}.jsonl"


def _progress_key(model: str) -> str:
    return f"council:runner:progress:{model}"


def _load_progress(model: str) -> int:
    """Последний успешно записанный idx вопроса (-1 = не начат)."""
    r = _r()
    val = r.get(_progress_key(model))
    return int(val) if val is not None else -1


def _save_progress(model: str, idx: int):
    r = _r()
    r.set(_progress_key(model), idx)


def _set_state(model: str, block: bool):
    """Заблокировать или разблокировать модель."""
    local_post(
        f"{COUNCIL_API}/api/council/state",
        json={f"{model}_blocked": block},
        timeout=5,
    )


def _block_others(active_model: str):
    for m in MODELS:
        if m != active_model:
            _set_state(m, True)
    # Убедиться что активная разблокирована
    _set_state(active_model, False)


def _unblock_all():
    for m in MODELS:
        _set_state(m, False)


def _send_question(text: str) -> bool:
    try:
        resp = local_post(
            f"{COUNCIL_API}/api/council/relay",
            json={"text": PLAIN_PREFIX + text},
            timeout=10,
        )
        return resp.json().get("ok", False)
    except Exception as e:
        print(f"  relay error: {e}", flush=True)
        return False


def _wait_answer(model: str, sent_at: float, timeout: int = WAIT_TIMEOUT) -> str | None:
    """Ждать ответа конкретной модели. Возвращает текст или None при timeout."""
    r = _r()
    ps = r.pubsub(ignore_subscribe_messages=True)
    ps.subscribe(PUBSUB_CH)
    deadline = time.time() + timeout

    while time.time() < deadline:
        msg = ps.get_message(timeout=2)
        if not msg:
            continue
        try:
            data = json.loads(msg["data"])
        except Exception:
            continue
        if data.get("from") == model and data.get("_ts", 0) >= sent_at:
            text = data.get("text", "").strip()
            if text and not text.startswith("[timeout") and not text.startswith("[нет"):
                ps.unsubscribe()
                return text

    ps.unsubscribe()
    return None


def _clear_chat():
    try:
        local_post(f"{COUNCIL_API}/api/council/clear", json={}, timeout=5)
    except Exception:
        pass


def run_model(model: str, resume: bool = False):
    if model not in MODELS:
        print(f"Неизвестная модель: {model}. Доступны: {MODELS}")
        return

    out_file = _out_file(model)
    start_from = 0

    if resume:
        start_from = _load_progress(model) + 1
        print(f"[{model}] Возобновление с вопроса #{start_from}", flush=True)
    else:
        # Сбросить прогресс
        _save_progress(model, -1)

    total = len(ALL_QUESTIONS)
    remaining = ALL_QUESTIONS[start_from:]

    print(f"[{model}] Запуск: {len(remaining)}/{total} вопросов → {out_file.name}", flush=True)
    print(f"[{model}] Блокирую остальные модели...", flush=True)
    _block_others(model)

    # Graceful stop on Ctrl+C
    stopped = False
    def _handle_stop(sig, frame):
        nonlocal stopped
        stopped = True
        print(f"\n[{model}] Получен сигнал остановки, завершаю после текущего вопроса...", flush=True)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    answered = 0
    timed_out = 0

    with open(out_file, "a", encoding="utf-8") as f:
        for entry in remaining:
            if stopped:
                break

            q_idx   = entry["idx"]
            topic   = entry["topic"]
            q_text  = entry["text"]
            q_num   = q_idx + 1

            print(f"\n[{model}] [{q_num}/{total}] {topic}", flush=True)
            print(f"  Q: {q_text[:90]}...", flush=True)

            sent_at = time.time()
            ok = _send_question(q_text)
            if not ok:
                print(f"  ❌ не удалось отправить", flush=True)
                continue

            answer = _wait_answer(model, sent_at, WAIT_TIMEOUT)

            if answer:
                print(f"  ✓ ответ ({len(answer)} символов)", flush=True)
                record = {
                    "idx":      q_idx,
                    "topic":    topic,
                    "model":    model,
                    "question": q_text,
                    "answer":   answer,
                    "ts":       datetime.now().isoformat(),
                    "outcome":  "success",
                    "messages": [
                        {"role": "system",    "content": "Ты эксперт по распределённым AI-системам."},
                        {"role": "user",      "content": q_text},
                        {"role": "assistant", "content": answer},
                    ],
                }
                answered += 1
            else:
                print(f"  ⚠️  timeout {WAIT_TIMEOUT}s", flush=True)
                record = {
                    "idx":      q_idx,
                    "topic":    topic,
                    "model":    model,
                    "question": q_text,
                    "answer":   None,
                    "ts":       datetime.now().isoformat(),
                    "outcome":  "timeout",
                    "messages": [],
                }
                timed_out += 1

            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            f.flush()
            _save_progress(model, q_idx)
            _clear_chat()
            time.sleep(AFTER_Q)

    _unblock_all()
    print(f"\n[{model}] === ГОТОВО ===", flush=True)
    print(f"  Отвечено: {answered}  Timeout: {timed_out}", flush=True)
    print(f"  Файл: {out_file}", flush=True)


def show_stats():
    files = sorted(OUT_DIR.glob("*.jsonl"))
    if not files:
        print("Нет файлов сессий.")
        return
    for f in files:
        lines = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
        ok  = sum(1 for l in lines if l.get("outcome") == "success")
        to  = sum(1 for l in lines if l.get("outcome") == "timeout")
        model = lines[0]["model"] if lines else "?"
        print(f"  {f.name}: {ok} ответов, {to} timeout, всего {len(lines)}/24")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python3 model_runner.py claude|gpt|deepseek [--resume]")
        print("               python3 model_runner.py stats")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "stats":
        show_stats()
    elif cmd in MODELS:
        resume = "--resume" in sys.argv
        run_model(cmd, resume=resume)
    else:
        print(f"Неизвестная команда: {cmd}")
