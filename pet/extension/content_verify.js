/**
 * content_verify.js — YANDI Verify Overlay
 * Shows a floating panel with verification result when user right-clicks text.
 */

const YANDI_API = "http://127.0.0.1:9010";

// ── Panel UI ──────────────────────────────────────────────────────────────────

function createPanel() {
  const existing = document.getElementById("yandi-panel");
  if (existing) existing.remove();

  const panel = document.createElement("div");
  panel.id = "yandi-panel";
  panel.style.cssText = `
    position: fixed; top: 20px; right: 20px; width: 420px; max-height: 80vh;
    background: #0f1117; color: #e8e8e8; border-radius: 12px;
    border: 1px solid #2a2d3a; box-shadow: 0 8px 32px rgba(0,0,0,0.6);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px; z-index: 2147483647; overflow: hidden;
    display: flex; flex-direction: column;
  `;

  panel.innerHTML = `
    <div style="padding:12px 16px; background:#1a1d2e; border-bottom:1px solid #2a2d3a;
                display:flex; align-items:center; justify-content:space-between; cursor:move;"
         id="yandi-header">
      <span style="font-weight:600; color:#7c8cf8; font-size:14px;">⬡ YANDI Verify</span>
      <button id="yandi-close" style="background:none;border:none;color:#888;cursor:pointer;font-size:18px;padding:0 4px;">✕</button>
    </div>
    <div id="yandi-content" style="padding:14px 16px; overflow-y:auto; flex:1;">
      <div id="yandi-body"></div>
    </div>
  `;

  document.body.appendChild(panel);
  document.getElementById("yandi-close").onclick = () => panel.remove();
  makeDraggable(panel, document.getElementById("yandi-header"));
  return panel;
}

function setBody(html) {
  const el = document.getElementById("yandi-body");
  if (el) el.innerHTML = html;
}

function trustColor(level) {
  const map = { "VERIFIED": "#4ade80", "HYPOTHESIS": "#facc15", "PERSONAL": "#f87171" };
  return map[level] || "#888";
}

function trustLabel(level) {
  const map = { "VERIFIED": "✅ Verified", "HYPOTHESIS": "⚠️ Hypothesis", "PERSONAL": "❓ Unverified" };
  return map[level] || level;
}

function renderLoading(text) {
  setBody(`
    <div style="color:#aaa; margin-bottom:10px; font-style:italic; word-break:break-word;">
      "${text.slice(0, 120)}${text.length > 120 ? '…' : ''}"
    </div>
    <div style="color:#7c8cf8; display:flex; align-items:center; gap:8px;">
      <span style="animation:yandi-spin 1s linear infinite; display:inline-block;">⟳</span>
      Searching web + asking Claude, GPT, DeepSeek…
    </div>
    <style>@keyframes yandi-spin { from{transform:rotate(0deg)} to{transform:rotate(360deg)} }</style>
  `);
}

function renderResult(text, data) {
  const trust = data.trust_level || "UNKNOWN";
  const answer = data.answer || "No answer";
  const domain = data.domain || "";
  const missing = (data.missing || []).join(", ");
  const frame = data.frame || {};

  let sourcesHtml = "";
  if (frame.search_queries && frame.search_queries.length) {
    const queries = frame.search_queries.slice(0, 3).map(q =>
      `<div style="color:#6ee7f7; margin:2px 0;">🔍 ${q}</div>`
    ).join("");
    sourcesHtml = `<div style="margin-top:10px;">${queries}</div>`;
  }

  let missingHtml = missing
    ? `<div style="margin-top:8px; color:#facc15; font-size:12px;">⚠️ Missing context: ${missing}</div>`
    : "";

  setBody(`
    <div style="color:#aaa; margin-bottom:10px; font-size:12px; word-break:break-word; border-left:3px solid #2a2d3a; padding-left:8px;">
      "${text.slice(0, 100)}${text.length > 100 ? '…' : ''}"
    </div>

    <div style="background:#1a1d2e; border-radius:8px; padding:10px 12px; margin-bottom:10px;">
      <div style="color:${trustColor(trust)}; font-weight:600; font-size:13px; margin-bottom:6px;">
        ${trustLabel(trust)} ${domain ? `· <span style="color:#888; font-weight:400;">${domain}</span>` : ""}
      </div>
      <div style="line-height:1.5; color:#e0e0e0;">${answer}</div>
    </div>

    ${sourcesHtml}
    ${missingHtml}

    <div style="margin-top:12px; display:flex; gap:8px; flex-wrap:wrap;">
      <span id="yandi-copy-btn" style="cursor:pointer; background:#2a2d3a; border-radius:6px;
            padding:4px 10px; color:#7c8cf8; font-size:12px;">📋 Copy</span>
      <span style="color:#555; font-size:12px; align-self:center;">preliminary · validating…</span>
    </div>
  `);

  document.getElementById("yandi-copy-btn").onclick = () => {
    const copyText = `YANDI Verify\n\nQ: ${text}\n\nA: ${answer}\n\nTrust: ${trust}`;
    navigator.clipboard.writeText(copyText).catch(() => {});
  };

  // Poll for validation update
  pollValidation(data.msg_id, text, answer);
}

