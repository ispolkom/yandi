"""
chat_translate.py — переводчик и языковые утилиты.
Endpoint: /api/council/translate, /api/council/languages
Утилиты: _ollama_mini, _detect_lang_name, _translate, _gen_slug, _gen_tags, _write_knowledge
Используется только этим модулем — настройки модели не влияют на другие чаты.
"""
import json
from pathlib import Path

from fastapi import APIRouter

from pet.shared import OLLAMA_URL, OLLAMA_MOD, LANG_NAMES, LANG_FULL

router = APIRouter()

_KW_DIR  = Path(__file__).parent.parent / "registry" / "verified_knowledge"
_KW_FILE = _KW_DIR / "knowledge.jsonl"


# ── Ollama utility (только для переводчика/тегировщика) ───────────────────────

def _ollama_mini(prompt: str, max_tokens: int = 60) -> str:
    import requests, re
    try:
        s = requests.Session(); s.trust_env = False
        r = s.post(
            f"{OLLAMA_URL}/api/generate",
            json={"model": OLLAMA_MOD, "prompt": prompt, "stream": False,
                  "options": {"temperature": 0.1, "num_predict": max_tokens}},
            timeout=60,
        )
        raw = r.json().get("response", "").strip()
        raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
        for stop in ("<|endoftext|>", "<|im_start|>", "<|im_end|>", "</s>"):
            raw = raw.split(stop)[0]
        return raw.strip()
    except Exception:
        return ""


# ── Языковые утилиты ──────────────────────────────────────────────────────────

def _detect_lang_name(text: str) -> str:
    """Определить язык — вернуть полное название на английском (любой язык)."""
    import re
    prompt = (
        "What language is the following text written in? "
        "Reply with ONLY the language name in English (e.g. Russian, Chinese, Nanai, French, Arabic). "
        "One word or short phrase, nothing else.\n"
        "Text: " + text[:300] + "\nLanguage:"
    )
    raw = _ollama_mini(prompt, max_tokens=8).strip()
    raw = re.sub(r"[^a-zA-Z\s\-]", "", raw).strip()
    raw = " ".join(raw.split()[:3])
    return raw if raw else "English"


def _detect_lang(text: str) -> str:
    """Обратная совместимость — 2-буквенный код."""
    name = _detect_lang_name(text).lower()
    _map = {
        "russian": "ru", "english": "en", "chinese": "zh", "german": "de",
        "french": "fr", "spanish": "es", "romanian": "ro", "ukrainian": "uk",
        "japanese": "ja", "korean": "ko", "arabic": "ar", "polish": "pl", "turkish": "tr",
    }
    return _map.get(name, name[:2])


def _translate(text: str, target_lang_name: str) -> str:
    """Перевести на target_lang_name (полное название, любой язык)."""
    prompt = (
        f"Translate the following text to {target_lang_name}. "
        "Output ONLY the translation, no explanations, no prefix.\n"
        "Text:\n" + text[:2000] + "\nTranslation:"
    )
    raw = _ollama_mini(prompt, max_tokens=800)
    paras = raw.split("\n\n")
    seen, out = set(), []
    for p in paras:
        ps = p.strip()
        if ps and ps not in seen:
            seen.add(ps); out.append(ps)
    return "\n\n".join(out).strip()


# ── Датасет-утилиты (используются в /api/council/save_dataset) ────────────────

def _gen_slug(question: str) -> str:
    import re
    prompt = (
        "Translate to English, make a 3-5 word lowercase dash-separated slug. "
        "Output ONLY the slug, nothing else.\n"
        f"Input: {question[:200]}\nSlug:"
    )
    raw = _ollama_mini(prompt, max_tokens=20)
    slug = re.sub(r"[^a-z0-9-]", "-", raw.lower().strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:60] if slug else "council-session"


def _gen_tags(question: str) -> list[str]:
    prompt = (
        "Classify the topic. Output 3-5 English tags as comma-separated domain:subcategory pairs. "
        "One tag may be 'noise:flood' if it's casual/trivial. Output ONLY tags.\n"
        f"Question: {question[:300]}\nTags:"
    )
    raw = _ollama_mini(prompt, max_tokens=40)
    import re
    tags = [t.strip() for t in re.split(r"[,\n]", raw) if t.strip()]
    return tags[:7]


def _gen_en_summary(question: str, answers: dict[str, str]) -> str:
    parts = [f"Q: {question}"] + [f"{m}: {a[:200]}" for m, a in answers.items()]
    prompt = (
        "Write a 1-sentence English summary of this Q&A exchange. Output ONLY the summary.\n\n"
        + "\n".join(parts)[:800] + "\nSummary:"
    )
    return _ollama_mini(prompt, max_tokens=60)


def _write_knowledge(question: str, answer: str, tags: list[str],
                     slug: str, en_summary: str, models: list[str]):
    import time
    _KW_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "question":   question,
        "answer":     answer,
        "tags":       tags,
        "slug":       slug,
        "en_summary": en_summary,
        "models":     models,
        "ts":         time.time(),
        "verdict":    "COUNCIL_VERIFIED",
    }
    with open(_KW_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/api/council/languages")
async def get_languages():
    return {"languages": LANG_NAMES}


@router.post("/api/council/translate")
async def council_translate(payload: dict):
    """Перевести текст на язык пользователя (любой язык). detect_only=true — только определить."""
    import asyncio
    text        = (payload.get("text") or "").strip()
    target_lang = (payload.get("target_lang") or "").strip()
    detect_only = bool(payload.get("detect_only"))
    if not text:
        return {"ok": False, "error": "empty text"}
    loop = asyncio.get_event_loop()
    detected_name = await loop.run_in_executor(None, _detect_lang_name, text)
    if detect_only:
        return {"ok": True, "source_lang": detected_name, "translation": ""}
    target_name = LANG_FULL.get(target_lang, target_lang) if target_lang else detected_name
    if detected_name.lower() == target_name.lower():
        return {"ok": True, "translation": text,
                "source_lang": detected_name, "target_lang": target_name}
    translation = await loop.run_in_executor(None, _translate, text, target_name)
    return {"ok": True, "translation": translation,
            "source_lang": detected_name, "target_lang": target_name}
