# Memory and identity

YANDI keeps persistent agent state outside the language model. This document describes the
mechanisms; it contains no data from any real installation. Examples use synthetic text.

```text
MEMORY MAY AFFECT THE REPLY.
MEMORY MUST NOT BECOME A NEW USER EVENT.

RELATIONAL STATE != EVENT IDENTITY.

UNKNOWN != EMPTY
```

## Layers

| Layer | Module | What it holds |
|---|---|---|
| Self model | `agent/self_model.py` | Durable facts about the agent (character metadata, public repo/website). Fail-loud: no silent fallback if it cannot be read. |
| Episodic memory | `agent/memory_episodic.py`, `agent/orchestrator/response/writeback.py` | Answered questions with outcome and canonical trust, written back to SQL. |
| Relationship memory | `agent/relationship_memory.py` | Grievances and forgiveness for one interlocutor. |
| Beliefs | `agent/belief_manager.py` | Confidence with evidence for/against, history, decay. |
| Reflection | `agent/reflection_loop.py` | Policies derived from the agent's own mistakes. |

The single-owner personal chat (`pet/chat_local.py`) reads the self model and relationship memory.
Other layers are stored and used by the orchestrator, and are only partly wired into that chat path;
see [KNOWN_ISSUES.md](KNOWN_ISSUES.md).

## Relationship memory

A **grievance** records an event the agent took offence at: `event_type`, `description` (the
triggering text), `severity`, `status`, timestamps. Its lifecycle:

```text
registered → acknowledged → understood → healing → forgiven | unforgiven
```

- A sincere-enough apology advances a grievance to `understood`; a plain low-sincerity one only to
  `acknowledged`.
- Forgiveness additionally needs a minimum time of **healing**, enough remaining *forgiveness
  capacity*, and not too many other unforgiven grievances. A simple "sorry" is deliberately not
  enough.
- Two clocks are kept apart: the age of the **offense** (`created_at`, or the last recurrence) and
  the age of the current **healing phase**, which starts when an apology is first *accepted*
  (understood) in the current offense cycle. An old grievance apologised for today starts healing
  now; a plain low-sincerity "sorry" does not start it; a recurrence of the offense clears the
  cycle so an earlier apology cannot forgive the new offense.
- An open grievance is any not yet `forgiven`/`unforgiven`.

### Relationship state (how she currently stands towards this person)

```text
EVENTS WRITE STATE.
STATE DOES NOT INVENT EVENTS.
THE LLM DOES NOT WRITE RELATIONSHIP STATE.
```

`relationship_memory` is the **biography** (what happened, when, healing). `agent/relationship_state.py`
holds the **current stance**, four coordinates with different meanings:

| Coordinate | Meaning | Moved by |
|---|---|---|
| `trust` | expectation that this person will not harm or deceive her | insults (down). Not restored by an apology: trust needs later behaviour, and no such event is validated yet. |
| `respect` | how much their conduct keeps their worth to her as a conversation partner | insults (down, hardest); an accepted apology gives back a bounded share of what that offense cost |
| `affection` | how dear the interaction with this person is; slow to grow | insults (down slightly) |
| `forgiveness_capacity` | how much recovery from harm is currently possible | offenses and recurrences (down), an accepted apology (up once per offense cycle), forgiveness (up); it also gates forgiveness |

Only lifecycle events that were already validated upstream (a provenance-checked insult, an accepted
apology) call the deterministic event → delta rules; those constants are the persona's dynamics.
The model never supplies a coordinate (its output schema has none), and reading the state never
writes an event. The state is stated to the model as qualitative bands, not numbers (a raw figure
was recited in live replies). An `inner_state_event` row records why each change happened.

Storage is the person's row (`"owner"`, never a session id) in the existing `inner_state` table,
because a dedicated table needs DDL rights the runtime database user does not have; it is confined
to two functions in `relationship_state.py`. The counterfactual tests check that changing or removing
this state changes the next prompt, and that the same apology forgives or not depending on it.

`agent/inner_state.py` / `agent/character_engine.py` remain a separate, orchestrator-only model keyed
by session id and driven by keyword detectors; it is not connected to the personal chat. See
[KNOWN_ISSUES.md](KNOWN_ISSUES.md).

### Commitments: trust built on verifiable behaviour

`agent/relationship_commitments.py` is an immutable promise ledger (`commitment`,
`commitment_event`; status is folded from the events, history is never rewritten):

```text
promise made -> open -> fulfillment_claimed (the person's report) -> verified_fulfilled | verified_broken
```

```text
USER SAID "I did it"  !=  YANDI KNOWS it was done.
ONE CAUSAL EVENT -> ONE STATE TRANSITION.
```

- A **report** is recorded but moves nothing. Only an outcome established by a **verifier**, that is
  by something independent of the person's own words, changes `trust` (up a lot), `respect` (up)
  and leaves `affection` alone; a verified broken promise hits trust hardest. No verifier is wired
  into the personal chat yet, so today the live path records promises and reports but does not
  move trust.
