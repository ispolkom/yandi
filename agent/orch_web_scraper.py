"""
assistant/orch_web_scraper.py — Web Scraper с DuckDuckGo и фильтрацией.
Сохраняет причины отклонения каждого URL для evidence_trace.
"""
from __future__ import annotations

import concurrent.futures
import re
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from typing import Optional, List, Dict, Any

import requests as _requests
from bs4 import BeautifulSoup

from agent.orch_schemas import WebQueryResult, WebScrapeResult, WebSnippet
from agent.source_quality import evaluate_source_quality
from agent.transport_memory import (
    record_transport_result,
    is_stoplisted,
    stoplist_url,
)

# Попытка импорта DuckDuckGo
try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False
    print("[scraper] duckduckgo-search не установлен. Установи: pip install duckduckgo-search")

_session = _requests.Session()
_session.trust_env = False

# ------------------------------------------------------------
# OPTIONAL PROXY TRANSPORT
# ------------------------------------------------------------
#
# Секреты НЕ хранятся в source code.
# Формат /home/iam/yandi/proxy.txt:
#
#     host:port:user:password
#
# Direct transport остаётся основным.
# Proxy используется только для retry queue.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROXY_FILE = PROJECT_ROOT / "proxy.txt"


# ------------------------------------------------------------
# SHARED FETCH CACHE (P0, performance architecture pass)
# ------------------------------------------------------------
#
# FUNDAMENTAL INVARIANT: computation may be shared, epistemic
# ownership must not be shared implicitly. This cache stores ONLY the
# raw physical fetch result (HTTP GET + HTML text extraction + title)
# — never a per-claim/per-query decision. Confirmed by reading every
# call site in this file: _fetch_url()/_fetch_url_proxy() are ALWAYS
# invoked with query="" here (their query-keyword relevance branch is
# dead code on this path), so caching their whole return value carries
# no risk of leaking one claim's relevance judgement into another's.
# Each claim still independently runs directness/NLI/eligibility
# against this same shared raw content downstream — this cache never
# decides supports/contradicts/eligible, only "don't re-download".
#
# Request-scoped: one instance is meant to live for the duration of
# one retrieve_for_claims() call (one user query), not persisted
# across queries or users. Thread-safe in-flight dedup (not just a
# plain dict) — with claim workers running in a ThreadPoolExecutor,
# two threads can race to fetch the same URL at nearly the same
# instant; a plain dict would let both through.
class SharedFetchCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._results: Dict[str, tuple] = {}
        self._events: Dict[str, threading.Event] = {}
        self.requests = 0
        self.hits = 0
        self.inflight_waits = 0
        self.network_fetches = 0

        # P1-B (YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md Phase 3):
        # request-scoped SEARCH-QUERY dedup, same object/instance as
        # the URL fetch cache above — deliberately NOT a parallel
        # subsystem. Two different claims (or a claim's search vs. the
        # initial/refutation search) can independently generate the
        # EXACT SAME search-engine query text; without this, that pays
        # for the DDGS network call twice for identical results, on
        # top of whatever URL-level fetch dedup already happens
        # downstream. Keyed by NORMALIZED query text only (never fuzzy/
        # semantic similarity) — see normalize_query()'s docstring for
        # why exact-text identity cannot merge a support query with a
        # contradiction query.
        self._query_results: Dict[str, tuple] = {}
        self._query_events: Dict[str, threading.Event] = {}
        self.query_requests = 0
        self.query_hits = 0

    @staticmethod
    def canonicalize(url: str) -> str:
        """
        Minimal, conservative canonicalization: lowercase scheme/host,
        drop the fragment. Deliberately does NOT strip or reorder query
        params — the task's own instruction warns that a query string
        (e.g. Nature's ?error=cookies_not_supported&code=...) may or
        may not indicate the same content; blind stripping is unproven
        and not done here.
        """
        try:
            parsed = urlsplit(url)
            return urlunsplit((
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                "",
            ))
        except Exception:
            return url

    @staticmethod
    def normalize_query(query: str) -> str:
        """
        Conservative normalization for exact-duplicate detection ONLY:
        collapse whitespace, casefold. Deliberately does NOT strip
        punctuation beyond whitespace collapsing, and deliberately does
        NOT do any semantic/fuzzy matching — the task's own instruction
        is explicit that a near-duplicate (paraphrase) query must only
        ever be MEASURED, never merged, and that dedup must never
        conflate a support-intent query with a contradiction-intent
        query just because their text looks similar. Normalizing this
        conservatively means two queries only ever collapse to the same
        key when they are, character-for-character (modulo case and
        whitespace run-length), the SAME search — which by construction
        cannot cross a support/counter query boundary generated from
        genuinely different prompts, since those are never designed to
        produce identical text.
        """
        return " ".join((query or "").split()).casefold()

    def get_or_search(self, query: str, search_fn):
        """
        Same request-scoped, thread-safe, in-flight-coalescing pattern
        as get_or_fetch() above, applied to search-engine queries
        instead of URL fetches. search_fn: callable(query) -> (urls,
        rejected). Called AT MOST ONCE per normalized query text for
        this cache instance's lifetime (one user request).
        """
        key = self.normalize_query(query)

        if not key:
            return search_fn(query)

        with self._lock:
            self.query_requests += 1

            if key in self._query_results:
                self.query_hits += 1
                print(f"  [scraper] query cache HIT: {query[:80]!r}")
                return self._query_results[key]

            existing_event = self._query_events.get(key)

            if existing_event is None:
                event = threading.Event()
                self._query_events[key] = event
                is_owner = True
            else:
                event = existing_event
                is_owner = False

        if not is_owner:
            event.wait(timeout=FETCH_TIMEOUT + 10)

            with self._lock:
                if key in self._query_results:
                    self.query_hits += 1
                    print(f"  [scraper] query cache HIT (in-flight): {query[:80]!r}")
                    return self._query_results[key]
            # Owner never populated a result — fetch it ourselves
            # rather than return nothing (same fallback as
            # get_or_fetch()).

        result = None
        try:
            result = search_fn(query)
        finally:
            with self._lock:
                self._query_results[key] = result
                event.set()

        return result

    def get_or_fetch(self, url: str, transport: str, fetch_fn):
        """
        fetch_fn: callable(url) -> (result, reason). Called AT MOST
        ONCE per (transport, canonical_url) for this cache instance's
        lifetime — transport is part of the key because direct and
        proxy fetches of the same URL can legitimately have different
        outcomes (e.g. direct blocked by Cloudflare, proxy succeeds).
        """
        key = f"{transport}:{self.canonicalize(url)}"

        with self._lock:
            self.requests += 1

            if key in self._results:
                self.hits += 1
                # P1-B (YANDI_AGENT_RETRIEVAL_PERFORMANCE_AUDIT.md
                # Phase 4/6): explicit, provable HIT marker. Without
                # this, a URL fetched by N different claim-workers
                # prints N "proxy OK"/"OK" lines with identical
                # payload size regardless of whether 1 or N real
                # network fetches happened (every caller prints its
                # own copy of the SAME cached result) — making the
                # log alone look like duplicate fetches even when the
                # cache is working correctly. This line makes the
                # distinction directly observable instead of requiring
                # code-tracing to prove it.
                print(f"  [scraper] fetch cache HIT ({transport}): {url[:70]}")
                return self._results[key]

            existing_event = self._events.get(key)

            if existing_event is None:
                event = threading.Event()
                self._events[key] = event
                is_owner = True
            else:
                event = existing_event
                is_owner = False

        if not is_owner:
            self.inflight_waits += 1
            event.wait(timeout=FETCH_TIMEOUT + 10)

            with self._lock:
                if key in self._results:
                    self.hits += 1
                    print(f"  [scraper] fetch cache HIT (in-flight, {transport}): {url[:70]}")
                    return self._results[key]
            # Owner never populated a result (crashed before the
            # finally block below, which should not happen, but this
            # is the safe fallback) -- fetch it ourselves rather than
            # return nothing.

        # result must be bound before the try so that, if fetch_fn
        # raises, the finally block below can still record a result
        # (None) and set the event -- otherwise this UnboundLocalError
        # would itself replace/mask the original exception AND leave
        # any other thread waiting on `event` blocked for the full
        # FETCH_TIMEOUT+10 instead of failing fast. The exception
        # itself still propagates to this (owner) caller as before;
        # only the bookkeeping is now crash-safe.
        result = None
        try:
            self.network_fetches += 1
            result = fetch_fn(url)
        finally:
            with self._lock:
                self._results[key] = result
                event.set()

        return result

    def summary(self) -> Dict[str, Any]:
        saved = self.hits
        total = self.requests

        return {
            "requests": total,
            "unique": self.network_fetches,
            "hits": self.hits,
            "inflight_waits": self.inflight_waits,
            "network_fetches": self.network_fetches,
            "saved": saved,
            "hit_ratio": (saved / total) if total else 0.0,
            "query_requests": self.query_requests,
            "query_hits": self.query_hits,
            "query_searches": self.query_requests - self.query_hits,
        }


