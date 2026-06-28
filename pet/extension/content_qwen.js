/**
 * content_qwen.js — Council Bridge content script for chat.qwen.ai
 */

const MODEL = "qwen";

const INPUT_SEL = [
  'textarea[placeholder]',
  'div[contenteditable="true"]',
  'textarea',
  '[class*="input"] textarea',
  '[class*="editor"]',
  '#chat-input',
];

const SEND_SEL = [
  'button[aria-label*="Send" i]',
  'button[aria-label*="发送" i]',
  'button[type="submit"]',
  '[class*="send-btn"]',
  '[class*="send"]',
  'div[role="button"][aria-label*="send" i]',
];

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function findInput() {
  for (const s of INPUT_SEL) {
    const el = document.querySelector(s);
    if (el) return el;
  }
  return null;
}

async function typeInto(el, text) {
  el.focus();
  await sleep(150);
  if (el.tagName === "TEXTAREA") {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, "value");
    nativeSetter.set.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    const range = document.createRange();
    range.selectNodeContents(el);
    window.getSelection().removeAllRanges();
    window.getSelection().addRange(range);
    await sleep(50);
    document.execCommand("delete", false, null);
    await sleep(50);
    el.focus();
    const ok = document.execCommand("insertText", false, text);
    if (!ok || !(el.innerText || "").trim()) {
      const p = el.querySelector("p") || el;
      p.textContent = text;
      el.dispatchEvent(new InputEvent("input", { inputType: "insertText", data: text, bubbles: true }));
    }
  }
  await sleep(200);
}

async function clickSend(input) {
  for (const s of SEND_SEL) {
    const btn = document.querySelector(s);
    if (btn && !btn.disabled) { btn.click(); return; }
  }
  input.dispatchEvent(new KeyboardEvent("keydown", {
    key: "Enter", code: "Enter", keyCode: 13, bubbles: true
  }));
}

function getConvoText() {
  const candidates = [
    document.querySelector('main'),
    document.querySelector('[class*="chat"]'),
    document.querySelector('[class*="conversation"]'),
    document.querySelector('[class*="message-list"]'),
    document.body,
  ];
  for (const el of candidates) {
    if (el && el.innerText.length > 100) return el.innerText;
  }
  return document.body.innerText;
}

async function waitStableText(prevText, stableMs, deadline) {
  let last = getConvoText(), stableSince = Date.now();
  while (Date.now() < deadline) {
    await sleep(500);
    const cur = getConvoText();
    if (cur !== last) { last = cur; stableSince = Date.now(); }
    else if (Date.now() - stableSince >= stableMs) {
      const diff = last.slice(prevText.length).trim();
      if (diff.length > 20) return diff;
    }
  }
  return last.slice(prevText.length).trim() || "[timeout]";
}

async function waitForNewResponse(prevText, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const cur = getConvoText();
    if (cur.length > prevText.length + 100) {
      return await waitStableText(prevText, 5000, deadline);
    }
    await sleep(400);
  }
  return "[timeout: нет ответа Qwen]";
}

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.action !== "send") return;
  const input = findInput();
  if (!input) return { text: "[Qwen: поле ввода не найдено]" };
  const prevText = getConvoText();
  await typeInto(input, msg.text);
  await sleep(400);
  await clickSend(input);
  const response = await waitForNewResponse(prevText);
  return { text: response };
});
