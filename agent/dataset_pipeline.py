#!/usr/bin/env python3
"""
assistant/dataset_pipeline.py — автоматический пайплайн датасетов.

Поток: council:chat:messages (Redis)
  → ConversationSplitter  (по времени + теме)
  → QualityFilter         (dedup + длина + scoring)
  → DatasetWriter         (draft/ → final/ JSONL)

Команды:
  python3 dataset_pipeline.py build   — собрать черновик из Redis
  python3 dataset_pipeline.py filter  — отфильтровать draft → final
  python3 dataset_pipeline.py stats   — статистика датасетов
  python3 dataset_pipeline.py run     — build + filter за раз
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import redis

# ── Пути ─────────────────────────────────────────────────────────────────────

BASE        = Path(__file__).parent.parent
DATASET_DIR = BASE / "registry" / "dataset"
DRAFT_DIR   = DATASET_DIR / "draft"
FINAL_DIR   = DATASET_DIR / "final"
LOG_DIR     = DATASET_DIR / "logs"

for d in (DRAFT_DIR, FINAL_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

MESSAGES_KEY = "council:chat:messages"

# ── Вспомогательные ──────────────────────────────────────────────────────────

STOPWORDS = {
    "и", "в", "на", "с", "по", "а", "но", "или", "как", "что", "это",
    "не", "из", "к", "у", "за", "от", "до", "для", "при", "он", "она",
    "они", "мы", "вы", "я", "то", "же", "if", "the", "a", "an", "is",
    "in", "of", "to", "and", "or", "for", "with", "are", "be", "it",
}

TOPIC_KEYWORDS: dict[str, list[str]] = {
    "dataset":      ["датасет", "dataset", "фильтр", "filter", "pipeline", "пайплайн"],
    "architecture": ["архитектур", "модуль", "модели", "module", "architecture", "schema"],
    "security":     ["безопасност", "sudo", "изоляц", "permission", "pet_claude", "firejail"],
    "council":      ["совет", "council", "broadcast", "модель", "claude", "gpt", "deepseek"],
    "coding":       ["код", "code", "python", "функци", "класс", "function", "class", "bug"],
    "search":       ["поиск", "search", "найди", "fetch", "ddg", "google"],
    "registry":     ["реестр", "registry", "scribe", "flood", "записать", "decision"],
    "yandi":        ["yandi", "mesh", "p2p", "dht", "rust", "узел"],
    "pet":          ["pet", "pipeline", "inference", "embedding", "train", "обучен"],
}

MIN_TEXT_LEN    = 30     # символов
MAX_TEXT_LEN    = 15000
MIN_WORD_COUNT  = 5
SESSION_GAP_MIN = 30     # минут тишины = новая сессия
DEDUP_THRESHOLD = 0.85   # MinHash-подобная схожесть

# ── Паттерны мусора — немедленный REJECT ─────────────────────────────────────

# Мета-отказы: модель комментирует механику вместо ответа
_META_REFUSE_PATTERNS = [
    re.compile(p, re.I) for p in [
        r'это\s+(дубль|повтор|копия)',
        r'я\s+уже\s+(дал|ответил|написал|сказал|провёл)',
        r'(дублирует|повторяет)\s+(предыдущ|мой)',
        r'было\s+отправлено\s+повторно',
        r'вопрос\s+(был\s+)?(задан\s+)?повторно',
        r'отвечать\s+на\s+этот\s+вопрос\s+снова',
        r'уточни[,\s].+(был|повторн)',
        r"don.t\s+repeat",
        r'already\s+answered\s+this',
    ]
]

# Системный префикс extension без содержания
_SYSTEM_PREFIX_PATTERNS = [
    re.compile(p, re.I) for p in [
        r'^claude\s+responded:\s*$',
        r'^(gpt|deepseek|claude)\s+responded:\s+[А-Яа-я]{1,20}\s*$',
        r'^\[timeout',
        r'^error:',
    ]
]

# Эхо-ответ: текст содержит >60% входного промпта (модель вернула документ)
_ECHO_MARKERS = [
    "НАЧАЛО ДОКУМЕНТА", "КОНЕЦ ДОКУМЕНТА",
    "Ты участник совета AI-агентов. Перед тобой описание",
    "Отвечай конкретно, без воды. Это рабочий технический анализ",
]


# ── MinHash-лёгкий (без зависимостей) ────────────────────────────────────────

def _shingles(text: str, k: int = 4) -> set[str]:
    text = text.lower()
    return {text[i:i+k] for i in range(len(text) - k + 1)}

def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)

def _text_hash(text: str) -> str:
    return hashlib.md5(text.strip().lower().encode()).hexdigest()


# ── ConversationSplitter ──────────────────────────────────────────────────────

class ConversationSplitter:
    """Разбивает поток сообщений на сессии (по времени + теме)."""

    def load_from_redis(self, r: redis.Redis) -> list[dict]:
        raw = r.lrange(MESSAGES_KEY, 0, -1)
        msgs = []
        for item in reversed(raw):  # Redis хранит новые первыми
            try:
                d = json.loads(item)
                msgs.append(d)
            except Exception:
                pass
        return msgs

    def _parse_ts(self, msg: dict) -> float:
        ts = msg.get("ts", "")
        # формат "HH:MM" или "HH:MM:SS"
        try:
            parts = ts.split(":")
            if len(parts) >= 2:
                h, m = int(parts[0]), int(parts[1])
                s = int(parts[2]) if len(parts) > 2 else 0
                # используем сегодняшнюю дату — для относительных меток
                now = datetime.now()
                dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
                return dt.timestamp()
        except Exception:
            pass
        return time.time()

    def _detect_topic(self, texts: list[str]) -> str:
        combined = " ".join(texts).lower()
        scores: dict[str, int] = {}
        for topic, keywords in TOPIC_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in combined)
            if score:
                scores[topic] = score
        if not scores:
            return "general"
        return max(scores, key=scores.get)

    def split(self, msgs: list[dict]) -> list[dict]:
        """Вернуть список сессий: [{session_id, topic, messages, time_start, time_end}]."""
        if not msgs:
            return []

        sessions = []
        current: list[dict] = []
        last_ts = None

        for msg in msgs:
            ts = self._parse_ts(msg)
            if last_ts is not None:
                gap = (ts - last_ts) / 60  # минуты
                if gap > SESSION_GAP_MIN:
                    if current:
                        sessions.append(self._make_session(current))
                    current = []
            current.append({**msg, "_ts": ts})
            last_ts = ts

        if current:
            sessions.append(self._make_session(current))

        return sessions

    def _make_session(self, msgs: list[dict]) -> dict:
        texts = [m.get("text", "") for m in msgs]
        topic = self._detect_topic(texts)
        ts_start = msgs[0]["_ts"]
        ts_end   = msgs[-1]["_ts"]
        sid = f"{datetime.fromtimestamp(ts_start).strftime('%Y%m%d_%H%M')}_{topic}"
        return {
            "session_id":  sid,
            "topic":       topic,
            "time_start":  datetime.fromtimestamp(ts_start).isoformat(),
            "time_end":    datetime.fromtimestamp(ts_end).isoformat(),
            "msg_count":   len(msgs),
            "messages":    [_clean_msg(m) for m in msgs],
        }


def _clean_msg(m: dict) -> dict:
    return {
        "from": m.get("from", "?"),
        "text": m.get("text", "").strip(),
        "ts":   m.get("ts", ""),
    }


# ── QualityFilter ─────────────────────────────────────────────────────────────

class QualityFilter:
    """Оценивает и фильтрует сессии и отдельные сообщения."""

    def __init__(self):
        self._seen_hashes: set[str] = set()
        self._seen_shingles: list[set] = []

    def score_message(self, msg: dict) -> dict:
        """Добавить поле score и verdict к сообщению."""
        text = msg.get("text", "")
        issues = []
        score  = 100

        # ── Немедленный REJECT: мета-отказы ──────────────────────────────────
        for pat in _META_REFUSE_PATTERNS:
            if pat.search(text):
                issues.append("meta_refuse")
                score = 0
                break

        # ── Немедленный REJECT: системные префиксы без содержания ────────────
        if score > 0:
            for pat in _SYSTEM_PREFIX_PATTERNS:
                if pat.search(text):
                    issues.append("system_prefix_only")
                    score = 0
                    break

        # ── Немедленный REJECT: эхо-ответ (модель вернула входной документ) ──
        if score > 0:
            echo_hits = sum(1 for m in _ECHO_MARKERS if m in text)
            if echo_hits >= 2:
                issues.append("echo_response")
                score = 0

        # ── Обычные проверки (только если не уже REJECT) ─────────────────────
        if score > 0:
            # Длина
            if len(text) < MIN_TEXT_LEN:
                issues.append("too_short")
                score -= 40
            if len(text) > MAX_TEXT_LEN:
                issues.append("too_long")
                score -= 10

            # Слов
            words = [w for w in text.lower().split() if w not in STOPWORDS]
            if len(words) < MIN_WORD_COUNT:
                issues.append("few_words")
                score -= 30

            # Мусор — повторяющиеся символы
            if re.search(r'(.)\1{6,}', text):
                issues.append("repetitive")
                score -= 20

            # Timeout-сообщения от content script
            if "[timeout" in text.lower() or "permission denied" in text.lower():
                issues.append("system_error")
                score -= 60

            # TTR — лексическое разнообразие
            words_all = text.lower().split()
            if words_all:
                ttr = len(set(words_all)) / len(words_all)
                if ttr < 0.3:
                    issues.append("low_diversity")
                    score -= 15

        score = max(0, score)
        verdict = "keep" if score >= 60 else ("review" if score >= 35 else "reject")

        return {**msg, "score": score, "verdict": verdict, "issues": issues}

    def is_duplicate(self, text: str) -> bool:
        h = _text_hash(text)
        if h in self._seen_hashes:
            return True

        sh = _shingles(text)
        for seen in self._seen_shingles:
            if _jaccard(sh, seen) >= DEDUP_THRESHOLD:
                return True

        self._seen_hashes.add(h)
        self._seen_shingles.append(sh)
        return False

    def filter_session(self, session: dict) -> dict:
        """Отфильтровать сообщения внутри сессии, добавить summary."""
        scored = []
        dedup_removed = 0

        for msg in session["messages"]:
            text = msg.get("text", "")
            if self.is_duplicate(text):
                dedup_removed += 1
                continue
            scored.append(self.score_message(msg))

        keep   = [m for m in scored if m["verdict"] == "keep"]
        review = [m for m in scored if m["verdict"] == "review"]
        reject = [m for m in scored if m["verdict"] == "reject"]

        avg_score = (sum(m["score"] for m in scored) / len(scored)) if scored else 0

        return {
            **session,
            "messages":       scored,
            "filter_summary": {
                "total":         len(session["messages"]),
                "dedup_removed": dedup_removed,
                "keep":          len(keep),
                "review":        len(review),
                "reject":        len(reject),
                "avg_score":     round(avg_score, 1),
                "ready":         avg_score >= 60 and len(keep) >= 2,
            },
        }


# ── DatasetWriter ─────────────────────────────────────────────────────────────

class DatasetWriter:

    def write_draft(self, sessions: list[dict]) -> Path:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = DRAFT_DIR / f"draft_{ts}.jsonl"
        with open(path, "w", encoding="utf-8") as f:
            for s in sessions:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")
        return path

    def write_final(self, sessions: list[dict], source_draft: Path) -> Path:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = FINAL_DIR / f"final_{ts}.jsonl"
        hf_path = FINAL_DIR / f"final_{ts}_hf.jsonl"  # HuggingFace формат

        ready = [s for s in sessions if s.get("filter_summary", {}).get("ready")]

        with open(path, "w", encoding="utf-8") as f:
            for s in ready:
                f.write(json.dumps(s, ensure_ascii=False) + "\n")

        # HuggingFace datasets-совместимый формат: один пример = один диалог
        with open(hf_path, "w", encoding="utf-8") as f:
            for s in ready:
                for msg in s["messages"]:
                    if msg.get("verdict") != "keep":
                        continue
                    row = {
                        "session_id":  s["session_id"],
                        "topic":       s["topic"],
                        "time_start":  s["time_start"],
                        "role":        msg["from"],
                        "content":     msg["text"],
                        "score":       msg.get("score", 0),
                        "source":      str(source_draft.name),
                    }
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

        return path

    def write_log(self, stats: dict) -> Path:
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = LOG_DIR / f"run_{ts}.json"
        path.write_text(json.dumps(stats, ensure_ascii=False, indent=2))
        return path

    def stats(self) -> dict:
        drafts = list(DRAFT_DIR.glob("*.jsonl"))
        finals = list(FINAL_DIR.glob("final_*.jsonl"))
        hf     = list(FINAL_DIR.glob("*_hf.jsonl"))

        total_hf_rows = 0
        for p in hf:
            try:
                total_hf_rows += sum(1 for _ in open(p, encoding="utf-8"))
            except Exception:
                pass

        return {
            "drafts":         len(drafts),
            "finals":         len(finals),
            "hf_files":       len(hf),
            "hf_total_rows":  total_hf_rows,
            "draft_dir":      str(DRAFT_DIR),
            "final_dir":      str(FINAL_DIR),
        }


# ── Pipeline — точка входа ────────────────────────────────────────────────────

class DatasetPipeline:

    def __init__(self, redis_url: str = "redis://127.0.0.1:6379/0"):
        self.r       = redis.from_url(redis_url)
        self.splitter = ConversationSplitter()
        self.writer   = DatasetWriter()

    def build(self, verbose: bool = True) -> Path:
        """Читать Redis → сплит → черновик."""
        if verbose:
            print("[pipeline] читаю сообщения из Redis...")
        msgs = self.splitter.load_from_redis(self.r)
        if verbose:
            print(f"  загружено: {len(msgs)} сообщений")

        sessions = self.splitter.split(msgs)
        if verbose:
            topics = ", ".join(f"{s['topic']}({s['msg_count']})" for s in sessions)
            print(f"  сессий: {len(sessions)} — {topics}")

        path = self.writer.write_draft(sessions)
        if verbose:
            print(f"  черновик → {path}")
        return path

    def filter(self, draft_path: Path = None, verbose: bool = True) -> Path:
        """Черновик → фильтр → финал."""
        if draft_path is None:
            drafts = sorted(DRAFT_DIR.glob("*.jsonl"), reverse=True)
            if not drafts:
                raise FileNotFoundError("Нет черновиков. Сначала запусти build.")
            draft_path = drafts[0]

        if verbose:
            print(f"[pipeline] фильтрую: {draft_path.name}")

        sessions = []
        with open(draft_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    sessions.append(json.loads(line))

        qf = QualityFilter()
        filtered = [qf.filter_session(s) for s in sessions]

        ready = [s for s in filtered if s["filter_summary"]["ready"]]
        if verbose:
            for s in filtered:
                fs = s["filter_summary"]
                status = "✓" if fs["ready"] else "✗"
                print(f"  {status} {s['session_id']}: keep={fs['keep']} "
                      f"review={fs['review']} reject={fs['reject']} "
                      f"score={fs['avg_score']} dedup={fs['dedup_removed']}")
            print(f"  готово к финалу: {len(ready)}/{len(filtered)} сессий")

        final_path = self.writer.write_final(filtered, draft_path)

        stats = {
            "ts":           datetime.now().isoformat(),
            "draft":        str(draft_path),
            "final":        str(final_path),
            "sessions_in":  len(sessions),
            "sessions_out": len(ready),
            "topics":       list({s["topic"] for s in ready}),
        }
        self.writer.write_log(stats)

        if verbose:
            print(f"  финал → {final_path}")
        return final_path

    def run(self, verbose: bool = True) -> dict:
        """build + filter за один вызов."""
        draft  = self.build(verbose=verbose)
        final  = self.filter(draft, verbose=verbose)
        stats  = self.writer.stats()
        if verbose:
            print(f"\n[pipeline] готово: {stats['hf_total_rows']} строк в HF-формате")
        return {"draft": str(draft), "final": str(final), **stats}

    def show_stats(self):
        s = self.writer.stats()
        print(f"Черновиков:      {s['drafts']}")
        print(f"Финалов:         {s['finals']}")
        print(f"HF-файлов:       {s['hf_files']}")
        print(f"Строк (HF):      {s['hf_total_rows']}")
        print(f"Draft dir:       {s['draft_dir']}")
        print(f"Final dir:       {s['final_dir']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    p   = DatasetPipeline()

    if cmd == "build":
        p.build()
    elif cmd == "filter":
        draft = Path(sys.argv[2]) if len(sys.argv) > 2 else None
        p.filter(draft)
    elif cmd == "stats":
        p.show_stats()
    elif cmd == "run":
        p.run()
    else:
        print("Usage: dataset_pipeline.py [build|filter|stats|run]")
