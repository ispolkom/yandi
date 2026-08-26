/**
 * content_deepseek.js — Council Bridge content script for chat.deepseek.com.
 */

const INPUT_SEL = [
  'textarea#chat-input',
  'textarea[placeholder]',
  'div[contenteditable="true"]',
  '.chat-input textarea',
  'textarea',
];

const SEND_SEL = [
  'button[aria-label*="Send" i]',
  'button[type="submit"]',
  '.send-button',
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
    // Native textarea
    const nativeSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, "value"
    );
    nativeSetter.set.call(el, text);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  } else {
    // contenteditable
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

// Считает блоки ответа DeepSeek в DOM
function countMd() {
  return document.querySelectorAll('.ds-markdown, .ds-markdown--block').length;
}

// Возвращает текст ВСЕХ новых блоков начиная с startCount
function getNewMdText(startCount) {
  const els = Array.from(document.querySelectorAll('.ds-markdown, .ds-markdown--block'));
  return els.slice(startCount).map(el => el.innerText.trim()).filter(t => t).join("\n");
}

// Ждём пока новые md-блоки появятся и стабилизируются
async function waitForNewResponse(mdBefore, timeoutMs = 120000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (countMd() > mdBefore) {
      return await waitStableMd(mdBefore, 5000, deadline);
    }
    await sleep(500);
  }
  return "[timeout: нет ответа DeepSeek]";
}

async function waitStableMd(startCount, stableMs, deadline) {
  let last = "", stableSince = Date.now();
  while (Date.now() < deadline) {
    const cur = getNewMdText(startCount);
    if (cur !== last) { last = cur; stableSince = Date.now(); }
    else if (cur && Date.now() - stableSince >= stableMs) return cur;
    await sleep(400);
  }
  return last || "[timeout]";
}

browser.runtime.onMessage.addListener(async (msg) => {
  if (msg.action !== "send") return;

  const input = findInput();
  if (!input) return { text: "[DeepSeek: поле ввода не найдено]" };

  // Запоминаем сколько md-блоков было ДО нашей отправки
  const mdBefore = countMd();

  await typeInto(input, msg.text);
  await sleep(400);
  await clickSend(input);

  const response = await waitForNewResponse(mdBefore);
  return { text: response };
});
