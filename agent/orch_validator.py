"""
assistant/orch_validator.py — Parallel Validator.
Отправляет вопрос + предварительный ответ 3 нодам одновременно.
MVP: локальные Ollama с разными seed (псевдо-независимость).
Council mode: endpoint="council" → реальные GPT/Claude/DeepSeek.
"""
from __future__ import annotations

import concurrent.futures
import json
import re
import time

import requests as _requests

from llm_gateway import complete as llm_complete

from agent.orch_schemas      import NodeSelectorResult, NodeValidation, ValidationResult
from agent.orch_node_selector import get_node_params, P2P_ENDPOINT_SENTINEL
from agent.orch_reputation   import update_node
from agent.orch_peer_directory import infer_on_peer, PeerDirectoryError

# _session остаётся: _validate_on_yandi_node() ниже бьёт в отдельный
# YANDI-транспорт (pet/council_chat_server.py), не в Ollama — это не
# call-сайт для llm_gateway.
TIMEOUT  = 90   # секунд на одну ноду

_session = _requests.Session()
_session.trust_env = False

VALIDATOR_PROMPT = """Ты верификатор ответов. Проверь правильность ответа на вопрос.

Верни ТОЛЬКО валидный JSON:
{{
  "verdict": "agree|disagree|partial",
  "reason": "краткое обоснование (1-2 предложения)"
}}

Правила вердикта:
- agree: ответ в целом верный и полезный
- disagree: ответ содержит существенные ошибки или вводит в заблуждение
- partial: ответ частично верный, но неполный или есть неточности

Вопрос: {question}

Ответ для проверки:
{answer}"""

# Ключевые слова для разбора свободного текста Council-нод
_AGREE_KW    = ["верн", "правильн", "точн", "согласен", "agree", "correct", "accurate"]
_DISAGREE_KW = ["неверн", "ошибк", "неправильн", "не согласен", "disagree", "incorrect", "wrong", "mislead"]


def _parse_free_text_verdict(text: str) -> tuple[str, str]:
    """
    Извлечь вердикт из свободного текста Council-ноды.
    Сначала пробуем JSON, затем ключевые слова.
    """
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Попытка 1: структурированный JSON
    try:
        data = json.loads(text)
        v = data.get("verdict", "")
        if v in ("agree", "disagree", "partial"):
            return v, data.get("reason", "")
    except Exception:
        pass
    m = re.search(r"\{.*?\}", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group())
            v = data.get("verdict", "")
            if v in ("agree", "disagree", "partial"):
                return v, data.get("reason", "")
        except Exception:
            pass

    # Попытка 2: ключевые слова в свободном тексте
    lower = text.lower()
    disagree_score = sum(1 for kw in _DISAGREE_KW if kw in lower)
    agree_score    = sum(1 for kw in _AGREE_KW    if kw in lower)

    if disagree_score > agree_score:
        verdict = "disagree"
    elif agree_score > 0:
        verdict = "agree"
    else:
        verdict = "partial"

    reason = text[:200].replace("\n", " ").strip()
    return verdict, reason


def _validate_on_council_node(
    node_id: str,
    question: str,
    answer: str,
    domain: str,
) -> NodeValidation:
    """Валидация через Council (GPT/Claude/DeepSeek) — реальная внешняя проверка."""
    from agent.orch_council_connector import ask_council

    t0 = time.time()
    # Определить имя модели по node_id (council-claude → claude)
    model_key = node_id.replace("council-", "")

    prompt = VALIDATOR_PROMPT.format(
        question=question[:500],
        answer=answer[:1500],
    )

    try:
        responses = ask_council(prompt, models=[model_key], timeout=60)
        raw = responses.get(model_key, "")
        latency = time.time() - t0

        if not raw:
            update_node(node_id, correct=False, latency=latency, domain=domain)
            return NodeValidation(
                node_id=node_id, verdict="partial", confidence=0.0,
                explanation="[Council не ответил]", latency=latency,
            )

        verdict, reason = _parse_free_text_verdict(raw)
        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, confidence=0.7,
                               explanation=reason, latency=latency)

    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id, verdict="partial", confidence=0.0,
            explanation=f"[ошибка Council: {e}]", latency=latency,
        )


