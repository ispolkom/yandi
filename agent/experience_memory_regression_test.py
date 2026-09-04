"""
agent/experience_memory_regression_test.py — Этап 5 storage-audit
follow-up (P2, mandate §25): fixes a real, isolated bug found during
the audit — get_lessons() had no return statement (dead code, always
returned None), and get_relevant_lessons() was defined TWICE
(byte-identical bodies, the second silently shadowing the first —
Python only keeps the last definition of a method with a given name in
a class body), with a stray unreachable `return lessons[:limit]` left
over after the merge. Fixed by keeping ONE get_relevant_lessons
definition and giving get_lessons its missing return.

Deliberately NOT touched in this pass (documented, separate P2/P3 debt
— see the final report): the destructive running-average blend in
update_success() that overwrites the prior reaction instead of
preserving it. A real behavior-tradeoff decision, not an obvious bug,
and out of scope for a one-line dead-code fix.

"ТОЧКА НОЛЬ" UPDATE (owner mandate, 2026-09): ExperienceMemory is
SQL-only now (no EXPERIENCE_DIR, no per-user JSON file) — the silent
200-entry cap in the old _save() no longer exists at all (fixed, not
just left alone) since experience is now a real SQL table. This test's
own fixture patches agent.experience_memory.get_connection with a fake
claim_family-shaped connection instead of a tempdir.

Run: /home/iam/venv/bin/python3 -m agent.experience_memory_regression_test
"""
from __future__ import annotations

import contextlib

import agent.experience_memory as em

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class _FakeCursor:
    lastrowid = 1

    def __init__(self, conn):
        self.conn = conn
        self._results = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        upper = " ".join(sql.split()).upper()
        self._results = None
        conn = self.conn
        if upper.startswith("INSERT INTO EXPERIENCE"):
            experience_id, user_id, speech_act, topic, query, response, context, created_at = params
            conn.experiences[experience_id] = {
                "experience_id": experience_id, "user_id": user_id, "speech_act": speech_act,
                "topic": topic, "query": query, "response": response, "user_reaction": None,
                "success": 0.5, "used_count": 0, "context": context, "created_at": created_at,
            }
        elif upper.startswith("SELECT * FROM EXPERIENCE WHERE USER_ID=%S ORDER BY CREATED_AT ASC"):
            (user_id,) = params
            rows = [dict(e) for e in conn.experiences.values() if e["user_id"] == user_id]
            rows.sort(key=lambda e: e["created_at"])
            self._results = rows
        elif upper.startswith("UPDATE EXPERIENCE SET USED_COUNT"):
            (experience_id,) = params
            conn.experiences[experience_id]["used_count"] += 1
        elif upper.startswith("UPDATE EXPERIENCE SET USER_REACTION"):
            user_reaction, success, experience_id = params
            conn.experiences[experience_id]["user_reaction"] = user_reaction
            conn.experiences[experience_id]["success"] = success

    def fetchone(self):
        return None

    def fetchall(self):
        return self._results or []


class _FakeConnection:
    def __init__(self):
        self.experiences = {}

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        pass


_conn = _FakeConnection()


@contextlib.contextmanager
def _fake_get_connection(autocommit=False):
    yield _conn


em.get_connection = _fake_get_connection

mem = em.ExperienceMemory(user_id="test_user")
mem.add_experience(
    speech_act="insult", topic="general", query="ты глупая",
    response="Спасибо за отзыв.",
    context={"domain": "general", "trust": "UNVERIFIED", "confidence": 0.4,
             "mistakes": [], "lessons": ["не реагировать эмоционально"],
             "policy_changes": [], "timestamp": "2026-01-01T00:00:00"},
)
mem.add_experience(
    speech_act="question", topic="science", query="сколько спутников у юпитера",
    response="95 подтверждённых.",
    context={"domain": "science", "trust": "SUPPORTED", "confidence": 0.8,
             "mistakes": [], "lessons": ["перепроверять числовые claims"],
             "policy_changes": [], "timestamp": "2026-01-02T00:00:00"},
)
mem.add_experience(
    speech_act="question", topic="general", query="no lessons here",
    response="ok", context={"domain": "general"},  # no "lessons" key at all
)

lessons = mem.get_lessons(limit=10)
relevant = mem.get_relevant_lessons("сколько спутников", limit=5)

check(
    "get_lessons(): returns an actual list, not None (the missing-return bug)",
    isinstance(lessons, list),
    f"{lessons!r}",
)
check(
    "get_lessons(): finds both experiences that actually have a non-empty context.lessons "
    "(the third experience, with no lessons key, is correctly excluded)",
    len(lessons) == 2,
    f"{lessons}",
)
check(
    "get_lessons(): sorted by confidence descending (0.8 before 0.4)",
    lessons[0]["confidence"] == 0.8 and lessons[1]["confidence"] == 0.4,
    f"{lessons}",
)

check(
    "get_relevant_lessons(): returns a list scored by query-word overlap "
    "(not just an alias that silently returns nothing)",
    isinstance(relevant, list) and len(relevant) >= 1,
    f"{relevant}",
)
check(
    "get_relevant_lessons(): the science/Jupiter experience ranks first for a "
    "'сколько спутников' query (real keyword-overlap scoring, not just confidence order)",
    relevant[0]["domain"] == "science",
    f"{relevant}",
)

check(
    "structural: get_relevant_lessons is defined exactly ONCE in the class body "
    "(no silent duplicate-definition shadowing)",
    __import__("inspect").getsource(em.ExperienceMemory).count("def get_relevant_lessons(") == 1,
)
check(
    "structural: no unreachable dead code after get_relevant_lessons's real return "
    "(the stray 'return lessons[:limit]' leftover from the merge accident is gone)",
    "return lessons[:limit]\n        return lessons[:limit]" not in __import__("inspect").getsource(em),
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
