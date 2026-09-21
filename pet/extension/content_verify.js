/**
 * content_verify.js — YANDI Verify overlay.
 *
 * Внедряется в страницу ТОЛЬКО по действию пользователя (правый клик → «Verify with YANDI» или Alt+Shift+Y),
 * см. background.js. Сам в сеть не ходит: все запросы к серверу YANDI делает фоновый скрипт
 * (runtime.sendMessage), поэтому CSP/CORS страницы не мешают.
 *
 * Безопасность: выделенный текст страницы и ответ модели — это НЕДОВЕРЕННЫЕ данные. Они попадают в DOM только
 * через textContent (никакого innerHTML), а панель живёт в закрытом Shadow DOM: стили страницы её не ломают,
 * а скрипты страницы не читают её содержимое.
 */
(function () {
  "use strict";
  // executeScript может выполнить файл повторно в той же песочнице: слушатель ставим один раз
  if (window.__yandiVerifyInstalled) return;
  window.__yandiVerifyInstalled = true;

  const POLL_MS = 3000;
  const POLL_MAX_TRIES = 110;          // ~5.5 минут: столько сервер ждёт внешнюю валидацию

  const TRUST = {
    VERIFIED:      { color: "#4ade80", label: "✅ Verified" },
    HYPOTHESIS:    { color: "#facc15", label: "⚠️ Hypothesis" },
    PERSONAL:      { color: "#f87171", label: "❓ Unverified" },
    CLARIFICATION: { color: "#7c8cf8", label: "💬 Clarification needed" },
  };
  const trustInfo = (level) => TRUST[level] || { color: "#888", label: level || "UNKNOWN" };

  let host = null;       // элемент-хозяин в странице
  let root = null;       // закрытый shadow root
  let token = 0;         // номер текущей проверки: устаревшие опросы прекращаются

  const CSS = `
    :host { all: initial; }
    .panel { position: fixed; top: 20px; right: 20px; width: 420px; max-height: 80vh; box-sizing: border-box;
      background: #0f1117; color: #e8e8e8; border-radius: 12px; border: 1px solid #2a2d3a;
      box-shadow: 0 8px 32px rgba(0,0,0,.6); z-index: 2147483647; overflow: hidden; display: flex; flex-direction: column;
      font: 13px -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
    .head { padding: 12px 16px; background: #1a1d2e; border-bottom: 1px solid #2a2d3a; display: flex;
      align-items: center; justify-content: space-between; cursor: move; user-select: none; }
    .title { font-weight: 600; color: #7c8cf8; font-size: 14px; }
    .close { background: none; border: none; color: #888; cursor: pointer; font-size: 18px; padding: 0 4px; }
    .body { padding: 14px 16px; overflow-y: auto; flex: 1; }
    .quote { color: #aaa; font-size: 12px; word-break: break-word; border-left: 3px solid #2a2d3a; padding-left: 8px; margin-bottom: 10px; }
    .card { background: #1a1d2e; border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
    .trust { font-weight: 600; font-size: 13px; margin-bottom: 6px; }
    .domain { color: #888; font-weight: 400; }
    .answer { line-height: 1.5; color: #e0e0e0; white-space: pre-wrap; word-break: break-word; }
    .query { color: #6ee7f7; margin: 2px 0; }
    .missing { margin-top: 8px; color: #facc15; font-size: 12px; }
    .row { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
    .btn { cursor: pointer; background: #2a2d3a; border: none; border-radius: 6px; padding: 4px 10px; color: #7c8cf8; font-size: 12px; }
    .muted { color: #666; font-size: 12px; }
    .err { color: #f87171; margin-bottom: 8px; }
    .loading { color: #7c8cf8; }
    .valid { margin-top: 10px; padding: 8px 10px; background: #1a1d2e; border-radius: 6px; border-left: 3px solid #888; }
    .valid b { font-size: 12px; }
    a { color: #7c8cf8; font-size: 12px; }
  `;

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function closePanel() {
    token++;
    if (host) host.remove();
    host = root = null;
    document.removeEventListener("keydown", onKey, true);
  }

  function onKey(ev) { if (ev.key === "Escape") closePanel(); }

  function createPanel() {
    if (host) host.remove();
    host = document.createElement("div");
    host.id = "yandi-panel";
    root = host.attachShadow({ mode: "closed" });
    const style = document.createElement("style");
    style.textContent = CSS;
    const panel = el("div", "panel");
    const head = el("div", "head");
    head.appendChild(el("span", "title", "⬡ YANDI Verify"));
    const close = el("button", "close", "✕");
    close.title = "Close (Esc)";
    close.addEventListener("click", closePanel);
    head.appendChild(close);
    const body = el("div", "body");
    panel.append(head, body);
    root.append(style, panel);
    (document.body || document.documentElement).appendChild(host);
    makeDraggable(panel, head);
    document.addEventListener("keydown", onKey, true);
    return body;
  }

  function quoteBox(text) {
    return el("div", "quote", `"${text.slice(0, 120)}${text.length > 120 ? "…" : ""}"`);
  }

  function render(body, ...nodes) {
    body.replaceChildren(...nodes);
  }

  function renderLoading(body, text) {
    const wait = el("div", "loading", "⟳ Searching web + asking the council…");
    render(body, quoteBox(text), wait);
  }

  function renderError(body, err) {
    const link = el("a", "", "Open YANDI dashboard →");
    link.href = "http://127.0.0.1:9010";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    const row = el("div", "row");
    row.appendChild(link);
    render(body,
      el("div", "err", "❌ YANDI server unavailable"),
      el("div", "muted", `Make sure YANDI is running on port 9010. (${err})`),
      row);
  }

  function renderClarification(body, text, data) {
    const card = el("div", "card");
    const t = trustInfo("CLARIFICATION");
    const head = el("div", "trust", t.label);
    head.style.color = t.color;
    card.append(head, el("div", "answer", String(data.question || "YANDI needs more context.")));
    render(body, quoteBox(text), card);
  }

  function renderResult(body, text, data) {
    const trust = data.trust_level || "UNKNOWN";
    const info = trustInfo(trust);
    const answer = String(data.answer || "No answer");
    const domain = data.domain || "";

    const card = el("div", "card");
    const head = el("div", "trust");
    head.style.color = info.color;
    head.textContent = info.label;
    if (domain) head.appendChild(el("span", "domain", ` · ${domain}`));
    card.append(head, el("div", "answer", answer));

    const nodes = [quoteBox(text), card];
    const queries = (data.frame && Array.isArray(data.frame.search_queries)) ? data.frame.search_queries.slice(0, 3) : [];
    if (queries.length) {
      const box = el("div", "");
      queries.forEach((q) => box.appendChild(el("div", "query", `🔍 ${String(q)}`)));
      nodes.push(box);
    }
    const missing = Array.isArray(data.missing) ? data.missing.join(", ") : "";
    if (missing) nodes.push(el("div", "missing", `⚠️ Missing context: ${missing}`));

    const copy = el("button", "btn", "📋 Copy");
    copy.addEventListener("click", () => {
      const out = `YANDI Verify\n\nQ: ${text}\n\nA: ${answer}\n\nTrust: ${trust}`;
      navigator.clipboard.writeText(out).catch(() => {});
    });
    const status = el("span", "muted", data.preliminary === false ? "✓ validated" : "preliminary · validating…");
    const row = el("div", "row");
    row.append(copy, status);
    nodes.push(row);
    render(body, ...nodes);
    return status;
  }

  function appendValidation(body, statusEl, msg) {
    const level = msg.trust_level || "";
    const info = trustInfo(level);
    const box = el("div", "valid");
    box.style.borderLeftColor = info.color;
    const b = el("b", "", `🔍 Multi-model validation: ${level || "done"}`);
    b.style.color = info.color;
    box.appendChild(b);
    body.appendChild(box);
    statusEl.textContent = "✓ validated";
  }

  async function ask(query) {
    return browser.runtime.sendMessage({ type: "yandi_ask", query });
  }

  function note(query, state, trust) {
    browser.runtime.sendMessage({ type: "yandi_note", note: { query, state, trust: trust || "" } }).catch(() => {});
  }

  // Ждём, пока сервер закончит внешнюю валидацию: запись в истории получает preliminary=false
  async function pollValidation(mine, msgId, query, body, statusEl) {
    for (let i = 0; i < POLL_MAX_TRIES; i++) {
      await new Promise((r) => setTimeout(r, POLL_MS));
      if (mine !== token) return;
      let res;
      try { res = await browser.runtime.sendMessage({ type: "yandi_history" }); } catch (_) { continue; }
      if (!res || !res.ok) continue;
      const msg = (res.data.messages || []).find((m) => m.id === msgId);
      if (msg && msg.preliminary === false) {
        if (mine !== token) return;
        appendValidation(body, statusEl, msg);
        note(query, "validated", msg.trust_level);
        return;
      }
    }
    if (mine === token) statusEl.textContent = "validation still pending — see the YANDI dashboard";
  }

  async function verify(text) {
    const mine = ++token;
    const body = createPanel();
    renderLoading(body, text);
    note(text, "loading");
    let res;
    try { res = await ask(text); } catch (e) { res = { ok: false, error: e.message }; }
    if (mine !== token) return;            // панель закрыта или запущена другая проверка
    if (!res || !res.ok) {
      renderError(body, (res && res.error) || "no response");
      note(text, "error");
      return;
    }
    const data = res.data || {};
    if (data.ok === false) {
      renderError(body, data.error || "server error");
      note(text, "error");
    } else if (data.is_clarification) {
      renderClarification(body, text, data);
      note(text, "clarification", "CLARIFICATION");
    } else {
      const statusEl = renderResult(body, text, data);
      note(text, "result", data.trust_level);
      if (data.msg_id && data.preliminary !== false) pollValidation(mine, data.msg_id, text, body, statusEl);
    }
  }

  function makeDraggable(panel, handle) {
    handle.addEventListener("mousedown", (e) => {
      if (e.target.tagName === "BUTTON") return;
      e.preventDefault();
      const rect = panel.getBoundingClientRect();
      const ox = e.clientX - rect.left, oy = e.clientY - rect.top;
      const move = (ev) => {
        panel.style.left = `${ev.clientX - ox}px`;
        panel.style.top = `${ev.clientY - oy}px`;
        panel.style.right = "auto";
      };
      const up = () => {
        document.removeEventListener("mousemove", move, true);
        document.removeEventListener("mouseup", up, true);
      };
      document.addEventListener("mousemove", move, true);
      document.addEventListener("mouseup", up, true);
    });
  }

  browser.runtime.onMessage.addListener((msg) => {
    if (!msg || msg.type !== "yandi_verify") return undefined;
    const text = String(msg.text || "").trim();
    if (!text) return undefined;
    verify(text);
    return Promise.resolve({ ok: true });
  });
})();
