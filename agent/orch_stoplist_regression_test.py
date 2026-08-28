"""
agent/orch_stoplist_regression_test.py — regression for the permanent
stoplist + interleaved direct/proxy fetch lifecycle
(agent/transport_memory.py, agent/orch_web_scraper.py).

Product decision (explicit, from the session): keep proxy fallback,
but run it IMMEDIATELY per-URL instead of as a separate phase after
the whole direct batch completes; if direct AND proxy both genuinely
fail (transport-level reasons only, never content-level), permanently
stoplist the URL — no TTL, never retried again in any future
scrape() call, in this or a later process.

CRITICAL: registry/transport_memory.json is REAL, accumulated runtime
state (229KB+ as of this session) — every test here patches
transport_memory.REGISTRY_DIR/MEMORY_FILE to an isolated temp path so
nothing here ever touches the real file.

Run: /home/iam/venv/bin/python3 -m agent.orch_stoplist_regression_test
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import agent.transport_memory as tm
import agent.orch_web_scraper as ows
from agent.orch_schemas import WebQueryResult
from agent.orch_web_scraper import SharedFetchCache, scrape

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


def _isolated_memory():
    """Context-manager-less helper: returns (tmpdir, patches) — caller
    uses `with patch.object(...), patch.object(...):` directly, this
    just computes the paths so every test uses a FRESH isolated file
    (no cross-test pollution, and never the real registry file)."""
    d = tempfile.mkdtemp(prefix="yandi_transport_memory_test_")
    return Path(d), Path(d) / "transport_memory.json"


# ============================================================
# A. Stoplist persistence + pre-fetch skip
# ============================================================

tmp_dir, tmp_file = _isolated_memory()
direct_calls = []
proxy_calls = []


def _fake_direct_fail(url, query=""):
    direct_calls.append(url)
    return None, "http_403"


def _fake_proxy_fail(url, query=""):
    proxy_calls.append(url)
    return None, "proxy_http_403"


def _fake_ddgs(query, max_results=10, fetch_cache=None):
    if fetch_cache is not None:
        return ows.SharedFetchCache.get_or_search(
            fetch_cache, query, lambda q: (["https://unreachable.example/page"], []),
        )
    return ["https://unreachable.example/page"], []


with patch.object(tm, "REGISTRY_DIR", tmp_dir), patch.object(tm, "MEMORY_FILE", tmp_file), \
     patch.object(ows, "_search_with_ddgs", _fake_ddgs), \
     patch.object(ows, "_fetch_url", _fake_direct_fail), \
     patch.object(ows, "_fetch_url_proxy", _fake_proxy_fail), \
     patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

    wq = WebQueryResult(queries=["unreachable test query"])

    # First scrape(): real attempt on both transports, both fail.
    result1 = scrape(wq, fetch_cache=SharedFetchCache())

    check(
        "first request: direct WAS attempted for the unreachable URL",
        direct_calls == ["https://unreachable.example/page"],
        f"direct_calls={direct_calls}",
    )
    check(
        "first request: proxy WAS attempted immediately after direct "
        "failed (not skipped, not deferred)",
        proxy_calls == ["https://unreachable.example/page"],
        f"proxy_calls={proxy_calls}",
    )
    check(
        "first request: URL is now stoplisted in transport_memory "
        "after both transports genuinely failed",
        tm.is_stoplisted("https://unreachable.example/page"),
    )

    direct_calls.clear()
    proxy_calls.clear()

    # Second scrape(), FRESH SharedFetchCache (simulates a new,
    # unrelated request) — persistent stoplist must survive across
    # separate scrape() calls / separate SharedFetchCache instances.
    result2 = scrape(wq, fetch_cache=SharedFetchCache())

    check(
        "second request: direct fetch is NOT attempted at all — "
        "skipped before any network call, purely from the persistent "
        "stoplist",
        direct_calls == [],
        f"direct_calls={direct_calls}",
    )
    check(
        "second request: proxy is NOT attempted either",
        proxy_calls == [],
        f"proxy_calls={proxy_calls}",
    )
    check(
        "second request: zero results, correctly reflects that the "
        "only discovered URL was stoplisted",
        result2.snippets == [],
    )

# ============================================================
# Persistence across process restart (re-import-equivalent: read the
# on-disk file fresh via a brand-new _load(), not via any in-memory
# state — transport_memory.py has none, it's file-backed on every
# call, so this proves the ACTUAL persistence mechanism, not a cache).
# ============================================================

with patch.object(tm, "REGISTRY_DIR", tmp_dir), patch.object(tm, "MEMORY_FILE", tmp_file):
    check(
        "persistence: the on-disk file itself (read via a fresh "
        "_load(), simulating a process restart) contains the "
        "stoplisted URL — this is real file-backed persistence, not "
        "an in-process cache",
        tm._load()["urls"].get("https://unreachable.example/page", {}).get("stoplisted") is True,
    )

# ============================================================
# B. Content-level rejects must NEVER stoplist
# ============================================================

tmp_dir2, tmp_file2 = _isolated_memory()


def _fake_no_content(url, query=""):
    return None, "no_content"


with patch.object(tm, "REGISTRY_DIR", tmp_dir2), patch.object(tm, "MEMORY_FILE", tmp_file2), \
     patch.object(ows, "_search_with_ddgs", lambda q, max_results=10, fetch_cache=None:
                  (["https://empty-page.example/x"], [])), \
     patch.object(ows, "_fetch_url", _fake_no_content):

    scrape(WebQueryResult(queries=["empty page test"]), fetch_cache=SharedFetchCache())

    check(
        "content-level reject (no_content — page has no usable text) "
        "does NOT stoplist the URL, even though direct 'failed' — "
        "this says nothing about reachability",
        not tm.is_stoplisted("https://empty-page.example/x"),
    )

# ============================================================
# C. proxy_unavailable must NEVER stoplist (can't claim 'even via
# proxy failed' if proxy was never actually tried)
# ============================================================

tmp_dir3, tmp_file3 = _isolated_memory()

with patch.object(tm, "REGISTRY_DIR", tmp_dir3), patch.object(tm, "MEMORY_FILE", tmp_file3), \
     patch.object(ows, "_search_with_ddgs", lambda q, max_results=10, fetch_cache=None:
                  (["https://no-proxy-configured.example/x"], [])), \
     patch.object(ows, "_fetch_url", lambda url, query="": (None, "http_403")), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    scrape(WebQueryResult(queries=["no proxy configured test"]), fetch_cache=SharedFetchCache())

    check(
        "direct fails but proxy is not configured at all "
        "(_load_proxy_url() -> None) - does NOT stoplist, since proxy "
        "was genuinely never attempted",
        not tm.is_stoplisted("https://no-proxy-configured.example/x"),
    )

# ============================================================
# D. Successful proxy fetch: correct transport attribution, no
# stoplist, direct failure alone is forgiven
# ============================================================

tmp_dir4, tmp_file4 = _isolated_memory()


def _fake_proxy_ok(url, query=""):
    text = "recovered content " * 10
    return {"url": url, "title": "t", "text": text, "content": text}, ""


with patch.object(tm, "REGISTRY_DIR", tmp_dir4), patch.object(tm, "MEMORY_FILE", tmp_file4), \
     patch.object(ows, "_search_with_ddgs", lambda q, max_results=10, fetch_cache=None:
                  (["https://recovered-via-proxy.example/x"], [])), \
     patch.object(ows, "_fetch_url", lambda url, query="": (None, "timeout")), \
     patch.object(ows, "_fetch_url_proxy", _fake_proxy_ok), \
     patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

    result_proxy = scrape(WebQueryResult(queries=["recovered content"]), fetch_cache=SharedFetchCache())

    check(
        "direct times out but proxy succeeds: the URL is NOT "
        "stoplisted (it IS reachable, just via a different transport)",
        not tm.is_stoplisted("https://recovered-via-proxy.example/x"),
    )
    check(
        "the recovered content is present in the scrape result",
        len(result_proxy.snippets) == 1 and "recovered content" in result_proxy.snippets[0].text,
    )

# ============================================================
# E. Domain-level promotion after >=2 independently stoplisted URLs
# ============================================================

tmp_dir5, tmp_file5 = _isolated_memory()

with patch.object(tm, "REGISTRY_DIR", tmp_dir5), patch.object(tm, "MEMORY_FILE", tmp_file5):
    tm.stoplist_url("https://baddomain.example/page1", "http_403", "proxy_http_403")
    check(
        "domain-level: after only 1 stoplisted URL, the DOMAIN itself "
        "is not yet stoplisted (a sibling page could still be fine)",
        not tm.is_stoplisted("https://baddomain.example/completely-different-page"),
    )

    tm.stoplist_url("https://baddomain.example/page2", "timeout", "proxy_timeout")
    check(
        "domain-level: after 2 independently stoplisted URLs on the "
        "same domain, a THIRD, never-before-seen URL on that domain "
        "is also treated as stoplisted (domain-level promotion, "
        "mirrors the existing browser_required_count>=2 pattern)",
        tm.is_stoplisted("https://baddomain.example/never-seen-page3"),
    )

# ============================================================
# F. Interleaved lifecycle: a fast-failing URL's proxy attempt is not
# held up by a slow, unrelated URL's direct attempt elsewhere in the
# same batch (the actual architectural point of this change).
# ============================================================

tmp_dir6, tmp_file6 = _isolated_memory()
event_order = []
_lock_for_order = __import__("threading").Lock()


def _fake_direct_variable(url, query=""):
    if "slow" in url:
        time.sleep(0.3)
        with _lock_for_order:
            event_order.append(f"direct_done:{url}")
        return None, "timeout"
    with _lock_for_order:
        event_order.append(f"direct_done:{url}")
    return None, "http_403"


def _fake_proxy_immediate(url, query=""):
    with _lock_for_order:
        event_order.append(f"proxy_done:{url}")
    return None, "proxy_http_403"


with patch.object(tm, "REGISTRY_DIR", tmp_dir6), patch.object(tm, "MEMORY_FILE", tmp_file6), \
     patch.object(ows, "_search_with_ddgs", lambda q, max_results=10, fetch_cache=None:
                  (["https://slow.example/x", "https://fast.example/y"], [])), \
     patch.object(ows, "_fetch_url", _fake_direct_variable), \
     patch.object(ows, "_fetch_url_proxy", _fake_proxy_immediate), \
     patch.object(ows, "_load_proxy_url", lambda: "http://fake-proxy:8080"):

    scrape(WebQueryResult(queries=["interleave test"]), fetch_cache=SharedFetchCache())

    fast_proxy_idx = event_order.index("proxy_done:https://fast.example/y")
    slow_direct_idx = event_order.index("direct_done:https://slow.example/x")

    check(
        "interleaved lifecycle: the FAST url's proxy attempt "
        "completes BEFORE the SLOW url's direct attempt even finishes "
        "- proven by real event ordering, not just an API-shape claim. "
        "The old two-phase design would have forced ALL direct "
        "attempts (including the slow one) to finish before ANY proxy "
        "attempt could start.",
        fast_proxy_idx < slow_direct_idx,
        f"event_order={event_order}",
    )

# ============================================================
# G. not_done (cancelled) tasks are never stoplisted — timeout on the
# whole lifecycle is not proof both transports were exhausted.
# ============================================================

tmp_dir7, tmp_file7 = _isolated_memory()


def _fake_direct_hangs(url, query=""):
    # Must exceed the pool's wait deadline ((FETCH_TIMEOUT*2)+5, with
    # FETCH_TIMEOUT patched to 0.05 below = 5.1s) so the future is
    # genuinely marked not_done/cancelled, not just slow-but-completed.
    # Kept as short as possible above that bound — an already-running
    # thread survives cancel() (concurrent.futures.ThreadPoolExecutor
    # joins its workers at interpreter exit), so this directly adds to
    # this test file's own process exit time.
    time.sleep(6)
    return None, "timeout"


with patch.object(tm, "REGISTRY_DIR", tmp_dir7), patch.object(tm, "MEMORY_FILE", tmp_file7), \
     patch.object(ows, "FETCH_TIMEOUT", 0.05), \
     patch.object(ows, "_search_with_ddgs", lambda q, max_results=10, fetch_cache=None:
                  (["https://hangs-forever.example/x"], [])), \
     patch.object(ows, "_fetch_url", _fake_direct_hangs), \
     patch.object(ows, "_load_proxy_url", lambda: None):

    scrape(WebQueryResult(queries=["hang test"]), fetch_cache=SharedFetchCache())

    # CRITICAL: the cancelled task's underlying thread is STILL
    # RUNNING (concurrent.futures cancel() cannot interrupt an
    # already-executing thread) — it finishes its 6s sleep and
    # resumes AFTER scrape() has already returned. If this `with`
    # block (which is what redirects transport_memory to the isolated
    # temp file) exited before that thread finishes, the thread would
    # resume with the REAL agent.transport_memory.MEMORY_FILE/
    # _load_proxy_url restored — and could write real (fake-domain)
    # data into the actual production registry/transport_memory.json.
    # This is exactly how an earlier version of this test leaked
    # "hangs-forever.example" into the real file (found and cleaned up
    # during this task) — waiting here, still inside the patched
    # block, until the orphaned thread has genuinely finished is the
    # fix, not a nicety.
    time.sleep(2.5)

    check(
        "a task that never completes within the pool's wait timeout "
        "(not_done/cancelled) is NOT stoplisted - we don't know both "
        "transports were genuinely exhausted, only that we gave up "
        "waiting",
        not tm.is_stoplisted("https://hangs-forever.example/x"),
    )

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