def _load_proxy_url() -> Optional[str]:
    try:
        raw = PROXY_FILE.read_text(
            encoding="utf-8"
        ).strip()

        if not raw:
            return None

        parts = raw.split(":", 3)

        if len(parts) != 4:
            print(
                "[scraper] proxy config invalid: "
                "expected host:port:user:password"
            )
            return None

        host, port, user, password = [
            part.strip()
            for part in parts
        ]

        if not all((host, port, user, password)):
            return None

        user = quote(user, safe="")
        password = quote(password, safe="")

        return (
            f"http://{user}:{password}"
            f"@{host}:{port}"
        )

    except FileNotFoundError:
        return None

    except Exception as exc:
        print(
            f"[scraper] proxy config error: "
            f"{type(exc).__name__}"
        )
        return None


def _build_proxy_session() -> Optional[_requests.Session]:
    proxy_url = _load_proxy_url()

    if not proxy_url:
        return None

    session = _requests.Session()
    session.trust_env = False

    session.proxies.update({
        "http": proxy_url,
        "https": proxy_url,
    })

    return session

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}

MAX_RESULTS = 5

# Search discovery должен смотреть глубже первых нескольких
# результатов поисковика.
#
# MAX_RESULTS — сколько источников в итоге нужно pipeline.
# DISCOVERY_RESULTS — сколько кандидатов разрешено рассмотреть
# до Source Quality / relevance ranking.
DISCOVERY_RESULTS = 20

FETCH_TIMEOUT = 15
MAX_CONTENT_LENGTH = 5000

# P4 (web budget 3+3): hard, per-side NETWORK FETCH budget for
# scrape_budgeted() (claim-specific PASS2 retrieval). This is a fetch
# CEILING, not a target — "attempt at most 3 direct-side and 3
# counter-side candidates," never "keep searching until 3 independent
# sources are found." Independence (source_cluster) is determined
# AFTER fetch, over whatever was actually fetched this cycle; a future
# verification-memory cycle, not this one, is responsible for
# broadening coverage if these 6 attempts turn out to share roots.
PASS2_DIRECT_BUDGET = 3
PASS2_COUNTER_BUDGET = 3

# Причины отклонения источников
REJECT_REASONS = {
    "fetch_failed": "не удалось загрузить страницу",
    "timeout": "таймаут при загрузке",
    "http_error": "HTTP ошибка",
    "cloudflare_challenge": "Cloudflare challenge",
    "browser_required": "требуется браузерный transport",
    "no_content": "страница не содержит текста",
    "no_keywords": "нет ключевых слов из запроса",
    "video_domain": "видео-домен (youtube/vk/rutube)",
    "translation_page": "страница перевода (translate.yandex)",
    "duplicate": "дубликат уже полученного URL",
    "seo_spam": "SEO-мусор, низкое качество",
    "irrelevant": "нерелевантно запросу",
    "low_quality": "низкое качество контента",
    "proxy_unavailable": "proxy не настроен (proxy.txt отсутствует/битый)",
}


def _is_cloudflare_challenge(resp) -> bool:
    """
    Определяет Cloudflare browser/JS challenge.

    Это транспортное ограничение, а не плохой источник.
    """

    if resp is None:
        return False

    try:
        server = (
            resp.headers.get("server", "")
            or ""
        ).lower()

        cf_ray = (
            resp.headers.get("cf-ray", "")
            or ""
        ).strip()

        body = (
            resp.text
            or ""
        )[:12000].lower()

        signals = 0

        if "cloudflare" in server:
            signals += 1

        if cf_ray:
            signals += 1

        if "just a moment" in body:
            signals += 1

        if "challenges.cloudflare.com" in body:
            signals += 1

        if "cf-chl-" in body:
            signals += 1

        if "challenge-platform" in body:
            signals += 1

        # Не классифицируем любой Cloudflare 403 как challenge.
        # Нужны хотя бы два независимых признака.
        return signals >= 2

    except Exception:
        return False


def _clean_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return text.strip()


def _extract_text_from_html(html: str) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        
        main = soup.find("main") or soup.find("article") or soup.find("div", class_="content")
        if main:
            text = main.get_text(separator=" ", strip=True)
        else:
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text(strip=True) for p in paragraphs[:20])
        
        if not text or len(text) < 50:
            body = soup.find("body")
            if body:
                text = body.get_text(separator=" ", strip=True)
        
        return _clean_text(text[:MAX_CONTENT_LENGTH])
    except Exception:
        return ""


