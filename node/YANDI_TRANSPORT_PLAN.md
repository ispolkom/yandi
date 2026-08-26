# YANDI Transport Plan

## Purpose

This document defines the target transport architecture for YANDI and the practical sequence of work.

The goal is not "invent a protocol for the sake of it".
The goal is:

- stable transport under hostile network conditions
- dual-path delivery with clone-based recovery
- predictable behavior under loss, jitter, DPI pressure, and asymmetric links
- clean separation between `netlayer` traffic and `p2p` communication traffic
- end-to-end encryption for human communication on top of transport encryption

## Core Position

YANDI will keep two independent transports:

- `netlayer`
  for proxying, gateway, tunnels, internet access, service traffic
- `p2p`
  for direct client-to-client communication: chat, files, voice, video

These transports must be independent by sockets, port rotation, runtime state, and failure domains.

At the same time, both transports should share the same transport philosophy:

- station / train / wagon model
- original + full clone
- receiver-side recovery
- deduplication
- graceful port rotation
- permanent fallback ports
- encrypted transport sessions

## Transport Invariants

These are non-negotiable.

1. Fallback ports must always remain open.
   For `netlayer`, fallback is `9000/10000`.

2. Active rotated ports may change, but fallback ports must still accept late, stale, or new peers.

3. Port rotation must not break established traffic.
   Old rotated ports must stay alive during grace TTL and then close.

4. Sender does not depend on constant ACK/NACK chatter for normal operation.
   Reliability is based primarily on redundancy and receiver-side recovery.

5. Receiver is the source of truth for loss.
   Sender may report what it transmits, but effective delivery is determined by the receiving side.

6. Path0 loss is not the same as effective delivery loss.
   Metrics and UI must keep these separate.

7. Completed trains must not pollute the same resource budget as incomplete trains indefinitely.

8. Transport must remain usable in degraded networks.
   Lower speed is acceptable. Silent corruption and broken delivery semantics are not.

9. `netlayer` encryption protects transport sessions.
   `p2p` communication must additionally protect content with E2E encryption.

10. Every optimization must preserve observability.
   If behavior changes, logs and metrics must still explain what happened.

## Target Architecture

### 1. Netlayer

`netlayer` is the hardened transport core for:

- proxy requests/responses
- gateway traffic
- internet tunneling
- service wagons
- station-to-station transport

It must guarantee:

- stable rotation
- fallback recovery
- dual-path recovery
- depot stability under load
- measurable throughput, loss, and recovery behavior

### 2. P2P Transport

`p2p` will be a separate transport family for:

- chat
- file transfer
- voice
- video
- session signaling

It should become transport-identical in philosophy, but operationally independent:

- separate socket family
- separate port pool
- separate rotation state
- separate telemetry
- separate resource limits

### 3. E2E Layer

The `p2p` stack must include content-layer E2E encryption on top of transport encryption:

- message payload E2E
- file metadata and payload E2E
- voice/video session E2E

Transport crypto protects the pipe.
E2E protects the meaning.

## Current Status

Already working or partially working:

- dual-path train delivery
- clone-based recovery
- transport encryption
- proxy traffic over custom transport
- permanent fallback ports for `netlayer`
- active port rotation with grace-old sockets
- UI metric semantics improved
- live transport dashboard on main page
- depot cleanup and resource accounting improved
- proxy tunnel teardown noise reduced (`Broken pipe` cleanup)

Current weak areas:

- adaptive pacing is not yet mature
- adaptive pacing is not yet mature
- wagon sizing is still static
- effective loss is still inferential, not explicitly tracked end-to-end
- `p2p` transport does not yet inherit the same hardened semantics

## Work Plan

### Phase 1. Stabilize Netlayer

Goal:
make `netlayer` operationally trustworthy.

Steps:

1. Done: keep fallback `9000/10000` permanently open.
2. Done: keep rotated sockets alive for grace TTL.
3. Done: ensure heartbeat/stream/send paths follow current active sockets.
4. Done: reduce false depot pressure and train evictions.
5. Done: separate incomplete train pressure from completed-train dedup retention.
6. In progress: make metrics honest:
   - done: local RX
   - done: local TX
   - done: path0 loss
   - done: clone recovery
   - pending: explicit effective loss
