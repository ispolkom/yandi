"""
assistant/orch_citations.py — Citation Layer.
Добавляет источники к ответу в читаемом формате.
"""
from __future__ import annotations

import re
from pathlib import Path

from agent.orch_schemas import SynthesisResult, SearchResult, SearchDoc


def format_citations(sources: list[str]) -> str:
    """Форматировать список источников в виде сносок."""
    if not sources:
        return ""
    lines = ["\n\n---\n**Источники:**"]
    for i, src in enumerate(sources, 1):
        # Сократить путь до читаемого вида
        p = Path(src)
        short = p.name if len(src) > 60 else src
        lines.append(f"[{i}] {short}")
    return "\n".join(lines)


def add_citations(
    answer: str,
    synthesis: SynthesisResult,
    search_result: SearchResult | None = None,
) -> str:
    """
    Добавить источники к ответу.

    Args:
        answer:        финальный текст ответа
        synthesis:     результат синтеза (содержит sources)
        search_result: результат локального поиска (для confidence и доп. docs)

    Returns:
        Ответ с добавленными сносками (если sources есть)
    """
    sources = synthesis.sources or []

    # Добавить confidence метку если поиск дал результат
    if search_result and search_result.docs:
        conf = search_result.confidence
        if conf >= 0.85:
            label = "✓ высокая уверенность"
        elif conf >= 0.6:
            label = "~ средняя уверенность"
        else:
            label = "? низкая уверенность"
        conf_note = f"\n\n_Confidence поиска: {conf:.0%} ({label})_"
    else:
        conf_note = ""

    citations = format_citations(sources)
    return answer + conf_note + citations


def strip_citations(text: str) -> str:
    """Убрать секцию источников из текста (для повторной обработки)."""
    return re.split(r"\n\n---\n\*\*Источники:\*\*", text)[0].strip()


def build_source_list(docs: list[SearchDoc]) -> list[str]:
    """Извлечь список источников из найденных документов."""
    sources = []
    seen: set[str] = set()
    for doc in docs:
        src = doc.source
        if src and src not in seen:
            sources.append(src)
            seen.add(src)
    return sources


if __name__ == "__main__":
    from agent.orch_schemas import SynthesisResult, SearchResult, SearchDoc

    synth = SynthesisResult(
        answer="Kademlia — алгоритм маршрутизации в P2P-сетях.",
        confidence=0.8,
        sources=["registry/dataset/model_sessions/claude_20260517.jsonl",
                 "registry/council/sessions/042.md"],
        trust_level="VERIFIED",
    )
    search = SearchResult(docs=[], confidence=0.82, source="local")
    result = add_citations(synth.answer, synth, search)
    print(result)
