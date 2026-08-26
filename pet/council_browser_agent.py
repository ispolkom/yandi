#!/usr/bin/env python3
"""
council_browser_agent.py — Layer 2 browser bridge.

Listens to council chat Redis pubsub. On each Human message:
  1. Opens Claude.ai and ChatGPT.com in Chromium (with saved session)
  2. Types the message, waits for full response
  3. Posts response back to council chat via POST /reply

First run — use --setup to log in:
  python3 council/council_browser_agent.py --setup

Normal run:
  python3 council/council_browser_agent.py
  python3 council/council_browser_agent.py --headless
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent))
from assistant.local_http import local_post as _local_post

import redis.asyncio as aioredis
from playwright.async_api import async_playwright, BrowserContext, Page, TimeoutError as PWTimeout

# ── config ────────────────────────────────────────────────────────────────────

REDIS_URL    = "redis://127.0.0.1:6379"
PUBSUB_CH    = "council:chat:pubsub"
COUNCIL_API  = "http://127.0.0.1:9010/reply"
CONFIG_FILE  = Path(__file__).parent / "council_config.json"
BROWSER_DATA = Path(__file__).parent / "browser_data"    # persistent Chromium profile

# ── helpers ───────────────────────────────────────────────────────────────────

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def load_config() -> dict:
    return json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}

def proxy_kwargs(cfg: dict) -> dict:
    """Return Playwright proxy= kwarg dict if proxy is configured."""
    raw = cfg.get("proxy", "").strip()
    if not raw:
        return {}
    # accept both "host:port:user:pass" and "http://user:pass@host:port"
    if raw.startswith("http"):
        # parse http://user:pass@host:port
        from urllib.parse import urlparse
        u = urlparse(raw)
        return {"proxy": {
            "server":   f"{u.scheme}://{u.hostname}:{u.port}",
            "username": u.username or "",
            "password": u.password or "",
        }}
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, pw = parts
        return {"proxy": {
            "server":   f"http://{host}:{port}",
            "username": user,
            "password": pw,
        }}
    # host:port only
    if len(parts) == 2:
        return {"proxy": {"server": f"http://{raw}"}}
    return {}

def council_post(who: str, text: str, turn_next: str = "human") -> None:
    try:
        _local_post(COUNCIL_API, json={"from": who, "text": text, "turn_next": turn_next}, timeout=5)
    except Exception as e:
        log(f"council_post error: {e}")

# ── DOM helpers ───────────────────────────────────────────────────────────────

async def wait_stable(page: Page, selector: str,
                      stable_sec: float = 2.5, timeout_sec: int = 120) -> str:
    """Wait until the text of `selector` stops changing for stable_sec seconds."""
    deadline = time.time() + timeout_sec
    last_text = ""
    stable_since: float | None = None
    while time.time() < deadline:
        try:
            els = page.locator(selector)
            count = await els.count()
            if count:
                text = await els.nth(count - 1).inner_text(timeout=3000)
                now = time.time()
                if text != last_text:
                    last_text = text
                    stable_since = now
                elif stable_since and (now - stable_since) >= stable_sec:
                    return text.strip()
        except Exception:
            pass
        await asyncio.sleep(0.4)
    return last_text.strip()


async def fill_contenteditable(page: Page, selector: str, text: str) -> bool:
    """Type text into a contenteditable element. Returns True on success."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=10000)
        await el.click()
        await asyncio.sleep(0.3)
        # clear
        await page.keyboard.press("Control+a")
        await asyncio.sleep(0.1)
        await page.keyboard.press("Backspace")
        await asyncio.sleep(0.2)
        # type
        await page.keyboard.type(text, delay=15)
        return True
    except Exception as e:
        log(f"fill_contenteditable({selector}): {e}")
        return False


