"""
llm_gateway/config_regression_test.py

Проверяет чтение/запись настройки узла — на временном файле, никогда
не трогает реальный ~/.config/yandi/llm_models.json пользователя.

Запуск: python3 -m llm_gateway.config_regression_test
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def main() -> int:
    from llm_gateway import config as cfg

    with tempfile.TemporaryDirectory() as tmp:
        fake_path = Path(tmp) / "llm_models.json"

        with patch.dict("os.environ", {"YANDI_LLM_CONFIG": str(fake_path)}):
            check("no config file yet -> get_model_entry returns None", cfg.get_model_entry("x") is None)
            check("no config file yet -> list_models is empty", cfg.list_models() == {})

            cfg.set_model_entry("my-model", {"backend": "llamacpp", "path": "/x/y.gguf"})
            check("config file created after first set_model_entry", fake_path.exists())
            entry = cfg.get_model_entry("my-model")
            check("set entry round-trips correctly", entry == {"backend": "llamacpp", "path": "/x/y.gguf"}, repr(entry))

            cfg.set_model_entry("my-remote", {"backend": "remote", "protocol": "anthropic", "base_url": "https://api.anthropic.com", "model": "claude-x", "api_key_env": "MY_KEY"})
            check("second entry doesn't clobber the first", cfg.get_model_entry("my-model") is not None)
            check("both entries visible in list_models", set(cfg.list_models()) == {"my-model", "my-remote"}, repr(cfg.list_models()))

            check("remove_model_entry returns True for existing name", cfg.remove_model_entry("my-model") is True)
            check("removed entry is gone", cfg.get_model_entry("my-model") is None)
            check("remove_model_entry returns False for missing name", cfg.remove_model_entry("my-model") is False)
            check("the other entry survived removal of the first", cfg.get_model_entry("my-remote") is not None)

        # Corrupted / malformed config file must degrade to empty, never crash.
        fake_path.write_text("не json вообще", encoding="utf-8")
        with patch.dict("os.environ", {"YANDI_LLM_CONFIG": str(fake_path)}):
            check("malformed JSON file degrades to empty config, no crash", cfg.list_models() == {})

        fake_path.write_text('{"models": "не словарь"}', encoding="utf-8")
        with patch.dict("os.environ", {"YANDI_LLM_CONFIG": str(fake_path)}):
            check("wrong-shaped 'models' value degrades to empty config, no crash", cfg.list_models() == {})

    print()
    print("=" * 72)
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: {len(FAILURES)} провал(ов): {FAILURES}")
    else:
        print("РЕЗУЛЬТАТ: все проверки пройдены")
    print("=" * 72)

    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
