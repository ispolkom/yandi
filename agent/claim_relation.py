"""
agent/claim_relation.py — Определение отношения источников к основному утверждению.
"""
from typing import Dict, Any, List, Optional
import re

class ClaimRelation:
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    UNRELATED = "unrelated"
    UNCERTAIN = "uncertain"

def extract_main_claim(answer: str, query: str = "") -> str:
    """Выбирает из ответа гипотезу, наиболее связанную с вопросом."""
    if not answer:
        return ""

    sentences = [
        x.strip()
        for x in re.split(r'(?<=[.!?])\s+', answer)
        if 20 <= len(x.strip()) <= 500
    ]

    if not sentences:
        return answer[:500]

    if not query:
        return sentences[0][:500]

    stop_words = {
        "что", "это", "как", "для", "на", "в", "с", "по", "из", "от",
        "до", "за", "у", "о", "к", "и", "а", "но", "или", "же", "бы",
        "не", "да", "нет", "кто", "какой", "какая", "какие"
    }

    query_words = {
        w for w in re.findall(r"[а-яёa-z0-9]+", query.lower())
        if len(w) > 2 and w not in stop_words
    }

    best_sentence = sentences[0]
    best_score = -1

    for sentence in sentences:
        words = set(re.findall(r"[а-яёa-z0-9]+", sentence.lower()))
        score = len(query_words & words)

        if score > best_score:
            best_score = score
            best_sentence = sentence

    return best_sentence[:500]

def extract_claim_from_source(text: str, main_claim: str = "") -> str:
    """
    Выбирает из источника наиболее релевантные passage относительно main_claim.

    ВАЖНО:
    - embedding определяет только тематическую близость;
    - он НЕ определяет supports/contradicts;
    - возвращаем несколько лучших passage, чтобы NLI видел контекст;
    - окончательное логическое отношение определяет LLM NLI.
    """
    if not text:
        return ""

    # Делим источник на предложения.
    sentences = [
        re.sub(r"\s+", " ", x).strip()
        for x in re.split(r'(?<=[.!?])\s+', text)
        if len(re.sub(r"\s+", " ", x).strip()) >= 20
    ]

    if not sentences:
        return re.sub(r"\s+", " ", text).strip()[:1600]

    if not main_claim:
        return " ".join(sentences[:3])[:1600]

    try:
        import requests
        import numpy as np

        session = requests.Session()
        session.trust_env = False

        # P0 (autonomous fix pass, extract_claim_from_source 343s
        # investigation): this used to call /api/embed ONCE PER
        # SENTENCE (N+1 sequential HTTP round-trips per snippet: one
        # for main_claim, one per sentence) — measured live at ~160ms/
        # call, so a 20-sentence source cost ~3.2s in pure round-trip
        # overhead alone, called once per retrieved snippet (up to 10
        # per claim). Confirmed live: Ollama's /api/embed accepts a
        # LIST input and returns all embeddings in one call — batching
        # main_claim + every sentence into a single request measured
        # 12.7x faster for an equivalent 20-item batch (3.20s -> 0.25s).
        # Same cosine-similarity math, same ranking, same fallback —
        # this changes ONLY how many HTTP calls it takes, not what is
        # computed or selected.
        def _gemma_embed_batch(values):
            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": [v[:2000] for v in values],
                },
                timeout=30,
            )
            resp.raise_for_status()

            vecs = np.array(
                resp.json()["embeddings"],
                dtype=np.float32,
            )

            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return vecs / norms

        all_vecs = _gemma_embed_batch([main_claim] + sentences)
        query_vec = all_vecs[0]
        sentence_vecs = all_vecs[1:]

        scored = []

        for i, sentence in enumerate(sentences):
            similarity = float(np.dot(query_vec, sentence_vecs[i]))
            scored.append((similarity, i, sentence))

        # Берём три наиболее близких passage.
        # Порог намеренно не используется как доказательство:
        # ranking нужен только для выбора контекста NLI.
        best = sorted(
            scored,
            key=lambda item: item[0],
            reverse=True,
        )[:3]

        if not best:
            return " ".join(sentences[:3])[:1600]

        # После semantic ranking возвращаем passage в исходном порядке,
        # чтобы не разрушать контекст источника.
        best.sort(key=lambda item: item[1])

        return " ".join(
            sentence for _, _, sentence in best
        )[:1600]

    except Exception:
        # Аварийный lexical ranking.
        # Никакого вывода supports/contradicts здесь не делаем.
        stop_words = {
            "что", "это", "как", "для", "на", "в", "с", "по", "из", "от",
            "до", "за", "у", "о", "к", "и", "а", "но", "или", "же", "бы",
            "да", "нет"
        }

        main_words = {
            w
            for w in re.findall(r"[а-яёa-z0-9]+", main_claim.lower())
            if len(w) > 2 and w not in stop_words
        }

        scored = []

        for i, sentence in enumerate(sentences):
            sentence_words = set(
                re.findall(r"[а-яёa-z0-9]+", sentence.lower())
            )

            score = len(main_words & sentence_words)
            scored.append((score, i, sentence))

        best = sorted(
            scored,
            key=lambda item: item[0],
            reverse=True,
        )[:3]

        best.sort(key=lambda item: item[1])

        return " ".join(
            sentence for _, _, sentence in best
        )[:1600]