7. Done: verify behavior during peak hours.
8. Done: reduce proxy/browser teardown noise in logs.

Completion criteria:

- rotations happen without breaking traffic
- peers survive multiple rotations
- fallback reconnect works
- eviction count is materially reduced
- clone recovery remains functional

Status:

- substantially achieved for current two-node field tests
- keep monitoring under real peak-hour traffic before marking fully closed

### Phase 2. Add Adaptive Transport Control

Goal:
make the transport adapt instead of using rigid constants.

Steps:

1. Add adaptive pacing.
2. Add dynamic wagon delay tuning.
3. Add safe dynamic wagon sizing in bounded ranges.
4. Use receiver-side signals to tune sender behavior.
5. Never jump to unsafe MTU assumptions.

Rules:

- optimize for internet-safe behavior, not lab maximum throughput
- prefer small controlled adjustments
- no "large MTU" jumps without evidence

Suggested safe progression:

1. tune pacing
2. observe loss and clone usage
3. increase wagon size gradually
4. stop increasing when clone recovery or instability spikes

Completion criteria:

- better throughput in practice
- no major rise in effective delivery failures
- stable behavior under jitter and peak-hour congestion

### Phase 3. Harden Telemetry

Goal:
make transport behavior measurable enough for disciplined tuning.

Required metrics:

- sent wagons path0
- sent wagons path1
- received wagons path0
- received wagons path1
- clone-used count
- duplicate-dropped count
- incomplete-train timeout count
- eviction count
- path0 loss rate
- effective loss rate
- RTT
- raw TX/RX throughput
- useful throughput

Current progress:

- done: raw TX/RX throughput
- done: path0 loss rate
- done: clone-used count
- done: eviction count
- done: timeout / cleanup counts
- done: RTT from active streams
- done: peer-side TX and peer-side Path0 view
- pending: duplicate-dropped count
- pending: explicit useful throughput
- pending: explicit effective loss rate

Completion criteria:

- UI stops lying about loss
- logs explain transport state transitions
- every future optimization can be judged with evidence

### Phase 4. Define P2P Transport v2

Goal:
build a communication transport that is behaviorally aligned with `netlayer`.

Requirements:

- separate sockets and port pools
- separate rotation TTLs
- station/train/wagon semantics
- dual-path original + clone
- receiver-side reassembly
- separate media/file/chat policies
- no dependency on `netlayer` runtime state

Completion criteria:

- direct communication no longer relies on the old lightweight `p2p` assumptions
- large file transfer uses hardened semantics
- voice/video can run on a transport designed for them

### Phase 5. Add E2E for Communication

Goal:
communication remains private even beyond transport confidentiality.

Scope:

- chat messages
- file attachments
- media session negotiation
- voice/video content

Rules:

- E2E must be above transport encryption
- transport nodes may carry data but should not need plaintext access
- metadata exposure should be minimized where practical

Completion criteria:

- content privacy is independent from transport path trust

## Immediate Priorities

These are the next practical items.

1. Compare new peak-hour logs after depot fixes.
2. Measure whether `Evicted oldest train` drops materially.
3. If improved, implement adaptive pacing.
4. Then introduce bounded dynamic wagon sizing.
5. Only after `netlayer` is stable enough, start `p2p transport v2`.

## What Not To Do

1. Do not chase huge MTU values just because local code allows them.
2. Do not mix `netlayer` and `p2p` into one socket family.
3. Do not present path0 loss as effective user-visible loss.
4. Do not optimize speed before stabilizing depot behavior.
5. Do not remove fallback ports.

## Decision Rule

When choosing between elegance and survivability:

prefer survivability.

When choosing between peak benchmark speed and stable peak-hour behavior:

prefer stable peak-hour behavior.

When choosing between less code and clearer transport semantics:

prefer clearer transport semantics.
