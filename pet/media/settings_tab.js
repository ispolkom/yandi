/**
 * settings_tab.js — вкладка «⚙ YANDI» (настройки): какая модель отвечает («Голос») и какие дают мнение («Советники»).
 *
 * МАКЕТ: настройки сохраняются на сервере (файл в папке пользователя), но ещё не управляют ответами Помощницы;
 * ключ API форма принимает, но не сохраняет (хранение ключа ещё не решено). Всё, что приходит с диска и от сервера
 * (имена файлов, ошибки), попадает в страницу только через textContent — никакого innerHTML.
 *
 * Порядок вкладок: пока настройки не сохранены, вкладка ПЕРВАЯ и открывается сама; после «Применить» она уходит
 * в конец списка (в раскладке из трёх вкладок — на третье место), чтобы не мешать рабочим.
 */
(function () {
  "use strict";
  const KINDS = ["local", "remote", "api"];
  const TITLES = { local: "Локальная", remote: "Удалённая", api: "API" };
  const HELP = {
    local: "Файл модели на этом компьютере (формат .gguf), на любом диске и в любой папке.",
    remote: "Модель на другой машине: своя в сети или на своём сервере. Нужны адрес и имя модели.",
    api: "Сервис по API (для слабых компьютеров без своей модели). Нужны сервис, имя модели и ключ.",
  };

  let saved = false;
  let dirty = false;
  const $ = (id) => document.getElementById(id);

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function field(id, label, input) {
    const row = el("div", "st-row");
    const l = el("label", "", label);
    l.htmlFor = id;
    row.append(l, input);
    return row;
  }
  function textInput(id, placeholder, type) {
    const i = document.createElement("input");
    i.type = type || "text";
    i.id = id;
    i.placeholder = placeholder || "";
    i.autocomplete = "off";
    i.spellcheck = false;
    i.addEventListener("input", () => { dirty = true; setStatus("", ""); });
    return i;
  }
  function button(text, cls) {
    const b = el("button", cls || "tool-btn", text);
    b.type = "button";
    return b;
  }

  async function api(path, options) {
    const resp = await fetch(path, options);
    return resp.json();
  }

  // ── построение формы ───────────────────────────────────────────────────────
  function card(kind) {
    const box = el("div", "st-card");
    box.id = `st-card-${kind}`;
    const hdr = el("div", "st-card-hdr");
    hdr.appendChild(el("span", "st-title", TITLES[kind]));
    const voice = document.createElement("input");
    voice.type = "radio"; voice.name = "st-voice"; voice.id = `st-voice-${kind}`; voice.value = kind;
    const vl = el("label", "");
    vl.append(voice, document.createTextNode(" Голос"));
    vl.title = "Отвечает пользователю: формулирует готовый разбор";
    const adv = document.createElement("input");
    adv.type = "checkbox"; adv.id = `st-adv-${kind}`;
    const al = el("label", "");
    al.append(adv, document.createTextNode(" Советник"));
    al.title = "Его мнение запрашивается и записывается в базу";
    hdr.append(vl, al);
    voice.addEventListener("change", () => { dirty = true; markVoice(); setStatus("", ""); });
    adv.addEventListener("change", () => { dirty = true; setStatus("", ""); });
    box.appendChild(hdr);
    const body = el("div", "st-card-body");
    body.appendChild(el("div", "st-note", HELP[kind]));
    box.appendChild(body);
    return { box, body };
  }

  function build(root) {
    root.replaceChildren();
    const page = el("div", "st-page");
    page.appendChild(el("h2", "", "⚙ Настройки YANDI"));
    page.appendChild(el("div", "st-sub", "Выберите модели для работы:"));
    const current = el("div", "st-current");
    current.id = "st-current";
    page.appendChild(current);

    // Локальная
    const local = card("local");
    const path = textInput("st-local-path", "/путь/к/модели.gguf");
    const browse = button("📂 Обзор…");
    browse.id = "st-browse";
    browse.addEventListener("click", openBrowse);
    const check = button("Проверить");
    check.id = "st-local-check";
    check.addEventListener("click", checkLocal);
    const row = field("st-local-path", "Файл", path);
    row.append(browse, check);
    const res = el("div", "st-result");
    res.id = "st-local-result";
    local.body.append(row, res);

    // Удалённая
    const remote = card("remote");
    remote.body.append(
      field("st-remote-address", "Адрес", textInput("st-remote-address", "http://192.168.1.5:8080")),
      field("st-remote-model", "Модель", textInput("st-remote-model", "например qwen-14b")));
    const rc = button("Проверить");
    rc.disabled = true;
    rc.title = "Проверка подключения будет в следующем шаге";
    remote.body.appendChild(rc);

    // API
    const apiCard = card("api");
    const service = document.createElement("select");
    service.id = "st-api-service";
    [["openai", "OpenAI-совместимый"], ["anthropic", "Anthropic"], ["other", "Другой"]].forEach(([v, t]) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = t;
      service.appendChild(o);
    });
    service.addEventListener("change", () => { dirty = true; setStatus("", ""); });
    apiCard.body.append(
      field("st-api-service", "Сервис", service),
      field("st-api-model", "Модель", textInput("st-api-model", "например gpt-4o-mini")),
      field("st-api-key", "Ключ", textInput("st-api-key", "вставьте ключ API", "password")));
    apiCard.body.appendChild(el("div", "st-note", "Ключ пока не сохраняется: хранение ключа будет подключено отдельно."));
    const ac = button("Проверить");
    ac.disabled = true;
    ac.title = "Проверка подключения будет в следующем шаге";
    apiCard.body.appendChild(ac);

    page.append(local.box, remote.box, apiCard.box);

    const foot = el("div", "st-foot");
    const apply = button("💾 Применить", "tool-btn primary");
    apply.id = "st-apply";
    apply.addEventListener("click", applySettings);
    const status = el("span", "st-status");
    status.id = "st-status";
    foot.append(apply, status);
    page.appendChild(foot);
    page.appendChild(el("div", "st-note", "Голос — один: он формулирует ответ. Советники — любые: их мнения запрашиваются и записываются. " +
      "Итог (что совпало, где расходятся, какое доверие) считает код YANDI, а не модель."));
    page.appendChild(el("div", "st-mock", "МАКЕТ: настройки сохраняются, но пока не управляют ответами Помощницы."));
    root.appendChild(page);
  }

  // ── состояние формы ────────────────────────────────────────────────────────
  function setStatus(text, cls) {
    const s = $("st-status");
    if (!s) return;
    s.textContent = text;
    s.className = "st-status" + (cls ? " " + cls : "");
  }
  function markVoice() {
    KINDS.forEach((k) => $(`st-card-${k}`).classList.toggle("is-voice", $(`st-voice-${k}`).checked));
  }
  function readForm() {
    const voice = KINDS.find((k) => $(`st-voice-${k}`).checked) || "";
    return {
      voice,
      advisors: KINDS.filter((k) => $(`st-adv-${k}`).checked),
      local: { path: $("st-local-path").value.trim() },
      remote: { address: $("st-remote-address").value.trim(), model: $("st-remote-model").value.trim() },
      api: { service: $("st-api-service").value, model: $("st-api-model").value.trim() },
      // ключ ($("st-api-key")) намеренно НЕ отправляется
    };
  }
  function writeForm(cfg) {
    KINDS.forEach((k) => { $(`st-voice-${k}`).checked = cfg.voice === k; $(`st-adv-${k}`).checked = (cfg.advisors || []).includes(k); });
    $("st-local-path").value = (cfg.local || {}).path || "";
    $("st-remote-address").value = (cfg.remote || {}).address || "";
    $("st-remote-model").value = (cfg.remote || {}).model || "";
    $("st-api-service").value = (cfg.api || {}).service || "openai";
    $("st-api-model").value = (cfg.api || {}).model || "";
    markVoice();
  }
  function describe(cfg) {
    const one = (k) => {
      if (k === "local") return `Локальная · ${((cfg.local || {}).path || "").split("/").pop() || "—"}`;
      if (k === "remote") return `Удалённая · ${(cfg.remote || {}).model || "—"} @ ${(cfg.remote || {}).address || "—"}`;
      return `API · ${(cfg.api || {}).model || "—"}`;
    };
    return { voice: cfg.voice ? one(cfg.voice) : "", advisors: (cfg.advisors || []).map(one) };
  }
  function renderCurrent(cfg) {
    const box = $("st-current");
    if (!box) return;
    box.replaceChildren();
    if (!cfg.saved) {
      box.appendChild(document.createTextNode("Сейчас используется: настройки ещё не сохранены — выберите Голос и нажмите «Применить»."));
      return;
    }
    const d = describe(cfg);
    box.append(document.createTextNode("Сейчас используется: Голос — "), el("b", "", d.voice));
    box.appendChild(document.createTextNode(d.advisors.length ? `; советники: ${d.advisors.join(", ")}` : "; советников нет"));
  }

  // ── порядок вкладок ────────────────────────────────────────────────────────
  function applyTabOrder() {
    const tab = $("tab-settings");
    if (tab) tab.style.order = saved ? "100" : "-1";          // сохранено -> в конец; иначе первая
  }

  // ── действия ───────────────────────────────────────────────────────────────
  async function checkLocal() {
    const res = $("st-local-result");
    const path = $("st-local-path").value.trim();
    res.className = "st-result";
    res.textContent = "⏳ Проверяю…";
    try {
      const d = await api("/api/models/check", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ path }) });
      res.className = "st-result " + (d.ok ? "ok" : "bad");
      res.textContent = d.ok ? `✅ Файл найден, формат GGUF: ${d.file} (${d.size_gb} ГБ)` : `❌ ${d.error}`;
    } catch (e) {
      res.className = "st-result bad";
      res.textContent = `❌ ${e.message}`;
    }
  }

  async function applySettings() {
    setStatus("⏳ Сохраняю…", "");
    try {
      const d = await api("/api/ui/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(readForm()) });
      if (!d.ok) { setStatus(`❌ ${d.error}`, "bad"); return; }
      saved = true;
      dirty = false;
      $("st-api-key").value = "";                 // ключ не сохранён и не остаётся в поле
      renderCurrent(d);
      applyTabOrder();
      setStatus("✅ Сохранено. Вкладка «YANDI» перенесена в конец списка.", "ok");
    } catch (e) {
      setStatus(`❌ ${e.message}`, "bad");
    }
  }

  // ── окно «Обзор» ───────────────────────────────────────────────────────────
  let modal = null;
  function closeBrowse() { if (modal) { modal.remove(); modal = null; } }

  function openBrowse() {
    closeBrowse();
    modal = el("div", "modal open");
    const box = el("div", "modal-box br-box");
    const hdr = el("div", "modal-hdr");
    hdr.appendChild(el("h3", "", "📂 Выбор файла модели (.gguf)"));
    const x = el("button", "modal-x", "✕");
    x.type = "button";
    x.addEventListener("click", closeBrowse);
    hdr.appendChild(x);
    const body = el("div", "br-body");

    const bar = el("div", "br-bar");
    const up = button("⬆ Вверх");
    const where = document.createElement("input");
    where.type = "text"; where.id = "br-path"; where.spellcheck = false;
    const go = button("Перейти");
    bar.append(up, where, go);
    const short = el("div", "br-short");
    const list = el("div", "br-list");
    list.id = "br-list";
    const err = el("div", "br-err");
    err.id = "br-err";
    const hideLabel = el("label", "br-hide");
    const showHidden = document.createElement("input");
    showHidden.type = "checkbox";
    hideLabel.append(showHidden, document.createTextNode(" показывать скрытые папки"));
    body.append(bar, short, list, err, hideLabel);
    box.append(hdr, body);
    modal.appendChild(box);
    modal.addEventListener("mousedown", (e) => { if (e.target === modal) closeBrowse(); });
    document.body.appendChild(modal);

    let parent = null;
    async function load(path) {
      err.textContent = "";
      let d;
      try {
        d = await api(`/api/models/browse?path=${encodeURIComponent(path || "")}&hidden=${showHidden.checked}`);
      } catch (e) { err.textContent = e.message; return; }
      if (!d.ok) { err.textContent = d.error; return; }
      where.value = d.path;
      parent = d.parent;
      up.disabled = !parent;
      short.replaceChildren(...(d.shortcuts || []).map((s) => {
        const b = button(s.label);
        b.addEventListener("click", () => load(s.path));
        return b;
      }));
      const rows = d.entries.map((en) => {
        const row = el("div", "br-item" + (en.kind === "gguf" ? " gguf" : ""));
        row.append(el("span", "", en.kind === "dir" ? "📁" : "🧠"), el("span", "", en.name));
        if (en.size != null) row.appendChild(el("span", "br-size", `${(en.size / 1024 ** 3).toFixed(2)} ГБ`));
        row.addEventListener("click", () => {
          if (en.kind === "dir") { load(d.path.replace(/\/$/, "") + "/" + en.name); return; }
          $("st-local-path").value = d.path.replace(/\/$/, "") + "/" + en.name;
          dirty = true;
          setStatus("", "");
          $("st-local-result").textContent = "";
          closeBrowse();
        });
        return row;
      });
      list.replaceChildren(...(rows.length ? rows : [el("div", "br-item", "Здесь нет подпапок и файлов .gguf")]));
      if (d.truncated) err.textContent = "Показана только часть списка (слишком много файлов).";
    }
    up.addEventListener("click", () => parent && load(parent));
    go.addEventListener("click", () => load(where.value.trim()));
    where.addEventListener("keydown", (e) => { if (e.key === "Enter") load(where.value.trim()); });
    showHidden.addEventListener("change", () => load(where.value.trim()));
    const start = $("st-local-path").value.trim();
    load(start.includes("/") ? start.slice(0, start.lastIndexOf("/")) || "/" : "");
  }

  // ── запуск ─────────────────────────────────────────────────────────────────
  async function load(initial) {
    const root = $("msgs-settings");
    if (!root) return;
    build(root);
    let cfg = { saved: false, voice: "", advisors: [], local: {}, remote: {}, api: {} };
    try { cfg = await api("/api/ui/settings"); } catch (_) { /* сервер недоступен: как в первый запуск */ }
    saved = cfg.saved === true;
    writeForm(cfg);
    renderCurrent(cfg);
    applyTabOrder();
    if (typeof switchMode !== "function" || typeof currentMode === "undefined") return;
    // Первый запуск: настройки открываются сами, пока их не сохранили
    if (!saved && currentMode !== "settings") switchMode("settings");
    // Страница помнит последнюю вкладку, но сохранённые настройки сами не открываются: рабочая вкладка не должна
    // каждый раз уступать место настройкам
    else if (saved && initial && currentMode === "settings") switchMode("orch");
  }

  window.SettingsTab = {
    onEnter() { if (!dirty) load(); },
  };
  load(true);
})();
