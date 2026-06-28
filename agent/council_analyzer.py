#!/usr/bin/env python3
"""
assistant/council_analyzer.py — анализ диалогов совета через Qwen3:14b.

Разбивает историю чата на Q&A-треды (вопрос → ответы каждой модели),
запускает сравнительный анализ через локальный Qwen3:14b (Ollama),
сохраняет отдельный файл на каждый вопрос + индекс по темам.

Структура вывода:
  registry/analysis/
    q_<HASH>.json        — анализ одного вопроса
    index.json           — индекс всех вопросов (тема, дата, согласие)
    topics/              — кластеры по теме

Команды CLI:
  python3 assistant/council_analyzer.py run       — полный прогон
  python3 assistant/council_analyzer.py index     — только показать индекс
  python3 assistant/council_analyzer.py analyze <q_HASH>  — один вопрос
  python3 assistant/council_analyzer.py topics    — кластеры по темам
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis
import requests

BASE              = Path(__file__).parent.parent
ANALYSIS_DIR      = BASE / "registry" / "analysis"
TOPICS_DIR        = ANALYSIS_DIR / "topics"
INDEX_FILE        = ANALYSIS_DIR / "index.json"
THREADS_CACHE_FILE = ANALYSIS_DIR / "threads_cache.json"

ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
TOPICS_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA_URL    = "http://127.0.0.1:11434"
ANALYSIS_MODEL = "qwen3:14b"
EMBED_MODEL    = "nomic-embed-text"

REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
MESSAGES_KEY = "council:chat:messages"

MODELS = ("claude", "gpt", "deepseek")

# Минимальная длина ответа чтобы считать его реальным
MIN_REPLY_LEN  = 80
MIN_QUESTION_LEN = 15  # вопрос короче → тривиальный, не анализировать

# Макро-категории тем для нормализации
MACRO_TOPICS = {
    "архитектура":    ["архитектур", "architecture", "слой", "layer", "stack", "стек", "api", "эндпоинт", "endpoint", "модуль", "module", "протокол", "protocol"],
    "датасет":        ["датасет", "dataset", "фильтр", "pipeline", "пайплайн", "запис", "jsonl", "обучен", "train", "fine-tun"],
    "браузер":        ["браузер", "browser", "firefox", "chrome", "playwright", "automation", "автоматиза", "chromium"],
    "совет":          ["совет", "council", "модел", "claude", "gpt", "deepseek", "broadcast", "голосован"],
    "yandi_pet":      ["yandi", "pet", "mesh", "p2p", "dht", "федерац", "federation", "rust", "inference", "inference"],
    "security":       ["безопасност", "security", "policy", "trust", "identity", "auth", "permission"],
    "devops":         ["перезапуск", "restart", "сервер", "server", "deploy", "запуск", "лог", "log", "daemon"],
    "memory_kg":      ["память", "memory", "граф", "graph", "knowledge", "решени", "decision", "рефлекс"],
    "small_talk":     ["привет", "дела", "скучаешь", "новост", "hello", "hi", "как дела"],
}

def _classify_macro_topic(topic_text: str, question: str) -> str:
    """Классифицировать тему в одну из макро-категорий."""
    combined = (topic_text + " " + question).lower()
    scores: dict[str, int] = {}
    for macro, keywords in MACRO_TOPICS.items():
        scores[macro] = sum(1 for kw in keywords if kw in combined)
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "другое"


# Паттерны мусорных ответов (не сохранять в тред)
_JUNK_PATTERNS = [
    re.compile(p, re.I) for p in [
        r'это\s+(дубль|повтор)',
        r'я\s+уже\s+(дал|ответил|написал)',
        r'already\s+answered',
        r'^claude\s+responded:\s*$',
        r'^\s*Э\s*$',  # обрезанный ответ
    ]
]


def _is_junk(text: str) -> bool:
    if len(text.strip()) < MIN_REPLY_LEN:
        return True
    return any(p.search(text) for p in _JUNK_PATTERNS)


def _qid(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ── Redis helpers ─────────────────────────────────────────────────────────────

def load_messages() -> list[dict]:
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    raw = r.lrange(MESSAGES_KEY, 0, -1)
    msgs = []
    for item in reversed(raw):  # Redis lpush → reversed = chronological
        try:
            msgs.append(json.loads(item))
        except Exception:
            pass
    return msgs


def _redis_message_count() -> int:
    try:
        r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        return r.llen(MESSAGES_KEY)
    except Exception:
        return -1


# ── Thread cache (watermark) ──────────────────────────────────────────────────

def load_threads_cache() -> tuple[list[dict], int]:
    """Возвращает (threads, watermark). watermark=0 если кэша нет."""
    if not THREADS_CACHE_FILE.exists():
        return [], 0
    try:
        data = json.loads(THREADS_CACHE_FILE.read_text())
        return data.get("threads", []), data.get("watermark", 0)
    except Exception:
        return [], 0


def save_threads_cache(threads: list[dict], watermark: int):
    THREADS_CACHE_FILE.write_text(
        json.dumps({"watermark": watermark, "threads": threads},
                   ensure_ascii=False, indent=2)
    )


# ── Разбивка на Q&A-треды ────────────────────────────────────────────────────

def extract_threads(messages: list[dict]) -> list[dict]:
    """
    Группирует сообщения в треды:
    тред = одно сообщение human + ответы каждой из моделей (до следующего human).
    """
    threads = []
    current_question = None
    current_replies: dict[str, list[str]] = {m: [] for m in MODELS}

    def _flush():
        if not current_question:
            return
        replies = {}
        for model in MODELS:
            good = [r for r in current_replies[model] if not _is_junk(r)]
            if good:
                # берём самый длинный (наиболее полный) ответ
                replies[model] = max(good, key=len)
        if replies:  # хотя бы одна модель ответила
            threads.append({
                "id":       _qid(current_question),
                "question": current_question,
                "replies":  replies,
                "ts":       datetime.now().isoformat(),
            })

    for msg in messages:
        role = msg.get("from", msg.get("role", ""))
        text = msg.get("text", msg.get("content", "")).strip()
        if not text:
            continue

        if role == "human":
            _flush()
            current_question = text
            current_replies = {m: [] for m in MODELS}
        elif role in MODELS and current_question:
            current_replies[role].append(text)

    _flush()
    return threads


# ── Ollama helpers ────────────────────────────────────────────────────────────

def _ollama_generate(prompt: str, model: str = ANALYSIS_MODEL,
                     system: str = "", timeout: int = 120) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 1024},
    }
    if system:
        payload["system"] = system
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/generate",
            json=payload, timeout=timeout,
            proxies={"http": None, "https": None},
        )
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"[ERROR: {e}]"


def _ollama_embed(text: str) -> list[float]:
    try:
        resp = requests.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": text[:512]},
            timeout=30,
            proxies={"http": None, "https": None},
        )
        return resp.json().get("embeddings", [[]])[0]
    except Exception:
        return []


# ── Анализ одного треда ───────────────────────────────────────────────────────

SYSTEM_PROMPT = """Ты аналитик, который сравнивает ответы трёх ИИ-систем на один вопрос.
Отвечай строго в JSON. Никаких пояснений вне JSON."""

def analyze_thread(thread: dict, verbose: bool = True) -> dict:
    q = thread["question"]
    replies = thread["replies"]

    if not replies:
        return {**thread, "analysis": None, "status": "no_replies"}

    # Формируем промпт
    replies_text = ""
    for model in MODELS:
        if model in replies:
            r = replies[model]
            # Убираем системный префикс "Claude responded:"
            r = re.sub(r'^(claude|gpt|deepseek)\s+responded:\s*', '', r, flags=re.I).strip()
            replies_text += f"\n\n[{model.upper()}]:\n{r[:1500]}"

    prompt = f"""Вопрос: {q[:500]}

