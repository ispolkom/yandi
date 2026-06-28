#!/usr/bin/env python3
"""
assistant/dashboard.py — веб-дашборд состояния всей системы PET/Council.

Отдаёт HTML-страницу на порту 9011 (отдельно от чат-сервера на 9010).
Обновляется каждые 10s через SSE или авто-refresh.

Разделы:
  📊 Dataset        — draft/final/synthetic/adversarial/failures статистика
  🔗 Knowledge Graph — узлов/рёбер, топ-концепты
  🧠 Decisions       — открытые/закрытые решения
  🤖 Fine-tuning     — список runs, последний promoted
  🔍 Reflections     — последний отчёт
  ⚙️ Daemon          — статус демона, Redis-каналы
  📡 Models          — токены Claude/GPT/DeepSeek

Запуск:
  python3 assistant/dashboard.py
  # → http://127.0.0.1:9011
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE = Path(__file__).parent.parent

try:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse
    import uvicorn
    import redis as _redis
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False


# ── helpers ───────────────────────────────────────────────────────────────────

def _r():
    return _redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        return sum(1 for line in open(path, encoding="utf-8") if line.strip())
    except Exception:
        return 0


def _latest_file(pattern: str, directory: Path) -> Optional[Path]:
    files = sorted(directory.glob(pattern), reverse=True)
    return files[0] if files else None


# ── сбор данных ───────────────────────────────────────────────────────────────

def collect_stats() -> dict:
    r = _r()
    stats = {"timestamp": datetime.now().isoformat()}

    # ── Dataset ───────────────────────────────────────────────────────────────
    FINAL_DIR     = BASE / "registry" / "dataset" / "final"
    DRAFT_DIR     = BASE / "registry" / "dataset" / "draft"
    SYNTH_DIR     = BASE / "registry" / "dataset" / "synthetic"
    FAIL_DIR      = BASE / "registry" / "dataset" / "failures"
    ADV_DIR       = BASE / "registry" / "dataset" / "adversarial"
    MANIFEST      = BASE / "registry" / "dataset" / "manifest.json"

    hf_rows  = sum(_count_jsonl(f) for f in FINAL_DIR.glob("*_hf.jsonl"))
    hf_files = len(list(FINAL_DIR.glob("*_hf.jsonl")))
    draft_files = len(list(DRAFT_DIR.glob("*.jsonl"))) if DRAFT_DIR.exists() else 0
    synth_rows = sum(_count_jsonl(f) for f in SYNTH_DIR.glob("*.jsonl")) if SYNTH_DIR.exists() else 0
    fail_rows  = sum(_count_jsonl(f) for f in FAIL_DIR.glob("*.jsonl")) if FAIL_DIR.exists() else 0
    adv_rows   = sum(_count_jsonl(f) for f in ADV_DIR.glob("*_hf.jsonl")) if ADV_DIR.exists() else 0

    versions = []
    if MANIFEST.exists():
        try:
            versions = json.loads(MANIFEST.read_text())["versions"]
        except Exception:
            pass

    stats["dataset"] = {
        "hf_rows"    : hf_rows,
        "hf_files"   : hf_files,
        "draft_files": draft_files,
        "synthetic"  : synth_rows,
        "failures"   : fail_rows,
        "adversarial": adv_rows,
        "versions"   : len(versions),
        "latest_version": versions[-1].get("version") if versions else 0,
    }

    # ── Knowledge Graph ───────────────────────────────────────────────────────
    DB = BASE / "registry" / "knowledge" / "graph.db"
    if DB.exists():
        try:
            import sqlite3
            con = sqlite3.connect(DB)
            nodes = con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            edges = con.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
            top = con.execute(
                "SELECT label, COUNT(*) as deg FROM ("
                "  SELECT src as label FROM edges UNION ALL SELECT dst FROM edges"
                ") GROUP BY label ORDER BY deg DESC LIMIT 5"
            ).fetchall()
            node_types = dict(con.execute(
                "SELECT type, COUNT(*) FROM nodes GROUP BY type").fetchall())
            con.close()
            stats["kg"] = {
                "nodes": nodes, "edges": edges,
                "top_concepts": [{"label": r[0], "degree": r[1]} for r in top],
                "node_types": node_types,
            }
        except Exception as e:
            stats["kg"] = {"error": str(e)}
    else:
        stats["kg"] = {"nodes": 0, "edges": 0}

    # ── Decisions ─────────────────────────────────────────────────────────────
    DEC_FILE = BASE / "registry" / "decisions" / "decisions.jsonl"
    open_dec = closed_dec = 0
    open_list = []
    if DEC_FILE.exists():
        seen = {}
        for line in open(DEC_FILE, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "_patch_for" in rec:
                    if rec["_patch_for"] in seen:
                        seen[rec["_patch_for"]]["status"] = rec.get("status", "open")
                else:
                    seen[rec["id"]] = rec
            except Exception:
                pass
        for rec in seen.values():
            s = rec.get("status", "open")
            if s == "open":
                open_dec += 1
                open_list.append(rec.get("text", "")[:60])
            else:
                closed_dec += 1
    stats["decisions"] = {"open": open_dec, "closed": closed_dec, "open_list": open_list[:5]}

    # ── Finetune ──────────────────────────────────────────────────────────────
    RUNS_DIR = BASE / "registry" / "finetune" / "runs"
    runs = []
    if RUNS_DIR.exists():
        for f in sorted(RUNS_DIR.glob("*.json"), reverse=True)[:5]:
            try:
                runs.append(json.loads(f.read_text()))
            except Exception:
                pass
    stats["finetune"] = {
        "total": len(runs),
        "runs" : [{"id": r.get("run_id","")[-16:], "model": r.get("model_key",""),
                   "status": r.get("status",""), "elapsed": r.get("elapsed_sec","?"),
                   "promoted": r.get("promoted", False)} for r in runs],
    }

    # ── Reflections ───────────────────────────────────────────────────────────
    REFLECT_DIR = BASE / "registry" / "reflections"
    last_reflect = ""
    if REFLECT_DIR.exists():
        files = sorted(REFLECT_DIR.glob("*.md"), reverse=True)
        if files:
            try:
                last_reflect = files[0].read_text(encoding="utf-8")[:1500]
            except Exception:
                pass
    stats["reflections"] = {"last": last_reflect[:400] if last_reflect else "нет"}

    # ── Daemon / Redis ────────────────────────────────────────────────────────
    try:
        status = r.get("council:daemon:status") or "unknown"
        msgs   = r.llen("council:chat:messages")
        skill_rpts = r.llen("council:skill:reports")
        stats["daemon"] = {
            "status"      : status,
            "chat_msgs"   : msgs,
            "skill_reports": skill_rpts,
        }
    except Exception as e:
        stats["daemon"] = {"status": "redis_error", "error": str(e)}

    # ── Models tokens ─────────────────────────────────────────────────────────
    try:
        tokens = {}
        for m in ("claude", "gpt", "deepseek"):
            v = r.get(f"council:tokens:{m}")
            tokens[m] = int(v) if v else 0
        stats["tokens"] = tokens
    except Exception:
        stats["tokens"] = {}

    # ── Sessions ──────────────────────────────────────────────────────────────
    SESSION_DIR = BASE / "registry" / "council" / "sessions"
    sessions = sorted(SESSION_DIR.glob("*.md"), reverse=True)[:5] if SESSION_DIR.exists() else []
    stats["sessions"] = {
        "total": len(list(SESSION_DIR.glob("*.md"))) if SESSION_DIR.exists() else 0,
        "recent": [f.name for f in sessions],
    }

    return stats


# ── HTML шаблон ───────────────────────────────────────────────────────────────

def render_html(stats: dict) -> str:
    ds  = stats.get("dataset", {})
    kg  = stats.get("kg", {})
    dec = stats.get("decisions", {})
    ft  = stats.get("finetune", {})
    rfx = stats.get("reflections", {})
    dmn = stats.get("daemon", {})
    tok = stats.get("tokens", {})
    ses = stats.get("sessions", {})
    ts  = stats.get("timestamp", "")[:19]

    def card(title: str, content: str, color: str = "#1e1e2e") -> str:
        return f"""
        <div class="card" style="border-left: 4px solid {color}">
          <h3>{title}</h3>
          {content}
        </div>"""

    def row(label: str, value) -> str:
        return f'<div class="row"><span class="label">{label}</span><span class="value">{value}</span></div>'

    daemon_status = dmn.get("status", "?")
    status_color  = "#a6e3a1" if daemon_status == "online" else "#f38ba8"

    ds_content = (
        row("Финальных строк (HF)", f"<b>{ds.get('hf_rows',0)}</b>") +
        row("Версий", f"v{ds.get('latest_version',0)} / {ds.get('versions',0)} файлов") +
        row("Черновиков", ds.get("draft_files", 0)) +
        row("Синтетических", ds.get("synthetic", 0)) +
        row("Adversarial", ds.get("adversarial", 0)) +
        row("Провальных (failures)", ds.get("failures", 0))
    )

    kg_content = (
        row("Узлов", f"<b>{kg.get('nodes',0)}</b>") +
        row("Рёбер", kg.get("edges", 0)) +
        row("Типы", str(kg.get("node_types", {}))) +
        "<div class='sublabel'>Топ концептов:</div>" +
        "".join(f'<div class="tag">{c["label"]} ({c["degree"]})</div>'
                for c in kg.get("top_concepts", [])[:5])
    )

    dec_content = (
        row("Открытых", f'<b style="color:#f9e2af">{dec.get("open",0)}</b>') +
        row("Закрытых", dec.get("closed", 0)) +
        "<div class='sublabel'>Открытые:</div>" +
        "".join(f'<div class="todo">🔄 {d}</div>' for d in dec.get("open_list", []))
    )

    ft_runs = ft.get("runs", [])
    ft_content = (
        row("Всего runs", ft.get("total", 0)) +
        "".join(
            f'<div class="run {"promoted" if r["promoted"] else ""}">'
            f'{"⭐ " if r["promoted"] else ""}'
            f'[{r["id"]}] {r["model"]} — {r["status"]} {r["elapsed"]}s'
            f'</div>'
            for r in ft_runs
        )
    )

    tok_content = "".join(
        row(m.capitalize(), f"{v:,} токенов")
        for m, v in tok.items()
    )

    ses_content = (
        row("Всего сессий", ses.get("total", 0)) +
        "".join(f'<div class="session">{s}</div>' for s in ses.get("recent", []))
    )

    dmn_content = (
        row("Статус демона", f'<span style="color:{status_color}"><b>{daemon_status}</b></span>') +
        row("Сообщений в чате", dmn.get("chat_msgs", 0)) +
        row("Отчётов скиллов", dmn.get("skill_reports", 0))
    )

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="15">
  <title>PET/Council Dashboard</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Segoe UI', monospace;
      background: #11111b;
      color: #cdd6f4;
      padding: 20px;
    }}
    h1 {{ color: #89b4fa; margin-bottom: 4px; font-size: 1.4em; }}
    .ts {{ color: #585b70; font-size: 0.8em; margin-bottom: 20px; }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      gap: 16px;
    }}
    .card {{
      background: #1e1e2e;
      border-radius: 8px;
      padding: 16px;
    }}
    .card h3 {{ color: #89b4fa; margin-bottom: 12px; font-size: 1em; }}
    .row {{ display: flex; justify-content: space-between; margin-bottom: 6px; font-size: 0.85em; }}
    .label {{ color: #a6adc8; }}
    .value {{ color: #cdd6f4; font-family: monospace; }}
    .sublabel {{ color: #6c7086; font-size: 0.75em; margin: 8px 0 4px; }}
    .tag {{
      display: inline-block; background: #313244; border-radius: 4px;
      padding: 2px 8px; margin: 2px; font-size: 0.8em; color: #89dceb;
    }}
    .todo {{ color: #f9e2af; font-size: 0.82em; margin: 3px 0; padding-left: 8px; }}
    .session {{ color: #a6adc8; font-size: 0.78em; margin: 2px 0; font-family: monospace; }}
    .run {{ font-size: 0.82em; margin: 3px 0; color: #a6adc8; font-family: monospace; }}
    .run.promoted {{ color: #a6e3a1; }}
    a {{ color: #89b4fa; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>🧠 PET/Council — Dashboard</h1>
  <div class="ts">Обновлено: {ts} · <a href="/">↻ обновить</a> · автообновление каждые 15s</div>
  <div class="grid">
    {card("📊 Dataset", ds_content, "#89b4fa")}
    {card("🔗 Knowledge Graph", kg_content, "#a6e3a1")}
    {card("🧠 Decisions", dec_content, "#f9e2af")}
    {card("🤖 Fine-tuning", ft_content, "#cba6f7")}
    {card("📡 Модели (токены)", tok_content, "#89dceb")}
    {card("📁 Сессии совета", ses_content, "#fab387")}
    {card("⚙️ Демон / Redis", dmn_content, "#f38ba8")}
    {card("🔍 Последняя рефлексия", f'<pre style="font-size:0.72em;white-space:pre-wrap;color:#a6adc8">{rfx.get("last","—")}</pre>', "#74c7ec")}
  </div>
</body>
</html>"""


# ── FastAPI app ───────────────────────────────────────────────────────────────

if HAS_FASTAPI:
    app = FastAPI(title="PET Dashboard", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        stats = collect_stats()
        return render_html(stats)

    @app.get("/api/stats")
    async def api_stats():
        return collect_stats()


def main():
    if not HAS_FASTAPI:
        print("fastapi/uvicorn не установлены. pip install fastapi uvicorn")
        return
    port = int(os.environ.get("DASHBOARD_PORT", 9011))
    print(f"[dashboard] http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "stats":
        print(json.dumps(collect_stats(), ensure_ascii=False, indent=2))
    else:
        main()
