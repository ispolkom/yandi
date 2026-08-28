"""
agent/belief_manager.py — Управление убеждениями для YANDI V7.

Хранит убеждения с:
- уверенностью (confidence) с Bayesian обновлением
- доказательствами ЗА (evidence_for)
- доказательствами ПРОТИВ (evidence_against)
- противоречиями (contradiction_score)
- историей изменений
- автоматическим затуханием (decay)

Цель: система может менять мнение при появлении новых данных,
используя математически обоснованное обновление убеждений.
"""

from __future__ import annotations

import sys
import math
from pathlib import Path
BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from agent.db.sql.shadow_write import shadow_record_belief_assessment


@dataclass
class Belief:
    """Убеждение системы о мире с Bayesian поддержкой."""
    id: str
    topic: str
    statement: str
    confidence: float  # 0..1 (апостериорная вероятность)
    evidence_for: List[str]  # ссылки на evidence, которые ПОДДЕРЖИВАЮТ
    evidence_against: List[str]  # ссылки на evidence, которые ПРОТИВОРЕЧАТ
    claim_ids: List[str]
    created_at: float
    updated_at: float
    history: List[Dict[str, Any]]
    status: str = "active"  # active | revised | rejected | superseded
    
    # Bayesian параметры
    prior: float = 0.5  # априорная вероятность
    likelihood: float = 0.5  # правдоподобие
    contradiction_score: float = 0.0  # 0..1, насколько убеждение противоречиво
    decay_factor: float = 0.95  # затухание уверенности со временем
    superseded_by: Optional[str] = None  # id убеждения, которое заменило это


