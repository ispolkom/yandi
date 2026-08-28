from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Dict, Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REGISTRY_DIR = PROJECT_ROOT / "registry"
MEMORY_FILE = REGISTRY_DIR / "transport_memory.json"

_lock = threading.Lock()


def _domain(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _load() -> Dict[str, Any]:
    try:
        if not MEMORY_FILE.exists():
            return {
                "version": 1,
                "domains": {},
                "urls": {},
            }

        data = json.loads(
            MEMORY_FILE.read_text(
                encoding="utf-8"
            )
        )

        if not isinstance(data, dict):
            raise ValueError("root is not dict")

        data.setdefault("version", 1)
        data.setdefault("domains", {})
        data.setdefault("urls", {})

        return data

    except Exception:
        return {
            "version": 1,
            "domains": {},
            "urls": {},
        }


def _save(data: Dict[str, Any]) -> None:
    REGISTRY_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = MEMORY_FILE.with_suffix(".json.tmp")

    tmp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    tmp.replace(MEMORY_FILE)


def get_transport_state(
    url: str,
) -> Dict[str, Any]:
    domain = _domain(url)

    with _lock:
        data = _load()

        url_state = dict(
            data.get("urls", {}).get(
                url,
                {},
            )
        )

        domain_state = dict(
            data.get("domains", {}).get(
                domain,
                {},
            )
        )

    # URL-specific state имеет приоритет.
    state = dict(domain_state)
    state.update(url_state)

    return state


def preferred_transport(
    url: str,
) -> Optional[str]:
    state = get_transport_state(url)

    preferred = state.get(
        "preferred_transport"
    )

    if preferred in {
        "direct",
        "proxy",
        "browser",
    }:
        return preferred

    return None


def is_stoplisted(url: str) -> bool:
    """
    Permanent, no-TTL: True once direct AND proxy have BOTH genuinely
    failed for this URL (stoplist_url() below), or once this domain/URL
    was already flagged browser_required (the pre-existing Cloudflare-
    double-failure case — a strict subset of "both transports failed",
    folded into the same concept for backward compatibility with any
    transport_memory.json written before this field existed, no
    migration needed). Checked at both URL and domain level via
    get_transport_state()'s existing merge (domain state, then
    URL-specific state on top).
    """
    state = get_transport_state(url)
    return bool(state.get("stoplisted")) or bool(state.get("browser_required"))


def stoplist_url(url: str, direct_reason: str, proxy_reason: str) -> None:
    """
    Permanent ban, no TTL/expiry — matches the explicit product
    decision (a temporarily-down site is treated as operationally
    unusable; recoverable only by manually editing/clearing
    registry/transport_memory.json). Called ONLY after direct AND
    proxy have BOTH been attempted and BOTH failed with a genuine
    transport-level reason (never for content-level rejects like
    no_content/no_keywords — those say nothing about whether the URL
    is reachable, only whether THIS particular query/page matched).

    Domain-level promotion after >=2 independently stoplisted URLs on
    the same domain — mirrors the existing browser_required_count>=2
    pattern already used for the Cloudflare case, not a new policy.
    """
    domain = _domain(url)
    now = time.time()

    with _lock:
        data = _load()

        domains = data.setdefault("domains", {})
        urls = data.setdefault("urls", {})

        ustate = urls.setdefault(url, {})
        dstate = domains.setdefault(domain, {})

        if not ustate.get("stoplisted"):
            ustate["stoplisted"] = True
            ustate["stoplisted_at"] = now
            ustate["stoplisted_reason"] = f"direct={direct_reason}+proxy={proxy_reason}"

        stoplisted_count = sum(
            1
            for candidate_url, candidate_state in urls.items()
            if _domain(candidate_url) == domain
            and candidate_state.get("stoplisted") is True
        )

        if stoplisted_count >= 2:
            dstate["stoplisted"] = True
            dstate["stoplisted_at"] = now

        _save(data)


def record_transport_result(
    url: str,
    transport: str,
    status: str,
) -> None:
    """
    transport:
        direct | proxy | browser

    status examples:
        ok
        timeout
        http_403
        cloudflare_challenge
        fetch_failed
    """

    if transport not in {
        "direct",
        "proxy",
        "browser",
    }:
        return

    domain = _domain(url)

    now = time.time()

    with _lock:
        data = _load()

        domains = data.setdefault(
            "domains",
            {},
        )

        urls = data.setdefault(
            "urls",
            {},
        )

        dstate = domains.setdefault(
            domain,
            {},
        )

        ustate = urls.setdefault(
            url,
            {},
        )

        for state in (
            dstate,
            ustate,
        ):
            state[f"{transport}_status"] = status
            state["last_seen"] = now

            if status == "ok":
                state[f"{transport}_successes"] = (
                    int(
                        state.get(
                            f"{transport}_successes",
                            0,
                        )
                    )
                    + 1
                )
            else:
                state[f"{transport}_failures"] = (
                    int(
                        state.get(
                            f"{transport}_failures",
                            0,
                        )
                    )
                    + 1
                )

        # ----------------------------------------------------
        # Preferred transport policy
        # ----------------------------------------------------

        if status == "ok":
            ustate["preferred_transport"] = (
                transport
            )

            # Domain-level preference только после
            # нескольких успехов, чтобы один URL
            # не переписал весь домен.
            if (
                dstate.get(
                    f"{transport}_successes",
                    0,
                )
                >= 2
            ):
                dstate["preferred_transport"] = (
                    transport
                )

        # Cloudflare challenge:
        #
        # direct challenge сам по себе ещё не означает,
        # что browser обязателен — proxy может помочь.
        #
        # Но если direct И proxy получили challenge,
        # URL становится browser_required.
        if (
            ustate.get("direct_status")
            == "cloudflare_challenge"
            and
            ustate.get("proxy_status")
            == "cloudflare_challenge"
        ):
            # URL точно требует browser transport.
            ustate["preferred_transport"] = (
                "browser"
            )
            ustate["browser_required"] = True

            # Cloudflare challenge обычно действует на hostname,
            # а не только на одну конкретную страницу.
            #
            # Поэтому для этого СПЕЦИАЛЬНОГО случая разрешаем
            # сразу наследовать browser transport на домен.
            #
            # Это правило НЕ распространяется на timeout,
            # 403, 429 и прочие обычные ошибки.
            dstate["preferred_transport"] = (
                "browser"
            )
            dstate["browser_required"] = True

        # Для домена применяем browser preference
        # только после нескольких URL с таким результатом.
        browser_required_count = sum(
            1
            for candidate_url, candidate_state
            in urls.items()
            if (
                _domain(candidate_url)
                == domain
                and candidate_state.get(
                    "browser_required"
                )
                is True
            )
        )

        if browser_required_count >= 2:
            dstate["preferred_transport"] = (
                "browser"
            )
            dstate["browser_required"] = True

        _save(data)
