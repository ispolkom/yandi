"""
orch_query_framer.py — Матрица составляющих запроса (QueryFrame).

Двухпроходный анализ в одном LLM-вызове:
  Шаг 1 — извлечь что ЕСТЬ в запросе (object, action, constraints)
  Шаг 2 — найти что ОТСУТСТВУЕТ, опираясь на извлечённое
Это устраняет баг: слот уже в тексте → не попадает в missing.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from llm_gateway import complete as llm_complete

MODEL  = "heretic:q8"


# ── Dataclass ──────────────────────────────────────────────────────────────────

@dataclass
class QueryFrame:
    raw_query:           str
    enriched_query:      str
    intent:              str       = "question"
    domain:              str       = "general"
    obj:                 Optional[str] = None
    action:              Optional[str] = None
    constraints:         dict      = field(default_factory=dict)
    missing:             list[str] = field(default_factory=list)
    search_queries:      list[str] = field(default_factory=list)
    clarifying_question: Optional[str] = None
    answer_policy:       str       = "answer_with_assumptions"
    trust_penalty:       bool      = False

    def to_dict(self) -> dict:
        return asdict(self)


# ── Policy ─────────────────────────────────────────────────────────────────────

_SAFE_DOMAINS = frozenset({
    "медицина", "здоровье", "фармакология", "психология",
    "финансы", "инвестиции", "юриспруденция", "право",
    "налоги", "страхование",
    "политика", "выборы", "голосование", "партии", "кандидаты",
    "религия", "национальность", "раса",
})

_GENERIC_OBJECTS = frozenset({
    "пример", "задача", "текст", "что-то", "это", "вопрос",
    "example", "task", "text", "something",
})

# Шаблоны уточняющих вопросов для случаев когда LLM не сгенерировал cq
_MISSING_TO_QUESTION = {
    "объект":       "Что именно нужно {action}? Уточните предмет запроса.",
    "действие":     "Что нужно сделать? Опишите желаемое действие.",
    "место":        "Где это будет происходить? Укажите место или регион.",
    "бюджет":       "Какой у вас бюджет?",
    "уровень":      "Какой у вас уровень опыта: начинающий, средний или продвинутый?",
    "цель":         "Какова ваша цель? Для чего это нужно?",
    "для кого":     "Для кого это: для себя, ребёнка, другого человека?",
    "сезон":        "В какое время года / сезон?",
    "инструменты":  "Какие инструменты или технологии доступны?",
}


# Rust-перенос (2026-09-24): rustlib/yandi_rs/src/orch_query_framer.rs — `decide_policy`, `_auto_cq`, `_is_safe_domain`. Доказан тестом
# agent/orch_query_framer_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается YANDI_ORCH_QUERY_FRAMER_ENGINE=rust ПОСЛЕ сборки
# rustlib/yandi_rs. Делегирует, только пока таблицы (`_SAFE_DOMAINS`, `_GENERIC_OBJECTS`, `_MISSING_TO_QUESTION`) не изменены и поля кадра —
# «родные» типы; иначе — исходный код ниже.
_log = logging.getLogger("yandi.orch_query_framer")
_rust_qf = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_qf():
    global _rust_qf
    if _rust_qf is None:
        if os.environ.get("YANDI_ORCH_QUERY_FRAMER_ENGINE") == "rust":
            try:
                import yandi_rs.orch_query_framer as _rs
                _rust_qf = _rs
                _log.warning("YANDI_ORCH_QUERY_FRAMER_ENGINE=rust: используется Rust-реализация orch_query_framer (rustlib/yandi_rs)")
            except ImportError as e:
                _log.warning("YANDI_ORCH_QUERY_FRAMER_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_qf = False
        else:
            _rust_qf = False
    return _rust_qf or None


_TABLES_AT_IMPORT = (frozenset(_SAFE_DOMAINS), frozenset(_GENERIC_OBJECTS), tuple(_MISSING_TO_QUESTION.items()))


def _rust_ok():
    """Rust-ядро, если запрошено и таблицы не менялись с момента импорта."""
    rs = _get_rust_qf()
    if rs is None:
        return None
    snap = (frozenset(_SAFE_DOMAINS), frozenset(_GENERIC_OBJECTS), tuple(_MISSING_TO_QUESTION.items()))
    return rs if snap == _TABLES_AT_IMPORT else None


def _auto_cq(frame: QueryFrame) -> Optional[str]:
    """Генерирует уточняющий вопрос из missing-списка если LLM не дал своего."""
    rs = _rust_ok()
    if rs is not None and (frame.action is None or type(frame.action) is str):
        miss = frame.missing
        if not miss:
            try:
                return rs.auto_cq(None, frame.action)
            except UnicodeEncodeError:
                pass
        elif type(miss) is list and type(miss[0]) is str:
            try:
                return rs.auto_cq(miss[0], frame.action)
            except UnicodeEncodeError:
                pass
    if not frame.missing:
        # Нет объекта и нет контекста — очень пустой запрос
        action_str = f"«{frame.action}»" if frame.action else "сделать"
        return f"Что именно нужно {action_str}? Уточните предмет запроса."
    first = frame.missing[0].lower()
    for key, tmpl in _MISSING_TO_QUESTION.items():
        if key in first:
            return tmpl.format(action=frame.action or "сделать")
    return f"Уточните: {frame.missing[0]}?"


def _is_safe_domain(domain: str) -> bool:
    rs = _rust_ok()
    if rs is not None and type(domain) is str:
        try:
            return rs.is_safe_domain(domain)
        except UnicodeEncodeError:
            pass
    d = domain.lower()
    return any(s in d for s in _SAFE_DOMAINS)


def decide_policy(frame: QueryFrame) -> str:
    rs = _rust_ok()
    if rs is not None and type(frame.domain) is str and (frame.obj is None or type(frame.obj) is str):
        try:
            return rs.decide_policy(frame.domain, frame.obj, bool(frame.action), bool(frame.constraints))
        except UnicodeEncodeError:
            pass
    real_obj  = bool(frame.obj) and frame.obj.lower() not in _GENERIC_OBJECTS
    has_ctx   = bool(frame.constraints)

    # Чувствительные домены имеют приоритет — даже без объекта
    if _is_safe_domain(frame.domain):
        return "safe_general"
    if not real_obj and not has_ctx:
        return "ask_first"
    if real_obj and frame.action and has_ctx:
        return "answer_direct"
    return "answer_with_assumptions"


def _fallback(query: str) -> QueryFrame:
    return QueryFrame(
        raw_query=query, enriched_query=query,
        search_queries=[query], answer_policy="answer_with_assumptions",
    )


# ── JSON extraction ────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Brace-walking: находит первый валидный JSON с ключом 'extracted'."""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text, flags=re.IGNORECASE).strip()
    pos = 0
    while True:
        start = text.find("{", pos)
        if start == -1:
            break
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start: i + 1])
                        if isinstance(obj, dict) and "extracted" in obj:
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        pos = start + 1
    return {}


