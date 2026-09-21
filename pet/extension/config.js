/**
 * config.js — the one place the extension learns where YANDI lives.
 * Loaded before background.js (background page) and popup.js (popup page).
 * The address is also the ONLY host the extension is allowed to call (manifest "permissions").
 */
var YANDI_CONFIG = Object.freeze({
  API: "http://127.0.0.1:9010",
  ASK_TIMEOUT_MS: 180000,     // the orchestrator searches the web and asks other models
  SHORT_TIMEOUT_MS: 8000,
});