def _fetch_url(url: str, query: str = "") -> tuple[Optional[dict], str]:
    """
    Загрузить страницу.
    Возвращает (результат, причина_отклонения)
    """
    try:
        resp = _session.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers=HEADERS,
            allow_redirects=True,
        )

        # Cloudflare challenge нужно распознать ДО raise_for_status(),
        # иначе он потеряется как обычный http_403.
        if _is_cloudflare_challenge(resp):
            record_transport_result(
                url,
                "direct",
                "cloudflare_challenge",
            )
            return None, "cloudflare_challenge"

        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")
        if "text/html" not in content_type:
            return None, "no_content"
        
        text = _extract_text_from_html(resp.text)
        if not text or len(text) < 50:
            return None, "no_content"
        
        # Проверка релевантности по ключевым словам
        if query and len(query) > 5:
            keywords = [w for w in query.lower().split() if len(w) > 3]
            if keywords:
                text_lower = text.lower()
                matches = sum(1 for kw in keywords if kw in text_lower)
                if matches == 0:
                    return None, "no_keywords"
        
        soup = BeautifulSoup(resp.text, "html.parser")
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True)[:200] if title_tag else url

        # P4 (permanent stoplist): direct success was never recorded
        # into transport_memory at all (only failures were) — fixed
        # while touching this code, since accurate direct_status is
        # now load-bearing for is_stoplisted()/stoplist_url().
        record_transport_result(url, "direct", "ok")

        return {"url": url, "title": title, "text": text, "content": text}, ""
    except _requests.Timeout:
        record_transport_result(
            url,
            "direct",
            "timeout",
        )
        return None, "timeout"

    except _requests.HTTPError as exc:
        status = getattr(
            getattr(exc, "response", None),
            "status_code",
            None,
        )

        if status:
            reason = f"http_{status}"
            record_transport_result(
                url,
                "direct",
                reason,
            )
            return None, reason

        record_transport_result(
            url,
            "direct",
            "http_error",
        )
        return None, "http_error"

    except Exception:
        record_transport_result(
            url,
            "direct",
            "fetch_failed",
        )
        return None, "fetch_failed"



def _fetch_url_proxy(
    url: str,
    query: str = "",
) -> tuple[Optional[dict], str]:
    """
    Повторная загрузка URL через proxy.txt.

    Используется только после неудачного direct fetch.
    """

    session = _build_proxy_session()

    if session is None:
        return None, "proxy_unavailable"

    try:
        resp = session.get(
            url,
            timeout=FETCH_TIMEOUT,
            headers=HEADERS,
            allow_redirects=True,
        )

        # Proxy может сменить IP, но Cloudflare challenge
        # всё равно остаётся browser-required transport case.
        if _is_cloudflare_challenge(resp):
            record_transport_result(
                url,
                "proxy",
                "cloudflare_challenge",
            )
            return None, "cloudflare_challenge"

        resp.raise_for_status()

        content_type = resp.headers.get(
            "content-type",
            "",
        )

        if "text/html" not in content_type:
            return None, "no_content"

        text = _extract_text_from_html(
            resp.text
        )

        if not text or len(text) < 50:
            return None, "no_content"

        if query and len(query) > 5:
            keywords = [
                w
                for w in query.lower().split()
                if len(w) > 3
            ]

            if keywords:
                text_lower = text.lower()

                matches = sum(
                    1
                    for kw in keywords
                    if kw in text_lower
                )

                if matches == 0:
                    return None, "no_keywords"

        soup = BeautifulSoup(
            resp.text,
            "html.parser",
        )

        title_tag = soup.find("title")

        title = (
            title_tag.get_text(strip=True)[:200]
            if title_tag
            else url
        )

        # P4 (permanent stoplist): this function is only ever called
        # for the PROXY transport — recording "direct","ok" here was a
        # pre-existing mislabeling bug (this fetch used proxy, not
        # direct; direct may well have just failed, which is WHY proxy
        # was tried at all). Fixed while touching this code for the
        # stoplist feature, since accurate per-transport status is now
        # load-bearing for is_stoplisted()/stoplist_url().
        record_transport_result(
            url,
            "proxy",
            "ok",
        )

        return {
            "url": url,
            "title": title,
            "text": text,
            "content": text,
        }, ""

    except _requests.Timeout:
        record_transport_result(
            url,
            "proxy",
            "proxy_timeout",
        )
        return None, "proxy_timeout"

    except _requests.HTTPError as exc:
        status = getattr(
            getattr(exc, "response", None),
            "status_code",
            None,
        )

        if status:
            reason = f"proxy_http_{status}"
            record_transport_result(
                url,
                "proxy",
                reason,
            )
            return None, reason

        record_transport_result(
            url,
            "proxy",
            "proxy_http_error",
        )
        return None, "proxy_http_error"

    except Exception:
        record_transport_result(
            url,
            "proxy",
            "proxy_fetch_failed",
        )
        return None, "proxy_fetch_failed"

    finally:
        try:
            session.close()
        except Exception:
            pass


# HTTP codes where switching transport/IP is potentially useful (P4:
# hoisted from a closure inside scrape() to module level so
# _fetch_url_with_proxy_fallback can share the exact same retry
# decision — same set, same semantics, not duplicated).
_PROXY_RETRY_HTTP_CODES = {
    401, 403, 407, 408, 429, 451, 500, 502, 503, 504,
}


def _should_proxy_retry(reason: str) -> bool:
    if reason in {
        "timeout",
        "fetch_failed",
        "http_error",
        "cloudflare_challenge",
    }:
        return True

    if reason.startswith("http_"):
        try:
            code = int(reason.split("_", 1)[1])
        except Exception:
            return False

        return code in _PROXY_RETRY_HTTP_CODES

    return False


def _fetch_url_with_proxy_fallback(
    url: str,
    query: str,
    fetch_cache: "SharedFetchCache",
) -> tuple[Optional[dict], str, str, Optional[tuple]]:
    """
    P4 (permanent stoplist): ONE lifecycle per URL — direct, then
    IMMEDIATELY proxy if direct fails with a transport-level (proxy-
    retryable) reason — instead of the old two-PHASE design (wait for
    the entire direct batch, only then start a separate proxy-retry
    pass). Other URLs' own lifecycles proceed independently/in
    parallel via the same worker pool (unchanged) — this only changes
    the SEQUENCING within one URL's own attempt, not overall
    concurrency.

    Returns (result_or_None, reason, transport_used,
    stoplist_reasons_or_None). stoplist_reasons is only non-None when
    BOTH direct AND proxy were genuinely attempted and BOTH failed
    with a transport-level reason — never for content-level rejects
    (no_content/no_keywords/etc — those say nothing about whether the
    URL is reachable) and never when proxy was unavailable/not
    configured (can't claim "even via proxy failed" if proxy was never
    tried).
    """
    result, reason = fetch_cache.get_or_fetch(
        url, "direct", lambda u: _fetch_url(u, query)
    )

    if result:
        return result, "", "direct", None

    if not _should_proxy_retry(reason):
        return None, reason, "direct", None

    if not _load_proxy_url():
        return None, "proxy_unavailable", "direct", None

    proxy_result, proxy_reason = fetch_cache.get_or_fetch(
        url, "proxy", lambda u: _fetch_url_proxy(u, query)
    )

    if proxy_result:
        return proxy_result, "", "proxy", None

    return None, proxy_reason, "proxy", (reason, proxy_reason)


