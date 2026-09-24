"""
pet/pet_web_login_regression_test.py — the assistant's web page (port 9010) asks for the SAME password as the node's web page.

    ONE ACCOUNT (~/.yandi_keys/auth.json), TWO DOORS.  A BROWSER WITHOUT A SESSION GETS NOTHING.  A FAILED CHECK NEVER OPENS THE DOOR.
    THE PASSWORD IS CHECKED BY THE SAME RUST CODE THE NODE USES, AND TRAVELS ONLY ON STANDARD INPUT.  THE FORMS ARE THE NODE'S OWN FILES.

Most cases run against a small stand-in for the key tool (so that every failure mode can be produced); a few run against the REAL
`yandi-keys` binary (skipped, said so, when it is not built): setting the passwords in one door and entering with them, the reset by the
master password, and that the node's own check agrees. Then each safety net is removed on purpose (mutants) and the checks must notice.

Run: python -m pet.pet_web_login_regression_test
"""
from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("YANDI_TEST_MODE", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from fastapi import FastAPI, WebSocket  # noqa: E402

from pet import web_login as wl  # noqa: E402

BROWSER = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}
NAVIGATE = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "accept": "text/html"}
LOGIN, MASTER, NEW_LOGIN = "login password 1", "master phrase of several words", "brand new login pw"
REAL_TOOL = ROOT / "node" / "target" / "debug" / "yandi-keys"

FAKE_TOOL = '''#!{python}
import os, sys
args = sys.argv[1:]
cmd, state = args[0], args[args.index("--dir") + 1]
lines = sys.stdin.read().split("\\n")
with open(os.path.join(state, "calls.log"), "a") as log:
    log.write(repr((args, lines)) + "\\n")
def read(name):
    try:
        return open(os.path.join(state, name)).read()
    except OSError:
        return None
def write(name, text):
    with open(os.path.join(state, name), "w") as f:
        f.write(text)
mode = read("mode")
if mode == "crash":
    sys.exit(139)
if mode == "weird":
    sys.exit(3)
if cmd == "login-check":
    current = read("login")
    if current is None:
        print("yandi-keys: Вход ещё не настроен", file=sys.stderr); sys.exit(2)
    sys.exit(0 if lines[0] == current else 1)
if cmd == "login-reset":
    if len(lines[1]) < 8:
        print("yandi-keys: Новый пароль входа должен быть не короче 8 символов", file=sys.stderr); sys.exit(1)
    if lines[0] != read("master"):
        print("yandi-keys: Код восстановления не подошёл", file=sys.stderr); sys.exit(1)
    write("login", lines[1]); sys.exit(0)
if cmd == "setup":
    if os.path.exists(os.path.join(state, "auth.json")):
        print("yandi-keys: Auth is already set up; refusing to overwrite the existing key file", file=sys.stderr); sys.exit(1)
    if lines[0] != lines[1]:
        print("yandi-keys: Пароли входа не совпадают", file=sys.stderr); sys.exit(1)
    write("login", lines[0]); write("master", lines[2]); write("auth.json", "{{}}"); sys.exit(0)
sys.exit(2)
'''


