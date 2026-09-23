"""
pet/web_login.py — вход по паролю во ВТОРУЮ веб-морду (Помощница, порт 9010) с тем же паролем и тем же мастер-паролем, что у ноды.

Учётная запись одна: файл ~/.yandi_keys/auth.json (пароль входа + ключ восстановления = мастер-пароль, который человек придумал
при первой настройке). Обе веб-морды работают с ней; каждая просит пароль при входе (или восстановление по мастер-паролю), у каждой
своя сессия. Можно настроить пароли в любой из них — во вторую войдёшь тем же.

ОДНА реализация проверки. Пароль не проверяется в Python: страницу обслуживает утилита `yandi-keys` (login-check, login-reset, setup),
тот же код на Rust, что использует нода (node/key_root/src/login.rs). Пароли идут утилите через стандартный ввод, не через аргументы.
Утилиты нет — вход закрыт (не открыт): страница входа скажет, что собрать.

ФОРМЫ — те же самые файлы, что у ноды: node/src/web/ui/login.html и setup.html читаются отсюда при запуске, поэтому вид и поведение
совпадают по построению, а не по копии (в них подменены только названия «нода» -> «Помощница», см. _PAGE_EDITS; если страница ноды
изменилась так, что подмена не находит места, сервер не запускается, а не молча расходится).

КОГО ПРОВЕРЯЕМ. Запрос из БРАУЗЕРА (у него есть заголовок Sec-Fetch-Site, страница не может его подделать) обязан иметь сессию.
Программы на этом же компьютере (агент, скрипты, curl) этого заголовка не шлют и проходят как раньше: для них вход по паролю не
подходит, пока они не переехали за ноду (docs/STORAGE_PROTECTION.md, «Honest limits»). Расширение Firefox проходит только на своих
адресах, как и раньше (pet/local_guard.py).

Rust-перенос (2026-09-23): rustlib/yandi_rs/src/web_login.rs — построчный перевод классов Sessions/Throttle (сами определения
классов здесь, в Python, остаются — их сабклассит pet_web_login_regression_test.py для собственных тестовых дублей; переносится
то, что встаёт ВМЕСТО экземпляров-одиночек `sessions`/`throttle`). Доказан на совпадение тестом
pet/pet_web_login_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН; включается переменной окружения YANDI_LOGIN_ENGINE=rust ПОСЛЕ
сборки rustlib/yandi_rs (`maturin develop`, см. rustlib/README.md). Не собран — тихо остаёмся на Python, сервер не падает.
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from pet.local_guard import EXTENSION_ORIGIN_RE, is_extension_path

log = logging.getLogger("yandi.web_login")

COOKIE = "yandi_pet_session"                    # не совпадает с cookie ноды (cookie не различают порты одного адреса)
SESSION_SECS = 12 * 3600
REMEMBER_SECS = 30 * 24 * 3600
FREE_ATTEMPTS, BACKOFF_BASE, BACKOFF_MAX = 3, 2, 300          # те же правила замедления, что у ноды (node/src/web/auth.rs)
TOOL_TIMEOUT = 40
REPO = Path(__file__).resolve().parents[1]
UI_DIR = REPO / "node" / "src" / "web" / "ui"

PUBLIC_PATHS = frozenset({"/login", "/setup", "/api/auth/login", "/api/auth/recover", "/api/auth/setup", "/api/auth/status", "/api/auth/logout"})

# что заменено в страницах ноды (старое, новое); нет старого текста — ошибка запуска
_PAGE_EDITS = {
    "login.html": [
        ("<title>YANDI — Вход</title>", "<title>YANDI — Помощница: вход</title>"),
        ('<p id="subtitle">Добро пожаловать</p>', '<p id="subtitle">Помощница</p>'),
    ],
    "setup.html": [
        ("Создать и запустить ноду", "Создать пароли"),
        ("Ключи созданы. Нода запускается…", "Ключи созданы. Открываю вход…"),
        ("<p>Первичная настройка ноды</p>", "<p>Первичная настройка · Помощница</p>"),
    ],
}


def _load_pages() -> dict:
    pages = {}
    for name, edits in _PAGE_EDITS.items():
        text = (UI_DIR / name).read_text(encoding="utf-8")
        for old, new in edits:
            if old not in text:
                raise RuntimeError(f"страница ноды {name} изменилась: не найдено {old!r}; обнови pet/web_login._PAGE_EDITS")
            text = text.replace(old, new)
        pages[name] = text
    return pages


PAGES = _load_pages()


# ── утилита ключей ────────────────────────────────────────────────────────────────────────────────────────────────

class KeysToolError(Exception):
    """Утилита недоступна или сломалась (не «неверный пароль»)."""


def keys_tool() -> Path:
    return Path(os.environ.get("YANDI_KEYS_TOOL") or REPO / "node" / "target" / "release" / "yandi-keys")


def keys_dir() -> Path:
    return Path(os.environ.get("YANDI_KEYS_DIR") or Path.home() / ".yandi_keys")


def account_exists() -> bool:
    try:
        return (keys_dir() / "auth.json").exists()
    except OSError:
        return False


def _safe_line(value: object) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise ValueError("недопустимые символы в пароле")
    return value


def run_tool(command: str, *lines: str) -> tuple[int, str]:
    """(код выхода, первая строка stderr). Пароли — только через стандартный ввод."""
    tool = keys_tool()
    if not tool.is_file() or not os.access(tool, os.X_OK):
        raise KeysToolError("Не собрана утилита ключей. Выполните: cd ~/yandi/node && cargo build --release -p yandi-key-root")
    env = {k: v for k, v in os.environ.items() if k != "YANDI_KEY_PASSWORD"}
    try:
        proc = subprocess.run([str(tool), command, "--dir", str(keys_dir())], input=("\n".join(lines) + "\n").encode("utf-8"),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TOOL_TIMEOUT, env=env)
    except (OSError, subprocess.SubprocessError):
        raise KeysToolError("Утилита ключей не запустилась") from None
    first = proc.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
    message = first[0] if first else ""
    return proc.returncode, message.removeprefix("yandi-keys: ")


# ── сессии и замедление ───────────────────────────────────────────────────────────────────────────────────────────

class Sessions:
    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self._items: dict[str, float] = {}

    def create(self, remember: bool) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        secs = REMEMBER_SECS if remember else SESSION_SECS
        self._items[token] = self._clock() + secs
        self._purge()
        return token, secs

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        expires = self._items.get(token)
        if expires is None:
            return False
        if expires <= self._clock():
            self._items.pop(token, None)
            return False
        return True

    def end(self, token: Optional[str]) -> None:
        self._items.pop(token or "", None)

    def clear(self) -> None:
        self._items.clear()

    def _purge(self) -> None:
        now = self._clock()
        for token in [t for t, e in self._items.items() if e <= now]:
            del self._items[token]


class Throttle:
    """Замедление подбора: несколько ошибок бесплатно, дальше 2, 4, 8… секунд, не больше 300; настоящий успех обнуляет."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._clock = clock
        self.failures = 0
        self.last = 0.0

    @staticmethod
    def backoff_after(failures: int) -> int:
        if failures <= FREE_ATTEMPTS:
            return 0
        return min(BACKOFF_BASE * (1 << min(failures - FREE_ATTEMPTS - 1, 31)), BACKOFF_MAX)

    def remaining(self) -> int:
        backoff = self.backoff_after(self.failures)
        if backoff == 0:
            return 0
        wait = backoff - (self._clock() - self.last)
        return int(wait) + 1 if wait > 0 else 0

    def failed(self) -> None:
        self.failures += 1
        self.last = self._clock()

    def succeeded(self) -> None:
        self.failures = 0


