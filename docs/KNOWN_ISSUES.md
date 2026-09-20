# Known issues

Recorded, not hidden. None of these are fixed by the documentation change that introduced this
file.

## Event evidence recall

The current-event provenance guard requires the model to quote, verbatim, the part of the *current*
user message that shows an insult or apology. This removes false events created from memory or
history, but the model often omits or invents the quote, so some genuine insults and apologies are
dropped. The guard is fail-safe (an event may be missed, never fabricated). Improving recall is open
work and must not weaken the provenance check.

## Reply / grievance target mismatch

The reply is generated before apology matching and is shown the most salient (highest-severity)
open grievance as historical memory. The apology matcher may select a different grievance as the
target. The visible reply can therefore refer to one grievance while the state change applies to
another.

## Healing clock semantics

Minimum healing time is computed from the grievance's creation time, not from the apology or
healing event. A sincere apology about an old grievance can therefore satisfy the time condition
immediately.

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
- `deploy/` (installer and systemd units) and several node READMEs contain paths and host
  assumptions from the reference deployment; treat them as a worked example, not a portable
  installer.

## Trust is computed twice

The user-visible trust label and the stricter epistemic "trust gate" are separate computations; the
canonical trust is shadow-only. See [EPISTEMIC_CORE.md](EPISTEMIC_CORE.md).

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
- No top-level `LICENSE` file exists; see the README.
