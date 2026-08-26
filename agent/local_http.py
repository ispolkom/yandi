"""
assistant/local_http.py — HTTP к localhost без прокси.

Использование:
    from agent.local_http import local_get, local_post, local_session

Причина: HTTP_PROXY/HTTPS_PROXY глобально выставлен в системе.
requests и urllib по умолчанию пускают через него ВСЕ запросы,
включая 127.0.0.1 и localhost. local_session.trust_env=False обходит это.
"""
import requests as _requests

local_session = _requests.Session()
local_session.trust_env = False  # игнорировать HTTP_PROXY, HTTPS_PROXY


def local_get(url: str, **kwargs) -> _requests.Response:
    return local_session.get(url, **kwargs)


def local_post(url: str, **kwargs) -> _requests.Response:
    return local_session.post(url, **kwargs)
