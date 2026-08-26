"""
agent/orch_synthesizer.py — Двухпроходный синтезатор ответа.

ЧЕСТНАЯ ВЕРСИЯ v4.0:
- Всё — гипотеза. Нет "фактов".
import sys
- Наука — это модель, а не истина.
- Система не знает, она только передаёт наблюдения.
- Рекомендация: проверять на личном опыте.
"""
from __future__ import annotations

import re
import time
import uuid
import sys
import threading
from pathlib import Path
from agent.source_quality import evaluate_source_quality
from typing import List, Dict, Any, Optional

import requests as _requests

from agent.orch_schemas import (
    SearchResult,
    WebScrapeResult,
    EnrichedQuery,
    SynthesisResult,
    EvidenceRecord,
    ClaimRecord,
    TrustReport,
    CoverageReport,
    OutcomeRecord,
)
from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_ANALYST, TEMP_ANALYST

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

from agent.claim_types import ClaimType, ResponseMode, get_response_mode, CLAIM_TO_RESPONSE_MODE
from agent.epistemic_router import EPISTEMIC_WARNING

# D (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §D): supports_query_aspect
# раньше был dead stub (всегда ["general"]). Оживляем его тем самым
# claim role классификатором, что уже используется в retrieval priority
# (claim_evidence_retriever.py) — чтобы роль claim вычислялась ОДИН раз,
# а не дублировалась в двух местах с риском разойтись.
from agent.claim_evidence_retriever import _classify_claim_role


def _clean_refutation_text(text: str) -> str:
    """Очищает текст опровержения от технического мусора."""
    if not text:
        return ""
    
    # Список стоп-фраз (технический мусор)
    stop_phrases = [
        "Добавил:", "Upload", "Скачать", "Страница", "Вуз:", "Предмет:",
        "Авторские права", "Сообщите нам", "Нарушает ваши авторские права",
        "Опубликованный материал", "НЕСОРТИРОВАННОЕ", "Лекции", "Скачиваний",
        "Размер:", "Содержание", "Предисловие", "Введение", "Заключение",
        "Материал из", "Википедии", "Редакция", "Портал", "Навигация",
        "Меню", "Реклама", "Копирайт", "Copyright", "All rights reserved",
        "Если вы считаете", "Пожалуйста, сообщите", "Обратная связь",
    ]
    
    # Удаляем строки, содержащие стоп-фразы
    lines = text.split("\n")
    cleaned_lines = []
    for line in lines:
        if not any(phrase.lower() in line.lower() for phrase in stop_phrases):
            cleaned_lines.append(line)
    
    # Объединяем обратно
    cleaned = "\n".join(cleaned_lines).strip()
    
    # Удаляем слишком короткие фрагменты
    if len(cleaned) < 20:
        return ""
    
    return cleaned[:500]  # Ограничиваем длину

def _extract_refutation_argument(text: str) -> str:
    """Извлекает осмысленный аргумент из текста опровержения."""
    if not text:
        return ""
    
    # Очищаем текст
    cleaned = _clean_refutation_text(text)
    if not cleaned:
        return ""
    
    # Разбиваем на предложения
    sentences = cleaned.replace("! ", ". ").replace("? ", ". ").split(". ")
    
    # Ищем первое осмысленное предложение (не вопрос, не слишком короткое)
    for sent in sentences[:5]:
        sent = sent.strip()
        if len(sent) > 30 and not sent.endswith("?"):
            # Проверяем, что это не технический мусор
            if not any(phrase in sent.lower() for phrase in ["http", "www", "pdf", "doc", "скачать"]):
                return sent[:200] + "..." if len(sent) > 200 else sent
    
    # Если не нашли — берём первое длинное предложение
    for sent in sentences[:3]:
        sent = sent.strip()
        if len(sent) > 20:
            return sent[:200] + "..." if len(sent) > 200 else sent
    
    return cleaned[:150] + "..."
TIMEOUT       = 180
MAX_CTX_CHARS = 12000

_session = _requests.Session()
_session.trust_env = False

# ============================================================
# LOCAL GENERATION CONCURRENCY GATE
# ============================================================
#
# Один локальный Ollama обслуживает несколько компонентов YANDI.
# Параллельные тяжёлые /api/generate запросы могут конкурировать
# за RAM/VRAM и приводить к каскадным timeout.
#
# Embedding pipeline использует /api/embed и этим semaphore
# намеренно не блокируется.
_GENERATION_SEMAPHORE = threading.Semaphore(2)


# ── LLM-вызов ─────────────────────────────────────────────────────────────────

def _call(
    prompt: str,
    max_tokens: int = MAX_TOKENS_ANALYST,
    temp: float = TEMP_ANALYST,
) -> str:
    # Все тяжёлые локальные generation calls проходят по одному.
    #
    # Это не epistemic gate и не retry policy.
    # Его задача только не позволять компонентам YANDI одновременно
    # забивать один Ollama несколькими генерациями.
    wait_started = time.time()

    with _GENERATION_SEMAPHORE:
        waited = time.time() - wait_started

        if waited > 0.05:
            print(
                f"[Local LLM] generation queue wait="
                f"{waited:.2f}s"
            )

        call_started = time.time()

        resp = _session.post(
            f"{OLLAMA}/api/generate",
            json={
                "model": MODEL,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": temp,
                    "num_predict": max_tokens,
                },
            },
            timeout=TIMEOUT,
        )

        resp.raise_for_status()

        elapsed = time.time() - call_started

        print(
            f"[Local LLM] generation done "
            f"in {elapsed:.2f}s "
            f"tokens<={max_tokens}"
        )

        return resp.json().get(
            "response",
            "",
        ).strip()


