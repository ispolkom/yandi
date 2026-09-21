"""
pet/web_guard_firefox_e2e.py — ЗАКРЫТ ЛИ РЕАЛЬНЫЙ СЕРВЕР ДЛЯ ЧУЖИХ САЙТОВ, И РАБОТАЕТ ЛИ ПРИ ЭТОМ РАСШИРЕНИЕ.

    python -m pet.web_guard_firefox_e2e

Поднимает РЕАЛЬНЫЙ сервер (127.0.0.1:9010) с собственным временным Redis (порт 6379, без записи на диск), ставит в настоящий
headless Firefox ПАКЕТ расширения и открывает «чужую» страницу на соседнем порту (127.0.0.1:9011: другой источник). Проверяет:

  * своя веб-морда открывается и держит WebSocket; ответы несут защитные заголовки;
  * расширение по-прежнему работает с настоящим сервером (окно показывает статус чатов от него);
  * расширению закрыты все адреса, кроме его собственных (выполнить команду через него нельзя);
  * чужая страница не читает историю личного чата, не читает и не меняет настройки, не запускает инструменты агента,
    не подключается к WebSocket и не может ничего записать даже «простым» запросом без предзапроса (проверяется по Redis).

Нужны firefox-esr/firefox, redis-server и `pip install marionette_driver`; без них — SKIP. Если 9010, 9011 или 6379 заняты
(настоящий сервер / Redis владельца), тест отказывается запускаться и ничего не трогает.
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
MARKER = "EVIL-MARKER-must-never-be-stored"


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def main() -> int:
    firefox = shutil.which("firefox-esr") or shutil.which("firefox")
    redis = shutil.which("redis-server")
    redis_cli = shutil.which("redis-cli")
    try:
        from marionette_driver.marionette import Marionette
        from marionette_driver.addons import Addons
    except ImportError:
        print("SKIP: marionette_driver is not installed (pip install marionette_driver)")
        return 0
    if not (firefox and redis and redis_cli):
        print("SKIP: no firefox binary, redis-server or redis-cli")
        return 0
    for port in (9010, 9011, 6379):
        if port_in_use(port):
            print(f"REFUSED: something already listens on 127.0.0.1:{port} (a real server?); this test never touches it")
            return 2

    import importlib.util
    has_ws = bool(importlib.util.find_spec("websockets") or importlib.util.find_spec("wsproto"))
    if not has_ws:
        print("NOTE: no WebSocket library (pip install websockets): the WebSocket checks are SKIPPED, the server cannot serve WebSocket at all")

    tmp = Path(tempfile.mkdtemp(prefix="yandi-guard-e2e-"))
    xpi = tmp / "yandi.xpi"
    subprocess.run([sys.executable, str(ROOT / "scripts" / "build_extension.py"), "--out", str(xpi)], check=True, capture_output=True)
    procs: list[subprocess.Popen] = []
    evil_srv = None
    m = None
    try:
        (tmp / "redis").mkdir()
        procs.append(subprocess.Popen([redis, "--port", "6379", "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", str(tmp / "redis")],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        env = {**os.environ, "YANDI_WEB_SETTINGS": str(tmp / "web_settings.json"), "YANDI_TEST_MODE": "1"}
        procs.append(subprocess.Popen([sys.executable, "-m", "pet.council_chat_server", "--port", "9010"], cwd=str(ROOT), env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        deadline = time.time() + 90
        while time.time() < deadline and not port_in_use(9010):
            time.sleep(0.5)
        if not port_in_use(9010):
            print("FAIL: the server did not start")
            return 1
        time.sleep(1)

        page = b"<!doctype html><html><body>another website</body></html>"

        class Evil(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)
        evil_srv = ThreadingHTTPServer(("127.0.0.1", 9011), Evil)
        threading.Thread(target=evil_srv.serve_forever, daemon=True).start()

        os.environ["MOZ_HEADLESS"] = "1"
        m = Marionette(bin=firefox, startup_timeout=120, prefs={"network.proxy.type": 0, "app.update.enabled": False, "browser.shell.checkDefaultBrowser": False,
                                                                "extensions.webextensions.uuids": "{}"})
        m.start_session()
        addon_id = Addons(m).install(str(xpi), temp=True)
        time.sleep(3)
        ext = json.loads(m.get_pref("extensions.webextensions.uuids") or "{}").get(addon_id)

        # ── 1. своя веб-морда ──
        m.navigate("http://127.0.0.1:9010/")
        time.sleep(4)
        own = m.execute_async_script("""
          const done = arguments[arguments.length - 1];
          fetch('/').then(r => done({status: r.status, xfo: r.headers.get('x-frame-options'), csp: r.headers.get('content-security-policy'),
                                     nosniff: r.headers.get('x-content-type-options'), acao: r.headers.get('access-control-allow-origin')}))
                    .catch(e => done({error: String(e)}));
        """)
        check("own page: the web UI loads for its own user", own.get("status") == 200, repr(own))
        check("own page: answers carry the anti-framing headers and NO wildcard CORS", own.get("xfo") == "DENY" and "frame-ancestors 'none'" in (own.get("csp") or "")
              and own.get("nosniff") == "nosniff" and own.get("acao") is None, repr(own))
        ws_dot = m.execute_script("return document.getElementById('st-redis').className")
        if has_ws:
            check("own page: its WebSocket connects (the guard lets the own page in)", "dot-on" in ws_dot, ws_dot)

        # ── 2. расширение ──
        m.navigate(f"moz-extension://{ext}/popup.html")
        time.sleep(3)
        rows = m.execute_script("return [...document.querySelectorAll('#models-list .model-name')].map(e => e.textContent)")
        check("extension: the popup shows the chat status from the REAL server (guard + CORS let the extension in on its own paths)",
              rows == ["Claude", "GPT", "DeepSeek", "Kimi"], repr(rows))
        blocked = m.execute_async_script("""
          const done = arguments[arguments.length - 1];
          const out = {};
          const t = (name, p) => p.then(async r => { out[name] = [r.status, (await r.text()).slice(0, 60)]; }).catch(e => { out[name] = ['blocked', e.name]; });
          Promise.all([
            t('tools_run', fetch('http://127.0.0.1:9010/api/tools/run', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{"tool":"system.os","args":{}}'})),
            t('local_history', fetch('http://127.0.0.1:9010/api/local/history')),
            t('config', fetch('http://127.0.0.1:9010/api/council/config')),
          ]).then(() => done(out));
        """)
        check("extension: outside its own paths it gets nothing (no tools, no personal history, no config)",
              all(v[0] in (403, "blocked") for v in blocked.values()) and len(blocked) == 3, repr(blocked))

        # ── 3. чужая страница ──
        m.navigate("http://127.0.0.1:9011/")
        time.sleep(1)
        out = m.execute_async_script("""
          const done = arguments[arguments.length - 1];
          const base = 'http://127.0.0.1:9010';
          const out = {};
          const t = (name, p) => p.then(r => { out[name] = r.status; }).catch(e => { out[name] = 'blocked'; });
          const jsonPost = (path, body) => fetch(base + path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
          // «простые» запросы: без предзапроса, браузер отправляет их сразу (записывать ответ не нужно — важен побочный эффект)
          const simple = (path, body) => fetch(base + path, {method: 'POST', mode: 'no-cors', headers: {'Content-Type': 'text/plain'}, body});
          const ws = new Promise(res => {
            const s = new WebSocket('ws://127.0.0.1:9010/ws/evil');
            let opened = false;
            s.onopen = () => { opened = true; s.close(); };
            s.onclose = () => res(opened ? 'CONNECTED' : 'refused');
            s.onerror = () => {};
            setTimeout(() => res(opened ? 'CONNECTED' : 'refused'), 4000);
          });
          Promise.all([
            t('local_history', fetch(base + '/api/local/history')),
            t('orch_history', fetch(base + '/api/orch/history')),
            t('config_read', fetch(base + '/api/council/config')),
            t('config_write', jsonPost('/api/council/config', {proxy: 'evil:1:x:y'})),
            t('tools_run', jsonPost('/api/tools/run', {tool: 'system.os', args: {}})),
            t('settings', fetch(base + '/api/ui/settings')),
            t('browse', fetch(base + '/api/models/browse?path=/')),
            t('state', jsonPost('/api/council/state', {paused: true})),
            t('simple_write', simple('/api/local/message', JSON.stringify({role: 'user', content: '%s'}))),
            t('simple_pause', simple('/api/council/pause', '{}')),
            ws.then(v => { out.websocket = v; }),
          ]).then(() => done(out));
        """ % MARKER)
        # «простые» запросы no-cors всегда дают непрозрачный ответ (статус 0): их проверяет побочный эффект ниже, а не статус
        http_ok = {k: v for k, v in out.items() if k not in ("websocket", "simple_write", "simple_pause") and v not in (403, "blocked")}
        check("another website: cannot read the personal chat history, the settings or the disk, nor change the config, the state or run agent tools",
              not http_ok and len(out) == 11, repr(out))
        if has_ws:
            check("another website: cannot connect to the WebSocket", out.get("websocket") == "refused", repr(out.get("websocket")))
        time.sleep(1)
        stored = subprocess.run([redis_cli, "-p", "6379", "lrange", "council:local:messages", "0", "-1"], capture_output=True, text=True).stdout
        check("another website: even a 'simple' request written straight to the server stored nothing (checked in Redis)", MARKER not in stored, stored[:200])
        paused = subprocess.run([sys.executable, "-c",
                                 "import json,urllib.request;print(list(json.load(urllib.request.urlopen('http://127.0.0.1:9010/api/council/tokens'))))"],
                                capture_output=True, text=True)
        import urllib.request
        state = json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:9010/api/council/state", data=b"{}",
                                                                        headers={"Content-Type": "application/json"})))
        check("another website: a 'simple' pause request did not pause the bridge", state.get("paused") is False, repr(state))
        check("the server is still healthy for local programs after all that (no Origin header = allowed)", paused.returncode == 0 and "tokens" in paused.stdout, paused.stdout + paused.stderr[:200])

        m.navigate("http://127.0.0.1:9010/")
        time.sleep(2)
        back = m.execute_script("return [document.getElementById('st-redis').className.includes('dot-on'), !!document.getElementById('tab-settings')]")
        check("own page: the web UI still works for its own user after the attack", back[1] and (back[0] or not has_ws), repr(back))
    finally:
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