# ── Prompt ────────────────────────────────────────────────────────────────────

def _gather_context(query: str) -> str:
    """Быстрый контекст перед классификацией: DDG сниппеты + знание из registry."""
    parts: list[str] = []

    # DDG: 2 сниппета, без скрапинга — только title+body из поиска
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=2))
        for h in hits[:2]:
            snippet = (h.get("body") or "").strip()[:250]
            if snippet:
                parts.append(f"[Web] {snippet}")
    except Exception:
        pass

    # Registry: топ-2 документа по сходству
    try:
        from agent.orch_registry_search import search_registry
        res = search_registry(query, top_k=2)
        for doc in res.docs[:2]:
            if doc.score > 0.25:
                parts.append(f"[Знание] {doc.text[:250].strip()}")
    except Exception:
        pass

    return "\n".join(parts) if parts else ""


_PROMPT = """\
Ты анализатор запросов. Работай в ДВА ШАГА и верни ТОЛЬКО JSON без markdown. Все текстовые значения — на русском языке.

{context_block}История диалога (от старых к новым):
{history}

Запрос пользователя: "{query}"

ШАГ 1 — ИЗВЛЕЧЕНИЕ (только то, что явно написано в запросе или истории):
Найди объект, действие и ограничения прямо из текста выше.
ВАЖНО: объект — это главная тема запроса (плов, ноутбук, резюме, квантовая запутанность, порода собаки).
Если слот не упомянут — ставь null. Не додумывай.

ШАГ 2 — АНАЛИЗ ПРОБЕЛОВ (на основе ШАГ 1):
Посмотри что извлечено. Чего важного не хватает для точного ответа?
СТРОГОЕ ПРАВИЛО: если слот уже есть в extracted — он НЕ попадает в missing.
СТРОГОЕ ПРАВИЛО: clarifying_question — всегда на русском языке.

Верни JSON:
{{
  "extracted": {{
    "object": "главный объект/тема из текста или null",
    "action": "главное действие из текста или null",
    "constraints": {{
      "season": "сезон если упомянут или null",
      "place":  "место если упомянуто или null",
      "tools":  "инструменты если упомянуты или null",
      "goal":   "цель если упомянута или null",
      "level":  "уровень опыта если упомянут или null",
      "budget": "бюджет если упомянут или null",
      "who":    "для кого если упомянуто или null"
    }}
  }},
  "gap_analysis": {{
    "domain": "широкая тема на русском",
    "intent": "question|instruction|comparison|definition",
    "missing": ["только то чего нет в тексте запроса"],
    "clarifying_question": "один вопрос о самом важном пробеле на русском или null",
    "search_queries": ["конкретный запрос с деталями", "широкий запрос"],
    "trust_penalty": true
  }},
  "enriched_query": "полный поисковый запрос = запрос + контекст из истории"
}}
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def build_query_frame(query: str, history: list[dict] | None = None) -> QueryFrame:
    lines = []
    for m in (history or [])[-6:]:
        role = "Пользователь" if m.get("from") == "human" else "Ассистент"
        text = re.sub(r"^\[ПРЕДВАРИТЕЛЬНЫЙ.*?\]\n+", "", str(m.get("text", "")), flags=re.DOTALL)
        lines.append(f"{role}: {text[:300].strip()}")
    history_str = "\n".join(lines) if lines else "(нет истории)"

    ctx = _gather_context(query)
    context_block = (
        f"Контекст из интернета и базы знаний:\n{ctx}\n\n"
        if ctx else ""
    )

    try:
        raw = llm_complete(
            _PROMPT.format(history=history_str, query=query, context_block=context_block),
            model=MODEL, temperature=0.1, max_tokens=800, timeout=90,
        )
        data = _extract_json(raw)
    except Exception:
        return _fallback(query)

    if not data:
        return _fallback(query)

    # ── Шаг 1: извлечённые слоты ──────────────────────────────────────────────
    ext = data.get("extracted") or {}
    raw_c = ext.get("constraints") or {}
    constraints = {k: v for k, v in raw_c.items() if v and str(v).lower() not in ("null", "none", "")}

    # ── Шаг 2: анализ пробелов ────────────────────────────────────────────────
    gap = data.get("gap_analysis") or {}

    missing = gap.get("missing") or []
    missing = [str(m).strip() for m in missing if m][:6] if isinstance(missing, list) else []

    sq = gap.get("search_queries") or []
    sq = [str(s).strip() for s in sq if s][:3] if isinstance(sq, list) else []

    enriched = (data.get("enriched_query") or query).strip() or query
    if not sq:
        sq = [enriched]

    cq = gap.get("clarifying_question")
    if not cq or str(cq).lower() in ("null", "none", ""):
        cq = None

    obj_raw = ext.get("object")
    if obj_raw and str(obj_raw).lower() in ("null", "none", ""):
        obj_raw = None

    action_raw = ext.get("action")
    if action_raw and str(action_raw).lower() in ("null", "none", ""):
        action_raw = None

    frame = QueryFrame(
        raw_query           = query,
        enriched_query      = enriched,
        intent              = str(gap.get("intent") or "question"),
        domain              = str(gap.get("domain") or "general"),
        obj                 = obj_raw or None,
        action              = action_raw or None,
        constraints         = constraints,
        missing             = missing,
        search_queries      = sq,
        clarifying_question = cq,
        answer_policy       = "answer_with_assumptions",
        trust_penalty       = bool(gap.get("trust_penalty", bool(missing))),
    )
    frame.answer_policy = decide_policy(frame)

    # Генерируем cq из шаблона если политика требует уточнения, а LLM не дал вопроса
    if frame.answer_policy in ("ask_first", "safe_general") and not frame.clarifying_question:
        frame.clarifying_question = _auto_cq(frame)

    return frame