# ── Постобработка ──────────────────────────────────────────────────────────────

def _is_technical_failure_text(text: str) -> bool:
    """
    Определяет инфраструктурную/техническую ошибку.

    Technical failure НЕ является:
      - ответом о мире;
      - claim;
      - evidence;
      - epistemic observation.

    Эта функция намеренно консервативна и ловит только явные
    служебные сигнатуры.
    """
    if not text:
        return False

    low = str(text).lower()

    markers = (
        "не удалось сгенерировать локальный ответ",
        "не удалось получить ответ:",
        "httpconnectionpool",
        "read timed out",
        "connection refused",
        "max retries exceeded",
        "requests.exceptions",
        "urllib3.exceptions",
        "traceback (most recent call last)",
        "127.0.0.1:11434",
    )

    return any(marker in low for marker in markers)


def _strip_think(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<verification>.*?</verification>", "", text, flags=re.DOTALL)
    text = re.sub(r"<review>.*?</review>", "", text, flags=re.DOTALL)
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
    paras = text.split("\n\n")
    seen: list[str] = []
    for p in paras:
        p_s = p.strip()
        if p_s in seen and len(p_s) > 40:
            break
        seen.append(p_s)
    return "\n\n".join(seen).strip()


def _strip_self_report(text: str) -> str:
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


def _remove_meta_facts(text: str) -> str:
    """Удалить мета-факты типа 'в предоставленных данных нет информации'."""
    lines = text.split("\n")
    filtered = []
    for line in lines:
        low = line.lower().strip()
        if any(phrase in low for phrase in [
            "в предоставленных данных нет",
            "в предоставленных фактах нет",
            "в найденных источниках нет",
            "в доступных источниках не",
            "в имеющихся данных не",
            "информация отсутствует",
            "нет информации",
            "не хватает данных",
            "недостаточно данных",
            "не удалось найти",
            "не найдено",
            "отсутствует информация",
        ]):
            continue
        filtered.append(line)
    return "\n".join(filtered)


# ── Генерация честного эпистемического ответа ──────────────────────────────

def _format_honest_answer(
    answer: str,
    domain: str,
    testability: str,
    knowledge_stability: str,
    sources_count: int,
    is_hypothetical: bool = False,
    is_science_as_model: bool = True,
) -> str:
    """
    Форматирует ответ в честном эпистемическом стиле.
    """
    # 1. Добавляем глобальное предупреждение в начало
    result = EPISTEMIC_WARNING + "\n\n---\n\n"

    # 2. Основной ответ
    result += answer

    # 3. Добавляем информацию о согласии источников (не "подтверждено")
    if sources_count >= 3:
        result += "\n\n📚 **Согласованное мнение:** Три источника сходятся на этом утверждении. Это не делает его истиной. Это просто означает, что несколько людей независимо пришли к похожим выводам."
    elif sources_count >= 2:
        result += "\n\n📚 **Частичное согласие:** Два источника сходятся на этом утверждении. Рекомендуется проверка через дополнительные источники."
    elif sources_count >= 1:
        result += "\n\n📚 **Единичное наблюдение:** Один источник утверждает это. Настоятельно рекомендуется проверка через независимые источники."
    else:
        result += "\n\n❓ **Нет согласованных данных.** Это утверждение не имеет подтверждения в доступных источниках."

    # 4. Для научных утверждений — добавляем оговорку
    if is_science_as_model and domain in ["scientific", "biological", "factual", "physical", "chemical", "astronomical", "medical", "technological"]:
        result += "\n\n🧪 **Научный статус:** Это научная модель, а не установленная истина. Она основана на наблюдениях и может быть пересмотрена."
    elif domain == "philosophical":
        result += "\n\n📜 **Философская интерпретация:** Это одна из возможных точек зрения, не единственная."
    elif domain == "religious":
        result += "\n\n🕊️ **Религиозный взгляд:** Это утверждение основано на вере, а не на эмпирических данных."
    elif domain == "historical":
        result += "\n\n📜 **Историческая интерпретация:** История пишется на основе источников, которые могут быть неполными или предвзятыми."

    # 5. Для гипотетических утверждений
    if is_hypothetical:
        result += "\n\n⚠️ **Это гипотетическое утверждение.** Нет прямых доказательств. Относитесь критически."

    # 6. Для спорных утверждений
    if knowledge_stability == "controversial":
        result += "\n\n⚡ **Это спорное утверждение.** Существуют разные, иногда противоположные точки зрения."


    # 8. Рекомендация
    result += "\n\n💡 **Рекомендация:** Проверьте эту информацию на своём опыте. Наблюдайте, экспериментируйте, делайте свои выводы. Никто не знает истину за вас."

    return result


# ── Контекст из источников ────────────────────────────────────────────────────

def _get_snippet_text(snip) -> str:
    if hasattr(snip, "content") and snip.content:
        return snip.content
    if hasattr(snip, "text") and snip.text:
        return snip.text
    if isinstance(snip, dict):
        return snip.get("content", snip.get("text", ""))
    return str(snip) if snip else ""


def _get_snippet_url(snip) -> str:
    if hasattr(snip, "url") and snip.url:
        return snip.url
    if isinstance(snip, dict):
        return snip.get("url", "")
    return ""


def _get_snippet_title(snip) -> str:
    if hasattr(snip, "title") and snip.title:
        return snip.title
    if isinstance(snip, dict):
        return snip.get("title", "")
    return ""


def _build_context(
    search_result: SearchResult | None,
    web_result: WebScrapeResult | None,
) -> tuple[str, List[str]]:
    parts = []
    used_sources = []

    if search_result and search_result.docs:
        parts.append("=== Наблюдения из локальной базы ===")
        for i, doc in enumerate(search_result.docs[:3], 1):
            trust = getattr(doc, "trust_level", "UNKNOWN")
            score = getattr(doc, "score", 0.0)
            text = getattr(doc, "text", "")
            if not text:
                text = getattr(doc, "answer", "")
            parts.append(f"[{i}] (уровень согласия: {trust}, релевантность: {score:.2f})")
            parts.append(text[:800])
            source = getattr(doc, "source", "local")
            if source:
                used_sources.append(f"local:{source}")

    if web_result and web_result.snippets:
        parts.append("=== Наблюдения из интернета ===")
        for i, snip in enumerate(web_result.snippets[:5], 1):
            title = _get_snippet_title(snip)
            text = _get_snippet_text(snip)
            url = _get_snippet_url(snip)
            parts.append(f"[{i}] {title}")
            parts.append(text[:700])
            if url:
                used_sources.append(url)

    return "\n\n".join(parts), list(dict.fromkeys(used_sources))


def _extract_sources(
    search_result: SearchResult | None,
    web_result: WebScrapeResult | None,
) -> list[str]:
    sources = []
    seen: set[str] = set()

    if web_result and web_result.snippets:
        for snip in web_result.snippets[:5]:
            url = _get_snippet_url(snip)
            if url and url not in seen and not url.startswith("local:"):
                sources.append(url)
                seen.add(url)

    if not sources and search_result and search_result.docs:
        for doc in search_result.docs[:3]:
            source = getattr(doc, "source", "local")
            if source and source not in seen and not source.startswith("council"):
                sources.append(f"local:{source}")
                seen.add(source)

    return sources[:5]


def _compress(text: str) -> str:
    return text[:MAX_CTX_CHARS] + "\n[... обрезано ...]" if len(text) > MAX_CTX_CHARS else text


# ── Промпты ───────────────────────────────────────────────────────────────────

def _frame_hint(query_frame: dict) -> str:
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

    epistemic = query_frame.get("epistemic", {})
    if epistemic:
        parts.append(f"Эпистемический режим: {epistemic.get('answer_mode', 'unknown')}")
        parts.append(f"Проверяемость: {epistemic.get('testability', 'unknown')}")
        if epistemic.get("should_avoid_single_truth_claim"):
            parts.append("НЕ давать утверждение как единственную истину (всё — гипотеза)")

    return "\n".join(parts)


# ── Промпты для разных режимов ──────────────────────────────────────────────

_EXTRACT_PROMPT = """\
Ты анализатор проверяемых утверждений.

Твоя задача — извлечь из ответа модели АТОМАРНЫЕ claims,
которые затем будут независимо проверяться по внешним источникам.

Вопрос пользователя:
"{query}"

{frame_hint}

Ответ модели:
{context}

ЗАДАЧА:

1. Извлеки ТОЛЬКО утверждения, реально содержащиеся в ответе модели.

2. Каждый claim должен содержать РОВНО ОДНО независимо
   проверяемое утверждение.

3. Если предложение содержит несколько независимо проверяемых
   утверждений — раздели его на несколько claims.

4. Каждый claim должен быть самодостаточным:
   субъект утверждения должен быть указан ЯВНО.

   ПЛОХО:
   "Температура достигает -145°C."

   ХОРОШО:
   "Температура верхних слоёв атмосферы Юпитера составляет
   около -145°C."

5. Не используй местоимения или потерянные ссылки вроде:
   "он", "она", "они", "его", "её", "их", "там",
   если из вопроса или ответа можно восстановить конкретный субъект.

6. Не заменяй конкретный субъект слишком общим словом.

   ПЛОХО:
   "Планета не имеет твёрдой поверхности."

   ХОРОШО:
   "Юпитер не имеет твёрдой поверхности."

7. Не объединяй независимые утверждения через:
   "и", "а", "но", "при этом", "также",
   если каждую часть можно проверить отдельно.

8. Числовые характеристики выделяй отдельно,
   если они могут проверяться независимо.

9. Причину и следствие разделяй на отдельные claims,
   если каждое из них является самостоятельным
   проверяемым утверждением.

10. Не добавляй сведения, которых нет в исходном ответе.

11. Не исправляй исходный ответ своими знаниями.
    Даже если утверждение кажется ошибочным — извлеки его как claim.
    Истинность будет проверяться downstream.

12. Убери дубли и нерелевантные утверждения.

13. Не превращай предположение в установленный факт.
    Сохраняй модальность исходного текста:
    "может", "предположительно", "теоретически",
    "возможно" и т.д.

ПРИМЕР:

Исходное предложение:

"Юпитер состоит преимущественно из водорода и гелия,
не имеет твёрдой поверхности и обладает мощным магнитным полем."

ПЛОХО — один claim:

"Юпитер состоит из водорода и гелия, не имеет твёрдой
поверхности и обладает мощным магнитным полем."

ХОРОШО — три claims:

Юпитер состоит преимущественно из водорода и гелия.
Юпитер не имеет твёрдой поверхности.
Юпитер обладает мощным магнитным полем.

ВАЖНО:
Это extraction, а не проверка истинности.
Не оценивай claims.
Не объясняй их.
Не добавляй комментариев.
Не нумеруй строки.
Не используй markdown.

ФОРМАТ:
Одна строка = один атомарный claim.

Только список claims:
"""

_COMPOSE_PROMPT_HONEST = """\
Ты пишешь ответ пользователю, используя наблюдения из источников.

Вопрос: "{query}"
{frame_hint}

Доступные наблюдения:
{context}

ПРАВИЛА:
1. Все утверждения — это гипотезы и наблюдения, а не истина
2. Используй выражения: "согласно наблюдениям", "по имеющейся информации", "некоторые исследователи считают"
3. Не выдавай ничего как "факт" или "доказано"
4. Чётко отделяй наблюдения от интерпретаций
5. Указывай, если есть разные точки зрения
6. Отвечай на том же языке, на котором задан вопрос
7. Без markdown-заголовков и звёздочек
8. Максимум 350 слов

Ответ:"""

_HYPOTHESIS_FIRST_PROMPT = """\
Ты пишешь ответ пользователю в РЕЖИМЕ ГИПОТЕЗЫ.

Вопрос: "{query}"
{frame_hint}

Доступные наблюдения:
{context}

СТРУКТУРА ОТВЕТА:
1. НАБЛЮДЕНИЕ: Что именно наблюдается (факты, данные, измерения)
2. ГИПОТЕЗА: Какое объяснение предлагается для этих наблюдений
3. ПОДДЕРЖКА: Какие данные поддерживают эту гипотезу
4. ОГРАНИЧЕНИЯ: Что гипотеза не объясняет
5. АЛЬТЕРНАТИВЫ: Другие возможные объяснения
6. СТАТУС: Степень поддержки гипотезы (WEAKLY/PARTIALLY/STRONGLY_SUPPORTED)

ПРАВИЛА:
- ВСЁ — ГИПОТЕЗА. Нет "фактов".
- Наука — это модель, а не истина.
- Чётко разделяй НАБЛЮДЕНИЕ и ИНТЕРПРЕТАЦИЮ.
- Не выдавай ничего как "доказано".
- Отвечай на том же языке.

Ответ:"""

_SINGLE_PROMPT = """\
Ты экспертный ассистент. Ответь на вопрос пользователя.

Вопрос: "{query}"
{frame_hint}

Правила:
- Все утверждения — гипотезы
- Не выдавай ничего как "факт"
- Используй выражения: "согласно", "по некоторым данным", "предположительно"
- Отвечай на том же языке, на котором задан вопрос
- Без markdown, без заголовков
- Максимум 350 слов

Ответ:"""


def _get_compose_prompt(answer_mode: str) -> str:
    """Выбрать промпт в зависимости от режима ответа."""
    if answer_mode == "hypothesis_first":
        return _HYPOTHESIS_FIRST_PROMPT
    # Всегда используем честный промпт
    return _COMPOSE_PROMPT_HONEST


# ===== ОСНОВНАЯ ФУНКЦИЯ =====

def synthesize(
    enriched: EnrichedQuery,
    search_result: SearchResult | None = None,
    web_result: WebScrapeResult | None = None,
    query_frame: dict | None = None,
    response_mode: str = "qualified_factual",
) -> tuple[SynthesisResult, Dict[str, Any]]:
    """
    Возвращает (SynthesisResult, reasoning_info).
    Все ответы — с честным эпистемическим предупреждением.
    """
    context, used_sources = _build_context(search_result, web_result)
    context = _compress(context)
    frame = query_frame or {}
    local_answer = frame.get("local_answer", "")

    # ---- ИЗВЛЕЧЕНИЕ ГИПОТЕЗ ИЗ ГРАФА ----
    hypothesis_graph = frame.get("hypothesis_graph")
    hypothesis_text = ""
    if hypothesis_graph:
        if isinstance(hypothesis_graph, dict):
            nodes = hypothesis_graph.get("nodes", [])
            if nodes:
                hypothesis_text = "\n=== ГИПОТЕЗЫ ИЗ ГРАФА ===\n"
                for node in nodes[:5]:
                    if isinstance(node, dict):
                        label = node.get("label", node.get("text", str(node)))
                        hypothesis_text += f"- {label}\n"
                    else:
                        hypothesis_text += f"- {str(node)}\n"
        elif hasattr(hypothesis_graph, "nodes"):
            nodes = hypothesis_graph.nodes
            if nodes:
                hypothesis_text = "\n=== ГИПОТЕЗЫ ИЗ ГРАФА ===\n"
                for node in nodes[:5]:
                    if hasattr(node, "label"):
                        hypothesis_text += f"- {node.label}\n"
                    elif hasattr(node, "text"):
                        hypothesis_text += f"- {node.text}\n"
                    else:
                        hypothesis_text += f"- {str(node)}\n"
        if hypothesis_text:
            context = hypothesis_text + "\n\n" + context

    # ---- ДОБАВЛЕНИЕ ЛОКАЛЬНОГО ОТВЕТА В КОНТЕКСТ ----
    # ---- ДОБАВЛЕНИЕ ВЫБРАННОГО ИСТОЧНИКА В КОНТЕКСТ ----
    blind_status = frame.get("blind_status", "undecided")
    blind_selected = frame.get("blind_selected_source")
    
    if blind_status == "selected" and blind_selected and blind_selected != "local_model":
        selected_answer = frame.get("best_answer")
        if selected_answer:
            context = context + "\n\n=== ВЫБРАННЫЙ ИСТОЧНИК (Blind) ===\n" + selected_answer[:2000]
            print(f"[Synthesizer] Использован источник: {blind_selected}")
    else:
        local_answer = frame.get("local_answer")
        if local_answer:
            context = context + "\n\n=== ЛОКАЛЬНЫЙ ОТВЕТ МОДЕЛИ ===\n" + local_answer[:2000]
            print(f"[Synthesizer] Добавлен локальный ответ (длина: {len(local_answer)})")

    hint = _frame_hint(frame)

    epistemic = frame.get("epistemic", {})
    domain = epistemic.get("domain", "factual")
    testability = epistemic.get("testability", "fully_testable")
    knowledge_stability = epistemic.get("knowledge_stability", "unknown")
    is_hypothetical = epistemic.get("is_hypothesis", True)
    is_science_as_model = epistemic.get("is_science_as_model", True)
    answer_mode = response_mode

    claims = []
    evidence_records = []
    meta_comments = []
    why_trust = []
    used_count = 0
    claim_evidence_map = {}

    try:
        if context:
            # ============================================================
            # CLAIM EXTRACTION SOURCE
            # ============================================================
            #
            # Claims должны описывать утверждения ОТВЕТА, а не корпуса
            # источников. Раньше _EXTRACT_PROMPT получал весь context:
            #
            #   web sources + local registry + local_answer
            #
            # Из-за этого модель могла извлекать заголовки источников
            # ("NASA Life Detection...", "Juno spacecraft...") вместо
            # фактических утверждений ответа.
            #
            # Фактический SynthesisResult ниже возвращает local_answer,
            # поэтому именно local_answer является canonical claim source.
            #
            # Evidence остаётся независимым и связывается с claims только
            # downstream через claim_evidence_mapper.
            claim_source = (local_answer or "").strip()

            # Инфраструктурный сбой не имеет права попадать
            # в semantic / epistemic lifecycle.
            if _is_technical_failure_text(claim_source):
                print(
                    "[Synthesizer Claims] "
                    "technical local_answer rejected"
                )
                why_trust.append(
                    "local model unavailable: technical failure"
                )
                claim_source = ""

            if claim_source:
                print(
                    f"[Synthesizer Claims] source=local_answer "
                    f"chars={len(claim_source)}"
                )

                # P0-A (autonomous fix pass): bounded, non-secret diagnostic
                # to determine — for any future live run — whether a direct
                # existence verdict was present in local_answer BEFORE
                # extraction runs, without dumping the full payload. Reuses
                # the single existing classifier (_classify_claim_role) on
                # the whole local_answer text instead of building a second,
                # independent detector. role=CORE here means the full text
                # somewhere asserts/denies the existence target directly —
                # if extraction later still yields core_claims=0, the loss
                # happened at extraction, not generation. Any other role
                # means the verdict most likely was never generated in the
                # first place.
                try:
                    _local_answer_role_info = _classify_claim_role(
                        claim_source,
                        enriched.original,
                    )
                    print(
                        "[Local Answer Existence Check] "
                        f"existence_question={_local_answer_role_info['role'] is not None} "
                        f"role_on_full_answer={_local_answer_role_info['role'] or '-'} "
                        f"target_match={_local_answer_role_info['target_match']} "
                        f"has_assertion={_local_answer_role_info['has_assertion']} "
                        f"preview={claim_source[:220]!r}"
                    )
                except Exception as e:
                    print(f"[Local Answer Existence Check] error={e}")

                extract_prompt = _EXTRACT_PROMPT.format(
                    query=enriched.original,
                    frame_hint=hint,
                    context=claim_source,
                )

                raw_facts = _call(
                    extract_prompt,
                    max_tokens=800,
                    temp=0.1,
                )

                facts_text = _strip_think(raw_facts).strip()
                facts_text = _remove_meta_facts(facts_text)

                # Fallback тоже обязан оставаться answer-grounded.
                # Никогда не подменяем answer source общим evidence context.
                if not facts_text or len(facts_text) < 50:
                    facts_text = claim_source[:4000]
                    why_trust.append(
                        "claim extraction fallback: использован local_answer напрямую"
                    )
            else:
                # Если local_answer отсутствует, не создаём фиктивные claims
                # из названий/текста источников.
                facts_text = ""
                why_trust.append(
                    "claim extraction skipped: local_answer отсутствует"
                )
                print(
                    "[Synthesizer Claims] source=NONE "
                    "(local_answer отсутствует)"
                )

            claim_lines = [
                line.strip()
                for line in facts_text.split("\n")
                if line.strip() and len(line.strip()) > 20
            ]

            print(
                f"[Synthesizer] Извлечено claims: {len(claim_lines)}"
            )

            # ========================================================
            # CLAIM ATOMICITY DIAGNOSTIC
            # ========================================================
            #
            # Только диагностика.
            #
            # НИЧЕГО:
            #   - не удаляет,
            #   - не переписывает,
            #   - не меняет claim status,
            #   - не вызывает LLM.
            #
            # Цель — увидеть, насколько хорошо extractor разделяет
            # составные утверждения на атомарные claims.
            import re

            _atomicity_connectors = (
                r"\s+и\s+",
                r"\s+а\s+",
                r"\s+но\s+",
                r"\s+при этом\s+",
                r"\s+также\s+",
                r"\s+однако\s+",
            )

            suspected_compound_claims = []

            for _claim_line in claim_lines:
                _normalized = " " + _claim_line.lower().strip() + " "

                _connector_hits = sum(
                    1
                    for _pattern in _atomicity_connectors
                    if re.search(_pattern, _normalized)
                )

                # Несколько глагольных/предикативных частей часто
                # проявляются через союзы. Это только heuristic:
                # false positive допустим, поскольку gate ничего
                # не меняет в epistemic lifecycle.
                if _connector_hits > 0:
                    suspected_compound_claims.append(_claim_line)

            _atomicity_total = len(claim_lines)
            _atomicity_suspected = len(suspected_compound_claims)

            _atomicity_ratio = (
                _atomicity_suspected / _atomicity_total
                if _atomicity_total
                else 0.0
            )

            print(
                f"[Claim Atomicity] "
                f"claims={_atomicity_total} "
                f"suspected_compound={_atomicity_suspected} "
                f"ratio={_atomicity_ratio:.2f}"
            )

            for _compound_idx, _compound_claim in enumerate(
                suspected_compound_claims,
                1,
            ):
                print(
                    f"[Claim Atomicity] SUSPECT "
                    f"{_compound_idx}: {_compound_claim[:300]}"
                )

            for claim_idx, claim_line in enumerate(claim_lines, 1):
                print(
                    f"[Synthesizer Claim DEBUG] "
                    f"{claim_idx}: {claim_line[:300]}"
                )

            if web_result and web_result.snippets:
                for i, snip in enumerate(web_result.snippets[:5]):
                    text = _get_snippet_text(snip)
                    if len(text) > 100:
                        ev_id = f"ev_{uuid.uuid4().hex[:8]}"

                        source_uri = _get_snippet_url(snip)
                        source_title = _get_snippet_title(snip)

                        quality = evaluate_source_quality(
                            url=source_uri,
                            title=source_title,
                            text=text,
                            source_type="web",
                        )

                        evidence_records.append({
                            "evidence_id": ev_id,
                            "source_type": "web",
                            "source_uri": source_uri,
                            "source_title": source_title,
                            "content_excerpt": text[:500],
                            "relevance_to_query": 0.7 if i < 3 else 0.3,

                            # Source Quality Gate metadata.
                            "quality_score": quality.quality_score,
                            "source_class": quality.source_class,
                            "evidence_eligible": quality.evidence_eligible,
                            "evidence_role": quality.evidence_role,
                            "authority": quality.authority,
                            "traceability": quality.traceability,
                            "primaryness": quality.primaryness,
                            "quality_reasons": list(quality.reasons),

                            "is_meta_pipeline_output": False,
                            "is_subject_matter_evidence": True,
                            "rejection_reason": None,
                        })

                        print(
                            f"[Source Quality] "
                            f"{source_uri[:60]} "
                            f"class={quality.source_class} "
                            f"role={quality.evidence_role} "
                            f"score={quality.quality_score:.3f} "
                            f"eligible={quality.evidence_eligible}"
                        )

            # ВАЖНО:
            # Synthesizer НЕ связывает claims с evidence.
            #
            # Раньше каждому claim автоматически назначались первые
            # два evidence:
            #
            #     evidence_ids[:2]
            #
            # Это создавало ложный grounding: наличие источника
            # ошибочно превращалось в доказательство конкретного claim.
            #
            # Единственный владелец связи claim -> evidence теперь:
            #
            #     claim_evidence_mapper.map_claims_to_evidence()
            #
            # Здесь claims и evidence существуют независимо.
            # Все извлечённые claims должны попасть в lifecycle.
            #
            # Раньше здесь использовался claim_lines[:5], из-за чего
            # Synthesizer мог извлечь, например, 23 claims, но только
            # первые 5 превращались в Claim objects. Остальные исчезали
            # ещё до Structural Validator / Mapper / NLI.
            #
            # Ограничение дорогих downstream-операций, если оно потребуется,
            # должно происходить явно на соответствующем этапе, а не здесь.
            for i, line in enumerate(claim_lines):
                # --------------------------------------------------------
                # NO META FILTER HERE
                # --------------------------------------------------------
                #
                # Synthesizer отвечает только за создание Claim objects.
                #
                # Нельзя определять meta-claim по отдельным словам вроде:
                #
                #     "отсутствует"
                #     "отсутствуют"
                #     "не содержит"
                #
                # поскольку отрицание является нормальной частью
                # проверяемого factual claim:
                #
                #     "Юпитер не имеет твёрдой поверхности."
                #
                # Structural/meta classification принадлежит
                # ClaimValidator downstream.
                #
                # Поэтому ВСЕ извлечённые claim_lines входят
                # в epistemic lifecycle.
                claim_type = "hypothesis"
                claim_id = f"cl_{uuid.uuid4().hex[:8]}"
                # Никаких искусственных связей здесь.
                # Mapper определит реальные candidate evidence позже.
                claim_evidence_map[claim_id] = []

                # D: supports_query_aspect теперь реальная роль claim
                # относительно query (CORE/DIRECT_DECISION_EVIDENCE/
                # EXPLANATORY/BACKGROUND), а не всегда "general".
                # Для query, не являющихся existence-questions,
                # _classify_claim_role возвращает role=None — тогда
                # сохраняем прежнее поведение ("general"), т.к. role-
                # логика для таких query намеренно не применяется
                # (см. claim_evidence_retriever.py).
                try:
                    _role_info = _classify_claim_role(
                        line,
                        enriched.original,
                    )
                    _claim_role = _role_info["role"] or "general"
                except Exception:
                    _claim_role = "general"
                    _role_info = None

                claims.append({
                    "claim_id": claim_id,
                    "claim_text": line[:300],
                    "claim_type": claim_type,
                    "claim_confidence": 0.3,  # Всегда низкая
                    "supports_query_aspect": [_claim_role],
                    # P0-A (autonomous fix pass): persist the FULL
                    # classification (not just the role label) so that
                    # claim_evidence_retriever.py's reuse path can show
                    # real target_match/has_assertion/has_instrument in
                    # its [Claim Retrieval Priority] diagnostic instead
                    # of hardcoding None whenever role is reused instead
                    # of recomputed. Single source of truth — same
                    # _classify_claim_role() call above, no second
                    # independent classifier.
                    "_role_classification": _role_info,
                    "derived_from_evidence_ids": [],
                    "is_meta": False,
                })
                used_count += 1

            print(
                f"[Synthesizer Claims] "
                f"extracted={len(claim_lines)} "
                f"lifecycle={len(claims)} "
                f"meta_skipped={len(meta_comments)}"
            )

            compose_prompt_template = _get_compose_prompt(answer_mode)
            compose_prompt = compose_prompt_template.format(
                query=enriched.original,
                frame_hint=hint,
                context=context[:4000],
            )
            raw_answer = _call(compose_prompt, max_tokens=600, temp=TEMP_ANALYST)
        else:
            raw_answer = _call(
                _SINGLE_PROMPT.format(query=enriched.original, frame_hint=hint),
                max_tokens=MAX_TOKENS_ANALYST,
                temp=TEMP_ANALYST,
            )
            why_trust.append("нет контекста — ответ из знаний модели")

        answer = _strip_think(raw_answer)
        answer = _strip_self_report(answer)
        answer = _remove_meta_facts(answer)

    except Exception as e:
        # P0 (YANDI_CLAIM_LIFECYCLE_DISAPPEARANCE_AUDIT.md):
        # Раньше этот except перехватывал ЛЮБОЙ сбой во всём блоке —
        # включая сбой ПОЗДНЕГО, не связанного с claims шага (compose
        # answer LLM-вызов, например ReadTimeout после TIMEOUT=180s) —
        # и терял уже полностью построенные claims/evidence_records,
        # возвращая {"error": ...} без ключа "claims". Orchestrator
        # читает claims_data = reasoning_info.get("claims", []), поэтому
        # 15 успешно извлечённых claims молча превращались в 0 ещё до
        # ClaimValidator, хотя extraction сам по себе не падал.
        #
        # Claims, извлечённые ДО сбоя, остаются валидными объектами
        # lifecycle независимо от того, что упало ПОЗЖЕ (composition).
        return SynthesisResult(
            answer=f"Не удалось получить ответ: {e}",
            confidence=0.0, sources=[], trust_level="UNVERIFIED",
        ), {
            "error": str(e),
            "claims": claims,
            "evidence_records": evidence_records,
        }

    # ── ЕДИНАЯ МОДЕЛЬ TRUST ──────────────────────────────────────────────
    web_snippets = web_result.snippets if web_result else []
    web_count = sum(1 for s in web_snippets if len(_get_snippet_text(s)) >= 200)
    local_ok = bool(search_result and search_result.confidence >= 0.7)
    
    print("[TRUST] Начало вычисления Trust...")
    # Компоненты Trust
    claim_validity_score = 0.0
    source_agreement = 0.0
    source_quality = 0.0
    hypothesis_consistency = 0.0
    reflection_success = 0.0
    historical_reliability = 0.0
    
    # Evidence Score — доля claims, имеющих привязку к evidence
    if claims:
        grounded_claims = sum(
            1 for c in claims
            if c.get("derived_from_evidence_ids")
        )
        evidence_score = grounded_claims / len(claims)
    else:
        evidence_score = 0.0

    # 1. Validation Score (если есть validation в frame)
    if frame.get("validation_result"):
        claim_validity_score = frame["validation_result"].get("score", 0.0)
    else:
        claim_validity_score = 0.3  # нет валидации — средняя уверенность
    
    # 2. Source Agreement (согласие между источниками)
    if web_count >= 3:
        source_agreement = 0.8
    elif web_count >= 2:
        source_agreement = 0.6
    elif web_count >= 1:
        source_agreement = 0.4
    elif local_ok:
        source_agreement = 0.3
    else:
        source_agreement = 0.1
    
    # 3. Source Quality (репутация источников)
    if web_count >= 3:
        source_quality = 0.7
    elif web_count >= 2:
        source_quality = 0.5
    elif web_count >= 1:
        source_quality = 0.3
    elif local_ok:
        source_quality = 0.4
    else:
        source_quality = 0.1
    
    # 4. Hypothesis Consistency (согласованность гипотез)
    hypothesis_graph = frame.get("hypothesis_graph")
    if hypothesis_graph:
        if hasattr(hypothesis_graph, "nodes"):
            nodes = hypothesis_graph.nodes
            if nodes and len(nodes) > 5:
                hypothesis_consistency = 0.7
            elif nodes and len(nodes) > 2:
                hypothesis_consistency = 0.5
            else:
                hypothesis_consistency = 0.3
        else:
            hypothesis_consistency = 0.2
    else:
        hypothesis_consistency = 0.1
    
    # 5. Reflection Success (успешность рефлексии)
    # По умолчанию, пока нет реальной рефлексии
    reflection_success = 0.3
    
    # 6. Historical Reliability (историческая надёжность)
    # По умолчанию, пока нет истории
    historical_reliability = 0.4
    trust = "UNVERIFIED"
    confidence = 0.0

    try:
        trust_raw = (
            claim_validity_score * 0.10 +
            evidence_score * 0.20 +
            source_agreement * 0.15 +
            source_quality * 0.15 +
            hypothesis_consistency * 0.15 +
            reflection_success * 0.15 +
            historical_reliability * 0.10
        )
        sys.stderr.write(f"[TRUST DEBUG] trust_raw={trust_raw:.3f}\n")
        if trust_raw >= 0.7:
            trust = "STRONGLY_SUPPORTED"
        elif trust_raw >= 0.5:
            trust = "PARTIALLY_SUPPORTED"
        elif trust_raw >= 0.3:
            trust = "WEAKLY_SUPPORTED"
        else:
            trust = "UNVERIFIED"
        confidence = trust_raw
        why_trust = [
            f"validity={claim_validity_score:.2f}",
            f"evidence={evidence_score:.2f}",
            f"agreement={source_agreement:.2f}",
            f"quality={source_quality:.2f}",
            f"hypothesis={hypothesis_consistency:.2f}",
            f"reflection={reflection_success:.2f}",
            f"historical={historical_reliability:.2f}",
            f"total={trust_raw:.2f}"
        ]
        sys.stderr.write(f"[TRUST DEBUG] trust={trust}, confidence={confidence:.3f}\n")
    except Exception as e:
        trust = "UNVERIFIED"
        confidence = 0.0
        why_trust = [f"TRUST ERROR: {e}"]
        sys.stderr.write(f"[TRUST ERROR] {e}\n")
        confidence = 0.0
        why_trust = [f"TRUST ERROR: {e}"]
        sys.stderr.write(f"[TRUST ERROR] {e}\n")

    sources = _extract_sources(search_result, web_result)

    # ---- ДОБАВЛЕНИЕ ОПРОВЕРЖЕНИЙ В ОТВЕТ ----
    print("[Synthesizer DEBUG] classified_sources in frame:", frame.get("classified_sources", "NOT FOUND"))
    # ---- ДОБАВЛЕНИЕ АЛЬТЕРНАТИВНЫХ ИСТОЧНИКОВ В ОТВЕТ ----
    classified = frame.get("classified_sources", {})
    contradicts = classified.get("contradicts", [])
    supports = classified.get("supports", [])
    
    if contradicts or supports:
        print(f"[Synthesizer] refutation_text будет добавлен: supports={len(supports)}, contradicts={len(contradicts)}")
        refutation_text = "\n\n=== ИСТОЧНИКИ ===\n"
        
        if supports:
            refutation_text += "\n📚 **Подтверждающие источники:**\n"
            for source in supports[:2]:
                text = source.get("text", "")
                print(f"[Synthesizer] support text length: {len(text)}")
                url = source.get("url", "")
                if text and len(text) > 20:
                    refutation_text += f"- {text[:200]}...\n"
                    if url:
                        refutation_text += f"  Источник: {url}\n"
    # Возвращаем объект, совместимый с Orchestrator.
    #
    # ВАЖНО:
    # раньше использовалось:
    #
    #   from orchestrator_v2 import LocalSynthesisResult as SynthesisResult
    #
    # Это делало SynthesisResult ЛОКАЛЬНЫМ именем для всей функции
    # synthesize(). Если исключение происходило раньше этой строки,
    # exception handler не мог обратиться к импортированному сверху
    # agent.orch_schemas.SynthesisResult и падал с UnboundLocalError.
    from agent.orchestrator_v2 import LocalSynthesisResult

    synthesis_result = LocalSynthesisResult(
        # Canonical answer — фактически сформированный Synthesizer output.
        #
        # local_answer является независимым candidate input, а не
        # безусловным владельцем финального ответа.
        answer=(answer or local_answer or "").strip(),
        trust_level=trust,
        confidence=confidence,
        why_trust=why_trust,
        sources=sources,
        refutation_text=refutation_text if "refutation_text" in locals() else ""
    )
    reasoning_info = {
        "trust": trust,
        "confidence": confidence,
        "why_trust": why_trust,
        "source_count": len(sources) if sources else 0,
        "refutation_count": len(contradicts) if 'contradicts' in locals() else 0,
        "claims": claims,
        "evidence_records": evidence_records if 'evidence_records' in locals() else []
    }
    return synthesis_result, reasoning_info