def classify_relation(main_claim: str, source_claim: str) -> str:
    """
    Определяет отношение source_claim к main_claim.

    ВАЖНО:
    наличие слов 'не', 'но', 'однако' само по себе НЕ означает
    противоречие. Сначала проверяем смысловую близость.

    YANDI не считает источник истинным только потому, что он
    семантически согласуется с утверждением.
    """

    if not main_claim or not source_claim:
        return ClaimRelation.UNCERTAIN

    try:
        from agent.orch_registry_search import _embed
        import numpy as np

        main_vec = _embed(main_claim[:1000])
        source_vec = _embed(source_claim[:1000])

        similarity = float(np.dot(main_vec, source_vec))

    except Exception:
        # Если embedding недоступен — не выдумываем отношение.
        similarity = None

    def words(text):
        stop = {
            "что", "это", "как", "для", "на", "в", "с", "по",
            "из", "от", "до", "за", "у", "о", "к", "и", "а",
            "но", "или", "же", "бы", "да"
        }
        return {
            w for w in re.findall(r"[а-яёa-z0-9]+", text.lower())
            if len(w) > 2 and w not in stop
        }

    main_words = words(main_claim)
    source_words = words(source_claim)

    common = main_words & source_words

    if not common:
        if similarity is not None and similarity < 0.35:
            return ClaimRelation.UNRELATED
        return ClaimRelation.UNCERTAIN

    # --------------------------------------------------------
    # Явное отрицание.
    #
    # Не считаем любое "не" противоречием.
    # Для CONTRADICTS нужна одновременно:
    #   1. тематическая близость;
    #   2. явная отрицательная конструкция.
    # --------------------------------------------------------

    explicit_negation = bool(re.search(
        r"\b("
        r"не является|"
        r"не существует|"
        r"не может|"
        r"не имеет|"
        r"не означает|"
        r"не подтверждается|"
        r"не соответствует|"
        r"не согласуется|"
        r"неверно|"
        r"ложно|"
        r"опровергает|"
        r"противоречит"
        r")\b",
        source_claim.lower()
    ))

    if explicit_negation:
        if similarity is None or similarity >= 0.45:
            return ClaimRelation.CONTRADICTS

    # --------------------------------------------------------
    # Семантически близкий источник.
    #
    # Это SUPPORTS только в смысле "содержательно согласуется",
    # а НЕ "доказывает истинность".
    # --------------------------------------------------------

    if similarity is not None:
        if similarity >= 0.60:
            return ClaimRelation.SUPPORTS

        if similarity >= 0.40:
            return ClaimRelation.UNCERTAIN

        return ClaimRelation.UNRELATED

    # lexical fallback
    overlap_ratio = len(common) / max(1, len(main_words))

    if overlap_ratio >= 0.50:
        return ClaimRelation.SUPPORTS

    if overlap_ratio >= 0.20:
        return ClaimRelation.UNCERTAIN

    return ClaimRelation.UNRELATED



