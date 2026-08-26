# -*- coding: utf-8 -*-
"""
Модуль вкладки «Верификация» для YANDI Council.
"""

REVIEW_HTML = """
<div id="review-container" style="display:flex;flex-direction:column;height:100%;padding:8px 12px">
  <div id="review-list" style="flex:1;overflow-y:auto">
    <!-- Список записей на проверку -->
  </div>
</div>
"""

REVIEW_JS = """
// ── Верификация ──────────────────────────────────────────────────────────────
let _reviewItems = [];
let _reviewActiveId = null;

function _rvSetInputLocked(locked) {
  // Блокируем только если нужно
}

function _rvClose() {
  if (_reviewActiveId) {
    const el = document.getElementById("review-msg-"+_reviewActiveId);
    if (el) el.remove();
    _reviewActiveId = null;
  }
}

function _rvUpdateCount() {
  const cnt = _reviewItems.length;
  const el = document.getElementById("review-count");
  if (el) el.textContent = cnt > 0 ? `(${cnt})` : "";
}

async function loadReviewQueue() {
  try {
    const d = await fetch("/api/review/list").then(r=>r.json());
    _reviewItems = d.items || [];
    _rvUpdateCount();
    const list = document.getElementById("review-list");
    if (!_reviewItems.length) {
      list.innerHTML = '<div style="padding:6px 10px;font-size:11px;color:var(--icq-text-muted)">Нет записей на проверку</div>';
      return;
    }
    list.innerHTML = _reviewItems.map((it,i) => `
      <div class="review-item" onclick="openReviewItem(${i})" style="cursor:pointer;padding:6px 10px;border-bottom:1px solid var(--icq-border)">
        <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(it.query.slice(0,55))}</div>
        <div style="color:var(--icq-text-muted);font-size:11px">${it.tag} · ${(it.confidence*100).toFixed(0)}%</div>
      </div>
    `).join("");
  } catch(e) {
    console.error("review load", e);
  }
}

function openReviewItem(idx) {
  const it = _reviewItems[idx];
  if (!it) return;
  _rvClose();
  _reviewActiveId = it.id;

  const panel = document.getElementById("msgs-review");
  const div = document.createElement("div");
  div.id = "review-msg-" + it.id;
  div.style.cssText = "border:2px solid var(--icq-border-dark);border-radius:6px;margin:8px 4px;padding:0;background:var(--icq-bg)";
  div.innerHTML = `
    <div style="padding:6px 10px 4px;background:var(--icq-panel);border-bottom:1px solid var(--icq-border);display:flex;justify-content:space-between;align-items:center">
      <span>📋 На проверку · <b>${escHtml(it.tag)}</b> · ${(it.confidence*100).toFixed(0)}%</span>
      <button onclick="_rvClose()" style="background:none;border:none;cursor:pointer;font-size:14px;color:var(--icq-text-muted);padding:0 2px;">✕</button>
    </div>
    <div style="padding:8px 10px 4px">
      <div style="font-weight:bold;font-size:13px;margin-bottom:6px">${escHtml(it.query)}</div>
      <div id="rv-answer-${it.id}" style="font-size:12px;color:var(--icq-text);white-space:pre-wrap;line-height:1.5;background:white;border:1px solid var(--icq-border);padding:6px 8px;border-radius:3px;min-height:40px;outline:none" contenteditable="false">${escHtml(it.answer)}</div>
    </div>
    <div style="padding:4px 10px 8px;display:flex;gap:6px">
      <button class="fb-btn" onclick="rvVerify('${it.id}',this)">✅ Верно</button>
      <button class="fb-btn" onclick="rvEdit('${it.id}',this)">✏ Исправить</button>
      <button class="fb-btn" id="rv-save-${it.id}" onclick="rvSave('${it.id}',this)" style="display:none">💾 Сохранить</button>
      <button class="fb-btn act-btn del" onclick="rvDelete('${it.id}',this)">🗑 Удалить</button>
    </div>
  `;
  panel.appendChild(div);
  div.scrollIntoView({block:"nearest",behavior:"smooth"});
}

async function rvVerify(id, btn) {
  btn.disabled = true;
  try {
    const j = await fetch("/api/review/verify", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})}).then(r=>r.json());
    if (j.ok) {
      btn.textContent = "✅";
      setTimeout(() => _rvAfterAction(id), 500);
    } else {
      btn.textContent = "❌";
    }
  } catch(e) {
    btn.textContent = "❌";
  }
}

function _rvAfterAction(id) {
  _reviewItems = _reviewItems.filter(x => x.id !== id);
  _rvUpdateCount();
  const el = document.getElementById("review-msg-"+id);
  if (el) el.remove();
  _rvClose();
  loadReviewQueue();
}

function rvEdit(id, btn) {
  const el = document.getElementById("rv-answer-"+id);
  if (!el) return;
  el.contentEditable = "true";
  el.style.outline = "2px solid var(--icq-header)";
  el.focus();
  btn.style.display = "none";
  const saveBtn = document.getElementById("rv-save-"+id);
  if (saveBtn) saveBtn.style.display = "";
}

async function rvSave(id, btn) {
  const el = document.getElementById("rv-answer-"+id);
  if (!el) return;
  const answer = el.innerText.trim();
  if (!answer) {
    btn.textContent = "❌ пусто";
    return;
  }
  btn.disabled = true;
  try {
    const j = await fetch("/api/review/update", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id, answer})}).then(r=>r.json());
    if (j.ok) {
      btn.textContent = "✅";
      setTimeout(() => _rvAfterAction(id), 500);
    } else {
      btn.textContent = "❌";
    }
  } catch(e) {
    btn.textContent = "❌";
  }
}

async function rvDelete(id, btn) {
  if (!confirm("Удалить запись из базы знаний?")) return;
  btn.disabled = true;
  try {
    const j = await fetch("/api/review/delete", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({id})}).then(r=>r.json());
    if (j.ok) {
      btn.textContent = "🗑";
      setTimeout(() => _rvAfterAction(id), 500);
    } else {
      btn.textContent = "❌";
    }
  } catch(e) {
    btn.textContent = "❌";
  }
}

// ── Загружаем список при открытии вкладки ──────────────────────────────────
document.addEventListener("DOMContentLoaded", function() {
  // Если вкладка уже активна — загружаем
  if (document.getElementById("tab-review").classList.contains("active")) {
    loadReviewQueue();
  }
});
"""
