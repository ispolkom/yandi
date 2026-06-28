"""
agent/orch_tool_agent.py — ReAct-агент поверх heretic:q8

Модель делает ONE вещь: понимает о чём пользователь говорит.
Всё остальное (поиск, открытие, хранение) — детерминировано.
"""
import re
import subprocess
import requests

_SESSION = requests.Session()
_SESSION.trust_env = False
OLLAMA  = "http://127.0.0.1:11434"
NODE_KB = "http://127.0.0.1:18082/api/ai-rpc/knowledge"
MODEL   = "heretic:q8"


# ── Инструменты ───────────────────────────────────────────────────────────────

def _kb_search(query: str, top_k: int = 3) -> list[dict]:
    """Семантический поиск через FAISS реестр."""
    try:
        import sys as _sys; _sys.path.insert(0, "/media/iam/DATASET/yandi")
        from agent.orch_registry_search import search_registry
        result = search_registry(query, top_k=top_k)
        return [{"synthesis": d.text, "score": d.score}
                for d in result.docs if d.score >= 0.40]
    except Exception:
        return []


def _kb_store(question: str, synthesis: str) -> None:
    try:
        import sys as _sys; _sys.path.insert(0, "/media/iam/DATASET/yandi")
        from agent.orch_registry_search import store_synthesis
        store_synthesis(question, synthesis, domain="search", models=["agent"])
    except Exception:
        pass
    try:
        _SESSION.post(f"{NODE_KB}/store",
                      json={"question": question, "synthesis": synthesis,
                            "models": ["agent"], "domain": "search"}, timeout=3)
    except Exception:
        pass


def _ddg_search(query: str) -> list[dict]:
    try:
        from agent.orch_web_scraper import _ddg_search as _ddg
        return _ddg(query, max_results=6)
    except Exception:
        return []


def _open_url(url: str) -> None:
    subprocess.Popen(["firefox", url])


def _extract_yt(text: str) -> str | None:
    m = re.search(r'(https?://(?:www\.)?youtube\.com/watch\?v=[\w-]{11})', text)
    return m.group(1) if m else None


def _model_identify(user_query: str) -> str:
    """Модель переводит бытовое описание в официальное название исполнитель - трек."""
    resp = _SESSION.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": MODEL,
            "messages": [
                {"role": "system", "content":
                    "Ты определяешь о какой песне/клипе говорит пользователь. "
                    "Отвечай ТОЛЬКО: Исполнитель - Название. "
                    "Если не знаешь — отвечай: unknown\n"
                    "Примеры:\n"
                    "вороны крутятся мадонна → Madonna - Frozen\n"
                    "рюмка водки на столе → Григорий Лепс - Рюмка водки на столе\n"
                    "мастхэв бинлав роксет → Roxette - It Must Have Been Love"},
                {"role": "user", "content": user_query},
            ],
            "stream": False,
            "options": {"num_predict": 25, "temperature": 0.1},
        },
        timeout=30,
    )
    text = resp.json().get("message", {}).get("content", "")
    text = re.sub(r"<\|[^|]*\|>", "", text).strip().splitlines()[0].strip()
    return text if text and text.lower() != "unknown" else ""


def _find_on_youtube(title: str) -> str | None:
    """DDG → первый youtube.com/watch URL."""
    results = _ddg_search(f"{title} official music video youtube")
    for r in results:
        url = _extract_yt(r.get("href", "") + r.get("url", ""))
        if url:
            return url
    # retry с site:
    results2 = _ddg_search(f"{title} site:youtube.com")
    for r in results2:
        url = _extract_yt(r.get("href", "") + r.get("url", ""))
        if url:
            return url
    return None


# ── Основной цикл ─────────────────────────────────────────────────────────────

def run(user_query: str) -> str:
    # ── 1. Модель: о чём говорит пользователь? ────────────────────────────────
    title = _model_identify(user_query)
    print(f"  [модель идентифицировала]: {title!r}")

    search_key = title if title else user_query

    # ── 2. KB: уже знаем? ─────────────────────────────────────────────────────
    print(f"  → kb_search({search_key!r})")
    kb_entries = _kb_search(search_key)

    if kb_entries:
        for e in kb_entries:
            url = _extract_yt(e.get("synthesis", ""))
            if url:
                print(f"  ← [KB] нашёл (score={e['score']:.2f}): {url}")
                _open_url(url)
                return f"(из KB) {title or user_query}: {url}"

    # ── 3. DDG: ищем в интернете ──────────────────────────────────────────────
    print(f"  → ddg_search({search_key!r})")
    url = _find_on_youtube(search_key)

    if not url:
        return f"Не нашёл видео для: {search_key}"

    print(f"  ← [DDG] нашёл: {url}")

    # ── 4. Открыть ────────────────────────────────────────────────────────────
    _open_url(url)

    # ── 5. Сохранить в KB ─────────────────────────────────────────────────────
    synthesis = f"{title or user_query} | URL: {url}"
    _kb_store(user_query, synthesis)
    if title:
        _kb_store(title, synthesis)   # и по официальному названию тоже
    print(f"  [KB] сохранено: {synthesis}")

    return f"Открыл: {url}"


if __name__ == "__main__":
    import sys
    query = " ".join(sys.argv[1:]) or "найди Roxette на YouTube"
    print(f"Запрос: {query}\n")
    print(run(query))
