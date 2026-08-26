"""
assistant/orch_council_connector.py — Council Connector.
MVP: отправляет вопрос в Council (GPT/Claude/DeepSeek) через Redis pubsub,
собирает ответы с таймаутом, возвращает dict{model: answer}.

Future: заменить на P2P YANDI RPC.
"""
from __future__ import annotations

import json
import time
import threading
from typing import Optional

COUNCIL_SERVER = "http://127.0.0.1:9010"
PUBSUB_CH      = "council:chat:pubsub"
TIMEOUT        = 60  # секунд на сбор ответов

_session = None

def _get_session():
    global _session
    if _session is None:
        import requests
        _session = requests.Session()
        _session.trust_env = False
    return _session


def _ask_council_http(question: str, models: list[str] | None = None) -> str:
    """Отправить вопрос в Council через /api/council/broadcast."""
    s = _get_session()
    resp = s.post(
        f"{COUNCIL_SERVER}/api/council/broadcast",
        json={"text": question},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    return data.get("task_id", "")


def _collect_responses_redis(
    task_id: str,
    models: list[str],
    timeout: float,
) -> dict[str, str]:
    """Слушать Redis pubsub и собирать ответы нод."""
    try:
        import redis
        r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)
        r.ping()
    except Exception:
        return {}

    collected: dict[str, str] = {}
    deadline = time.time() + timeout
    pub = r.pubsub()
    pub.subscribe(PUBSUB_CH)

    try:
        while time.time() < deadline and len(collected) < len(models):
            msg = pub.get_message(timeout=1.0)
            if not msg or msg["type"] != "message":
                continue
            try:
                data = json.loads(msg["data"])
            except Exception:
                continue
            sender = data.get("from", "")
            text   = data.get("text", "")
            if sender in models and sender not in collected and text:
                collected[sender] = text
    finally:
        pub.unsubscribe()
        pub.close()
        r.close()

    return collected


def ask_council(
    question: str,
    models: list[str] | None = None,
    timeout: float = TIMEOUT,
) -> dict[str, str]:
    """
    Задать вопрос моделям Council, вернуть их ответы.

    Args:
        question: текст вопроса
        models:   список моделей ["claude","gpt","deepseek"] (None = все три)
        timeout:  секунд ждать ответов

    Returns:
        {"claude": "...", "gpt": "...", "deepseek": "..."}
        Неответившие модели отсутствуют в dict.
    """
    if models is None:
        models = ["claude", "gpt", "deepseek"]

    # Проверить доступность сервера
    try:
        s = _get_session()
        s.get(f"{COUNCIL_SERVER}/api/council/state", timeout=3)
    except Exception:
        return {}

    # Отправить запрос
    try:
        task_id = _ask_council_http(question, models)
    except Exception as e:
        return {}

    # Собрать ответы через Redis pubsub
    return _collect_responses_redis(task_id, models, timeout)


def council_available() -> bool:
    """Проверить доступность Council Server."""
    try:
        s = _get_session()
        r = s.get(f"{COUNCIL_SERVER}/api/council/state", timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def get_council_state() -> dict:
    """Получить текущее состояние Council (паузы, блокировки)."""
    try:
        s = _get_session()
        r = s.get(f"{COUNCIL_SERVER}/api/council/state", timeout=3)
        return r.json()
    except Exception:
        return {}


if __name__ == "__main__":
    print("Council доступен:", council_available())
    state = get_council_state()
    print("State:", state)

    if council_available():
        print("\nОтправляем тестовый вопрос...")
        answers = ask_council(
            "Что такое DHT? Ответь в 2-3 предложениях.",
            timeout=30,
        )
        for model, ans in answers.items():
            print(f"\n[{model}]: {ans[:200]}")
