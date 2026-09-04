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

"ТОЧКА НОЛЬ" (owner mandate, 2026-09): раньше текущее состояние жило в
registry/beliefs.json, а история изменений ДОПОЛНИТЕЛЬНО дублировалась
в SQL через shadow_record_belief_assessment() — то есть JSON оставался
источником истины, SQL был лишь тенью. Теперь SQL (agent.db.sql.belief
+ belief_assessment_history) — ЕДИНСТВЕННЫЙ источник истины, JSON-файла
для этого состояния больше нет вообще. Владелец: "если мы строим
бастион, то json не должно быть в принципе — мы пытаемся сохранить
цепочку знаний, рассуждений, почему они менялись". Старое содержимое
registry/beliefs.json НЕ переносится — начинаем с чистого состояния,
не с миграции.

ВАЖНОЕ ПОСЛЕДСТВИЕ: раньше при недоступности SQL это was a "shadow"
write — сбой молча проглатывался, JSON-путь работал независимо. Теперь
SQL — не тень, а единственный путь: agent.db.sql.connection.
SqlUnavailable из любого метода этого класса ПРОБРАСЫВАЕТСЯ наружу, а
не глотается. Система, которая не доверяет себе, не может тихо делать
вид, что убеждение сохранено, когда оно на самом деле нигде не
записано.
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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List

from agent.db.sql.connection import get_connection
import agent.db.sql.repositories as repo


def _dt_to_unix(value) -> float:
    """SQL DATETIME columns come back as naive datetime objects
    representing UTC (see repositories._coerce_datetime's own
    docstring) — .timestamp() on a naive datetime assumes LOCAL time,
    which silently corrupts the value (same class of bug already found
    and fixed once this session in agent/relationship_memory_regression_
    test.py). Attaching tzinfo=utc explicitly before converting is the
    only correct way back to a Unix timestamp float."""
    if value is None:
        return time.time()
    if isinstance(value, (int, float)):
        return float(value)
    return value.replace(tzinfo=timezone.utc).timestamp()


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


def _row_to_belief(row: Dict[str, Any]) -> Belief:
    """SQL row (agent.db.sql.repositories.get_belief()/list_*) -> Belief.
    `history` is deliberately empty here — it is no longer embedded in
    the current-state row (it lives in belief_assessment_history, a
    separate append-only table); call get_belief_history() for the real
    thing. No current caller reads Belief.history (confirmed via grep
    before this rewrite), so an empty list is honest, not a stub
    pretending to be complete data."""
    return Belief(
        id=row["belief_id"], topic=row["topic"], statement=row["statement"],
        confidence=row["confidence"], evidence_for=row.get("evidence_for") or [],
        evidence_against=row.get("evidence_against") or [], claim_ids=row.get("claim_ids") or [],
        created_at=_dt_to_unix(row["created_at"]), updated_at=_dt_to_unix(row["updated_at"]),
        history=[], status=row["status"], prior=row["prior"], likelihood=row["likelihood"],
        contradiction_score=row["contradiction_score"], decay_factor=row["decay_factor"],
        superseded_by=row.get("superseded_by"),
    )


