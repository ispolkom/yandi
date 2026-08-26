"""
agent/hypothesis_builder.py — Построитель графа гипотез из источников.
Улучшенная версия: классификация гипотез по маркерам, без избыточной группировки.
"""

from __future__ import annotations

import sys
import re
import uuid
from pathlib import Path
from typing import List, Dict, Any, Optional
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.hypothesis_graph import (
    HypothesisGraph,
    Observation,
    Inference,
    Hypothesis,
    InterpretiveTradition,
    SupportScores,
    build_support,
)


# ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

def _normalize_text(text: str) -> str:
    return re.sub(r'\s+', ' ', text).strip()


def _extract_sentences(text: str) -> List[str]:
    sentences = re.split(r'[.!?]+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 15]


def _relevance_score(sentence: str, query: str) -> float:
    q_words = set(query.lower().split())
    s_words = set(sentence.lower().split())
    if not q_words:
        return 0.5
    intersection = q_words.intersection(s_words)
    return len(intersection) / len(q_words)


# ===== ИЗВЛЕЧЕНИЕ НАБЛЮДЕНИЙ (L1) =====

def extract_observations(text: str, source_ref: str = "unknown", query: str = "") -> List[Observation]:
    sentences = _extract_sentences(text)
    observations = []
    evaluative_words = ["зависть", "ревность", "гордость", "справедливость", "несправедливость",
                        "вина", "наказание", "ненависть", "предательство", "верность"]
    
    for sent in sentences:
        sent = _normalize_text(sent)
        if 15 < len(sent) < 800:
            evaluative_count = sum(1 for word in evaluative_words if word in sent.lower())
            if evaluative_count <= 2:
                if query:
                    rel = _relevance_score(sent, query)
                    if rel < 0.03:
                        continue
                obs_id = f"obs_{uuid.uuid4().hex[:8]}"
                observations.append(Observation(
                    id=obs_id,
                    text=sent,
                    source_ref=source_ref,
                    confidence=0.9,
                    is_direct_quote=False,
                    raw_quote=None,
                ))
    if not observations and len(text) > 50:
        obs_id = f"obs_{uuid.uuid4().hex[:8]}"
        observations.append(Observation(
            id=obs_id,
            text=text[:200],
            source_ref=source_ref,
            confidence=0.7,
            is_direct_quote=False,
        ))
    return observations


# ===== ПОСТРОЕНИЕ ЛОГИЧЕСКИХ СЛЕДСТВИЙ (L2) — оставляем только для причинных вопросов =====

def build_inferences(observations: List[Observation], question: str = "") -> List[Inference]:
    # Если вопрос не про причину/связь, пропускаем L2
    causal_keywords = ['почему', 'зачем', 'из-за', 'причина', 'следствие', 'после', 'затем', 'тогда']
    if not any(kw in question.lower() for kw in causal_keywords):
        return []
    
    inferences = []
    obs_texts = [obs.text.lower() for obs in observations]
    
    if any('гнев' in t or 'гневался' in t or 'разгневался' in t for t in obs_texts):
        inferences.append(Inference(
            id=f"inf_{uuid.uuid4().hex[:8]}",
            text="Упоминается сильное отрицательное эмоциональное состояние (гнев).",
            based_on=[obs.id for obs in observations if 'гнев' in obs.text.lower() or 'гневался' in obs.text.lower() or 'разгневался' in obs.text.lower()],
            confidence=0.9
        ))
    
    if any('после' in t or 'затем' in t or 'тогда' in t for t in obs_texts):
        inferences.append(Inference(
            id=f"inf_{uuid.uuid4().hex[:8]}",
            text="Между событиями существует временная и вероятная причинная связь.",
            based_on=[obs.id for obs in observations if any(w in obs.text.lower() for w in ['после', 'затем', 'тогда'])],
            confidence=0.7
        ))
    
    return inferences


# ===== ДИНАМИЧЕСКОЕ ИЗВЛЕЧЕНИЕ ГИПОТЕЗ (L3) с классификацией по маркерам =====

def classify_hypothesis(text: str) -> str:
    """Определяет категорию гипотезы по ключевым словам."""
    lower = text.lower()
    if 'конфликт' in lower or 'антагонизм' in lower or 'война' in lower:
        return 'Конфликт'
    elif 'сотрудничеств' in lower or 'взаимодейств' in lower or 'диалог' in lower:
        return 'Сотрудничество'
    elif 'независимость' in lower or 'независим' in lower or 'автоном' in lower:
        return 'Независимость'
    elif 'интеграция' in lower or 'синтез' in lower or 'единство' in lower:
        return 'Интеграция'
    else:
        return 'Общая точка зрения'


def extract_hypotheses_from_texts(texts: List[str], query: str = "") -> List[Hypothesis]:
    """
    Извлекает гипотезы из текстов, классифицирует их по маркерам.
    Каждая найденная точка зрения становится отдельной гипотезой.
    """
    raw_hypotheses = []
    all_text = " ".join(texts).lower()
    
    # Маркеры, указывающие на точку зрения
    markers = [
        "согласно", "по мнению", "исследователи считают", "некоторые учёные полагают",
        "предполагается", "выдвигается гипотеза", "есть точка зрения", "распространено мнение",
        "традиционно считается", "в литературе выделяют", "существует подход",
        "один из подходов", "другая интерпретация", "альтернативная точка зрения",
        "конфликт", "сотрудничество", "независимость", "диалог", "интеграция",
        "тезис", "концепция", "модель", "парадигма"
    ]
    
    # Собираем все предложения с маркерами
    for text in texts:
        sentences = _extract_sentences(text)
        for sent in sentences:
            sent_lower = sent.lower()
            for marker in markers:
                if marker in sent_lower:
                    hyp_text = _normalize_text(sent)
                    if len(hyp_text) > 30:
                        raw_hypotheses.append(hyp_text)
                        break
    
    # Если ничего не найдено, создаём обобщённую гипотезу
    if not raw_hypotheses:
        if "наука" in all_text and "религия" in all_text:
            raw_hypotheses = ["Наука и религия — разные способы познания, но могут пересекаться."]
        elif "коммунизм" in all_text or "демократия" in all_text:
            raw_hypotheses = ["Политические идеологии могут приобретать квазирелигиозные черты."]
        else:
            raw_hypotheses = ["В предоставленных текстах содержатся различные точки зрения."]
    
    # Формируем гипотезы без группировки, давая название по классификации
    hypotheses = []
    for hyp_text in raw_hypotheses:
        if len(hyp_text) > 200:
            description = hyp_text[:200] + "..."
        else:
            description = hyp_text
        name = classify_hypothesis(hyp_text)
        # Считаем поддержку как частоту упоминаний
        count = sum(1 for t in texts if hyp_text[:50] in t)
        support = build_support(
            text=min(1.0, count / 3) if count > 0 else 0.2,
            tradition=0.3,
            science=0.3
        )
        hypotheses.append(Hypothesis(
            id=f"hyp_{uuid.uuid4().hex[:8]}",
            name=name,
            description=description,
            support=support,
            explains=[],
            not_explains=[],
            competitors=[],
            assumptions=[],
            confirm_if=[],
            refute_if=[],
            origin="извлечено из текстов"
        ))
    
    # Ограничиваем количество до 5, чтобы не перегружать
    if len(hypotheses) > 5:
        hypotheses = hypotheses[:5]
    
    return hypotheses


# ===== ДИНАМИЧЕСКОЕ ИЗВЛЕЧЕНИЕ ШКОЛ (L4) =====

def extract_traditions_from_texts(texts: List[str]) -> List[InterpretiveTradition]:
    all_text = " ".join(texts)
    scholars = {
        "Конфликт": ["Докинз", "Уайт", "Фейнман", "Крик", "Эткинс", "Гинзбург"],
        "Сотрудничество": ["Брук", "Фернгрен", "Маска"],
        "Независимость": ["Гулд", "Доукинс"],
        "Диалог": ["Полани", "Барбур"],
    }
    
    found = {}
    for direction, names in scholars.items():
        found_names = [name for name in names if name in all_text]
        if found_names:
            found[direction] = found_names
    
    traditions = []
    for direction, names in found.items():
        traditions.append(InterpretiveTradition(
            id=f"trad_{uuid.uuid4().hex[:8]}",
            name=direction,
            description=f"Представлена учёными: {', '.join(names)}",
            preferred_hypotheses=[],
            key_figures=names,
            representative_works=[]
        ))
    
    # Если ничего не найдено, добавляем стандартные
    if not traditions:
        traditions = [
            InterpretiveTradition(
                id="trad_academic",
                name="Академическая традиция",
                description="Основана на критическом анализе источников и научных методах.",
                preferred_hypotheses=[],
                key_figures=[],
                representative_works=[]
            ),
            InterpretiveTradition(
                id="trad_philosophical",
                name="Философская традиция",
                description="Рассмотрение вопроса через призму философских категорий.",
                preferred_hypotheses=[],
                key_figures=[],
                representative_works=[]
            ),
        ]
    
    return traditions


# ===== ОСНОВНАЯ ФУНКЦИЯ =====

def build_hypothesis_graph(
    question: str,
    texts: List[str],
    source_refs: List[str] = None,
) -> HypothesisGraph:
    if source_refs is None:
        source_refs = ["unknown"] * len(texts)
    print(f"[DEBUG build] texts count: {len(texts)}")
    if texts:
        print(f"[DEBUG build] first text: {texts[0][:100]}")
    
    graph = HypothesisGraph(question=question)
    
    # L1
    all_obs = []
    for text, ref in zip(texts, source_refs):
        obs = extract_observations(text, ref, query=question)
        print(f"[DEBUG loop] text: {text[:50]}..., obs: {len(obs)}")
        all_obs.extend(obs)
    graph.observations = all_obs[:10]
    
    # L2 — только если вопрос причинный
    inferences = build_inferences(all_obs, question=question)
    graph.inferences = inferences
    
    # L3
    hypotheses = extract_hypotheses_from_texts(texts, query=question)
    graph.hypotheses = hypotheses
    
    # L4
    traditions = extract_traditions_from_texts(texts)
    graph.traditions = traditions
    
    return graph


# ===== ТЕСТ =====

if __name__ == "__main__":
    test_texts = [
        "Согласно современным исследованиям, наука и религия представляют собой разные способы познания мира. Некоторые учёные считают, что они могут дополнять друг друга.",
        "Коммунизм часто рассматривается как светская религия, имеющая свои догмы и ритуалы.",
        "Демократия основана на принципах свободы и равенства, что роднит её с религиозными ценностями."
    ]
    
    graph = build_hypothesis_graph(
        question="Разве наука, это не религия?",
        texts=test_texts,
        source_refs=["src1", "src2", "src3"]
    )
    
    print("=== ГРАФ ГИПОТЕЗ (улучшенный) ===")
    print(f"Вопрос: {graph.question}")
    print(f"Наблюдений: {len(graph.observations)}")
    print(f"Инференций: {len(graph.inferences)}")
    print(f"Гипотез: {len(graph.hypotheses)}")
    for h in graph.hypotheses:
        print(f"  - {h.name}: {h.description[:80]}...")
    print(f"Школ: {len(graph.traditions)}")
    for t in graph.traditions:
        print(f"  - {t.name}")
