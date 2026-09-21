# The «YANDI» tab: the chosen Voice now answers (local model)

What is **implemented and tested**. Code: `pet/voice.py`, `pet/settings_api.py`, the override in `pet/chat_local.py` (`/api/local/chat`), the page text in
`pet/media/settings_tab.js`. Proof: `pet/pet_voice_regression_test.py` (+ mutants) and the updated `pet/pet_settings_tab_regression_test.py`.

## What was wrong

The tab already let you browse the disk, pick a `.gguf` model and press «Применить» — but that only wrote `~/.local/share/yandi/web_settings.json`. Nothing read it: the
assistant kept answering with whatever the old model list said, while the page said «Сейчас используется». (Registering the chosen file in the node's gateway was the
step that had been deferred: see the note in `pet/chat_models.py`.)

## What it does now

* **«Применить» = check → register → save.** The file must exist and be a GGUF model; it is registered in the node's gateway config (the encrypted node store) under the name
  `yandi-voice`; only then are the settings saved. A missing or wrong file is refused with the reason and **nothing is saved or changed**.
* **The assistant answers with the Voice.** While the saved Voice is a local model that is registered, `/api/local/chat` uses `yandi-voice` instead of the model chosen in the old
  drop-down (the reply says which model answered: `model_used`). The reply, the event extraction and the verification all use it.
* **The page does not claim what is not true.** After «Применить»: «✅ Применено: отвечает локальная модель …» — or, for a Voice that is not connected, «⚠️ Сохранено, но не применено: …»
  and the line «Выбран Голос …, но он пока НЕ используется». The status is recomputed on every load (a registration that disappeared shows as not in use).
* **A damaged settings file never breaks the chat**: it answers as before.
* **No silent substitution:** if the engine cannot load the chosen file, the gateway reports the error (an explicitly configured model has no automatic fallback).

## Not connected yet (said so on the page)

* A **remote** model and an **API service** can be chosen and are saved, but are reported «не применено»: they need the API-key storage and the outbound gate
  (only the question leaves the machine). That is the next step and awaits the owner's decisions.
* The advisors («Советники») are still only recorded.

## Limits

* Switching to another file loads the new model; the previous one stays in memory until the assistant restarts (`llm_gateway/llamacpp_backend.py` caches by file path).
* The first answer after applying is slow: the model loads then.
* The registration lives in the node's config store (`node_kek.bin` key), not in the sealed database.