def infer_claim_relation(
    main_claim: str,
    other_claim: str,
) -> Dict[str, Any]:
    """
    Общий LLM NLI primitive для логического отношения двух утверждений.

    ВАЖНО:
    - embeddings здесь не определяют логическое отношение;
    - тематическая похожесть не означает supports;
    - функция не оценивает истинность утверждений;
    - функция не оценивает качество источника;
    - fallback считается аварийным и явно маркируется.
    """
    if not main_claim or not other_claim:
        return {
            "relation": ClaimRelation.UNCERTAIN,
            "method": "invalid_input",
            "error": None,
        }

    import json
    import os
    import requests

    session = requests.Session()
    session.trust_env = False

    model = os.environ.get("YANDI_LOCAL_MODEL", "heretic:q8")

    allowed = {
        ClaimRelation.SUPPORTS,
        ClaimRelation.CONTRADICTS,
        ClaimRelation.UNRELATED,
        ClaimRelation.UNCERTAIN,
    }

    prompt = f"""
Ты классифицируешь ТОЛЬКО логическое отношение одного утверждения
к другому утверждению.

ОСНОВНОЕ УТВЕРЖДЕНИЕ:
{main_claim}

ВТОРОЕ УТВЕРЖДЕНИЕ:
{other_claim}

Выбери ровно одно:

supports
- второе утверждение содержательно поддерживает основное;

contradicts
- второе утверждение логически противоположно или несовместимо
  с основным;

unrelated
- утверждения не связаны по существу;

uncertain
- утверждения относятся к общей теме, но между ними нельзя
  надёжно установить поддержку или противоречие.

КРИТИЧЕСКИ ВАЖНО:
- сравнивай СМЫСЛ двух утверждений, а не наличие одинаковых слов;
- одинаковая тема НЕ означает supports;
- одинаковые слова НЕ означают supports;
- отсутствие поддержки НЕ означает contradicts;
- CONTRADICTS означает, что оба утверждения не могут быть истинны
  одновременно в одном и том же смысле;
- SUPPORTS означает, что второе утверждение подтверждает, повторяет,
  уточняет или логически усиливает основное;
- если ОБА утверждения отрицают одно и то же, это SUPPORTS,
  а не CONTRADICTS;
- наличие слова "не" само по себе НИКОГДА не означает CONTRADICTS;
- если второе утверждение описывает конкретный факт, из которого
  непосредственно следует основное, это SUPPORTS;
- если тексты относятся к одной теме, но логическая связь недостаточна,
  используй UNCERTAIN;
- UNRELATED используй только когда второе утверждение действительно
  не относится по существу к основному;
- не оценивай истинность утверждений;
- не пытайся решать, какое утверждение правильное;
- определяй только логическое отношение ВТОРОГО утверждения
  к ОСНОВНОМУ.

ПРИМЕРЫ:

Основное: Планета не имеет твёрдой поверхности.
Второе: У этой планеты отсутствует твёрдая поверхность.
Ответ: supports

Основное: Планета не имеет твёрдой поверхности.
Второе: Планета имеет твёрдую поверхность.
Ответ: contradicts

Основное: Давление возрастает с глубиной.
Второе: На большой глубине давление достигает значений,
во много раз превышающих давление у поверхности.
Ответ: supports

Основное: Давление возрастает с глубиной.
Второе: Атмосфера содержит водород.
Ответ: unrelated

Верни ТОЛЬКО JSON:
{{"relation":"supports"}}
"""

    try:
        resp = session.post(
            "http://127.0.0.1:11434/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "format": "json",
                "options": {
                    "temperature": 0.0,
                    "num_predict": 40,
                },
            },
            timeout=60,
        )
        resp.raise_for_status()

        raw = resp.json().get("response", "").strip()
        parsed = json.loads(raw)

        relation = str(parsed.get("relation", "")).strip().lower()

        if relation not in allowed:
            raise ValueError(f"invalid relation: {relation!r}")

        return {
            "relation": relation,
            "method": "llm_nli",
            "error": None,
        }

    except Exception as e:
        relation = classify_relation(main_claim, other_claim)

        return {
            "relation": relation,
            "method": "fallback",
            "error": str(e)[:200],
        }


