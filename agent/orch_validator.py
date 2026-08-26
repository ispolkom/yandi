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

from agent.orch_schemas      import NodeSelectorResult, NodeValidation, ValidationResult
from agent.orch_node_selector import get_node_params
from agent.orch_reputation   import update_node

OLLAMA   = "http://127.0.0.1:11434"
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
                node_id=node_id, verdict="partial",
                reason="[Council не ответил]", latency=latency,
            )

        verdict, reason = _parse_free_text_verdict(raw)
        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, reason=reason, latency=latency)

    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id, verdict="partial",
            reason=f"[ошибка Council: {e}]", latency=latency,
        )


def _validate_on_yandi_node(
    node_id: str,
    endpoint: str,
    question: str,
    answer: str,
    domain: str,
) -> NodeValidation:
    """Валидация через YANDI AI-RPC (порт 18082)."""
    t0 = time.time()
    prompt = VALIDATOR_PROMPT.format(
        question=question[:500],
        answer=answer[:1500],
    )
    try:
        resp = _session.post(
            endpoint,
            json={"question": question[:500], "answer": answer[:1500]},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data    = resp.json()
        raw     = data.get("reason", data.get("verdict", "partial"))
        raw     = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        latency = time.time() - t0
        verdict, reason = _parse_free_text_verdict(raw)
        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, reason=reason, latency=latency)
    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id, verdict="partial",
            reason=f"[ошибка YANDI: {e}]", latency=latency,
        )


def _validate_on_node(
    node_id: str,
    model: str,
    endpoint: str,
    question: str,
    answer: str,
    domain: str,
) -> NodeValidation:
    """Выполнить валидацию на одной ноде (Ollama, Council или YANDI)."""
    if endpoint == "council":
        return _validate_on_council_node(node_id, question, answer, domain)

    if "/api/yandi/validate" in endpoint:
        return _validate_on_yandi_node(node_id, endpoint, question, answer, domain)

    t0 = time.time()
    params = get_node_params(node_id)

    prompt = VALIDATOR_PROMPT.format(
        question=question[:500],
        answer=answer[:1500],
    )

    try:
        resp = _session.post(
            f"{endpoint}/api/generate",
            json={
                "model":   model,
                "prompt":  prompt,
                "stream":  False,
                "options": {
                    "temperature": params.get("temperature", 0.2),
                    "seed":        params.get("seed", 0),
                    "num_predict": 200,
                },
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        raw  = resp.json().get("response", "").strip()
        raw  = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        data = {}
        try:
            data = json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except Exception:
                    pass

        verdict = data.get("verdict", "partial")
        if verdict not in ("agree", "disagree", "partial"):
            verdict = "partial"
        reason  = data.get("reason", "")
        latency = time.time() - t0

        update_node(node_id, correct=(verdict == "agree"), latency=latency, domain=domain)
        return NodeValidation(node_id=node_id, verdict=verdict, reason=reason, latency=latency)

    except Exception as e:
        latency = time.time() - t0
        update_node(node_id, correct=False, latency=latency, domain=domain)
        return NodeValidation(
            node_id=node_id,
            verdict="partial",
            reason=f"[ошибка ноды: {e}]",
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
        print(f"  [{v.verdict:8s}] {v.node_id} ({v.latency:.1f}s): {v.reason[:80]}")
