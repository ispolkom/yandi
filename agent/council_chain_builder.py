#!/usr/bin/env python3
"""
assistant/council_chain_builder.py — авто-анализ совета → второй датасет.

Когда все 3 модели ответили на broadcast:
  1. Извлекает вопрос + 3 ответа из Redis
  2. Qwen3:14b строит конкретную цепочку исполняемых шагов
  3. Пишет в registry/dataset/council_chains/YYYYMMDD.jsonl

Формат assistant-части — конкретные шаги демона:
  [
    {"cmd": "kg", "sub": "add_decision", "text": "...", "reason": "...", "label": "..."},
    {"cmd": "scribe", "text": "синтез", "label": "save_synthesis"},
    {"cmd": "broadcast", "text": "следующий вопрос", "label": "next_question"},
    ...
  ]

Все cmd соответствуют реальным командам daemon.on_control().

CLI:
  python3 assistant/council_chain_builder.py last       — обработать последний тред
  python3 assistant/council_chain_builder.py run        — все необработанные треды
  python3 assistant/council_chain_builder.py stats      — статистика
  python3 assistant/council_chain_builder.py tail [N]   — последние N цепочек
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
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.local_http import local_post

BASE       = Path(__file__).parent.parent
CHAINS_DIR = BASE / "registry" / "dataset" / "council_chains"
CACHE_FILE = CHAINS_DIR / "processed.json"
CHAINS_DIR.mkdir(parents=True, exist_ok=True)

OLLAMA_URL     = "http://127.0.0.1:11434"
ANALYSIS_MODEL = "qwen3:14b"

REDIS_HOST   = "127.0.0.1"
REDIS_PORT   = 6379
MESSAGES_KEY = "council:chat:messages"

SYSTEM_PROMPT = (
    "Ты оркестратор совета трёх ИИ-моделей. Получаешь вопрос модератора "
    "и независимые ответы Claude, GPT и DeepSeek. "
    "Синтезируешь ответы и выстраиваешь конкретную цепочку действий системы."
)

# Справочник реальных команд демона с описанием
COMMANDS_REF = """
Доступные команды демона (cmd → описание → обязательные поля):

ЗНАНИЯ И ПАМЯТЬ:
  kg / add_decision  → добавить решение в граф знаний
    {"cmd":"kg","sub":"add_decision","text":"решение","reason":"почему","expected":"ожидаемый результат","label":"метка"}
  kg / index         → переиндексировать граф знаний
    {"cmd":"kg","sub":"index","label":"reindex_kg"}
  scribe             → записать мысль/синтез в реестр через LLM
    {"cmd":"scribe","text":"текст для записи","label":"save_synthesis"}
  decision / add     → добавить решение в трекер
    {"cmd":"decision","sub":"add","text":"что решили","reason":"почему","expected":"результат","label":"track_decision"}

ПОИСК:
  search             → веб-поиск через DuckDuckGo
    {"cmd":"search","query":"поисковый запрос","label":"web_search"}
  code_search        → поиск по исходникам проекта
    {"cmd":"code_search","query":"что ищем","mode":"search","label":"code_search"}
  file_search        → поиск файлов по паттерну
    {"cmd":"file_search","dirs":["/path"],"pattern":"*.py","label":"find_files"}

СОВЕТ:
  broadcast          → задать вопрос всем трём моделям одновременно
    {"cmd":"broadcast","text":"следующий вопрос","label":"next_question"}
  session_save       → сохранить текущую сессию совета с summary
    {"cmd":"session_save","topic":"название темы","label":"save_session"}

ОРКЕСТРАТОР:
  orch               → выполнить задачу через оркестратор (classify→model→execute)
    {"cmd":"orch","task":"задача","label":"orchestrate"}

ДАТАСЕТ:
  dataset / run      → пересобрать датасет (draft→filter→final)
    {"cmd":"dataset","sub":"run","label":"rebuild_dataset"}
  adversarial        → запустить дебат по тезису
    {"cmd":"adversarial","sub":"defend_claim","claim":"тезис","label":"debate"}

АНАЛИЗ:
  reflect            → запустить рефлексию системы
    {"cmd":"reflect","label":"reflect"}

SHELL:
  shell              → выполнить разрешённую команду
    {"cmd":"shell","run":"команда","label":"run_cmd"}
"""

ANALYSIS_PROMPT_TPL = """Ты получил вопрос и ответы трёх ИИ-моделей. Построй цепочку конкретных действий.

=== ВОПРОС МОДЕРАТОРА ===
{question}

