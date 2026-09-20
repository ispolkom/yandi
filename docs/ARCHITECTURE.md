# Architecture

This document separates what exists in the code today from what is direction only. For the
per-component details see [INFERENCE_GATEWAY.md](INFERENCE_GATEWAY.md),
[MEMORY_AND_IDENTITY.md](MEMORY_AND_IDENTITY.md) and [EPISTEMIC_CORE.md](EPISTEMIC_CORE.md).

## The central idea

```text
LLM != YANDI
```

A YANDI instance is a persistent agent. The language model is one replaceable mechanism it uses to
think and speak. Identity, memory, beliefs and relationships are stored by the agent itself, so
changing the model, restarting the process, or clearing a context window does not erase them.

## Layers

```text
                      YANDI instance

   ┌──────────────────────────────────────────────────┐
   │  PET / agent            (pet/, agent/)           │
   │  personal chat · orchestrator · reflection       │
   └───────┬───────────────────────────────┬──────────┘
           │                               │
           │ persistent state              │ generation
           ▼                               ▼
   ┌───────────────────┐        ┌─────────────────────────┐
   │ SQL state layer   │        │ llm_gateway             │
   │ self · episodes   │        │ resolve_target()        │
   │ relationships     │        │   → ResolvedInference   │
   │ beliefs · claims  │        │     Target              │
   │ reflection        │        │   → Adapter             │
   └───────────────────┘        └───────────┬─────────────┘
                                            ▼
                             runtime / network endpoint / provider
                    (llama.cpp · Ollama-compatible · OpenAI-compatible · Anthropic)

   ┌──────────────────────────────────────────────────┐
   │  P2P node (node/, Rust) ── loopback bridge ──►   │
   │  peers · DHT · AI-RPC          llm_gateway       │
   └──────────────────────────────────────────────────┘
```

## Components

| Component | Path | Role | State |
|---|---|---|---|
| PET server | `pet/` | FastAPI server. `chat_local` is the personal single-owner chat with persistent relationship state; the council/orchestrator tabs are separate modes. | Implemented |
| Orchestrator | `agent/orchestrator*`, `agent/orch_*` | Question → claims → evidence retrieval and validation → synthesis with a trust label → write-back. | Implemented |
| Inference gateway | `llm_gateway/` | Resolves a logical model to one target and adapter, enforces an output contract, returns a normalized result. | Implemented |
| State layer | `agent/db/sql/` | Dedicated MySQL-compatible instance with tiered tables (immutable history, current-state projections). Access goes through repositories and fail-open "shadow" wrappers where a DB outage must not break a reply. | Implemented |
| Relationship memory | `agent/relationship_memory.py` | Grievance/forgiveness lifecycle and apology matching. | Implemented |
| Beliefs, reflection, self model | `agent/belief_manager.py`, `agent/reflection_loop.py`, `agent/self_model.py` | Confidence-tracked beliefs, lessons from mistakes, durable self facts. | Implemented; partly wired into the live chat path |
| P2P node | `node/` | Rust node: encrypted transport, discovery, proxy layers, web UI, AI-RPC. | Experimental |

## One turn of the personal chat

1. The current user message and history go to `llm_gateway.complete_semantic()`, together with the
   agent's self facts and (as historical context only) a remembered grievance.
2. The gateway resolves exactly one target and adapter, picks the strongest output contract that
   target supports, and returns `reply` and an optional internal `state`.
3. The caller validates `state` against the **current** user message (provenance check) before it
   may become a memory event. Only `reply` is shown to the user.
4. A validated insult creates a grievance; a validated apology is matched to at most one existing
   grievance and advances only that one.

## Storage and privacy stance

The design is local-first: state lives in a database on the operator's machine, the personal chat
server binds to loopback by default, and inference can run entirely on local weights. External
providers are opt-in per model and their keys are referenced by environment-variable name. This is a
design intent for an experimental system, not a security guarantee.

## What is not built yet

- Embeddings do not yet fully go through `ResolvedInferenceTarget` and an adapter.
- Identity is single-owner in the personal chat; per-visitor identity is not implemented.
- `canonical_trust` is computed in shadow mode next to the live trust value; they are not unified.
- Cross-node trust as a graph and further adapter types are direction, not features.

See [KNOWN_ISSUES.md](KNOWN_ISSUES.md).
