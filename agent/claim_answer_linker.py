"""
agent/claim_answer_linker.py — Связь ответа с claims для YANDI V5.

Связывает финальный ответ с использованными claims.
В трейсе появляется supporting_claim_ids.

Цель: каждое важное утверждение в ответе имеет происхождение.

Rust-перенос (2026-09-23): rustlib/yandi_rs/src/claim_answer_linker.rs — _extract_key_phrases()/
_is_claim_supporting()/link_answer_to_claims(). ClaimAnswerLinker как класс (linked_claims,
get_summary, enrich_trace) остаётся в Python. Доказан на совпадение тестом
agent/claim_answer_linker_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается переменной
окружения YANDI_CLAIM_ANSWER_LINKER_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs (`maturin develop`,
см. rustlib/README.md). Не собран — тихо остаёмся на Python.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import re
from typing import List, Dict, Any, Optional, Tuple

log = logging.getLogger("yandi.claim_answer_linker")

_rust_cal = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_cal():
    global _rust_cal
    if _rust_cal is None:
        if os.environ.get("YANDI_CLAIM_ANSWER_LINKER_ENGINE") == "rust":
            try:
                import yandi_rs.claim_answer_linker as _rs
                _rust_cal = _rs
                log.warning("YANDI_CLAIM_ANSWER_LINKER_ENGINE=rust: используется Rust-реализация claim_answer_linker (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_CLAIM_ANSWER_LINKER_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_cal = False
        else:
            _rust_cal = False
    return _rust_cal or None


class ClaimAnswerLinker:
    """
    Связывает ответ с claims.
    """
    
    def __init__(self):
        self.linked_claims: List[str] = []
    
    def link_answer_to_claims(
        self,
        answer: str,
        claims: List[Dict[str, Any]],
    ) -> Tuple[str, List[str]]:
        """
        Связать ответ с claims.

        Возвращает: (answer, supporting_claim_ids)
        """
        rs = _get_rust_cal()
        if rs is not None:
            return tuple(rs.link_answer_to_claims(answer or "", claims or []))
        if not claims:
            return answer, []
        
        # Извлекаем ключевые фразы из ответа
        key_phrases = self._extract_key_phrases(answer)
        
        # Ищем claims, которые поддерживают эти фразы
        supporting_ids = []
        for claim in claims:
            claim_text = claim.get("claim_text", "")
            if self._is_claim_supporting(claim_text, key_phrases):
                supporting_ids.append(claim.get("claim_id"))
        
        # Если связь не найдена — ничего не выдумываем.
        # Пустой список означает: ответ не удалось связать
        # с конкретными claims.
        if not supporting_ids:
            supporting_ids = []
        
        return answer, supporting_ids
    
    def _extract_key_phrases(self, text: str) -> List[str]:
        """Извлечь ключевые фразы из текста."""
        # Разбиваем на предложения
        sentences = re.split(r'[.!?]\s+', text)
        
        # Берём первые 5 предложений
        phrases = []
        for sent in sentences[:5]:
            sent = sent.strip()
            if len(sent) > 20:
                # Берём первые 50 символов как ключевую фразу
                phrases.append(sent[:50].lower())
        
        return phrases
    
    def _is_claim_supporting(self, claim_text: str, key_phrases: List[str]) -> bool:
        """Проверить, поддерживает ли claim ключевую фразу."""
        claim_lower = claim_text.lower()
        
        for phrase in key_phrases:
            # Проверяем, есть ли часть фразы в claim
            words = phrase.split()[:5]  # первые 5 слов
            word_match = sum(1 for w in words if w in claim_lower)
            if word_match >= len(words) * 0.4:  # 40% слов совпало
                return True
        
        return False
    
    def enrich_trace(self, outcome: Dict[str, Any], supporting_ids: List[str]) -> Dict[str, Any]:
        """
        Обогатить outcome supporting_claim_ids.
        """
        if outcome:
            outcome["supporting_claim_ids"] = supporting_ids
        return outcome
    
    def get_summary(self) -> Dict[str, Any]:
        """Статистика линкера."""
        return {
            "linked_claims_count": len(self.linked_claims),
        }


# Глобальный экземпляр
_linker: Optional[ClaimAnswerLinker] = None

def get_claim_answer_linker() -> ClaimAnswerLinker:
    global _linker
    if _linker is None:
        _linker = ClaimAnswerLinker()
    return _linker


if __name__ == "__main__":
    # Тестирование
    linker = get_claim_answer_linker()
    
    # Тестовый ответ
    answer = "Сознание определяется как способность к субъективному восприятию. Оно возникает из активности нейронных сетей. Современная наука изучает его через функциональную роль в принятии решений."
    
    # Тестовые claims
    claims = [
        {
            "claim_id": "cl_001",
            "claim_text": "Сознание определяется как способность к субъективному восприятию",
        },
        {
            "claim_id": "cl_002",
            "claim_text": "Нейробиология связывает сознание с активностью коры головного мозга",
        },
        {
            "claim_id": "cl_003",
            "claim_text": "В современной психологии сознание определяется по его функциональной роли",
        },
    ]
    
    print("=== CLAIM ANSWER LINKER TEST ===")
    print(f"Ответ: {answer[:80]}...")
    
    linked_answer, supporting_ids = linker.link_answer_to_claims(answer, claims)
    
    print(f"\nСвязанные claims: {supporting_ids}")
    print(f"Всего claims: {len(claims)}")
    
    # Проверка соответствия
    for claim in claims:
        if claim["claim_id"] in supporting_ids:
            print(f"  ✅ {claim['claim_text'][:60]}...")
        else:
            print(f"  ❌ {claim['claim_text'][:60]}...")
    
    print("\n=== ОБОГАЩЕНИЕ OUTCOME ===")
    outcome = {"final_answer": answer, "trust_label": "SUPPORTED"}
    enriched = linker.enrich_trace(outcome, supporting_ids)
    print(f"supporting_claim_ids: {enriched.get('supporting_claim_ids')}")
