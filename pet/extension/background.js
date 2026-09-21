/**
 * background.js — YANDI Council Bridge v3.1
 *
 * Каналы:
 *   1. Council-чат  → /api/ext/poll      (групповой чат, все модели)
 *   2. Orch AI      → /api/ext/orch/poll (валидация оркестратора, только deepseek)
 *   3. Verify       → правый клик на тексте / Alt+Shift+Y → оверлей YANDI Verify
 *
 * Оверлей внедряется в страницу ТОЛЬКО по действию пользователя (activeTab), а все обращения к серверу
 * YANDI делает этот фоновый скрипт: страница и её CSP/CORS не участвуют.
 */

const API      = `${YANDI_CONFIG.API}/api/ext`;
const ORCH_API = `${YANDI_CONFIG.API}/api/ext/orch`;
const POLL_MS  = 3000;

// ── Состояние сервера на значке ──────────────────────────────────────────────

let serverUp = null;
function setServerState(up) {
  if (up === serverUp) return;
  serverUp = up;
  browser.browserAction.setBadgeText({ text: up ? "" : "off" });
  browser.browserAction.setBadgeBackgroundColor({ color: "#b91c1c" });
  browser.browserAction.setTitle({ title: up ? "YANDI" : "YANDI — сервер на порту 9010 не отвечает" });
}

async function apiFetch(path, options = {}, timeoutMs = YANDI_CONFIG.SHORT_TIMEOUT_MS) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    return await fetch(`${YANDI_CONFIG.API}${path}`, { cache: "no-store", ...options, signal: ctl.signal });
  } finally {
    clearTimeout(timer);
  }
}

// ── Verify: меню, горячая клавиша, оверлей ───────────────────────────────────

let lastCheck = null;   // что проверяли последним (показывает popup)

browser.contextMenus.create({
  id: "yandi-verify",
  title: "⬡ Verify with YANDI",
  contexts: ["selection"],
});

async function verifyInTab(tab, text) {
  text = (text || "").trim();
  if (!text || !tab || tab.id === undefined) return false;
  try {
    // activeTab даёт право внедрить скрипт в эту вкладку после клика по меню / нажатия клавиши
    await browser.tabs.executeScript(tab.id, { file: "content_verify.js" });
    await browser.tabs.sendMessage(tab.id, { type: "yandi_verify", text });
    return true;
  } catch (e) {
    // about:, addons.mozilla.org, PDF-просмотрщик и т.п. — туда расширениям внедряться нельзя
    console.warn("[YANDI] не удалось показать оверлей на этой странице:", e.message);
    browser.browserAction.setBadgeText({ text: "!" });
    browser.browserAction.setBadgeBackgroundColor({ color: "#b45309" });
    setTimeout(() => { browser.browserAction.setBadgeText({ text: serverUp === false ? "off" : "" }); }, 4000);
    return false;
  }
}

browser.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== "yandi-verify") return;
  verifyInTab(tab, info.selectionText);
});

browser.commands.onCommand.addListener(async (command) => {
  if (command !== "verify-selection") return;
  const [tab] = await browser.tabs.query({ active: true, currentWindow: true });
  if (!tab) return;
  try {
    const res = await browser.tabs.executeScript(tab.id, { code: "String(window.getSelection())" });
    await verifyInTab(tab, (res && res[0]) || "");
  } catch (e) {
    console.warn("[YANDI] не удалось прочитать выделение:", e.message);
  }
});

// Сообщения от собственных скриптов расширения (оверлей, popup)
browser.runtime.onMessage.addListener((msg, sender) => {
  if (!sender || sender.id !== browser.runtime.id || !msg) return undefined;
  if (msg.type === "yandi_ask") return handleAsk(msg.query);
  if (msg.type === "yandi_history") return handleHistory();
  if (msg.type === "yandi_note") { noteCheck(msg.note); return Promise.resolve({ ok: true }); }
  if (msg.type === "yandi_last") return Promise.resolve({ ok: true, last: lastCheck });
  return undefined;
});

function noteCheck(note) {
  if (!note || typeof note !== "object") return;
  lastCheck = {
    query: String(note.query || "").slice(0, 200),
    state: String(note.state || ""),
    trust: String(note.trust || ""),
    at: Date.now(),
  };
}

async function handleAsk(query) {
  query = String(query || "").trim();
  if (!query) return { ok: false, error: "пустой запрос" };
  try {
    const resp = await apiFetch("/api/orchestrator/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, enable_web: true }),
    }, YANDI_CONFIG.ASK_TIMEOUT_MS);
    if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}` };
    setServerState(true);
    return { ok: true, data: await resp.json() };
  } catch (e) {
    if (e.name !== "AbortError") setServerState(false);
    return { ok: false, error: e.name === "AbortError" ? "сервер не ответил вовремя" : e.message };
  }
}

async function handleHistory() {
  try {
    const resp = await apiFetch("/api/orch/history");
    if (!resp.ok) return { ok: false, error: `HTTP ${resp.status}` };
    return { ok: true, data: await resp.json() };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

const MODELS = {
  "claude":   ["claude.ai"],
  "gpt":      ["chatgpt.com"],
  "deepseek": ["chat.deepseek.com"],
  "kimi":     ["kimi.com", "www.kimi.com"],
};

const MODEL_URLS = {
  "claude":   "https://claude.ai/",
  "gpt":      "https://chatgpt.com/",
  "deepseek": "https://chat.deepseek.com/",
  "kimi":     "https://www.kimi.com/",
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

async function waitForTabComplete(tabId, timeoutMs = 15000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const tab = await browser.tabs.get(tabId);
      if (tab && tab.status === "complete") return true;
    } catch (_) {
      return false;
    }
    await sleep(250);
  }
  return false;
}

async function openIsolatedTab(model) {
  const url = MODEL_URLS[model];
  if (!url) return null;
  const tab = await browser.tabs.create({ url, active: false });
  await waitForTabComplete(tab.id);
  await sleep(1200);
  return tab;
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
    setServerState(true);
    if (!resp.ok) return;
    const task = await resp.json();
    if (task && task.task_id && !task.paused && hasTab) {
      busy[model] = true;
      handleTask(model, task).finally(() => { busy[model] = false; });
    }
  } catch (_) {
    setServerState(false);
  }
}

async function handleTask(model, task) {
  const { task_id, text } = task;
  const rawAcquisition = task && task.raw_acquisition === true;
  const myTab = rawAcquisition ? await openIsolatedTab(model) : await findTab(model);
  if (!myTab) {
    console.warn(`[Council Bridge] вкладка ${model} не найдена`);
    return;
  }
  let responseText;
  try {
    const r = await browser.tabs.sendMessage(myTab.id, {
      action: "send",
      text,
      task_id,
      request_id: task.request_id || "",
      raw_acquisition: rawAcquisition,
    });
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
