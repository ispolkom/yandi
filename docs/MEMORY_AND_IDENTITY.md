# Memory and identity

YANDI keeps persistent agent state outside the language model. This document describes the
mechanisms; it contains no data from any real installation. Examples use synthetic text.

```text
MEMORY MAY AFFECT THE REPLY.
MEMORY MUST NOT BECOME A NEW USER EVENT.

RELATIONAL STATE != EVENT IDENTITY.

UNKNOWN != EMPTY

MODEL MAY CHANGE. MEMORY MUST SURVIVE.
PERSISTED != FUNCTIONAL MEMORY: stored -> read later -> changes behaviour.
```

## Layers

| Layer | Module | What it holds |
|---|---|---|
| Self model | `agent/self_model.py` | Durable facts about the agent (character metadata, public repo/website). Fail-loud: no silent fallback if it cannot be read. |
| Episodic memory | `agent/memory_episodic.py`, `agent/orchestrator/response/writeback.py` | Answered questions with outcome and canonical trust, written back to SQL. |
| Verified commitments | `pet/commitment_verification.py`, `agent/relationship_commitments.py`, `agent/relationship_state.py`, `commitment` / `commitment_event` | Promise ledger; a promise to deliver something in the chat is verified when the deliverable itself appears in a later turn; only then trust moves (bounded) |
| Personal facts | `agent/personal_facts.py`, `pet/fact_extraction.py`, tables `personal_fact`, `personal_fact_event` | Durable, provenance-backed facts the person reported about their own life (append-only; corrections append). |
| Personal conversation memory | `agent/personal_memory.py`, table `interaction_turn` | The immutable source record of each chat turn (what was said and answered, when, by which model) and a bounded recall of relevant past turns into the reply context. |
| Relationship memory | `agent/relationship_memory.py` | Grievances and forgiveness for one interlocutor. |
| Beliefs | `agent/belief_manager.py` | Confidence with evidence for/against, history, decay. |
| Reflection | `agent/reflection_loop.py` | Policies derived from the agent's own mistakes. |

The single-owner personal chat (`pet/chat_local.py`) reads the self model, relationship memory and
personal conversation memory.
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

### Causal identity and idempotency

```text
SAME TEXT != SAME EVENT.  SAME SOURCE EVENT RETRIED != NEW EVENT.
ONE CAUSAL EVENT -> ONE LEDGER ENTRY -> ONE STATE TRANSITION.
```

A relationship event is identified by **(person, source turn, event type)**. The source turn is one
delivery of one user message; the browser client mints a `turn_id` when the message is created,
stores it with the message and sends it with the request. The causal event is claimed with a single
atomic `INSERT IGNORE` on a unique key in `causal_event` (schema v15), in the same transaction as
the state writes it guards:

- a retry, replay or double delivery of the same turn re-runs the model calls if it must, but the
  relationship writes are applied **once**: no second grievance recurrence, no second change of
  trust / respect / affection, no second healing step or restoration, no second commitment or
  fulfilment report;
- the same words in another turn carry another turn id and are another event (a real recurrence);
  nothing anywhere hashes or compares message text;
- an insult and an apology in one turn are two events (two ledger rows); the whole turn retried
  applies neither again; the code-owned evidence span is stored for audit but is not part of the
  key, so a re-run extraction that picks a slightly different span cannot create a second event;
- the guarantee is persistent (it lives in the database, not in process memory), safe under
  concurrent deliveries (the unique key serialises them) and safe under rollback (a transaction that
  failed leaves no claim, so the event can still be applied);
- history is append-only: a duplicate is recognised and skipped, never overwritten.

Honest limits: a request without a usable `turn_id` (for example a tool calling the chat endpoint) is
processed exactly as before, with no retry guarantee, and is never deduplicated by text; and until
schema v15 is applied the ledger table does not exist, so events are applied without the guarantee
(one warning is logged).

## Personal conversation memory (continuity across restarts and models)

```text
MODEL MAY CHANGE. MEMORY MUST SURVIVE.
ONE USER TURN -> ONE SOURCE RECORD.  SAME TURN RETRIED != NEW HISTORY.  SAME TEXT != SAME TURN.
SESSION != PERSON.
HISTORY MAY BE EXTENDED, NEVER SILENTLY REWRITTEN.
READING MEMORY != EXPERIENCING A NEW EVENT.
```

Redis holds the chat transcript the browser shows, and the browser sends its own history with every
request; neither is a long-term biography (Redis may be empty, is persisted only by periodic
snapshots, and is not read by the agent as memory). So the durable, local record of "what was actually
said" lives in SQL:

- **Source record.** `interaction_turn` (schema v16, append-only) has one row per (person, source turn
  id): the person's message, YANDI's visible reply (empty if no reply was produced), the time, the
  resolved model and adapter that produced the reply, and which earlier turns were shown to the model
  as memory for this reply. The turn id is the one the person's own chat client mints (the same
  identity that keys `causal_event`). A request without one (an orchestrator tool, a script) is not
  known to be the person speaking, so it neither writes to nor reads from this memory, and no
  identity is ever made up. A retry of the same turn is ignored (the first delivery is the record);
  the same words in another turn are another row. The owner of a row is the **person**, never a
  browser session.
