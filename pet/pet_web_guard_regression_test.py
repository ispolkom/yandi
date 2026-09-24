"""
pet/pet_web_guard_regression_test.py — ВЕСЬ СЕРВЕР ЗАКРЫТ ДЛЯ ЧУЖИХ САЙТОВ (HTTP и WebSocket).

    ПО УМОЛЧАНИЮ НЕЛЬЗЯ.   СТРАНИЦА ЧУЖОГО САЙТА НЕ ЧИТАЕТ, НЕ МЕНЯЕТ И НЕ ЗАПУСКАЕТ НИЧЕГО.
    РАСШИРЕНИЕ FIREFOX — ТОЛЬКО НА СВОИХ АДРЕСАХ.   CORS «*» БОЛЬШЕ НЕТ.

Правило (pet/local_guard.LocalOnlyMiddleware) проверяется на ASGI-уровне без сервера: и обычные запросы, и WebSocket, и «хитрые»
пути; затем — вместе с настоящим CORS-слоем; в конце мутанты (каждый — реальное изменение исходника правила).

Run: python -m pet.pet_web_guard_regression_test
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"[{'OK' if condition else 'FAIL'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


UUID = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
EXT = f"moz-extension://{UUID}"
OWN = "127.0.0.1:9010"


def headers_for(host=OWN, origin=None, site=None, extra=()):
    h = [(b"host", host.encode())]
    if origin is not None:
        h.append((b"origin", origin.encode()))
    if site is not None:
        h.append((b"sec-fetch-site", site.encode()))
    h.extend((k.encode(), v.encode()) for k, v in extra)
    return h


async def _inner(scope, receive, send):
    if scope["type"] == "websocket":
        await send({"type": "websocket.accept"})
    else:
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"ok"})


def call(mod, path, headers, kind="http", method="GET"):
    """('http', status, response headers) or ('ws', 'accepted' | 'refused', None)."""
    scope = {"type": kind, "path": path, "method": method, "headers": headers, "query_string": b""}
    sent = []

    async def receive():
        return {"type": "websocket.connect"} if kind == "websocket" else {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)
    asyncio.run(mod.LocalOnlyMiddleware(_inner)(scope, receive, send))
    if kind == "websocket":
        return "ws", ("accepted" if any(m["type"] == "websocket.accept" for m in sent) else "refused"), None
    start = next(m for m in sent if m["type"] == "http.response.start")
    return "http", start["status"], dict((k.decode(), v.decode()) for k, v in start["headers"])


def scenario(mod) -> list[str]:
    """Every invariant of the rule; returns the ones that are VIOLATED."""
    bad: list[str] = []

    def http(path, **kw):
        return call(mod, path, headers_for(**kw))[1]

    def ws(path, **kw):
        return call(mod, path, headers_for(**kw), kind="websocket")[1]
    everything = ["/", "/api/tools/run", "/api/local/history", "/api/council/config", "/api/browser/open", "/api/ext/poll",
                  "/api/orchestrator/ask", "/api/ui/settings", "/media/logo.png"]
    # I1: a foreign website, however it reaches the server
    for label, kw in {"чужой Origin": dict(origin="http://evil.example"), "тот же хост, другой порт": dict(origin="http://127.0.0.1:9999"),
                      "Origin null": dict(origin="null"), "cross-site без Origin (картинка, ссылка)": dict(site="cross-site"),
                      "same-site": dict(site="same-site"), "чужое расширение chrome": dict(origin=f"chrome-extension://{UUID}"),
                      "DNS-rebinding": dict(host="evil.example:9010"), "IP не локальный": dict(host="192.168.1.5:9010")}.items():
        if any(http(p, **kw) != 403 for p in everything):
            bad.append(f"I1 {label}: HTTP пропущен")
        if ws("/ws/human", **kw) != "refused":
            bad.append(f"I1 {label}: WebSocket пропущен")
    # I2: own page and local programs pass
    for kw in (dict(), dict(origin="http://127.0.0.1:9010", site="same-origin"), dict(site="none"), dict(host="localhost:9010", origin="http://localhost:9010"),
               dict(host="[::1]:9010")):
        if any(http(p, **kw) != 200 for p in everything):
            bad.append(f"I2 свой запрос {kw} не пропущен")
        if ws("/ws/human", **kw) != "accepted":
            bad.append(f"I2 свой WebSocket {kw} не пропущен")
    # I3: the extension — its own paths only
    allowed = ["/api/ext/poll", "/api/ext/result", "/api/ext/orch/poll", "/api/ext/orch/result", "/api/orchestrator/ask", "/api/orch/history",
               "/api/council/connections"]
    if any(http(p, origin=EXT, site="cross-site") != 200 for p in allowed):
        bad.append("I3 расширение не пущено на свои адреса")
    foreign_paths = ["/", "/api/tools/run", "/api/tools/execute", "/api/local/history", "/api/council/config", "/api/browser/open", "/api/ui/settings",
                     "/api/models/browse", "/api/agent/state", "/api/council/broadcast", "/api/ext", "/api/ext/", "/media/logo.png",
                     "/api/ext/../tools/run", "/api/ext//x", "/api/ext/x/../../tools/run", "/api/orchestrator/ask/../../tools/run", "/api/orchestrator/asks"]
    if any(http(p, origin=EXT, site="cross-site") == 200 for p in foreign_paths):
        bad.append("I3 расширению открыт не только свой набор адресов: " + ", ".join(p for p in foreign_paths if http(p, origin=EXT, site="cross-site") == 200))
    if ws("/ws/human", origin=EXT) != "refused":
        bad.append("I3 расширению открыт WebSocket")
    for fake in ("moz-extension://evil", f"moz-extension://{UUID}.evil.example", f"moz-extension://{UUID}/x", f"http://{UUID}"):
        if http("/api/ext/poll", origin=fake) == 200:
            bad.append(f"I3 поддельное расширение {fake} пропущено")
    # I4: two headers that must be single make the request ambiguous
    dup_origin = [(b"host", OWN.encode()), (b"origin", b"http://127.0.0.1:9010"), (b"origin", b"http://evil.example")]
    if call(mod, "/api/tools/run", dup_origin)[1] != 403:
        bad.append("I4 два Origin пропущены")
    dup_host = [(b"host", OWN.encode()), (b"host", b"evil.example")]
    if call(mod, "/api/tools/run", dup_host)[1] != 403:
        bad.append("I4 два Host пропущены")
    # I5: security headers on answers (allowed and refused)
    for status_kw in (dict(), dict(origin="http://evil.example")):
        h = call(mod, "/", headers_for(**status_kw))[2]
        if h.get("x-frame-options") != "DENY" or "frame-ancestors 'none'" not in h.get("content-security-policy", "") or h.get("x-content-type-options") != "nosniff":
            bad.append(f"I5 нет защитных заголовков ({'отказ' if status_kw else 'ответ'})")
    return bad


def main() -> int:
    import pet.local_guard as lg

    violated = scenario(lg)
    check("1: настоящее правило соблюдает все инварианты I1-I5 (чужие сайты, свои запросы, расширение, двойные заголовки, заголовки защиты)", violated == [], repr(violated))

    # ── 2. вместе с настоящим CORS-слоем ──
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origin_regex=lg.EXTENSION_CORS_ORIGIN_REGEX, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(lg.LocalOnlyMiddleware)

    @app.get("/api/council/connections")
    async def _connections():
        return {"claude": {"connected": False}}

    @app.post("/api/orchestrator/ask")
    async def _ask(payload: dict):
        return {"ok": True}

    @app.post("/api/tools/run")
    async def _run(payload: dict):
        return {"ran": True}
    c = TestClient(app, base_url="http://127.0.0.1:9010")
    pre = c.options("/api/orchestrator/ask", headers={"Origin": EXT, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
                                                     "Sec-Fetch-Site": "cross-site"})
    check("2: предзапрос расширения на его адрес проходит и получает разрешение именно для этого расширения",
          pre.status_code == 200 and pre.headers.get("access-control-allow-origin") == EXT, repr(dict(pre.headers)))
    r = c.get("/api/council/connections", headers={"Origin": EXT, "Sec-Fetch-Site": "cross-site"})
    check("2: ответ расширению читаем (есть разрешение CORS) и содержит данные", r.status_code == 200 and r.headers.get("access-control-allow-origin") == EXT and "claude" in r.json())
    pre_evil = c.options("/api/tools/run", headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    check("2: предзапрос чужого сайта отвергнут ещё до CORS и не получает никакого разрешения",
          pre_evil.status_code == 403 and "access-control-allow-origin" not in pre_evil.headers)
    ran = c.post("/api/tools/run", json={"tool": "shell.run"}, headers={"Origin": "http://evil.example", "Content-Type": "text/plain"})
    check("2: «простой» запрос чужого сайта (без предзапроса) тоже не доходит до обработчика", ran.status_code == 403 and "ran" not in ran.text)
    ext_tools = c.post("/api/tools/run", json={}, headers={"Origin": EXT, "Sec-Fetch-Site": "cross-site"})
    check("2: расширение на чужой для него адрес не допущено, хотя CORS его Origin знает", ext_tools.status_code == 403)
    check("2: разрешения CORS для произвольного сайта нет нигде (нет «*»)", all("access-control-allow-origin" not in x.headers for x in (
        c.get("/api/council/connections", headers={"Origin": "http://evil.example"}), pre_evil, ran)))

    # ── 3. сервер настроен именно так ──
    src = (ROOT / "pet" / "council_chat_server.py").read_text(encoding="utf-8")
    check("3: в сервере нет разрешения CORS для всех сайтов", 'allow_origins=["*"]' not in src and "allow_origins=['*']" not in src)
    check("3: CORS отвечает только расширению", "allow_origin_regex=EXTENSION_CORS_ORIGIN_REGEX" in src)
    check("3: прослойка правила подключена и добавлена ПОСЛЕ CORS (то есть она самая внешняя)",
          "app.add_middleware(LocalOnlyMiddleware)" in src and src.index("app.add_middleware(LocalOnlyMiddleware)") > src.index("app.add_middleware(\n    CORSMiddleware"))
    check("3: сервер слушает только 127.0.0.1 по умолчанию", 'parser.add_argument("--host",            default="127.0.0.1"' in src)

    # ── 4. МУТАНТЫ: каждый — реальное изменение исходника правила ──
    lg_src = inspect.getsource(lg)

    def mutate(old, new, label):
        assert lg_src.count(old) == 1, (label, old)
        mod = types.ModuleType("pet.local_guard__mutant")
        mod.__file__ = lg.__file__
        sys.modules[mod.__name__] = mod
        exec(compile(lg_src.replace(old, new), lg.__file__, "exec"), mod.__dict__)
        # Мутант — это испорченный ПИТОНОВСКИЙ исходник правила; если в окружении включён Rust-движок
        # (YANDI_GUARD_ENGINE=rust), делегирование в начале функций обошло бы порчу и мутант «выжил» бы.
        # Поэтому мутанту явно выключаем Rust (Rust-мутанты проверяет pet_local_guard_rust_parity_test).
        mod._rust_guard = False
        return mod
    mutants = {
        "M1 расширению открыт весь /api/": ('EXTENSION_PATH_PREFIXES = ("/api/ext/",)', 'EXTENSION_PATH_PREFIXES = ("/api/",)'),
        "M2 правило не смотрит на WebSocket": ('if scope["type"] not in ("http", "websocket"):', 'if scope["type"] not in ("http",):'),
        "M3 не проверяется Host (DNS-rebinding)": ('if _host_name(host) not in LOOPBACK_HOSTS:\n        return False, "запрос адресован не локальному хосту"', "if False:\n        pass"),
        "M4 Sec-Fetch-Site не проверяется": ('if fetch_site is not None and fetch_site not in ("same-origin", "none"):', "if False:"),
        "M5 двойные заголовки пропускаются": ('(False, "неоднозначные заголовки запроса") if ambiguous else', "("),
        "M6 в путях расширения разрешены «..»": ('if not path or ".." in path or "//" in path or "\\\\" in path:', "if not path:"),
        "M7 любой moz-extension, включая поддельный": ('EXTENSION_ORIGIN_RE = re.compile(r"moz-extension://[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")',
                                                        'EXTENSION_ORIGIN_RE = re.compile(r"moz-extension://.*")'),
        "M8 нет защитных заголовков": ("_SECURITY_HEADERS = [", "_SECURITY_HEADERS = [] and ["),
    }
    for label, (old, new) in mutants.items():
        try:
            m = mutate(old, new, label)
            found = scenario(m)
        except Exception as exc:  # noqa: BLE001 - мутант, который ломает правило вовсе, тоже пойман
            found = [f"падение: {exc}"]
        check(f"{label} ЛОВИТСЯ", found != [], "правило осталось «в порядке» — тест ничего не заметил")

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