def _search_with_ddgs(
    query: str,
    max_results: int = MAX_RESULTS,
    fetch_cache: "Optional[SharedFetchCache]" = None,
) -> tuple[List[str], List[Dict[str, str]]]:
    """
    Поиск через DuckDuckGo.
    Возвращает (список URL, список отклонённых с причинами)

    fetch_cache: P1-B — when provided, the actual DDGS call is
    deduped through SharedFetchCache.get_or_search(), request-scoped
    (see that method's docstring). None (default) preserves the exact
    prior behavior (always searches) for any other/direct caller.
    """
    if fetch_cache is not None:
        return fetch_cache.get_or_search(
            query,
            lambda q: _search_with_ddgs(q, max_results=max_results, fetch_cache=None),
        )

    if not HAS_DDGS:
        return [], []

    urls = []
    rejected = []
    # Один домен может содержать несколько независимых
    # качественных документов.
    #
    # Поэтому discovery не делает жёсткий "1 URL = 1 domain".
    # Финальная domain diversity применяется позже.
    domain_counts = {}
    MAX_DISCOVERY_URLS_PER_DOMAIN = 3
    
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        
        with DDGS() as ddgs:
            # Не ограничиваем discovery первыми 2 * max_results.
            # Search engine rank != evidence quality.
            discovery_limit = max(
                DISCOVERY_RESULTS,
                max_results * 2,
            )

            for r in ddgs.text(
                query,
                max_results=discovery_limit,
            ):
                url = r.get("href", "")
                if not url or not url.startswith("http"):
                    continue
                
                # Проверяем видео-домены
                if any(domain in url for domain in ["youtube.com", "vk.com", "rutube.ru"]):
                    rejected.append({"url": url, "reason": "video_domain"})
                    continue
                
                # Проверяем страницы перевода
                if "translate.yandex" in url or "translate.google" in url:
                    rejected.append({"url": url, "reason": "translation_page"})
                    continue
                
                # Ограничиваем количество кандидатов одного домена,
                # но НЕ выбрасываем все URL после первого.
                #
                # Разные страницы одного сайта могут иметь совершенно
                # разную evidence value.
                domain = (
                    url.split("/")[2]
                    if "/" in url
                    else url
                ).lower()

                domain_count = domain_counts.get(
                    domain,
                    0,
                )

                if (
                    domain_count
                    >= MAX_DISCOVERY_URLS_PER_DOMAIN
                ):
                    rejected.append({
                        "url": url,
                        "reason": "domain_candidate_limit",
                    })
                    continue

                domain_counts[domain] = (
                    domain_count + 1
                )

                urls.append(url)

                # Discovery имеет собственный предел.
                # Финальный max_results применяется позже,
                # после оценки кандидатов.
                if len(urls) >= discovery_limit:
                    break
    except Exception as e:
        print(f"  [scraper] DDGS error: {e}")
    
    return urls, rejected


def _budgeted_side_candidates(
    queries: List[str],
    budget: int,
    fetch_cache: "SharedFetchCache",
    side: str,
    processed_urls_canonical: Optional[set] = None,
) -> tuple[List[str], int, int, int]:
    """
    ONE side (e.g. direct/counter, or main/counter) of a budgeted
    discovery -> exact-dedup -> stoplist-exclusion -> processed-
    exclusion -> budget-cap funnel (P4 §6/§7/§12, P6 §6). Accepts a
    LIST of queries because a "side" is not always one query — PASS2
    (scrape_budgeted) has exactly one query per side, but stage 6
    (scrape_budgeted_side) can have up to 3 alternative main-query
    formulations or 2-3 refutation queries; all of them feed the SAME
    candidate pool for that side before dedup/stoplist/processed/cap,
    same as the old scrape()'s multi-query discovery loop did for a
    single undifferentiated pool.

    processed_urls_canonical: pre-canonicalized (SharedFetchCache.
    canonicalize) set of URLs already verified for THIS claim in a
    prior cycle (Этап 4 / P6) — None or empty for stage 6, which has
    no claim/content_hash yet (P6 §8, deliberately not touched). A
    processed URL is excluded BEFORE the budget cap, same tier as
    stoplist — it never occupies one of the 3 slots. Shared identically
    across BOTH sides (P6 §10: a URL processed as direct is just as
    processed if it would otherwise be discovered again as counter —
    there is no per-side processed state).

    Returns (candidate_urls_capped_at_budget, discovered_count,
    stoplist_excluded_count, processed_excluded_count) — NO fetch
    happens here, this is candidate SELECTION only.

    Independent of the other side by construction — this function
    never sees or is affected by the other side's query/results, which
    is what makes "counter saturation never blocks direct/main and
    vice versa" (P4 §4) true structurally, not just by convention.
    """
    discovered: List[str] = []

    for query in queries or []:
        if not query or len(query) < 3:
            continue
        urls, _rejected = _search_with_ddgs(
            query, max_results=budget, fetch_cache=fetch_cache,
        )
        discovered.extend(urls)

    if not discovered:
        return [], 0, 0, 0

    # Exact URL dedup (P4 §6) — canonicalized, same normalization
    # SharedFetchCache itself uses, not a second dedup concept.
    seen = set()
    deduped = []
    for url in discovered:
        key = SharedFetchCache.canonicalize(url)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(url)

    # Stoplist exclusion BEFORE budget (P4 §7) — a stoplisted URL
    # never occupies one of the 3 slots; it simply isn't a valid
    # network candidate at all.
    stoplist_excluded = 0
    after_stoplist = []
    for url in deduped:
        if is_stoplisted(url):
            stoplist_excluded += 1
            print(f"  [scraper][{side}] stoplist SKIP (pre-budget): {url[:60]}...")
            continue
        after_stoplist.append(url)

    # Processed-for-this-claim exclusion (P6 §6/§12) — SAME tier as
    # stoplist: a URL already verified for this exact claim (via
    # persistent Verification Memory, Этап 3) is not a new candidate
    # either. Distinct storage/semantics from stoplist (P6 §12 — never
    # written into transport_memory, never global) but the same
    # position in the funnel: excluded before the 3-slot cap is spent,
    # not after.
    processed_excluded = 0
    eligible = []
    for url in after_stoplist:
        if processed_urls_canonical and SharedFetchCache.canonicalize(url) in processed_urls_canonical:
            processed_excluded += 1
            print(f"  [scraper][{side}] processed SKIP (pre-budget): {url[:60]}...")
            continue
        eligible.append(url)

    # Hard cap (P4 §1/§5, P6 §11) — no top-up if some of these later
    # turn out to be reprints of each other or to fail; that is next
    # cycle's job, not this one's.
    return eligible[:budget], len(discovered), stoplist_excluded, processed_excluded


