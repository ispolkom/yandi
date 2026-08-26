"""
assistant/orch_unanswered.py — Unanswered Query Detector.
Детектирует слабые ответы оркестратора и запускает дообучение.

Цикл:
  Запрос → оркестратор → confidence < THRESHOLD
  → record_unanswered(query, tag, confidence)
  → Redis событие orch:unanswered:<tag>
  → bootstrap_background(tag) — нода дообучается на накопленных запросах

CLI:
  python3 assistant/orch_unanswered.py listen       — слушать события и запускать bootstrap
  python3 assistant/orch_unanswered.py stats        — статистика неотвеченных запросов
  python3 assistant/orch_unanswered.py check <tag>  — принудительная проверка тега
"""
from __future__ import annotations

import json
import sys
import time
import threading
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

UNANSWERED_FILE  = BASE / "registry" / "query_archive" / "unanswered.jsonl"
REDIS_HOST       = "127.0.0.1"
REDIS_PORT       = 6379
REDIS_CH_PREFIX  = "orch:unanswered:"   # + tag

# Порог уверенности: ниже → считаем "неотвеченным"
CONFIDENCE_THRESHOLD = 0.5
# Минимум неотвеченных запросов в теге чтобы запустить bootstrap
MIN_UNANSWERED_FOR_BOOTSTRAP = 5
# Cooldown: не запускать bootstrap чаще чем раз в N секунд для одного тега
BOOTSTRAP_COOLDOWN = 3600  # 1 час

_cooldowns: dict[str, float] = {}
_lock = threading.Lock()


def record_unanswered(
    query: str,
    tag: str,
    confidence: float,
    answer: str = "",
    session_id: str = "",
) -> bool:
    """
    Зафиксировать слабый ответ. Если confidence ниже порога — публикует
    Redis-событие и возвращает True (был зафиксирован как неотвеченный).
    """
    if confidence >= CONFIDENCE_THRESHOLD:
        return False

    entry = {
        "query":      query[:300],
        "tag":        tag,
        "confidence": round(confidence, 3),
        "answer_len": len(answer),
        "session_id": session_id[:12] if session_id else "",
        "ts":         time.time(),
    }

    # Записать в файл
    UNANSWERED_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(UNANSWERED_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # Опубликовать в Redis (best-effort)
    _publish_event(tag, entry)

    # Проверить: достаточно ли неотвеченных чтобы запустить bootstrap
    _maybe_trigger_bootstrap(tag)

    return True


def _publish_event(tag: str, entry: dict):
    """Опубликовать событие в Redis pubsub."""
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        channel = REDIS_CH_PREFIX + tag.replace(":", "_")
        r.publish(channel, json.dumps(entry, ensure_ascii=False))
        r.close()
    except Exception:
        pass


def get_unanswered_stats(tag: str | None = None) -> dict:
    """Статистика неотвеченных запросов (по тегу или всего)."""
    if not UNANSWERED_FILE.exists():
        return {"total": 0, "by_tag": {}}

    total  = 0
    by_tag: dict[str, int] = {}

    with open(UNANSWERED_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                t = d.get("tag", "general")
                if tag and t != tag:
                    continue
                by_tag[t] = by_tag.get(t, 0) + 1
                total += 1
            except Exception:
                pass

    return {"total": total, "by_tag": by_tag}


def get_unanswered_count(tag: str) -> int:
    """Количество неотвеченных запросов для конкретного тега."""
    return get_unanswered_stats(tag).get("by_tag", {}).get(tag, 0)


def _maybe_trigger_bootstrap(tag: str):
    """Запустить bootstrap если накопилось достаточно неотвеченных запросов."""
    count = get_unanswered_count(tag)
    if count < MIN_UNANSWERED_FOR_BOOTSTRAP:
        return

    with _lock:
        now = time.time()
        last = _cooldowns.get(tag, 0)
        if now - last < BOOTSTRAP_COOLDOWN:
            return
        _cooldowns[tag] = now

    print(f"[Unanswered] Тег '{tag}': {count} слабых ответов → запускаем bootstrap", flush=True)
    try:
        from agent.orch_node_bootstrap import bootstrap_background
        bootstrap_background(tag, max_queries=50)
    except Exception as e:
        print(f"[Unanswered] Bootstrap не запустился: {e}", flush=True)


def listen_and_react(tags: list[str] | None = None, verbose: bool = True):
    """
    Слушать Redis-события о неотвеченных запросах и реагировать.
    Блокирующий вызов — запускать в отдельном треде или как daemon.

    Args:
        tags: конкретные теги для слушания (None = все через pattern)
    """
    try:
        import redis
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        pub = r.pubsub()

        if tags:
            channels = [REDIS_CH_PREFIX + t.replace(":", "_") for t in tags]
            pub.subscribe(*channels)
        else:
            pub.psubscribe(REDIS_CH_PREFIX + "*")

        if verbose:
            print(f"[Unanswered] Слушаем события неотвеченных запросов...", flush=True)

        for msg in pub.listen():
            if msg["type"] not in ("message", "pmessage"):
                continue
            try:
                entry = json.loads(msg["data"])
                tag   = entry.get("tag", "general")
                conf  = entry.get("confidence", 0)
                query = entry.get("query", "")[:60]
                if verbose:
                    print(f"[Unanswered] {tag} conf={conf:.2f}: {query}...", flush=True)
                _maybe_trigger_bootstrap(tag)
            except Exception:
                pass

    except Exception as e:
        if verbose:
            print(f"[Unanswered] Redis недоступен: {e}", flush=True)


def start_listener_daemon(tags: list[str] | None = None):
    """Запустить слушатель в фоновом daemon-треде."""
    t = threading.Thread(
        target=listen_and_react,
        args=(tags,),
        kwargs={"verbose": False},
        daemon=True,
        name="unanswered-listener",
    )
    t.start()
    return t


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "listen":
        listen_and_react(verbose=True)

    elif cmd == "stats":
        st = get_unanswered_stats()
        print(f"Неотвеченных запросов: {st['total']}")
        if st["by_tag"]:
            print("По тегам:")
            for tag, cnt in sorted(st["by_tag"].items(), key=lambda x: -x[1]):
                ready = "→ BOOTSTRAP" if cnt >= MIN_UNANSWERED_FOR_BOOTSTRAP else ""
                print(f"  {tag:<30} {cnt:>4}  {ready}")
        else:
            print("  (нет данных)")

    elif cmd == "check":
        tag = sys.argv[2] if len(sys.argv) > 2 else "general"
        count = get_unanswered_count(tag)
        print(f"Тег '{tag}': {count} неотвеченных")
        if count >= MIN_UNANSWERED_FOR_BOOTSTRAP:
            print("→ Достаточно для bootstrap")
            _maybe_trigger_bootstrap(tag)
        else:
            print(f"→ Нужно ещё {MIN_UNANSWERED_FOR_BOOTSTRAP - count}")

    elif cmd == "test":
        print("Тест: записываем слабый ответ...")
        result = record_unanswered(
            query="Как заменить тормозные колодки на Тойоте?",
            tag="авто:тормоза:тойота",
            confidence=0.3,
            answer="Не знаю",
        )
        print(f"Зафиксирован: {result}")
        st = get_unanswered_stats()
        print(f"Статистика: {st}")

    else:
        print("Команды: listen, stats, check <tag>, test")
