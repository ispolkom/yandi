/**
 * content_gpt.js — Council Bridge content script for ChatGPT.com.
 */

const RESP_SEL = [
  '[data-message-author-role="assistant"] .markdown',
  '[data-message-author-role="assistant"]',
  '.agent-turn .markdown',
  '.markdown.prose',
];

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function findInput() {
  // Try known selectors in order of reliability
  const selectors = [
    '#prompt-textarea',
    'div[contenteditable="true"][data-virtual-keyboard-dismissal]',
    'div[contenteditable="true"][role="textbox"]',
    'div[contenteditable="true"]',
  ];
  for (const s of selectors) {
    const el = document.querySelector(s);
    if (el) return el;
  }
  return null;
}

async function typeInto(el, text) {
  el.focus();
  await sleep(150);

  // Select all existing content and delete
  const range = document.createRange();
  range.selectNodeContents(el);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  await sleep(50);
  document.execCommand('delete', false, null);
  await sleep(100);

  // Method 1: execCommand insertText (works in most contenteditable)
  el.focus();
  const ok = document.execCommand('insertText', false, text);
  if (ok && (el.innerText || el.textContent || "").trim()) return;

  // Method 2: set innerText of inner <p> + dispatch input event
  await sleep(50);
  const p = el.querySelector('p') || el;
  p.textContent = text;
  el.dispatchEvent(new InputEvent('input', {
    inputType: 'insertText',
    data: text,
    bubbles: true,
    composed: true,
  }));
  await sleep(100);

  // Method 3: clipboard paste simulation
  if (!(el.innerText || el.textContent || "").trim()) {
    const dt = new DataTransfer();
    dt.setData('text/plain', text);
    el.dispatchEvent(new ClipboardEvent('paste', {
      clipboardData: dt,
      bubbles: true,
      cancelable: true,
    }));
  }
}

async function clickSend() {
  const selectors = [
    'button[data-testid="send-button"]',
    'button[aria-label="Send prompt"]',
    'button[aria-label="Send message"]',
    'button[aria-label*="Send"]',
    'button[type="submit"]',
  ];
  for (const s of selectors) {
    const btn = document.querySelector(s);
    if (btn && !btn.disabled) {
      btn.click();
      return true;
    }
  }
  // fallback: Enter
  document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {
    key: 'Enter', code: 'Enter', keyCode: 13, bubbles: true,
  }));
  return false;
}

async function waitForNewResponse(prevCount, timeoutMs = 90000) {
  const deadline = Date.now() + timeoutMs;
  // wait for a new message to appear
  while (Date.now() < deadline) {
    for (const sel of RESP_SEL) {
      if (document.querySelectorAll(sel).length > prevCount) {
        return await waitStable(sel, 2500, deadline);
      }
    }
    await sleep(400);
  }
  return '[timeout: нет ответа ChatGPT]';
}

async function waitStable(sel, stableMs, deadline) {
  let last = '', stableSince = null;
  while (Date.now() < deadline) {
    const els = document.querySelectorAll(sel);
    const cur = els.length ? els[els.length - 1].innerText.trim() : '';
    const now = Date.now();
    if (cur !== last) { last = cur; stableSince = now; }
    else if (stableSince && now - stableSince >= stableMs && cur) return cur;
    await sleep(350);
  }
  return last;
}

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.action !== 'send') return;

  const input = findInput();
  if (!input) return { text: '[ChatGPT: поле ввода не найдено — нет активного чата?]' };

  let prevCount = 0;
  for (const sel of RESP_SEL) {
    prevCount = Math.max(prevCount, document.querySelectorAll(sel).length);
  }

  await typeInto(input, msg.text);
  await sleep(400);
  await clickSend();

  const response = await waitForNewResponse(prevCount);
  return { text: response };
});
