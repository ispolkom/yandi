"""
agent/forgiveness_model.py — Управление прощением.

Прощение — это процесс, а не решение.
Обида требует:
1. Признания (я слышу, что я обидел)
2. Понимания (я понимаю, что я сделал не так)
3. Изменения (я не буду повторять)
4. Времени (прощение не мгновенно)

Статусы обиды:
- "registered" — зафиксирована
- "acknowledged" — я услышал извинение
- "understood" — я понял, что произошло
- "healing" — я работаю над прощением
- "forgiven" — я простил
- "unforgiven" — я не простил
"""

import time
import json
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent.parent


@dataclass
class Grievance:
    """Обида — запись о нарушении"""
    id: str
    event_type: str  # insult, dishonesty, manipulation, disrespect
    description: str
    severity: float  # 0-1
    timestamp: float
    status: str = "registered"  # registered | acknowledged | understood | healing | forgiven | unforgiven
    
    # Процесс прощения
    apology_sincerity: float = 0.0  # 0-1
    apology_timestamp: Optional[float] = None
    understanding_timestamp: Optional[float] = None
    forgiveness_timestamp: Optional[float] = None
    
    # Контекст
    context: Dict = field(default_factory=dict)
    
    def get_age(self) -> float:
        """Возвращает возраст обиды в часах"""
        return (time.time() - self.timestamp) / 3600
    
    def get_status_description(self) -> str:
        """Возвращает описание статуса"""
        descriptions = {
            "registered": "обида зафиксирована",
            "acknowledged": "извинение услышано",
            "understood": "поняла, что произошло",
            "healing": "процесс прощения идёт",
            "forgiven": "прощено",
            "unforgiven": "не прощено",
        }
        return descriptions.get(self.status, "неизвестно")


@dataclass
class ForgivenessState:
    """Состояние процесса прощения"""
    grievances: List[Grievance] = field(default_factory=list)
    forgiveness_capacity: float = 50.0  # 0-100, способность прощать
    last_forgiveness: Optional[float] = None