# ── необязательный Rust-движок (см. модульный docstring выше) ────────────────
def _make_sessions() -> "Sessions":
    """Обычно Python Sessions(); если YANDI_LOGIN_ENGINE=rust и rustlib/yandi_rs собран — Rust-класс
    с тем же публичным API (create/valid/end/clear/_clock). Отдельная функция (не инлайн), чтобы
    pet_web_login_rust_parity_test.py могла вызвать её напрямую с любым состоянием окружения,
    не полагаясь на то, что происходило при первом импорте модуля."""
    if os.environ.get("YANDI_LOGIN_ENGINE") == "rust":
        try:
            from yandi_rs.web_login import Sessions as _RustSessions
            log.warning("YANDI_LOGIN_ENGINE=rust: используется Rust-реализация Sessions (rustlib/yandi_rs)")
            return _RustSessions()
        except ImportError as e:
            log.warning("YANDI_LOGIN_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python Sessions", e)
    return Sessions()


def _make_throttle() -> "Throttle":
    """Sessions-аналог для Throttle — см. _make_sessions."""
    if os.environ.get("YANDI_LOGIN_ENGINE") == "rust":
        try:
            from yandi_rs.web_login import Throttle as _RustThrottle
            log.warning("YANDI_LOGIN_ENGINE=rust: используется Rust-реализация Throttle (rustlib/yandi_rs)")
            return _RustThrottle()
        except ImportError as e:
            log.warning("YANDI_LOGIN_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python Throttle", e)
    return Throttle()


sessions = _make_sessions()
throttle = _make_throttle()


def reset_state() -> None:
    """Для тестов."""
    sessions.clear()
    throttle.failures = 0
    throttle.last = 0.0


def _cookie(token: str, secs: int) -> str:
    return f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={secs}"


def _cookie_from(headers: dict) -> Optional[str]:
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE:
            return value
    return None


# ── маршруты ──────────────────────────────────────────────────────────────────────────────────────────────────────

router = APIRouter()


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status)


async def _tool(command: str, *lines: str) -> tuple[int, str]:
    return await asyncio.get_running_loop().run_in_executor(None, lambda: run_tool(command, *lines))


@router.get("/login")
async def login_page():
    if not account_exists():
        return RedirectResponse("/setup", status_code=302)
    return HTMLResponse(PAGES["login.html"], headers={"Cache-Control": "no-store"})


