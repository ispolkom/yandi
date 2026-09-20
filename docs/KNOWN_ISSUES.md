# Known issues

Recorded, not hidden. None of these are fixed by the documentation change that introduced this
file.

## Event recognition: measured rates and remaining failure modes

Relationship events are recognised by `pet/event_extraction.py` (the model chooses where the
evidence is; code reconstructs and owns the evidence text; a blind check on the exact span must
agree). It replaced a design in which the reply generation returned the events together with a quote
it had to copy verbatim: the model classified events correctly but almost never produced the quote,
so only 0-1 of 10 real insults or apologies and 0 of 10 promises or claims got through.

Measured with the local `heretic:q8` model (temperature 0, one run per message) on three synthetic
message sets, 28 positives per event type and 126 negatives (neutral, resembling relational language,
somebody else's words quoted, hypotheticals, negations, retrospective mentions):

| Event | Recognised by the extractor | Passed evidence + blind check (written) | Before |
|---|---|---|---|
| insult | 28/28 | 28/28 | 0-1/10 |
| apology | 28/28 | 28/28 | 0/10 |
| promise | 25/28 | 17/28 | 0/10 |
| claim of fulfilment | 27/28 | 20/28 | 0/10 |

False events (an event on a negative message) and wrong-type events (a different event than the message
contains): **0** in all 154 messages.

A stronger or dedicated model was not needed: `Rocinante-X-12B` under the same protocol was worse
(insult 2/8, claim 1/8). The prompts were tuned on two of the three sets, so treat the numbers as
indicative; the third set (never tuned on before the final protocol) gave 8/8, 8/8, 4/8 and 6/8.

Remaining failure modes:

- **Promise and claim recall is lower** (17/28 and 20/28). Misses fail closed: the extractor or the
  blind check declines, or a claim such as "Отчёт готов" is judged a report about the past.
- **The blind check is a model too.** It is independent of the extractor's claim and code compares
  the two, but correlated model errors could still confirm a wrong event. Zero were observed in 126
  negatives; the sample is small. One such case appeared during development (a one-word fragment
  confirmed as the wrong commitment type) and led to the rule that a commitment event is admitted only
  as the sole candidate the extractor proposed; a mixed message therefore loses its commitment event.
- **Coverage.** Messages longer than 120 words, or in languages the model handles poorly, produce no
  events. A one-word message needed the word count in the prompt to avoid an off-by-one reference.
- **Cost.** One extra model call per turn (and one per candidate event).
- **Retries.** With a client-minted `turn_id` a repeated delivery of the same turn is applied once
  (see *Causal identity and idempotency*); a request without one has no such guarantee.

## Embedding routing

Embeddings do not yet fully use `ResolvedInferenceTarget` and an adapter. Embedding calls still take
a partly separate path.

## Legacy inference configuration

The encrypted config store still supports the legacy `backend` / `protocol` / `base_url` entry
shape, which is normalised into a resolved target at resolution time. A native adapter-based
configuration shape is not defined yet.

## Machine-specific defaults

- The built-in default model registry in `llm_gateway/llamacpp_backend.py` points at a GGUF file
  path on the reference machine. On other machines, configure a model with
  `python -m llm_gateway.setup`.
- Some Rust node code (`node/src/web/server.rs`, `node/src/communication/*file_transfer.rs`) and a few
  scripts/tests still contain absolute reference-machine paths (uploads, downloads, cache and
  interpreter locations). They should become configurable.
- `deploy/` (installer and systemd units) and several node READMEs contain paths and host
  assumptions from the reference deployment; treat them as a worked example, not a portable
  installer.

## Trust is computed twice

The user-visible trust label and the stricter epistemic "trust gate" are separate computations; the
canonical trust is shadow-only. See [EPISTEMIC_CORE.md](EPISTEMIC_CORE.md).

## Callers without a turn id

The chat endpoint is also called by an orchestrator tool (`agent/tools/tool_ai.py`) that sends a
prompt with no `turn_id`. Such a call is processed like any owner message (its text can be run
through event extraction) and has no retry guarantee. Restricting relationship writes to identified
owner turns would be the safer rule; it is not done yet.

## Commitments need a schema migration and a verifier

The promise ledger and the causal-event idempotency ledger add three tables (`commitment`,
`commitment_event`, `causal_event`, schema v15). The runtime
database user cannot create tables, so an administrator has to apply the migration once
(`python -m agent.db.sql.migrate` with DDL rights); until then the ledger is inert and the personal
chat behaves as before. Even after that, trust only moves for **verified** outcomes and no verifier
exists in the personal chat yet, so reported fulfilment is remembered but does not change trust.

## Split subject: two relationship models

The personal chat's relationship state is `agent/relationship_state.py` (trust, respect, affection,
plus `forgiveness_capacity`), moved only by validated events. The orchestrator has a separate scalar
model in `agent/inner_state.py` / `agent/character_engine.py` keyed by **session id** and driven by
**keyword detectors**. The same YANDI can therefore hold two different stances depending on which
door a message came through. The orchestrator side has not been migrated: a session is not a person,
and a keyword hit is not a validated event.

Related limits of the personal-chat state: it starts at the defaults for the existing owner (past
grievances are not replayed into it, so test residue cannot skew it); there is no validated event
yet that could raise trust or affection; and it lives in the existing `inner_state` table until
someone with DDL rights adds a dedicated table.

## Partially wired subsystems

Beliefs, reflection, the scalar "inner state" and character engine, and the legacy JSON forgiveness
model are not all connected to the personal chat path. `relationship_memory` (event-based) is the
one used by PET.

## Single-owner personal chat

`pet/chat_local.py` treats whoever is in the chat as the single owner. There is no per-visitor
identity or per-user relationship state yet.

## Repository hygiene

- Root-level `YANDI_*_AUDIT.md` / `*_REPORT.md` files are dated development history and contain
  reference-machine paths. They are indexed in [README.md](README.md) and were not moved because
  source comments refer to them by name.
- `node/mobile/` contains a Flutter client whose local build caches are no longer tracked.
- No top-level `LICENSE` file exists. `node/Cargo.toml` declares MIT for the Rust crate only; the
  license of the rest of the project is undecided (see the README).