def _fetch_budgeted_tagged_urls(
    tagged_urls: List[tuple],
    fetch_cache: "SharedFetchCache",
) -> tuple:
    """
    Shared fetch lifecycle for scrape_budgeted() and
    scrape_budgeted_side() — takes an already-budgeted, already-tagged
    (url, origin) list and fetches each via the SAME interleaved
    direct-then-proxy-immediately lifecycle (_fetch_url_with_proxy_
    fallback) as scrape(), stoplisting genuine transport failures the
    same way. `origin` is a caller-defined label ("direct"/"counter" or
    "main"/"counter") used only for per-origin fetched counts and log
    tagging — this function doesn't interpret it.

    Returns (snippets, total_chars, rejected, fetched_by_origin,
    proxy_fetched) — proxy_fetched is the count of successes among
    those that specifically needed the proxy transport (direct having
    failed first), for the [WebBudget]/final-summary proxy_attempts
    metric.
    """
    snippets: List[WebSnippet] = []
    total_chars = 0
    rejected: List[Dict[str, str]] = []
    fetched_by_origin: Dict[str, int] = {}
    proxy_fetched = 0

    if not tagged_urls:
        return snippets, total_chars, rejected, fetched_by_origin, proxy_fetched

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=min(5, len(tagged_urls))
    )

    futures = {
        executor.submit(
            _fetch_url_with_proxy_fallback, url, "", fetch_cache,
        ): (url, origin)
        for url, origin in tagged_urls
    }

    done, not_done = concurrent.futures.wait(
        futures, timeout=(FETCH_TIMEOUT * 2) + 5,
    )

    for future in done:
        url, origin = futures[future]

        try:
            result_dict, reason, transport_used, stoplist_reasons = future.result()

            if result_dict:
                snippets.append(WebSnippet(
                    url=result_dict["url"],
                    title=result_dict["title"],
                    content=result_dict["content"],
                    text=result_dict["text"],
                    relevance=0.7,
                    origin=origin,
                ))
                total_chars += len(result_dict["text"])
                fetched_by_origin[origin] = fetched_by_origin.get(origin, 0) + 1

                if transport_used == "proxy":
                    proxy_fetched += 1
                    print(f"  [scraper][{origin}] proxy OK: {url[:60]}... ({len(result_dict['text'])} chars)")
                else:
                    print(f"  [scraper][{origin}] OK: {url[:60]}... ({len(result_dict['text'])} chars)")
                continue

            if stoplist_reasons is not None:
                direct_reason, proxy_reason = stoplist_reasons
                stoplist_url(url, direct_reason, proxy_reason)
                print(f"  [scraper][{origin}] STOPLISTED (direct={direct_reason} proxy={proxy_reason}): {url[:60]}...")
            else:
                print(f"  [scraper][{origin}] reject: {url[:60]}... ({reason or 'unknown'})")

            rejected.append({"url": url, "reason": reason or "unknown"})

        except Exception as e:
            rejected.append({"url": url, "reason": "fetch_failed"})
            print(f"  [scraper][{origin}] error: {url[:60]}... ({e})")

    for future in not_done:
        url, origin = futures[future]
        future.cancel()
        rejected.append({"url": url, "reason": "timeout"})
        print(f"  [scraper][{origin}] TIMEOUT (direct+proxy lifecycle): {url[:60]}...")

    executor.shutdown(wait=False, cancel_futures=True)

    return snippets, total_chars, rejected, fetched_by_origin, proxy_fetched