- In the personal chat a promise or a claim is an event only if the extraction step confirms it from
  the current message (the same evidence rules as insults and apologies). Promise and claim are
  accepted only when they are the only event in the turn,
  so a grounded apology cannot vouch for a promise the model merely remembered. The claim is linked
  to one open promise chosen before the reply; the reply's memory context states that promise, or
  the ambiguity, or an earlier unverified report, as plain facts.
- A missed deadline is only a derived `overdue` flag. It never breaks a promise by itself.
- A claim is linked to a **specific** promise (content overlap, or the only open one). With several
  candidates and no clear winner it is ambiguous and nothing is written.
- `UNIQUE (commitment_id, event_type)` plus "apply the transition only if the row was new" means
  the same fulfilment cannot raise trust twice.
- The audit trail carries each event's machine-readable magnitude, so `relationship_state.replay()`
  rebuilds trust, respect and affection from the trail alone.

### Current-event provenance and event extraction

```text
THE MODEL MAY CHOOSE THE EVIDENCE LOCATION.
THE CODE OWNS THE EVIDENCE TEXT.
NO EXACT SUPPORT IN THE USER'S MESSAGE = NO EVENT.
```

A remembered insult or an apology earlier in the history may colour a reply, but must not be
re-recorded as something the user just did. Relationship events are therefore recognised by a step
that is separate from the reply (`pet/event_extraction.py`):

1. The current message is cut into numbered words. The extractor sees **only** that message, never
   memory or history.
2. For each candidate event it returns the **word range** that constitutes it, then the event type
   (plus severity for an insult and sincerity for an apology). It never returns quote text.
3. Code validates the references (integers, in range, non-empty, bounded) and reconstructs the
   evidence itself as a slice of the message, so it is literally part of the message.
4. A second judgement about that exact span, **blind** to what the extractor claimed, says which act
   the fragment expresses and in what frame (performed now by the user, a quotation, a hypothetical,
   a negation, a report about the past, a topic). The event is admissible only if the act equals the
   extracted type and the frame is "performed now".
5. Malformed output, invalid references, overlapping spans, disagreement or a failed call all mean
   no event (fail closed).

The recogniser holds no vocabulary of insults, apologies or promises; the string operations in the code
are segmentation, offset validation and exact reconstruction. It never changes trust, respect or
affection, never creates grievance transitions and never verifies that a promise was kept.
Why it replaced the earlier design (reply and events in one generation, with a quote supplied by the
model): the model classified events correctly but almost never copied the quote verbatim, so nearly
every real event was rejected. See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for measured rates and the
remaining failure modes.

### Relationship focus: one causal target for reply and write

Before the reply is generated, `resolve_relationship_focus` decides which open grievance (if any)
the **current** user message is about, using the selection policy below. The prompt's memory
context is built around that grievance (or states that the message does not single one out, or
that the matter is already settled), and a later apology changes **only that grievance**. So the
visible reply and the persistent transition share one causal target. The focus is only a *target*:
it never decides that a message is an insult or an apology. That still comes from the model's
validated state, so a missed event stays missed and memory still cannot create an event.

### Apology → grievance matching

**A valid apology does not imply the heaviest grievance is its target.** An apology is about a
specific event, so the target is chosen deterministically, without a second model call
(`match_grievance_target`, `resolve_relationship_focus`, `apply_apology` in
`agent/relationship_memory.py`):

1. **Explicit reference.** Content words named by the apology (after removing apology/filler words)
   overlap an open grievance's description. Best overlap wins. Equal overlap: most recent offense,
   then higher severity, then id. Never random.
2. **Names something already resolved.** If the apology overlaps a resolved (`forgiven`/
   `unforgiven`) grievance better than any open one, there is no target. It is not redirected to an
   unrelated open grievance and the resolved one is not reopened.
3. **Generic apology** (for example a bare "sorry"): no causal link is invented. One open grievance
   → that one. Otherwise exactly one open grievance whose offense is recent (within one hour) →
   that one. Otherwise the apology is ambiguous: no target and no state change.
4. **No open grievance:** no target; an apology never creates one.

At most one grievance changes per apology. Severity is only a late tie-break. Affection, trust and
forgiveness capacity are not inputs to matching.

Synthetic illustration: with an old severe grievance ("example insult A") and a recent mild one
("example insult B"), the apology "sorry I called you that just now" resolves to B if it names B's
words or if B is the only recent one; naming A's words resolves to A even when B is fresher. The
same apology text against a different set of grievances selects a different target — the
regression tests assert this counterfactual.

## Error handling stance

Relationship writes go through "shadow" wrappers in `agent/db/sql/shadow_write.py` that are
**fail-open**: if the database is unreachable the reply is still produced, with no memory update. A
failed read is reported as *unknown* rather than *empty*, so the prompt never states "no open
grievances" merely because the database was down.