class Clock:
    now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def run_checks() -> list:
    failures: list = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    tmp = Path(tempfile.mkdtemp(prefix="yandi-weblogin-"))
    clock = Clock()

    def fresh(account: bool = True, tool: str = "fake"):
        """A new key directory (with an account, or without), the tool to use, and clean login state."""
        state = tmp / f"keys{len(list(tmp.iterdir()))}"
        state.mkdir()
        script = tmp / "fake-yandi-keys"
        script.write_text(FAKE_TOOL.format(python=sys.executable))
        script.chmod(stat.S_IRWXU)
        if account:
            (state / "auth.json").write_text("{}")
            (state / "login").write_text(LOGIN)
            (state / "master").write_text(MASTER)
        os.environ["YANDI_KEYS_DIR"] = str(state)
        os.environ["YANDI_KEYS_TOOL"] = str(script if tool == "fake" else tool)
        wl.reset_state()
        clock.now = 1_000_000.0
        wl.sessions._clock = clock
        wl.throttle._clock = clock
        return state

    def make_app() -> FastAPI:
        app = FastAPI()
        app.add_middleware(wl.WebLoginMiddleware)
        app.include_router(wl.router)

        @app.get("/")
        async def index():
            return {"page": "assistant"}

        @app.get("/api/secret")
        async def secret():
            return {"secret": "history"}

        @app.get("/api/council/connections")
        async def connections():
            return {"ext": True}

        @app.websocket("/ws/{client}")
        async def ws(websocket: WebSocket, client: str):
            await websocket.accept()
            await websocket.send_text("hello")
            await websocket.close()
        return app

    def client(app=None):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app or make_app()), base_url="http://127.0.0.1:9010", follow_redirects=False)

    async def ws_result(app, headers: dict) -> str:
        """'closed' when the handshake is refused, 'accepted' when it goes through."""
        sent: list = []
        queue = [{"type": "websocket.connect"}]

        async def receive():
            return queue.pop(0) if queue else {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message["type"])
        scope = {"type": "websocket", "path": "/ws/x", "raw_path": b"/ws/x", "query_string": b"", "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
                 "subprotocols": [], "server": ("127.0.0.1", 9010), "client": ("127.0.0.1", 5), "scheme": "ws", "root_path": "", "asgi": {"version": "3.0"}}
        await app(scope, receive, send)
        return "accepted" if "websocket.accept" in sent else "closed"

    async def scenario() -> None:
        # ── the gate ──
        fresh()
        async with client() as c:
            r = await c.get("/", headers=NAVIGATE)
            check("G1 a browser without a session is sent to the login page", r.status_code == 302 and r.headers["location"] == "/login")
            r = await c.get("/api/secret", headers=BROWSER)
            check("G2 a browser API call without a session is refused (401) and shows nothing", r.status_code == 401 and "history" not in r.text)
            r = await c.get("/api/secret")
            check("G3 a local program (no browser header) is not affected", r.status_code == 200 and r.json() == {"secret": "history"})
            r = await c.get("/api/secret", headers={**BROWSER, "cookie": f"{wl.COOKIE}=not-a-session"})
            check("G4 a made-up session cookie is refused", r.status_code == 401)
            r = await c.get("/api/secret", headers={**BROWSER, "cookie": "yandi_session=whatever"})
            check("G5 the NODE's cookie name does not open this door (they never share a session)", r.status_code == 401 and wl.COOKIE != "yandi_session")
            for path in ("/login", "/api/auth/status"):
                r = await c.get(path, headers=BROWSER)
                check(f"G6 {path} is open without a session", r.status_code == 200)
            r = await c.get("/api/council/connections", headers={"origin": "moz-extension://12345678-1234-1234-1234-123456789abc", "sec-fetch-site": "cross-site"})
            check("G7 the Firefox extension keeps its own paths without a session", r.status_code == 200)
            r = await c.get("/api/secret", headers={"origin": "moz-extension://12345678-1234-1234-1234-123456789abc", "sec-fetch-site": "cross-site"})
            check("G8 …but not the rest of the server", r.status_code == 401)
        check("G9 a browser WebSocket without a session is closed before it is accepted", await ws_result(make_app(), BROWSER) == "closed")
        check("G10 a local program's WebSocket is not affected", await ws_result(make_app(), {}) == "accepted")

        fresh(account=False)
        async with client() as c:
            r = await c.get("/", headers=NAVIGATE)
            check("G11 no passwords yet: the browser is sent to the setup page", r.status_code == 302 and r.headers["location"] == "/setup")
            r = await c.get("/login", headers=NAVIGATE)
            check("G12 …the login page also sends it there", r.status_code == 302 and r.headers["location"] == "/setup")
            check("G13 …and a local program still passes", (await c.get("/api/secret")).status_code == 200)

        # ── login, session, logout ──
        state = fresh()
        async with client() as c:
            r = await c.post("/api/auth/login", json={"login_password": "wrong"})
            check("L1 a wrong password: 401, no cookie", r.status_code == 401 and "set-cookie" not in r.headers and r.json()["error"])
            r = await c.post("/api/auth/login", json={"login_password": LOGIN})
            cookie = r.headers.get("set-cookie", "")
            check("L2 the right password: 200 and a session cookie", r.status_code == 200 and cookie.startswith(wl.COOKIE + "="))
            check("L3 …HttpOnly, SameSite=Strict, whole site, 12 hours", "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/" in cookie and f"Max-Age={wl.SESSION_SECS}" in cookie)
            token = cookie.split(";")[0]
            r = await c.get("/api/secret", headers={**BROWSER, "cookie": token})
            check("L4 with the session the browser gets in", r.status_code == 200)
            r = await c.get("/", headers={**NAVIGATE, "cookie": token})
            check("L5 …and the page", r.status_code == 200 and r.json() == {"page": "assistant"})
            r = await c.post("/api/auth/logout", headers={**BROWSER, "cookie": token})
            check("L6 logout clears the cookie", r.status_code == 200 and "Max-Age=0" in r.headers.get("set-cookie", ""))
            check("L7 …and the session no longer works", (await c.get("/api/secret", headers={**BROWSER, "cookie": token})).status_code == 401)
            r = await c.post("/api/auth/login", json={"login_password": LOGIN, "remember_me": True})
            check("L8 'remember me' lasts 30 days", f"Max-Age={wl.REMEMBER_SECS}" in r.headers.get("set-cookie", ""))
            token = r.headers["set-cookie"].split(";")[0]
            clock.now += wl.REMEMBER_SECS + 1
            check("L9 a session expires", (await c.get("/api/secret", headers={**BROWSER, "cookie": token})).status_code == 401)
            r = await c.post("/api/auth/login", json={"login_password": "a\nb"})
            check("L10 a password with a line break is refused before the tool is asked", r.status_code == 400)
            r = await c.post("/api/auth/login", json={})
            check("L11 an empty request is refused", r.status_code == 400)
            log = (state / "calls.log").read_text() if (state / "calls.log").exists() else ""
            check("L12 the password is given to the tool on standard input, never on its command line", LOGIN in log and f"'{LOGIN}'" not in log.split("], [")[0])

        # ── brute force ──
        fresh()
        async with client() as c:
            for _ in range(wl.FREE_ATTEMPTS):
                r = await c.post("/api/auth/login", json={"login_password": "wrong"})
            check("B1 the first few mistakes are not slowed", r.status_code == 401)
            r = await c.post("/api/auth/login", json={"login_password": "wrong"})
            r = await c.post("/api/auth/login", json={"login_password": LOGIN})
            check("B2 after several mistakes even the RIGHT password waits", r.status_code == 429 and "Подождите" in r.json()["error"])
            clock.now += 3
            r = await c.post("/api/auth/login", json={"login_password": LOGIN})
            check("B3 after the wait it opens, and the slow-down is cleared", r.status_code == 200 and wl.throttle.failures == 0)
            for _ in range(60):
                wl.throttle.failed()
            check("B4 the wait never exceeds five minutes", wl.throttle.backoff_after(wl.throttle.failures) == wl.BACKOFF_MAX)

        # ── recovery by the master password ──
        fresh()
        async with client() as c:
            r = await c.post("/api/auth/login", json={"login_password": LOGIN})
            old = r.headers["set-cookie"].split(";")[0]
            r = await c.post("/api/auth/recover", json={"recovery_code": "not the master", "new_login_password": NEW_LOGIN})
            check("R1 a wrong recovery secret: 401 with the tool's short message, nothing changed", r.status_code == 401 and "не подошёл" in r.json()["error"]
                  and (await c.post("/api/auth/login", json={"login_password": LOGIN})).status_code == 200)
            r = await c.post("/api/auth/recover", json={"recovery_code": MASTER, "new_login_password": "short"})
            check("R2 a short new password is refused with a reason", r.status_code == 401 and "8" in r.json()["error"])
            wl.throttle.failures = 0
            r = await c.post("/api/auth/recover", json={"recovery_code": MASTER, "new_login_password": NEW_LOGIN})
            check("R3 the master password resets the login password and opens a session", r.status_code == 200 and r.headers.get("set-cookie", "").startswith(wl.COOKIE))
            check("R4 the old login password no longer works, the new one does", (await c.post("/api/auth/login", json={"login_password": LOGIN})).status_code == 401
                  and (await c.post("/api/auth/login", json={"login_password": NEW_LOGIN})).status_code == 200)
            check("R5 every session that existed before the reset is ended", (await c.get("/api/secret", headers={**BROWSER, "cookie": old})).status_code == 401)

        # ── first setup in this door ──
        state = fresh(account=False)
        async with client() as c:
            page = await c.get("/setup", headers=NAVIGATE)
            check("S1 no passwords yet: the setup page is served", page.status_code == 200 and "master_pw2" in page.text)
            r = await c.post("/api/auth/setup", json={"login_password": LOGIN, "login_password_repeat": "different", "master_password": MASTER, "master_password_repeat": MASTER})
            check("S2 what the person typed is checked (mismatch refused with a reason, nothing created)", r.status_code == 400 and not (state / "auth.json").exists() and r.json()["error"])
            r = await c.post("/api/auth/setup", json={"login_password": LOGIN, "login_password_repeat": LOGIN, "master_password": MASTER, "master_password_repeat": MASTER})
            check("S3 the passwords are created", r.status_code == 200 and (state / "auth.json").exists())
            check("S4 …and now the SAME password logs in here", (await c.post("/api/auth/login", json={"login_password": LOGIN})).status_code == 200)
            before = (state / "master").read_text()
            r = await c.post("/api/auth/setup", json={"login_password": "other login pw", "login_password_repeat": "other login pw", "master_password": "other master phrase", "master_password_repeat": "other master phrase"})
            check("S5 a second setup is refused and changes nothing", r.status_code == 409 and (state / "master").read_text() == before)
            r = await c.get("/setup", headers=NAVIGATE)
            check("S6 …the setup page then sends to the login page", r.status_code == 302 and r.headers["location"] == "/login")

        # ── the tool fails: never open ──
        fresh(tool=str(tmp / "no-such-tool"))
        async with client() as c:
            r = await c.post("/api/auth/login", json={"login_password": LOGIN})
            check("F1 no key tool: login is unavailable (503) with what to build, never 'ok'", r.status_code == 503 and "cargo build" in r.json()["error"] and "set-cookie" not in r.headers)
            check("F2 …the gate stays CLOSED for browsers", (await c.get("/api/secret", headers=BROWSER)).status_code == 401)
            check("F3 …and local programs are unaffected", (await c.get("/api/secret")).status_code == 200)
        state = fresh()
        for mode in ("crash", "weird"):
            (state / "mode").write_text(mode)
            async with client() as c:
                r = await c.post("/api/auth/login", json={"login_password": LOGIN})
                check(f"F4 a key tool that fails in a way it should not ({mode}) is 503, never a session", r.status_code == 503 and "set-cookie" not in r.headers)
                r = await c.post("/api/auth/recover", json={"recovery_code": MASTER, "new_login_password": NEW_LOGIN})
                check(f"F5 …the same for recovery ({mode})", r.status_code == 503 and "set-cookie" not in r.headers)

        # ── the forms are the node's own ──
        node_login = (ROOT / "node" / "src" / "web" / "ui" / "login.html").read_text(encoding="utf-8")
        node_setup = (ROOT / "node" / "src" / "web" / "ui" / "setup.html").read_text(encoding="utf-8")

        def style(text: str) -> str:
            return text[text.index("<style>"):text.index("</style>")]
        check("P1 the login page has the node's style, byte for byte", style(wl.PAGES["login.html"]) == style(node_login))
        check("P2 …and the node's script (same fields, same calls)", wl.PAGES["login.html"].split("<script>")[1] == node_login.split("<script>")[1])
        check("P3 the setup page has the node's style and script", style(wl.PAGES["setup.html"]) == style(node_setup)
              and wl.PAGES["setup.html"].split("<script>")[1].replace("Ключи созданы. Открываю вход…", "Ключи созданы. Нода запускается…").replace("Создать пароли", "Создать и запустить ноду") == node_setup.split("<script>")[1])
        check("P4 the setup page no longer speaks of starting the node", "запустить ноду" not in wl.PAGES["setup.html"] and "Нода запускается" not in wl.PAGES["setup.html"])
        try:
            with patch.object(Path, "read_text", lambda self, **kw: "<html>a page that changed</html>"):
                wl._load_pages()
            check("P5 a node page that changed under the edits stops the server, it does not diverge silently", False)
        except RuntimeError:
            pass

    asyncio.run(scenario())

    # ── against the REAL tool: the same account, two doors ──
    if REAL_TOOL.is_file():
        async def real() -> None:
            state = fresh(account=False, tool=str(REAL_TOOL))
            async with client() as c:
                r = await c.post("/api/auth/setup", json={"login_password": LOGIN, "login_password_repeat": LOGIN, "master_password": MASTER, "master_password_repeat": MASTER})
                check("X1 [real tool] the passwords are created in this door", r.status_code == 200 and (state / "auth.json").exists() and (state / "device.key").exists())
                check("X2 [real tool] the typed login password opens this door", (await c.post("/api/auth/login", json={"login_password": LOGIN})).status_code == 200)
                check("X3 [real tool] a wrong one does not", (await c.post("/api/auth/login", json={"login_password": LOGIN + "x"})).status_code == 401)
                text = (state / "auth.json").read_text()
                check("X4 [real tool] the file holds neither typed secret in the clear", LOGIN not in text and MASTER not in text)
                import subprocess
                node_side = subprocess.run([str(REAL_TOOL), "login-check", "--dir", str(state)], input=(LOGIN + "\n").encode(), capture_output=True)
                check("X5 [real tool] the very same check the NODE uses agrees (one account, one implementation)", node_side.returncode == 0)
                r = await c.post("/api/auth/recover", json={"recovery_code": MASTER, "new_login_password": NEW_LOGIN})
                check("X6 [real tool] the master password resets the login password", r.status_code == 200
                      and subprocess.run([str(REAL_TOOL), "login-check", "--dir", str(state)], input=(NEW_LOGIN + "\n").encode(), capture_output=True).returncode == 0
                      and subprocess.run([str(REAL_TOOL), "login-check", "--dir", str(state)], input=(LOGIN + "\n").encode(), capture_output=True).returncode == 1)
                wl.throttle.failures = 0
                r = await c.post("/api/auth/recover", json={"recovery_code": "not the master phrase", "new_login_password": "another login pw"})
                check("X7 [real tool] a wrong master password is refused", r.status_code == 401)
        asyncio.run(real())
    else:
        print("SKIP [real tool]: node/target/debug/yandi-keys is not built (cd node && cargo build -p yandi-key-root)")

    # ── the assistant's real app is wired to it ──
    fresh()
    import pet.council_chat_server as server
    names = [m.cls.__name__ for m in server.app.user_middleware]

    async def wiring() -> None:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://127.0.0.1:9010") as c:
            r = await c.get("/", headers=NAVIGATE)
            check("W2 the real assistant page is behind the login for a browser", r.status_code == 302 and r.headers["location"] == "/login")
            r = await c.get("/")
            check("W3 …and unchanged for a local program, with a logout link", r.status_code == 200 and 'id="yandi-logout"' in r.text)
    check("W1 the real app: the local guard is outermost, then the login, inside it", names[:2] == ["LocalOnlyMiddleware", "WebLoginMiddleware"])
    asyncio.run(wiring())
    shutil.rmtree(tmp, ignore_errors=True)
    return failures