class BeliefManager:
    """
    Управление убеждениями с Bayesian обновлением. SQL-backed — см.
    модульный докстринг про "точку ноль".
    """

    def __init__(self):
        self._apply_decay()

    def _apply_decay(self):
        """Применить затухание уверенности со временем — раньше
        выполнялось один раз при загрузке JSON в память; теперь читает
        активные убеждения из SQL, считает decay в Python, и пишет
        изменившиеся обратно за один проход."""
        now = time.time()
        with get_connection() as conn:
            active = repo.list_active_beliefs(conn)
            for row in active:
                updated_at = _dt_to_unix(row["updated_at"])
                age_days = (now - updated_at) / 86400
                if age_days <= 1:
                    continue
                decay = row["decay_factor"] ** age_days
                old_conf = row["confidence"]
                new_conf = old_conf * decay
                repo.upsert_belief(
                    conn, row["belief_id"], row["topic"], row["statement"], new_conf,
                    status=row["status"], evidence_for=row.get("evidence_for") or [],
                    evidence_against=row.get("evidence_against") or [], claim_ids=row.get("claim_ids") or [],
                    prior=row["prior"], likelihood=row["likelihood"],
                    contradiction_score=row["contradiction_score"], decay_factor=row["decay_factor"],
                    superseded_by=row.get("superseded_by"), created_at=row["created_at"], updated_at=now,
                )
                repo.record_belief_assessment(
                    conn, row["belief_id"], change_type="decayed",
                    old_confidence=old_conf, new_confidence=new_conf,
                    reason=f"decay: {age_days:.1f} days", created_at=now,
                )
            conn.commit()

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
            posterior = (prior * likelihood) / (prior * likelihood + (1 - prior) * (1 - likelihood))
        else:
            posterior = (prior * (1 - likelihood)) / (prior * (1 - likelihood) + (1 - prior) * likelihood)

        posterior = max(0.01, min(0.99, posterior))
        return posterior

    def _calculate_contradiction_score(self, belief: Belief) -> float:
        """Рассчитать противоречивость убеждения."""
        if not belief.evidence_for and not belief.evidence_against:
            return 0.0

        total = len(belief.evidence_for) + len(belief.evidence_against)
        if total == 0:
            return 0.0

        ratio = len(belief.evidence_against) / total
        return min(1.0, ratio * 2)

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

        belief_id = f"bel_{uuid.uuid4().hex[:8]}"
        now = time.time()
        with get_connection() as conn:
            repo.upsert_belief(
                conn, belief_id, topic, statement, confidence, status="active",
                evidence_for=evidence_for or [], evidence_against=evidence_against or [],
                claim_ids=claim_ids or [], prior=prior, likelihood=confidence,
                contradiction_score=0.0, decay_factor=0.95, created_at=now, updated_at=now,
            )
            repo.record_belief_assessment(
                conn, belief_id, change_type="created", old_confidence=0.0,
                new_confidence=confidence, reason="initial", created_at=now,
            )
            conn.commit()
            row = repo.get_belief(conn, belief_id)
        return _row_to_belief(row)

    def _find_similar(self, topic: str, statement: str) -> Optional[Belief]:
        """
        Найти существующее убеждение, эквивалентное новому statement.

        Сначала быстрый exact-match проход (без HTTP-вызовов). Только
        если exact match не найден — один batch-embed вызов, и по
        кандидатам в том же порядке threshold-gated LLM judge, первый
        "equivalent" выигрывает.
        """
        import numpy as np

        if not statement:
            return None

        with get_connection() as conn:
            candidates = repo.list_beliefs_by_topic(conn, topic)

        if not candidates:
            return None

        statement_norm = " ".join(statement.lower().split())

        for row in candidates:
            belief_norm = " ".join((row["statement"] or "").lower().split())
            if belief_norm == statement_norm:
                return _row_to_belief(row)

        vectors = self._embed_batch([statement] + [c["statement"] for c in candidates])

        if vectors is None:
            return None

        for i, row in enumerate(candidates):
            similarity = float(np.dot(vectors[0], vectors[i + 1]))
            if similarity < 0.70:
                continue
            if self._llm_judge_relation(row["statement"], statement) == "equivalent":
                return _row_to_belief(row)

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
        Короткий LLM judge, вызывается только для кандидатов, уже
        прошедших embedding-prefilter (similarity >= 0.70).
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

        evidence_strength = max(0.05, min(0.95, float(new_confidence)))

        known_for = set(belief.evidence_for or [])
        known_against = set(belief.evidence_against or [])

        if new_evidence_for:
            for ev in new_evidence_for:
                if not ev or ev in known_for or ev in known_against:
                    continue
                belief.confidence = self._bayesian_update(belief, evidence_strength, True)
                belief.evidence_for.append(ev)
                known_for.add(ev)

        if new_evidence_against:
            for ev in new_evidence_against:
                if not ev or ev in known_against or ev in known_for:
                    continue
                belief.confidence = self._bayesian_update(belief, evidence_strength, False)
                belief.evidence_against.append(ev)
                known_against.add(ev)

        belief.contradiction_score = self._calculate_contradiction_score(belief)

        if belief.contradiction_score > 0.5:
            belief.confidence = belief.confidence * (1 - belief.contradiction_score * 0.2)

        belief.updated_at = time.time()

        with get_connection() as conn:
            repo.upsert_belief(
                conn, belief.id, belief.topic, belief.statement, belief.confidence,
                status=belief.status, evidence_for=belief.evidence_for,
                evidence_against=belief.evidence_against, claim_ids=belief.claim_ids,
                prior=belief.prior, likelihood=belief.likelihood,
                contradiction_score=belief.contradiction_score, decay_factor=belief.decay_factor,
                superseded_by=belief.superseded_by, created_at=belief.created_at, updated_at=belief.updated_at,
            )
            repo.record_belief_assessment(
                conn, belief.id, change_type="updated", old_confidence=old_confidence,
                new_confidence=belief.confidence, reason="bayesian_update", created_at=belief.updated_at,
            )
            conn.commit()
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
        with get_connection() as conn:
            row = repo.get_belief(conn, belief_id)
        if not row:
            return None
        belief = _row_to_belief(row)

        old_confidence = belief.confidence
        challenge_strength = max(0.05, min(0.95, float(new_confidence)))
        known_against = set(belief.evidence_against or [])

        if counter_evidence and counter_evidence not in known_against:
            belief.evidence_against.append(counter_evidence)
            belief.confidence = self._bayesian_update(belief, challenge_strength, False)

        belief.contradiction_score = self._calculate_contradiction_score(belief)
        belief.updated_at = time.time()

        if belief.confidence < 0.3:
            belief.status = "revised"

        with get_connection() as conn:
            repo.upsert_belief(
                conn, belief.id, belief.topic, belief.statement, belief.confidence,
                status=belief.status, evidence_for=belief.evidence_for,
                evidence_against=belief.evidence_against, claim_ids=belief.claim_ids,
                prior=belief.prior, likelihood=belief.likelihood,
                contradiction_score=belief.contradiction_score, decay_factor=belief.decay_factor,
                superseded_by=belief.superseded_by, created_at=belief.created_at, updated_at=belief.updated_at,
            )
            repo.record_belief_assessment(
                conn, belief.id, change_type="revised", old_confidence=old_confidence,
                new_confidence=belief.confidence, reason=f"challenged: {reason}", created_at=belief.updated_at,
            )
            conn.commit()
        return belief

    def supersede_belief(self, old_belief_id: str, new_belief_id: str):
        """Заменяет одно убеждение другим."""
        with get_connection() as conn:
            old_row = repo.get_belief(conn, old_belief_id)
            new_row = repo.get_belief(conn, new_belief_id)
            if not old_row or not new_row:
                return False

            now = time.time()
            repo.upsert_belief(
                conn, old_row["belief_id"], old_row["topic"], old_row["statement"], old_row["confidence"],
                status="superseded", evidence_for=old_row.get("evidence_for") or [],
                evidence_against=old_row.get("evidence_against") or [], claim_ids=old_row.get("claim_ids") or [],
                prior=old_row["prior"], likelihood=old_row["likelihood"],
                contradiction_score=old_row["contradiction_score"], decay_factor=old_row["decay_factor"],
                superseded_by=new_belief_id, created_at=old_row["created_at"], updated_at=now,
            )
            repo.record_belief_assessment(
                conn, old_belief_id, change_type="superseded", old_confidence=None, new_confidence=None,
                reason=f"superseded by {new_belief_id}", created_at=now,
            )
            conn.commit()
        return True

    def get_beliefs_by_topic(self, topic: str) -> List[Belief]:
        with get_connection() as conn:
            rows = repo.list_beliefs_by_topic(conn, topic, statuses=["active"])
        return [_row_to_belief(r) for r in rows]

    def get_all_active(self) -> List[Belief]:
        with get_connection() as conn:
            rows = repo.list_active_beliefs(conn)
        return [_row_to_belief(r) for r in rows]

    def get_belief(self, belief_id: str) -> Optional[Belief]:
        with get_connection() as conn:
            row = repo.get_belief(conn, belief_id)
        return _row_to_belief(row) if row else None

    def get_belief_history(self, belief_id: str) -> List[Dict[str, Any]]:
        """The REAL, append-only history — belief_assessment_history,
        never Belief.history (always [] now, see _row_to_belief's own
        docstring)."""
        with get_connection() as conn:
            return repo.list_belief_history(conn, belief_id)

    def get_all(self) -> List[Belief]:
        """Every belief regardless of status. Public replacement for
        directly reaching into a (now nonexistent) `.beliefs` in-memory
        list — agent/dependency_recheck.py's _belief_for_family() used
        to do exactly that; fixed to call this instead as part of
        "точка ноль"."""
        with get_connection() as conn:
            rows = repo.list_all_beliefs(conn)
        return [_row_to_belief(r) for r in rows]

    def get_contradictory(self, min_score: float = 0.5) -> List[Belief]:
        """Получить убеждения с высокой противоречивостью."""
        with get_connection() as conn:
            rows = repo.list_contradictory_beliefs(conn, min_score)
        return [_row_to_belief(r) for r in rows]

    def get_stats(self) -> Dict[str, Any]:
        with get_connection() as conn:
            return repo.get_belief_stats(conn)

    def summary(self) -> str:
        stats = self.get_stats()
        with get_connection() as conn:
            cur_rows = repo.list_active_beliefs(conn)
        recent = [_row_to_belief(r) for r in cur_rows[-3:]] if cur_rows else []

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
