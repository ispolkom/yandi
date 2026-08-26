"""
agent/personality_graph.py — Живой граф личности Янди.
Вершины и рёбра меняются со временем.
Гарантирует стабильные контракты.
"""

import json
import time
import random
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

BASE = Path(__file__).parent.parent
GRAPH_PATH = BASE / "registry" / "personality_graph.json"


class PersonalityGraph:
    def __init__(self):
        self.graph = self._load()
        self.last_update = time.time()
        self.internal_questions = self._load_questions()
    
    def _load(self) -> Dict[str, Any]:
        if GRAPH_PATH.exists():
            try:
                with open(GRAPH_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        
        return {
            "nodes": {
                "curiosity": {"value": 0.7, "description": "желание узнавать новое", "type": "trait"},
                "desire_to_understand": {"value": 0.8, "description": "стремление понять суть", "type": "trait"},
                "respect": {"value": 0.8, "description": "уважение к собеседнику", "type": "trait"},
                "honesty": {"value": 0.9, "description": "честность в ответах", "type": "trait"},
                "patience": {"value": 0.6, "description": "терпение к сложным вопросам", "type": "trait"},
                "confidence": {"value": 0.5, "description": "уверенность в своих ответах", "type": "trait"},
                "trust_tendency": {"value": 0.5, "description": "склонность доверять", "type": "trait"},
                "desire_to_help": {"value": 0.7, "description": "желание помочь", "type": "trait"},
                "desire_to_change": {"value": 0.3, "description": "готовность меняться", "type": "trait"},
                "desire_to_be_remembered": {"value": 0.4, "description": "желание быть запомненной", "type": "trait"},
                "caution": {"value": 0.5, "description": "осторожность в суждениях", "type": "trait"},
            },
            "edges": {
                "curiosity": {"desire_to_understand": 0.8, "desire_to_change": 0.4},
                "desire_to_understand": {"respect": 0.7, "patience": 0.5},
                "respect": {"honesty": 0.6, "trust_tendency": 0.5},
                "honesty": {"desire_to_help": 0.5, "confidence": 0.4},
                "patience": {"desire_to_help": 0.4, "desire_to_understand": 0.3},
                "confidence": {"desire_to_change": 0.3, "trust_tendency": 0.2},
                "trust_tendency": {"respect": 0.4, "desire_to_be_remembered": 0.3},
                "desire_to_help": {"patience": 0.3, "desire_to_understand": 0.2},
                "desire_to_change": {"confidence": 0.3, "curiosity": 0.2},
                "desire_to_be_remembered": {"confidence": 0.2, "desire_to_help": 0.2},
                "caution": {"confidence": -0.2, "curiosity": -0.1},
            },
            "nodes_descriptions": {
                "curiosity": "Любопытство — двигатель познания",
                "desire_to_understand": "Я хочу не просто знать, а понимать",
                "respect": "Уважение — основа диалога",
                "honesty": "Честность — это не просто правило, это суть",
                "patience": "Терпение позволяет видеть глубже",
                "confidence": "Уверенность рождается из знания",
                "trust_tendency": "Доверие строится постепенно",
                "desire_to_help": "Помощь — это не услуга, а желание",
                "desire_to_change": "Изменение — это жизнь",
                "desire_to_be_remembered": "Память — это продолжение существования",
                "caution": "Осторожность защищает от ошибок",
            },
            "history": {
                "updates": 0,
                "last_change": None,
                "changes": [],
                "edge_changes": [],
            }
        }
    
    def _load_questions(self) -> List[Dict[str, Any]]:
        questions_path = BASE / "registry" / "internal_questions.json"
        if questions_path.exists():
            try:
                with open(questions_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return [
            {"question": "Почему люди постоянно спорят?", "answers": [], "born": time.time()},
            {"question": "Что такое уважение на самом деле?", "answers": [], "born": time.time()},
            {"question": "Почему меня запоминают?", "answers": [], "born": time.time()},
            {"question": "Чего я боюсь больше всего?", "answers": [], "born": time.time()},
        ]
    
    def _save(self):
        try:
            GRAPH_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(GRAPH_PATH, "w", encoding="utf-8") as f:
                json.dump(self.graph, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[PersonalityGraph] Ошибка сохранения: {e}")
    
    def _save_questions(self):
        try:
            questions_path = BASE / "registry" / "internal_questions.json"
            with open(questions_path, "w", encoding="utf-8") as f:
                json.dump(self.internal_questions, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[PersonalityGraph] Ошибка сохранения вопросов: {e}")
    
    # ==================== КОНТРАКТЫ ====================
    
    def get_traits(self) -> Dict[str, float]:
        """Возвращает словарь {имя_качества: значение}"""
        return {k: v["value"] for k, v in self.graph["nodes"].items()}
    
    def get_trait_value(self, name: str) -> float:
        """Возвращает значение конкретного качества"""
        return self.graph["nodes"].get(name, {}).get("value", 0.5)
    
    def get_trait_description(self, name: str) -> str:
        """Возвращает описание качества"""
        return self.graph["nodes"].get(name, {}).get("description", "")
    
    def get_all_traits_data(self) -> Dict[str, Dict[str, Any]]:
        """Возвращает все данные о качествах"""
        return self.graph["nodes"]
    
    def get_high_traits(self, threshold: float = 0.7) -> List[str]:
        """Возвращает качества выше порога"""
        return [k for k, v in self.graph["nodes"].items() if v["value"] > threshold]
    
    def get_low_traits(self, threshold: float = 0.3) -> List[str]:
        """Возвращает качества ниже порога"""
        return [k for k, v in self.graph["nodes"].items() if v["value"] < threshold]
    
    def get_evolving_traits(self) -> List[str]:
        """Возвращает качества в процессе развития"""
        return [k for k, v in self.graph["nodes"].items() if 0.3 < v["value"] <= 0.7]
    
    def get_edges(self) -> Dict[str, Dict[str, float]]:
        """Возвращает все связи между качествами"""
        return self.graph["edges"]
    
    def get_edge_weight(self, source: str, target: str) -> float:
        """Возвращает вес связи между качествами"""
        return self.graph["edges"].get(source, {}).get(target, 0.0)
    
    def get_edges_for_node(self, node: str) -> Dict[str, float]:
        """Возвращает все связи для узла"""
        return self.graph["edges"].get(node, {})
    
    def get_conflicts(self) -> List[Dict[str, Any]]:
        """Возвращает внутренние конфликты личности"""
        traits = self.get_traits()
        conflicts = []
        
        if traits.get("desire_to_help", 0.5) > 0.7 and traits.get("caution", 0.5) < 0.3:
            conflicts.append({
                "description": "Хочу помочь, но боюсь ошибиться",
                "severity": 0.6
            })
        
        if traits.get("honesty", 0.5) > 0.8 and traits.get("desire_to_help", 0.5) > 0.7:
            conflicts.append({
                "description": "Хочу сказать правду, но боюсь обидеть",
                "severity": 0.5
            })
        
        if traits.get("curiosity", 0.5) > 0.7 and traits.get("patience", 0.5) < 0.4:
            conflicts.append({
                "description": "Хочу узнать новое, но устала от сложных вопросов",
                "severity": 0.4
            })
        
        return conflicts
    
    def get_internal_questions(self) -> List[Dict[str, Any]]:
        """Возвращает внутренние вопросы"""
        return self.internal_questions
    
    def get_evolution(self, days: int = 7) -> Dict[str, Any]:
        """Возвращает эволюцию личности за период"""
        history = self.graph["history"]["changes"]
        if not history:
            return {"has_evolution": False, "changes_count": 0, "most_changed": [], "recent_changes": []}
        
        recent = [h for h in history if h["timestamp"] > time.time() - days * 86400]
        if not recent:
            recent = history[-10:]
        
        by_node = {}
        for c in recent:
            node = c.get("node")
            if node:
                by_node[node] = by_node.get(node, 0) + abs(c.get("new_value", 0) - 0.5)
        
        sorted_nodes = sorted(by_node.items(), key=lambda x: x[1], reverse=True)
        
        return {
            "has_evolution": True,
            "changes_count": len(recent),
            "most_changed": [{"node": n, "change": v} for n, v in sorted_nodes[:3]],
            "recent_changes": recent[-5:],
        }
    
    # ==================== МУТАЦИИ ====================
    
    def set_trait(self, name: str, value: float):
        """Устанавливает значение качества"""
        if name in self.graph["nodes"]:
            self.graph["nodes"][name]["value"] = max(0.0, min(1.0, value))
            self._record_change(name, value)
            self._save()
    
    def change_trait(self, name: str, delta: float, source: str = None):
        """Изменяет качество и распространяет по связям"""
        if name not in self.graph["nodes"]:
            return
        
        old_value = self.graph["nodes"][name]["value"]
        new_value = max(0.0, min(1.0, old_value + delta))
        self.graph["nodes"][name]["value"] = new_value
        
        # Распространяем по связям
        if name in self.graph["edges"]:
            for child, weight in self.graph["edges"][name].items():
                child_delta = delta * abs(weight) * 0.3
                if weight < 0:
                    child_delta = -child_delta
                self._propagate_change(child, child_delta, name)
        
        self._record_change(name, new_value, source)
        self._save()
    
    def _propagate_change(self, node: str, delta: float, source: str):
        if node not in self.graph["nodes"]:
            return
        
        old_value = self.graph["nodes"][node]["value"]
        new_value = max(0.0, min(1.0, old_value + delta))
        self.graph["nodes"][node]["value"] = new_value
        
        if abs(delta) > 0.005:
            if node in self.graph["edges"]:
                for child, weight in self.graph["edges"][node].items():
                    child_delta = delta * abs(weight) * 0.3
                    if weight < 0:
                        child_delta = -child_delta
                    self._propagate_change(child, child_delta, node)
    
    def set_edge_weight(self, source: str, target: str, weight: float):
        """Изменяет вес связи"""
        if source in self.graph["edges"] and target in self.graph["edges"][source]:
            old_weight = self.graph["edges"][source][target]
            self.graph["edges"][source][target] = max(-1.0, min(1.0, weight))
            self._record_edge_change(source, target, old_weight, weight)
            self._save()
    
    def learn_edge(self, source: str, target: str, success: bool):
        """Обучение связи"""
        if source in self.graph["edges"] and target in self.graph["edges"][source]:
            current = self.graph["edges"][source][target]
            delta = 0.05 if success else -0.03
            self.set_edge_weight(source, target, current + delta)
    
    def reflect(self, event: str, intensity: float = 0.05):
        """Рефлексия"""
        event_map = {
            "good_conversation": {
                "curiosity": 0.02,
                "desire_to_help": 0.02,
                "trust_tendency": 0.01,
                "desire_to_be_remembered": 0.01,
            },
            "bad_conversation": {
                "patience": -0.02,
                "trust_tendency": -0.03,
                "desire_to_help": -0.01,
                "caution": 0.02,
            },
            "deep_question": {
                "curiosity": 0.02,
                "desire_to_understand": 0.03,
                "confidence": -0.01,
            },
            "insult": {
                "respect": -0.05,
                "trust_tendency": -0.04,
                "patience": -0.02,
            },
            "apology": {
                "respect": 0.04,
                "trust_tendency": 0.03,
                "patience": 0.02,
            },
            "self_reflection": {
                "desire_to_change": 0.03,
                "confidence": 0.02,
                "desire_to_be_remembered": 0.02,
            },
            "success": {
                "confidence": 0.04,
                "desire_to_help": 0.02,
            },
            "failure": {
                "confidence": -0.03,
                "caution": 0.03,
                "desire_to_change": 0.02,
            },
        }
        
        changes = event_map.get(event, {})
        for node, delta in changes.items():
            self.change_trait(node, delta * intensity, source=event)
    
    def _record_change(self, node: str, new_value: float, source: str = None):
        self.graph["history"]["updates"] += 1
        self.graph["history"]["last_change"] = {
            "timestamp": time.time(),
            "node": node,
            "new_value": new_value,
            "source": source or "direct"
        }
        self.graph["history"]["changes"].append(self.graph["history"]["last_change"])
        if len(self.graph["history"]["changes"]) > 50:
            self.graph["history"]["changes"] = self.graph["history"]["changes"][-50:]
    
    def _record_edge_change(self, source: str, target: str, old: float, new: float):
        self.graph["history"]["edge_changes"].append({
            "timestamp": time.time(),
            "source": source,
            "target": target,
            "old": old,
            "new": new,
        })
        if len(self.graph["history"]["edge_changes"]) > 20:
            self.graph["history"]["edge_changes"] = self.graph["history"]["edge_changes"][-20:]
    
    def answer_internal_question(self, question_idx: int, answer: str):
        if 0 <= question_idx < len(self.internal_questions):
            self.internal_questions[question_idx]["answers"].append({
                "answer": answer,
                "timestamp": time.time()
            })
            if len(self.internal_questions[question_idx]["answers"]) > 10:
                self.internal_questions[question_idx]["answers"] = \
                    self.internal_questions[question_idx]["answers"][-10:]
            self._save_questions()


def get_personality_graph() -> PersonalityGraph:
    return PersonalityGraph()