def mutants() -> list:
    import contextlib
    real_run_tool = wl.run_tool
    real_valid = wl.Sessions.valid

    @contextlib.contextmanager
    def swapped(obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        try:
            yield
        finally:
            setattr(obj, name, original)

    async def open_door(self, scope, receive, send):                         # M1: the gate lets everything through
        return await self.app(scope, receive, send)

    def any_password(command, *lines):                                       # M2: the check says yes to anything
        return (0, "") if command == "login-check" else real_run_tool(command, *lines)

    def cookie_without_httponly(token, secs):                                # M3
        return f"{wl.COOKIE}={token}; SameSite=Strict; Path=/; Max-Age={secs}"

    class NoThrottle(wl.Throttle):                                           # M5
        def remaining(self):
            return 0

    def tool_missing_is_ok(command, *lines):                                 # M7: a missing tool counts as success
        try:
            return real_run_tool(command, *lines)
        except wl.KeysToolError:
            return 0, ""

    def password_in_argv(command, *lines):                                   # M9: the password goes on the command line
        import subprocess
        tool = wl.keys_tool()
        try:
            proc = subprocess.run([str(tool), command, "--dir", str(wl.keys_dir()), *lines[:1]], input=("\n".join(lines) + "\n").encode(),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError:
            raise wl.KeysToolError("no tool") from None
        first = proc.stderr.decode().strip().splitlines()[:1]
        return proc.returncode, (first[0] if first else "").removeprefix("yandi-keys: ")

    class NeverExpiringSessions(wl.Sessions):                                # M10: sessions never expire
        # Подменяем ОБЪЕКТ wl.sessions (как M5/M6), а не метод класса: при включённом Rust-движке
        # (YANDI_LOGIN_ENGINE=rust) wl.sessions — Rust-объект, и порча метода питоновского класса его
        # не задела бы. Rust-мутанты проверяет pet_web_login_rust_parity_test.
        def valid(self, token):
            return bool(token) and token in self._items

    class KeepsSessions(wl.Sessions):                                        # M6: a reset does not end old sessions
        def clear(self):
            pass

    real_is_ext = wl._is_extension_request

    return [
        ("M1 the gate lets every browser through", swapped(wl.WebLoginMiddleware, "__call__", open_door)),
        ("M2 the password check says yes to anything", swapped(wl, "run_tool", any_password)),
        ("M3 the session cookie is readable by page scripts (no HttpOnly)", swapped(wl, "_cookie", cookie_without_httponly)),
        ("M4 the cookie has the node's name", swapped(wl, "COOKIE", "yandi_session")),
        ("M5 no slow-down after mistakes", swapped(wl, "throttle", NoThrottle())),
        ("M6 a password reset does not end old sessions", swapped(wl, "sessions", KeepsSessions())),
        ("M7 a missing key tool counts as a correct password", swapped(wl, "run_tool", tool_missing_is_ok)),
        ("M8 the extension may reach any path", swapped(wl, "_is_extension_request", lambda headers, path: bool(headers.get("origin", "").startswith("moz-extension://")))),
        ("M9 the password is given to the tool on its command line", swapped(wl, "run_tool", password_in_argv)),
        ("M10 sessions never expire", swapped(wl, "sessions", NeverExpiringSessions())),
        ("M11 the browser test is forgeable (the header is not required)", swapped(wl, "_is_browser", lambda headers: False)),
    ]


def main() -> int:
    print("clean code:")
    failures = run_checks()
    for name in failures:
        print(f"[FAIL] {name}")
    if not failures:
        print("[OK] every check passes")
    bad = bool(failures)
    for label, applied in mutants():
        with applied:
            try:
                caught = run_checks()
            except Exception as exc:                  # noqa: BLE001 — a defect that breaks the run is noticed too
                caught = [f"the run itself failed: {type(exc).__name__}"]
        print(f"[{'OK' if caught else 'FAIL'}] {label} -> {'caught, e.g. ' + caught[0][:70] if caught else 'NOT CAUGHT'}")
        bad = bad or not caught
    print("RESULT:", "all checks passed" if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