@router.get("/setup")
async def setup_page():
    if account_exists():
        return RedirectResponse("/login", status_code=302)
    return HTMLResponse(PAGES["setup.html"], headers={"Cache-Control": "no-store"})


@router.get("/api/auth/status")
async def status(request: Request):
    return {"ok": True, "account": account_exists(), "logged_in": sessions.valid(request.cookies.get(COOKIE))}


@router.post("/api/auth/login")
async def login(payload: dict):
    wait = throttle.remaining()
    if wait:
        return _error(429, f"Слишком много попыток. Подождите {wait} с")
    try:
        password = _safe_line(payload.get("login_password"))
    except ValueError as exc:
        return _error(400, str(exc))
    if not password:
        return _error(400, "Введите пароль")
    try:
        code, message = await _tool("login-check", password)
    except KeysToolError as exc:
        return _error(503, str(exc))
    if code == 0:
        throttle.succeeded()
        token, secs = sessions.create(bool(payload.get("remember_me")))
        response = JSONResponse({"ok": True})
        response.headers["set-cookie"] = _cookie(token, secs)
        return response
    if code == 1:
        throttle.failed()
        return _error(401, "Неверный пароль")
    return _error(503, message or "Вход недоступен")


@router.post("/api/auth/recover")
async def recover(payload: dict):
    wait = throttle.remaining()
    if wait:
        return _error(429, f"Слишком много попыток. Подождите {wait} с")
    try:
        secret = _safe_line(payload.get("recovery_code"))
        new_password = _safe_line(payload.get("new_login_password"))
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        code, message = await _tool("login-reset", secret.strip(), new_password)
    except KeysToolError as exc:
        return _error(503, str(exc))
    if code == 0:
        throttle.succeeded()
        sessions.clear()                                          # сброс пароля заканчивает все прежние сессии
        token, secs = sessions.create(bool(payload.get("remember_me")))
        response = JSONResponse({"ok": True})
        response.headers["set-cookie"] = _cookie(token, secs)
        return response
    if code == 1:
        throttle.failed()
        return _error(401, message or "Не удалось восстановить")
    return _error(503, message or "Восстановление недоступно")


@router.post("/api/auth/setup")
async def setup(payload: dict):
    if account_exists():
        return _error(409, "Пароли уже созданы. Войдите или восстановите доступ")
    try:
        lines = [_safe_line(payload.get(k)) for k in ("login_password", "login_password_repeat", "master_password", "master_password_repeat")]
    except ValueError as exc:
        return _error(400, str(exc))
    try:
        code, message = await _tool("setup", *lines)
    except KeysToolError as exc:
        return _error(503, str(exc))
    if code == 0:
        return {"ok": True}
    return _error(400 if code == 1 else 503, message or "Ошибка настройки")


@router.post("/api/auth/logout")
async def logout(request: Request):
    sessions.end(request.cookies.get(COOKIE))
    response = JSONResponse({"ok": True})
    response.headers["set-cookie"] = f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
    return response


# ── охрана ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _is_browser(headers: dict) -> bool:
    return "sec-fetch-site" in headers or "sec-fetch-mode" in headers


def _is_extension_request(headers: dict, path: str) -> bool:
    return bool(EXTENSION_ORIGIN_RE.fullmatch(headers.get("origin", ""))) and is_extension_path(path)


class WebLoginMiddleware:
    """Запрос из браузера без действующей сессии не доходит до приложения: страницы уводятся на /login (или /setup, пока пароли не
    созданы), API получает 401, WebSocket закрывается. Вход, восстановление и настройка открыты (они и создают сессию)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        if (not _is_browser(headers) or path in PUBLIC_PATHS or _is_extension_request(headers, path)
                or sessions.valid(_cookie_from(headers))):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        wants_page = scope.get("method") == "GET" and (path == "/" or headers.get("sec-fetch-mode") == "navigate"
                                                      or "text/html" in headers.get("accept", ""))
        if wants_page:
            response = RedirectResponse("/login" if account_exists() else "/setup", status_code=302)
        else:
            response = JSONResponse({"ok": False, "error": "login required"}, status_code=401)
        response.headers["cache-control"] = "no-store"
        return await response(scope, receive, send)


LOGOUT_SNIPPET = ('<a href="#" id="yandi-logout" onclick="fetch(\'/api/auth/logout\',{method:\'POST\'}).then(()=>{location.href=\'/login\'});return false;" '
                  'style="position:fixed;right:12px;bottom:10px;z-index:9999;font:12px system-ui,sans-serif;color:#94a3b8;'
                  'background:#0f172a;border:1px solid #334155;border-radius:8px;padding:5px 10px;text-decoration:none;">Выйти</a>')


def startup_line() -> str:
    tool = keys_tool()
    if not tool.is_file():
        return "вход по паролю: утилита ключей не собрана — браузерам вход закрыт (cd ~/yandi/node && cargo build --release -p yandi-key-root)"
    return "вход по паролю: включён; пароли " + ("уже созданы" if account_exists() else "ещё не созданы — откройте страницу и задайте их")
