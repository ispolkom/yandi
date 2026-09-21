# Firefox extension (YANDI Council Bridge)

Source: `pet/extension/`. Package: `pet/council_bridge.xpi` (built, committed). It talks only to the local
YANDI server on `http://127.0.0.1:9010` and to the four chat sites below.

## What it does

1. **Council bridge.** The server queues a prompt for a model (`claude`, `gpt`, `deepseek`, `kimi`); the extension
   polls `/api/ext/poll`, types it into the matching chat page you have open and logged in to, waits until the reply
   stops changing, and posts it back to `/api/ext/result`. A second channel serves the orchestrator's DeepSeek
   validation (`/api/ext/orch/*`).
2. **Verify.** Select text on any page → right-click → *Verify with YANDI* (or **Alt+Shift+Y**). A panel shows
   the answer with its trust level, then updates when the server finishes its multi-model validation.
   The panel is injected only when you do this (the extension has no script running on ordinary pages and no
   all-sites permission); it is a closed shadow root, and the page text and the answer are shown as text, never as HTML.
3. **Popup.** Council status (which chat tabs the server has seen), a quick question, the last check.
   The toolbar icon shows `off` when the server does not answer.

## Build, install

```bash
python3 scripts/build_extension.py                # validate + rebuild pet/council_bridge.xpi (also: --check)
sudo pet/install_extension.sh                     # Firefox ESR: enterprise policy, merged into policies.json
sudo pet/install_extension.sh --dry-run           # show what would be written
sudo pet/install_extension.sh --uninstall         # remove exactly what was added
```

The add-on is unsigned. The policy therefore switches off signature enforcement
(`xpinstall.signatures.required = false`, locked), which is honoured only by Firefox **ESR** / Developer Edition /
Nightly and lets any unsigned add-on install in that Firefox. Restart Firefox after installing. Without root, or in a
regular Firefox: `about:debugging` → *This Firefox* → *Load Temporary Add-on* → `pet/extension/manifest.json`
(lasts until the browser restarts).

## Tests

- `pet.pet_extension_regression_test` (core suite): strict manifest, the committed `.xpi` equals a fresh build,
  narrow permissions, no HTML injection, every server path the extension calls exists, installer merges policies.
- `python -m pet.extension_firefox_e2e` (needs `firefox-esr` and `pip install marionette_driver`): installs the
  package in a real headless Firefox against a stub server and checks the popup, the shortcut and overlay, the
  validation update, the council bridge against a fake chat page, and that HTML in an answer runs nothing.

## Limits

- The chat-site scripts (`content_claude.js`, `content_gpt.js`, `content_deepseek.js`, `content_kimi.js`) depend on
  each site's page structure (selectors); they are checked only against a fake page. A site redesign breaks them.
- A task queued for a model whose chat tab is not open is taken from the server's queue and dropped (the requester
  times out); the extension does not queue it for later.
- The server currently answers with `allow_origins=["*"]`; Firefox 140 still applies CORS to the extension's own
  requests, so the extension depends on that. Tightening the server's CORS would break it.
- `pet/extension/content_qwen.js` is not registered (the server has no qwen queue).
