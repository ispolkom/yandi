"""
pet/settings_tab_firefox_e2e.py — вкладка «⚙ YANDI» (настройки) в НАСТОЯЩЕЙ веб-морде и настоящем Firefox.

    python -m pet.settings_tab_firefox_e2e

Поднимает РЕАЛЬНЫЙ сервер (pet.council_chat_server на 127.0.0.1:9010) с собственным временным Redis (порт 6379, без
записи на диск), файлом настроек во временной папке и «вторым диском» из фикстур (папки и файлы .gguf, в том числе
с именем-ловушкой). Firefox (headless) ведётся через Marionette:

  * первый запуск: вкладка настроек первая и открыта сама, чата в ней нет;
  * «Обзор»: окно открывается, можно перейти в любую папку, выбрать файл, имя-ловушка показано текстом и ничего не запускает;
  * «Проверить», ошибка без Голоса, «Применить»: файл настроек записан, ключа в нём нет;
  * после сохранения вкладка уходит в конец списка и больше не открывается сама; после перезагрузки всё на месте;
  * страница ДРУГОГО сайта (соседний порт) не может ни прочитать диск, ни изменить настройки.

Нужны firefox-esr/firefox, redis-server и `pip install marionette_driver`; без них — SKIP. Если 9010 или 6379 уже заняты
(настоящий сервер / настоящий Redis владельца), тест отказывается запускаться и ничего не трогает.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


TRAP = '<img src=x onerror="window.__pwned=1">.gguf'
EVIL_PAGE = b"<!doctype html><html><body>evil</body></html>"


def main() -> int:
    firefox = shutil.which("firefox-esr") or shutil.which("firefox")
    redis = shutil.which("redis-server")
    try:
        from marionette_driver.marionette import Marionette
        from marionette_driver.by import By
        from marionette_driver.keys import Keys
    except ImportError:
        print("SKIP: marionette_driver is not installed (pip install marionette_driver)")
        return 0
    if not (firefox and redis):
        print("SKIP: no firefox binary or redis-server")
        return 0
    for port in (9010, 6379, 9011):
        if port_in_use(port):
            print(f"REFUSED: something already listens on 127.0.0.1:{port} (a real server?); this test never touches it")
            return 2

    tmp = Path(tempfile.mkdtemp(prefix="yandi-settings-e2e-"))
    settings_file = tmp / "cfg" / "web_settings.json"
    disk = tmp / "disk2" / "models"
    (disk / "big").mkdir(parents=True)
    (disk / "good.gguf").write_bytes(b"GGUF" + b"\0" * 2048)
    (disk / TRAP).write_bytes(b"GGUF" + b"\0" * 16)
    (disk / "fake.gguf").write_bytes(b"NOPE" + b"\0" * 16)

    procs: list[subprocess.Popen] = []
    evil_srv = None
    m = None
    try:
        (tmp / "redis").mkdir()
        procs.append(subprocess.Popen([redis, "--port", "6379", "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(tmp / "redis")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        env = {**os.environ, "YANDI_WEB_SETTINGS": str(settings_file), "YANDI_TEST_MODE": "1"}
        procs.append(subprocess.Popen([sys.executable, "-m", "pet.council_chat_server", "--port", "9010"], cwd=str(ROOT), env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.time() + 90
        while time.time() < deadline and not port_in_use(9010):
            time.sleep(0.5)
        if not port_in_use(9010):
            print("FAIL: the server did not start")
            return 1
        time.sleep(1)

        class Evil(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(EVIL_PAGE)))
                self.end_headers()
                self.wfile.write(EVIL_PAGE)
        evil_srv = ThreadingHTTPServer(("127.0.0.1", 9011), Evil)
        threading.Thread(target=evil_srv.serve_forever, daemon=True).start()

        os.environ["MOZ_HEADLESS"] = "1"
        m = Marionette(bin=firefox, startup_timeout=120, prefs={"network.proxy.type": 0, "app.update.enabled": False,
                                                                "browser.shell.checkDefaultBrowser": False})
        m.start_session()
        m.navigate("http://127.0.0.1:9010/")
        time.sleep(3)
        js = m.execute_script

        # ── 1. первый запуск ──
        state = js("""
          const tab = document.getElementById('tab-settings');
          const others = [...document.querySelectorAll('.nav-btn')].filter(b => b !== tab);
          const vis = id => { const e = document.getElementById(id); return e && getComputedStyle(e).display !== 'none'; };
          return { active: tab.classList.contains('active'), leftmost: tab.getBoundingClientRect().left < Math.min(...others.map(b => b.getBoundingClientRect().left)),
                   panel: vis('msgs-settings'), input: vis('input-area'), right: vis('tools-orch') || vis('tools-inet'), current: document.getElementById('st-current').textContent };
        """)
        check("first run: the settings tab is the FIRST tab and opens by itself", state["active"] and state["leftmost"], repr(state))
        check("first run: there is no chat in it (no input line, no tools panel)", state["panel"] and not state["input"] and not state["right"], repr(state))
        check("first run: it says nothing is chosen yet", "ещё не сохранены" in state["current"], state["current"])

        # ── 2. Обзор ──
        m.find_element(By.ID, "st-browse").click()
        time.sleep(1.5)
        opened = js("return !!document.querySelector('.br-box') && document.getElementById('br-path').value")
        check("browse: the window opens (on the home folder)", bool(opened), repr(opened))
        path_in = m.find_element(By.ID, "br-path")
        path_in.clear()
        path_in.send_keys(str(disk))
        path_in.send_keys(Keys.ENTER)
        time.sleep(1.5)
        rows = js("return [...document.querySelectorAll('#br-list .br-item')].map(r => [r.className, r.textContent])")
        names = [r[1] for r in rows]
        check("browse: a folder on ANOTHER disk opens; folders first, then .gguf files", any("big" in n for n in names) and rows[0][1].endswith("big")
              and any("good.gguf" in n for n in names) and any("fake.gguf" in n for n in names), repr(rows))
        trap_shown = js("return [document.querySelectorAll('#br-list img').length, window.wrappedJSObject.__pwned === undefined]")
        check("browse: a file named like HTML is shown as plain text and runs nothing", trap_shown == [0, True] and any("<img" in n for n in names), repr((trap_shown, names)))
        m.find_element(By.XPATH, "//*[@id='br-list']//span[text()='good.gguf']").click()
        time.sleep(1)
        picked = js("return [document.getElementById('st-local-path').value, !document.querySelector('.br-box')]")
        check("browse: choosing a file fills the path field and closes the window", picked == [str(disk / "good.gguf"), True], repr(picked))

        # ── 3. Проверить и Применить ──
        m.find_element(By.ID, "st-local-check").click()
        time.sleep(1.5)
        res = js("return document.getElementById('st-local-result').textContent")
        check("check: a real GGUF file is confirmed with name and size", "✅" in res and "good.gguf" in res, res)
        js("document.getElementById('st-local-path').value = arguments[0]", [str(disk / "fake.gguf")])
        m.find_element(By.ID, "st-local-check").click()
        time.sleep(1.5)
        res = js("return document.getElementById('st-local-result').textContent")
        check("check: a file that is not a model is refused with the reason", "❌" in res and "GGUF" in res, res)
        js("document.getElementById('st-local-path').value = arguments[0]", [str(disk / "good.gguf")])

        m.find_element(By.ID, "st-apply").click()
        time.sleep(1.5)
        status = js("return document.getElementById('st-status').textContent")
        check("apply: without a Voice it refuses and says why (nothing is saved)", "Голос" in status and not settings_file.exists(), status)

        js("document.getElementById('st-voice-local').click(); document.getElementById('st-adv-local').click();")
        SECRET = "sk-test-SECRET-1234567890"
        key = m.find_element(By.ID, "st-api-key")
        key.send_keys(SECRET)
        m.find_element(By.ID, "st-apply").click()
        time.sleep(2)
        status = js("return document.getElementById('st-status').textContent")
        check("apply: saved, and the page says the tab moves to the end", "✅" in status and "перенесена" in status, status)
        saved = json.loads(settings_file.read_text(encoding="utf-8")) if settings_file.exists() else {}
        check("apply: the file has the choice (Voice local, this file, advisor local)",
              saved.get("saved") is True and saved.get("voice") == "local" and saved.get("local", {}).get("path") == str(disk / "good.gguf")
              and saved.get("advisors") == ["local"], repr(saved))
        check("apply: the API key is NOT in the file and not left in the field", SECRET not in settings_file.read_text(encoding="utf-8")
              and js("return document.getElementById('st-api-key').value") == "")
        moved = js("""
          const tab = document.getElementById('tab-settings');
          const others = [...document.querySelectorAll('.nav-btn')].filter(b => b !== tab);
          return { rightmost: tab.getBoundingClientRect().left > Math.max(...others.map(b => b.getBoundingClientRect().left)),
                   current: document.getElementById('st-current').textContent };
        """)
        check("apply: the tab moved to the END of the list", moved["rightmost"], repr(moved))
        check("apply: 'currently used' shows the Voice and the file", "Голос" in moved["current"] and "good.gguf" in moved["current"], moved["current"])

        # ── 4. после перезагрузки ──
        m.navigate("http://127.0.0.1:9010/")
        time.sleep(3)
        after = js("""
          const tab = document.getElementById('tab-settings');
          const others = [...document.querySelectorAll('.nav-btn')].filter(b => b !== tab);
          return { active: tab.classList.contains('active'), rightmost: tab.getBoundingClientRect().left > Math.max(...others.map(b => b.getBoundingClientRect().left)) };
        """)
        check("reload: settings are saved, so the tab stays LAST and no longer opens by itself", after["rightmost"] and not after["active"], repr(after))
        m.find_element(By.ID, "tab-settings").click()
        time.sleep(1.5)
        restored = js("return [document.getElementById('st-voice-local').checked, document.getElementById('st-adv-local').checked, document.getElementById('st-local-path').value]")
        check("reload: opening the tab shows the saved choice", restored == [True, True, str(disk / "good.gguf")], repr(restored))
        js("document.getElementById('tab-orch').click()")
        time.sleep(1)
        back = js("return [getComputedStyle(document.getElementById('input-area')).display !== 'none', document.body.classList.contains('mode-settings')]")
        check("other tabs are untouched: leaving the settings brings the chat input back", back == [True, False], repr(back))

        # ── 5. чужая страница ──
        m.navigate("http://127.0.0.1:9011/")
        time.sleep(1)
        outcome = m.execute_async_script("""
          const done = arguments[arguments.length - 1];
          const out = {};
          const t = (name, p) => p.then(r => { out[name] = r.status; }).catch(e => { out[name] = 'blocked:' + e.name; });
          Promise.all([
            t('browse', fetch('http://127.0.0.1:9010/api/models/browse?path=/')),
            t('settings_get', fetch('http://127.0.0.1:9010/api/ui/settings')),
            t('settings_post', fetch('http://127.0.0.1:9010/api/ui/settings', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'})),
            t('check', fetch('http://127.0.0.1:9010/api/models/check', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"path":"/etc/hostname"}'})),
          ]).then(() => done(out));
        """)
        blocked = all(v == 403 or str(v).startswith("blocked") for v in outcome.values())
        check("another website's page cannot list the disk or read/change the settings (403 or blocked)", blocked and len(outcome) == 4, repr(outcome))
        still = json.loads(settings_file.read_text(encoding="utf-8"))
        check("... and the saved settings are unchanged", still == saved, repr(still))
    finally:
        if FAILURES and "m" in dir():
            pass
        if m is not None:
            try:
                m.cleanup()
            except Exception:
                pass
        if evil_srv:
            evil_srv.shutdown()
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        shutil.rmtree(tmp, ignore_errors=True)

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
