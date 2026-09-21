"""
agent/relationship_direct_fulfilment_regression_test.py — VERIFIED RELIABILITY
RAISES TRUST, AND NOTHING ELSE DOES.

    USER REPORTS FULFILMENT  !=  FULFILMENT VERIFIED.
    TRUST GROWS ONLY FROM VERIFIED EVIDENCE.
    ONE COMMITMENT -> MAX ONE VERIFIED TRUST REWARD.   ONE EVIDENCE -> MAX ONE COMMITMENT.
    TRIVIAL PROMISE FARMING MUST NOT DOMINATE TRUST.
    APOLOGY != TRUST RESTORATION.   RELATIONAL TRUST != EPISTEMIC TRUST.
    THE LLM DOES NOT WRITE RELATIONSHIP STATE.   HISTORY IS EXTENDED, NEVER REWRITTEN.

The ledger half of Cycle 8 (agent/relationship_commitments.record_direct_fulfilment and the
transition in agent/relationship_state.record_observed_commitment) over the in-memory fake SQL,
synthetic person, no model. The invariants are written ONCE as a scenario and run against the
real code and against mutants of its source, which must each be caught.

Run: python -m agent.relationship_direct_fulfilment_regression_test
"""
from __future__ import annotations

import inspect
import logging
import re
import sys
import types
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


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def trust(conn) -> float:
    return rs.get_state(conn, P)["trust"]


class World:
    """A synthetic person with a fake database. `promise()` makes an in_chat promise in its own turn;
    `say()` records a later turn's immutable text and returns the exact span of `quote` in it."""

    def __init__(self, rc_mod=rc):
        self.rc = rc_mod
        self.conn = FakeConnection()
        self.n = 0

    def promise(self, text="в следующем сообщении дам кодовое слово", kind=rc.KIND_IN_CHAT, user=P) -> str:
        self.n += 1
        turn = f"turn-promise-{self.n:04d}"
        repo.record_interaction_turn(self.conn, user, turn, "client", f"Обещаю: {text}.", None)
        return self.rc.create_commitment(self.conn, user, f"Обещаю: {text}.", text, kind=kind, source_turn_id=turn, span=(8, 8 + len(text)))["commitment_id"]

    def say(self, text: str, quote: str, user=P):
        self.n += 1
        turn = f"turn-say-{self.n:04d}"
        repo.record_interaction_turn(self.conn, user, turn, "client", text, None)
        at = text.index(quote)
        return turn, (at, at + len(quote))

    def verify(self, cid, text="Кодовое слово: АЛЬФА", quote="АЛЬФА", user=P, turn=None, span=None, evidence=None):
        if turn is None:
            turn, span = self.say(text, quote, user)
        return self.rc.record_direct_fulfilment(self.conn, user, cid, quote if evidence is None else evidence, turn, span)


