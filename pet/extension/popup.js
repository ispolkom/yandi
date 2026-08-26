const API = "http://127.0.0.1:9010";

const MODEL_NAMES = {
  claude: "Claude", gpt: "GPT", deepseek: "DeepSeek", kimi: "Kimi", qwen: "Qwen"
};

async function loadStatus() {
  try {
    const resp = await fetch(`${API}/api/council/connections`, { cache: "no-store" });
    const data = await resp.json();
    const el = document.getElementById("models-list");
    el.innerHTML = Object.entries(data).map(([k, v]) => `
      <div class="model">
        <div class="dot ${v.connected ? 'on' : 'off'}"></div>
        <span class="model-name">${MODEL_NAMES[k] || k}</span>
        ${v.connected ? `<span class="last-seen">${v.last_seen_sec}s ago</span>` : ""}
      </div>
    `).join("");
  } catch (e) {
    document.getElementById("models-list").innerHTML =
      `<div style="color:#f87171; font-size:12px;">⚠ YANDI not running on port 9010</div>`;
  }
}

async function ask() {
  const query = document.getElementById("query").value.trim();
  if (!query) return;
  const btn = document.getElementById("ask-btn");
  btn.disabled = true;
  btn.textContent = "Asking…";

  try {
    const resp = await fetch(`${API}/api/orchestrator/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, enable_web: true }),
    });
    const data = await resp.json();

    const result = document.getElementById("result");
    result.style.display = "block";

    const colors = { VERIFIED: "#4ade80", HYPOTHESIS: "#facc15", PERSONAL: "#f87171" };
    const trust = data.trust_level || "UNKNOWN";
    const badge = document.getElementById("trust-badge");
    badge.innerHTML = `<span class="trust-badge" style="background:${colors[trust] || '#374151'}20;
      color:${colors[trust] || '#888'}; border:1px solid ${colors[trust] || '#374151'}">
      ${trust}
    </span>`;

    document.getElementById("answer-text").textContent = data.answer || "No answer";
  } catch (e) {
    document.getElementById("result").style.display = "block";
    document.getElementById("answer-text").textContent = "Error: " + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = "Ask YANDI";
  }
}

document.getElementById("ask-btn").addEventListener("click", ask);
document.getElementById("query").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.ctrlKey) ask();
});

loadStatus();