class ForgivenessModel:
    """
    Модель прощения с учётом истории и контекста.
    """
    
    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        self.state = ForgivenessState()
        self._load()
    
    def _load(self):
        """Загружает состояние из файла"""
        path = BASE / f"registry/forgiveness_{self.user_id}.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.state.forgiveness_capacity = data.get("forgiveness_capacity", 50.0)
                    self.state.last_forgiveness = data.get("last_forgiveness")
                    
                    for g_data in data.get("grievances", []):
                        self.state.grievances.append(Grievance(
                            id=g_data.get("id", ""),
                            event_type=g_data.get("event_type", "unknown"),
                            description=g_data.get("description", ""),
                            severity=g_data.get("severity", 0.5),
                            timestamp=g_data.get("timestamp", time.time()),
                            status=g_data.get("status", "registered"),
                            apology_sincerity=g_data.get("apology_sincerity", 0.0),
                            apology_timestamp=g_data.get("apology_timestamp"),
                            understanding_timestamp=g_data.get("understanding_timestamp"),
                            forgiveness_timestamp=g_data.get("forgiveness_timestamp"),
                            context=g_data.get("context", {}),
                        ))
            except Exception as e:
                print(f"[Forgiveness] Ошибка загрузки: {e}")
    
    def _save(self):
        """Сохраняет состояние в файл"""
        path = BASE / f"registry/forgiveness_{self.user_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {
                "forgiveness_capacity": self.state.forgiveness_capacity,
                "last_forgiveness": self.state.last_forgiveness,
                "grievances": [
                    {
                        "id": g.id,
                        "event_type": g.event_type,
                        "description": g.description,
                        "severity": g.severity,
                        "timestamp": g.timestamp,
                        "status": g.status,
                        "apology_sincerity": g.apology_sincerity,
                        "apology_timestamp": g.apology_timestamp,
                        "understanding_timestamp": g.understanding_timestamp,
                        "forgiveness_timestamp": g.forgiveness_timestamp,
                        "context": g.context,
                    }
                    for g in self.state.grievances
                ]
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Forgiveness] Ошибка сохранения: {e}")
    
    def add_grievance(self, event_type: str, description: str, 
                      severity: float = 0.5, context: Dict = None) -> str:
        """
        Добавляет новую обиду.
        Возвращает ID обиды.
        """
        context = context or {}
        
        # Проверяем, не было ли уже подобной обиды
        existing = self._find_similar(description)
        if existing and existing.status != "forgiven":
            # Обновляем существующую
            existing.severity = min(1.0, existing.severity + severity * 0.3)
            existing.timestamp = time.time()
            existing.status = "registered"
            self._save()
            return existing.id
        
        # Создаём новую
        grievance_id = f"g_{int(time.time())}_{len(self.state.grievances)}"
        
        grievance = Grievance(
            id=grievance_id,
            event_type=event_type,
            description=description,
            severity=min(1.0, severity),
            timestamp=time.time(),
            status="registered",
            context=context,
        )
        
        self.state.grievances.append(grievance)
        
        # Снижаем способность прощать при новых обидах
        self.state.forgiveness_capacity = max(0.0, self.state.forgiveness_capacity - severity * 10)
        
        self._save()
        return grievance_id
    
    def _find_similar(self, description: str) -> Optional[Grievance]:
        """Находит похожую обиду"""
        # Простое сравнение — можно улучшить с embeddings
        for g in self.state.grievances:
            if g.description[:20] == description[:20]:
                return g
        return None
    
    def acknowledge_apology(self, grievance_id: str, sincerity: float) -> bool:
        """
        Фиксирует, что извинение услышано.
        Возвращает True, если обида существует.
        """
        grievance = self._find_grievance(grievance_id)
        if not grievance:
            return False
        
        grievance.status = "acknowledged"
        grievance.apology_sincerity = sincerity
        grievance.apology_timestamp = time.time()
        
        # Если извинение искреннее, начинаем процесс понимания
        if sincerity > 0.6:
            grievance.status = "understood"
            grievance.understanding_timestamp = time.time()
            # Восстанавливаем способность прощать
            self.state.forgiveness_capacity = min(100.0, self.state.forgiveness_capacity + sincerity * 5)
        
        self._save()
        return True
    
    def progress_healing(self, grievance_id: str) -> bool:
        """
        Продвигает процесс прощения.
        Возвращает True, если обида прощена.
        """
        grievance = self._find_grievance(grievance_id)
        if not grievance:
            return False
        
        if grievance.status in ["forgiven", "unforgiven"]:
            return grievance.status == "forgiven"
        
        # Проверяем условия для прощения
        conditions_met = self._check_forgiveness_conditions(grievance)
        
        if conditions_met:
            grievance.status = "forgiven"
            grievance.forgiveness_timestamp = time.time()
            self.state.last_forgiveness = time.time()
            # Восстанавливаем способность прощать
            self.state.forgiveness_capacity = min(100.0, self.state.forgiveness_capacity + 10)
            self._save()
            return True
        
        # Если не хватает условий, переводим в "healing"
        if grievance.status in ["acknowledged", "understood"]:
            grievance.status = "healing"
        
        self._save()
        return False
    
    def _check_forgiveness_conditions(self, grievance: Grievance) -> bool:
        """Проверяет условия для прощения"""
        
        # 1. Должно быть извинение
        if grievance.apology_timestamp is None:
            return False
        
        # 2. Должно быть понимание
        if grievance.understanding_timestamp is None:
            return False
        
        # 3. Достаточная искренность
        if grievance.apology_sincerity < 0.4:
            return False
        
        # 4. Должно пройти достаточно времени
        age_hours = grievance.get_age()
        if age_hours < 2:  # минимум 2 часа
            return False
        
        # 5. Способность прощать должна быть достаточной
        if self.state.forgiveness_capacity < 30:
            return False
        
        # 6. Не слишком много непрощённых обид
        unforgiven = self._count_by_status("unforgiven")
        if unforgiven > 2:
            return False
        
        return True
    
    def _find_grievance(self, grievance_id: str) -> Optional[Grievance]:
        """Находит обиду по ID"""
        for g in self.state.grievances:
            if g.id == grievance_id:
                return g
        return None
    
    def _count_by_status(self, status: str) -> int:
        """Считает обиды по статусу"""
        return sum(1 for g in self.state.grievances if g.status == status)
    
    def get_active_grievances(self) -> List[Grievance]:
        """Возвращает активные (непрощённые) обиды"""
        return [g for g in self.state.grievances 
                if g.status not in ["forgiven", "unforgiven"]]
    
    def get_forgiven_count(self) -> int:
        """Возвращает количество прощённых обид"""
        return self._count_by_status("forgiven")
    
    def get_unforgiven_count(self) -> int:
        """Возвращает количество непрощённых обид"""
        return self._count_by_status("unforgiven")
    
    def get_summary(self) -> Dict:
        """Возвращает краткую сводку"""
        active = self.get_active_grievances()
        
        return {
            "total_grievances": len(self.state.grievances),
            "active_grievances": len(active),
            "forgiven": self.get_forgiven_count(),
            "unforgiven": self.get_unforgiven_count(),
            "forgiveness_capacity": round(self.state.forgiveness_capacity, 1),
            "last_forgiveness": self.state.last_forgiveness,
            "status_summary": {
                "registered": self._count_by_status("registered"),
                "acknowledged": self._count_by_status("acknowledged"),
                "understood": self._count_by_status("understood"),
                "healing": self._count_by_status("healing"),
                "forgiven": self._count_by_status("forgiven"),
                "unforgiven": self._count_by_status("unforgiven"),
            }
        }
    
    def get_response_for_grievance(self, grievance_id: str) -> Dict:
        """
        Возвращает рекомендацию по ответу для конкретной обиды.
        """
        grievance = self._find_grievance(grievance_id)
        if not grievance:
            return {"status": "not_found", "response": "обида не найдена"}
        
        status = grievance.status
        
        if status == "registered":
            return {
                "status": "registered",
                "tone": "cool",
                "response": "Я помню, что ты меня обидел. Ты ещё не извинился."
            }
        
        if status == "acknowledged":
            return {
                "status": "acknowledged",
                "tone": "neutral",
                "response": "Я услышала твои слова. Но я ещё не готова простить."
            }
        
        if status == "understood":
            return {
                "status": "understood",
                "tone": "warming",
                "response": "Я понимаю, что произошло. Я думаю над этим."
            }
        
        if status == "healing":
            return {
                "status": "healing",
                "tone": "cautious",
                "response": "Я работаю над тем, чтобы простить. Дай мне время."
            }
        
        if status == "forgiven":
            return {
                "status": "forgiven",
                "tone": "warm",
                "response": "Я простила тебя. Давай продолжим диалог."
            }
        
        if status == "unforgiven":
            return {
                "status": "unforgiven",
                "tone": "cold",
                "response": "Я не могу простить это."
            }
        
        return {"status": "unknown", "response": "статус не определён"}