def invariants(rc_mod=rc, rs_mod=rs) -> list[str]:
    """Every invariant of the ledger half, as a list of the ones that are VIOLATED (empty = all hold)."""
    bad: list[str] = []
    with patch.object(rm, "_now", lambda: T0), patch.object(repo, "_now", lambda: T0), patch.object(rc_mod, "relationship_state", rs_mod):
        # I1: a REPORT is not verification and moves nothing
        w = World(rc_mod)
        cid = w.promise()
        t0 = trust(w.conn)
        rc_mod.record_fulfillment_claim(w.conn, P, cid, "Я выполнил обещание", source_turn_id="turn-claim-0001")
        if trust(w.conn) != t0 or any(e["event_type"] != rc_mod.CLAIMED for e in w.conn.commitment_events) or w.conn.inner_state_events:
            bad.append("I1 a fulfilment claim was treated as verification")
        # I2: direct evidence in an identified turn raises trust, with provenance
        r = w.verify(cid)
        ev = [e for e in w.conn.commitment_events if e["event_type"] == rc_mod.VERIFIED_KEPT]
        if not (r["recorded"] and trust(w.conn) > t0 and len(ev) == 1 and ev[0]["source_turn_id"] and ev[0]["span_start"] is not None):
            bad.append("I2 a directly observed delivery was not verified with provenance")
        # I3: the same commitment cannot pay twice (another turn, same evidence)
        t1 = trust(w.conn)
        w.verify(cid)
        w.verify(cid)
        if trust(w.conn) != t1 or len([e for e in w.conn.commitment_events if e["event_type"] == rc_mod.VERIFIED_KEPT]) != 1:
            bad.append("I3 one commitment raised trust twice")
        # I4: one turn verifies at most one commitment
        w = World(rc_mod)
        a, b = w.promise("в следующем сообщении дам слово"), w.promise("в следующем сообщении дам число")
        turn, span = w.say("Вот: АЛЬФА", "АЛЬФА")
        rc_mod.record_direct_fulfilment(w.conn, P, a, "АЛЬФА", turn, span)
        rc_mod.record_direct_fulfilment(w.conn, P, b, "АЛЬФА", turn, span)
        if len([e for e in w.conn.commitment_events if e["event_type"] == rc_mod.VERIFIED_KEPT]) != 1:
            bad.append("I4 one evidence span verified more than one commitment")
        # I5: the evidence must be, byte for byte, in the immutable record of the turn it names
        w = World(rc_mod)
        cid = w.promise()
        turn, span = w.say("Сегодня хорошая погода", "погода")
        r = rc_mod.record_direct_fulfilment(w.conn, P, cid, "АЛЬФА", turn, span)
        r2 = rc_mod.record_direct_fulfilment(w.conn, P, cid, "погода", "turn-nonexistent-1", span)
        if r["recorded"] or r2["recorded"] or trust(w.conn) != 50.0:
            bad.append("I5 evidence that is not in the recorded turn was accepted")
        # I6: only a promise classified in_chat can be verified (an external promise is only ever reported)
        w = World(rc_mod)
        for kind in (rc_mod.KIND_EXTERNAL, rc_mod.KIND_GENERAL):
            cid = w.promise("оплатить счёт", kind=kind)
            if w.verify(cid, "Я оплатил счёт", "оплатил счёт")["recorded"]:
                bad.append(f"I6 a {kind} promise was verified")
        if rc_mod.verifiable_commitments(w.conn, P, None):
            bad.append("I6 an external promise is offered as a verification target")
        # I7: 20 trivial promises do not farm trust
        w = World(rc_mod)
        for i in range(20):
            cid = w.promise(f"в следующем сообщении назову число {i}")
            w.verify(cid, f"Число {i}", str(i))
        if trust(w.conn) > 50.0 + 6.0 or trust(w.conn) > rs_mod.OBSERVED_TRUST_CEILING:
            bad.append(f"I7 trivial promises farmed trust to {trust(w.conn):.1f}")
        # I8: a failure of the trust transition reaches the caller (so its transaction rolls the proof back)
        w = World(rc_mod)
        cid = w.promise()
        turn, span = w.say("Кодовое слово: АЛЬФА", "АЛЬФА")
        logging.disable(logging.CRITICAL)      # a mutant that swallows the failure logs it: expected noise
        try:
            with patch.object(repo, "update_inner_state", side_effect=RuntimeError("state write failed")):
                try:
                    rc_mod.record_direct_fulfilment(w.conn, P, cid, "АЛЬФА", turn, span)
                    bad.append("I8 a failed trust transition was swallowed after the verified event was written")
                except RuntimeError:
                    pass
        finally:
            logging.disable(logging.NOTSET)
    return bad


def mutant(source_module, replacements: list[tuple[str, str]], name: str):
    """A copy of `source_module` with textual replacements applied (each must match exactly once)."""
    src = inspect.getsource(source_module)
    for old, new in replacements:
        if src.count(old) != 1:
            raise AssertionError(f"mutant {name}: pattern not found exactly once: {old!r} ({src.count(old)})")
        src = src.replace(old, new)
    mod = types.ModuleType(source_module.__name__ + "__" + name)
    mod.__file__ = source_module.__file__
    sys.modules[mod.__name__] = mod
    exec(compile(src, mod.__file__, "exec"), mod.__dict__)
    return mod