def infer_claim_relations_batch(
    pairs: List[Dict[str, Any]],
    batch_size: int = 16,
) -> List[Dict[str, Any]]:
    """
    Batch LLM NLI для большого количества пар claims.

    Вход:
        [
            {
                "pair_id": "0:1",
                "main_claim": "...",
                "other_claim": "...",
            },
            ...
        ]

    Выход сохраняет порядок входных пар:
        [
            {
                "pair_id": "0:1",
                "relation": "supports|contradicts|unrelated|uncertain",
                "method": "llm_nli_batch",
                "error": None,
            },
            ...
        ]

    ВАЖНО:
    - один batch = один Ollama generation;
    - batch failure НЕ запускает N одиночных LLM-вызовов;
    - при ошибке используется conservative fallback;
    - fallback никогда не должен сам создавать disagreement.
    """
    import json
    import os
    import requests

    if not pairs:
        return []

    allowed = {
        ClaimRelation.SUPPORTS,
        ClaimRelation.CONTRADICTS,
        ClaimRelation.UNRELATED,
        ClaimRelation.UNCERTAIN,
    }

    model = os.environ.get(
        "YANDI_LOCAL_MODEL",
        "heretic:q8",
    )

    session = requests.Session()
    session.trust_env = False

    results_by_id = {}

    # Не позволяем случайно получить batch_size <= 0.
    batch_size = max(1, int(batch_size or 1))

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start:start + batch_size]

        payload_pairs = []

        for item in batch:
            pair_id = str(item.get("pair_id", ""))

            main_claim = str(
                item.get("main_claim", "") or ""
            ).strip()

            other_claim = str(
                item.get("other_claim", "") or ""
            ).strip()

            payload_pairs.append({
                "pair_id": pair_id,
                "main_claim": main_claim,
                "other_claim": other_claim,
            })

        prompt = f"""
Ты выполняешь пакетную NLI-классификацию.

Для КАЖДОЙ пары утверждений независимо определи только логическое
отношение ВТОРОГО утверждения к ОСНОВНОМУ.

Допустимые значения:

supports
- второе утверждение поддерживает, повторяет, уточняет
  или непосредственно логически усиливает основное;

contradicts
- оба утверждения не могут быть истинны одновременно
  в одном и том же смысле;

unrelated
- утверждения не связаны по существу;

uncertain
- утверждения относятся к общей теме, но логическое отношение
  нельзя определить надёжно.

КРИТИЧЕСКИ ВАЖНО:

- сравнивай смысл, а не совпадение слов;
- одинаковая тема НЕ означает supports;
- отсутствие поддержки НЕ означает contradicts;
- слово "не" само по себе НЕ означает contradicts;
- если оба утверждения отрицают одно и то же — это supports;
- не определяй истинность утверждений;
- не выбирай, какое утверждение правильное;
- классифицируй КАЖДУЮ пару независимо;
- верни результат для КАЖДОГО pair_id;
- не пропускай pair_id;
- не добавляй пояснений.

ПАРЫ:

{json.dumps(payload_pairs, ensure_ascii=False)}

Верни ТОЛЬКО JSON следующего формата:

{{
  "results": [
    {{
      "pair_id": "0:1",
      "relation": "supports"
    }}
  ]
}}
"""

        # ----------------------------------------------------
        # DIAGNOSTIC ONLY (read-only counters for this chunk).
        #
        # Ничего из этого не влияет на results_by_id / relation /
        # method — только печать, чтобы отличить:
        #   1. модель реально вернула uncertain;
        #   2. pair отсутствовал в results;
        #   3. label был off-taxonomy;
        #   4. batch JSON/parsing упал.
        # ----------------------------------------------------
        expected_pair_ids = {
            str(item.get("pair_id", ""))
            for item in batch
        }

        try:
            resp = session.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.0,

                        # Для batch нужен существенно больший output budget,
                        # чем для одиночного NLI.
                        "num_predict": max(
                            160,
                            len(batch) * 32,
                        ),
                    },
                },
                timeout=120,
            )

            resp.raise_for_status()

            raw = resp.json().get(
                "response",
                "",
            ).strip()

            print(
                f"[NLI Batch Response] "
                f"chars={len(raw)} "
                f"preview={raw[:1000]!r}"
                + ("...(truncated)" if len(raw) > 1000 else "")
            )

            parsed = json.loads(raw)

            returned = parsed.get(
                "results",
                [],
            )

            if not isinstance(returned, list):
                raise ValueError(
                    "batch results is not a list"
                )

            # DIAGNOSTIC ONLY.
            returned_pair_ids = set()
            valid_count = 0
            invalid_count = 0

            for item in returned:
                if not isinstance(item, dict):
                    continue

                pair_id = str(
                    item.get("pair_id", "")
                )

                relation = str(
                    item.get("relation", "")
                ).strip().lower()

                # DIAGNOSTIC ONLY: считаем pair "возвращённым" моделью
                # независимо от того, валиден ли label.
                returned_pair_ids.add(pair_id)

                if relation not in allowed:
                    invalid_count += 1

                    print(
                        f"[NLI Batch Raw] "
                        f"pair_id={pair_id} "
                        f"raw_relation={relation!r} "
                        f"accepted_relation=- "
                        f"status=invalid_label"
                    )

                    continue

                valid_count += 1

                print(
                    f"[NLI Batch Raw] "
                    f"pair_id={pair_id} "
                    f"raw_relation={relation!r} "
                    f"accepted_relation={relation} "
                    f"status=valid"
                )

                results_by_id[pair_id] = {
                    "pair_id": pair_id,
                    "relation": relation,
                    "method": "llm_nli_batch",
                    "error": None,
                }

            # DIAGNOSTIC ONLY.
            missing_count = len(
                expected_pair_ids - returned_pair_ids
            )

            print(
                f"[NLI Batch Summary] "
                f"batch_size={len(batch)} "
                f"expected={len(expected_pair_ids)} "
                f"returned={len(returned_pair_ids)} "
                f"valid={valid_count} "
                f"invalid_label={invalid_count} "
                f"missing={missing_count} "
                f"parsing=ok"
            )

        except Exception as e:
            # ----------------------------------------------------
            # CONSERVATIVE FAILURE
            # ----------------------------------------------------
            #
            # Здесь НЕЛЬЗЯ запускать infer_claim_relation()
            # для каждой пары, иначе при batch failure мы снова
            # получим старые N последовательных LLM calls.
            #
            # Также fallback НЕ имеет права создавать contradiction.
            error_text = str(e)[:200]

            # DIAGNOSTIC ONLY.
            print(
                f"[NLI Batch Summary] "
                f"batch_size={len(batch)} "
                f"expected={len(expected_pair_ids)} "
                f"returned=0 valid=0 invalid_label=0 "
                f"missing={len(expected_pair_ids)} "
                f"parsing=failed "
                f"error={type(e).__name__}:{error_text}"
            )

            for item in batch:
                pair_id = str(
                    item.get("pair_id", "")
                )

                # DIAGNOSTIC ONLY.
                print(
                    f"[NLI Batch Raw] "
                    f"pair_id={pair_id} "
                    f"status=batch_error "
                    f"error={type(e).__name__}:{error_text}"
                )

                if pair_id not in results_by_id:
                    results_by_id[pair_id] = {
                        "pair_id": pair_id,
                        "relation": ClaimRelation.UNCERTAIN,
                        "method": "batch_fallback",
                        "error": error_text,
                    }

        # --------------------------------------------------------
        # PARTIAL RESPONSE PROTECTION
        # --------------------------------------------------------
        #
        # Модель может вернуть валидный JSON, но потерять одну пару.
        # Такая пара считается uncertain, а не отправляется
        # дополнительным LLM-вызовом.
        for item in batch:
            pair_id = str(
                item.get("pair_id", "")
            )

            if pair_id not in results_by_id:
                # DIAGNOSTIC ONLY.
                print(
                    f"[NLI Batch Raw] "
                    f"pair_id={pair_id} "
                    f"status=missing"
                )

                results_by_id[pair_id] = {
                    "pair_id": pair_id,
                    "relation": ClaimRelation.UNCERTAIN,
                    "method": "batch_missing",
                    "error": "pair missing from batch response",
                }

    return [
        results_by_id.get(
            str(item.get("pair_id", "")),
            {
                "pair_id": str(
                    item.get("pair_id", "")
                ),
                "relation": ClaimRelation.UNCERTAIN,
                "method": "batch_missing",
                "error": "missing result",
            },
        )
        for item in pairs
    ]