Ответы моделей:{replies_text}

Верни JSON с полями:
{{
  "topic": "одна фраза — о чём вопрос",
  "consensus": true/false,
  "agreement_level": 0-100,
  "key_points": ["пункт1", "пункт2", ...],  // 3-5 общих тезисов
  "disagreements": ["разногласие1", ...],    // что модели думают по-разному
  "best_reply": "claude|gpt|deepseek|tie",   // чей ответ наиболее полный
  "best_reason": "почему",
  "rejected_models": ["model если ответ мусор"],
  "summary": "2-3 предложения итога"
}}"""

    if verbose:
        print(f"  🔍 Анализирую: {q[:60]}...", flush=True)

    raw = _ollama_generate(prompt, system=SYSTEM_PROMPT)

    # Парсим JSON из ответа
    analysis = None
    try:
        # Qwen3 может добавить <think>...</think> блок — убираем
        clean = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()
        # Ищем JSON
        m = re.search(r'\{[\s\S]+\}', clean)
        if m:
            analysis = json.loads(m.group(0))
    except Exception as e:
        analysis = {"parse_error": str(e), "raw": raw[:300]}

    # Embedding для кластеризации
    embed_text = f"{q} {analysis.get('topic','') if analysis else ''}"
    embedding = _ollama_embed(embed_text)

    result = {
        **thread,
        "analysis":  analysis,
        "embedding": embedding[:64] if embedding else [],  # первые 64 для экономии
        "analyzed_at": datetime.now().isoformat(),
        "status":    "ok" if analysis and "parse_error" not in analysis else "parse_error",
    }
    return result


# ── Сохранение и индексирование ───────────────────────────────────────────────

def save_thread(result: dict) -> Path:
    qid = result["id"]
    path = ANALYSIS_DIR / f"q_{qid}.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return path


def load_index() -> dict:
    if INDEX_FILE.exists():
        try:
            return json.loads(INDEX_FILE.read_text())
        except Exception:
            pass
    return {"threads": {}, "topics": {}}


def update_index(result: dict):
    idx = load_index()
    qid = result["id"]
    analysis = result.get("analysis") or {}

    raw_topic = analysis.get("topic", "")
    topic = _classify_macro_topic(raw_topic, result["question"])

    idx["threads"][qid] = {
        "id":         qid,
        "question":   result["question"][:120],
        "topic":      topic,
        "topic_raw":  raw_topic,
        "consensus":  analysis.get("consensus", None),
        "agreement":  analysis.get("agreement_level", 0),
        "best_reply": analysis.get("best_reply", ""),
        "models":     list(result.get("replies", {}).keys()),
        "ts":         result.get("analyzed_at", ""),
        "status":     result.get("status", ""),
    }

    idx["topics"].setdefault(topic, [])
    if qid not in idx["topics"][topic]:
        idx["topics"][topic].append(qid)

    INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=2))


def save_topic_file(topic: str, thread_ids: list[str]):
    """Один файл на тему со всеми треда-анализами."""
    slug = re.sub(r'[^\w\-]', '_', topic.lower())[:40]
    path = TOPICS_DIR / f"{slug}.json"
    threads = []
    for qid in thread_ids:
        f = ANALYSIS_DIR / f"q_{qid}.json"
        if f.exists():
            try:
                threads.append(json.loads(f.read_text()))
            except Exception:
                pass
    data = {
        "topic": topic,
        "count": len(threads),
        "threads": [
            {
                "id": t["id"],
                "question": t["question"][:200],
                "analysis": t.get("analysis"),
            }
            for t in threads
        ],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def cmd_run(max_threads: int = 50, verbose: bool = True):
    current_count = _redis_message_count()
    cached_threads, watermark = load_threads_cache()

    if current_count == watermark and cached_threads:
        print(f"[analyzer] кэш актуален ({watermark} сообщений) — Redis не трогаем", flush=True)
        threads = cached_threads
    else:
        print(f"[analyzer] загружаю сообщения из Redis...", flush=True)
        messages = load_messages()
        actual_count = len(messages)
        print(f"[analyzer] {actual_count} сообщений (было {watermark})", flush=True)

        threads = extract_threads(messages)

        # Фильтруем тривиальные вопросы и без ответов
        threads = [
            t for t in threads
            if len(t["question"].strip()) >= MIN_QUESTION_LEN and t.get("replies")
        ]

        # Сохраняем/обновляем кэш тредов
        save_threads_cache(threads, actual_count)
        print(f"[analyzer] кэш обновлён → {len(threads)} тредов, watermark={actual_count}", flush=True)

    print(f"[analyzer] {len(threads)} Q&A-тредов найдено", flush=True)

    # Пропускаем уже проанализированные
    idx = load_index()
    existing = set(idx["threads"].keys())
    new_threads = [t for t in threads if t["id"] not in existing]
    print(f"[analyzer] новых: {len(new_threads)} (уже есть: {len(existing)})", flush=True)

    if not new_threads:
        print("[analyzer] всё уже проанализировано")
        return

    new_threads = new_threads[:max_threads]
    ok = 0
    for i, thread in enumerate(new_threads):
        print(f"\n[{i+1}/{len(new_threads)}] {thread['question'][:70]}...")
        result = analyze_thread(thread, verbose=verbose)
        save_thread(result)
        update_index(result)
        if result["status"] == "ok":
            ok += 1
            a = result.get("analysis", {})
            print(f"  ✓ тема={a.get('topic','')} consensus={a.get('consensus')} agreement={a.get('agreement_level')}%")
            print(f"    best={a.get('best_reply')} | {a.get('summary','')[:100]}")
        else:
            print(f"  ⚠ {result['status']}")
        time.sleep(0.5)  # не перегружаем Ollama

    # Обновляем topic-файлы
    idx = load_index()
    for topic, qids in idx["topics"].items():
        save_topic_file(topic, qids)

    print(f"\n[analyzer] готово: {ok}/{len(new_threads)} успешно")
    print(f"  📁 {ANALYSIS_DIR}")
    print(f"  📋 индекс: {INDEX_FILE}")


def cmd_index():
    idx = load_index()
    threads = idx.get("threads", {})
    topics = idx.get("topics", {})
    print(f"\n📋 Индекс: {len(threads)} вопросов, {len(topics)} тем\n")
    print(f"{'Тема':<30} {'Вопросов':>8}")
    print("-" * 42)
    for topic, qids in sorted(topics.items(), key=lambda x: -len(x[1])):
        print(f"  {topic:<30} {len(qids):>6}")
    print("\nПоследние анализы:")
    recent = sorted(threads.values(), key=lambda x: x.get("ts",""), reverse=True)[:10]
    for t in recent:
        ag = t.get("agreement", 0)
        mark = "✅" if t.get("consensus") else "⚡"
        print(f"  {mark} [{ag:3d}%] {t['question'][:60]}")
        print(f"       тема: {t['topic']} | лучший: {t.get('best_reply','?')}")


def cmd_topics():
    for f in sorted(TOPICS_DIR.glob("*.json")):
        data = json.loads(f.read_text())
        print(f"\n📂 {data['topic']} ({data['count']} вопросов)")
        for t in data["threads"][:3]:
            a = t.get("analysis") or {}
            print(f"  • {t['question'][:70]}")
            print(f"    → {a.get('summary','')[:100]}")


def cmd_cache_status():
    current = _redis_message_count()
    cached_threads, watermark = load_threads_cache()
    print(f"Redis сообщений сейчас : {current}")
    print(f"Watermark (последний run): {watermark}")
    print(f"Кэшировано тредов       : {len(cached_threads)}")
    if current == watermark:
        print("Статус: ✅ актуален — Redis пропустим при следующем run")
    else:
        delta = current - watermark
        print(f"Статус: 🔄 устарел (+{delta} новых сообщений) — нужен re-parse")
    if THREADS_CACHE_FILE.exists():
        sz = THREADS_CACHE_FILE.stat().st_size
        print(f"Файл кэша: {THREADS_CACHE_FILE} ({sz} bytes)")


def cmd_cache_reset():
    if THREADS_CACHE_FILE.exists():
        THREADS_CACHE_FILE.unlink()
        print("Кэш тредов удалён — следующий run перечитает Redis полностью")
    else:
        print("Кэша нет")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        max_t = int(sys.argv[2]) if len(sys.argv) > 2 else 50
        cmd_run(max_threads=max_t)
    elif cmd == "index":
        cmd_index()
    elif cmd == "topics":
        cmd_topics()
    elif cmd == "analyze":
        qid = sys.argv[2] if len(sys.argv) > 2 else ""
        f = ANALYSIS_DIR / f"q_{qid}.json"
        if f.exists():
            print(json.dumps(json.loads(f.read_text()), ensure_ascii=False, indent=2))
        else:
            print(f"Не найдено: {f}")
    elif cmd == "cache":
        sub = sys.argv[2] if len(sys.argv) > 2 else "status"
        if sub == "reset":
            cmd_cache_reset()
        else:
            cmd_cache_status()
    else:
        print("Usage: council_analyzer.py run [max]|index|topics|analyze <id>|cache [reset]")


if __name__ == "__main__":
    main()
