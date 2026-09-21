"""
pet/extension_firefox_e2e.py — END-TO-END check of the YANDI Firefox extension in a REAL headless Firefox.

    python -m pet.extension_firefox_e2e

Builds the .xpi from pet/extension/, starts a stub of the YANDI server on 127.0.0.1:9010 (the address the extension is
allowed to call) and a hostile-looking test page on 127.0.0.1:9011, installs the PACKAGE (not the source folder)
as a temporary add-on, and drives it through Marionette:

  * the popup shows the council status the server reports, answers a quick question, and shows a server clarification;
  * Alt+Shift+Y on a selection injects the overlay on demand, the background asks the server, the overlay waits for
    the server's validation and picks it up (the old code waited on an endpoint that does not exist);
  * an answer full of HTML is shown as text (nothing runs), the page cannot read the overlay (closed shadow root);

It needs `firefox-esr` (or `firefox`) and `marionette_driver` (pip install marionette_driver); without them it prints
SKIP and exits 0. It never touches the real YANDI server, any database or any account: the stub answers on the same
loopback port, so it refuses to start when something already listens on 9010.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


XSS = '<img src=x onerror="window.__pwned=1">'


class Stub:
    """A stand-in for the YANDI server: records every request, answers like chat_orch / council_chat_server do
    (including their CORS wildcard: council_chat_server.py allows every origin, and Firefox 140 still applies CORS to the
    extension's requests, so the extension depends on that today)."""

    def __init__(self):
        self.log: list[tuple[str, str, dict | None]] = []
        self.history_polls = 0
        self.task_sent = False
        self.lock = threading.Lock()

    def handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_OPTIONS(self):          # CORS preflight, like the real server's CORSMiddleware
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def _send(self, obj, code=200, ctype="application/json"):
                body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = urlparse(self.path).path
                with stub.lock:
                    stub.log.append(("GET", path, None))
                if path == "/api/council/connections":
                    return self._send({"claude": {"connected": True, "last_seen_sec": 4, "url": ""},
                                       "gpt": {"connected": False, "last_seen_sec": None, "url": ""},
                                       "deepseek": {"connected": False, "last_seen_sec": None, "url": ""},
                                       "kimi": {"connected": False, "last_seen_sec": None, "url": ""}})
                if path == "/api/ext/poll":
                    q = dict(kv.split("=", 1) for kv in urlparse(self.path).query.split("&") if "=" in kv)
                    with stub.lock:
                        if q.get("model") == "claude" and q.get("tab_open") == "true" and not stub.task_sent:
                            stub.task_sent = True
                            return self._send({"task_id": "council-task-1", "text": "Привет, совет: сколько будет два плюс два?"})
                    return self._send({})
                if path == "/api/ext/orch/poll":
                    return self._send({})
                if path == "/api/orch/history":
                    with stub.lock:
                        stub.history_polls += 1
                        done = stub.history_polls >= 2
                    msg = {"id": "m-1", "from": "orchestrator", "text": "x", "trust_level": "VERIFIED" if done else "HYPOTHESIS",
                           "preliminary": not done}
                    return self._send({"messages": [msg]})
                if path == "/page.html":
                    return self._send(PAGE.encode(), ctype="text/html; charset=utf-8")
                if path == "/claude.html":
                    return self._send(FAKE_CLAUDE.encode(), ctype="text/html; charset=utf-8")
                return self._send({"detail": "not found"}, 404)

            def do_POST(self):
                path = urlparse(self.path).path
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    payload = json.loads(self.rfile.read(n) or b"{}")
                except Exception:
                    payload = {}
                with stub.lock:
                    stub.log.append(("POST", path, payload))
                if path == "/api/orchestrator/ask":
                    q = payload.get("query", "")
                    if "нужно уточнение" in q:
                        return self._send({"ok": True, "is_clarification": True, "question": "О какой именно версии речь?",
                                           "missing": ["version"], "domain": "software"})
                    return self._send({"ok": True, "answer": f"Ответ: {XSS} <b>bold</b>", "trust_level": "HYPOTHESIS",
                                       "preliminary": True, "msg_id": "m-1", "domain": "general", "missing": [],
                                       "frame": {"search_queries": ["<i>q1</i>"]}})
                if path == "/api/ext/result":
                    return self._send({"ok": True})
                return self._send({"detail": "not found"}, 404)

        return H


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>page</title></head><body>
<p id="claim">Земля плоская, а вода мокрая.</p>
<p id="claim2">нужно уточнение про версию</p>
<script>
  window.__leak = "none";
  new MutationObserver(() => {
    const host = document.getElementById("yandi-panel");
    if (host) window.__leak = host.shadowRoot ? "readable" : "closed";
  }).observe(document.documentElement, {childList: true, subtree: true});
</script></body></html>"""


FAKE_CLAUDE = """<!doctype html><html><head><meta charset="utf-8"><title>fake claude</title></head><body>
<div id="log"></div>
<div contenteditable="true" class="ProseMirror" id="ed" style="min-height:40px;border:1px solid #888"></div>
<button aria-label="Send message" id="send">Send</button>
<script>
  document.getElementById("send").addEventListener("click", () => {
    const ed = document.getElementById("ed");
    const q = ed.innerText.trim();
    ed.textContent = "";
    setTimeout(() => {
      const d = document.createElement("div");
      d.setAttribute("data-testid", "assistant-message");
      d.textContent = "ЭХО: " + q;
      document.getElementById("log").appendChild(d);
    }, 400);
  });
</script></body></html>"""


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    firefox = shutil.which("firefox-esr") or shutil.which("firefox")
    try:
        from marionette_driver.marionette import Marionette
        from marionette_driver.addons import Addons
        from marionette_driver.keys import Keys
    except ImportError:
        print("SKIP: marionette_driver is not installed (pip install marionette_driver)")
        return 0
    if not firefox:
        print("SKIP: no firefox binary")
        return 0
    if port_in_use(9010):
        print("REFUSED: something already listens on 127.0.0.1:9010 (the real YANDI server?); this test never talks to it")
        return 2

    xpi = Path(os.environ.get("TMPDIR", "/tmp")) / "yandi-e2e.xpi"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py"), "--out", str(xpi)], check=True, capture_output=True)

    stub = Stub()
    servers = []
    for port in (9010, 9011, 9012):
        srv = ThreadingHTTPServer(("127.0.0.1", port), stub.handler())
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)

    os.environ["MOZ_HEADLESS"] = "1"
    m = Marionette(bin=firefox, startup_timeout=120, prefs={
        "extensions.webextensions.uuids": "{}", "app.update.enabled": False, "datareporting.policy.dataSubmissionEnabled": False,
        "browser.shell.checkDefaultBrowser": False, "network.proxy.type": 0, "network.proxy.allow_hijacking_localhost": False,
        "network.dns.localDomains": "claude.ai",
    })
    try:
        m.start_session()
        addon_id = Addons(m).install(str(xpi), temp=True)
        check("the PACKAGE installs as an add-on in a real Firefox (valid manifest, every referenced file present)", addon_id == "council-bridge@yandi.local", str(addon_id))
        time.sleep(3)
        uuids = json.loads(m.get_pref("extensions.webextensions.uuids") or "{}")
        ext = uuids.get(addon_id)
        check("the add-on got an internal address", bool(ext), repr(uuids))

        # ── popup ──
        m.navigate(f"moz-extension://{ext}/popup.html")
        time.sleep(2)
        rows = m.execute_script("return [...document.querySelectorAll('#models-list .model')].map(r => [r.querySelector('.dot').className, r.querySelector('.model-name').textContent])")
        check("popup: shows the council status the server reports (Claude connected, others not)",
              ["dot on", "Claude"] in rows and ["dot off", "GPT"] in rows, repr(rows))
        m.execute_script("document.getElementById('query').value = 'что такое трейт?'; document.getElementById('ask-btn').click();")
        time.sleep(2)
        shown = m.execute_script("return [document.getElementById('answer-text').textContent, document.getElementById('answer-text').querySelector('*') !== null, document.getElementById('trust-badge').textContent.trim()]")
        check("popup: a quick question shows the answer as TEXT (the HTML in it is not interpreted) with its trust badge",
              "<img" in shown[0] and shown[1] is False and shown[2] == "HYPOTHESIS", repr(shown))
        m.execute_script("document.getElementById('query').value = 'нужно уточнение'; document.getElementById('ask-btn').click();")
        time.sleep(2)
        clar = m.execute_script("return [document.getElementById('answer-text').textContent, document.getElementById('trust-badge').textContent.trim()]")
        check("popup: a clarifying question from the server is shown as such (not 'No answer')", "какой именно версии" in clar[0] and clar[1] == "CLARIFICATION", repr(clar))

        # ── overlay: keyboard shortcut on a selection ──
        m.navigate("http://127.0.0.1:9011/page.html")
        time.sleep(1)
        m.execute_script("const r = document.createRange(); r.selectNodeContents(document.getElementById('claim')); const s = getSelection(); s.removeAllRanges(); s.addRange(r);")
        n_before = sum(1 for e in stub.log if e[1] == "/api/orchestrator/ask")
        # The add-on's keyboard shortcut is handled by the browser WINDOW, not by the page: send the key to the window
        m.set_context("chrome")
        m.execute_script("""
            const tip = Cc["@mozilla.org/text-input-processor;1"].createInstance(Ci.nsITextInputProcessor);
            tip.beginInputTransaction(window, () => {});
            const ev = (key, code, keyCode) => new window.KeyboardEvent("", { key, code, keyCode });
            window.focus();
            tip.keydown(ev("Alt", "AltLeft", 18));
            tip.keydown(ev("Shift", "ShiftLeft", 16));
            tip.keydown(ev("Y", "KeyY", 89));
            tip.keyup(ev("Y", "KeyY", 89));
            tip.keyup(ev("Shift", "ShiftLeft", 16));
            tip.keyup(ev("Alt", "AltLeft", 18));
        """)
        m.set_context("content")
        deadline = time.time() + 30
        while time.time() < deadline and sum(1 for e in stub.log if e[1] == "/api/orchestrator/ask") == n_before:
            time.sleep(0.5)
        asks = [e[2] for e in stub.log if e[1] == "/api/orchestrator/ask"][n_before:]
        check("overlay: Alt+Shift+Y sends the SELECTED text (and only it) to the server, through the background page",
              len(asks) == 1 and asks[0].get("query") == "Земля плоская, а вода мокрая." and asks[0].get("enable_web") is True, repr(asks))
        # the overlay reports its state to the background; the popup (in a SECOND tab, the page keeps its overlay) shows the last check
        page_tab = m.current_window_handle
        popup_tab = m.open(type="tab", focus=False)["handle"]
        m.switch_to_window(popup_tab)
        state = ""
        deadline = time.time() + 45
        while time.time() < deadline:
            m.navigate(f"moz-extension://{ext}/popup.html")
            time.sleep(1)
            state = m.execute_script("return document.getElementById('last-text').textContent")
            if "validated" in state:
                break
            time.sleep(3)
        check("overlay: it waited for the server's validation (history record turned preliminary=false) and got VERIFIED",
              "validated" in state and "VERIFIED" in state and "Земля плоская" in state, repr(state))
        m.close()
        m.switch_to_window(page_tab)
        polled = [e[1] for e in stub.log]
        check("overlay: the validation was awaited through /api/orch/history (an endpoint that exists)", "/api/orch/history" in polled and "/api/orch/status/m-1" not in polled)

        # ── the council bridge: a task queued for "claude" reaches a claude.ai page and its answer comes back ──
        claude_tab = m.open(type="tab", focus=False)["handle"]
        m.switch_to_window(claude_tab)
        m.navigate("http://claude.ai:9012/claude.html")
        deadline = time.time() + 40
        while time.time() < deadline and not any(e[1] == "/api/ext/result" for e in stub.log):
            time.sleep(0.5)
        results = [e[2] for e in stub.log if e[1] == "/api/ext/result"]
        typed = ""
        try:
            typed = m.execute_script("return [...document.querySelectorAll('[data-testid=assistant-message]')].map(x => x.textContent).join('|')")
        except Exception:
            pass
        check("council: a task the server queued for claude was typed into the claude.ai page, sent, and the page's reply was posted back",
              len(results) == 1 and results[0].get("task_id") == "council-task-1" and results[0].get("from") == "claude"
              and results[0].get("text") == "ЭХО: Привет, совет: сколько будет два плюс два?", repr((results, typed)))
        check("council: the task is delivered once (no repeat while the model is busy)", len(results) == 1)
        m.close()
        m.switch_to_window(page_tab)

        # ── a hostile page cannot run or read anything of the overlay ──
        pwned, leak = m.execute_script("return [window.wrappedJSObject.__pwned === undefined, window.wrappedJSObject.__leak]")
        check("security: HTML in the answer ran nothing in the page", pwned is True)
        check("security: the page could not read the overlay (closed shadow root)", leak in ("closed", "none"), repr(leak))
    finally:
        if FAILURES:
            print("[diagnostic] requests the stub server saw:", [(e[0], e[1]) for e in stub.log][:40])
        try:
            m.cleanup()
        except Exception:
            pass
        for srv in servers:
            srv.shutdown()

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