def classify_claim_evidence_batch(
    claim_jobs: List[Dict[str, Any]],
    batch_size: int = 16,
) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """
    Batch Claim <-> Evidence NLI.

    Вход:
        [
            {
                "claim_id": "...",
                "claim_text": "...",
                "sources": [source_dict, ...],
            },
            ...
        ]

    Выход:
        {
            claim_id: {
                "supports": [...],
                "contradicts": [...],
                "unrelated": [...],
                "uncertain": [...],
            }
        }

    ВАЖНО:
    - source_claim по-прежнему извлекается отдельно;
    - relation вычисляется batch NLI;
    - source metadata сохраняется;
    - batch failure не запускает каскад одиночных LLM-вызовов.
    """

    grouped = {}

    pairs = []
    pair_source = {}

    for job in claim_jobs or []:
        claim_id = str(job.get("claim_id", "") or "")
        claim_text = str(job.get("claim_text", "") or "").strip()
        sources = list(job.get("sources", []) or [])

        grouped[claim_id] = {
            ClaimRelation.SUPPORTS: [],
            ClaimRelation.CONTRADICTS: [],
            ClaimRelation.UNRELATED: [],
            ClaimRelation.UNCERTAIN: [],
        }

        if not claim_text:
            continue

        for source_index, original_source in enumerate(sources):
            source = dict(original_source)

            if source.get("relevance") == "rejected_irrelevant":
                source["relation"] = ClaimRelation.UNRELATED
                source["relation_method"] = "relevance_gate"
                source["source_claim"] = ""

                grouped[claim_id][
                    ClaimRelation.UNRELATED
                ].append(source)

                continue

            text = (
                source.get("text", "")
                or source.get("content", "")
                or ""
            )

            source_claim = extract_claim_from_source(
                text,
                claim_text,
            )

            source["source_claim"] = source_claim

            pair_id = f"{claim_id}:{source_index}"

            pairs.append({
                "pair_id": pair_id,
                "main_claim": claim_text,
                "other_claim": source_claim,
            })

            pair_source[pair_id] = (
                claim_id,
                source,
            )

    if not pairs:
        return grouped

    relations = infer_claim_relations_batch(
        pairs,
        batch_size=batch_size,
    )

    relation_by_id = {
        str(item.get("pair_id", "")): item
        for item in (relations or [])
        if isinstance(item, dict)
    }

    for pair_id, (claim_id, source) in pair_source.items():
        item = relation_by_id.get(
            pair_id,
            {},
        )

        relation = item.get(
            "relation",
            ClaimRelation.UNCERTAIN,
        )

        method = item.get(
            "method",
            "batch_missing",
        )

        error = item.get(
            "error",
        )

        if relation not in {
            ClaimRelation.SUPPORTS,
            ClaimRelation.CONTRADICTS,
            ClaimRelation.UNRELATED,
            ClaimRelation.UNCERTAIN,
        }:
            relation = ClaimRelation.UNCERTAIN
            method = "batch_invalid_relation"

        source["relation"] = relation
        source["relation_method"] = method

        if error:
            source["relation_error"] = str(error)[:200]

        grouped[claim_id][relation].append(
            source
        )

    return grouped