=== ОТВЕТ CLAUDE ===
{claude}

=== ОТВЕТ GPT ===
{gpt}

=== ОТВЕТ DEEPSEEK ===
{deepseek}

=== ДОСТУПНЫЕ КОМАНДЫ ===
{commands}

=== ЗАДАЧА ===
1. Проанализируй ответы: найди консенсус, противоречия, ключевые инсайты.
2. Построй цепочку из 2–5 конкретных шагов которые система должна выполнить.
3. Каждый шаг — реальная команда демона из справочника выше.
4. Шаги должны логически вытекать друг из друга.

Верни ТОЛЬКО валидный JSON без markdown-фенсов:
{{
  "topic": "краткая тема одной строкой",
  "macro_topic": "архитектура|датасет|совет|yandi_pet|security|devops|memory_kg|small_talk",
  "consensus_score": 0.0-1.0,
  "synthesis": "синтез ответов 3-5 предложений",
  "key_insights": ["инсайт 1", "инсайт 2", "инсайт 3"],
  "disagreements": ["противоречие 1"],
  "steps": [
    {{"cmd": "...", ...поля команды..., "label": "метка", "reason": "зачем этот шаг"}},
    {{"cmd": "...", ...поля команды..., "label": "метка", "reason": "зачем этот шаг"}},
    ...
  ]
}}"""


def _call_qwen(prompt: str, timeout: int = 180) -> Optional[str]:
    try:
        r = local_post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model":    ANALYSIS_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "stream":   False,
            },
            timeout=timeout,
        )
        raw = r.json()["message"]["content"].strip()
        return re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    except Exception:
        return None


def _extract_json(text: str) -> Optional[dict]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group())
    except Exception:
        return None


def _thread_id(question: str) -> str:
    return hashlib.md5(question.encode()).hexdigest()[:12]


def _validate_steps(steps: list) -> tuple[list, list]:
    """Проверяет и фильтрует шаги — возвращает (valid, errors)."""
    VALID_CMDS = {
        "kg", "scribe", "decision", "search", "code_search",
        "file_search", "broadcast", "session_save", "orch",
        "dataset", "adversarial", "reflect", "shell",
    }
    valid, errors = [], []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step[{i}] не dict")
            continue
        cmd = step.get("cmd", "")
        if cmd not in VALID_CMDS:
            errors.append(f"step[{i}] неизвестная cmd={cmd!r}")
            continue
        if "label" not in step:
            step["label"] = f"{cmd}_{i}"
        valid.append(step)
    return valid, errors


def _quality(steps: list, responses: dict, consensus: float) -> float:
    score = 0.4
    if len(steps) >= 2:     score += 0.15
    if len(steps) >= 3:     score += 0.1
    cmds = {s.get("cmd") for s in steps}
    if len(cmds) >= 2:      score += 0.1   # разнообразие команд
    if consensus > 0.6:     score += 0.1
    total_resp = sum(len(v) for v in responses.values())
    if total_resp > 500:    score += 0.1
    if total_resp > 2000:   score += 0.05
    return min(1.0, round(score, 2))


class CouncilChainBuilder:

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self._processed: set[str] = self._load_processed()

    def _load_processed(self) -> set[str]:
        if CACHE_FILE.exists():
            try:
                return set(json.loads(CACHE_FILE.read_text()))
            except Exception:
                pass
        return set()

    def _save_processed(self):
        CACHE_FILE.write_text(json.dumps(list(self._processed), ensure_ascii=False))

    def extract_last_broadcast_thread(self) -> Optional[dict]:
        """Найти последний broadcast-вопрос и ответы ≥2 моделей."""
        total = self.r.llen(MESSAGES_KEY)
        if total == 0:
            return None

        msgs = []
        for i in range(min(total, 80)):
            raw = self.r.lindex(MESSAGES_KEY, i)
            try:
                msgs.append(json.loads(raw))
            except Exception:
                pass

        for i, msg in enumerate(msgs):
            if msg.get("from") != "human" or len(msg.get("text", "")) < 100:
                continue
            responses: dict[str, str] = {}
            for j in range(i - 1, max(i - 25, -1), -1):
                if j < 0:
                    break
                who  = msgs[j].get("from", "")
                text = msgs[j].get("text", "")
                if who in ("claude", "gpt", "deepseek"):
                    if who not in responses or len(text) > len(responses[who]):
                        responses[who] = text
            if len(responses) >= 2:
                return {
                    "thread_id": _thread_id(msg["text"]),
                    "question":  msg["text"],
                    "responses": responses,
                    "ts":        datetime.now().isoformat(),
                }
        return None

    def build_chain(self, thread: dict, verbose: bool = False) -> Optional[dict]:
        tid       = thread["thread_id"]
        question  = thread["question"]
        responses = thread["responses"]

        if len(responses) < 2:
            return None

        if verbose:
            print(f"  [chain] {tid}: {len(responses)} моделей, анализирую через Qwen3...")

        prompt = ANALYSIS_PROMPT_TPL.format(
            question  = question[:1200],
            claude    = responses.get("claude",   "(нет ответа)")[:1000],
            gpt       = responses.get("gpt",      "(нет ответа)")[:1000],
            deepseek  = responses.get("deepseek", "(нет ответа)")[:1000],
            commands  = COMMANDS_REF,
        )

        raw = _call_qwen(prompt, timeout=180)
        if not raw:
            if verbose:
                print("  [chain] Qwen не ответил")
            return None

        analysis = _extract_json(raw)
        if not analysis:
            if verbose:
                print(f"  [chain] JSON не распознан: {raw[:300]}")
            return None

        raw_steps = analysis.get("steps", [])
        steps, step_errors = _validate_steps(raw_steps)

        if verbose and step_errors:
            print(f"  [chain] предупреждения шагов: {step_errors}")

        if not steps:
            if verbose:
                print("  [chain] нет валидных шагов после проверки")
            return None

        quality = _quality(steps, responses, analysis.get("consensus_score", 0))

        # user = вопрос + ответы всех моделей
        parts = [f"Вопрос модератора:\n{question[:1500]}"]
        for model in ("claude", "gpt", "deepseek"):
            if model in responses:
                parts.append(f"\nОтвет {model.capitalize()}:\n{responses[model][:1200]}")
        user_content = "\n\n".join(parts)

        # assistant = синтез + исполняемая цепочка шагов
        assistant_content = json.dumps({
            "synthesis":      analysis.get("synthesis", ""),
            "consensus_score":analysis.get("consensus_score", 0),
            "key_insights":   analysis.get("key_insights", []),
            "disagreements":  analysis.get("disagreements", []),
            "steps":          steps,
        }, ensure_ascii=False)

        return {
            "thread_id":     tid,
            "ts":            thread["ts"],
            "topic":         analysis.get("topic", ""),
            "macro_topic":   analysis.get("macro_topic", ""),
            "quality":       quality,
            "models_count":  len(responses),
            "consensus_score": analysis.get("consensus_score", 0),
            "steps_count":   len(steps),
            "step_cmds":     [s.get("cmd") for s in steps],
            "messages": [
                {"role": "system",    "content": SYSTEM_PROMPT},
                {"role": "user",      "content": user_content},
                {"role": "assistant", "content": assistant_content},
            ],
            "raw_responses": {k: v[:300] for k, v in responses.items()},
        }

    def save_chain(self, chain: dict) -> Path:
        day_file = CHAINS_DIR / f"{datetime.now().strftime('%Y%m%d')}.jsonl"
        with day_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(chain, ensure_ascii=False) + "\n")
        return day_file

    def process_last(self, verbose: bool = True) -> Optional[dict]:
        thread = self.extract_last_broadcast_thread()
        if not thread:
            if verbose:
                print("  [chain] broadcast-тред не найден")
            return None

        chain = self.build_chain(thread, verbose=verbose)
        if not chain:
            return None

        path = self.save_chain(chain)
        self._processed.add(thread["thread_id"])
        self._save_processed()

        if verbose:
            print(f"  ✓ chain сохранён: {path.name}")
            print(f"    тема: {chain['topic']}  consensus: {chain['consensus_score']}")
            print(f"    шаги ({chain['steps_count']}): {chain['step_cmds']}")
            try:
                asst  = json.loads(chain["messages"][2]["content"])
                for s in asst["steps"]:
                    print(f"      [{s['cmd']}] label={s.get('label')} reason={s.get('reason','')[:80]}")
            except Exception:
                pass

        return chain

    def run_all(self, verbose: bool = True) -> dict:
        total_msgs = self.r.llen(MESSAGES_KEY)
        if total_msgs == 0:
            return {"processed": 0, "skipped": 0, "errors": 0, "found_threads": 0}

        msgs = []
        for i in range(min(total_msgs, 300)):
            raw = self.r.lindex(MESSAGES_KEY, i)
            try:
                msgs.append(json.loads(raw))
            except Exception:
                pass

        threads = []
        for i, msg in enumerate(msgs):
            if msg.get("from") != "human" or len(msg.get("text", "")) < 100:
                continue
            tid = _thread_id(msg["text"])
            if tid in self._processed:
                continue
            responses: dict[str, str] = {}
            for j in range(i - 1, max(i - 25, -1), -1):
                if j < 0:
                    break
                who  = msgs[j].get("from", "")
                text = msgs[j].get("text", "")
                if who in ("claude", "gpt", "deepseek"):
                    if who not in responses or len(text) > len(responses[who]):
                        responses[who] = text
            if len(responses) >= 2:
                threads.append({
                    "thread_id": tid,
                    "question":  msg["text"],
                    "responses": responses,
                    "ts":        msg.get("_ts", time.time()),
                })

        processed = skipped = errors = 0
        for thread in threads:
            try:
                chain = self.build_chain(thread, verbose=verbose)
                if chain:
                    self.save_chain(chain)
                    self._processed.add(thread["thread_id"])
                    processed += 1
                    if verbose:
                        print(f"  ✓ {thread['thread_id']}: {chain['topic']} шаги={chain['step_cmds']}")
                else:
                    skipped += 1
            except Exception as e:
                errors += 1
                if verbose:
                    print(f"  ❌ {thread['thread_id']}: {e}")

        self._save_processed()
        return {"processed": processed, "skipped": skipped, "errors": errors, "found_threads": len(threads)}

    def stats(self) -> dict:
        files = sorted(CHAINS_DIR.glob("*.jsonl"))
        total = 0
        by_topic: dict[str, int]  = {}
        by_cmd: dict[str, int]    = {}
        quality_sum = 0.0
        steps_sum   = 0

        for f in files:
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    c = json.loads(line)
                    total += 1
                    mt = c.get("macro_topic", "?")
                    by_topic[mt] = by_topic.get(mt, 0) + 1
                    quality_sum += c.get("quality", 0)
                    steps_sum   += c.get("steps_count", 0)
                    for cmd in c.get("step_cmds", []):
                        by_cmd[cmd] = by_cmd.get(cmd, 0) + 1
                except Exception:
                    pass

        return {
            "total":       total,
            "files":       len(files),
            "processed":   len(self._processed),
            "by_topic":    by_topic,
            "by_cmd":      by_cmd,
            "avg_quality": round(quality_sum / total, 2) if total else 0.0,
            "avg_steps":   round(steps_sum / total, 1)  if total else 0.0,
        }

    def tail(self, n: int = 3) -> list[dict]:
        files = sorted(CHAINS_DIR.glob("*.jsonl"), reverse=True)
        out: list[dict] = []
        for f in files:
            for line in reversed(f.read_text(encoding="utf-8").splitlines()):
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                    if len(out) >= n:
                        return out
                except Exception:
                    pass
        return out


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parent.parent))

    builder = CouncilChainBuilder()
    sub     = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if sub == "last":
        print("Обрабатываю последний broadcast-тред...")
        chain = builder.process_last(verbose=True)
        if not chain:
            print("Нет данных.")

    elif sub == "run":
        print("Ищу необработанные треды...")
        result = builder.run_all(verbose=True)
        print(f"\nГотово: обработано={result['processed']} пропущено={result['skipped']} "
              f"ошибок={result['errors']} тредов={result['found_threads']}")

    elif sub == "stats":
        st = builder.stats()
        print(f"Council chains: {st['total']} цепочек, {st['files']} файлов")
        print(f"  avg_quality={st['avg_quality']}  avg_steps={st['avg_steps']}")
        print(f"  по темам:   {st['by_topic']}")
        print(f"  по командам:{st['by_cmd']}")

    elif sub == "tail":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for c in builder.tail(n):
            print(f"\n── {c['thread_id']} | {c['topic']} [{c['macro_topic']}] q={c['quality']} ──")
            print(f"   модели: {list(c['raw_responses'].keys())}  consensus={c['consensus_score']}")
            try:
                asst = json.loads(c["messages"][2]["content"])
                print(f"   синтез: {asst['synthesis'][:200]}")
                print(f"   цепочка ({len(asst['steps'])} шагов):")
                for s in asst["steps"]:
                    args = {k: v for k, v in s.items() if k not in ("cmd","label","reason")}
                    print(f"     [{s['cmd']}] {s.get('label','')} → {args}")
                    if s.get("reason"):
                        print(f"             причина: {s['reason'][:80]}")
            except Exception as e:
                print(f"   ошибка чтения: {e}")

    else:
        print("Команды: last, run, stats, tail [N]")