def main() -> int:
    with patch.object(rm, "_now", lambda: T0), patch.object(repo, "_now", lambda: T0):
        # ── the real code holds every invariant ──
        violated = invariants()
        check("0: the real code holds every ledger invariant I1-I8", violated == [], repr(violated))

        # ── 1. provenance, epistemic ladder, append-only ──
        w = World()
        cid = w.promise()
        base = rs.get_state(w.conn, P)
        claim = rc.record_fulfillment_claim(w.conn, P, cid, "Я всё выполнил", source_turn_id="turn-claim-0001", span=(0, 5))
        check("1: the report is recorded WITH its own provenance (turn, span) and is only a report",
              claim["recorded"] and w.conn.commitment_events[0]["source_turn_id"] == "turn-claim-0001"
              and w.conn.commitment_events[0]["source"] == rc.USER_REPORT and rc.commitment_statuses(w.conn, P)[0]["status"] == "reported_fulfilled")
        check("1: the promise registered and the fulfilment reported: trust is still T", rs.get_state(w.conn, P) == base and not w.conn.inner_state_events)
        turn, span = w.say("Кодовое слово: АЛЬФА", "АЛЬФА")
        done = rc.record_direct_fulfilment(w.conn, P, cid, "АЛЬФА", turn, span)
        v = [e for e in w.conn.commitment_events if e["event_type"] == rc.VERIFIED_KEPT][0]
        check("1: a directly observed delivery is a NEW event after the report; the report is untouched (append-only)",
              [e["event_type"] for e in w.conn.commitment_events] == [rc.CLAIMED, rc.VERIFIED_KEPT] and w.conn.commitment_events[0]["source"] == rc.USER_REPORT)
        check("1: the verified event knows its commitment, person, source turn, exact evidence, span, verifier class and time",
              (v["commitment_id"], v["user_id"], v["source_turn_id"], v["evidence"], (v["span_start"], v["span_end"]), v["source"], v["created_at"])
              == (cid, P, turn, "АЛЬФА", span, rc.VERIFIER_IN_CHAT, T0))
        check("1: the evidence IS the bytes of the recorded turn at that span",
              [t for t in w.conn.interaction_turns if t["source_turn_id"] == turn][0]["user_text"][span[0]:span[1]] == v["evidence"])
        after = rs.get_state(w.conn, P)
        check("1: verified reliability raises TRUST, by the bounded reward, and only trust",
              done["recorded"] and done["state_changed"] and after["trust"] == base["trust"] + rs.OBSERVED_TRUST_BASE
              and after["respect"] == base["respect"] and after["affection"] == base["affection"], repr(after))
        check("1: the audit row carries the magnitude, and replay from the audit trail alone rebuilds the state",
              [(e["event_type"], e["weight"]) for e in w.conn.inner_state_events] == [("commitment_observed", rs.OBSERVED_TRUST_BASE)]
              and rs.replay(w.conn, P) == {k: after[k] for k in rs.COORDINATES})
        check("1: the resolved commitment is no longer an open target, and its history stays readable",
              rc.commitment_statuses(w.conn, P)[0]["status"] == "verified_fulfilled" and rc.verifiable_commitments(w.conn, P, None) == [])

        # ── 2. refusals ──
        w = World()
        cid = w.promise()
        turn, span = w.say("Кодовое слово: АЛЬФА", "АЛЬФА")
        cases = {
            "no turn id": dict(turn=None, span=span), "no span": dict(turn=turn, span=None), "empty evidence": dict(turn=turn, span=span, evidence=""),
            "evidence differs from the record": dict(turn=turn, span=span, evidence="БЕТА"), "span shifted": dict(turn=turn, span=(0, 5), evidence="АЛЬФА"),
            "a turn with no source record": dict(turn="turn-unrecorded-1", span=span),
        }
        for label, kw in cases.items():
            t = kw.get("turn")
            r = rc.record_direct_fulfilment(w.conn, P, cid, kw.get("evidence", "АЛЬФА"), t, kw["span"])
            check(f"2: {label} -> refused, nothing written", not r["recorded"] and trust(w.conn) == 50.0 and len(w.conn.commitment_events) == 0, repr(r))
        other = w.promise("в следующем сообщении дам слово", user="someone_else")
        turn2, span2 = w.say("Кодовое слово: АЛЬФА", "АЛЬФА")
        check("2: another person's commitment cannot be verified by this person's turn",
              not rc.record_direct_fulfilment(w.conn, P, other, "АЛЬФА", turn2, span2)["recorded"])
        same = World()
        cid = same.promise()
        made_in = same.conn.commitments[cid]["source_turn_id"]
        own = same.conn.commitments[cid]["evidence"]
        check("2: a promise is not fulfilled by the very turn that made it (even with evidence that IS in that turn's record)",
              not rc.record_direct_fulfilment(same.conn, P, cid, own, made_in, (8, 8 + len(own)))["recorded"]
              and same.conn.interaction_turns[0]["user_text"][8:8 + len(own)] == own
              and rc.verifiable_commitments(same.conn, P, made_in) == [] and len(rc.verifiable_commitments(same.conn, P, "turn-later-0001")) == 1)
        check("2: a commitment made before provenance existed (no source turn: kind general) is never a target",
              rc.verifiable_commitments(FakeConnection(), P, None) == [])

        # ── 3. anti-farming, ceiling, negative history ──
        w = World()
        rewards = []
        for i in range(20):
            before = trust(w.conn)
            w.verify(w.promise(f"в следующем сообщении назову число {i}"), f"Число {i}", str(i))
            rewards.append(round(trust(w.conn) - before, 4))
        check("3: FARMING — twenty trivial verified promises raise trust by a small bounded total (< 5), never to a high value",
              50.0 < trust(w.conn) < 55.0, f"{trust(w.conn)} {rewards}")
        check("3: each further proof is worth less than the one before, and past a point nothing",
              all(rewards[i] >= rewards[i + 1] for i in range(19)) and rewards[0] == rs.OBSERVED_TRUST_BASE and rewards[-1] == 0.0, repr(rewards))
        check("3: every one of the twenty is still a verified event (history is not dropped because the reward is zero)",
              len([e for e in w.conn.commitment_events if e["event_type"] == rc.VERIFIED_KEPT]) == 20)
        check("3: replay of the audit trail equals the materialised state after the whole run", rs.replay(w.conn, P) == {k: rs.get_state(w.conn, P)[k] for k in rs.COORDINATES})
        check("3: a serious harm (insult 8) costs more than the FIRST reward and far more than the later ones",
              rs.INSULT_TRUST_PER_SEVERITY > rs.OBSERVED_TRUST_BASE and rs.INSULT_TRUST_PER_SEVERITY > sum(rewards[1:]))
        hi = World()
        hi.conn.inner_state[P] = {"user_id": P, "trust": 80.0, "respect": 50.0, "affection": 30.0}
        hi.verify(hi.promise())
        check("3: CEILING — a person already above it gets no further trust from trivial proofs (and never a loss)", trust(hi.conn) == 80.0)
        near = World()
        near.conn.inner_state[P] = {"user_id": P, "trust": 59.5, "respect": 50.0, "affection": 30.0}
        near.verify(near.promise())
        check("3: CEILING — near it the reward is cut to reach it exactly", trust(near.conn) == rs.OBSERVED_TRUST_CEILING)

        hurt = World()
        for _ in range(3):
            rs.record_insult(hurt.conn, P, 1.0)
        low = trust(hurt.conn)
        hurt.verify(hurt.promise())
        check("3: NEGATIVE HISTORY — after serious harm one small verified promise does not restore trust to neutral or high",
              low < 30.0 and low < trust(hurt.conn) < 50.0 and trust(hurt.conn) - low == rs.OBSERVED_TRUST_BASE, f"{low} -> {trust(hurt.conn)}")
        healed = World()
        rs.record_insult(healed.conn, P, 1.0)
        respect_lost = rs.get_state(healed.conn, P)["respect"]
        trust_lost = trust(healed.conn)
        rs.record_accepted_apology(healed.conn, P, 1.0, 1.0)
        check("3: APOLOGY != TRUST RESTORATION — an accepted apology gives back some respect and no trust",
              rs.get_state(healed.conn, P)["respect"] > respect_lost and trust(healed.conn) == trust_lost)

        # ── 4. deadlines and silence do not break promises ──
        late = World()
        cid = late.promise()
        late.conn.commitments[cid]["due_at"] = T0 - timedelta(days=30)
        st = rc.commitment_statuses(late.conn, P, now=T0)[0]
        check("4: DEADLINE PASSED != BROKEN — an overdue promise is only flagged, nothing is verified broken and trust is unchanged",
              st["overdue"] and st["status"] == "open" and trust(late.conn) == 50.0
              and not any(e["event_type"] == rc.VERIFIED_BROKEN for e in late.conn.commitment_events))
        import ast
        writes = [n for n in ast.walk(ast.parse(inspect.getsource(rc.record_direct_fulfilment)))
                  if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "record_commitment_event"]
        check("4: the only outcome the direct-fulfilment path can write is 'verified fulfilled' (never a broken one)",
              len(writes) == 1 and isinstance(writes[0].args[3], ast.Name) and writes[0].args[3].id == "VERIFIED_KEPT")

        # ── 5. boundaries: the model has no path to trust, trust is not epistemic ──
        params = list(inspect.signature(rc.record_direct_fulfilment).parameters)
        check("5: the write API takes a commitment, evidence and a turn — never a coordinate or a reward",
              params == ["conn", "user_id", "commitment_id", "evidence", "source_turn_id", "span"], repr(params))
        check("5: the reward is a pure function of how many proofs the person already gave",
              list(inspect.signature(rs.observed_trust_reward).parameters) == ["prior_observed"])
        import ast
        for mod in (rc, rs):
            imported = {n.module or "" for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.ImportFrom)} | \
                       {a.name for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.Import) for a in n.names} | \
                       {a.name for n in ast.walk(ast.parse(inspect.getsource(mod))) if isinstance(n, ast.ImportFrom) for a in n.names}
            check(f"5: RELATIONAL TRUST != EPISTEMIC TRUST — {mod.__name__} imports nothing from the belief / claim / source-authority world",
                  not any(re.search(r"belief|claim|epistemic|authority", name) for name in imported), repr(sorted(imported)))
        rc_src = inspect.getsource(rc.record_direct_fulfilment)
        check("5: a verified fulfilment writes no personal fact", "personal_fact" not in rc_src)

        # ── 6. the pre-v18 database: no provenance column -> nothing can be verified, nothing breaks ──
        old = World()
        cid = old.promise()
        for row in old.conn.commitments.values():
            row.pop("source_turn_id", None)
        check("6: rows without the v18 provenance column offer no verification target", rc.verifiable_commitments(old.conn, P, None) == [])

        # ── 7. MUTANTS: each is a real change to the source, and the invariants must catch it ──
        m1 = mutant(rc, [(
            "        source_turn_id=source_turn_id, span_start=span[0] if span else None, span_end=span[1] if span else None)\n    return result\n",
            "        source_turn_id=source_turn_id, span_start=span[0] if span else None, span_end=span[1] if span else None)\n"
            "    if result[\"recorded\"]:\n        relationship_state.record_observed_commitment(conn, user_id, 0)\n    return result\n")], "M1")
        check("M1: MUTANT 'a fulfilment claim moves trust (treated as verification)' is CAUGHT", "I1 a fulfilment claim was treated as verification" in invariants(m1), repr(invariants(m1)))
        m3 = mutant(rc, [("    if turn_text is None or turn_text[span[0]:span[1]] != evidence:\n        return result       # the evidence is not, byte for byte, in the immutable record of the turn it claims to come from\n", "")], "M2")
        check("M2: MUTANT 'evidence is taken from the caller, not checked against the recorded turn' is CAUGHT",
              any(v.startswith("I5") for v in invariants(m3)), repr(invariants(m3)))
        m5 = mutant(rc, [("    if VERIFIED_KEPT in kinds or VERIFIED_BROKEN in kinds:\n        return result       # already resolved: one commitment, one verified outcome\n    turn_text",
                          "    turn_text"),
                         ("    if result[\"recorded\"]:\n        result[\"reward\"]", "    if True:\n        result[\"reward\"]")], "M5")
        check("M5: MUTANT 'the same commitment can raise trust twice' is CAUGHT", any(v.startswith("I3") for v in invariants(m5)), repr(invariants(m5)))
        m4 = mutant(rs, [("           strict=True)", "           strict=False)")], "M4")
        check("M4: MUTANT 'the trust transition swallows its failure after the verified event was written' is CAUGHT",
              any(v.startswith("I8") for v in invariants(rc, m4)), repr(invariants(rc, m4)))
        m6 = mutant(rs, [("OBSERVED_TRUST_DECAY = 0.6 ", "OBSERVED_TRUST_DECAY = 1.0 "), ("OBSERVED_TRUST_CEILING = 60.0", "OBSERVED_TRUST_CEILING = 100.0")], "M6")
        check("M6: MUTANT 'repeated trivial promises farm trust (no diminishing returns, no ceiling)' is CAUGHT",
              any(v.startswith("I7") for v in invariants(rc, m6)), repr(invariants(rc, m6)))
        m9 = mutant(rc, [("commitment.get(\"kind\") != KIND_IN_CHAT or ", "")], "M9")
        check("M9: MUTANT 'an external commitment can be verified' is CAUGHT", any(v.startswith("I6") for v in invariants(m9)), repr(invariants(m9)))
        m4b = mutant(rc, [("    if not causal_events.may_apply(causal_events.claim(conn, user_id, source_turn_id, \"commitment_verified\", span)):\n        return result       # this turn already verified something (or this is a retry of it)\n", "")], "M4b")
        check("M3': MUTANT 'one evidence span may verify several commitments' is CAUGHT", any(v.startswith("I4") for v in invariants(m4b)), repr(invariants(m4b)))

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
