# Known issues

Recorded, not hidden. None of these are fixed by the documentation change that introduced this
file.

## Event evidence recall

The current-event provenance guard requires the model to quote, verbatim, the part of the *current*
user message that shows an insult or apology. This removes false events created from memory or
history, but the model often omits or invents the quote, so some genuine insults and apologies are
dropped. The guard is fail-safe (an event may be missed, never fabricated). Improving recall is open
work and must not weaken the provenance check.

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
