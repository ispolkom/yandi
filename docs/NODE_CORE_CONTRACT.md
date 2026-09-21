# Node ⇄ Core contract (v1) — 1.0-rc1

**Status: release candidate 1, nothing here is implemented.** Architecture approved by the owner on 2026-09-21; the choices in
section 9 were made by the engineer on the owner's instruction ("do it as best you can, for the long term"). An independent review
(a second model) produced eight corrections, all applied; they are listed in appendix C. Freeze as 1.0 after the owner's review of
section 10.

**Freeze rule.** 1.0-rc1 may receive only clarifications and additive corrections unless a conformance contradiction proves an
invariant wrong. Any such change makes the next candidate, 1.0-rc2. Section 10 stays open and is decided only when an
implementation chapter needs the answer. The executable form of this contract is the directory `contract/` (section 6).

This document is the *only* thing the two halves of the system agree on. The **node** (Rust, `node/`) and the **core** (today
Python: `pet/`, `agent/`, `llm_gateway/`; later Rust) are separate programs. Any implementation of the core that satisfies this
document can replace the current one without the node, the web UI or the phone app noticing.

---

## 0. Scope

In scope: how the node starts, unlocks and talks to the core; how a user turn, a verification, a knowledge entry and an event
travel between them; what may leave the machine; how a client (browser, phone) sees the same surface.

Out of scope: the node's peer-to-peer transport and its chat/call protocol (owned by the node), the core's internal storage
(owned by the core), UI layout, the Firefox extension's page-automation protocol (an adapter behind `chat.ask`, appendix B).

## 1. Principles (the invariants every implementation must keep)

| # | Invariant | Why |
|---|---|---|
| I1 | **The core has exactly one caller: the node.** It listens on loopback only and accepts a request only with the per-launch secret. Users, browsers and phones are authorised by the node, never by the core. | One place decides who may do what. |
| I2 | **The master key never leaves the node.** The core receives a derived key only after `unlock`, keeps it in memory only, and forgets it on `lock`. A locked core refuses everything except health and unlock. | One master password, one recovery, nothing secret at rest in the core's hands. |
| I3 | **The core has no network of its own.** Everything that leaves the machine goes through the node's egress (section 5.4), which only accepts two payload classes, `user_query` and `derived_query`, and logs each transmission. API keys of remote models live in the node's vault; the core never sees them. The supervisor confines the core's own sockets at operating-system level wherever the OS allows it and reports the level it achieved (section 1.1); the contract alone is not the enforcement. | "Only the user's query and its locally-processed form leave the machine" is enforced, not promised. |
| I4 | **Conversations between people stay in the node.** The peer-to-peer, end-to-end encrypted chats, calls and files between the owner and other people are never given to the core. What the user says *to YANDI* (a turn, section 5.3) does go to the core: that is its purpose. A text from a person-to-person chat reaches the core only when the user explicitly hands it over (`attachments`). | The reason the two are separate programs, stated so that no implementation can read it another way. |
| I5 | **Trust is never truth.** No field, name or value in this contract means "true". Epistemic support is an enumerated level (`epistemic_level`) with the *basis* it was derived from, and is a different axis from the kind of answer (`response_kind`). The summary of a turn (what agrees, what conflicts, what was confirmed, the level) is computed by code; a model only phrases it. | The epistemic core of YANDI. |
| I6 | **History is append-only and retries change nothing.** Every mutating request carries a client-minted idempotency key; the same key returns the same result and creates no new history, **for as long as the history it created exists**; the same words under another key are a new event. | Lost responses, retries and double delivery are normal. |
| I7 | **A model is a tool, never YANDI.** No request may choose a backend outside the aliases the owner configured; a peer or a client cannot name a model, an address or a key. | Models change, YANDI stays. |
| I8 | **Errors never leak internals** (paths, addresses, key names, backend messages). Details go to the local log only. | Existing rule of the intelligence bridge. |
| I9 | **Additive evolution, strict requests.** Inside `v1` fields and endpoints may be added, never removed or reinterpreted. A consumer of an **answer or an event** ignores fields it does not know. A receiver of a **request** rejects a field it does not know (`invalid_payload`), except inside the request's `extensions` object, which is ignored and may never carry anything that changes what is allowed. A sender uses only the fields the receiver advertised in `capabilities`. | The Rust core will arrive piece by piece, and a newer node must never rely on a security-relevant field an older core silently ignored. |
| I10 | **Fail closed.** A malformed, oversized, unauthorised or ambiguous request is refused; a failed check is "not allowed", never "allowed with a warning". | Consistent with every protocol in the project. |
| I11 | **A person is not a device.** Memory, trust and mood belong to a **principal** (the person, `person_id`); the device or session a request came from (`actor_id`) is only for attribution. The same person on a phone and in a browser is one subject. | The project's existing rule SESSION != PERSON, carried across the process boundary. |
| I12 | **Egress derives only from the present.** What leaves the machine is derived only from the current user turn and the attachments the user handed over in it. Long-term memory is not a source of egress text unless the owner switched `egress.memory_context` on. | The core holds a lasting personal memory; it must not leak into a query by way of "helpful context". |