def _validate_on_yandi_node(
    node_id: str,
    endpoint: str,
    question: str,
    answer: str,
    domain: str,
) -> NodeValidation:
    """Валидация через YANDI transport endpoint (pet/council_chat_server.py
    /api/yandi/validate).

    PET_AGENT_BOUNDARY_AUDIT.md Phase 4A: that endpoint is pure transport
    now (returns raw_text + transport_status, never a computed verdict) -
    this function is the only place that turns the raw browser-model text
    into agree/disagree/partial, via the same _parse_free_text_verdict()
    every other node type in this file already uses.

    transport_status "unavailable" (no active browser model) and
    "timeout" (relayed, no answer in time) are distinguished from an
    actual ambiguous/unparseable answer in the `reason` text, but both
    still resolve to verdict="partial" - neither state must count as
    negative (disagree) evidence just because nothing answered.
    """
    t0 = time.time()
    try:
        resp = _session.post(
            endpoint,
            json={"question": question[:500], "answer": answer[:1500]},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data    = resp.json()
        latency = time.time() - t0
        status  = data.get("transport_status", "completed" if data.get("ok") else "error")

        if status == "unavailable":
            update_node(node_id, correct=False, latency=latency, domain=domain)
            return NodeValidation(
                node_id=node_id, verdict="partial", confidence=0.0,
                explanation="[YANDI: нет активных браузерных моделей]", latency=latency,
            )
        if status == "timeout":
            update_node(node_id, correct=False, latency=latency, domain=domain)
            return NodeValidation(
                node_id=node_id, verdict="partial", confidence=0.0,
                explanation="[YANDI: нет ответа за отведённое время]", latency=latency,
            )
        raw = (data.get("raw_text") or "").strip()
        if not data.get("ok") or not raw:
            update_node(node_id, correct=False, latency=latency, domain=domain)
            return NodeValidation(
                node_id=node_id, verdict="partial", confidence=0.0,
                explanation=f"[YANDI: {data.get('transport_error', 'нет ответа')}]", latency=latency,
            )

        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        verdict, reason = _parse_free_text_verdict(raw)
        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, confidence=0.7,
                               explanation=reason, latency=latency)
    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id, verdict="partial", confidence=0.0,
            explanation=f"[ошибка YANDI: {e}]", latency=latency,
        )


def _parse_verdict_json(raw: str) -> dict:
    """Общий разбор ответа валидатора (используется и локальной, и
    реальной peer-веткой) — сначала пробуем JSON целиком, затем ищем
    JSON-подстроку в свободном тексте."""
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return {}


def _validate_on_peer_node(node_id: str, question: str, answer: str, domain: str) -> NodeValidation:
    """Мандат "Real Node Directory Integration": настоящая проверка на
    ФИЗИЧЕСКИ ДРУГОЙ, реальной, доверенной P2P-ноде — через
    orch_peer_directory.infer_on_peer() (canonical node_id → Rust
    AI-RPC bridge → её собственный llm_gateway → ответ). Никакой model/
    endpoint/backend этой ноды здесь нет и быть не может — она решает
    это сама."""
    t0 = time.time()
    prompt = VALIDATOR_PROMPT.format(question=question[:500], answer=answer[:1500])
    try:
        raw = infer_on_peer(
            node_id,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        data = _parse_verdict_json(raw)
        verdict = data.get("verdict", "partial")
        if verdict not in ("agree", "disagree", "partial"):
            verdict = "partial"
        reason  = data.get("reason", "")
        latency = time.time() - t0

        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, confidence=0.7,
                               explanation=reason, latency=latency)
    except PeerDirectoryError as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id,
            verdict="partial",
            confidence=0.0,
            explanation=f"[peer недоступен: {e}]",
            latency=latency,
        )