def scrape_budgeted(
    direct_query: str,
    counter_query: str,
    direct_budget: int = PASS2_DIRECT_BUDGET,
    counter_budget: int = PASS2_COUNTER_BUDGET,
    fetch_cache: "Optional[SharedFetchCache]" = None,
    claim_id: str = "",
    content_hash: str = "",
) -> WebScrapeResult:
    """
    P4 (web budget 3+3), claim-specific (PASS2) retrieval. Replaces
    the old single scrape(web_query_with_2_queries, max_results=
    CLAIM_RETRIEVAL_POOL=10) call, which merged direct+counter
    discovery into one undifferentiated pool (losing query origin) and
    could select up to 10 URLs per claim with no independent per-side
    cap — the primary source of the "up to 8 claims x 10 URLs" breadth
    this task exists to bound.

    Two INDEPENDENT funnels (P4 §2/§12), each: discovery -> exact
    dedup -> stoplist exclusion -> processed exclusion -> hard budget
    cap -> fetch. Neither side's exhaustion affects the other (P4 §4 —
    fixes a real bug: the old scrape()'s shared discovery loop could
    break after the FIRST query alone hit DISCOVERY_RESULTS, silently
    skipping the second query's search entirely; each side here gets
    its own _search_with_ddgs() call, unaffected by the other).

    content_hash (P6 / Этап 4 §7): identity token for the "processed"
    exclusion — deliberately just the token, not the claim dict itself
    (§7: "scraper должен знать только минимально необходимый identity
    token", no coupling to claims_data internals). Empty string (the
    default) disables processed exclusion entirely (both counts read
    as 0) — used by any caller that doesn't have a claim yet.

    The processed set (agent.verification_memory.get_historical_web_urls)
    is fetched ONCE and shared identically across BOTH sides (P6 §10 —
    a URL already verified for this claim is excluded regardless of
    which side would have rediscovered it).

    Reuses (not reimplements): _search_with_ddgs, SharedFetchCache
    (dedup + canonicalize), is_stoplisted/stoplist_url,
    _fetch_url_with_proxy_fallback (the same interleaved direct-then-
    proxy-immediately lifecycle from the stoplist patch — one URL,
    direct+proxy together, still costs exactly ONE budget slot, per
    P4 §8), agent.verification_memory (Этап 3's persistent evidence,
    not a new processed-state store — P6 §16).

    No relevance/quality filtering or ranking here — the caller
    (retrieve_claim_evidence) already runs its own subject-anchor/
    semantic-relevance/source-quality gates on whatever this returns;
    duplicating that here would be a second, competing gate, not a
    minimal patch.
    """
    if fetch_cache is None:
        fetch_cache = SharedFetchCache()

    processed_urls: set = set()
    historical_occurrences = 0
    if content_hash:
        from agent.verification_memory import get_historical_web_urls
        processed_urls, historical_occurrences = get_historical_web_urls(content_hash)

    processed_urls_canonical = {SharedFetchCache.canonicalize(u) for u in processed_urls}

    print(
        f"[ProcessedSources] claim_id={claim_id or 'unknown'} "
        f"content_hash={(content_hash or '-')[:12]} "
        f"historical_occurrences={historical_occurrences} "
        f"processed_urls={len(processed_urls)}"
    )

    direct_candidates, direct_discovered, direct_stoplist_excluded, direct_processed_excluded = (
        _budgeted_side_candidates([direct_query], direct_budget, fetch_cache, "direct", processed_urls_canonical)
    )
    counter_candidates, counter_discovered, counter_stoplist_excluded, counter_processed_excluded = (
        _budgeted_side_candidates([counter_query], counter_budget, fetch_cache, "counter", processed_urls_canonical)
    )

    print(
        f"[WebBudget] scope=claim claim_id={claim_id or 'unknown'} "
        f"direct_candidates={direct_discovered} "
        f"direct_stoplist_excluded={direct_stoplist_excluded} "
        f"direct_processed_excluded={direct_processed_excluded} "
        f"direct_selected={len(direct_candidates)} "
        f"counter_candidates={counter_discovered} "
        f"counter_stoplist_excluded={counter_stoplist_excluded} "
        f"counter_processed_excluded={counter_processed_excluded} "
        f"counter_selected={len(counter_candidates)}"
    )

    tagged_urls = (
        [(u, "direct") for u in direct_candidates]
        + [(u, "counter") for u in counter_candidates]
    )

    if not tagged_urls:
        result = WebScrapeResult(snippets=[], total_chars=0, urls=[])
        setattr(result, "_rejected", [])
        setattr(result, "_total_found", 0)
        return result

    snippets, total_chars, rejected, fetched_by_origin, proxy_fetched = _fetch_budgeted_tagged_urls(
        tagged_urls, fetch_cache,
    )

    print(
        f"[WebBudget] scope=claim claim_id={claim_id or 'unknown'} "
        f"direct_fetched={fetched_by_origin.get('direct', 0)} "
        f"counter_fetched={fetched_by_origin.get('counter', 0)} "
        f"proxy_fetched={proxy_fetched} "
        f"processed_urls_known={len(processed_urls)} "
        f"processed_candidates_excluded={direct_processed_excluded + counter_processed_excluded} "
        f"new_urls_selected={len(direct_candidates) + len(counter_candidates)}"
    )

    result = WebScrapeResult(
        snippets=snippets,
        total_chars=total_chars,
        urls=[s.url for s in snippets],
    )
    setattr(result, "_rejected", rejected)
    setattr(result, "_total_found", len(tagged_urls))

    fc = fetch_cache.summary()
    print(
        f"[Shared Fetch Cache] (running) "
        f"requests={fc['requests']} "
        f"network_fetches={fc['network_fetches']} "
        f"saved={fc['saved']}"
    )

    return result


# P4 (web budget 3+3): stage-6 (initial, whole-question) side budget.
STAGE6_MAIN_BUDGET = 3
STAGE6_COUNTER_BUDGET = 3


def scrape_budgeted_side(
    queries: List[str],
    budget: int,
    fetch_cache: "Optional[SharedFetchCache]" = None,
    side: str = "main",
    scope: str = "initial",
) -> WebScrapeResult:
    """
    P4 (web budget 3+3), question-scope (stage 6) retrieval for ONE
    side (main or counter/refutation) of the whole-question web step.

    Same discovery -> exact dedup -> stoplist exclusion -> hard budget
    cap -> fetch funnel as scrape_budgeted()'s per-side helper (both
    reuse _budgeted_side_candidates / _fetch_budgeted_tagged_urls),
    exposed standalone because stage 6's main (agent/orchestrator/
    pipeline.py) and counter/refutation (agent/orchestrator/
    synthesis.py) calls live in two different pipeline stages, not
    adjacent calls that could share one function call without a real
    cross-file restructuring of when refutation runs relative to main
    — out of scope for this patch (P4 §3: stage 6 gets the SAME 3+3
    philosophy, not a merged call site).

    Each call is independent by construction, same guarantee as
    scrape_budgeted()'s two sides (P4 §4) — one side's exhaustion
    cannot affect the other because they are, structurally, two
    entirely separate invocations (this was already true for stage 6
    even before this patch, per the INSPECT finding that stage 6's
    main/refutation were already two separate scrape() calls — the
    counter-starvation bug only existed inside PASS2's single shared
    scrape() call).
    """
    if fetch_cache is None:
        fetch_cache = SharedFetchCache()

    # P6 (Этап 4 §8): no processed_urls_canonical here — stage 6 runs
    # BEFORE claim extraction, so there is no content_hash to scope a
    # processed set by. Deliberately not touched (see this function's
    # own docstring above and the Этап 4 INSPECT report — no invented
    # question-identity, no symmetry-for-its-own-sake).
    candidates, discovered, stoplist_excluded, _processed_excluded_unused = _budgeted_side_candidates(
        queries, budget, fetch_cache, side,
    )

    print(
        f"[WebBudget] scope={scope} side={side} "
        f"{side}_candidates={discovered} "
        f"{side}_stoplist_excluded={stoplist_excluded} "
        f"{side}_selected={len(candidates)}"
    )

    tagged_urls = [(u, side) for u in candidates]

    if not tagged_urls:
        result = WebScrapeResult(snippets=[], total_chars=0, urls=[])
        setattr(result, "_rejected", [])
        setattr(result, "_total_found", 0)
        return result

    snippets, total_chars, rejected, fetched_by_origin, proxy_fetched = _fetch_budgeted_tagged_urls(
        tagged_urls, fetch_cache,
    )

    print(
        f"[WebBudget] scope={scope} side={side} "
        f"{side}_fetched={fetched_by_origin.get(side, 0)} "
        f"proxy_fetched={proxy_fetched}"
    )

    result = WebScrapeResult(
        snippets=snippets,
        total_chars=total_chars,
        urls=[s.url for s in snippets],
    )
    setattr(result, "_rejected", rejected)
    setattr(result, "_total_found", len(tagged_urls))

    fc = fetch_cache.summary()
    print(
        f"[Shared Fetch Cache] (running) "
        f"requests={fc['requests']} "
        f"network_fetches={fc['network_fetches']} "
        f"saved={fc['saved']}"
    )

    return result


