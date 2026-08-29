# YANDI — Disk Capacity Report (Этап 5E-S2, read-only audit)

Point-in-time snapshot. Nothing was deleted, cleaned, or modified to
produce this report — every command below is read-only (`df`, `du`,
`lsblk`, `journalctl --disk-usage`).

**Timestamp**: 2026-08-29 08:05 MSK.

## Volumes present on this host

| Device | Mountpoint | FS | Size | Used | Free | Use% |
|---|---|---|---|---|---|---|
| `/dev/sdb2` | `/` | ext4 | 218G | 81G | 126G | 40% |
| `/dev/sdb1` | `/boot/efi` | vfat | 511M | 5.9M | 506M | 2% |
| `/dev/sdc1` | `/mnt/ssd480` | ext4 | 440G | 428G | **0** | **100%** |
| `/dev/sda1` | `/mnt/backup` | ext4 (LABEL=DATASET) | 1.8T | 1.1T | 681G | 61% |

No LVM (`lvs`/`vgs` not installed — plain partitions, not volume-
managed). No swap-adjacent free space relevant to datadir placement
(`sdb3` is swap).

## Note on volatility observed during this audit

An earlier reading in this same session (before the owner manually
freed space) showed `/dev/sdb2` at **96% used, 9.7G free**. The owner
confirmed they cleaned the disk between that reading and this report.
**Recorded honestly**: this filesystem's free space is NOT static and
can swing by >100G within a session from causes outside YANDI's
control (the owner's own maintenance, in this instance). This is a
real operational fact worth designing around — see the storage-policy
rationale below: **a design gated on today's specific free-byte count
would already be stale by the time an installer runs.** The storage
policy (`storage_policy.py`) is deliberately threshold/hysteresis-based
against whatever the free space happens to be AT CHECK TIME, not a
one-time capacity assumption baked in from this audit.

## Inode headroom

`/dev/sdb2`: 14,557,184 total inodes, 495,931 used (4%), 14,061,253
free — no inode-exhaustion risk visible (YANDI's append-only tables are
few, large files, not a huge-file-count workload; a future concern
mainly if evidence-observation volume grows into the tens of millions
of tiny rows-as-files, which is not this architecture's shape).

## Largest consumers under `/var` (the mount `/var/lib/yandi` would live under)

| Path | Size |
|---|---|
| `/var/log` | 3.0G |
| `/var/www` | 555M (partially inaccessible — other users' FastPanel sites) |
| `/var/upload` | 489M |
| `/var/lib` | 264M (as visible to a non-root user — `/var/lib/mysql` itself is `0700`, size not measurable without root, expected and correct) |
| `/var/cache` | 254M (`/var/cache/apt` = 244M of that) |

`journalctl --disk-usage`: 198.5M (archived + active journals).

No Docker installed (`docker` command not found) — no container
storage layer to account for.

`/home/iam`: 24G (this repo + venv + scratch files live here; NOT a
candidate location for the dedicated datadir per the mandate's own
explicit rule — no YANDI DB files inside `/home/iam/yandi` or the repo).

## Candidate locations for a future dedicated YANDI datadir

1. **`/var/lib/yandi/...` on `/dev/sdb2`** (the root filesystem) —
   currently 126G free, matches Linux FHS convention (`/var/lib/<app>`
   for a service's persistent state), and is where the mandate's own
   proposed topology (§1) already points. **Recommended default**,
   subject to the storage-policy gate (below) being satisfied AT
   INSTALL TIME, not based on this specific snapshot.
2. `/dev/sdc1` (`/mnt/ssd480`) — **ruled out**: 100% full, 0 bytes
   free, right now. Not a viable target regardless of any future
   cleanup — this is a separate physical volume already at capacity
   for whatever it currently stores; not this audit's business to
   investigate further or free.
3. `/dev/sda1` (`/mnt/backup`) — 681G free, but **not recommended as
   the LIVE datadir location** without an explicit owner decision: its
   mount label (`DATASET`) and path (`/mnt/backup`) imply an existing,
   different purpose (backup/archival storage), and putting live
   transactional DB data on a volume semantically reserved for backups
   conflates two different reliability/retention expectations. It
   MAY be an excellent target for the mandate's own (separately
   deferred) integrity-checkpoint / future-backup-pipeline storage —
   flagged as a candidate for that, explicitly not decided here.

## What this report does NOT do

- Does not decide whether 126G is "enough" forever — that is exactly
  what `storage_policy.py`'s ongoing, live threshold checks are for,
  not a one-time capacity plan.
- Does not touch, clean, or recommend cleaning anything under `/var/www`,
  `/var/log`, `/mnt/ssd480`, or any other tenant's data on this shared
  host.
- Does not measure `/var/lib/mysql`'s actual size (permission denied as
  a non-root user — expected, and itself confirms the shared instance's
  datadir is correctly locked down at `0700`).