def classify_sources(main_claim: str, sources: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Определяет отношение каждого релевантного источника к main_claim.

    Embeddings здесь НЕ определяют supports/contradicts.
    Для каждого источника используется отдельный короткий NLI-запрос
    к локальной модели.

    Происхождение источника модели не сообщается:
    web/local/refutation остаются анонимными для судьи.
    """
    result = {
        ClaimRelation.SUPPORTS: [],
        ClaimRelation.CONTRADICTS: [],
        ClaimRelation.UNRELATED: [],
        ClaimRelation.UNCERTAIN: [],
    }

    if not sources:
        return result

    import json
    import os
    import requests

    session = requests.Session()
    session.trust_env = False

    model = os.environ.get("YANDI_LOCAL_MODEL", "heretic:q8")

    allowed = {
        ClaimRelation.SUPPORTS,
        ClaimRelation.CONTRADICTS,
        ClaimRelation.UNRELATED,
        ClaimRelation.UNCERTAIN,
    }

    for source in sources:
        if source.get("relevance") == "rejected_irrelevant":
            source["relation"] = ClaimRelation.UNRELATED
            source["relation_method"] = "relevance_gate"
            source["source_claim"] = ""
            result[ClaimRelation.UNRELATED].append(source)
            continue

        text = source.get("text", "") or source.get("content", "")

        # Выбираем несколько наиболее релевантных passage из источника.
        # Именно их видит NLI-судья, а не первые 1600 символов страницы.
        source_claim = extract_claim_from_source(text, main_claim)
        source["source_claim"] = source_claim

        excerpt = source_claim

        prompt = f"""
Ты классифицируешь ТОЛЬКО логическое отношение одного текста
к основному утверждению.

ОСНОВНОЕ УТВЕРЖДЕНИЕ:
{main_claim}

ТЕКСТ ИСТОЧНИКА:
{excerpt}

Выбери ровно одно:

supports
- текст утверждает совместимое с основным утверждением;

contradicts
- текст утверждает противоположное или несовместимое;

unrelated
- текст не относится по существу к основному утверждению;

uncertain
- тема связана, но определить направление нельзя.

КРИТИЧЕСКИ ВАЖНО:
- сравнивай СМЫСЛ текста с утверждением, а не отдельные слова;
- одинаковая тема НЕ означает supports;
- одинаковые слова НЕ означают supports;
- отсутствие поддержки НЕ означает contradicts;
- CONTRADICTS означает, что текст утверждает нечто логически
  несовместимое с основным утверждением;
- SUPPORTS означает, что текст подтверждает, повторяет, уточняет
  или содержит факт, из которого непосредственно следует утверждение;
- если утверждение и текст отрицают одно и то же, это SUPPORTS,
  а не CONTRADICTS;
- наличие отрицания само по себе НИКОГДА не означает CONTRADICTS;
- если текст сообщает, что с глубиной давление становится значительно
  выше, он SUPPORTS утверждение "давление возрастает с глубиной";
- если тема связана, но логической связи недостаточно, используй UNCERTAIN;
- UNRELATED используй только когда текст действительно не относится
  по существу к утверждению;
- не оценивай истинность текста;
- не оценивай авторитет источника;
- не пытайся решать, кто прав;
- определи только отношение ТЕКСТА к УТВЕРЖДЕНИЮ.

ПРИМЕРЫ:

Утверждение: Планета не имеет твёрдой поверхности.
Текст: Эта планета является газовым гигантом и твёрдая поверхность
у неё отсутствует.
Ответ: supports

Утверждение: Планета не имеет твёрдой поверхности.
Текст: Планета обладает твёрдой поверхностью.
Ответ: contradicts

Утверждение: Давление возрастает с глубиной.
Текст: На глубине нескольких тысяч километров давление достигает
значений примерно в 100000 раз выше земного поверхностного давления.
Ответ: supports

Утверждение: Давление возрастает с глубиной.
Текст: В атмосфере обнаружен аммиак.
Ответ: unrelated

Верни ТОЛЬКО JSON:
{{"relation":"supports"}}
"""

        try:
            resp = session.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                    "format": "json",
                    "options": {
                        "temperature": 0.0,
                        "num_predict": 40,
                    },
                },
                timeout=60,
            )
            resp.raise_for_status()

            raw = resp.json().get("response", "").strip()
            parsed = json.loads(raw)

            relation = str(parsed.get("relation", "")).strip().lower()

            if relation not in allowed:
                raise ValueError(f"invalid relation: {relation!r}")

            source["relation"] = relation
            source["relation_method"] = "llm_nli"
            result[relation].append(source)

        except Exception as e:
            # Только аварийный fallback.
            relation = classify_relation(main_claim, source_claim)

            source["relation"] = relation
            source["relation_method"] = "fallback"
            source["relation_error"] = str(e)[:200]

            result[relation].append(source)

    return result


def is_relevant(text: str, main_claim: str, threshold: float = 0.3) -> bool:
    """Семантическая релевантность через embeddinggemma в локальном Ollama."""
    if not text or not main_claim:
        return False

    try:
        import requests
        import numpy as np

        session = requests.Session()
        session.trust_env = False

        def _gemma_embed(value: str):
            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": value[:2000],
                },
                timeout=30,
            )
            resp.raise_for_status()
            vec = np.array(resp.json()["embeddings"][0], dtype=np.float32)
            norm = np.linalg.norm(vec)
            return vec / norm if norm > 0 else vec

        query_vec = _gemma_embed(main_claim)
        source_vec = _gemma_embed(text)

        score = float(np.dot(query_vec, source_vec))

        # По измеренным данным:
        # нерелевантное ~0.14-0.38
        # релевантное   ~0.45-0.56
        semantic_threshold = 0.45 if threshold >= 0.4 else 0.43

        return score >= semantic_threshold

    except Exception:
        # Аварийный lexical fallback
        main_words = set(re.findall(r"[а-яёa-z0-9]+", main_claim.lower()))
        text_words = set(re.findall(r"[а-яёa-z0-9]+", text.lower()))

        stop_words = {
            "что", "это", "как", "для", "на", "в", "с", "по", "из", "от",
            "до", "за", "у", "о", "к", "и", "а", "но", "или", "же", "бы",
            "не", "да", "нет"
        }

        main_words -= stop_words
        text_words -= stop_words

        if not main_words:
            return False

        return len(main_words & text_words) / len(main_words) >= threshold

