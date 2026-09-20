"""
agent/relationship_commitments_regression_test.py — PROMISE LEDGER AND
EVIDENCE-BASED TRUST.

    USER SAID "I did it"  !=  YANDI KNOWS it was done            (TRUST != TRUTH)
    ONE CAUSAL EVENT -> ONE STATE TRANSITION
    EVENTS WRITE STATE. STATE DOES NOT INVENT EVENTS. THE LLM DOES NOT WRITE TRUST.

Synthetic person, in-memory fake SQL, controlled clock, no model.

Run: python -m agent.relationship_commitments_regression_test
"""
from __future__ import annotations

import inspect
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

import agent.db.sql.repositories as repo
import agent.relationship_commitments as rc
import agent.relationship_memory as rm
import agent.relationship_state as rs
from agent.relationship_apology_matching_regression_test import FakeConnection

FAILURES: list[str] = []
P = "person_a"
T0 = datetime(2026, 6, 1, 12, 0, 0)
H, DAY = timedelta(hours=1), timedelta(days=1)


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


class Clock:
    def __init__(self):
        self.now = T0

    def advance(self, delta):
        self.now += delta

    def __call__(self):
        return self.now


def trust(conn):
    return rs.get_state(conn, P)["trust"]


def main() -> int:
    clock = Clock()
    with patch.object(rm, "_now", clock), patch.object(repo, "_now", clock):
        # ── 1-3. lifecycle, immutability, the epistemic ladder ──
        c = FakeConnection()
        made = rc.create_commitment(c, P, "Я пришлю тебе отчёт по проекту до пятницы", "Я пришлю тебе отчёт")
        cid = made["commitment_id"]
        check("1: a promise is recorded with its own words and evidence", made["created"] and c.commitments[cid]["evidence"] == "Я пришлю тебе отчёт")
        check("1: a new promise starts OPEN", rc.commitment_statuses(c, P)[0]["status"] == "open")
        again = rc.create_commitment(c, P, "я пришлю тебе отчёт по проекту до пятницы!", "Я пришлю тебе отчёт")
        check("1: saying the very same promise again is not a second promise", not again["created"] and again["commitment_id"] == cid and len(c.commitments) == 1)

        base = rs.get_state(c, P)
        claim = rc.record_fulfillment_claim(c, P, cid, "Я отправил отчёт")
        check("2: the person's REPORT is recorded (status reported_fulfilled) ...",
              claim["recorded"] and rc.commitment_statuses(c, P)[0]["status"] == "reported_fulfilled")
        check("2: ... but words move NO coordinate (USER SAID != YANDI KNOWS)", rs.get_state(c, P) == base and c.inner_state_events == [])
        rc.record_fulfillment_claim(c, P, cid, "Я отправил отчёт")
        check("2: repeating the claim adds nothing", len([e for e in c.commitment_events if e["event_type"] == rc.CLAIMED]) == 1)

        try:
            rc.record_verification(c, P, cid, True, source="user_report")
            refused = False
        except ValueError:
            refused = True
        check("3: the person's own report can never be the source of a VERIFIED outcome", refused and rs.get_state(c, P) == base)

        clock.advance(2 * H)
        done = rc.record_verification(c, P, cid, True, source="independent_check", evidence="файл появился")
        after = rs.get_state(c, P)
        check("3: a verified outcome moves the state: trust up a lot, respect up, affection not",
              done["state_changed"] and after["trust"] == base["trust"] + rs.KEPT_TRUST
              and after["respect"] == base["respect"] + rs.KEPT_RESPECT and after["affection"] == base["affection"], repr(after))
        check("3: history is append-only: the earlier claim is untouched, the verification is a later event",
              [e["event_type"] for e in c.commitment_events] == [rc.CLAIMED, rc.VERIFIED_KEPT]
              and c.commitment_events[0]["created_at"] < c.commitment_events[1]["created_at"])

        # ── 4. ONE CAUSAL EVENT -> ONE STATE TRANSITION ──
        again = rc.record_verification(c, P, cid, True, source="independent_check")
        again2 = rc.record_verification(c, P, cid, True, source="another_check")
        check("4: PROMISE FULFILLED TWICE — the same fact cannot raise trust twice",
              not again["recorded"] and not again2["recorded"] and rs.get_state(c, P) == after
              and [e["event_type"] for e in c.inner_state_events].count("commitment_kept") == 1)
        contra = rc.record_verification(c, P, cid, False, source="independent_check")
        check("4: an already resolved promise is not flipped by a later verdict (no second transition)",
              not contra["recorded"] and rs.get_state(c, P) == after)

        # ── 5. verified broken ──
        c2 = FakeConnection()
        cid2 = rc.create_commitment(c2, P, "Я верну тебе вещь в понедельник", "Я верну")["commitment_id"]
        b0 = rs.get_state(c2, P)
        rc.record_verification(c2, P, cid2, False, source="independent_check")
        b1 = rs.get_state(c2, P)
        check("5: a VERIFIED broken promise hits trust hardest",
              b1["trust"] == b0["trust"] + rs.BROKEN_TRUST and (b0["trust"] - b1["trust"]) > (b0["respect"] - b1["respect"]) > (b0["affection"] - b1["affection"]) >= 0, repr(b1))

        # ── 6. overdue is derived and moves nothing ──
        c3 = FakeConnection()
        clock.now = T0
        cid3 = rc.create_commitment(c3, P, "Я позвоню завтра", "Я позвоню", due_at=T0 + DAY)["commitment_id"]
        s0 = rs.get_state(c3, P)
        clock.advance(3 * DAY)
        st = rc.commitment_statuses(c3, P)[0]
        check("6: a passed deadline is only a derived flag (status stays open, overdue=True) ...", st["status"] == "open" and st["overdue"] is True)
        check("6: ... and it changes no coordinate: a missed deadline is not a broken promise", rs.get_state(c3, P) == s0 and c3.inner_state_events == [])

        # ── 7-9. linking a claim to a specific historical promise ──
        clock.now = T0
        m = FakeConnection()
        a = rc.create_commitment(m, P, "Я пришлю отчёт по проекту", "Я пришлю отчёт")["commitment_id"]
        clock.advance(H)
        b = rc.create_commitment(m, P, "Я куплю билеты на концерт", "Я куплю билеты")["commitment_id"]
        f = rc.resolve_commitment_focus(m, P, "Я отправил отчёт")
        check("7: a claim that names the promise's content links to THAT promise", f["commitment"]["commitment_id"] == a and f["basis"] == "explicit_reference", repr(f["basis"]))
        f = rc.resolve_commitment_focus(m, P, "Билеты купил, всё готово")
        check("7: ... including when it names the other one", f["commitment"]["commitment_id"] == b)
        f = rc.resolve_commitment_focus(m, P, "Я всё сделал")
        check("8: a generic 'I did everything' with several open promises is AMBIGUOUS: no target", f["commitment"] is None and f["basis"] == "ambiguous" and len(f["candidates"]) == 2)
        before_events = len(m.commitment_events)
        out = rc.record_fulfillment_claim(m, P, f["commitment"] and f["commitment"]["commitment_id"], "Я всё сделал")
        check("8: ... and nothing is written for it", not out["recorded"] and len(m.commitment_events) == before_events)
        one = FakeConnection()
        rc.create_commitment(one, P, "Я помогу тебе с переездом", "Я помогу")
        f = rc.resolve_commitment_focus(one, P, "Я всё сделал")
        check("8: a generic claim with exactly ONE open promise links to it", f["basis"] == "sole_open_commitment")
        none = FakeConnection()
        check("9: no promises at all -> a claim has no target", rc.resolve_commitment_focus(none, P, "Я всё сделал")["commitment"] is None)
        cross = FakeConnection()
        cid_x = rc.create_commitment(cross, "someone_else", "Я пришлю отчёт", "Я пришлю")["commitment_id"]
        check("9: another person's promise is never a target for this person",
              rc.resolve_commitment_focus(cross, P, "Я отправил отчёт")["commitment"] is None
              and not rc.record_fulfillment_claim(cross, P, cid_x, "Я отправил отчёт")["recorded"])

        # ── 10. COUNTERFACTUAL: same current message, different history ──
        msg = "Я отправил отчёт"
        with_promise, without_promise = FakeConnection(), FakeConnection()
        rc.create_commitment(with_promise, P, "Я пришлю отчёт по проекту", "Я пришлю отчёт")
        rc.create_commitment(without_promise, P, "Я куплю билеты на концерт", "Я куплю билеты")
        rc.create_commitment(without_promise, P, "Я помогу с переездом", "Я помогу")
        outcome = {}
        for label, conn in (("with", with_promise), ("without", without_promise)):
            focus = rc.resolve_commitment_focus(conn, P, msg)
            target = focus["commitment"]["commitment_id"] if focus["commitment"] else None
            outcome[label] = rc.record_fulfillment_claim(conn, P, target, msg)["recorded"]
        check("10: COUNTERFACTUAL — the same message records a claim only where the history holds the matching promise",
              outcome == {"with": True, "without": False}, repr(outcome))

        # ── 11. REPLAY: the state is reproducible from the immutable events ──
        r = FakeConnection()
        clock.now = T0
        g1 = rm.add_grievance(r, P, "insult", "Ты дура " + "а" * 25, 0.7)
        rm.acknowledge_apology(r, g1, 0.9)
        rm.add_grievance(r, P, "insult", "Ты бесполезная " + "б" * 25, 0.4)
        k1 = rc.create_commitment(r, P, "Я пришлю отчёт", "Я пришлю")["commitment_id"]
        k2 = rc.create_commitment(r, P, "Я верну книгу", "Я верну")["commitment_id"]
        rc.record_verification(r, P, k1, True, source="independent_check")
        rc.record_verification(r, P, k2, False, source="independent_check")
        rc.record_verification(r, P, k1, True, source="independent_check")
        live = rs.get_state(r, P)
        replayed = rs.replay(r, P)
        check("11: replaying the event trail from the defaults reproduces the live relationship state",
              all(abs(live[k] - replayed[k]) < 1e-9 for k in ("trust", "respect", "affection")), f"{live} vs {replayed}")
        wiped = FakeConnection()
        wiped.inner_state_events = list(r.inner_state_events)
        check("11: the state survives losing the materialised row: it is a cache of the trail",
              all(abs(rs.replay(wiped, P)[k] - live[k]) < 1e-9 for k in ("trust", "respect", "affection")))

        # ── 12. no keyword detectors, and the model has no path to trust ──
        src = inspect.getsource(rc)
        check("12: the ledger contains no regex / keyword lists that decide an event happened",
              "re.search" not in src and "re.match" not in src and "re.findall" not in src)
        check("12: the write API takes events and verifiers, never coordinate values",
              list(inspect.signature(rc.record_verification).parameters) == ["conn", "user_id", "commitment_id", "kept", "source", "evidence"]
              and list(inspect.signature(rc.record_fulfillment_claim).parameters) == ["conn", "user_id", "commitment_id", "evidence"])

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
