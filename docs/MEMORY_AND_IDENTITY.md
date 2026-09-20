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

### Continuous relationship state

On the personal-chat path the continuous relationship state is `forgiveness_capacity` (0–100,
default 50), owned by `relationship_memory`. Causation runs one way, **event → state**: a new
offense lowers it by 10 × severity, a recurrence by 10 × the severity it added, an accepted apology
raises it once per offense cycle, and forgiveness raises it further. Reading it never writes an
event. It affects later behaviour in two ways: it is stated to the model as a plain fact in the
memory context, and it gates forgiveness (too low a capacity blocks it however much time has
passed). The regression tests check both counterfactually: the same message with a damaged versus a
neutral relationship yields a different context, and the same apology at the same time forgives in
one case and not in the other.

`agent/inner_state.py` / `agent/character_engine.py` keep a separate, richer scalar model (trust,
respect, patience, affection, forgiveness) that is used only by the orchestrator, keyed by session
id and driven by keyword detectors; it is not connected to the personal chat. See
[KNOWN_ISSUES.md](KNOWN_ISSUES.md).

### Current-event provenance

The model produces its reply and a small state object in one generation. The state may claim "the
user just insulted me / apologised". That claim is only accepted as a **new event** if:

- the state is well-formed (severity/sincerity numeric and within 0..1), and
- the `evidence` quote it supplies occurs verbatim in the **current** user message.

Otherwise the event is dropped (fail-safe: an event may be missed, but is never fabricated). This is
a structural check, not a second classifier. Its purpose is exactly the invariant above: a
remembered insult or an apology earlier in the history may colour the reply, but must not be
re-recorded as something the user just did. Trade-off: recall of genuine events is lower than
without the guard (see [KNOWN_ISSUES.md](KNOWN_ISSUES.md)).

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