def _validate_on_node(
    node_id: str,
    model: str,
    endpoint: str,
    question: str,
    answer: str,
    domain: str,
) -> NodeValidation:
    """Выполнить валидацию на одной ноде (Ollama, Council, YANDI или
    реальный P2P-пир)."""
    if endpoint == "council":
        return _validate_on_council_node(node_id, question, answer, domain)

    if "/api/yandi/validate" in endpoint:
        return _validate_on_yandi_node(node_id, endpoint, question, answer, domain)

    if endpoint == P2P_ENDPOINT_SENTINEL:
        return _validate_on_peer_node(node_id, question, answer, domain)

    t0 = time.time()
    params = get_node_params(node_id)

    prompt = VALIDATOR_PROMPT.format(
        question=question[:500],
        answer=answer[:1500],
    )

    # Мандат "Node Intelligence RPC migration": раньше здесь стоял
    # base_url=endpoint, что в client.py безусловно уходило в
    # Ollama-протокол для ЛЮБОГО non-default base_url (обходя даже
    # explicit-config STEP1). Аудит подтвердил: get_node_params()
    # сегодня в каждой ветке отдаёт endpoint этой же ноды (реального
    # адреса другого физического узла не существует — см.
    # orch_node_selector.py/orch_reputation.py), т.е. фактического
    # межнодового HTTP-похода тут никогда и не было — только псевдо-
    # независимая проверка тем же backend'ом с другим seed. endpoint
    # оставлен в сигнатуре узла как логическая метка (для логов/отчёта),
    # backend теперь выбирает исключительно llm_gateway по имени model
    # — так же, как во всех остальных call-сайтах agent/. Настоящая
    # проверка ФИЗИЧЕСКИ ДРУГОЙ ноды — через YANDI Intelligence RPC
    # (node/src/ai_rpc), когда появится реальный peer-registry,
    # связывающий node_id с P2P-идентичностью; это отдельная, более
    # крупная задача (см. отчёт, раздел "ограничения").
    try:
        raw = llm_complete(
            prompt, model=model, max_tokens=200, timeout=TIMEOUT,
            temperature=params.get("temperature", 0.2),
            extra_options={"seed": params.get("seed", 0)},
        )
        data = _parse_verdict_json(raw)

        verdict = data.get("verdict", "partial")
        if verdict not in ("agree", "disagree", "partial"):
            verdict = "partial"
        reason  = data.get("reason", "")
        latency = time.time() - t0

        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, confidence=0.7,
                               explanation=reason, latency=latency)

    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id,
            verdict="partial",
            confidence=0.0,
            explanation=f"[ошибка ноды: {e}]",
            latency=latency,
        )


def validate_parallel(
    question: str,
    answer: str,
    nodes: NodeSelectorResult,
    domain: str = "general",
) -> ValidationResult:
    """
    Параллельная валидация ответа через несколько нод.

    Args:
        question: оригинальный вопрос
        answer:   предварительный ответ для проверки
        nodes:    выбранные ноды (NodeSelectorResult)
        domain:   домен для обновления репутации

    Returns:
        ValidationResult
    """
    validations: list[NodeValidation] = []
    timed_out:   list[str]            = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(nodes.nodes)) as ex:
        futures = {
            ex.submit(
                _validate_on_node,
                n.node_id, n.model, n.endpoint,
                question, answer, domain,
            ): n.node_id
            for n in nodes.nodes
        }
        for future in concurrent.futures.as_completed(futures, timeout=TIMEOUT + 5):
            node_id = futures[future]
            try:
                result = future.result(timeout=1)
                validations.append(result)
            except Exception:
                timed_out.append(node_id)

    agree    = sum(1 for v in validations if v.verdict == "agree")
    disagree = sum(1 for v in validations if v.verdict == "disagree")

    return ValidationResult(
        validations=validations,
        agree_count=agree,
        disagree_count=disagree,
        timed_out=timed_out,
    )


if __name__ == "__main__":
    from agent.orch_risk          import assess_risk
    from agent.orch_node_selector import select_nodes

    question = "Что такое Kademlia?"
    answer   = ("Kademlia — децентрализованный алгоритм маршрутизации для P2P-сетей. "
                "Использует XOR-метрику для определения расстояния между нодами. "
                "Применяется в BitTorrent, IPFS.")
    risk   = assess_risk(question)
    nodes  = select_nodes(risk, domain="tech")
    print(f"Валидируем через {len(nodes.nodes)} нод...")
    result = validate_parallel(question, answer, nodes, domain="tech")
    print(f"Agree: {result.agree_count}  Disagree: {result.disagree_count}  Timeout: {result.timed_out}")
    for v in result.validations:
        print(f"  [{v.verdict:8s}] {v.node_id} ({v.latency:.1f}s): {v.explanation[:80]}")
