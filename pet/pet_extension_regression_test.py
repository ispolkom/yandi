"""
pet/pet_extension_regression_test.py — THE FIREFOX EXTENSION IS BUILDABLE, CURRENT, MINIMAL IN WHAT IT MAY DO, AND
TALKS ONLY TO ENDPOINTS THAT EXIST.

Static checks (no browser): the manifest is strict JSON and complete, the committed pet/council_bridge.xpi is exactly
what pet/extension/ builds (a stale or hand-edited artifact fails), the permissions stay narrow, untrusted text is never
put into the page as HTML, every server path the extension calls is a route the server registers, and the installer merges
into an existing enterprise policy instead of overwriting it. The behaviour in a real Firefox is checked by
pet/extension_firefox_e2e.py (needs a browser, so it is not part of the core suite).

Run: python -m pet.pet_extension_regression_test
"""
from __future__ import annotations

import io
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def strip_comments(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(^|[^:\"'])//[^\n]*", r"\1", js)


def server_routes() -> set[str]:
    routes = set()
    for path in (ROOT / "pet").glob("*.py"):
        if path.name.endswith("_test.py"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r'@(?:app|router)\.(?:get|post|put|delete)\(\s*"([^"]+)"', text):
            routes.add(m.group(1))
    return routes


def main() -> int:
    import build_extension as be
    import install_extension as ie

    src = ROOT / "pet" / "extension"
    manifest, files = be.validate(src)

    # ── 1. the manifest ──
    check("1: manifest.json is strict JSON without duplicate keys and refers only to files that exist", True)
    check("1: it declares a stable add-on id and a dotted version", manifest["browser_specific_settings"]["gecko"]["id"] == "council-bridge@yandi.local"
          and re.fullmatch(r"\d+(\.\d+)+", manifest["version"]) is not None)
    check("1: it has an icon, a popup and the verify shortcut", bool(manifest.get("icons")) and manifest["browser_action"].get("default_popup")
          and "verify-selection" in manifest.get("commands", {}))

    def broken(mutate):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(src, tmp / "e", ignore=shutil.ignore_patterns("*.xpi"))
            mutate(tmp / "e")
            try:
                be.build_bytes(tmp / "e")
            except be.BuildError as exc:
                return str(exc)
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    mp = lambda d: d / "manifest.json"
    check("1: MUTANT a trailing comma (what the old artifact had) is refused by the build",
          broken(lambda d: mp(d).write_text(mp(d).read_text().replace('"kimi.com/*"\n  ]', '"kimi.com/*",\n  ]').replace('"*://kimi.com/*"\n  ]', '"*://kimi.com/*",\n  ]'))) is not None)
    check("1: MUTANT a duplicate key (the old artifact's two content_scripts blocks) is refused by the build",
          "duplicate key" in (broken(lambda d: mp(d).write_text(mp(d).read_text().replace('"commands"', '"content_scripts": [], "commands"', 1))) or ""))
    check("1: MUTANT a file the manifest refers to is missing -> refused", "missing" in (broken(lambda d: (d / "popup.js").unlink()) or ""))
    check("1: MUTANT a script the background injects on demand is missing -> refused (it is not in the manifest)",
          "missing" in (broken(lambda d: (d / "content_verify.js").unlink()) or ""))
    check("1: MUTANT a JavaScript syntax error is refused", "syntax" in (broken(lambda d: (d / "popup.js").write_text("function (")) or "").lower()
          or shutil.which("node") is None)

    # ── 2. the committed artifact ──
    xpi = ROOT / "pet" / "council_bridge.xpi"
    built = be.build_bytes(src)
    check("2: pet/council_bridge.xpi is EXACTLY what pet/extension/ builds (rebuild with scripts/build_extension.py)", xpi.is_file() and xpi.read_bytes() == built)
    names = zipfile.ZipFile(io.BytesIO(built)).namelist()
    check("2: the package holds the manifest at its root and every file the extension needs, including the injected overlay",
          "manifest.json" in names and "content_verify.js" in names and "config.js" in names and set(names) == set(files), repr(names))
    check("2: the build is reproducible (same source, same bytes)", be.build_bytes(src) == built)
    check("2: the stale duplicate package is gone", not (src / "yandi-bridge.xpi").exists())

    # ── 3. what the extension may do ──
    perms = manifest["permissions"]
    hosts = [p for p in perms if "://" in p or p == "<all_urls>"]
    check("3: NO all-sites permission (the extension is not given every page you open)", "<all_urls>" not in perms and not any(h.startswith("*://*/") or h == "*://*/*" for h in hosts))
    check("3: it may call the local YANDI server and the four chat sites, nothing else",
          set(hosts) == {"http://127.0.0.1:9010/*", "*://claude.ai/*", "*://chatgpt.com/*", "*://chat.deepseek.com/*", "*://www.kimi.com/*", "*://kimi.com/*"}, repr(hosts))
    matches = {m for block in manifest["content_scripts"] for m in block["matches"]}
    check("3: no content script runs on ordinary pages (the overlay is injected only when the user acts)",
          matches <= {"*://claude.ai/*", "*://chatgpt.com/*", "*://chat.deepseek.com/*", "*://www.kimi.com/*", "*://kimi.com/*"}, repr(matches))
    check("3: the overlay is not a manifest content script; the background injects it (activeTab)",
          all("content_verify.js" not in block["js"] for block in manifest["content_scripts"]) and "activeTab" in perms
          and "executeScript" in (src / "background.js").read_text(encoding="utf-8"))
    cfg = (src / "config.js").read_text(encoding="utf-8")
    check("3: the server address in config.js is exactly the one host the manifest permits", 'API: "http://127.0.0.1:9010"' in cfg)

    # ── 4. untrusted text never becomes page HTML ──
    danger = re.compile(r"\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write|\beval\(|new Function\(")
    offenders = {name: danger.findall(strip_comments((src / name).read_text(encoding="utf-8")))
                 for name in files if name.endswith(".js")}
    check("4: no extension script assigns HTML, evaluates strings or writes documents (the page text and the model's answer are shown via textContent)",
          not any(offenders.values()), repr({k: v for k, v in offenders.items() if v}))
    verify = strip_comments((src / "content_verify.js").read_text(encoding="utf-8"))
    check("4: the overlay lives in a CLOSED shadow root and does no network I/O itself",
          'mode: "closed"' in verify and "fetch(" not in verify and "XMLHttpRequest" not in verify)
    bg = strip_comments((src / "background.js").read_text(encoding="utf-8"))
    check("4: the background answers messages only from this extension's own scripts", "sender.id !== browser.runtime.id" in bg)

    # ── 5. every path the extension calls exists on the server ──
    routes = server_routes()

    def called_paths(text: str) -> set[str]:
        text = strip_comments(text)
        out = set(re.findall(r"(/api/[A-Za-z0-9_/\-]+)", text))
        out.update("/api/ext" + m for m in re.findall(r"\$\{API\}(/[A-Za-z0-9_/\-]+)", text))
        out.update("/api/ext/orch" + m for m in re.findall(r"\$\{ORCH_API\}(/[A-Za-z0-9_/\-]+)", text))
        out.discard("/api/ext")
        out.discard("/api/ext/orch")
        return out

    called = set()
    for name in files:
        if name.endswith(".js"):
            called |= called_paths((src / name).read_text(encoding="utf-8"))
    check("5: the extension calls a known set of server paths", {"/api/orchestrator/ask", "/api/orch/history", "/api/council/connections", "/api/ext/poll",
                                                                "/api/ext/result", "/api/ext/orch/poll", "/api/ext/orch/result"} <= called, repr(sorted(called)))
    old_call = "const s = await fetch(`${YANDI_API}/api/orch/status/${msgId}`);"
    check("5: MUTANT the old overlay's call to /api/orch/status/<id> is recognised as a path the server does not have",
          any(p not in routes for p in called_paths(old_call)) and called_paths(old_call) == {"/api/orch/status/"})
    missing = sorted(p for p in called if p not in routes)
    check("5: every server path the extension calls is a route the server registers (the old overlay waited on /api/orch/status/<id>, which never existed)",
          not missing, repr(missing))

    # ── 6. the installer merges, it does not overwrite ──
    existing = {"policies": {"DisableTelemetry": True, "Preferences": {"browser.foo": {"Value": 1, "Status": "locked"}},
                             "ExtensionSettings": {"other@x": {"installation_mode": "blocked"}}}}
    merged = ie.merged(existing, xpi)
    inner = merged["policies"]
    check("6: the installer keeps every policy that was already there (the old script replaced the whole file)",
          inner["DisableTelemetry"] is True and inner["Preferences"]["browser.foo"] == {"Value": 1, "Status": "locked"}
          and inner["ExtensionSettings"]["other@x"] == {"installation_mode": "blocked"})
    check("6: ... and adds this add-on with the ACTUAL location of the package, not a hard-coded disk path",
          inner["ExtensionSettings"]["council-bridge@yandi.local"]["install_url"] == xpi.resolve().as_uri() and "/media/" not in xpi.resolve().as_uri())
    check("6: installing twice changes nothing more; uninstalling restores what was there", ie.merged(merged, xpi) == merged and ie.without(merged) == existing)
    check("6: the installer's input is not modified in place", existing["policies"].get("ExtensionSettings", {}).get("council-bridge@yandi.local") is None)

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
