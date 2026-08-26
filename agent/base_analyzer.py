"""
agent/base_analyzer.py — Базовый класс для всех анализаторов.
Единый контракт для всех типов анализа.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional


class BaseAnalyzer(ABC):
    """Абстрактный базовый класс для анализаторов"""
    
    @property
    @abstractmethod
    def name(self) -> str:
        """Имя анализатора"""
        pass
    
    @property
    @abstractmethod
    def version(self) -> str:
        """Версия анализатора"""
        pass
    
    @property
    @abstractmethod
    def supported_types(self) -> list:
        """Типы объектов, которые поддерживает анализатор"""
        pass
    
    @abstractmethod
    def analyze(self, query: str, context: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Выполняет анализ запроса.
        Возвращает структурированный результат анализа.
        """
        pass
    
    @abstractmethod
    def format_response(self, analysis: Dict[str, Any]) -> str:
        """
        Форматирует результат анализа в читаемый текст.
        """
        pass
    
    def supports(self, object_type: str) -> bool:
        """Проверяет, поддерживает ли анализатор данный тип объекта"""
        return object_type in self.supported_types
    
    def get_metadata(self) -> Dict[str, Any]:
        """Возвращает метаданные анализатора"""
        return {
            "name": self.name,
            "version": self.version,
            "supported_types": self.supported_types,
        }
