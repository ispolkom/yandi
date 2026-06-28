"""
assistant/orch_synthesizer.py — Двухпроходный синтезатор ответа.

Проход 1 — Экстракция: из сырых данных вытащить только факты, релевантные матрице запроса,
           убрать дубли и мусор.
Проход 2 — Композиция: из чистых фактов составить логичный последовательный ответ.

Если контекста нет — единственный проход из знаний модели.
"""
from __future__ import annotations

import re
import requests as _requests

from agent.orch_schemas import SearchResult, WebScrapeResult, EnrichedQuery, SynthesisResult
from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_ANALYST, TEMP_ANALYST

TIMEOUT       = 180
MAX_CTX_CHARS = 12000

_session = _requests.Session()
_session.trust_env = False


# ── LLM-вызов ─────────────────────────────────────────────────────────────────

def _call(prompt: str, max_tokens: int = MAX_TOKENS_ANALYST, temp: float = TEMP_ANALYST) -> str:
    resp = _session.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": temp, "num_predict": max_tokens}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


# ── Постобработка ──────────────────────────────────────────────────────────────

def _strip_think(text: str) -> str:
    # XML-блоки которые модель добавляет сама
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<verification>.*?</verification>", "", text, flags=re.DOTALL)
    text = re.sub(r"<review>.*?</review>", "", text, flags=re.DOTALL)
    # Китайские символы (Qwen3 иногда вставляет)
    text = re.sub(r"[一-鿿㐀-䶿]+", "", text)

    m = re.search(r"<answer>(.*?)</answer>", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    for marker in (
        "---\n**Verification**", "---\n**Final Answer**", "\n**Verification**",
        "\n---\nКонтекст:", "\n---\nПроверка:", "\n---\nИсточники:",
        "\n**Проверка**", "\n**Верификация**", "\n**Проверка:**",
        "\n**Verification:**", "\nПроверка:", "\nVerification:",
    ):
        if marker in text:
            text = text[:text.index(marker)].strip()
    for marker in ("**Final Answer**:\n", "**Final Answer**:"):
        if marker in text:
            after = text[text.index(marker) + len(marker):].strip()
            if len(after) > 100:
                text = after
    for doc_marker in ("\n\nВопрос:", "\n\nQuestion:", "\nВопрос:", "<|endoftext|>", "<|im_start|>"):
        idx = text.find(doc_marker)
        if idx > 80:
            text = text[:idx].strip()
    # Убрать повторяющиеся абзацы
    paras = text.split("\n\n")
    seen: list[str] = []
    for p in paras:
        p_s = p.strip()
        if p_s in seen and len(p_s) > 40:
            break
        seen.append(p_s)
    return "\n\n".join(seen).strip()


def _strip_self_report(text: str) -> str:
    """Срезает мета-нарративы модели о собственном ответе в конце текста."""
    self_report_triggers = (
        "в данном ответе я",
        "в данном тексте я",
        "таким образом, я описал",
        "я последовательно описал",
        "текст логично структурирован",
        "соответствует всем требованиям",
        "объемом около",
        "в ответе описаны",
        "я постарался",
        "я рассмотрел",
        "итак, я объяснил",
        "подводя итог, я",
    )
    lines = text.split("\n")
    cutoff = len(lines)
    for i, line in enumerate(lines):
        low = line.lower().strip()
        if any(low.startswith(t) for t in self_report_triggers):
            cutoff = i
            break
    return "\n".join(lines[:cutoff]).strip()


# ── Контекст из источников ────────────────────────────────────────────────────

def _build_context(
    search_result: SearchResult | None,
    web_result: WebScrapeResult | None,
) -> str:
    parts = []
    if search_result and search_result.docs:
        parts.append("=== Локальная база знаний ===")
        for i, doc in enumerate(search_result.docs[:3], 1):
            parts.append(f"[{i}] (доверие: {doc.trust_level}, релевантность: {doc.score:.2f})")
            parts.append(doc.text[:800])
    if web_result and web_result.snippets:
        parts.append("=== Интернет ===")
        for i, snip in enumerate(web_result.snippets[:5], 1):
            parts.append(f"[{i}] {snip.title}\n{snip.text[:700]}")
    return "\n\n".join(parts)


def _extract_sources(
    search_result: SearchResult | None,
    web_result: WebScrapeResult | None,
) -> list[str]:
    sources = []
    if search_result and search_result.docs:
        seen: set[str] = set()
        for doc in search_result.docs[:3]:
            if doc.source not in seen:
                sources.append(f"local:{doc.source}")
                seen.add(doc.source)
    if web_result and web_result.snippets:
        for snip in web_result.snippets[:5]:
            sources.append(snip.url)
    return sources


def _compress(text: str) -> str:
    return text[:MAX_CTX_CHARS] + "\n[... обрезано ...]" if len(text) > MAX_CTX_CHARS else text


# ── Промпты ───────────────────────────────────────────────────────────────────

def _frame_hint(query_frame: dict) -> str:
    """Строка с матрицей запроса для вставки в промпты."""
    parts = []
    if query_frame.get("object"):
        parts.append(f"Объект: {query_frame['object']}")
    if query_frame.get("action"):
        parts.append(f"Действие: {query_frame['action']}")
    c = query_frame.get("constraints") or {}
    if c:
        ctx = ", ".join(f"{k}={v}" for k, v in c.items() if v)
        parts.append(f"Контекст запроса: {ctx}")
    if query_frame.get("missing"):
        parts.append(f"Неизвестно: {', '.join(query_frame['missing'])}")
    return "\n".join(parts)


_EXTRACT_PROMPT = """\
Ты анализатор данных. Твоя задача — отфильтровать и извлечь факты из сырых данных.

Вопрос пользователя: "{query}"
{frame_hint}

Сырые данные из источников:
{context}

ЗАДАЧА:
1. Выбери ТОЛЬКО факты, относящиеся к вопросу и матрице выше
2. Убери дубли — если несколько источников говорят одно и то же, оставь один раз
3. Убери нерелевантное — реклама, навигация, другие темы
4. Каждый факт — отдельная строка, коротко и конкретно

Только список фактов, без пояснений и вступлений:"""

_COMPOSE_PROMPT = """\
Ты пишешь ответ пользователю. У тебя есть список проверенных фактов.

Вопрос: "{query}"
{frame_hint}

Факты:
{facts}

ПРАВИЛА:
- Связный текст, логичная последовательность: главное сначала, детали потом
- Без дублей, без воды, без вступлений типа "Отвечая на ваш вопрос..."
- Отвечай на том же языке, на котором задан вопрос
- Без markdown-заголовков и звёздочек
- Максимум 350 слов

Ответ:"""

_SINGLE_PROMPT = """\
Ты экспертный ассистент. Ответь на вопрос пользователя.

Вопрос: "{query}"
{frame_hint}

Правила:
- Связный текст, логичная последовательность
- Конкретно и практично, без воды
- Отвечай на том же языке, на котором задан вопрос
- Без markdown, без заголовков
- Максимум 350 слов

Ответ:"""


# ── Основная функция ──────────────────────────────────────────────────────────

def synthesize(
    enriched: EnrichedQuery,
    search_result: SearchResult | None = None,
    web_result: WebScrapeResult | None = None,
    query_frame: dict | None = None,
) -> SynthesisResult:
    context = _build_context(search_result, web_result)
    context = _compress(context)
    frame   = query_frame or {}
    hint    = _frame_hint(frame)

    try:
        if context:
            # ── Проход 1: извлечение фактов ───────────────────────────────
            extract_prompt = _EXTRACT_PROMPT.format(
                query=enriched.original,
                frame_hint=hint,
                context=context,
            )
            raw_facts = _call(extract_prompt, max_tokens=800, temp=0.1)
            facts     = _strip_think(raw_facts).strip()

            if not facts or len(facts) < 50:
                # Экстракция ничего не дала — откат к единственному проходу
                facts = context[:2000]

            # ── Проход 2: компоновка ответа ───────────────────────────────
            compose_prompt = _COMPOSE_PROMPT.format(
                query=enriched.original,
                frame_hint=hint,
                facts=facts[:3000],
            )
            raw_answer = _call(compose_prompt, max_tokens=600, temp=TEMP_ANALYST)
        else:
            # Нет данных — единственный проход из знаний модели
            raw_answer = _call(
                _SINGLE_PROMPT.format(query=enriched.original, frame_hint=hint),
                max_tokens=MAX_TOKENS_ANALYST,
                temp=TEMP_ANALYST,
            )

        answer = _strip_think(raw_answer)
        answer = _strip_self_report(answer)

    except Exception as e:
        return SynthesisResult(
            answer=f"Не удалось получить ответ: {e}",
            confidence=0.0, sources=[], trust_level="UNVERIFIED",
        )

    # ── Trust level ───────────────────────────────────────────────────────────
    web_count    = sum(1 for s in web_result.snippets if len(s.text) >= 200) if web_result else 0
    local_ok     = bool(search_result and search_result.confidence >= 0.85)

    if local_ok or web_count >= 2:
        trust = "HYPOTHESIS"
    elif web_count == 1:
        trust = "UNVERIFIED"
        answer += "\n\n⚠ Найден только один источник — ответ не подтверждён повторно."
    else:
        trust = "UNVERIFIED"
        answer += "\n\n⚠ Веб-источники недоступны — ответ из знаний модели."

    confidence = search_result.confidence if search_result else (0.6 if web_count >= 2 else 0.3)

    return SynthesisResult(
        answer=answer,
        confidence=confidence,
        sources=_extract_sources(search_result, web_result),
        trust_level=trust,
        raw=raw_answer,
    )


if __name__ == "__main__":
    from agent.orch_schemas import EnrichedQuery
    eq = EnrichedQuery(
        original="Как работает DHT в P2P-сетях?",
        enriched="DHT distributed hash table Kademlia P2P принцип работы",
        params={},
    )
    from agent.orch_registry_search import search_registry
    sr = search_registry(eq.enriched)
    result = synthesize(eq, search_result=sr, query_frame={"object": "DHT", "action": "работает"})
    print(f"Trust: {result.trust_level}  Confidence: {result.confidence:.2f}")
    print(f"\nОтвет:\n{result.answer[:500]}")