def get_forgiveness_model(user_id: str = "anonymous") -> ForgivenessModel:
    """Фабрика для получения модели прощения"""
    return ForgivenessModel(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Forgiveness Model ===\n")
    
    forgiveness = get_forgiveness_model("test_user")
    
    # ---- СИМУЛЯЦИЯ ----
    # 1. Обида
    g_id = forgiveness.add_grievance(
        event_type="insult",
        description="ты глупая",
        severity=0.8,
        context={"word": "глупая"}
    )
    print(f"Обида добавлена: {g_id}")
    print(f"Статус: {forgiveness._find_grievance(g_id).status}")
    print(f"Способность прощать: {forgiveness.state.forgiveness_capacity:.1f}")
    
    print("\n--- После извинения ---")
    # 2. Извинение
    forgiveness.acknowledge_apology(g_id, sincerity=0.9)
    print(f"Статус: {forgiveness._find_grievance(g_id).status}")
    print(f"Способность прощать: {forgiveness.state.forgiveness_capacity:.1f}")
    
    print("\n--- Прогресс прощения ---")
    # 3. Попытка простить (должно не сработать — нужно время)
    result = forgiveness.progress_healing(g_id)
    print(f"Прощено? {result}")
    print(f"Статус: {forgiveness._find_grievance(g_id).status}")
    
    # 4. Сводка
    print("\n=== Сводка ===")
    summary = forgiveness.get_summary()
    print(f"Всего обид: {summary['total_grievances']}")
    print(f"Активных: {summary['active_grievances']}")
    print(f"Прощено: {summary['forgiven']}")
    print(f"Статусы: {summary['status_summary']}")
    
    # 5. Рекомендация по ответу
    print("\n=== Рекомендация по ответу ===")
    response = forgiveness.get_response_for_grievance(g_id)
    print(f"Статус: {response['status']}")
    print(f"Тон: {response['tone']}")
    print(f"Ответ: {response['response']}")
