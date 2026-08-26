/**
 * background.js — YANDI Council Bridge v6.0
 *
 * Три канала:
 *   1. Council-чат  → /api/ext/poll    (групповой чат, все модели)
 *   2. Orch AI      → /api/ext/orch/poll (валидация оркестратора, только deepseek)
 *   3. Verify       → правый клик на тексте → YANDI Verify overlay
 */

const API      = "http://127.0.0.1:9010/api/ext";
const ORCH_API = "http://127.0.0.1:9010/api/ext/orch";
const POLL_MS  = 3000;

// ── Context Menu: Verify with YANDI ──────────────────────────────────────────

browser.contextMenus.create({
  id: "yandi-verify",
  title: "⬡ Verify with YANDI",
  contexts: ["selection"],
});

browser.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "yandi-verify") return;
  const text = (info.selectionText || "").trim();
  if (!text || !tab) return;
  browser.tabs.sendMessage(tab.id, { type: "yandi_verify", text });
});

const MODELS = {
  "claude":   ["claude.ai"],
  "gpt":      ["chatgpt.com"],
  "deepseek": ["chat.deepseek.com"],
  "kimi":     ["kimi.com", "www.kimi.com"],
};

// busy разделён по каналам: council и orch — но вкладка одна, поэтому deepseek_orch
// ждёт пока deepseek освободится
const busy = {};
Object.keys(MODELS).forEach(m => busy[m] = false);
let orchBusy = false;  // блокировка orch-канала

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function approxTokens(str) { return Math.ceil((str || "").length / 4); }

async function tabOpen(model) {
  const domains = MODELS[model];
  const tabs = await browser.tabs.query({});
  return tabs.some(t => domains.some(d => (t.url || "").includes(d)));
}

async function findTab(model) {
  const domains = MODELS[model];
  const tabs = await browser.tabs.query({});
  return tabs.find(t => domains.some(d => (t.url || "").includes(d)));
}

// ── Канал 1: Council-чат ──────────────────────────────────────────────────────

async function poll() {
  await Promise.all(Object.keys(MODELS).map(m => pollModel(m)));
  setTimeout(poll, POLL_MS);
}

async function pollModel(model) {
  if (busy[model]) return;
  const hasTab = await tabOpen(model);
  try {
    const resp = await fetch(
      `${API}/poll?model=${model}&tab_open=${hasTab}`,
      { cache: "no-store" }
    );
    if (!resp.ok) return;
    const task = await resp.json();
    if (task && task.task_id && !task.paused && hasTab) {
      busy[model] = true;
      handleTask(model, task).finally(() => { busy[model] = false; });
    }
  } catch (_) {}
}

async function handleTask(model, task) {
  const { task_id, text } = task;
  const myTab = await findTab(model);
  if (!myTab) {
    console.warn(`[Council Bridge] вкладка ${model} не найдена`);
    return;
  }
  let responseText;
  try {
    const r = await browser.tabs.sendMessage(myTab.id, { action: "send", text });
    responseText = r?.text || "[нет ответа]";
  } catch (e) {
    responseText = `[ошибка ${model}: ${e.message}]`;
  }
  await postCouncilResult(task_id, model, responseText,
                          approxTokens(text), approxTokens(responseText));
}

async function postCouncilResult(task_id, from, text, tokens_sent, tokens_recv) {
  try {
    await fetch(`${API}/result`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ task_id, from, text, tokens_sent, tokens_recv }),
    });
  } catch (_) {}
}

// ── Канал 2: Orch AI Validator (только DeepSeek) ──────────────────────────────

async function pollOrch() {
  if (!orchBusy && !busy["deepseek"]) {
    const hasTab = await tabOpen("deepseek");
    if (hasTab) {
      try {
        const resp = await fetch(
          `${ORCH_API}/poll?model=deepseek`,
          { cache: "no-store" }
        );
        if (resp.ok) {
          const task = await resp.json();
          if (task && task.task_id) {
            orchBusy = true;
            handleOrchTask(task).finally(() => { orchBusy = false; });
          }
        }
      } catch (_) {}
    }
  }
  setTimeout(pollOrch, POLL_MS + 1000);  // чуть медленнее council-чата
}

async function handleOrchTask(task) {
  const { task_id, text, _query, _frame, _answer } = task;
  const myTab = await findTab("deepseek");
  if (!myTab) {
    console.warn("[Orch AI] вкладка DeepSeek не найдена");
    return;
  }

  let responseText;
  try {
    const r = await browser.tabs.sendMessage(myTab.id, { action: "send", text });
    responseText = r?.text || "[нет ответа]";
  } catch (e) {
    responseText = `[ошибка deepseek orch: ${e.message}]`;
  }

  await postOrchResult(task_id, responseText, { query: _query, frame: _frame, answer: _answer });
}

async function postOrchResult(task_id, text, meta) {
  try {
    await fetch(`${ORCH_API}/result`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        task_id,
        text,
        _meta: JSON.stringify(meta || {}),
      }),
    });
  } catch (_) {}
}

// ── Старт ─────────────────────────────────────────────────────────────────────

poll();
pollOrch();
