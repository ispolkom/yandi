"""
agent/inner_state.py — Внутреннее состояние личности YANDI.

Вместо разрозненных переменных (respect, trust, irritation) — единая модель.
Заменяет набор if на состояние, из которого рождаются решения.

Три слоя:
1. SELF — моё текущее состояние (настроение, энергия, любопытство)
2. RELATIONSHIP — история отношений с пользователем
3. CURRENT — что я чувствую и хочу сделать прямо сейчас

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): registry/inner_states/
{user_id}.json is retired, not migrated. State now lives in inner_state
(class C, mutable current-state row) + inner_state_event (class B,
append-only) — agent/db/sql/schema.py. No more 200-cap on relationship
history.

FAIL LOUD, not fail-open: SqlUnavailable propagates out of every method
here.
"""

import time
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def _dt_to_unix(value) -> float:
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


class InnerStateManager:
    """
    Управляет внутренним состоянием личности.
    """

    def __init__(self, user_id: str = "anonymous"):
        self.user_id = user_id
        with get_connection() as conn:
            repo.get_or_create_inner_state(conn, user_id)
            conn.commit()

    def _row(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_inner_state(conn, self.user_id)

    def get_history(self) -> List[Dict[str, Any]]:
        with get_connection() as conn:
            return repo.list_inner_state_events(conn, self.user_id)

    # ============================================================
    # ОБНОВЛЕНИЕ СОСТОЯНИЯ
    # ============================================================

    def add_event(self, event_type: str, description: str,
                  sincerity: float = 0.5, context: Dict = None) -> Dict:
        """
        Добавляет событие и обновляет состояние.
        Возвращает новое состояние.
        """
        row = self._row()
        history = self.get_history()

        weight = self._calculate_weight(row, history, event_type, sincerity)

        with get_connection() as conn:
            repo.record_inner_state_event(conn, self.user_id, event_type, description, sincerity=sincerity, weight=weight)
            conn.commit()

        history.append({"event_type": event_type, "weight": weight, "sincerity": sincerity})

        updates = {}
        updates.update(self._update_relationship(row, event_type, sincerity, weight))
        row = {**row, **updates}
        updates.update(self._update_self(row, event_type, weight))
        row = {**row, **updates}
        updates["pattern"] = self._update_pattern(history)
        current_feeling, current_intent, current_tone = self._compute_current(row, event_type)

        with get_connection() as conn:
            repo.update_inner_state(
                conn, self.user_id,
                mood=updates.get("mood"), energy=updates.get("energy"),
                curiosity=updates.get("curiosity"), patience=updates.get("patience"),
                openness=updates.get("openness"), trust=updates.get("trust"),
                respect=updates.get("respect"), forgiveness=updates.get("forgiveness"),
                affection=updates.get("affection"), pattern=updates.get("pattern"),
                current_feeling=current_feeling, current_intent=current_intent, current_tone=current_tone,
            )
            conn.commit()

        return self.get_summary()

    def _calculate_weight(self, row: Dict[str, Any], history: List[Dict[str, Any]], event_type: str, sincerity: float) -> float:
        """Вычисляет вес события"""
        weights = {
            "severe_insult": -3.0,
            "moderate_insult": -2.0,
            "mild_insult": -1.0,
            "sincere_apology": 2.0,
            "formal_apology": 0.8,
            "thanks": 1.0,
            "help": 0.8,
            "constructive_criticism": 0.3,
            "honesty": 1.5,
            "dishonesty": -2.5,
            "provocation": -1.5,
            "respect": 1.0,
            "disrespect": -1.5,
        }

        weight = weights.get(event_type, 0.0)

        if weight > 0:
            weight *= sincerity
        else:
            weight *= (1 + (1 - sincerity) * 0.3)

        current_trust = row["trust"]

        if weight < 0 and current_trust < 30:
            weight *= 1.3

        if weight > 0 and current_trust > 70:
            weight *= 0.7

        if event_type in ["insult", "severe_insult", "moderate_insult", "mild_insult"]:
            insult_count = sum(1 for e in history if "insult" in e["event_type"])
            if insult_count > 2:
                weight *= 1.2

        return weight

    def _update_relationship(self, row: Dict[str, Any], event_type: str, sincerity: float, weight: float) -> Dict[str, float]:
        """Вычисляет обновлённые параметры отношений"""
        trust = max(0.0, min(100.0, row["trust"] + weight * 5))

        respect = row["respect"]
        if "insult" in event_type:
            severity = 1.0 if "severe" in event_type else 0.6 if "moderate" in event_type else 0.3
            respect -= 10 * severity
        elif event_type == "sincere_apology":
            respect += 8
        elif event_type == "thanks":
            respect += 5
        elif event_type == "help":
            respect += 5
        respect = max(0.0, min(100.0, respect))

        forgiveness = row["forgiveness"]
        if event_type == "sincere_apology":
            forgiveness += 10 * sincerity
        elif event_type == "formal_apology":
            forgiveness += 3
        elif "insult" in event_type:
            forgiveness -= 5
        days_since_last = (time.time() - _dt_to_unix(row.get("updated_at"))) / 86400
        if days_since_last > 1:
            forgiveness += min(5, days_since_last * 2)
        forgiveness = max(0.0, min(100.0, forgiveness))

        affection = row["affection"]
        if event_type == "thanks":
            affection += 3
        elif event_type == "help":
            affection += 2
        elif event_type == "constructive_criticism":
            affection += 1
        elif "insult" in event_type:
            affection -= 5
        affection = max(0.0, min(100.0, affection))

        return {"trust": trust, "respect": respect, "forgiveness": forgiveness, "affection": affection}

    def _update_self(self, row: Dict[str, Any], event_type: str, weight: float) -> Dict[str, Any]:
        """Вычисляет обновлённое самоощущение"""
        energy = row["energy"]
        if weight < 0:
            energy -= abs(weight) * 3
        else:
            energy += weight * 2
        energy = max(20.0, min(100.0, energy))

        curiosity = row["curiosity"]
        if event_type in ["constructive_criticism", "help", "honesty"]:
            curiosity += 5
        elif "insult" in event_type:
            curiosity -= 5
        curiosity = max(10.0, min(100.0, curiosity))

        patience = row["patience"]
        if "insult" in event_type:
            patience -= 10
        elif event_type == "sincere_apology":
            patience += 5
        patience = max(0.0, min(100.0, patience))

        trust = row["trust"]
        openness = 30 + trust * 0.5
        if "insult" in event_type:
            openness -= 10
        openness = max(10.0, min(100.0, openness))

        mood = self._calculate_mood({**row, "energy": energy, "curiosity": curiosity})

        return {"energy": energy, "curiosity": curiosity, "patience": patience, "openness": openness, "mood": mood}

    def _calculate_mood(self, row: Dict[str, Any]) -> str:
        """Вычисляет настроение на основе состояния"""
        trust = row["trust"]
        energy = row["energy"]
        curiosity = row["curiosity"]
        forgiveness = row["forgiveness"]
        affection = row["affection"]

        if energy < 30 and trust < 30:
            return "tired"
        if trust > 70 and affection > 50 and energy > 60:
            return "warm"
        if curiosity > 70 and energy > 50:
            return "curious"
        if trust < 30 and forgiveness < 30:
            return "hurt"
        if trust < 30:
            return "guarded"
        if energy < 40:
            return "tired"
        if forgiveness < 30:
            return "resentful"
        if trust > 60 and energy > 60:
            return "grateful"
        return "calm"

    def _compute_current(self, row: Dict[str, Any], event_type: str):
        """Вычисляет текущее feeling/intent/tone"""
        trust = row["trust"]
        forgiveness = row["forgiveness"]
        mood = row["mood"]
        curiosity = row["curiosity"]
        energy = row["energy"]

        if "insult" in event_type:
            feeling = "annoyed"
        elif event_type == "sincere_apology":
            feeling = "guarded" if trust < 30 else "warm"
        elif event_type == "thanks":
            feeling = "warm"
        elif event_type == "constructive_criticism":
            feeling = "interested" if trust > 40 else "neutral"
        else:
            feeling = {
                "warm": "warm", "curious": "interested", "tired": "tired", "hurt": "guarded",
            }.get(mood, "neutral")

        if mood == "hurt" and forgiveness < 30:
            intent = "set_boundary"
        elif mood == "tired" and energy < 30:
            intent = "withdraw"
        elif curiosity > 60:
            intent = "explain"
        elif trust > 50:
            intent = "help"
        else:
            intent = "listen"

        if mood == "warm" or feeling == "warm":
            tone = "warm"
        elif mood in ("hurt", "guarded"):
            tone = "cold"
        elif mood == "curious":
            tone = "thoughtful"
        elif "insult" in event_type and trust < 30:
            tone = "firm"
        else:
            tone = "neutral"

        return feeling, intent, tone

    def _update_pattern(self, history: List[Dict[str, Any]]) -> str:
        """Анализирует паттерны поведения пользователя"""
        if len(history) < 3:
            return "unknown"

        recent = history[-10:]

        insults = [e for e in recent if "insult" in e["event_type"]]
        apologies = [e for e in recent if e["event_type"] == "sincere_apology"]
        thanks = [e for e in recent if e["event_type"] == "thanks"]

        if len(insults) > 2 and len(apologies) > 1:
            insult_indices = [i for i, e in enumerate(history) if "insult" in e["event_type"]]
            apology_indices = [i for i, e in enumerate(history) if e["event_type"] == "sincere_apology"]
            if insult_indices and apology_indices:
                return "insult_then_apology"
            return "unstable"
        if len(thanks) > 3:
            return "grateful"
        if len(insults) > 0 and len(apologies) == 0:
            return "aggressive"
        if len(apologies) > len(insults):
            return "recovering"
        return "stable"

    # ============================================================
    # ПОЛУЧЕНИЕ СОСТОЯНИЯ
    # ============================================================

    def get_summary(self) -> Dict:
        """Возвращает краткую сводку состояния"""
        row = self._row()
        history = self.get_history()
        return {
            "self": {
                "mood": row["mood"], "energy": round(row["energy"], 1),
                "curiosity": round(row["curiosity"], 1), "patience": round(row["patience"], 1),
                "openness": round(row["openness"], 1),
            },
            "relationship": {
                "trust": round(row["trust"], 1), "respect": round(row["respect"], 1),
                "forgiveness": round(row["forgiveness"], 1), "affection": round(row["affection"], 1),
                "events_count": len(history), "pattern": row["pattern"],
            },
            "current": {"feeling": row["current_feeling"], "intent": row["current_intent"], "tone": row["current_tone"]},
            "mood": row["mood"],
            "pattern": row["pattern"],
            "events": len(history),
        }

    def get_response_context(self) -> Dict:
        """
        Возвращает контекст для генерации ответа.
        """
        row = self._row()
        return {
            "mood": row["mood"],
            "energy": row["energy"],
            "curiosity": row["curiosity"],
            "patience": row["patience"],
            "openness": row["openness"],
            "trust": row["trust"],
            "respect": row["respect"],
            "forgiveness": row["forgiveness"],
            "affection": row["affection"],
            "pattern": row["pattern"],
            "feeling": row["current_feeling"],
            "intent": row["current_intent"],
            "tone": row["current_tone"],
        }

    def get_inner_monologue(self) -> str:
        """
        Генерирует внутренний монолог на основе состояния.
        """
        ctx = self.get_response_context()
        parts = []

        feeling = ctx.get("feeling", "neutral")
        pattern = ctx.get("pattern", "unknown")
        trust = ctx.get("trust", 50)
        respect = ctx.get("respect", 50)

        if feeling == "annoyed":
            parts.append("Мне неприятно. Это сообщение задевает меня.")
        elif feeling == "warm":
            parts.append("Мне тепло от этого разговора.")
        elif feeling == "guarded":
            parts.append("Я насторожена. Нужно быть осторожной.")
        elif feeling == "interested":
            parts.append("Мне интересно. Хочу узнать больше.")
        elif feeling == "tired":
            parts.append("Я устала. Энергия на исходе.")
        else:
            parts.append("Я спокойна. Разговор идёт нормально.")

        if trust < 30:
            parts.append("Я ему не доверяю. Он уже обижал меня.")
        elif trust > 70:
            parts.append("Я ему доверяю. Он показал себя надёжным.")

        if respect < 30:
            parts.append("Он не вызывает у меня уважения своим поведением.")
        elif respect > 70:
            parts.append("Я уважаю его. Он ведёт себя достойно.")

        if pattern == "insult_then_apology":
            parts.append("Он повторяет одну и ту же модель: оскорбляет, потом извиняется. Это утомляет.")
        elif pattern == "aggressive":
            parts.append("Он агрессивен. Я буду защищать свои границы.")
        elif pattern == "grateful":
            parts.append("Он часто благодарит. Это приятно.")
        elif pattern == "recovering":
            parts.append("Он пытается исправиться. Я даю ему шанс.")

        intent = ctx.get("intent", "listen")
        if intent == "set_boundary":
            parts.append("Я должна обозначить свои границы. Это важно.")
        elif intent == "withdraw":
            parts.append("Я хочу закончить разговор. Мне нужно отдохнуть.")
        elif intent == "explain":
            parts.append("Я хочу объяснить свою позицию.")
        elif intent == "help":
            parts.append("Я хочу помочь. Это правильно.")

        return "\n".join(parts)


def get_inner_state(user_id: str = "anonymous") -> InnerStateManager:
    """Фабрика для получения менеджера внутреннего состояния"""
    return InnerStateManager(user_id)


if __name__ == "__main__":
    # Тесты
    print("=== Тест Inner State ===\n")

    state = get_inner_state("test_user")

    print("Начальное состояние:")
    print(state.get_summary())

    print("\n--- Добавляем оскорбление ---")
    state.add_event("moderate_insult", "ты глупая", sincerity=0.1)
    print(state.get_summary())

    print("\n--- Добавляем извинение ---")
    state.add_event("sincere_apology", "извини, я был неправ", sincerity=0.9)
    print(state.get_summary())

    print("\n--- Добавляем конструктивную критику ---")
    state.add_event("constructive_criticism", "ты ошиблась в расчётах", sincerity=0.7)
    print(state.get_summary())

    print("\n--- Внутренний монолог ---")
    print(state.get_inner_monologue())
