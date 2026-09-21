"""
pet/local_guard.py — «только своя веб-морда» для опасных эндпоинтов.

Сервер отвечает CORS `allow_origins=["*"]` (так работает расширение Firefox), значит ЛЮБОЙ сайт, открытый в
браузере, может обратиться к http://127.0.0.1:9010. Эндпоинты, которые показывают файловую систему или меняют
настройки узла, этого допускать не должны. Охрана пропускает запрос, только если он пришёл:

  * с Host, который указывает на этот же компьютер (127.0.0.1 / localhost / ::1) — это отсекает DNS-rebinding,
    когда чужой домен временно указывают на 127.0.0.1;
  * без заголовка Origin ИЛИ с Origin, равным собственному адресу сервера — чужой сайт браузер подписывает своим
    Origin, подделать его страница не может;
  * без Sec-Fetch-Site ИЛИ с same-origin / none.

Программы на этом же компьютере (curl, агент) этих заголовков не шлют — им проход открыт; охрана защищает от
СТРАНИЦ, а не от локальных программ (те и так читают диск сами).
"""
from __future__ import annotations

from fastapi import HTTPException, Request

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_name(host_header: str) -> str:
    host = (host_header or "").strip().lower()
    if host.startswith("["):                      # [::1]:9010
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def is_local_request(headers) -> tuple[bool, str]:
    """(allowed, reason) for a request's headers (a mapping with case-insensitive get)."""
    host = headers.get("host", "")
    if _host_name(host) not in LOOPBACK_HOSTS:
        return False, "запрос адресован не локальному хосту"
    origin = headers.get("origin")
    if origin is not None and origin != f"http://{host}":
        return False, "запрос отправлен страницей другого сайта"
    fetch_site = headers.get("sec-fetch-site")
    if fetch_site is not None and fetch_site not in ("same-origin", "none"):
        return False, "запрос отправлен страницей другого сайта"
    return True, ""


async def require_local_origin(request: Request) -> None:
    """FastAPI-зависимость: Depends(require_local_origin)."""
    ok, reason = is_local_request(request.headers)
    if not ok:
        raise HTTPException(status_code=403, detail=f"forbidden: {reason}")
