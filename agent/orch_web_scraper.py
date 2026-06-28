"""
assistant/orch_web_scraper.py — Web Scraper + Content Trimmer.
DuckDuckGo → параллельный fetch 5 URL → trafilatura → чистый текст.
"""
from __future__ import annotations

import concurrent.futures
import re
from typing import Optional

import requests as _requests
import trafilatura

from agent.orch_schemas import WebQueryResult, WebScrapeResult, WebSnippet

MAX_RESULTS   = 7    # URL на один запрос DDG
MAX_PAGES     = 10   # итого страниц для парсинга (с запасом на 404)
MAX_CHARS     = 3500 # символов из одной страницы (чистый текст)
FETCH_TIMEOUT = 12   # секунд на загрузку страницы
WORKERS       = 7    # параллельных fetch
MIN_CONFIRMED = 2    # минимум источников для "подтверждённого" ответа
MIN_TEXT_LEN  = 200  # минимум символов чистого текста — иначе источник не считается

# Домены без текстового контента (видео, соцсети) — парсить бессмысленно
_BLOCKED_DOMAINS = frozenset({
    "youtube.com", "youtu.be", "tiktok.com", "instagram.com",
    "vk.com", "vkvideo.ru", "rutube.ru", "ok.ru",
    "twitter.com", "x.com", "facebook.com", "t.me",
})

# URL-префиксы видео-разделов (домен сам по себе OK, но /video/ — нет)
_BLOCKED_URL_PREFIXES = (
    "https://dzen.ru/video/",
    "http://dzen.ru/video/",
    "https://zen.yandex.ru/video/",
)

_session = _requests.Session()
_session.trust_env = False

_headers = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru,en;q=0.9",
}


def _ddg_search(query: str, max_results: int = MAX_RESULTS) -> list[dict]:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            return [
                {"url": r.get("href",""), "title": r.get("title",""), "body": r.get("body","")}
                for r in ddgs.text(query, max_results=max_results)
            ]
    except Exception as e:
        print(f"  [scraper] DDG error: {e}")
        return []


def _clean_text(raw: str) -> str:
    """Убрать навигацию, заголовки, теги, оставить только связный текст."""
    lines = []
    for line in raw.splitlines():
        line = line.strip()
        if len(line) < 20:           # совсем пустые строки и мусор (метки, иконки)
            continue
        if line.isupper() and len(line) > 6:  # ALL CAPS — заголовки разделов, но не аббревиатуры
            continue
        if line.count("|") > 3:      # breadcrumb навигация
            continue
        if re.match(r'^[\W\d\s]{0,5}$', line):  # почти нет букв
            continue
        lines.append(line)
    return "\n".join(lines)


def _fetch_and_extract(item: dict) -> Optional[tuple[str, str, str]]:
    """Загрузить страницу, извлечь чистый текст. Возвращает (url, title, text) только если страница реально загрузилась."""
    url   = item.get("url", "")
    title = item.get("title", url)
    try:
        resp = _session.get(url, timeout=FETCH_TIMEOUT, headers=_headers, allow_redirects=True)
        resp.raise_for_status()   # 404/403/5xx → исключение → None, URL не попадёт в источники
        raw = trafilatura.extract(
            resp.text,
            include_comments=False,
            include_tables=False,
            include_images=False,
            include_links=False,
            favor_recall=True,
            no_fallback=False,
        )
        if not raw:
            return None  # trafilatura не смогла извлечь — страница без текста (JS/paywall), не используем
        text = _clean_text(raw)[:MAX_CHARS]
        if len(text) < MIN_TEXT_LEN:
            return None
        return url, title, text
    except Exception as e:
        print(f"  [scraper] пропускаем {url}: {e.__class__.__name__}")
        return None   # страница недоступна — не цитируем


def _rank_snippet(snippet: WebSnippet, queries: list[str]) -> float:
    words = set()
    for q in queries:
        words.update(q.lower().split())
    text_words = set((snippet.title + " " + snippet.text).lower().split())
    return len(words & text_words) / max(len(words), 1)


def scrape(web_query_result: WebQueryResult, max_pages: int = MAX_PAGES) -> WebScrapeResult:
    """
    Параллельный поиск + парсинг 3-5 страниц.

    Args:
        web_query_result: результат WebQueryFormulator
        max_pages:        итого страниц (по умолчанию 5)

    Returns:
        WebScrapeResult со сниппетами
    """
    queries   = web_query_result.queries
    seen_urls : set[str] = set()
    candidates: list[dict] = []

    # Собрать кандидатов из всех запросов DDG (без видео-платформ)
    for query in queries:
        for r in _ddg_search(query, max_results=MAX_RESULTS):
            url = r.get("url", "")
            if not url or url in seen_urls:
                continue
            domain = url.split("/")[2].lstrip("www.")
            if any(domain == d or domain.endswith("." + d) for d in _BLOCKED_DOMAINS):
                print(f"  [scraper] skip video domain: {domain}")
                continue
            if any(url.startswith(p) for p in _BLOCKED_URL_PREFIXES):
                print(f"  [scraper] skip video url: {url[:60]}")
                continue
            seen_urls.add(url)
            candidates.append(r)

    print(f"  [scraper] {len(candidates)} уникальных URL, парсим до {max_pages}")

    # Параллельный fetch — берём больше кандидатов, чтобы после отсева 404 осталось достаточно
    snippets: list[WebSnippet] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = {ex.submit(_fetch_and_extract, c): c for c in candidates[:max_pages * 3]}
        for fut in concurrent.futures.as_completed(futures, timeout=FETCH_TIMEOUT + 5):
            res = fut.result()
            if res:
                url, title, text = res
                snippets.append(WebSnippet(url=url, title=title, text=text, rank=0.0))
            if len(snippets) >= max_pages:
                break

    # Ранжировать и вернуть топ
    for s in snippets:
        s.rank = _rank_snippet(s, queries)
    snippets.sort(key=lambda x: -x.rank)
    snippets = snippets[:max_pages]

    print(f"  [scraper] готово: {len(snippets)} сниппетов, {sum(len(s.text) for s in snippets)} символов")
    return WebScrapeResult(
        snippets=snippets,
        total_chars=sum(len(s.text) for s in snippets),
        queries_used=queries,
    )


if __name__ == "__main__":
    from agent.orch_schemas import WebQueryResult
    wq = WebQueryResult(queries=["Kademlia DHT algorithm explained", "Kademlia P2P distributed hash table"])
    result = scrape(wq)
    print(f"Найдено сниппетов: {len(result.snippets)}, символов: {result.total_chars}")
    for i, s in enumerate(result.snippets, 1):
        print(f"\n[{i}] rank={s.rank:.2f} | {s.title}")
        print(f"  URL: {s.url}")
        print(f"  {s.text[:200]}...")