### 1.1 Threat model: what is and is not protected

Protected by this design: the master key and the data at rest (the node holds the key, the core holds only a derived key in
memory, I2); the machine's network exit (I3, I12), including every remote-model credential; person-to-person conversations
(I4); other websites and other users of the browser (the node's front door; the core listens on loopback with a secret, I1).

**Not** protected, and stated plainly: a *running, compromised* core can see the decrypted memory and the current turn it is
working on, because it must. The design limits what such a core can *do* with them: it holds no credentials (I3), has no
network of its own where the operating system allows confining it, and everything it asks the node to send is logged. Where
the OS cannot confine the core (`egress_confinement: "none"` in `capabilities`), the node shows the owner that the
network exit is protected by the contract and by logging only. The levels are `none`, `firewall` (per-program or per-user OS
rules), `namespace` (an isolated network namespace or an equivalent sandbox).

## 2. Vocabulary

- **Node** — the Rust program: identity, login and master key, encrypted peer transport, E2E chats, web front door, supervisor.
- **Core** — the program that holds YANDI's memory, trust, mood and rules and produces answers. One instance per node.
- **Client** — a browser UI or the phone app. Talks to the node only.
- **Principal** — the person YANDI is talking to (`person_id`, stable, minted by the node at first setup and bound to its
  identity). **Actor** — the device or session a request came through (`actor_id`). One principal has many actors.
- **Voice** — the one model that phrases the answer. **Advisors** — any number of models whose opinions are asked and recorded.
  **Checkers** — peers, web search and AI chats that verify. All three are tools (I7); the words are the owner's.
- **Turn** — one user message and everything YANDI does about it, identified by a client-minted `turn_id`.
- **Egress** — any transmission to something outside this machine (a peer, a website, an AI chat, a remote model).

## 3. Deployment model

```text
  browser ─┐                      ┌────────────────────────────── one machine ──────────────────────────────┐
  phone ───┼─ session / device ──▶│  NODE (Rust)                                   CORE (Python → Rust)     │
           │   (node authorises)  │   web front door · login · master key           memory · trust · mood    │
           │                      │   E2E chats · peer transport                    verification · answers   │
           │                      │   vault (API keys) · supervisor                 state store · database   │
           │                      │        │   ▲                                        ▲                    │
           │                      │        │   └───── core API  /v1  (loopback, secret) ┘                    │
           │                      │        └────────── node API  /node/v1  (egress, loopback, secret) ───────▶│
           └──────────────────────┴──────────────────────────────────────────────────────────────────────────┘
```

- The node starts the core as a **separate process** (never a thread) and supervises it. The core may also be started by hand for
  development; it then waits for an `unlock` like in production.
- Two loopback APIs, both authenticated with the launch secret, both bound to `127.0.0.1` on ports chosen at start:
  the **core API** (`/v1`, node → core) and the **node API** (`/node/v1`, core → node: egress, vault references, peer directory).
- A client sees one surface: the node exposes the core API under its own front door (`/v1/...`) after authorising the session or
  device, and adds two headers when forwarding: `X-Principal` (the person, I11) and `X-Actor` (the device or session, for
  attribution only). The core never sees credentials.

## 4. Conventions

**Transport.** HTTP/1.1 + JSON (UTF-8, NFC-normalised text) over loopback TCP. Streaming and events use Server-Sent Events.
The node may carry the same requests to a remote client over its own encrypted transport; the wire format of that leg is the
node's business, the requests and answers are identical.

**Versioning.** Major version in the path (`/v1`). Minor changes are additive (I9). `GET /v1/capabilities` (section 5.1) is how a
caller learns what this implementation supports. A major version is supported while any released client uses it; two majors overlap.

**Identifiers and time.** Resource ids are opaque strings, sortable ULIDs by convention. `turn_id` and idempotency keys are
`[A-Za-z0-9_-]{8,64}`. Timestamps are RFC 3339 UTC. Every request has an optional `extensions` object (I9); a request that
sends any other field the receiver does not know is refused. `request_id` is an opaque caller-chosen trace id that is echoed back and
decides nothing.

**Idempotency (I6).** Every `POST`/`PUT`/`DELETE` carries `Idempotency-Key`. Same key and same body → the original result, no new
history. Same key and a different body → `409 idempotency_conflict`. A turn's key is its `turn_id`. The record `(key, request hash, resource id)` is kept **as long as
the history it created** (I6); only the cached *response body* may be dropped, and it is then rebuilt from the resource.

**Authentication.** `Authorization: Bearer <launch secret>` (32 random bytes, hex), generated by the node at each start, passed to
the core through a 0600 file in the per-user runtime directory whose path is given in an environment variable — never on the
command line. Compared in constant time. Only `GET /v1/health` is open. A different secret per launch and per direction.

**Errors.** Uniform body, stable machine codes (appendix A):
`{"error": {"code": "invalid_payload", "message": "...", "retryable": false, "request_id": "..."}}`. `message` is safe to show
to a user (I8).

**Limits** (each is also advertised in `capabilities`): request body ≤ 64 KiB unless an endpoint says otherwise; a message ≤ 32 768
characters; ≤ 64 messages per request; generated tokens ≤ 4 096 unless the owner raised the cap; a peer's rate ≤ 60 requests a
minute. (These are the limits the two existing bridges already enforce.)

**Events.** `GET /v1/events` (SSE) with the standard `Last-Event-ID` header: "everything after event N". Each event:
`id` (monotonic within an `epoch`), `type`, `ts`, `data`. If the log was reset the `epoch` changes and the client resynchronises
from state. The log is durable and bounded (age and size); a client that is offline longer than the retention resynchronises.

## 5. Chapters

### 5.1 Lifecycle and capabilities

States: `starting → locked → ready → draining → stopped`. `failed` on an unrecoverable start.

| Request | Meaning |
|---|---|
| `GET /v1/health` | Open. `{"state": "locked"}` — nothing else (no version, no detail). Readiness for the supervisor. |
| `GET /v1/capabilities` | `{"contract": "1.0-rc1", "implementation": {"name": "python-core", "version": "…"}, "features": ["conversation", "verification", …], "limits": {…}, "storage_schema": 18, "egress_confinement": "none"}`. A feature that is absent is not implemented; the node degrades gracefully. |
| `POST /v1/shutdown` | Graceful stop within `deadline_ms`; the core finishes or rolls back its transactions. |

Supervision (node side): start, wait for `locked`, `unlock`, wait for `ready`; restart with backoff on exit; after *N* failures
within a window stop restarting and show the reason. Crash of the core never affects the node's transport or chats.

### 5.2 Keys

`POST /v1/unlock` `{"key": "<base64 32 bytes>", "context": "yandi/core/v1"}` → `{"state": "ready"}`. The key is
`HKDF(master_key, "yandi/core/v1")` derived by the node; the core derives every storage key from it (fields of the database, the
state store, secrets it wraps) and never writes the key or its derivations to disk. `POST /v1/lock` forgets them and returns to
`locked`. A wrong key never becomes `ready` (the core proves it by decrypting a check value). Until the key exists the core
holds no decrypted state.

### 5.3 Conversation

```text
POST /v1/conversations                       create           → {"conversation_id"}
POST /v1/conversations/{cid}/turns           one user turn    (Idempotency-Key = turn_id)
GET  /v1/conversations/{cid}/turns?after=    history (paged)
GET  /v1/conversations/{cid}/turns/{tid}     the turn as it stands now
```

Turn request:

```json
{ "turn_id": "…", "text": "…",
  "routing": { "voice": "auto", "advisors": ["local"], "verify": { "web": true, "peers": true, "chats": ["deepseek"] } },
  "policy": { "egress": "allowed", "retention": "durable", "memory_scope": "personal" },
  "attachments": [] }
```

`routing` only *narrows or names* what the owner configured (I7). `policy` is three independent choices:
`egress` = `none | allowed` (may anything leave the machine for this turn), `retention` = `ephemeral | durable` (is the turn
kept at all), `memory_scope` = `none | personal | shared` (whether it enters the owner's memory, and whether it may be offered
to the network). "Remember this forever, locally, and never send it out" is `egress: none, retention: durable, memory_scope:
personal`; the quick local chat is `egress: none, retention: ephemeral, memory_scope: none`. The reply is `202` with the turn
resource; progress arrives as events. The turn resource:

```json
{ "turn_id": "…", "status": "answering|verifying|done|failed",
  "reply": { "text": "…", "voice": "<alias>" },
  "response_kind": "factual",
  "summary": { "trust": { "level": "hypothesis", "basis": ["opinions_agree", "web_partial"] },
               "agreed": [{"claim": "…", "sources": ["…"]}], "disputed": [{"claim": "…", "positions": [ … ]}],
               "unverified": [ … ] },
  "opinions": [{ "source": "advisor:local", "text": "…" }],
  "verification": [{ "checker": "peer:ab12", "status": "answered|pending|failed", "trust_effect": "…" }],
  "clarification": null }
```

Rules: `summary` is computed by code (I5); `reply.text` phrases it; conflicts are shown, never averaged away; every claim keeps
its sources; a turn that needs more context returns `clarification` and no reply. Levels (to be frozen before
1.0) come as **two axes**. `epistemic_level` — how far the claims are supported: `supported`, `partially_supported`, `hypothesis`,
`unverified`, `contradicted`. `response_kind` — what kind of answer it is: `factual`, `personal`, `clarification` (a personal
or clarifying answer has no epistemic level). The names of the older UI map as `VERIFIED → supported`, `PARTIALLY_VERIFIED →
partially_supported`, `HYPOTHESIS → hypothesis`, `UNVERIFIED → unverified`, `REJECTED → contradicted` (to be confirmed at the
freeze), `PERSONAL → personal`, `CLARIFICATION → clarification`. The support of a turn can change *after* the reply (a peer
answers late): that is an event, and `summary` is updated.

Events of a turn: `turn.accepted`, `turn.reply.delta`, `turn.reply.done`, `turn.verification.updated`, `turn.trust.changed`,
`turn.failed`, `turn.trace.recorded`.

### 5.4 Verification and egress

The core decides *that* something must be checked and *by whom* (within the owner's configuration); the node performs every
transmission. Node API:

```text
POST /node/v1/egress/{kind}     kind ∈ peer.infer | web.search | web.fetch | chat.ask | llm.remote
GET  /node/v1/peers             trusted peers and whether each is online (node id, optional label; no addresses, keys or models)
POST /node/v1/vault/resolve     never returns a secret; only answers whether a credential reference exists
```

Egress request: `{"purpose": "verification", "class": "user_query" | "derived_query", "text": "…", "target": {…},
"provenance": {"turn_id": "…", "sources": ["turn_text", "attachment:<id>"]}}`. The node refuses any other class (I3), a request
without provenance, one whose `turn_id` is not an open turn whose `policy.egress` is `allowed`, and one that lists a source other
than the turn's own text and its attachments (I12) — unless the owner switched `egress.memory_context` on, in which case
`memory` is a permitted source and is recorded as such. The node cannot judge the text itself, so I12 is also enforced by
the outbound log (below) and by a conformance scenario: a memory entry seeded with a unique marker must never appear in any
egress text of an unrelated turn (section 6). The node performs the call with its own credentials, streams the answer back, and appends to the **outbound
log**: `{id, ts, kind, destination, class, purpose, text}` — readable by the owner (`GET /v1/outbound-log?after=`). `llm.remote`
lets the Voice or an Advisor be a remote model by API: the request names an *alias*; the node holds the key and the address.
A peer `infer` names a peer, never a backend. Where the owner configured a remote model as the Voice, its prompt is the persona instruction plus the current turn; memory
excerpts are added only under the same switch. `chat.ask` reaches the AI chats open in the owner's browser through the node's
browser-chat adapter (appendix B).

### 5.5 Peers (inbound)

Other nodes may ask this YANDI. The node validates the peer (allow-list, signature, rate, replay window) and forwards:

```text
POST /v1/peer/infer              {"request_id", "messages", "max_tokens", "temperature", "stop"}  → {"success", "text", …}
POST /v1/peer/knowledge-offer    a signed entry a peer wants to share
```

`peer/infer` keeps the existing bridge rules: no field selects a model, an address or a key. The **node** builds this request
from the peer's validated fields and drops everything else (the peer's `model`, `base_url` and the like never travel); the
**core** refuses a request that carries an unknown field (I9). The core answers with the owner's peer alias; failures return a category, never backend detail. `knowledge-offer` is *offered*, never
*accepted by arrival*: the core records it as a hypothesis with its provenance (I5).

### 5.6 Knowledge

```text
POST /v1/knowledge               store an entry (with provenance)
POST /v1/knowledge/search        {"query", "limit"} → entries with trust and provenance
```

The core is the source of truth for what is known and how far it is trusted. The node only transports (gossip, offers) and
never decides trust.

### 5.7 Events

`GET /v1/events` streams every event of the core (conversation, verification, knowledge, lifecycle). Event types are enumerated in
appendix A. The same stream feeds the web UI, the phone app's sync and, later, push (a push is only "something new after N").

### 5.8 Settings and models

```text
GET/PUT /v1/settings            voice, advisors, defaults for routing and policy
GET     /v1/models              the aliases the owner configured, with kind (local | remote | peer) and availability
```

Settings never contain a secret. A remote model is an alias plus a `credential_ref` the node resolves (I3). Adding a local model
file is a node/owner action; the core learns of the alias. (The web settings tab of 2026-09 saves `voice`/`advisors` in a file
today; it moves behind this endpoint.)

### 5.9 Devices and clients

Owned by the node: pairing (QR), per-device keys, revocation, sessions. The core receives `X-Principal` (the person, whose memory it is) and
`X-Actor` (the device, for attribution in the trace only); it must never key memory, trust or mood on the actor (I11). A revoked device is refused by the node before anything reaches the core. The phone syncs with `GET /v1/events` and turn
history (`after=`); in the first version it polls (about hourly, on open, live while the app is in the foreground).

## 6. Conformance suite (how "any implementation" is proven)

The directory `contract/` (created in P0.5; its README says how to run it) holds (a) JSON Schemas for every request, answer and event, and (b) **scenario fixtures**
in plain data: `given` (state) / `when` (requests, faults) / `then` (answers, events, state). A small runner executes them against
any URL that speaks `/v1`. The Python test-suites that encode the project's invariants migrate into fixtures first:

- a self-report never raises trust; a verified delivery raises it by a bounded amount once; farming is bounded;
- the same turn retried, after a commit, or delivered eight times at once changes nothing;
- an ambiguous target is never verified; evidence is exact bytes of the turn it names;
- deny by default; an unlocked key exists only in memory; no egress outside the two payload classes;
- egress derives only from the present: a memory entry seeded with a unique marker never appears in an egress text of an
  unrelated turn (and does appear only when the memory switch is on and logged as `memory`);
- one person, two devices: the same memory, one subject; a request with an unknown field is refused; an idempotency key still
  returns the original result long after any response cache is gone.

A Rust core is *done* for a chapter when it passes that chapter's fixtures.

## 7. Migration path

| Step | What | Result |
|---|---|---|
| P0 | This document, reviewed and frozen as 1.0-rc1; P0.5 adds the executable P1 fixtures in `contract/`. | Agreement, then a test that fails first. |
| P1 | Supervision: launch secret, `health`, `capabilities`, `unlock/lock`, `shutdown` around the **existing** Python core; the node starts and watches it. | One start, one login. |
| P2 | Egress through the node: `peer.infer`, `web.*`, `chat.ask`, `llm.remote`; the outbound log; the core loses direct network. | I3 enforced. |
| P3 | Conversation + events over `/v1` (a thin adapter over today's `pet/` logic); the web UI moves onto it. | UI independent of the core's internals. |
| P4 | Web front door in the node (same login), links Node ⇄ YANDI; the core's own port is no longer exposed. | One door. |
| P5 | Phone app on the same surface; QR pairing; polling sync. | Mobile v1. |
| P6 | Rust core, chapter by chapter, each proven by fixtures; the state-store layer replaces direct Redis in the Python core first. | Migration without a flag day. |

## 8. Compatibility with today's interfaces

| Today | Becomes |
|---|---|
| node `POST /api/ai-rpc/infer` (18082) | `POST /node/v1/egress/peer.infer` (and `llm.remote`) |
| node `POST /api/ai-rpc/fetch` | `POST /node/v1/egress/web.fetch` |
| node `GET /api/ai-rpc/peers` | `GET /node/v1/peers` |
| node `…/knowledge/store`, `…/knowledge/search` | `POST /v1/knowledge`, `POST /v1/knowledge/search` (the core owns the entries) |
| Python bridge `POST` infer (18083) | `POST /v1/peer/infer` |
| "is the node alive": TCP connect to 9999, copied in two places | `GET /v1/health`, `GET /node/v1/peers` |
| `pet/chat_local.py` (`/api/local/chat`, personal memory) | conversation turns (5.3) with `policy` and memory rules |
| `pet/chat_orch.py` (`/api/orchestrator/ask`, history, validation) | the same turn with `routing.verify`; validation = `turn.trust.changed` |
| Firefox extension `/api/ext/*` | node's browser-chat adapter (appendix B) |
| Redis lists, keys, pub/sub | inside the core behind the state-store layer; invisible here |

## 9. Decisions made by the engineer (open to challenge)

1. **The core has a single caller, the node** (I1). Alternative — the core authorising devices itself — would duplicate login and
   spread trust; rejected.
2. **All egress goes through the node, and API keys of remote models live in the node's vault** (I3). This closes the earlier open
   question "where does the API key live": not in the core, never in a settings file. Cost: remote-model streaming needs plumbing
   through the node. A development-only feature flag may let the core call out directly; production must not.
3. **SSE for events and streaming, with `Last-Event-ID`** — one standard resume mechanism for browser, phone and any language.
4. **Idempotency keys on every mutation**, same rule as the turn identity already in `pet/` (I6).
5. **`capabilities` with features and limits** so a partial Rust core can coexist with the Python one.
6. **Conformance fixtures as data**, so the contract is executable and the Python invariants survive the port.
7. **Epistemic level as an enumeration with a `basis`, separate from the kind of answer**, no field that can mean "true" (I5).
8. **Principal and actor are different headers** (I11); **egress is derived from the present only** (I12); **requests are strict,
   answers tolerant** (I9); **the network confinement level of the core is measured and shown, not assumed** (1.1).

## 10. Open questions

- The final set of `basis` codes, and the confirmation of `REJECTED → contradicted` (freeze before 1.0).
- How each OS confines the core's sockets (Linux: network namespace or per-user firewall rules; Windows: per-program firewall
  rule; macOS: a sandbox profile) and what the node does when it cannot.
- Whether a person may have several principals on one node (the contract already carries `person_id`, so it can be added).
- Retention of the event log and of idempotency results; what a relay node may hold for an offline phone (a sealed mailbox or nothing).
- The embedded state store for Windows/macOS and for the Rust core; how the pub/sub inside the core is exposed to its own workers.
- The browser-chat adapter: whether the Firefox extension talks to the node directly (recommended) and how it authenticates.
- Streaming shape of `llm.remote` and the exact `target` fields per egress kind.
- Push delivery (later): the signal is an empty encrypted "something new after N"; the transport is undecided.

---

## Appendix A — codes

**Errors:** `invalid_payload` (400), `unauthorized` (401), `forbidden` (403, includes a refused egress class), `not_found` (404),
`idempotency_conflict` (409), `payload_too_large` (413), `locked` (423, the core is locked), `rate_limited` (429),
`backend_error` (502, no detail), `unavailable` (503), `internal` (500). `retryable` is set by the server.

**Events:** `core.state`, `turn.accepted`, `turn.reply.delta`, `turn.reply.done`, `turn.verification.updated`, `turn.trust.changed`,
`turn.failed`, `turn.trace.recorded`, `knowledge.stored`, `knowledge.offered`, `peer.online`, `peer.offline`, `settings.changed`,
`outbound.logged`, `events.reset` (new epoch).

## Appendix B — browser-chat adapter (placeholder)

Today the Firefox extension polls the PET server (`/api/ext/*`) and types prompts into chats the user has open. In the target it
polls the **node**, which exposes `chat.ask` as an egress kind and returns the chat's reply as untrusted data. The extension's
allowed addresses move with it (see `docs/EXTENSION.md`). To be specified when P2 starts.

## Appendix C — changes after the independent review (1.0-rc1)

1. I4 restated: person-to-person chats stay in the node; what the user says to YANDI goes to the core.
2. Principal (`person_id`) and actor (`actor_id`) separated; new invariant I11.
3. Idempotency records are kept as long as the history they created, not for a fixed number of days.
4. Requests are strict (unknown fields refused, `extensions` ignored), answers and events tolerant; the peer edge drops fields in the node.
5. I3 no longer relies on the contract alone: the supervisor confines the core where the OS allows and reports the level.
6. `privacy` split into `egress`, `retention` and `memory_scope`.
7. Egress derives only from the present turn and its attachments (I12), with provenance on every egress request and a conformance scenario.
8. Trust split into `epistemic_level` and `response_kind`.
Also added: the threat model (1.1), which says plainly that a running compromised core sees the memory it works on.

## Appendix D — P1 clarifications (rc1)

Additive clarifications made while turning the lifecycle chapter into executable fixtures (`contract/`). No invariant changed. Where
the text above was silent, this appendix pins one reading; what it leaves out stays open and is listed at the end.

1. **Answers.** `POST /v1/unlock` → `200 {"state": "ready"}`; `POST /v1/lock` → `200 {"state": "locked"}`;
   `POST /v1/shutdown` → `202 {"state": "draining"}`. `GET /v1/health` → `200`, a body of exactly `{"state": <state>}` and nothing
   else, in *any* state it can answer in; it never carries a version, implementation, storage detail, path, address or backend
   text. A locked core answers `GET /v1/capabilities` with `423 locked` (I2); a caller reads capabilities after `unlock`.
   `capabilities` also carries `egress_confinement` (`none`, `firewall` or `namespace`, section 1.1) and its `contract` field is the
   contract version the implementation claims (`"1.0-rc1"`).
2. **Request bodies** of `unlock`, `lock` and `shutdown` are JSON objects. `unlock`: `key` (base64 of exactly 32 bytes) and `context`
   (exactly `"yandi/core/v1"`), both required. `lock`: no fields. `shutdown`: optional `deadline_ms` (a positive integer). Every one may
   also carry `extensions` (I9) and nothing else.
3. **Refusals.** No or invalid `Authorization` → `401 unauthorized`, in every state including `locked`, before anything else is
   looked at. A protected request other than `unlock` on a locked core → `423 locked` (I2), which includes `capabilities`, `lock`
   and `shutdown` on a locked core; a locked core is ended by the supervisor terminating the process. A well-formed `unlock`
   with the wrong key → `403 forbidden`, the core stays `locked` and holds no decrypted state. A malformed key, a wrong `context`, an
   unknown field, a missing required field, a wrong type, a duplicate JSON key (ambiguous, I10), a missing or invalid
   `Idempotency-Key`, an unreadable body → `400 invalid_payload`. A body over the limit → `413 payload_too_large`. An unknown path,
   or the path of a feature the core does not list in `features` → `404 not_found` (the node must not assume it, I9). Every error uses
   the uniform body of section 4.
4. **Order of checks** (so the answer never depends on luck): authentication → `Idempotency-Key` header → body size → JSON parse and
   schema validation → idempotency lookup → state (`423`) → execution. A retry whose first attempt succeeded therefore gets its
   original answer even if the state has moved on since (I6).
5. **Lifecycle idempotency.** `unlock`, `lock` and `shutdown` are mutating requests and fall under section 4: same key and same
   body → the original answer, no second effect; same key and a different body → `409 idempotency_conflict`. They create no
   append-only history, so their record lives as long as the core process (never on disk; for `unlock` only a digest of the body
   is kept, never the key) — that is the same rule as before ("as long as the history it created"), not a time limit.
6. **Principal and actor.** `X-Principal` and `X-Actor` are independent, opaque, optional on lifecycle requests, and never grant
   anything: a request with either header and no valid secret is still `401`. A missing `X-Actor` never makes the principal act as
   the actor, and the core never derives one from the other (I11).
7. **Sanitisation (I8).** No error body and no `health` body contains a filesystem path, an address with a port, a socket name, a
   backend's raw message, a stack trace, an environment variable name or a credential name, and no response echoes the launch
   secret or the unlock key.

Gaps left open on purpose (not fixtured; candidates for a later candidate): `unlock` on an already `ready` core with a new key; the
scope of the idempotency request hash across principals; the format of `X-Principal`/`X-Actor` values; whether `request_id` is echoed in a
header; the field names inside `limits`; when `health` may answer `starting` or `failed`; the behaviour of a `draining` core toward
new requests.
