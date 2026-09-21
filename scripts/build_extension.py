#!/usr/bin/env python3
"""
scripts/build_extension.py — builds the YANDI Firefox extension (pet/extension/) into an installable .xpi.

    python scripts/build_extension.py                 # validate + write pet/council_bridge.xpi
    python scripts/build_extension.py --out FILE      # another output path
    python scripts/build_extension.py --check         # exit 1 if pet/council_bridge.xpi is not exactly what the
                                                      # source builds (a stale or hand-edited artifact)

What it guarantees before it writes anything:
  * manifest.json is STRICT JSON (no trailing commas, no duplicate keys: a duplicate key silently drops the first
    block, which is how an older artifact lost its content scripts, and Firefox refuses a manifest with a trailing comma);
  * every file the manifest refers to exists (background scripts, content scripts, popup, icons);
  * every JavaScript file passes `node --check` (when node is installed);
  * the package holds exactly the manifest and the files it refers to, and is REPRODUCIBLE: the same source always
    gives the same bytes (sorted entries, fixed timestamps), so `--check` can prove an artifact is current.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "pet" / "extension"
DEFAULT_OUT = ROOT / "pet" / "council_bridge.xpi"
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


class BuildError(Exception):
    pass


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise BuildError(f"manifest.json: duplicate key {key!r} (the later block silently replaces the earlier one)")
        seen[key] = value
    return seen


def load_manifest(src: Path = SRC) -> dict:
    path = src / "manifest.json"
    if not path.is_file():
        raise BuildError(f"{path} does not exist")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as exc:
        raise BuildError(f"manifest.json is not valid JSON: {exc}") from exc
    if manifest.get("manifest_version") != 2:
        raise BuildError("manifest_version must be 2 (Firefox persistent background pages)")
    gecko = (manifest.get("browser_specific_settings") or {}).get("gecko") or {}
    if not gecko.get("id"):
        raise BuildError("browser_specific_settings.gecko.id is required (a stable add-on id)")
    if not re.fullmatch(r"\d+(\.\d+){0,3}", str(manifest.get("version", ""))):
        raise BuildError(f"version {manifest.get('version')!r} is not a dotted number")
    return manifest


def referenced_files(manifest: dict, src: Path = SRC) -> list[str]:
    files = {"manifest.json"}
    files.update(manifest.get("background", {}).get("scripts", []))
    for block in manifest.get("content_scripts", []):
        files.update(block.get("js", []))
        files.update(block.get("css", []))
    action = manifest.get("browser_action", {})
    if action.get("default_popup"):
        files.add(action["default_popup"])
    icons = [*manifest.get("icons", {}).values()]
    default_icon = action.get("default_icon")
    icons += list(default_icon.values()) if isinstance(default_icon, dict) else ([default_icon] if default_icon else [])
    files.update(icons)
    if manifest.get("options_ui", {}).get("page"):
        files.add(manifest["options_ui"]["page"])
    # scripts the background page injects on demand: tabs.executeScript({file: "..."}) — not listed in the manifest,
    # so a package built from the manifest alone would silently lack them
    for script in manifest.get("background", {}).get("scripts", []):
        if (src / script).is_file():
            files.update(re.findall(r'executeScript\([^)]*?\{\s*file:\s*"([^"]+)"', (src / script).read_text(encoding="utf-8")))
    # files that the popup page itself loads
    popup = action.get("default_popup")
    if popup and (src / popup).is_file():
        html = (src / popup).read_text(encoding="utf-8")
        files.update(re.findall(r'<script[^>]+src="([^"]+)"', html))
        files.update(re.findall(r'<link[^>]+href="([^"]+\.css)"', html))
    return sorted(files)


def validate(src: Path = SRC) -> tuple[dict, list[str]]:
    manifest = load_manifest(src)
    files = referenced_files(manifest, src)
    missing = [f for f in files if not (src / f).is_file()]
    if missing:
        raise BuildError(f"referenced but missing: {', '.join(missing)}")
    node = shutil.which("node")
    if node:
        for name in files:
            if name.endswith(".js"):
                proc = subprocess.run([node, "--check", str(src / name)], capture_output=True, text=True)
                if proc.returncode != 0:
                    raise BuildError(f"{name}: JavaScript syntax error\n{proc.stderr.strip()}")
    return manifest, files


def build_bytes(src: Path = SRC) -> bytes:
    import io
    manifest, files = validate(src)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in files:                                    # already sorted
            info = zipfile.ZipInfo(name, FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, (src / name).read_bytes())
    return buf.getvalue()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="fail if the existing artifact differs from a fresh build")
    args = parser.parse_args(argv)
    try:
        data = build_bytes()
    except BuildError as exc:
        print(f"BUILD FAILED: {exc}", file=sys.stderr)
        return 1
    if args.check:
        if not args.out.is_file() or args.out.read_bytes() != data:
            print(f"STALE: {args.out} is not what pet/extension/ builds; run python scripts/build_extension.py", file=sys.stderr)
            return 1
        print(f"OK: {args.out} is current")
        return 0
    args.out.write_bytes(data)
    manifest = load_manifest()
    print(f"built {args.out} — {manifest['name']} {manifest['version']} ({len(data)} bytes, {len(referenced_files(manifest))} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