function renderError(text, err) {
  setBody(`
    <div style="color:#f87171; margin-bottom:8px;">❌ YANDI server unavailable</div>
    <div style="color:#888; font-size:12px;">Make sure YANDI is running on port 9010.<br>${err}</div>
    <div style="margin-top:10px;">
      <a href="http://127.0.0.1:9010" target="_blank"
         style="color:#7c8cf8; font-size:12px;">Open YANDI dashboard →</a>
    </div>
  `);
}

async function pollValidation(msgId, question, prelimAnswer) {
  if (!msgId) return;
  let tries = 0;
  while (tries < 20) {
    await new Promise(r => setTimeout(r, 3000));
    tries++;
    try {
      const resp = await fetch(`${YANDI_API}/api/orch/history`, { cache: "no-store" });
      if (!resp.ok) continue;
      const data = await resp.json();
      const msgs = data.messages || [];
      const msg = msgs.find(m => m.id === msgId || (m.validation && m.id === msgId));
      if (msg && msg.validation) {
        appendValidation(msg.validation);
        return;
      }
      // Check status endpoint
      const statusResp = await fetch(`${YANDI_API}/api/orch/status/${msgId}`, { cache: "no-store" });
      if (statusResp.ok) {
        const status = await statusResp.json();
        if (status.verdict) {
          appendValidation(status);
          return;
        }
      }
    } catch (e) { /* ignore */ }
  }
}

function appendValidation(v) {
  const el = document.getElementById("yandi-body");
  if (!el) return;
  const verdict = v.verdict || v.trust || "";
  const explanation = v.explanation || v.update_text || "";
  const color = trustColor(verdict);
  const note = document.createElement("div");
  note.style.cssText = `margin-top:10px; padding:8px 10px; background:#1a1d2e;
    border-radius:6px; border-left:3px solid ${color};`;
  note.innerHTML = `
    <div style="color:${color}; font-weight:600; font-size:12px;">
      🔍 Multi-model validation: ${verdict}
    </div>
    ${explanation ? `<div style="color:#aaa; font-size:12px; margin-top:4px;">${explanation}</div>` : ""}
  `;
  el.appendChild(note);
  const span = el.querySelector('[style*="preliminary"]');
  if (span) span.textContent = "✓ validated";
}

// ── Drag support ──────────────────────────────────────────────────────────────

function makeDraggable(el, handle) {
  let ox = 0, oy = 0;
  handle.addEventListener("mousedown", e => {
    e.preventDefault();
    ox = e.clientX - el.getBoundingClientRect().left;
    oy = e.clientY - el.getBoundingClientRect().top;
    const move = ev => {
      el.style.left = (ev.clientX - ox) + "px";
      el.style.top = (ev.clientY - oy) + "px";
      el.style.right = "auto";
    };
    const up = () => {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
    };
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  });
}

// ── Message handler ───────────────────────────────────────────────────────────

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.type !== "yandi_verify") return;

  const text = msg.text.trim();
  if (!text) return;

  createPanel();
  renderLoading(text);

  try {
    const resp = await fetch(`${YANDI_API}/api/orchestrator/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: text, enable_web: true }),
    });

    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const data = await resp.json();
    renderResult(text, data);
  } catch (e) {
    renderError(text, e.message);
  }
});
