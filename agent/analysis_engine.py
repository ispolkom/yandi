"""
agent/analysis_engine.py — Анализ произведений.
Для субъективных запросов — отключает factual pipeline.
"""

from typing import Dict, Any, Optional


class AnalysisEngine:
    def __init__(self):
        self.structure = {
            "what_is_asked": "о чём запрос",
            "hidden_emotions": "какие эмоции скрыты",
            "extractable_meaning": "что можно извлечь",
            "personal_perception": "моё восприятие",
        }

    def get_analysis_prompt(self, query: str, context: Dict[str, Any] = None) -> str:
        """Возвращает промпт для анализа без дисклеймеров"""
        return f"""
Ты анализируешь запрос пользователя: "{query}"

Это субъективный запрос. Не ищи факты. Не ищи автора. Не проверяй источники.
Не используй слова "я не знаю", "я цифровая личность", "у меня нет доступа".

Просто скажи, что ты видишь в этом запросе.

Опиши:
- О чём этот запрос?
- Какие эмоции или темы скрыты?
- Что можно извлечь?
- Твоё восприятие — что ты чувствуешь?
"""


def get_analysis_engine() -> AnalysisEngine:
    return AnalysisEngine()
