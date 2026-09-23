"""
pet/local_guard.py — «только своя веб-морда» для всего сервера.

Сервер слушает только 127.0.0.1, но браузер на этом же компьютере открывает и чужие сайты, а страница любого сайта
может обратиться к http://127.0.0.1:9010 (и подключиться к WebSocket): без защиты она прочитала бы историю чатов,
поменяла настройки и запустила команды агента. Поэтому ВЕСЬ сервер (LocalOnlyMiddleware, и HTTP, и WebSocket) работает
по правилу «по умолчанию нельзя». Запрос проходит, только если он пришёл:

  * с Host, который указывает на этот же компьютер (127.0.0.1 / localhost / ::1) — это отсекает DNS-rebinding, когда
    чужой домен временно указывают на 127.0.0.1;
  * без заголовка Origin ИЛИ с Origin, равным собственному адресу сервера — чужой сайт браузер подписывает своим Origin,
    подделать его страница не может;
  * без Sec-Fetch-Site ИЛИ с same-origin / none (страница чужого сайта, картинка, ссылка с другого сайта — cross-site);
  * ИЛИ от расширения Firefox (Origin moz-extension://…), но ТОЛЬКО на адреса из EXTENSION_PATHS: расширению нужны
    несколько адресов, а не весь сервер.

Программы на этом же компьютере (агент, скрипты, нода, curl) этих заголовков не шлют — им проход открыт; охрана защищает
от СТРАНИЦ и чужих расширений на чужих адресах, а не от локальных программ (те и так читают диск сами; для них будет вход
по паролю). Ответы получают заголовки против встраивания страницы в чужой фрейм.

Порядок в pet/council_chat_server.py: LocalOnlyMiddleware — самая внешняя прослойка (CORS её не обходит), CORS отвечает
только расширению (не «всем»).

Rust-перенос (2026-09-23): rustlib/yandi_rs/src/local_guard.rs — построчный перевод is_allowed_request/
is_local_request/is_extension_path/_host_name на Rust, доказанный на совпадение тестом
pet/pet_local_guard_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН — сервер работает на этой, Python-реализации,
как и раньше; включается переменной окружения YANDI_GUARD_ENGINE=rust ПОСЛЕ того, как rustlib/yandi_rs собран
(`maturin develop`, см. rustlib/README.md) и владелец сам решил попробовать. Если переменная стоит, а модуль не
собран — тихо остаёмся на Python (с предупреждением в лог), сервер не падает.
"""
from __future__ import annotations

import json
import logging
import os
import re

from fastapi import HTTPException, Request

log = logging.getLogger("yandi.guard")

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# Расширение Firefox: его Origin — moz-extension://<случайный uuid профиля>. Оно получает доступ ТОЛЬКО к этим адресам
# (сверяется тестом с тем, что вызывает код расширения, pet/extension/*.js).
EXTENSION_ORIGIN_RE = re.compile(r"moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
EXTENSION_PATH_PREFIXES = ("/api/ext/",)
EXTENSION_PATHS = frozenset({"/api/orchestrator/ask", "/api/orch/history", "/api/council/connections"})
EXTENSION_CORS_ORIGIN_REGEX = EXTENSION_ORIGIN_RE.pattern


def _host_name(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):                      # [::1]:9010
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_extension_path(path: str) -> bool:
    if not path or ".." in path or "//" in path or "\\" in path:
        return False
    return path in EXTENSION_PATHS or any(path.startswith(p) and len(path) > len(p) for p in EXTENSION_PATH_PREFIXES)


# ── необязательный Rust-движок (см. модульный docstring выше) ────────────────
_rust_guard = None          # None = ещё не пробовали; False = пробовали, не вышло/не запрошено; модуль = подключён


def _get_rust_guard():
    global _rust_guard
    if _rust_guard is None:
        if os.environ.get("YANDI_GUARD_ENGINE") == "rust":
            try:
                import yandi_rs.local_guard as _rs
                _rust_guard = _rs
                log.warning("YANDI_GUARD_ENGINE=rust: используется Rust-реализация local_guard (rustlib/yandi_rs)")
            except ImportError as e:
                log.warning("YANDI_GUARD_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_guard = False
        else:
            _rust_guard = False
    return _rust_guard or None


def _rust_headers(headers, host: str) -> dict:
    """headers для yandi_rs: ключ есть только если заголовок реально был (см. rustlib/yandi_rs/src/local_guard.rs)."""
    d = {"host": host}
    origin = headers.get("origin")
    if origin is not None:
        d["origin"] = origin
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None:
        d["sec-fetch-site"] = fetch_site
    return d


def is_allowed_request(headers, path: str | None = None) -> tuple[bool, str]:
    """(allowed, reason) for a request's headers (a mapping with case-insensitive get) and, when known, its path.
    With no path the extension is never allowed (strict form, for endpoints that only the own page may use)."""
    host = headers.get("host", "")
    rs = _get_rust_guard()
    if rs is not None:
        allowed, reason = rs.is_allowed_request(_rust_headers(headers, host), path)
        return bool(allowed), str(reason)
    if _host_name(host) not in LOOPBACK_HOSTS:
        return False, "запрос адресован не локальному хосту"
    origin = headers.get("origin")
    if origin is not None and origin != f"http://{host}":
        if path is not None and EXTENSION_ORIGIN_RE.fullmatch(origin) and is_extension_path(path):
            return True, ""                       # расширение на своём адресе; Sec-Fetch-Site у него не «same-origin»
        return False, "запрос отправлен страницей другого сайта"
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False, "запрос отправлен страницей другого сайта"
    return True, ""


def is_local_request(headers) -> tuple[bool, str]:
    """Строгая форма: только своя страница и локальные программы (расширению не открыто ничего)."""
    return is_allowed_request(headers, None)


async def require_local_origin(request: Request) -> None:
    """FastAPI-зависимость для эндпоинтов, которым расширение не нужно: Depends(require_local_origin)."""
    ok, reason = is_local_request(request.headers)
    if not ok:
        raise HTTPException(status_code=403, detail=f"forbidden: {reason}")


_SECURITY_HEADERS = [
    (b"x-frame-options", b"DENY"),
    (b"content-security-policy", b"frame-ancestors 'none'"),
    (b"x-content-type-options", b"nosniff"),
]
_GUARDED = ("host", "origin", "sec-fetch-site")


class LocalOnlyMiddleware:
    """Чистая ASGI-прослойка (в отличие от BaseHTTPMiddleware она видит и WebSocket)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        seen: dict[str, str] = {}
        ambiguous = False
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1").lower()
            if name in _GUARDED:
                if name in seen:
                    ambiguous = True                  # два одинаковых заголовка — так честный браузер не шлёт
                seen[name] = raw_value.decode("latin-1")
        path = scope.get("path", "")
        allowed, reason = (False, "неоднозначные заголовки запроса") if ambiguous else is_allowed_request(seen, path)
        if not allowed:
            log.warning("отказ: %s %s (%s; Origin=%s)", scope["type"], path, reason, seen.get("origin"))
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = json.dumps({"detail": f"forbidden: {reason}"}, ensure_ascii=False).encode("utf-8")
            await send({"type": "http.response.start", "status": 403,
                        "headers": [(b"content-type", b"application/json; charset=utf-8"),
                                    (b"content-length", str(len(body)).encode()), *_SECURITY_HEADERS]})
            await send({"type": "http.response.body", "body": body})
            return
        if scope["type"] == "websocket":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message = {**message, "headers": [*message.get("headers", []), *[h for h in _SECURITY_HEADERS if h[0] not in present]]}
            await send(message)
        await self.app(scope, receive, send_with_headers)
