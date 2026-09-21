#!/usr/bin/env python3
"""Mutation check of node/key_root (P1c-1): each mutant is a copy of the crate with ONE deliberate defect; the named tests must then fail.

  K1  a failed decrypt creates a new identity          K7  the recovery password is hashed with a plain hash, not Argon2id
  K2  a failed decrypt overwrites the old identity     K8  the password is printed
  K3  a wrong password changes bytes on disk           K9  a migration that fails verification cannot restore the originals
  K4  a changed machine id creates a new identity          (K9b: a migration that keeps no backup of the originals)
  K5  a corrupt identity creates a new one             K10 recovery returns a different node identity
  K6  the machine id is used as key material           K11 recovery on a new machine needs the old machine id
                                                       K12 setting YANDI_KEY_PASSWORD overwrites a legacy identity
  and: K13 loose file permissions accepted, K14 KDF floor removed, K15 atomic write skips its verification, K16 public identity fields unauthenticated,
  K17 a typo in a recovery code accepted, K18 a new code committed unconfirmed, K19 the old recovery secret still works, K20 a new code without the device,
  K21 core-key prints the root instead of the derived key, K22 core-key prints into a terminal,
  L1, L3-L7 the shared web account (login.rs): wrong password accepted, no backup on reset, weak inputs, tool exit codes
  (L2, removing the device-key-exists guard of create_keys, was tried and is EQUIVALENT: the device key file is created exclusively, so the second guard is redundant)

Run: python scripts/key_root_mutants.py [--only K1,K6]   (needs cargo; offline)
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
CRATE = ROOT / "node" / "key_root"
LOCK = ROOT / "node" / "Cargo.lock"
TARGET_DIR = ROOT / "node" / "target-mutants"   # its OWN build directory: mutant builds must never overwrite the real binaries and libraries

IDS = "src/identity_store.rs"
RC = "src/recovery_code.rs"
MIG = "src/migrate.rs"
RT = "src/root.rs"
WR = "src/wrap.rs"
KD = "src/keydir.rs"
AT = "src/atomic.rs"
CLI = "src/bin/yandi-keys.rs"

T = "--test", "key_root"
LT = "--test", "login"
LG = "src/login.rs"

F11_ALL = ("f11_a_damaged_identity_is_never_replaced", "f11_setting_yandi_key_password_never_destroys_a_legacy_identity", "f11_a_changed_machine_id_never_replaces_a_legacy_identity")
OVERWRITE_ON_ERR = (
    "            let identity = match open_identity(&bytes, ctx) {\n"
    "                Ok(i) => i,\n"
    "                Err(_) => {\n"
    "                    let m = make();\n"
    "                    let b = seal_identity_legacy(&m, &legacy_passphrase(ctx.env_password, ctx.machine, &m.address))?;\n"
    "                    write_atomic(&path, &b, |_| true)?;\n"
    "                    return Ok(Loaded { identity: m, created: true });\n"
    "                }\n"
    "            };\n"
)
OPEN_STRICT = "            let identity = open_identity(&bytes, ctx)?;\n"

MUTANTS = [
    ("K1", "a failed decrypt creates a new identity (the file is left)", {IDS: [(OPEN_STRICT, "            let identity = match open_identity(&bytes, ctx) {\n                Ok(i) => i,\n                Err(_) => return Ok(Loaded { identity: make(), created: true }),\n            };\n")]}, [T + F11_ALL]),
    ("K2", "a failed decrypt overwrites the old identity", {IDS: [(OPEN_STRICT, OVERWRITE_ON_ERR)]}, [T + F11_ALL]),
    ("K3", "a wrong password leaves a trace on disk", {MIG: [("        let root: Zeroizing<[u8; 32]> = doc.unlock_with_password(self.password, self.policy)?;\n", "        let root: Zeroizing<[u8; 32]> = match doc.unlock_with_password(self.password, self.policy) {\n            Ok(r) => r,\n            Err(e) => {\n                let _ = crate::atomic::create_new_file(&self.dir.file(\"attempts\"), b\"1\");\n                return Err(e);\n            }\n        };\n")]}, [T + ("losing_the_device_is_not_losing_the_identity",)]),
    ("K4", "a changed machine id (a failed decrypt) yields a NEW identity", {IDS: [(OPEN_STRICT, "            let identity = match open_identity(&bytes, ctx) {\n                Ok(i) => i,\n                Err(KeyRootError::DecryptFailed) => return Ok(Loaded { identity: make(), created: true }),\n                Err(e) => return Err(e),\n            };\n")]}, [T + ("f11_a_changed_machine_id_never_replaces_a_legacy_identity",)]),
    ("K5", "a corrupt identity file yields a NEW identity", {IDS: [(OPEN_STRICT, "            let identity = match open_identity(&bytes, ctx) {\n                Ok(i) => i,\n                Err(KeyRootError::Corrupt) => return Ok(Loaded { identity: make(), created: true }),\n                Err(e) => return Err(e),\n            };\n")]}, [T + ("f11_a_damaged_identity_is_never_replaced",)]),
    ("K6", "the machine id is the key material of the device wrapper", {RT: [("        let (dn, dc) = seal(device_key, &device_aad(&id, &machine.id()), root);", "        let mk: [u8; 32] = { use sha2::{Digest, Sha256}; Sha256::digest(machine.id().as_bytes()).into() };\n        let (dn, dc) = seal(&mk, &device_aad(&id, &machine.id()), root);"), ("        match open(\n            device_key,\n            &device_aad(&self.root_id, &machine.id()),", "        let mk: [u8; 32] = { use sha2::{Digest, Sha256}; Sha256::digest(machine.id().as_bytes()).into() };\n        match open(\n            &mk,\n            &device_aad(&self.root_id, &machine.id()),")]}, [T + ("the_device_key_is_the_secret_and_the_machine_id_is_not",)]),
    ("K7", "the recovery password is hashed with a plain SHA-256", {WR: [("    Argon2::new(Algorithm::Argon2id, Version::V0x13, p)\n        .hash_password_into(password, salt, out.as_mut_slice())\n        .map_err(|_| KeyRootError::WeakParameters)?;", "    {\n        use sha2::{Digest, Sha256};\n        let mut h = Sha256::new();\n        h.update(password);\n        h.update(salt);\n        out.copy_from_slice(&h.finalize());\n        let _ = (&p, Algorithm::Argon2id, Version::V0x13, Argon2::default());\n    }")]}, [T + ("the_password_goes_through_argon2id_and_stored_parameters_below_the_policy_are_refused",)]),
    ("K8", "the password is printed to the log", {CLI: [('                let again = ask("Repeat it: ", args.stdin, !args.show)?;\n', '                let again = ask("Repeat it: ", args.stdin, !args.show)?;\n                eprintln!("debug: password {}", &*pw);\n')]}, [T + ("the_tool_migrates_and_recovers_and_never_prints_a_secret",)]),
    ("K9", "a failed migration does not restore the originals", {MIG: [("            let _ = write_atomic(&auth_path, &auth_bytes, |_| true);\n            if !already_v3 {\n                let _ = write_atomic(&identity_path, &identity_bytes, |_| true);\n            }\n            return Err(e);", "            return Err(e);")]}, [T + ("a_migration_that_fails_verification_restores_the_originals_and_keeps_the_backups",)]),
    ("K9b", "a migration keeps no backup of the originals", {MIG: [('        let mut backups = vec![backup_copy(&auth_path, "legacy")?];\n        if !already_v3 {\n            backups.push(backup_copy(&identity_path, "legacy")?);\n        }', "        let backups: Vec<PathBuf> = Vec::new();")]}, [T + ("migration_keeps_the_same_root_the_same_identity_and_every_original", "a_migration_that_fails_verification_restores_the_originals_and_keeps_the_backups")]),
    ("K10", "recovery leaves a DIFFERENT node identity behind", {MIG: [('        let mut backups = vec![backup_copy(&auth_path, "before-recovery")?];\n', '        let mut backups = vec![backup_copy(&auth_path, "before-recovery")?];\n        let sub = IdentityMaterial { address: [0xAB; 32], public_key: material.public_key, signing_public_key: material.signing_public_key, created_at: material.created_at.clone(), private_key: Zeroizing::new(*material.private_key), signing_private_key: Zeroizing::new(*material.signing_private_key) };\n        write_atomic(&self.dir.identity_file(self.port), &seal_identity_v3(&root, &sub), |_| true)?;\n')]}, [T + ("losing_the_device_is_not_losing_the_identity",)]),
    ("K11", "a new machine needs the OLD machine id to recover", {RT: [("        let (rn, rc) = seal(&kek, &recovery_aad(&id, &params), root);", "        let (rn, rc) = seal(&kek, &recovery_aad(&format!(\"{id}|{}\", machine.id()), &params), root);")]}, [T + ("recovery_does_not_depend_on_any_machine_id", "the_root_opens_by_device_and_by_password_and_it_is_the_same_root")]),
    ("K12", "setting YANDI_KEY_PASSWORD overwrites a legacy identity", {IDS: [(OPEN_STRICT, "            let identity = match open_identity(&bytes, ctx) {\n                Ok(i) => i,\n                Err(_) if ctx.env_password.is_some() => {\n                    let m = make();\n                    let b = seal_identity_legacy(&m, &legacy_passphrase(ctx.env_password, ctx.machine, &m.address))?;\n                    write_atomic(&path, &b, |_| true)?;\n                    return Ok(Loaded { identity: m, created: true });\n                }\n                Err(e) => return Err(e),\n            };\n")]}, [T + ("f11_setting_yandi_key_password_never_destroys_a_legacy_identity",)]),
    ("K13", "a key file readable by others is accepted", {KD: [("    if meta.file_type().is_symlink()\n        || !meta.is_file()\n        || meta.uid() != euid()\n        || meta.mode() & 0o077 != 0\n    {", "    if meta.file_type().is_symlink() || !meta.is_file() || meta.uid() != euid() {")]}, [T + ("the_device_key_file_is_private_and_a_loose_one_is_refused", "f11_an_unsafe_identity_file_is_refused_and_left_alone")]),
    ("K14", "the KDF cost floor is not enforced", {WR: [("    if params.memory_kib < policy.min_memory_kib\n        || params.iterations < policy.min_iterations\n        || params.parallelism == 0", "    if params.parallelism == 0")]}, [T + ("the_password_goes_through_argon2id_and_stored_parameters_below_the_policy_are_refused",)]),
    ("K15", "an atomic write skips its read-back verification", {AT: [("        if back != bytes || !verify(&back) {", "        if false {")]}, [T + ("a_failed_verification_leaves_the_original_untouched_and_no_temporary_file",)]),
    ("K16", "the public fields of the identity file are not authenticated", {IDS: [('    format!(\n        "yandi/identity-file/v3|{}|{}|{}|{}",\n        s.address, s.public_key, s.signing_public_key, s.root_id\n    )\n    .into_bytes()', '    format!("yandi/identity-file/v3|{}", s.root_id).into_bytes()')]}, [T + ("identity_v3_opens_only_with_its_root_and_authenticates_its_public_fields",)]),
    ("K17", "a recovery code with a typo (bad check) is accepted", {RC: [("        if check_group(payload) != check {", "        if false {")]}, [T + ("a_typed_code_is_read_forgivingly_but_a_typo_is_named_as_a_typo",)]),
    ("K18", "a new recovery code is committed without the owner typing it back correctly", {MIG: [("        if !typed.same_as(&pending.code) {", "        if false {")]}, [T + ("replacing_the_recovery_secret_by_a_code_needs_the_device_and_the_typed_confirmation",)]),
    ("K19", "the OLD recovery secret still works after a new code is set", {RT: [("        next.recovery = RecoveryWrapper {", "        let _dead = RecoveryWrapper {")]}, [T + ("replacing_the_recovery_secret_by_a_code_needs_the_device_and_the_typed_confirmation", "the_new_code_recovers_the_same_identity_on_another_machine_and_the_old_password_does_not")]),
    ("K20", "a new recovery code can be set without the device opening the key", {MIG: [("        doc.unlock_with_device(&key, self.machine)?; // only the device may replace the recovery secret\n", "")]}, [T + ("replacing_the_recovery_secret_by_a_code_needs_the_device_and_the_typed_confirmation",)]),
    ("K21", "core-key prints the ROOT key instead of the derived Core key", {CLI: [("                    let key = derive_domain(&root, DOMAIN_CORE);\n", "                    let key = root;\n")]}, [T + ("core_key_prints_exactly_the_key_the_node_gives_the_core_and_nothing_else",)]),
    ("K22", "core-key prints into a terminal", {CLI: [("            if unsafe { libc::isatty(1) } == 1 {\n", "            if false {\n")]}, [T + ("core_key_never_prints_into_a_terminal",)]),
    ("L1", "the login password check says yes to a wrong password", {LG: [("        .verify_password(password.as_bytes(), &hash)\n        .is_ok()", "        .verify_password(password.as_bytes(), &hash)\n        .is_ok()\n        || true")]}, [LT + ("the_keys_made_from_what_the_person_typed_open_two_ways_and_the_login_password_checks",)]),
    ("L3", "a login reset keeps no copy of the previous file", {LG: [("    crate::atomic::backup_copy(&auth_path, \"before-login-reset\")\n        .map_err(|_| \"Не удалось сохранить копию ключей; ничего не изменено\".to_string())?;", "    let _ = &auth_path;")]}, [LT + ("the_master_password_resets_the_login_password_and_touches_nothing_else",)]),
    ("L4", "the master password may equal the login password", {LG: [("    if master == login {", "    if false {")]}, [LT + ("the_rules_for_what_the_person_typed",)]),
    ("L5", "a too-short login password is accepted at setup", {LG: [("    if login.chars().count() < 8 {", "    if false {")]}, [LT + ("the_rules_for_what_the_person_typed", "the_tool_creates_the_account_checks_the_password_and_resets_it_and_never_prints_a_secret")]),
    ("L6", "a too-short NEW login password is accepted at reset", {LG: [("    if new_login_password.chars().count() < 8 {", "    if false {")]}, [LT + ("a_wrong_or_mistyped_secret_or_a_short_new_password_changes_nothing",)]),
    ("L7", "the tool's login-check exits 0 for a wrong password", {CLI: [("                    eprintln!(\"yandi-keys: login_wrong\");\n                    Ok(1)", "                    eprintln!(\"yandi-keys: login_wrong\");\n                    Ok(0)")]}, [LT + ("the_tool_creates_the_account_checks_the_password_and_resets_it_and_never_prints_a_secret",)]),
]


def cargo_test(manifest: Path, group: tuple, timeout: int) -> tuple[int, str]:
    selector, names = group[:2], group[2:]
    cmd = ["cargo", "test", "--offline", "--manifest-path", str(manifest), *selector, "--", *names, "--exact", "--test-threads=1"]
    env = {**os.environ, "CARGO_TARGET_DIR": str(TARGET_DIR)}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env, cwd=str(manifest.parent))
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def make_copy(dest: Path, edits: dict) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CRATE, dest, ignore=shutil.ignore_patterns("target", "__pycache__"))
    shutil.copy(LOCK, dest / "Cargo.lock")
    for rel, changes in edits.items():
        path = dest / rel
        text = path.read_text(encoding="utf-8")
        for old, new in changes:
            if text.count(old) != 1:
                raise SystemExit(f"mutant anchor not found exactly once in {rel}: {old[:80]!r}")
            text = text.replace(old, new)
        path.write_text(text, encoding="utf-8")
    for path in dest.rglob("*"):          # cargo judges freshness by file times, and the build directory is shared
        if path.is_file():
            os.utime(path, None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    args = ap.parse_args()
    wanted = set(args.only.split(",")) if args.only else None
    work = Path(tempfile.mkdtemp(prefix="yandi-keyroot-mutants-"))
    for mid, _d, edits, _g in MUTANTS:            # every anchor must exist exactly once before anything runs
        make_copy(work / "anchors", edits)
    survivors: list[str] = []
    try:
        make_copy(work / "key_root", {})
        manifest = work / "key_root" / "Cargo.toml"
        print("control (no defect): the tests every mutant is judged by must pass", flush=True)
        groups = list(dict.fromkeys(g for _i, _d, _e, gs in MUTANTS for g in gs))
        for group in groups:
            code, out = cargo_test(manifest, group, 300)
            ok = code == 0 and "0 passed" not in out.split("test result:")[-1][:40]
            print(f"  [{'OK' if ok else 'FAIL'}] {' '.join(group[2:])[:110]}", flush=True)
            if not ok:
                print(out[-1500:])
                return 2
        print(flush=True)
        for mid, desc, edits, gs in MUTANTS:
            if wanted and mid not in wanted:
                continue
            make_copy(work / "key_root", edits)
            began = time.monotonic()
            results = []
            for group in gs:
                code, out = cargo_test(manifest, group, 300)
                if "error[E" in out or "could not compile" in out:
                    print(f"  [ERR ] {mid} {desc}: the mutant does not compile\n{out[-900:]}")
                    return 2
                results.append(code != 0)
            caught = any(results)
            print(f"  [{'CAUGHT' if caught else 'SURVIVED'}] {mid:<4} {desc}  ({time.monotonic() - began:.0f}s)", flush=True)
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
