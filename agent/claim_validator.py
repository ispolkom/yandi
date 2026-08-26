"""
agent/claim_validator.py — Валидатор атомарных утверждений для YANDI V5.

Отсеивает мусорные claims:
- мета-текст
- нумерация
- описания источников
- служебная информация

Оставляет только осмысленные утверждения о мире.
"""

from __future__ import annotations

import sys
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import re
from typing import List, Dict, Any, Tuple


class ClaimValidator:
    """
    Валидатор claims — фильтрует мусор и оставляет только знания.
    """
    
    # Паттерны для отбраковки
    META_PATTERNS = [
        # Только действительно self-referential / service / source-meta.
        #
        # ВАЖНО:
        # list markers, numbering, отрицания и отсутствие чего-либо
        # НЕ являются meta-признаками. Они нормализуются отдельно.
        r'(?i)источник\s+содержит',
        r'(?i)содержание\s+источников',
        r'(?i)проанализировав\s+(?:источник|источники|текст)',
        r'(?i)извлечённые\s+факты',
        r'(?i)список\s+фактов',
        r'(?i)сырые\s+данные',
        r'(?i)источник\s+#?\d+',
        r'(?i)интернет-источники',
        r'(?i)локальная\s+база',
        r'(?i)согласно\s+источнику',
        r'(?i)в\s+источнике',

        # Source/answer epistemic wrappers.
        #
        # Это утверждения о состоянии ответа/источников,
        # а не самостоятельные предметные claims.
        # ФИКС (YANDI_RUNTIME_REGRESSION_FIX_REPORT.md, §A):
        # раньше эти два паттерна матчились БЕЗУСЛОВНО — любое
        # предложение, начинающееся с этой хедж-фразы, считалось
        # meta, даже если дальше шёл обычный predметный factual
        # claim ("По имеющимся данным жизнь не обнаружена." —
        # SUBJECT MATTER, а не "по имеющимся данным ОТВЕТ
        # содержит..." — SOURCE/META). При этом сам
        # generate_local_answer прямо учит модель начинать
        # предложения именно с "по имеющейся информации".
        #
        # Теперь, как и у соседнего паттерна "согласно современным
        # данным", требуется явное продолжение про сам ответ/вывод/
        # источник — иначе это обычный predметный claim.
        r'(?i)^\s*по\s+имеющейся\s+информации\s*,?\s*'
        r'(?:ответ|вывод|оценка|источник[а-я]*)\b',
        r'(?i)^\s*по\s+имеющимся\s+данным\s*,?\s*'
        r'(?:ответ|вывод|оценка|источник[а-я]*)\b',
        r'(?i)^\s*некоторые\s+источники\s+'
        r'(?:указывают|сообщают|утверждают|предполагают)\b',
        r'(?i)^\s*согласно\s+современным\s+'
        r'(?:научным\s+)?данным\s*,?\s*'
        r'(?:ответ|вывод|оценка)\b',
        r'(?i)как\s+указано\s+в\s+источнике',
        r'(?i)ответ\s+содержит',
        r'(?i)данный\s+документ',
        r'(?i)этот\s+текст',
        r'(?i)статья\s+описывает',
        r'(?i)\|\s*[а-яa-z]+\s*\|',
        r'(?i)^---$',
        r'(?i)^===.*===$',
    ]
    
    # ------------------------------------------------------------
    # PIPELINE / MODEL META CLAIMS
    # ------------------------------------------------------------
    #
    # Это не утверждения о предметном мире, а высказывания
    # о работе модели, ответе, анализе, извлечении claims и т.п.
    #
    # Такие строки нельзя отправлять в:
    #
    #   retrieval -> mapper -> NLI -> claim status
    #
    # потому что evidence для них epistemically бессмысленен.
    #
    # ВАЖНО:
    # здесь намеренно используются достаточно специфичные конструкции,
    # чтобы не отбрасывать реальные утверждения, содержащие слова
    # "анализ", "ответ", "текст" и т.п. в предметном смысле.
    PIPELINE_META_PATTERNS = [
        # "Вот извлеченные атомарные claims:"
        r'(?i)^\s*вот\s+(?:извлеч[её]нн?ые|полученные|выделенные)\s+'
        r'(?:атомарные\s+)?(?:claims?|утверждения|факты)\s*:?\s*$',

        # "Анализ текста модели показывает..."
        r'(?i)^\s*анализ\s+(?:текста|ответа|вывода)\s+'
        r'(?:модели|системы|ассистента)\s+'
        r'(?:показывает|показал|указывает|выявляет|свидетельствует)',

        # "Ответ на вопрос ... содержит ..."
        r'(?i)^\s*ответ\s+на\s+(?:этот\s+)?вопрос\s+'
        r'(?:содержит|включает|имеет)',

        # "Ответ модели содержит..."
        r'(?i)^\s*(?:ответ|вывод)\s+'
        r'(?:модели|системы|ассистента)\s+'
        r'(?:содержит|включает|показывает|указывает)',

        # "В тексте/ответе модели утверждается..."
        r'(?i)^\s*в\s+(?:тексте|ответе|выводе)\s+'
        r'(?:модели|системы|ассистента)\s+'
        r'(?:утверждается|говорится|указывается|содержится)',

        # "Модель утверждает/сообщает..."
        r'(?i)^\s*(?:модель|ассистент|система)\s+'
        r'(?:утверждает|сообщает|отвечает|указывает|пишет|говорит)',

        # "Из текста были извлечены claims..."
        r'(?i)^\s*(?:из|на\s+основе)\s+'
        r'(?:данного\s+)?(?:текста|ответа)\s+'
        r'.{0,50}(?:извлеч[её]н|выделен|получен)',

        # Служебные заголовки extractor-а.
        r'(?i)^\s*(?:атомарные\s+)?'
        r'(?:claims?|утверждения|извлеченные\s+claims?)\s*:?\s*$',
    ]

    # Паттерны хороших claims (должен совпадать хотя бы один)
    GOOD_PATTERNS = [
        r'(?i)является',
        r'(?i)определяется',
        r'(?i)называется',
        r'(?i)представляет собой',
        r'(?i)состоит из',
        r'(?i)включает',
        r'(?i)содержит',
        r'(?i)возник',
        r'(?i)произошёл',
        r'(?i)создан',
        r'(?i)относится к',
        r'(?i)связан с',
        r'(?i)может быть',
        r'(?i)является результатом',
        r'(?i)имеет значение',
        r'(?i)составляет',
        r'(?i)достигает',
        r'(?i)продолжается',
        r'(?i)находится',
        r'(?i)расположен',
    ]
    
    @staticmethod
    def normalize_claim_text(claim_text: str) -> str:
        """
        Удалить только PRESENTATIONAL markup.

        Это не semantic rewriting.

        Примеры:
            "- Юпитер является..."  -> "Юпитер является..."
            "* Юпитер является..."  -> "Юпитер является..."
            "1. Юпитер является..." -> "Юпитер является..."

        Форматирование не должно определять epistemic судьбу claim.
        """
        text = (claim_text or "").strip()

        # Markdown bullets.
        text = re.sub(
            r'^\s*[-*+]\s+',
            '',
            text,
        )

        # Numbered list.
        text = re.sub(
            r'^\s*\d+[.)]\s+',
            '',
            text,
        )

        # Остаточное markdown-выделение.
        text = re.sub(r'^\*\*(.+)\*\*$', r'\1', text)

        return text.strip()


    def __init__(self):
        self.rejected_count = 0
        self.accepted_count = 0
        self.rejection_reasons: Dict[str, int] = {}
    
    def validate(self, claim_text: str) -> Tuple[bool, str]:
        """
        Проверить claim.
        Возвращает: (is_valid, reason)
        """
        text = self.normalize_claim_text(claim_text)
        
        # 1. Слишком короткие
        if len(text) < 20:
            return False, "too_short"
        
        # 2. Слишком длинные (куски текста, а не утверждения)
        if len(text) > 300:
            return False, "too_long"
        
        # 3. Жёсткий gate для pipeline/model meta claims.
        #
        # Выполняется ДО положительных эвристик. Строка вроде
        # "Вот извлеченные атомарные claims:" не должна становиться
        # factual claim только из-за заглавной буквы или других
        # поверхностных признаков.
        for pattern in self.PIPELINE_META_PATTERNS:
            if re.search(pattern, text):
                return False, "meta_pipeline_claim"

        # 4. Общие meta/source patterns.
        for pattern in self.META_PATTERNS:
            if re.search(pattern, text):
                return False, "meta_text"
        
        # 4. Проверка на хорошие паттерны.
        #
        # ВАЖНО:
        # GOOD_PATTERNS — это положительная эвристика качества формы,
        # а НЕ whitelist допустимых утверждений.
        #
        # Отсутствие совпадения с GOOD_PATTERNS не означает, что текст
        # не является claim. Structural Validator отвечает только за
        # пригодность утверждения к дальнейшему epistemic lifecycle.
        has_good_pattern = any(
            re.search(p, text)
            for p in self.GOOD_PATTERNS
        )

        if has_good_pattern:
            return True, "valid"

        # Дополнительная положительная эвристика.
        if self._looks_like_fact(text):
            return True, "fact"

        # Не уничтожаем потенциально валидный claim только потому,
        # что его языковая форма неизвестна текущему набору regex.
        #
        # Mapper / NLI / Claim Status определят его фактическую
        # поддержку позднее.
        return True, "weak_pattern"
    
    def _looks_like_fact(self, text: str) -> bool:
        """Проверить, выглядит ли текст как факт."""
        # Содержит цифры, даты, числа
        if re.search(r'\d+', text):
            return True
        
        # Содержит названия (с заглавной буквы)
        if re.search(r'[А-ЯЁ][а-яё]+', text):
            return True
        
        # Содержит глаголы в прошедшем времени
        if re.search(r'(?i)был|была|было|стал|стала|стало|создал|создала|возник|возникла', text):
            return True
        
        return False
    
    def filter_claims(self, claims: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Отфильтровать список claims.
        """
        filtered = []
        self.rejected_count = 0
        self.accepted_count = 0
        self.rejection_reasons = {}
        
        for claim in claims:
            original_text = claim.get("claim_text", "")
            text = self.normalize_claim_text(original_text)

            # Canonical claim representation downstream.
            #
            # Mapper / NLI не должны получать markdown bullet marker.
            claim["claim_text"] = text

            is_valid, reason = self.validate(text)
            
            if is_valid:
                # ClaimValidator проверяет только структурную пригодность claim:
                # это утверждение, а не мусор/meta/service text.
                #
                # ВАЖНО: прохождение этого фильтра НЕ является проверкой
                # истинности утверждения и не должно давать статус "verified".
                claim["verification_status"] = "candidate"
                claim["structural_validation"] = "accepted"

                # Structural quality описывает только качество формы claim.
                # Она не является evidence score и не повышает Trust.
                if reason == "weak_pattern":
                    claim["structural_quality"] = "weak"
                    claim["structural_warning"] = "no_good_pattern"
                else:
                    claim["structural_quality"] = "clean"
                    claim.pop("structural_warning", None)

                filtered.append(claim)
                self.accepted_count += 1
            else:
                claim["verification_status"] = "rejected"
                claim["structural_validation"] = "rejected"
                self.rejected_count += 1
                self.rejection_reasons[reason] = self.rejection_reasons.get(reason, 0) + 1
                # Добавляем причину отклонения в claim
                claim["_rejected_reason"] = reason
                claim["_rejected"] = True
        
        return filtered
    
    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику валидации."""
        return {
            "accepted": self.accepted_count,
            "rejected": self.rejected_count,
            "rejection_reasons": self.rejection_reasons,
            "total": self.accepted_count + self.rejected_count,
        }
    
    def summary(self) -> str:
        """Текстовое представление статистики."""
        stats = self.get_stats()
        return f"""
=== CLAIM VALIDATOR ===
Принято: {stats['accepted']}
Отклонено: {stats['rejected']}
Всего: {stats['total']}
Причины отклонения:
{chr(10).join(f'  - {k}: {v}' for k, v in stats['rejection_reasons'].items())}
"""


# Глобальный экземпляр
_validator: Optional[ClaimValidator] = None

def get_claim_validator() -> ClaimValidator:
    global _validator
    if _validator is None:
        _validator = ClaimValidator()
    return _validator


if __name__ == "__main__":
    # Тестирование
    validator = get_claim_validator()
    
    test_claims = [
        "2. Интернет-источники: Содержат общие определения жизни",  # Мусор
        "Сознание определяется как способность к субъективному восприятию",  # Хороший
        "Первый признак жизни датируется 3,5 млрд лет",  # Хороший
        "Источники описывают возникновение жизни",  # Мусор
        "Жизнь возникла из неживой материи в процессе абиогенеза",  # Хороший
        "3. Содержание источников:",  # Мусор
        "Согласно данным современной науки, мы можем с уверенностью говорить о том, что жизнь возникла",  # Хороший
    ]
    
    print("=== ТЕСТ CLAIM VALIDATOR ===")
    for claim in test_claims:
        is_valid, reason = validator.validate(claim)
        status = "✅" if is_valid else "❌"
        print(f"{status} {claim[:60]}... ({reason})")
    
    print("\n=== СТАТИСТИКА ===")
    print(validator.summary())
