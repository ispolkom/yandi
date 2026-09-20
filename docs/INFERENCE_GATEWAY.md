# Inference gateway (`llm_gateway/`)

```text
Ollama is an adapter, not the architecture.
```

## Invariants

```text
MODEL != LOCATION != RUNTIME != ADAPTER != PROVIDER

ONE GENERATION ATTEMPT = ONE RESOLVED INFERENCE TARGET = ONE ADAPTER

UNKNOWN != SUPPORTED          (a capability not declared is not assumed)

VISIBLE REPLY != INTERNAL STATE
```

- A **model** is a logical name a caller asks for.
- A **location** is where the weights or endpoint are (a file path, a URL).
- A **runtime** is what executes it (in-process llama.cpp, an external server, a hosted provider).
- An **adapter** is the code that speaks to that runtime.
- A **provider** is who operates it.

None of these is inferred from another. `heretic:q8` says nothing about Ollama; a URL says nothing
about which wire protocol it speaks until configuration or resolution says so.

## Resolution

```text
logical model
   → resolve_target()
   → ResolvedInferenceTarget
   → Adapter
   → runtime / network endpoint / provider
```

`resolve_target()` decides identity and capabilities only; it never generates. Order:

1. **Explicit node configuration** for the exact model name (stored by `python -m llm_gateway.setup`
   in the encrypted config store): `llamacpp` (local file) or `remote` (OpenAI-compatible or
   Anthropic endpoint). If this target fails, the error is surfaced. The owner's explicit choice is
   never silently replaced by another source.
2. **Built-in local engine**, only if `LLM_GATEWAY_ENABLE_LOCAL` is set and the built-in registry
   has the model file. This target is allowed one fallback.
3. **Ollama-compatible server** as the legacy default when nothing else applies, and as the single
   permitted fallback after a built-in local target fails.

A non-default `base_url` is treated as an Ollama-compatible target (legacy shape).

`ResolvedInferenceTarget` carries: logical and resolved model, adapter id and instance, declared
`capabilities`, `resolution_reason`, `attempt`, `source`, `location`, `runtime`, `provider`,
`config_ref`, and fallback information. Every attempt is recorded in a trace.

## Adapters

Registered in `llm_gateway/adapters.py` (`AdapterRegistry`):

| Adapter id | Runtime | `json_object` | `json_schema` |
|---|---|---|---|
| `llama_cpp` | in-process llama.cpp | yes | no |
| `ollama_compatible` | external HTTP server | yes | yes |
| `openai_compatible` | external HTTP server | yes | no |
| `anthropic` | hosted provider | no | no |

Capabilities are declared per adapter/target; a capability that is not declared is treated as
absent.

## Fallback attempts

A fallback is a new `resolve_target()` call with `fallback_from=<failed target>`. It gets its own
adapter, its own capabilities and its own output contract; it never reuses the failed attempt's
contract. Only a built-in registry target has an automatic fallback (to the Ollama-compatible
adapter). Explicitly configured targets have none.

## Output contracts and semantic completion

- `OutputContract` is the wire-level format selected for **one** attempt.
- `SemanticOutputRequirement` is what the caller needs (currently kind `reply_state`: a visible
  reply plus an optional internal state object with a caller-provided schema).
- `complete_semantic()` picks the strongest contract the resolved target supports:
  `semantic_json_schema` → `semantic_json_object` → a legacy marker format. Where the wire format
  cannot enforce a schema, the state field names are stated in the prompt.
- `SemanticCompletionResult` returns `reply`, `state`, `reply_ok`, `state_ok`, `parse_ok`, `error`
  and `metadata`. `reply` is the only text a caller may show to a user. `state` is internal data
  that the caller's domain layer must validate before acting on it. Malformed state degrades to
  "no state", never to a guessed one and never into the visible reply.

## Other pieces

- `secure_store.py` — encrypted, write-or-erase (never read back in plaintext) node config;
  remote keys are referenced by environment-variable name (`api_key_env`).
- `intelligence_bridge.py` — loopback-only HTTP bridge used by the Rust node to answer peer
  inference requests. A remote peer cannot pass a model or base URL; all peer requests use one
  logical alias configured by the owner of *this* node.
- `embed()` — embeddings. Not yet fully routed through `ResolvedInferenceTarget` (see
  [KNOWN_ISSUES.md](KNOWN_ISSUES.md)).