def scrape(
    web_query: WebQueryResult,
    max_results: int = MAX_RESULTS,
    domain_diversity: bool = True,
    fetch_cache: "Optional[SharedFetchCache]" = None,
) -> WebScrapeResult:
    if not web_query or not web_query.queries:
        return WebScrapeResult(snippets=[], total_chars=0, urls=[])

    # P0 (performance architecture pass): fetch_cache is request-scoped
    # and shared ACROSS claims by the caller (retrieve_for_claims) when
    # provided, so a URL discovered independently by two different
    # claims' searches is only physically downloaded once. When no
    # cache is passed in (other callers of scrape() not part of that
    # flow), a fresh one-off instance still dedupes duplicate URLs
    # within this single call — same code path either way.
    if fetch_cache is None:
        fetch_cache = SharedFetchCache()
    
    # 1. Собираем URL через DuckDuckGo
    all_urls = []
    all_rejected = []
    query_text = " ".join(web_query.queries)
    
    for query in web_query.queries[:3]:
        if not query or len(query) < 3:
            continue
        urls, rejected = _search_with_ddgs(query, max_results=max_results, fetch_cache=fetch_cache)
        all_urls.extend(urls)
        all_rejected.extend(rejected)

        # Discovery и final selection — разные этапы.
        # Не прекращаем поиск после первых max_results URL.
        if len(set(all_urls)) >= DISCOVERY_RESULTS:
            break
    
    # 2. Убираем URL-дубли, но НЕ режем до max_results.
    #
    # Сначала рассматриваем discovery pool,
    # затем выбираем лучшие источники.
    urls = list(dict.fromkeys(all_urls))[:DISCOVERY_RESULTS]

    # ========================================================
    # PERMANENT STOPLIST + TRANSPORT MEMORY ROUTING (P4)
    # ========================================================
    #
    # URL уже доказанно недоступен (direct И proxy оба провалились
    # ранее — stoplist_url(), или старое browser_required-состояние,
    # которое is_stoplisted() трактует так же) — не отправляем его в
    # fetch pool вообще, до любой сетевой попытки.
    stoplist_skipped = 0
    direct_urls = []

    for url in urls:
        if is_stoplisted(url):
            stoplist_skipped += 1

            print(
                f"  [scraper] stoplist SKIP: "
                f"{url[:60]}... "
                f"(permanent, transport_memory)"
            )
            continue

        direct_urls.append(url)

    urls = direct_urls

    if not urls:
        print("[scraper] No URLs found via DDGS")
        result = WebScrapeResult(
            snippets=[],
            total_chars=0,
            urls=[],
        )
        setattr(
            result,
            "_rejected",
            all_rejected,
        )
        setattr(
            result,
            "_total_found",
            len(all_urls) + len(all_rejected),
        )
        return result

    print(
        f"[scraper] discovery="
        f"{len(urls) + stoplist_skipped} URL, "
        f"eligible={len(urls)} "
        f"stoplisted={stoplist_skipped} "
        f"final={max_results}"
    )

    # 3. Парсим страницы с проверкой релевантности
    snippets = []
    total_chars = 0
    rejected = all_rejected.copy()
    rejected_count = 0

    # --------------------------------------------------------
    # 3. FETCH POOL — ONE LIFECYCLE PER URL (P4)
    # --------------------------------------------------------
    #
    # Direct, затем НЕМЕДЛЕННО proxy при retryable-провале direct —
    # внутри ОДНОЙ задачи на URL, не отдельной фазой после того, как
    # весь direct-батч завершится. Другие URL продолжают обрабатываться
    # независимо тем же пулом воркеров (concurrency не менялась).
    #
    # Таймаут пула увеличен вдвое относительно старого — задача теперь
    # может делать ДВЕ последовательные сетевые попытки (direct, затем
    # proxy), а не одну.
    #
    # Один зависший URL НЕ должен уничтожать весь search result.
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=5
    )

    futures = {
        executor.submit(
            _fetch_url_with_proxy_fallback,
            url,
            "",
            fetch_cache,
        ): url
        for url in urls
    }

    done, not_done = concurrent.futures.wait(
        futures,
        timeout=(FETCH_TIMEOUT * 2) + 5,
    )

    proxy_success = 0
    proxy_failed = 0
    stoplisted_now = 0

    for future in done:
        url = futures[future]

        try:
            result, reason, transport_used, stoplist_reasons = future.result()

            if result:
                snippet = WebSnippet(
                    url=result["url"],
                    title=result["title"],
                    content=result["content"],
                    text=result["text"],
                    relevance=0.7,
                )

                snippets.append(snippet)
                total_chars += len(result["text"])

                if transport_used == "proxy":
                    proxy_success += 1

                    print(
                        f"  [scraper] proxy OK: "
                        f"{url[:60]}... "
                        f"({len(result['text'])} chars)"
                    )
                else:
                    print(
                        f"  [scraper] OK: "
                        f"{url[:60]}... "
                        f"({len(result['text'])} chars)"
                    )

                continue

            reason = reason or "unknown"

            if stoplist_reasons is not None:
                direct_reason, proxy_reason = stoplist_reasons
                stoplist_url(url, direct_reason, proxy_reason)
                stoplisted_now += 1
                proxy_failed += 1
                rejected_count += 1

                rejected.append({"url": url, "reason": proxy_reason or "proxy_failed"})

                print(
                    f"  [scraper] STOPLISTED (direct={direct_reason} "
                    f"proxy={proxy_reason}): {url[:60]}..."
                )
                continue

            if transport_used == "proxy":
                # Proxy attempted but failed for a reason that doesn't
                # warrant a permanent ban (e.g. proxy_unavailable was
                # already filtered out earlier — this branch is
                # reachable only if _fetch_url_with_proxy_fallback's
                # own stoplist gate didn't fire, kept defensive).
                proxy_failed += 1
                rejected_count += 1
                rejected.append({"url": url, "reason": reason})

                print(
                    f"  [scraper] proxy FAIL: "
                    f"{url[:60]}... reason={reason}"
                )
                continue

            # Direct-only outcome: either a content-level reject
            # (no_content/no_keywords/etc — not proxy-retryable at
            # all) or proxy_unavailable (proxy not configured, so we
            # cannot claim "even via proxy failed").
            rejected_count += 1
            reason_text = REJECT_REASONS.get(reason, reason)
            rejected.append({"url": url, "reason": reason})

            print(
                f"  [scraper] reject: "
                f"{url[:60]}... ({reason_text})"
            )

        except Exception as e:
            rejected_count += 1

            rejected.append({
                "url": url,
                "reason": "fetch_failed",
            })

            print(
                f"  [scraper] error: "
                f"{url[:60]}... ({e})"
            )

    # --------------------------------------------------------
    # Незавершённые futures — ни direct, ни proxy не удалось
    # достоверно ЗАВЕРШИТЬ в отведённое время. НЕ заносим в stoplist:
    # мы не знаем, что оба transport'а реально провалились, только
    # что задача не успела. timeout != permanent failure proof.
    # --------------------------------------------------------
    for future in not_done:
        url = futures[future]

        future.cancel()

        rejected_count += 1
        rejected.append({"url": url, "reason": "timeout"})

        print(
            f"  [scraper] TIMEOUT (direct+proxy lifecycle): "
            f"{url[:60]}..."
        )

    # КРИТИЧНО:
    # не ждём завершения зависших workers.
    executor.shutdown(
        wait=False,
        cancel_futures=True,
    )

    if not_done:
        print(
            f"[scraper] partial fetch: "
            f"completed={len(done)} "
            f"unfinished={len(not_done)}"
        )
    else:
        print(
            f"[scraper] fetch complete: "
            f"completed={len(done)}"
        )

    print(
        f"[scraper] proxy summary: "
        f"ok={proxy_success} "
        f"failed={proxy_failed} "
        f"stoplisted_now={stoplisted_now} "
        f"stoplist_skipped={stoplist_skipped}"
    )

    # 4. Дополнительная lexical relevance-проверка.
    #
    # ВАЖНО:
    # несколько поисковых запросов являются АЛЬТЕРНАТИВНЫМИ
    # формулировками одной информационной потребности.
    #
    # Их нельзя склеивать и требовать, чтобы документ содержал
    # слова сразу из русского И английского вариантов.
    #
    # Документ проходит gate, если он достаточно соответствует
    # ХОТЯ БЫ ОДНОМУ query.
    if snippets and web_query.queries:
        query_keyword_sets = []

        for query in web_query.queries[:3]:
            keywords = [
                w
                for w in re.findall(
                    r"[a-zа-яё0-9]+",
                    query.lower(),
                )
                if len(w) > 3
            ]

            if keywords:
                query_keyword_sets.append(keywords)

        if query_keyword_sets:
            filtered = []

            for snippet in snippets:
                text = (
                    (snippet.content or "")
                    + " "
                    + (snippet.title or "")
                ).lower()

                best_ratio = 0.0
                best_matches = 0

                for keywords in query_keyword_sets:
                    matches = sum(
                        1
                        for kw in keywords
                        if kw in text
                    )

                    ratio = matches / max(1, len(keywords))

                    if ratio > best_ratio:
                        best_ratio = ratio
                        best_matches = matches

                # Нужен хотя бы один содержательный match
                # и минимум 20% слов ОДНОГО query.
                if best_matches >= 1 and best_ratio >= 0.20:
                    filtered.append(snippet)

                    print(
                        f"  [relevance] PASS "
                        f"ratio={best_ratio:.2f} "
                        f"url={snippet.url[:60]}"
                    )
                else:
                    rejected_count += 1
                    rejected.append({
                        "url": snippet.url,
                        "reason": "no_keywords",
                    })

                    print(
                        f"  [scraper] filtered: "
                        f"{snippet.url[:60]}... "
                        f"(best_ratio={best_ratio:.2f})"
                    )

            snippets = filtered

    # --------------------------------------------------------
    # 5. SOURCE QUALITY RANKING
    # --------------------------------------------------------
    #
    # Search engine rank != evidence quality.
    #
    # Source Quality здесь используется только для выбора
    # лучших кандидатов из discovery pool.
    #
    # НИЧЕГО не объявляется истинным и context-источники
    # не удаляются автоматически.
    if snippets:
        ranked_candidates = []

        role_priority = {
            "direct": 3,
            "secondary": 2,
            "context": 1,
            "internal": 0,
        }

        for snippet in snippets:
            text = snippet.text or snippet.content or ""

            quality = evaluate_source_quality(
                url=snippet.url,
                title=snippet.title,
                text=text,
                source_type="web",
            )

            ranked_candidates.append({
                "snippet": snippet,
                "quality": quality,
                "role_priority": role_priority.get(
                    quality.evidence_role,
                    1,
                ),
            })

            print(
                f"  [quality] "
                f"role={quality.evidence_role:<9} "
                f"class={quality.source_class:<18} "
                f"score={quality.quality_score:.3f} "
                f"url={snippet.url[:70]}"
            )

        # Сначала epistemic role, затем quality_score.
        ranked_candidates.sort(
            key=lambda item: (
                item["role_priority"],
                item["quality"].quality_score,
            ),
            reverse=True,
        )

        # ----------------------------------------------------
        # Domain diversity
        # ----------------------------------------------------
        #
        # Не позволяем одному домену забить весь final set.
        selected = []
        selected_domains = set()

        for item in ranked_candidates:
            snippet = item["snippet"]

            if domain_diversity:
                try:
                    domain = snippet.url.split("/")[2].lower()
                except Exception:
                    domain = snippet.url.lower()

                if domain in selected_domains:
                    continue

                selected_domains.add(domain)

            selected.append(snippet)

            if len(selected) >= max_results:
                break

        snippets = selected

        # total_chars должен описывать FINAL набор,
        # а не все загруженные discovery pages.
        total_chars = sum(
            len(s.text or s.content or "")
            for s in snippets
        )

        print(
            f"[scraper] selected={len(snippets)} "
            f"from discovery={len(ranked_candidates)}"
        )

    # 6. Сохраняем rejected для трейса
    result = WebScrapeResult(
        snippets=snippets,
        total_chars=total_chars,
        urls=[s.url for s in snippets]
    )
    setattr(result, "_rejected", rejected)
    setattr(result, "_total_found", len(urls) + rejected_count)

    # Cumulative snapshot of the (possibly cross-claim-shared) cache
    # as of THIS call finishing — the caller that owns the shared
    # instance (retrieve_for_claims) prints the final aggregate once
    # after all claim workers complete; this one is intentionally
    # per-call for live progress visibility, not the summary metric.
    fc = fetch_cache.summary()
    print(
        f"[Shared Fetch Cache] (running) "
        f"requests={fc['requests']} "
        f"network_fetches={fc['network_fetches']} "
        f"saved={fc['saved']}"
    )

    return result


if __name__ == "__main__":
    from agent.orch_schemas import WebQueryResult
    
    wq = WebQueryResult(queries=["как мариновать шашлык"])
    result = scrape(wq)
    print(f"\nСниппетов: {len(result.snippets)}")
    for s in result.snippets:
        print(f"  {s.url[:60]}... ({len(s.content)} chars)")
    
    if hasattr(result, "_rejected"):
        print(f"\nОтклонено: {len(result._rejected)}")
        for r in result._rejected[:3]:
            print(f"  {r['url'][:60]}... ({r['reason']})")
