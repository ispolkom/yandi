# YANDI documentation

| Document | Contents |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Layers, components, one turn of the personal chat, what is not built |
| [INFERENCE_GATEWAY.md](INFERENCE_GATEWAY.md) | Target resolution, adapters, fallback, output contracts |
| [MEMORY_AND_IDENTITY.md](MEMORY_AND_IDENTITY.md) | Self model, relationship memory, provenance guard, apology matching |
| [EPISTEMIC_CORE.md](EPISTEMIC_CORE.md) | Claims, evidence, trust labels, beliefs, reflection |
| [EXTENSION.md](EXTENSION.md) | Firefox extension: what it does, build, install, tests, limits |
| [NODE_CORE_CONTRACT.md](NODE_CORE_CONTRACT.md) | Contract (1.0-rc1) between the node and the YANDI core: invariants, lifecycle, keys, conversation, egress, events, migration; executable form in `contract/` |
| [CORE_LIFECYCLE.md](CORE_LIFECYCLE.md) | The Python Core's P1 lifecycle boundary: how it is started, locked, unlocked and shut down, and what still goes around the gate |
| [KEY_RECOVERY.md](KEY_RECOVERY.md) | P1c-1: the root key with a device wrapper and a recovery-password wrapper, the identity that survives loss of the device, `yandi-keys` |
| [KEY_CHAIN_AUDIT.md](KEY_CHAIN_AUDIT.md) | Read-only audit of every key and what it really protects (P1c part 1): findings, threat table, decisions, recommendation |
| [CORE_SUPERVISION.md](CORE_SUPERVISION.md) | How the node owns the Python Core process (P1b): spawn, unlock, restart with backoff, shutdown; canonical path vs legacy path |
| [TESTING.md](TESTING.md) | How to run the regression suites |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | Open problems and limitations |

Repository policy: [../SECURITY.md](../SECURITY.md), [../CONTRIBUTING.md](../CONTRIBUTING.md).

## Development history

These dated files at the repository root record audits and design decisions made during
development. They may describe states that have since changed, and some contain paths from the
reference machine. Read them as history, not as current documentation.

- [DATABASE_BOOTSTRAP_V1_CHECKPOINT.md](../DATABASE_BOOTSTRAP_V1_CHECKPOINT.md)
- [PET_AGENT_BOUNDARY_AUDIT.md](../PET_AGENT_BOUNDARY_AUDIT.md),
  [YANDI_PET_AGENT_BOUNDARY_REFACTOR_REPORT.md](../YANDI_PET_AGENT_BOUNDARY_REFACTOR_REPORT.md)
- [YANDI_OLLAMA_DECOUPLING_AUDIT.md](../YANDI_OLLAMA_DECOUPLING_AUDIT.md),
  [YANDI_LLM_GATEWAY_STATUS.md](../YANDI_LLM_GATEWAY_STATUS.md)
- [YANDI_EMBEDDINGS_ARCHITECTURE_AUDIT.md](../YANDI_EMBEDDINGS_ARCHITECTURE_AUDIT.md)
- [YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md](../YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md),
  [YANDI_EVIDENCE_ELIGIBILITY_REVIEW.md](../YANDI_EVIDENCE_ELIGIBILITY_REVIEW.md)
- [YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md](../YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md),
  [YANDI_SELF_LEARNING_FOUNDATION_REPAIR_REPORT.md](../YANDI_SELF_LEARNING_FOUNDATION_REPAIR_REPORT.md)
- [ROADMAP_v7.md](../ROADMAP_v7.md)

Component notes: the SQL layer has its own design documents under
[`agent/db/sql/`](../agent/db/sql/) (`SECURITY_ARCHITECTURE.md`, `SECURITY_THREAT_MODEL.md`,
`DEDICATED_INSTANCE_DESIGN.md`, `MIGRATION_STATUS.md`); the node has status and plan notes under
[`node/`](../node/) (`CURRENT_STATE.md`, `YANDI_VISION.md`, `YANDI_ROADMAP.md`).
