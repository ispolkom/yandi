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
    preferred_transport,
    record_transport_result,
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
                    return self._results[key]
            # Owner never populated a result (crashed before the
            # finally block below, which should not happen, but this
            # is the safe fallback) -- fetch it ourselves rather than
            # return nothing.

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

        record_transport_result(
            url,
            "direct",
            "ok",
        )

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


def _search_with_ddgs(query: str, max_results: int = MAX_RESULTS) -> tuple[List[str], List[Dict[str, str]]]:
    """
    Поиск через DuckDuckGo.
    Возвращает (список URL, список отклонённых с причинами)
    """
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
        urls, rejected = _search_with_ddgs(query, max_results=max_results)
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
    # TRANSPORT MEMORY ROUTING
    # ========================================================
    #
    # URL, для которых ранее уже доказано:
    #
    #   direct -> cloudflare_challenge
    #   proxy  -> cloudflare_challenge
    #
    # не отправляем повторно в requests pipeline.
    #
    # Они сохраняются отдельно как browser_queue.
    browser_queue = []
    direct_urls = []

    for url in urls:
        transport = preferred_transport(url)

        if transport == "browser":
            browser_queue.append(url)

            print(
                f"  [scraper] browser QUEUE: "
                f"{url[:60]}... "
                f"(transport memory)"
            )
            continue

        direct_urls.append(url)

    urls = direct_urls
    
    if not urls and not browser_queue:
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
        f"{len(urls) + len(browser_queue)} URL, "
        f"direct={len(urls)} "
        f"browser={len(browser_queue)} "
        f"final={max_results}"
    )
    
    # 3. Парсим страницы с проверкой релевантности
    snippets = []
    total_chars = 0
    rejected = all_rejected.copy()
    rejected_count = 0

    # Browser-required URL не считается плохим источником.
    # Он только недоступен текущими requests transport'ами.
    for url in browser_queue:
        rejected.append({
            "url": url,
            "reason": "browser_required",
        })

    if browser_queue:
        print(
            f"[scraper] browser queue="
            f"{len(browser_queue)}"
        )

    # Direct failures, которые имеет смысл повторить
    # через альтернативный transport.
    proxy_retry_urls = []

    # HTTP codes, где смена transport/IP потенциально полезна.
    proxy_retry_http_codes = {
        401,
        403,
        407,
        408,
        429,
        451,
        500,
        502,
        503,
        504,
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
                code = int(
                    reason.split("_", 1)[1]
                )
            except Exception:
                return False

            return code in proxy_retry_http_codes

        return False
    
    # --------------------------------------------------------
    # 3. FETCH POOL — PARTIAL RESULTS ARE VALID
    # --------------------------------------------------------
    #
    # Один зависший URL НЕ должен уничтожать весь search result.
    #
    # wait() возвращает:
    #   done     — что успело завершиться;
    #   not_done — что не успело.
    #
    # Обрабатываем done, not_done считаем timeout.
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=5
    )

    futures = {
        executor.submit(
            fetch_cache.get_or_fetch,
            url,
            "direct",
            lambda u: _fetch_url(u, ""),
        ): url
        for url in urls
    }

    done, not_done = concurrent.futures.wait(
        futures,
        timeout=FETCH_TIMEOUT + 5,
    )

    # --------------------------------------------------------
    # Готовые futures.
    # --------------------------------------------------------
    for future in done:
        url = futures[future]

        try:
            result, reject_reason = future.result()

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

                print(
                    f"  [scraper] OK: "
                    f"{url[:60]}... "
                    f"({len(result['text'])} chars)"
                )

            else:
                reason = (
                    reject_reason
                    or "unknown"
                )

                if _should_proxy_retry(reason):
                    proxy_retry_urls.append(url)

                    print(
                        f"  [scraper] direct FAIL: "
                        f"{url[:60]}... "
                        f"reason={reason} "
                        f"-> proxy queue"
                    )

                else:
                    rejected_count += 1

                    reason_text = REJECT_REASONS.get(
                        reason,
                        reason,
                    )

                    rejected.append({
                        "url": url,
                        "reason": reason,
                    })

                    print(
                        f"  [scraper] reject: "
                        f"{url[:60]}... "
                        f"({reason_text})"
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
    # Незавершённые futures.
    # --------------------------------------------------------
    for future in not_done:
        url = futures[future]

        future.cancel()

        proxy_retry_urls.append(url)

        print(
            f"  [scraper] direct TIMEOUT: "
            f"{url[:60]}... "
            f"-> proxy queue"
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

    # ========================================================
    # 3B. PROXY RETRY PASS
    # ========================================================
    #
    # Direct очередь уже полностью обработана.
    # Только теперь пробуем проблемные URL через proxy.
    proxy_retry_urls = list(
        dict.fromkeys(proxy_retry_urls)
    )

    proxy_success = 0
    proxy_failed = 0

    if proxy_retry_urls:
        if _load_proxy_url():
            print(
                f"[scraper] proxy retry queue="
                f"{len(proxy_retry_urls)}"
            )

            proxy_executor = (
                concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(
                        3,
                        len(proxy_retry_urls),
                    )
                )
            )

            proxy_futures = {
                proxy_executor.submit(
                    fetch_cache.get_or_fetch,
                    url,
                    "proxy",
                    lambda u: _fetch_url_proxy(u, ""),
                ): url
                for url in proxy_retry_urls
            }

            proxy_done, proxy_not_done = (
                concurrent.futures.wait(
                    proxy_futures,
                    timeout=FETCH_TIMEOUT + 5,
                )
            )

            for future in proxy_done:
                url = proxy_futures[future]

                try:
                    result, reason = future.result()

                    if result:
                        snippet = WebSnippet(
                            url=result["url"],
                            title=result["title"],
                            content=result["content"],
                            text=result["text"],
                            relevance=0.7,
                        )

                        snippets.append(snippet)
                        total_chars += len(
                            result["text"]
                        )

                        proxy_success += 1

                        print(
                            f"  [scraper] proxy OK: "
                            f"{url[:60]}... "
                            f"({len(result['text'])} chars)"
                        )

                    else:
                        proxy_failed += 1
                        rejected_count += 1

                        rejected.append({
                            "url": url,
                            "reason": (
                                reason
                                or "proxy_failed"
                            ),
                        })

                        if reason == "cloudflare_challenge":
                            print(
                                f"  [scraper] browser REQUIRED: "
                                f"{url[:60]}... "
                                f"reason=cloudflare_challenge"
                            )
                        else:
                            print(
                                f"  [scraper] proxy FAIL: "
                                f"{url[:60]}... "
                                f"reason="
                                f"{reason or 'unknown'}"
                            )

                except Exception as exc:
                    proxy_failed += 1
                    rejected_count += 1

                    rejected.append({
                        "url": url,
                        "reason": "proxy_fetch_failed",
                    })

                    print(
                        f"  [scraper] proxy ERROR: "
                        f"{url[:60]}... "
                        f"{type(exc).__name__}"
                    )

            for future in proxy_not_done:
                url = proxy_futures[future]

                future.cancel()

                proxy_failed += 1
                rejected_count += 1

                rejected.append({
                    "url": url,
                    "reason": "proxy_timeout",
                })

                print(
                    f"  [scraper] proxy TIMEOUT: "
                    f"{url[:60]}..."
                )

            proxy_executor.shutdown(
                wait=False,
                cancel_futures=True,
            )

        else:
            # Proxy config отсутствует/битый:
            # сохраняем исходные direct failures.
            for url in proxy_retry_urls:
                rejected_count += 1

                rejected.append({
                    "url": url,
                    "reason": "proxy_unavailable",
                })

    if proxy_retry_urls:
        print(
            f"[scraper] proxy summary: "
            f"queued={len(proxy_retry_urls)} "
            f"ok={proxy_success} "
            f"failed={proxy_failed}"
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
