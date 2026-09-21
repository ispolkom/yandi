#!/usr/bin/env python3
"""Mutation check of the Rust core supervisor (node/core_supervisor).

Each mutant is a copy of the crate with ONE deliberate defect; the named tests must then fail. A mutant that survives means a
test suite that would not notice that defect in the real code. The unmodified copy (the control) must pass the same tests first.

  S1  a core crash kills the node                 S6  a restart reuses the launch secret
  S2  restarts without waiting (busy loop)        S7  the launch secret appears in the child's argv
  S3  the failure ceiling is ignored              S8  the secret file / directory permissions are unsafe
  S4  the core survives the node's shutdown       S9  the MASTER key is sent instead of the derived key
  S5  the node attaches to a core it did not spawn S10 a key is invented when the node has none
  S14 a refused key is retried by restarting

Run: python scripts/supervisor_mutants.py [--only S3,S5]   (needs cargo and the crates in the local registry cache: offline)
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "node" / "core_supervisor"
LOCK = ROOT / "node" / "Cargo.lock"
TARGET_DIR = ROOT / "node" / "target-mutants"   # its OWN build directory: mutant builds must never overwrite the real binaries and libraries          # the dependencies are already compiled here; only the crate itself is rebuilt

SUP = "src/supervisor.rs"
RT = "src/runtime.rs"
CFG = "src/config.rs"
KEYS = "src/keys.rs"

SV = ("--test", "supervision")
LIB = ("--lib",)

MUTANTS = [
    ("S1", "a core crash kills the node", {SUP: [("                self.emit(Event::CoreExited { launch, code });\n", "                self.emit(Event::CoreExited { launch, code });\n                std::process::exit(70);\n")]},
     [SV + ("a_crash_is_survived_and_the_restart_gets_a_fresh_secret_and_is_unlocked_again",)]),
    ("S2", "restarts do not wait (busy loop)", {SUP: [("            let delay = self.cfg.backoff.delay(self.restarts.len() as u32);\n", "            let delay = Duration::ZERO;\n")]},
     [SV + ("restarts_wait_exponentially_and_never_spin",)]),
    ("S2b", "the backoff does not grow", {CFG: [("            d = d.saturating_mul(self.factor.max(1));\n", "            d = d.saturating_mul(1);\n")]},
     [SV + ("backoff_grows_by_the_factor_and_never_exceeds_its_maximum",)]),
    ("S3", "the failure ceiling is ignored", {SUP: [("            if self.restarts.len() as u32 >= self.cfg.restart_limit {\n", "            if false {\n")]},
     [SV + ("after_the_limit_restarting_stops_and_the_node_stays_up",)]),
    ("S4", "the core survives the node's shutdown", {SUP: [("        self.set(Phase::Stopping, None);\n        self.emit(Event::ShutdownRequested);\n", "        self.set(Phase::Stopping, None);\n        self.emit(Event::ShutdownRequested);\n        if true {\n            return SupEnd::Stopped(ShutdownReport::default());\n        }\n"),
                                                                                            ("        cmd.kill_on_drop(true);\n        cmd.process_group(0);", "        cmd.kill_on_drop(false);\n        cmd.process_group(0);")]},
     [SV + ("shutdown_escalates_from_the_request_to_term_and_leaves_no_orphan", "kill_is_the_last_resort_and_still_leaves_no_orphan")]),
    ("S5", "the node attaches to a port it did not prove is its child's", {SUP: [("                if owns_loopback_listener(pid, p) {\n", "                if true {\n")]},
     [SV + ("a_port_the_child_does_not_own_is_never_contacted",)]),
    ("S6", "a restart reuses the launch secret", {RT: [("        let secret = LaunchSecret::generate();\n", "        let secret = LaunchSecret(Zeroizing::new(\"ab\".repeat(32)));\n")]},
     [SV + ("a_crash_is_survived_and_the_restart_gets_a_fresh_secret_and_is_unlocked_again",), LIB + ("runtime::tests::every_launch_gets_a_different_secret_and_directory",)]),
    ("S7", "the launch secret is on the child's command line", {SUP: [("        cmd.stdin(Stdio::null());\n        if let Some(log)", "        cmd.arg(std::fs::read_to_string(dir.secret_path()).unwrap_or_default().trim());\n        cmd.stdin(Stdio::null());\n        if let Some(log)")]},
     [SV + ("the_launch_secret_is_in_a_private_file_only",)]),
    ("S8", "the secret file is readable by others", {RT: [("            .mode(0o600)\n            .custom_flags(libc::O_NOFOLLOW)", "            .mode(0o644)\n            .custom_flags(libc::O_NOFOLLOW)")]},
     [SV + ("the_launch_secret_is_in_a_private_file_only",), LIB + ("runtime::tests::a_new_launch_has_a_private_directory_and_a_private_secret_file",)]),
    ("S8b", "the launch directory is enterable by others", {RT: [("        DirBuilder::new().mode(0o700).create(&dir)?;\n", "        DirBuilder::new().mode(0o755).create(&dir)?;\n")]},
     [SV + ("the_launch_secret_is_in_a_private_file_only",), LIB + ("runtime::tests::a_new_launch_has_a_private_directory_and_a_private_secret_file",)]),
    ("S9", "the master key is sent instead of the derived key", {KEYS: [("    Hkdf::<Sha256>::new(None, master_key)\n        .expand(CORE_CONTEXT.as_bytes(), okm.as_mut_slice())\n        .expect(\"32 bytes is a valid HKDF-SHA256 output length\");\n", "    okm.copy_from_slice(master_key);\n")]},
     [LIB + ("keys::tests::matches_the_python_core_library_vector", "keys::tests::the_derived_key_is_not_the_master_key"), SV + ("spawn_waits_for_locked_unlocks_with_the_derived_key_and_waits_for_ready",)]),
    ("S10", "a key is invented when the node has none", {SUP: [("            if let Some(m) = (self.key)() {\n                break m;\n            }\n", "            break (self.key)().unwrap_or_else(|| Zeroizing::new([0u8; 32]));\n")]},
     [SV + ("without_a_master_key_the_core_stays_locked_and_nothing_is_invented",)]),
    ("S14", "a refused key is retried by restarting", {SUP: [("            if category == FailureCategory::CoreUnlockRefused {\n", "            if false {\n")]},
     [SV + ("a_refused_key_is_final_safe_and_leaves_no_process",)]),
]


def cargo_test(manifest: Path, group: tuple, timeout: int) -> tuple[int, str]:
    """group = ("--test", "supervision", name, ...) or ("--lib", name, ...): run exactly those tests, one at a time."""
    selector, names = (group[:2], group[2:]) if group[0] == "--test" else (group[:1], group[1:])
    cmd = ["cargo", "test", "--offline", "--manifest-path", str(manifest), *selector, "--", *names, "--exact", "--test-threads=1"]
    env = {**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(manifest.parent))
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def make_copy(dest: Path, edits: dict[str, list[tuple[str, str]]]) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CRATE, dest, ignore=shutil.ignore_patterns("target", "__pycache__"))
    shutil.copy(LOCK, dest / "Cargo.lock")
    for rel, changes in edits.items():
        path = dest / rel
        text = path.read_text(encoding="utf-8")
        for old, new in changes:
            if text.count(old) != 1:
                raise SystemExit(f"mutant anchor not found exactly once in {rel}: {old[:70]!r}")
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    # cargo decides "up to date" by file times, and the build directory is shared: every file of every copy must look newer than
    # the artefacts of the copy before it, or a stale (mutated) binary would be run again.
    for path in dest.rglob("*"):
        if path.is_file():
            os.utime(path, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated mutant ids")
    args = ap.parse_args()
    wanted = set(args.only.split(",")) if args.only else None
    work = Path(tempfile.mkdtemp(prefix="yandi-supervisor-mutants-"))
    survivors: list[str] = []
    try:
        all_groups = [g for _id, _d, _e, groups in MUTANTS for g in groups]
        make_copy(work / "core_supervisor", {})
        manifest = work / "core_supervisor" / "Cargo.toml"
        print("control (no defect): the tests every mutant is judged by must pass")
        for group in dict.fromkeys(all_groups):
            code, out = cargo_test(manifest, group, 240)
            ok = code == 0 and "test result: ok. 0 passed" not in out
            print(f"  [{'OK' if ok else 'FAIL'}] {' '.join(group)}")
            if not ok:
                print(out[-1500:])
                return 2
        print()
        for mid, desc, edits, groups in MUTANTS:
            if wanted and mid not in wanted:
                continue
            make_copy(work / "core_supervisor", edits)
            began = time.monotonic()
            results = []
            for group in groups:
                code, out = cargo_test(manifest, group, 240)
                if "error[E" in out or "could not compile" in out:
                    print(f"  [ERR ] {mid} {desc}: the mutant does not compile\n{out[-800:]}")
                    return 2
                results.append(code != 0)
            caught = any(results)
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {mid:<4} {desc}  ({time.monotonic() - began:.0f}s; failing tests: {sum(results)}/{len(results)})")
            if not caught:
                survivors.append(mid)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print()
    if survivors:
        print(f"SURVIVING MUTANTS: {survivors}")
        return 1
    print("all mutants caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