class BeliefManager:
    """
    Управление убеждениями с Bayesian обновлением.
    """
    
    def __init__(self, storage_file: Optional[Path] = None):
        self.storage_file = storage_file or BASE / "registry" / "beliefs.json"
        self.beliefs: List[Belief] = []
        self._load()
        self._apply_decay()  # применяем затухание при загрузке
    
    def _load(self):
        if self.storage_file.exists():
            try:
                with open(self.storage_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.beliefs = [Belief(**b) for b in data]
            except Exception as e:
                print(f"[belief_manager] Ошибка загрузки: {e}")
    
    def _save(self):
        try:
            self.storage_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.storage_file, 'w', encoding='utf-8') as f:
                json.dump([b.__dict__ for b in self.beliefs], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[belief_manager] Ошибка сохранения: {e}")
    
    def _apply_decay(self):
        """Применить затухание уверенности со временем."""
        now = time.time()
        for belief in self.beliefs:
            if belief.status == "active":
                age_days = (now - belief.updated_at) / 86400
                if age_days > 1:
                    decay = belief.decay_factor ** age_days
                    old_conf = belief.confidence
                    belief.confidence = belief.confidence * decay
                    belief.history.append({
                        "timestamp": now,
                        "old_confidence": old_conf,
                        "new_confidence": belief.confidence,
                        "reason": f"decay: {age_days:.1f} days",
                        "change": "decayed",
                    })
                    belief.updated_at = now
                    shadow_record_belief_assessment(
                        belief_id=belief.id, topic=belief.topic, statement=belief.statement,
                        confidence=belief.confidence, status=belief.status,
                        change_type="decayed", old_confidence=old_conf, new_confidence=belief.confidence,
                        reason=f"decay: {age_days:.1f} days",
                    )
        self._save()
    
    def _bayesian_update(self, belief: Belief, new_evidence: float, is_supporting: bool) -> float:
        """
        Bayesian обновление уверенности.
        
        Args:
            belief: текущее убеждение
            new_evidence: 0..1, сила нового свидетельства
            is_supporting: поддерживает ли новое свидетельство убеждение
        
        Returns:
            новая уверенность (апостериорная вероятность)
        """
        prior = belief.confidence
        likelihood = new_evidence
        
        if is_supporting:
            # P(E|H) = likelihood
            # P(E|¬H) = 1 - likelihood
            posterior = (prior * likelihood) / (prior * likelihood + (1 - prior) * (1 - likelihood))
        else:
            # Контраргумент: P(E|¬H) = likelihood
            # P(E|H) = 1 - likelihood
            posterior = (prior * (1 - likelihood)) / (prior * (1 - likelihood) + (1 - prior) * likelihood)
        
        # Ограничиваем и добавляем сглаживание
        posterior = max(0.01, min(0.99, posterior))
        return posterior
    
    def _calculate_contradiction_score(self, belief: Belief) -> float:
        """Рассчитать противоречивость убеждения."""
        if not belief.evidence_for and not belief.evidence_against:
            return 0.0
        
        total = len(belief.evidence_for) + len(belief.evidence_against)
        if total == 0:
            return 0.0
        
        # Чем больше противоречивых evidence, тем выше противоречивость
        ratio = len(belief.evidence_against) / total
        return min(1.0, ratio * 2)  # умножаем на 2, чтобы усилить эффект
    
    def add_belief(
        self,
        topic: str,
        statement: str,
        confidence: float,
        evidence_for: List[str] = None,
        evidence_against: List[str] = None,
        claim_ids: List[str] = None,
        prior: float = 0.5,
    ) -> Belief:
        """Добавить новое убеждение."""
        existing = self._find_similar(topic, statement)
        if existing:
            return self._update_existing(existing, confidence, evidence_for, evidence_against)
        
        belief = Belief(
            id=f"bel_{uuid.uuid4().hex[:8]}",
            topic=topic,
            statement=statement,
            confidence=confidence,
            evidence_for=evidence_for or [],
            evidence_against=evidence_against or [],
            claim_ids=claim_ids or [],
            created_at=time.time(),
            updated_at=time.time(),
            history=[{
                "timestamp": time.time(),
                "old_confidence": 0.0,
                "new_confidence": confidence,
                "reason": "initial",
                "change": "created",
            }],
            status="active",
            prior=prior,
            likelihood=confidence,
            contradiction_score=0.0,
        )
        self.beliefs.append(belief)
        self._save()
        shadow_record_belief_assessment(
            belief_id=belief.id, topic=belief.topic, statement=belief.statement,
            confidence=belief.confidence, status=belief.status,
            change_type="created", old_confidence=0.0, new_confidence=confidence,
            reason="initial",
        )
        return belief
    
    def _find_similar(self, topic: str, statement: str) -> Optional[Belief]:
        """
        Найти существующее убеждение, эквивалентное новому statement.

        P0 (performance architecture pass): раньше _is_similar_statement()
        делал СВОИ 2 embed HTTP-вызова на КАЖДОЕ сравнение — включая
        повторное re-embed одного и того же нового `statement` на
        каждой итерации. При 108 активных beliefs одной темы (реальное
        число в registry на момент фикса) один add_belief() для
        действительно нового утверждения мог стоить 200+ HTTP round-trips
        (наблюдалось ~27s/кандидат в живом прогоне).

        Теперь: сначала быстрый exact-match проход по всем кандидатам
        (без единого HTTP-вызова — как и раньше, эта проверка не стоила
        сети). Только если exact match не найден, делается ОДИН
        batch-embed вызов (statement + все оставшиеся кандидаты этой
        темы), и по кандидатам в ТОМ ЖЕ порядке — threshold-gated LLM
        judge, первый "equivalent" выигрывает. Критерии решения не
        изменились — изменился только способ получения embedding (один
        batch-запрос вместо N избыточных), и лишний embed-вызов больше
        не тратится впустую, когда дубликат находится по exact match.
        """
        import numpy as np

        candidates = [
            belief
            for belief in self.beliefs
            if belief.status in ["active", "revised"] and belief.topic == topic
        ]

        if not candidates or not statement:
            return None

        statement_norm = " ".join(statement.lower().split())

        for belief in candidates:
            belief_norm = " ".join((belief.statement or "").lower().split())
            if belief_norm == statement_norm:
                return belief

        vectors = self._embed_batch([statement] + [c.statement for c in candidates])

        if vectors is None:
            # Fail-safe (как раньше): при отказе embedding — не сливаем
            # по fuzzy-пути.
            return None

        for i, belief in enumerate(candidates):
            similarity = float(np.dot(vectors[0], vectors[i + 1]))

            # По измеренным данным:
            # ~0.17 — unrelated
            # ~0.54-0.64 — одна тема, разные утверждения
            # ~0.81 — даже противоположные утверждения могут быть близки
            # ~0.92 — почти эквивалентные формулировки
            #
            # Поэтому threshold здесь НЕ решает equivalence.
            # Он только отсекает явно разные утверждения.
            if similarity < 0.70:
                continue

            if self._llm_judge_relation(belief.statement, statement) == "equivalent":
                return belief

        return None

    @staticmethod
    def _embed_batch(texts: List[str]):
        """Один /api/embed вызов на N текстов вместо N отдельных вызовов."""
        try:
            import requests
            import numpy as np

            session = requests.Session()
            session.trust_env = False

            resp = session.post(
                "http://127.0.0.1:11434/api/embed",
                json={
                    "model": "embeddinggemma:latest",
                    "input": [t[:2000] for t in texts],
                },
                timeout=60,
            )
            resp.raise_for_status()

            vecs = np.array(resp.json()["embeddings"], dtype=np.float32)
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0

            return vecs / norms

        except Exception:
            return None

    def _llm_judge_relation(self, a: str, b: str) -> str:
        """
        Короткий LLM judge — то же решение, что раньше было хвостом
        _is_similar_statement(), вызывается только для кандидатов,
        уже прошедших embedding-prefilter (similarity >= 0.70).
        """
        try:
            import requests

            session = requests.Session()
            session.trust_env = False

            prompt = f"""
Ты определяешь отношение между двумя утверждениями.

УТВЕРЖДЕНИЕ A:
{a}

УТВЕРЖДЕНИЕ B:
{b}

Выбери ровно одно:

equivalent
- утверждения выражают по существу одну и ту же мысль;
- различия только в формулировке или несущественных деталях.

contradicts
- утверждения несовместимы или говорят противоположное.

different
- утверждения относятся к одной теме, но утверждают разные вещи.

ВАЖНО:
- тематическая похожесть НЕ означает equivalent;
- одинаковые слова НЕ означают equivalent;
- отрицание необходимо учитывать;
- не решай, какое утверждение истинно;
- определи только отношение между ними.

Верни ТОЛЬКО JSON:
{{"relation":"equivalent"}}
"""

            resp = session.post(
                "http://127.0.0.1:11434/api/generate",
                json={
                    "model": "heretic:q8",
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

            parsed = json.loads(
                resp.json().get("response", "{}")
            )

            return str(
                parsed.get("relation", "")
            ).strip().lower()

        except Exception:
            # При отказе LLM judge НЕ сливаем beliefs (как раньше).
            return ""

    def _update_existing(
        self,
        belief: Belief,
        new_confidence: float,
        new_evidence_for: List[str] = None,
        new_evidence_against: List[str] = None,
    ) -> Belief:
        """Обновить существующее убеждение с Bayesian обновлением."""
        old_confidence = belief.confidence
        
        # ------------------------------------------------------------
        # Обновление только по НОВЫМ evidence.
        #
        # Один и тот же evidence_id не должен повторно изменять belief
        # при каждом проходе orchestrator.
        #
        # new_confidence здесь трактуется как качество/сила текущего
        # candidate claim, а не как готовая новая confidence belief.
        # ------------------------------------------------------------

        evidence_strength = max(
            0.05,
            min(0.95, float(new_confidence))
        )

        known_for = set(belief.evidence_for or [])
        known_against = set(belief.evidence_against or [])

        if new_evidence_for:
            for ev in new_evidence_for:
                if not ev:
                    continue

                # Уже известное свидетельство повторно не учитываем.
                if ev in known_for:
                    continue

                # Один evidence не может одновременно считаться
                # и поддержкой, и опровержением.
                if ev in known_against:
                    continue

                belief.confidence = self._bayesian_update(
                    belief,
                    evidence_strength,
                    True,
                )

                belief.evidence_for.append(ev)
                known_for.add(ev)

        if new_evidence_against:
            for ev in new_evidence_against:
                if not ev:
                    continue

                if ev in known_against:
                    continue

                if ev in known_for:
                    continue

                belief.confidence = self._bayesian_update(
                    belief,
                    evidence_strength,
                    False,
                )

                belief.evidence_against.append(ev)
                known_against.add(ev)
        
        # Обновляем contradiction_score
        belief.contradiction_score = self._calculate_contradiction_score(belief)
        
        # Если противоречивость высокая — понижаем уверенность
        if belief.contradiction_score > 0.5:
            belief.confidence = belief.confidence * (1 - belief.contradiction_score * 0.2)
        
        belief.updated_at = time.time()
        belief.history.append({
            "timestamp": time.time(),
            "old_confidence": old_confidence,
            "new_confidence": belief.confidence,
            "reason": "bayesian_update",
            "change": "updated",
            "contradiction_score": belief.contradiction_score,
        })
        self._save()
        shadow_record_belief_assessment(
            belief_id=belief.id, topic=belief.topic, statement=belief.statement,
            confidence=belief.confidence, status=belief.status,
            change_type="updated", old_confidence=old_confidence, new_confidence=belief.confidence,
            reason="bayesian_update",
        )
        return belief
    
    def challenge_belief(
        self,
        belief_id: str,
        counter_evidence: str,
        new_confidence: float,
        reason: str,
    ) -> Optional[Belief]:
        """
        Оспорить убеждение — изменить мнение под влиянием контраргументов.
        """
        for belief in self.beliefs:
            if belief.id == belief_id:
                old_confidence = belief.confidence
                # ----------------------------------------------------
                # Challenge также учитываем только один раз.
                #
                # disagreement_engine уже передаёт new_confidence,
                # поэтому больше не выбрасываем этот параметр.
                # ----------------------------------------------------

                challenge_strength = max(
                    0.05,
                    min(0.95, float(new_confidence))
                )

                known_against = set(belief.evidence_against or [])

                if counter_evidence and counter_evidence not in known_against:
                    belief.evidence_against.append(counter_evidence)

                    belief.confidence = self._bayesian_update(
                        belief,
                        challenge_strength,
                        False,
                    )

                belief.contradiction_score = self._calculate_contradiction_score(belief)
                
                belief.updated_at = time.time()
                belief.history.append({
                    "timestamp": time.time(),
                    "old_confidence": old_confidence,
                    "new_confidence": belief.confidence,
                    "reason": f"challenged: {reason}",
                    "change": "revised",
                    "contradiction_score": belief.contradiction_score,
                })
                
                if belief.confidence < 0.3:
                    belief.status = "revised"

                self._save()
                shadow_record_belief_assessment(
                    belief_id=belief.id, topic=belief.topic, statement=belief.statement,
                    confidence=belief.confidence, status=belief.status,
                    change_type="revised", old_confidence=old_confidence, new_confidence=belief.confidence,
                    reason=f"challenged: {reason}",
                )
                return belief
        return None
    
    def supersede_belief(self, old_belief_id: str, new_belief_id: str):
        """Заменяет одно убеждение другим."""
        old_belief = None
        new_belief = None
        
        for belief in self.beliefs:
            if belief.id == old_belief_id:
                old_belief = belief
            if belief.id == new_belief_id:
                new_belief = belief
        
        if old_belief and new_belief:
            old_belief.status = "superseded"
            old_belief.superseded_by = new_belief_id
            old_belief.updated_at = time.time()
            old_belief.history.append({
                "timestamp": time.time(),
                "reason": f"superseded by {new_belief_id}",
                "change": "superseded",
            })
            self._save()
            shadow_record_belief_assessment(
                belief_id=old_belief.id, topic=old_belief.topic, statement=old_belief.statement,
                confidence=old_belief.confidence, status=old_belief.status,
                change_type="superseded", old_confidence=None, new_confidence=None,
                reason=f"superseded by {new_belief_id}",
            )
            return True
        return False
    
    def get_beliefs_by_topic(self, topic: str) -> List[Belief]:
        return [b for b in self.beliefs if b.topic == topic and b.status == "active"]
    
    def get_all_active(self) -> List[Belief]:
        return [b for b in self.beliefs if b.status == "active"]
    
    def get_contradictory(self, min_score: float = 0.5) -> List[Belief]:
        """Получить убеждения с высокой противоречивостью."""
        return [b for b in self.beliefs if b.contradiction_score >= min_score]
    
    def get_stats(self) -> Dict[str, Any]:
        active = len(self.get_all_active())
        revised = len([b for b in self.beliefs if b.status == "revised"])
        superseded = len([b for b in self.beliefs if b.status == "superseded"])
        contradictory = len(self.get_contradictory())
        
        topics = {}
        for b in self.beliefs:
            topics[b.topic] = topics.get(b.topic, 0) + 1
        
        avg_conf = sum(b.confidence for b in self.beliefs) / len(self.beliefs) if self.beliefs else 0
        avg_contradiction = sum(b.contradiction_score for b in self.beliefs) / len(self.beliefs) if self.beliefs else 0
        
        return {
            "total": len(self.beliefs),
            "active": active,
            "revised": revised,
            "superseded": superseded,
            "contradictory": contradictory,
            "topics": topics,
            "avg_confidence": round(avg_conf, 2),
            "avg_contradiction": round(avg_contradiction, 2),
        }
    
    def summary(self) -> str:
        stats = self.get_stats()
        recent = self.beliefs[-3:] if self.beliefs else []
        
        return f"""
=== BELIEF MANAGER V7 ===
Всего: {stats['total']} | Активных: {stats['active']} | Пересмотренных: {stats['revised']} | Заменённых: {stats['superseded']}
Противоречивых: {stats['contradictory']}
Средняя уверенность: {stats['avg_confidence']}
Средняя противоречивость: {stats['avg_contradiction']}
Темы: {', '.join(f'{k}={v}' for k, v in stats['topics'].items())}

Последние убеждения:
{chr(10).join(f'  - [{b.confidence:.2f}] {b.statement[:40]} (за: {len(b.evidence_for)}, против: {len(b.evidence_against)}, конфликт: {b.contradiction_score:.2f})' for b in recent) if recent else '  нет'}
"""


_inst: Optional[BeliefManager] = None

def get_belief_manager() -> BeliefManager:
    global _inst
    if _inst is None:
        _inst = BeliefManager()
    return _inst


if __name__ == "__main__":
    bm = get_belief_manager()
    print(bm.summary())
    
    b = bm.add_belief(
        topic="consciousness",
        statement="Сознание является эмерджентным свойством нейронных сетей",
        confidence=0.7,
        evidence_for=["ev_001", "ev_002"],
        evidence_against=["ev_003"],
    )
    print(f"\n✅ Добавлено: {b.statement} (conf: {b.confidence})")
    
    bm.challenge_belief(
        belief_id=b.id,
        counter_evidence="ev_004: философские аргументы против редукционизма",
        new_confidence=0.55,
        reason="философские контраргументы",
    )
    print(f"✅ Оспорено: новая уверенность {b.confidence}")
    
    # Добавляем ещё одно убеждение для демонстрации противоречий
    b2 = bm.add_belief(
        topic="consciousness",
        statement="Сознание первично по отношению к материи",
        confidence=0.6,
        evidence_for=["ev_005"],
        evidence_against=["ev_006"],
    )
    print(f"\n✅ Добавлено: {b2.statement} (conf: {b2.confidence})")
    
    print("\n=== СТАТИСТИКА ===")
    print(bm.get_stats())
    
    print("\n=== ПРОТИВОРЕЧИВЫЕ УБЕЖДЕНИЯ ===")
    for c in bm.get_contradictory(0.3):
        print(f"  [{c.confidence:.2f}] {c.statement[:50]} (score: {c.contradiction_score:.2f})")
    
    print(bm.summary())
