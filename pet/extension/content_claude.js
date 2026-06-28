/**
 * content_claude.js — Council Bridge content script for Claude.ai.
 * Listens for {action:"send", text} from background, types into the editor,
 * waits for the full response, returns {text}.
 */

const INPUT_SEL  = ['div[contenteditable="true"].ProseMirror', 'div[contenteditable="true"]'];
const SEND_SEL   = ['button[aria-label="Send message"]', 'button[aria-label="Send Message"]',
                    'button[data-value="send"]'];
const RESP_SEL   = ['[data-testid="assistant-message"]', '.font-claude-message',
                    '[data-is-streaming]'];

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function findEl(selectors) {
  for (const s of selectors) {
    const el = document.querySelector(s);
    if (el) return el;
  }
  return null;
}

async function typeInto(el, text) {
  el.focus();
  await sleep(100);
  // clear
  document.execCommand("selectAll", false, null);
  await sleep(50);
  // paste via DataTransfer (works with ProseMirror)
  const dt = new DataTransfer();
  dt.setData("text/plain", text);
  el.dispatchEvent(new ClipboardEvent("paste", { clipboardData: dt, bubbles: true }));
  await sleep(200);
  // fallback: execCommand
  if (!el.innerText.includes(text.slice(0, 20))) {
    document.execCommand("selectAll", false, null);
    document.execCommand("insertText", false, text);
  }
}

async function clickSend(input) {
  const btn = findEl(SEND_SEL);
  if (btn && !btn.disabled) {
    btn.click();
  } else {
    input.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter", code: "Enter", keyCode: 13, bubbles: true
    }));
  }
}

async function waitForNewResponse(prevCount, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  // 1. wait for a new message element to appear
  while (Date.now() < deadline) {
    for (const sel of RESP_SEL) {
      if (document.querySelectorAll(sel).length > prevCount) {
        return await waitStable(sel, 2500, deadline);
      }
    }
    await sleep(400);
  }
  return "[timeout: нет ответа Claude.ai]";
}

async function waitStable(sel, stableMs, deadline) {
  let last = "", stableSince = null;
  while (Date.now() < deadline) {
    const els = document.querySelectorAll(sel);
    const cur = els.length ? els[els.length - 1].innerText.trim() : "";
    const now = Date.now();
    if (cur !== last) { last = cur; stableSince = now; }
    else if (stableSince && now - stableSince >= stableMs && cur) return cur;
    await sleep(350);
  }
  return last;
}

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.action !== "send") return;

  const input = findEl(INPUT_SEL);
  if (!input) return { text: "[Claude.ai: поле ввода не найдено]" };

  // count existing responses
  let prevCount = 0;
  for (const sel of RESP_SEL) {
    prevCount = Math.max(prevCount, document.querySelectorAll(sel).length);
  }

  await typeInto(input, msg.text);
  await sleep(300);
  await clickSend(input);

  const response = await waitForNewResponse(prevCount);
  return { text: response };
});
