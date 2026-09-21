"""
pet/pet_voice_regression_test.py — the Voice chosen in the «YANDI» tab is the one that ANSWERS (not only a line in a file).

    «ПРИМЕНИТЬ» = ПРОВЕРИТЬ, ЗАРЕГИСТРИРОВАТЬ В ШЛЮЗЕ УЗЛА, СОХРАНИТЬ.   НЕ ПРИМЕНЕНО — НЕ «СЕЙЧАС ИСПОЛЬЗУЕТСЯ».
    ОТВЕЧАЕТ ВЫБРАННАЯ МОДЕЛЬ; ЕСЛИ НАСТРОЙКИ СЛОМАНЫ — ЧАТ ОТВЕЧАЕТ КАК РАНЬШЕ, А НЕ ПАДАЕТ.

Хранилище настроек и шлюза — временные (YANDI_WEB_SETTINGS, YANDI_NODE_DB, YANDI_KEK_PATH); модель не загружается (файл-«GGUF» из
заголовка), чат — настоящий роутер с подменённым ответом. Затем каждая защита снимается нарочно (мутанты).

Run: python -m pet.pet_voice_regression_test
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("YANDI_TEST_MODE", "1")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def run_checks() -> list:
    failures: list = []

    def check(name: str, condition: bool) -> None:
        if not condition:
            failures.append(name)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import pet.chat_local as chat_local
    import pet.settings_api as settings_api
    import pet.ui_settings as us
    import pet.voice as voice
    from llm_gateway import client as gateway_client, config as gateway_config

    tmp = Path(tempfile.mkdtemp(prefix="yandi-voice-"))
    os.environ["YANDI_WEB_SETTINGS"] = str(tmp / "cfg" / "web_settings.json")
    os.environ["YANDI_NODE_DB"] = str(tmp / "node.db")
    os.environ["YANDI_KEK_PATH"] = str(tmp / f"keys{len(list(tmp.iterdir()))}" / "kek.bin")
    models = tmp / "models"
    models.mkdir()
    first, second, fake = models / "one.gguf", models / "two.gguf", models / "notgguf.gguf"
    first.write_bytes(b"GGUF" + b"\0" * 60)
    second.write_bytes(b"GGUF" + b"\0" * 60)
    fake.write_bytes(b"not a model at all")

    def doc(path: str, voice_kind: str = "local") -> dict:
        return {"voice": voice_kind, "advisors": [], "local": {"path": path},
                "remote": {"address": "http://192.168.1.5:8080", "model": "qwen-14b"}, "api": {"service": "openai", "model": "gpt-4o-mini"}}

    app = FastAPI()
    app.include_router(settings_api.router)
    app.include_router(chat_local.router)
    c = TestClient(app, base_url="http://127.0.0.1:9010")
    settings_file = Path(os.environ["YANDI_WEB_SETTINGS"])

    def entry():
        return gateway_config.get_model_entry(voice.VOICE_ALIAS)

    # ── apply ──
    d = c.post("/api/ui/settings", json=doc(str(first))).json()
    check("V1 a local Voice is applied: ok, applied, and it says which model answers", d["ok"] and d["applied"] is True and "one.gguf" in d["note"])
    check("V2 …the file is registered in the node's gateway under the Voice name", entry() is not None and entry()["backend"] == "llamacpp" and entry()["path"] == str(first))
    target = gateway_client.resolve_target(voice.VOICE_ALIAS, base_url=gateway_client.DEFAULT_BASE_URL)
    check("V3 …and the gateway resolves that name to the local engine with THAT file (it reaches the model)", target.runtime == "llama_cpp" and target.target["spec"].path == str(first))
    before = entry()
    c.post("/api/ui/settings", json=doc(str(first)))
    check("V4 applying the same file again changes nothing", entry() == before)
    d = c.post("/api/ui/settings", json=doc(str(second))).json()
    check("V5 another file replaces the registration", d["applied"] and entry()["path"] == str(second))

    saved_before = settings_file.read_bytes()
    entry_before = entry()
    d = c.post("/api/ui/settings", json=doc(str(models / "missing.gguf"))).json()
    check("V6 a file that does not exist is NOT applied and NOT saved (nothing is left half-way)", d["ok"] is False and "не применено" in d["error"]
          and settings_file.read_bytes() == saved_before and entry() == entry_before)
    d = c.post("/api/ui/settings", json=doc(str(fake))).json()
    check("V7 a file that is not a GGUF model is refused the same way", d["ok"] is False and settings_file.read_bytes() == saved_before and entry() == entry_before)

    # ── the status never lies ──
    g = c.get("/api/ui/settings").json()
    check("V8 GET reports applied=true only for a Voice that is really registered", g["ok"] and g["saved"] and g["applied"] is True)
    gateway_config.remove_model_entry(voice.VOICE_ALIAS)
    g = c.get("/api/ui/settings").json()
    check("V9 …and applied=false with a reason when the registration is gone (the page must not say 'in use')", g["applied"] is False and "Применить" in g["note"])
    check("V10 …then the chat is not switched to a model that does not exist", voice.effective_model("heretic:q8") == "heretic:q8")

    # ── the other kinds of Voice are honest ──
    d = c.post("/api/ui/settings", json=doc(str(first), "remote")).json()
    check("V11 a remote Voice is saved but reported as NOT applied, with the reason", d["ok"] and d["applied"] is False and "пока не подключен" in d["note"])
    check("V12 …and the assistant keeps answering as before", voice.effective_model("heretic:q8") == "heretic:q8")
    d = c.post("/api/ui/settings", json=doc(str(first), "api")).json()
    check("V13 the same for an API service", d["ok"] and d["applied"] is False)

    # ── the chat ──
    c.post("/api/ui/settings", json=doc(str(first)))
    seen: dict = {}

    def fake_respond(model, messages, temperature, source_turn_id=None):
        seen["model"] = model
        return "ответ"
    body = {"model": "heretic:q8", "messages": [{"role": "user", "content": "привет"}], "turn_id": "turn-voice-0001"}
    with patch.object(chat_local, "_respond_with_character", fake_respond):
        r = c.post("/api/local/chat", json=body).json()
    check("V14 with a local Voice applied, the assistant answers with THE VOICE (not the model picked in the old list)", seen.get("model") == voice.VOICE_ALIAS
          and r["ok"] and r["model_used"] == voice.VOICE_ALIAS)
    os.remove(settings_file)
    seen.clear()
    with patch.object(chat_local, "_respond_with_character", fake_respond):
        r = c.post("/api/local/chat", json=body).json()
    check("V15 with no saved settings the requested model is used, exactly as before", seen.get("model") == "heretic:q8" and r["model_used"] == "heretic:q8")
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{ this is not json")
    seen.clear()
    with patch.object(chat_local, "_respond_with_character", fake_respond):
        r = c.post("/api/local/chat", json=body).json()
    check("V16 a damaged settings file never breaks the chat", r["ok"] and seen.get("model") == "heretic:q8")

    # ── the page does not claim what is not true ──
    js = (ROOT / "pet" / "media" / "settings_tab.js").read_text(encoding="utf-8")
    check("V17 the page has a 'chosen but NOT in use' state and shows the reason (it does not always say 'currently used')",
          "cfg.applied === false" in js and "НЕ используется" in js and "Сохранено, но не применено" in js)
    shutil.rmtree(tmp, ignore_errors=True)
    return failures


def mutants() -> list:
    import contextlib
    import pet.voice as voice

    real_apply = voice.apply

    @contextlib.contextmanager
    def swapped(obj, name, value):
        original = getattr(obj, name)
        setattr(obj, name, value)
        try:
            yield
        finally:
            setattr(obj, name, original)

    def ignores_settings(requested):                                # M1: the Voice never answers
        return requested

    def claims_without_registering(settings):                       # M2: says applied, registers nothing
        if settings.get("voice") == "local":
            return {"applied": True, "note": "отвечает локальная модель x"}
        return real_apply(settings)

    def remote_claims_applied(settings):                            # M3: a remote Voice is reported as in use
        out = real_apply(settings)
        return {**out, "applied": True}

    def always_applied(settings):                                   # M4: the status never says 'not applied'
        return {"applied": True, "note": ""}

    def switches_without_registration(requested):                   # M5: the chat is switched even if the model is not registered
        from pet import ui_settings
        s = ui_settings.load()
        return voice.VOICE_ALIAS if s.get("saved") and s.get("voice") == "local" else requested

    def register_without_checking(path):                            # M7: any path is registered (a missing file is 'applied')
        from llm_gateway import config as cfg
        from llm_gateway.setup import build_local_entry
        if cfg.get_model_entry(voice.VOICE_ALIAS) is not None:
            cfg.remove_model_entry(voice.VOICE_ALIAS)
        cfg.set_model_entry(voice.VOICE_ALIAS, build_local_entry(Path(path)))
        return Path(path).name

    return [
        ("M1 the chat never uses the Voice", swapped(voice, "effective_model", ignores_settings)),
        ("M2 'applied' is reported without registering the model", swapped(voice, "apply", claims_without_registering)),
        ("M3 a remote Voice is reported as in use", swapped(voice, "apply", remote_claims_applied)),
        ("M4 the status always says applied", swapped(voice, "status", always_applied)),
        ("M5 the chat switches to a Voice that is not registered", swapped(voice, "effective_model", switches_without_registration)),
        ("M7 any path is registered without checking the file", swapped(voice, "register_local", register_without_checking)),
    ]


def main() -> int:
    print("clean code:")
    failures = run_checks()
    for name in failures:
        print(f"[FAIL] {name}")
    if not failures:
        print("[OK] every check passes")
    bad = bool(failures)
    for label, applied in mutants():
        with applied:
            try:
                caught = run_checks()
            except Exception as exc:                  # noqa: BLE001
                caught = [f"the run itself failed: {type(exc).__name__}"]
        print(f"[{'OK' if caught else 'FAIL'}] {label} -> {'caught, e.g. ' + caught[0][:70] if caught else 'NOT CAUGHT'}")
        bad = bad or not caught
    print("RESULT:", "all checks passed" if not bad else "FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
