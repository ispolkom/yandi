#!/usr/bin/env python3
"""
scripts/install_extension.py — installs the YANDI Council Bridge into Firefox ESR through an enterprise policy.

    sudo python3 scripts/install_extension.py             # build the .xpi, merge the policy, done
    python3 scripts/install_extension.py --dry-run        # print the policy that would be written
    sudo python3 scripts/install_extension.py --uninstall # remove exactly what this script added

The policy is MERGED into any existing policies.json (a backup is kept next to it); nothing you configured is
overwritten. The add-on is unsigned, so the policy also switches off signature enforcement
(xpinstall.signatures.required = false, locked): that is only honoured by Firefox ESR / Developer Edition /
Nightly, not by the regular release build, and it lets ANY unsigned add-on install in that Firefox. If that is not
acceptable, use a temporary install instead (about:debugging → This Firefox → Load Temporary Add-on → pick
pet/extension/manifest.json); it lasts until the browser restarts.

Firefox reads the policy at startup: restart it after installing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
XPI = ROOT / "pet" / "council_bridge.xpi"
ADDON_ID = "council-bridge@yandi.local"
SIGNATURE_PREF = "xpinstall.signatures.required"
DEFAULT_DIRS = ("/usr/lib/firefox-esr/distribution", "/usr/lib64/firefox-esr/distribution", "/opt/firefox/distribution")


def merged(existing: dict, xpi: Path) -> dict:
    """`existing` policies plus this add-on's entries; every other key is left exactly as it was."""
    policies = json.loads(json.dumps(existing))          # deep copy
    inner = policies.setdefault("policies", {})
    inner.setdefault("Preferences", {})[SIGNATURE_PREF] = {"Value": False, "Status": "locked"}
    inner.setdefault("ExtensionSettings", {})[ADDON_ID] = {
        "installation_mode": "force_installed",
        "install_url": xpi.resolve().as_uri(),
    }
    return policies


def without(existing: dict) -> dict:
    """`existing` policies minus the entries `merged` adds (the signature pref only if nothing else needs it)."""
    policies = json.loads(json.dumps(existing))
    inner = policies.get("policies", {})
    inner.get("ExtensionSettings", {}).pop(ADDON_ID, None)
    if not inner.get("ExtensionSettings"):
        inner.pop("ExtensionSettings", None)
    inner.get("Preferences", {}).pop(SIGNATURE_PREF, None)
    if not inner.get("Preferences"):
        inner.pop("Preferences", None)
    return policies


def find_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    for candidate in DEFAULT_DIRS:
        if Path(candidate).parent.is_dir():
            return Path(candidate)
    raise SystemExit("Firefox ESR was not found (looked in " + ", ".join(DEFAULT_DIRS) + "); pass --policies-dir DIR, "
                     "or use the temporary install described in --help")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policies-dir", help="Firefox's distribution/ directory (default: auto-detect Firefox ESR)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--no-build", action="store_true", help="use the existing pet/council_bridge.xpi as it is")
    args = parser.parse_args(argv)

    directory = find_dir(args.policies_dir)
    path = directory / "policies.json"
    existing = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"REFUSED: {path} is not valid JSON ({exc}); fix or move it first, nothing was changed", file=sys.stderr)
            return 1

    if args.uninstall:
        result = without(existing)
    else:
        if not args.no_build:
            build = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py"), "--out", str(XPI)],
                                   capture_output=True, text=True)
            print(build.stdout.strip() or build.stderr.strip())
            if build.returncode != 0:
                return 1
        elif not XPI.is_file():
            print(f"REFUSED: {XPI} does not exist", file=sys.stderr)
            return 1
        result = merged(existing, XPI)

    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(f"# would write {path}\n{text}", end="")
        return 0
    try:
        directory.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            backup = path.with_name(f"policies.json.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(path, backup)
            print(f"backup: {backup}")
        path.write_text(text, encoding="utf-8")
    except PermissionError:
        print(f"REFUSED: no permission to write {path}; run with sudo (nothing was changed)", file=sys.stderr)
        return 1
    print(f"{'removed from' if args.uninstall else 'installed into'} {path}; restart Firefox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
