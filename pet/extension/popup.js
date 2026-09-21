/**
 * popup.js — окно расширения: статус совета, быстрый вопрос, последняя проверка.
 * Все данные сервера и модели вставляются только через textContent (никакого innerHTML).
 */
const API = YANDI_CONFIG.API;

const MODEL_NAMES = {
  claude: "Claude", gpt: "GPT", deepseek: "DeepSeek", kimi: "Kimi", qwen: "Qwen"
};
const TRUST_COLORS = { VERIFIED: "#4ade80", HYPOTHESIS: "#facc15", PERSONAL: "#f87171", CLARIFICATION: "#7c8cf8" };

function node(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function getJson(path, options, timeoutMs) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs || YANDI_CONFIG.SHORT_TIMEOUT_MS);
  try {
    const resp = await fetch(`${API}${path}`, { cache: "no-store", ...(options || {}), signal: ctl.signal });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    return await resp.json();
  } finally {
    clearTimeout(timer);
  }
}

async function loadStatus() {
  const el = document.getElementById("models-list");
  try {
    const data = await getJson("/api/council/connections");
    const rows = Object.entries(data).map(([k, v]) => {
      const row = node("div", "model");
      row.appendChild(node("div", `dot ${v.connected ? "on" : "off"}`));
      row.appendChild(node("span", "model-name", MODEL_NAMES[k] || k));
      if (v.connected) row.appendChild(node("span", "last-seen", `${v.last_seen_sec}s ago`));
      return row;
    });
    el.replaceChildren(...rows);
  } catch (e) {
    const warn = node("div", "", "⚠ YANDI not running on port 9010");
    warn.style.cssText = "color:#f87171; font-size:12px;";
    el.replaceChildren(warn);
  }
}

function showAnswer(trust, text) {
  document.getElementById("result").style.display = "block";
  const color = TRUST_COLORS[trust] || "#888";
  const badge = document.getElementById("trust-badge");
  if (trust) {
    const b = node("span", "trust-badge", trust);
    b.style.cssText = `background:${color}20; color:${color}; border:1px solid ${color}`;
    badge.replaceChildren(b);
  } else {
    badge.replaceChildren();
  }
  document.getElementById("answer-text").textContent = text;
}

async function ask() {
  const query = document.getElementById("query").value.trim();
  if (!query) return;
  const btn = document.getElementById("ask-btn");
  btn.disabled = true;
  btn.textContent = "Asking…";
  try {
    const res = await browser.runtime.sendMessage({ type: "yandi_ask", query });
    if (!res || !res.ok) throw new Error((res && res.error) || "no response");
    const data = res.data || {};
    if (data.ok === false) showAnswer("", "Error: " + (data.error || "server error"));
    else if (data.is_clarification) showAnswer("CLARIFICATION", String(data.question || "YANDI needs more context."));
    else showAnswer(data.trust_level || "UNKNOWN", data.answer || "No answer");
  } catch (e) {
    showAnswer("", "Error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Ask YANDI";
  }
}

async function loadLast() {
  try {
    const res = await browser.runtime.sendMessage({ type: "yandi_last" });
    if (!res || !res.last) return;
    const l = res.last;
    const parts = [l.state + (l.trust ? ` · ${l.trust}` : ""), `“${l.query}”`];
    document.getElementById("last-text").textContent = parts.join(" — ");
    document.getElementById("last").style.display = "block";
  } catch (_) { /* фоновый скрипт ещё не готов */ }
}

document.getElementById("ask-btn").addEventListener("click", ask);
document.getElementById("query").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.ctrlKey) ask();
});

loadStatus();
loadLast();