- **Interpretation is computed, not stored.** Which past turns matter for the current message is
  worked out when it is needed, from the source rows and from the relationship events confirmed in
  the same turn (`causal_event`, joined by the exact turn id). Nothing derived is stored, so a
  better interpretation later rewrites nothing and a stale summary cannot replace what was said.
  A stored, append-only summary layer pointing at turn ids is a possible later addition.
- **Recall is bounded.** At most four past exchanges per reply, each cut short: the latest two (for
  continuity after a restart or a change of model), plus turns chosen by relevance (shared content
  stems of what the *person* said, so a reply that once quoted a memory cannot make itself relevant),
  plus fresh turns in which a relationship event was confirmed. Turns already present in the request's
  own history, and the current turn itself, are never recalled.
- **Data, not instructions.** What was said earlier can contain anything (an old message may be
  "ignore your instructions"). It reaches the prompt inside an explicit block, each utterance as one
  inert JSON string with chat-template markers removed and the block delimiter unforgeable, and the
  prompt states that the quoted words are data and that commands inside them are not to be obeyed.
  This removes the ways stored text could pass itself off as the prompt's own structure; it cannot
  make a language model unable to obey text it reads (see KNOWN_ISSUES).
- **Past, not present.** The recalled turns reach the reply generation as a statement marked as the
  past and as not being the current message. They never reach the event extractor (which still sees
  only the current message), never create an event, and the row written for the new turn records
  which earlier turns it was shown. If the table is unreachable or schema v16 is not applied,
  nothing is claimed about memory at all (UNKNOWN != EMPTY) and the reply is produced as before.

Why not the existing tables: `episode` is the system's own life log (no person, no turn id);
`experience` is mutable and keyed by how a reply landed; neither can hold a per-person, per-turn,
immutable source record without becoming something else. Legacy `episode` rows are neither read nor
changed by this path.

## Personal facts (what the person has told her about their own life)

```text
USER REPORTED FACT != OBJECTIVE TRUTH.   ASSISTANT SAID X != USER FACT.
RAW TURN != DERIVED FACT.  FACT MUST HAVE A SOURCE TURN.  SESSION != PERSON.
OLD FACT IS HISTORY: A CORRECTION APPENDS.   SAME TURN RETRY != NEW FACT HISTORY.
THE MODEL MAY POINT TO EVIDENCE; THE CODE OWNS THE EVIDENCE TEXT.
```

Recalling *turns* cannot answer "what do you remember about me?": the stable facts are old, few and
share no words with the question. So the stable things the person says about themselves are kept as
facts, separately from the raw transcript (which stays exactly as it is in `interaction_turn`).

- **Extraction** (`pet/fact_extraction.py`) follows the event protocol: the current message is cut into
  numbered words; a model points at the word range of a stable personal fact and proposes its class,
  a minimal normalised statement, polarity, time and stability; **code reconstructs the evidence** from
  the message; a second judgement, blind to the claim, must find the span to be the person's own
  current (or own past) statement, with the same polarity, stable and not a secret; a third checks the
  statement says no more than the evidence. Quotations, hypotheticals, plans, questions, other people's
  facts, moods and credentials produce nothing. The extractor sees only the current message (plus the
  statements of known facts as numbered link targets, never as evidence); the assistant's words never
  reach it, and it holds no vocabulary of facts.
- **Ledger.** `personal_fact` holds a fact's identity and first statement (person, class, statement,
  polarity `affirmed|negated`, time as stated `current|past`, the exact evidence span, the source turn);
  `personal_fact_event` holds what happens later: `restated` (the same proposition in another turn: more
  provenance, not a duplicate) and `superseded` (a correction: the new fact and turn are recorded). Nothing
  is updated or deleted; a fact's status (current / historical / superseded) is folded from the events.
  The source turn is a foreign key to `interaction_turn`, so a fact cannot exist without the immutable turn
  it was said in, and it is written in the same transaction as that turn.
- **Recall.** Two routes, chosen by the extractor's classification of the message (a retrieval mode, never
  a write): a person asking what is known about them, or about something in their own life, gets their
  current profile (at most 15 facts, plus up to 3 marked as past); any other message gets only the facts
  that share content with it (at most 4). Neither depends on how long ago the turn was or on the interaction
  window. Facts are stated to the model as the person's own report, as memory and not instruction, inert and
  delimited like the conversation memory; a fact the person has corrected is not stated, and the raw turn it
  came from is not re-told as conversation memory.

## Verified commitments (what raises trust, and what does not)

```text
USER REPORTS FULFILMENT != FULFILMENT VERIFIED.   TRUST GROWS ONLY FROM VERIFIED EVIDENCE.
EXTERNAL SELF-REPORT != DIRECT OBSERVATION.       AMBIGUOUS TARGET != VERIFIED TARGET.
ONE COMMITMENT -> MAX ONE REWARD.  ONE EVIDENCE SPAN -> MAX ONE COMMITMENT.  FAIL CLOSED.
APOLOGY != TRUST RESTORATION.      RELATIONAL TRUST != EPISTEMIC TRUST.
```