async def click_send(page: Page, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                return True
        except Exception:
            continue
    # fallback: Enter
    await page.keyboard.press("Enter")
    return True

# ── Claude.ai ─────────────────────────────────────────────────────────────────

CLAUDE_INPUT  = 'div[contenteditable="true"].ProseMirror'
CLAUDE_INPUT2 = 'div[contenteditable="true"]'
CLAUDE_SEND   = ['button[aria-label="Send message"]', 'button[aria-label="Send Message"]',
                 'button[type="submit"]']
CLAUDE_STOP   = 'button[aria-label*="Stop"]'
CLAUDE_MSG    = '[data-testid="assistant-message"]'
CLAUDE_MSG2   = '.font-claude-message'
CLAUDE_MSG3   = '.prose'


async def send_claude(page: Page, text: str) -> str:
    log("Claude.ai: typing...")
    ok = await fill_contenteditable(page, CLAUDE_INPUT, text)
    if not ok:
        ok = await fill_contenteditable(page, CLAUDE_INPUT2, text)
    if not ok:
        return "[не удалось найти поле ввода Claude.ai]"

    await asyncio.sleep(0.5)
    await click_send(page, CLAUDE_SEND)

    # wait for stop-button to appear → generation started
    log("Claude.ai: waiting for response...")
    try:
        await page.locator(CLAUDE_STOP).wait_for(state="visible", timeout=20000)
        log("Claude.ai: generating...")
        await page.locator(CLAUDE_STOP).wait_for(state="hidden", timeout=120000)
        log("Claude.ai: generation done")
    except PWTimeout:
        log("Claude.ai: stop-button timeout — using stability wait")
        await asyncio.sleep(3)

    # read last message using stability
    for sel in [CLAUDE_MSG, CLAUDE_MSG2, CLAUDE_MSG3]:
        text_out = await wait_stable(page, sel, stable_sec=2.0, timeout_sec=10)
        if text_out:
            return text_out

    return "[ответ Claude.ai не найден]"


# ── ChatGPT.com ───────────────────────────────────────────────────────────────

GPT_INPUT   = '#prompt-textarea'
GPT_INPUT2  = 'div[contenteditable="true"]'
GPT_SEND    = ['button[data-testid="send-button"]', 'button[aria-label="Send prompt"]',
               'button[aria-label="Send message"]']
GPT_STOP    = ['button[aria-label*="Stop"]', 'button[aria-label*="stop"]']
GPT_MSG     = '[data-message-author-role="assistant"] .markdown'
GPT_MSG2    = '.agent-turn .markdown'
GPT_MSG3    = '.markdown.prose'


async def send_gpt(page: Page, text: str) -> str:
    log("ChatGPT: typing...")
    ok = await fill_contenteditable(page, GPT_INPUT, text)
    if not ok:
        ok = await fill_contenteditable(page, GPT_INPUT2, text)
    if not ok:
        return "[не удалось найти поле ввода ChatGPT]"

    await asyncio.sleep(0.5)
    await click_send(page, GPT_SEND)

    log("ChatGPT: waiting for response...")
    # wait for stop button
    stop_appeared = False
    for sel in GPT_STOP:
        try:
            await page.locator(sel).wait_for(state="visible", timeout=15000)
            stop_appeared = True
            log("ChatGPT: generating...")
            await page.locator(sel).wait_for(state="hidden", timeout=120000)
            log("ChatGPT: generation done")
            break
        except PWTimeout:
            continue

    if not stop_appeared:
        log("ChatGPT: stop-button not found — stability wait")
        await asyncio.sleep(4)

    for sel in [GPT_MSG, GPT_MSG2, GPT_MSG3]:
        text_out = await wait_stable(page, sel, stable_sec=2.0, timeout_sec=10)
        if text_out:
            return text_out

    return "[ответ ChatGPT не найден]"


# ── setup: one-time login ─────────────────────────────────────────────────────

async def run_setup(cfg: dict) -> None:
    BROWSER_DATA.mkdir(parents=True, exist_ok=True)
    px = proxy_kwargs(cfg)
    log(f"Setup mode — launching visible Chromium {'via proxy ' + cfg.get('proxy','') if px else '(no proxy)'}...")
    async with async_playwright() as pw:
        ctx = await pw.firefox.launch_persistent_context(
            str(BROWSER_DATA),
            headless=False,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
            **px,
        )
        pages = []
        if cfg.get("claude_web_url"):
            p = await ctx.new_page()
            await p.goto(cfg["claude_web_url"], wait_until="domcontentloaded")
            pages.append(("claude", p))
            log(f"Claude.ai opened: {cfg['claude_web_url']}")

        if cfg.get("gpt_web_url"):
            p = await ctx.new_page()
            await p.goto(cfg["gpt_web_url"], wait_until="domcontentloaded")
            pages.append(("gpt", p))
            log(f"ChatGPT opened: {cfg['gpt_web_url']}")

        log("")
        log("═" * 55)
        log("  Войди в оба чата в открывшемся браузере.")
        log("  Когда войдёшь — нажми Enter здесь.")
        log("═" * 55)
        input("  → Enter для сохранения сессии и выхода: ")

        await ctx.close()
        log("Сессия сохранена в council/browser_data/")
        log("Запусти агента без --setup для начала работы.")


# ── main bridge loop ──────────────────────────────────────────────────────────

async def run_bridge(cfg: dict, headless: bool) -> None:
    claude_url = cfg.get("claude_web_url", "")
    gpt_url    = cfg.get("gpt_web_url", "")

    if not claude_url and not gpt_url:
        log("ERROR: URL-адреса не настроены. Открой Memory панель в council chat и сохрани URL-ы.")
        sys.exit(1)

    if not BROWSER_DATA.exists():
        log("ERROR: Сессия не сохранена. Сначала запусти: python3 council/council_browser_agent.py --setup")
        sys.exit(1)

    px = proxy_kwargs(cfg)
    log(f"Запуск {'headless ' if headless else ''}Chromium {'через прокси ' + cfg.get('proxy','').split(':')[0] if px else '(без прокси)'}...")
    async with async_playwright() as pw:
        ctx: BrowserContext = await pw.firefox.launch_persistent_context(
            str(BROWSER_DATA),
            headless=headless,
            viewport={"width": 1280, "height": 900},
            ignore_https_errors=True,
            **px,
        )

        pages: dict[str, Page] = {}

        if claude_url:
            p = await ctx.new_page()
            await p.goto(claude_url, wait_until="domcontentloaded", timeout=30000)
            pages["claude"] = p
            log(f"Claude.ai: {claude_url}")

        if gpt_url:
            p = await ctx.new_page()
            await p.goto(gpt_url, wait_until="domcontentloaded", timeout=30000)
            pages["gpt"] = p
            log(f"ChatGPT: {gpt_url}")

        log("Подключение к Redis pubsub...")
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe(PUBSUB_CH)

        log("Мост активен. Жду сообщений от Human...")
        council_post("claude", "🌐 Browser bridge активен. Сообщения Human будут доставлены в Claude.ai и ChatGPT.", "human")

        seen_ids: set[str] = set()

        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                msg = json.loads(raw["data"])
            except Exception:
                continue

            if msg.get("type") != "message":
                continue
            if msg.get("from") != "human":
                continue

            msg_id = msg.get("id", "")
            if msg_id and msg_id in seen_ids:
                continue
            if msg_id:
                seen_ids.add(msg_id)

            query = (msg.get("text") or "").strip()
            if not query:
                continue

            log(f"Human: {query[:80]}")

            # ── Claude.ai ──────────────────────────────────────────────
            if "claude" in pages:
                try:
                    council_post("claude", "⏳ Печатаю в Claude.ai...", "claude")
                    await asyncio.sleep(0.5)
                    response = await send_claude(pages["claude"], query)
                    log(f"Claude.ai → {response[:80]}")
                    council_post("claude", response, "gpt" if "gpt" in pages else "human")
                except Exception as e:
                    log(f"Claude.ai ошибка: {e}")
                    council_post("claude", f"[Claude.ai ошибка: {e}]",
                                 "gpt" if "gpt" in pages else "human")

            # ── ChatGPT ────────────────────────────────────────────────
            if "gpt" in pages:
                try:
                    council_post("gpt", "⏳ Печатаю в ChatGPT...", "gpt")
                    await asyncio.sleep(0.5)
                    response = await send_gpt(pages["gpt"], query)
                    log(f"ChatGPT → {response[:80]}")
                    council_post("gpt", response, "human")
                except Exception as e:
                    log(f"ChatGPT ошибка: {e}")
                    council_post("gpt", f"[ChatGPT ошибка: {e}]", "human")

        await r.aclose()
        await ctx.close()


# ── entrypoint ────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Council browser bridge — Layer 2")
    ap.add_argument("--setup",    action="store_true", help="First-time login setup")
    ap.add_argument("--headless", action="store_true", help="Run browser headless")
    args = ap.parse_args()

    cfg = load_config()

    if args.setup:
        asyncio.run(run_setup(cfg))
    else:
        asyncio.run(run_bridge(cfg, headless=args.headless))


if __name__ == "__main__":
    main()