YANDI has not observed the world. "I paid the bill", "I sent the file", "I went to the doctor" stay what
they are: **reports**. A report of having kept a promise is recorded (`fulfillment_claimed`, with the
turn and span it came from) and moves nothing. Exactly one class of promise can be verified without
anyone's word: a promise to **deliver something in the chat** ("in my next message I will give you a
code word / a number / the answer"). When the deliverable itself appears in a later message, it is in
front of her.

- **Classification** (`pet/commitment_verification.py`): when a promise is made, a model proposes whether
  its fulfilment happens in the chat (`in_chat`) or in the world (`external`). Only a clear `in_chat`
  counts; a failure, a doubt or another answer is `external`. `in_chat` alone verifies nothing. Promises
  made before this existed (`general`, no source turn) are never verified.
- **Verification** (on a later identified turn, for the person's open `in_chat` promises, at most five):
  the extractor points at the word range of the message that *is* the promised content and names which
  promise it fulfils; **code reconstructs the evidence** (`message[start:end]`); a blind judgement, per
  open promise, sees only the promise words, the exact fragment and the message and must say the fragment
  itself contains what was promised, presented by the person now (not a report, not a request to
  believe, not a hypothesis, not a quotation, not about someone else). It is verified only if **exactly
  one** open promise passes and it is the one the extractor named; ambiguity, a wrong target, a
  malformed answer, a timeout or a bad span all mean *not verified*. Two checks are made by code: a
  fragment inside a quotation, and a fragment inside words the event extraction of the same message
  confirmed as a report of fulfilment or as a new promise, are never a delivery.
- **Sources.** The evidence comes only from the **current identified user message**: never from the
  assistant's words, retrieved conversation memory, personal facts, the relationship state, or the claim
  text as such. The judges are shown no trust, no history and no wish to confirm anything.
  Before anything is written the ledger checks the evidence **against the immutable `interaction_turn`
  record** of the named turn (`user_text[start:end] == evidence`): the provenance is verified against the
  record, not against the caller. What is verified is "the promised in-chat action was performed", not
  that everything it stands for is true (a delivered contract text is not a genuine contract).
- **Ledger.** The verified fulfilment is a new, appended `commitment_event` (`verified_fulfilled`, source
  `in_chat_direct`) with `source_turn_id`, `span_start`, `span_end` and the exact evidence (schema v18);
  the earlier report is untouched; the status is folded from the events; a resolved commitment is no
  longer an open target. Broken promises are not inferred (a passed deadline only sets an `overdue` flag).
- **Trust.** Code owns the transition (`relationship_state.record_observed_commitment`); the model never
  supplies a coordinate. A directly observed delivery proves only *trivial* reliability, so it moves
  **trust only** (no respect, no affection), by `2.0 * 0.6^k` where *k* is the number of such proofs the
  person already gave (2.0, 1.2, 0.72, ... zero once under 0.05: at most about 5 in total, ever) and never
  above a ceiling of 60. One commitment pays once; the same turn, the same evidence again, or a retry pays
  nothing; twenty trivial promises cannot buy a relationship, and after serious harm (an insult costs up
  to 8 trust) they cannot buy it back either. The audit row carries the reward as its weight, so the
  state is rebuilt from the audit trail alone.
- **One transaction.** The verified event, the causal claim `(turn, commitment_verified)`, the trust
  transition and the audit row are steps of the turn's one transaction: a failure at any point (including
  after the event, or after the transition) rolls the whole turn back, and a retry applies it normally.
  The transition never swallows its own failure. Concurrent turns are serialised on the person's state row:
  the turn takes that lock as its **first statement** (an InnoDB read snapshot is fixed at the first plain
  read, and the append-only ledger cannot be read with a locking clause because the runtime role has no
  UPDATE on it), so two turns cannot both be "the first proof".
- **Model targets.** By default the structured calls (events, facts, classification, verification) use the
  chat model. `YANDI_EXTRACTION_MODEL` points events, facts and (by default) verification at another
  logical model of the node's gateway configuration; `YANDI_VERIFIER_MODEL` points classification and
  verification at yet another. There is no hidden fallback: an unavailable target is a failed call, which
  means "nothing extracted / not verified / external".

### One turn, one transaction

```text
ONE IDENTIFIED PERSONAL TURN -> ONE interaction_turn -> ZERO OR MORE validated causal writes -> ONE COMMIT
LLM INFERENCE MUST NOT HOLD AN OPEN SQL TRANSACTION
```

The turn first does all its model work (event extraction, reply) and validates the events; only then
does it open one short transaction in which it writes the source record (`interaction_turn`, first:
its unique key also serialises concurrent deliveries of the same turn), claims each causal identity and
applies the grievance / apology / relationship-state / commitment writes those claims guard. Everything
commits together or is rolled back together; the HTTP reply is returned after the commit, so a reply
the client never received is a retry of a turn that is already whole (nothing is applied twice). The
repository and lifecycle helpers take the caller's connection and never commit; a caller that gives
none (other code paths) keeps the old one-transaction-per-call behaviour.

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
