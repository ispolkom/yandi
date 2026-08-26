#!/usr/bin/env python3
"""
assistant/daemon.py — постоянный процесс-ассистент.

Роль: мои руки, не мозг.
  - логирует всё что происходит в совете → registry/council/sessions/
  - выполняет команды из канала council:daemon:control
  - делегирует задачи моделям через qwen3/deepseek/gemma4
  - НЕ принимает решений — это делаю я (Claude Code)

Управление из Claude Code:
  redis-cli PUBLISH council:daemon:control '{"cmd":"status"}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"search","query":"..."}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"scribe","text":"..."}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"broadcast","text":"вопрос"}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"session_save","topic":"название"}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"pause"}'
  redis-cli PUBLISH council:daemon:control '{"cmd":"resume"}'

Запуск:
  python3 assistant/daemon.py
  python3 assistant/daemon.py --verbose
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen, ProxyHandler, build_opener

import redis
import requests
import yaml
sys.path.insert(0, str(Path(__file__).parent.parent))
from agent.local_http import local_get, local_post
from agent.orchestrator import Orchestrator
from agent.browser_manager import BrowserManager
from agent.skills import ShellSkill, CodeSearchSkill
from agent.watcher import FileWatcher
from agent.knowledge_graph import KnowledgeGraph

# ── константы ─────────────────────────────────────────────────────────────────

BASE          = Path(__file__).parent.parent
CONFIG_PATH   = BASE / "reader" / "config.yaml"
SESSION_DIR   = BASE / "registry" / "council" / "sessions"
FLOOD_DIR     = BASE / "registry" / "flood"
SEARCH_MOD    = BASE / "scripts" / "search_quality_test.py"

REDIS_HOST    = "127.0.0.1"
REDIS_PORT    = 6379
CHAT_CH       = "council:chat:pubsub"
CTRL_CH       = "council:daemon:control"
STATUS_KEY    = "council:daemon:status"
MESSAGES_KEY  = "council:chat:messages"
MONITOR_LIST  = "council:monitor:events"   # список последних событий (история)
MONITOR_CH    = "council:monitor:pubsub"   # pubsub канал для ./ctl monitor

COUNCIL_API   = "http://127.0.0.1:9010"
OLLAMA        = "http://localhost:11434"
PROXIES       = {"http": None, "https": None}

MODEL_NAMES   = {"claude": "Claude", "gpt": "GPT", "deepseek": "DeepSeek", "human": "Человек"}

SESSION_DIR.mkdir(parents=True, exist_ok=True)
FLOOD_DIR.mkdir(parents=True, exist_ok=True)

# ── загрузка конфига ───────────────────────────────────────────────────────────

def load_cfg() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── логирование сессии совета ──────────────────────────────────────────────────

class SessionLog:
    """Пишет сессию совета в один markdown-файл. Без LLM — просто append."""

    def __init__(self):
        self.path: Path | None = None
        self.topic: str = "untitled"
        self.messages: list[dict] = []

    def start(self, topic: str = "untitled"):
        self.topic   = topic
        slug         = re.sub(r"[^\w]", "_", topic.lower())[:40]
        ts           = datetime.now().strftime("%Y-%m-%d_%H-%M")
        self.path    = SESSION_DIR / f"{ts}_{slug}.md"
        self.messages = []
        self.path.write_text(
            f"---\ntopic: {topic}\ndate: {datetime.now().strftime('%Y-%m-%d')}\n"
            f"status: open\nparticipants: [claude, gpt, deepseek]\n---\n\n"
            f"# Сессия: {topic}\n\n",
            encoding="utf-8"
        )
        return str(self.path)

    def append(self, msg: dict):
        if not self.path:
            self.start()
        who   = msg.get("from", "?")
        name  = MODEL_NAMES.get(who, who)
        ts    = msg.get("ts", datetime.now().strftime("%H:%M"))
        text  = msg.get("text", "")
        self.messages.append(msg)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"## [{ts}] {name}\n\n{text}\n\n---\n\n")

    def close(self, summary: str = ""):
        if not self.path:
            return
        with self.path.open("a", encoding="utf-8") as f:
            f.write(f"## Итог сессии\n\n{summary or '*(нет summary)*'}\n")
        old  = self.path
        done = self.path.parent / self.path.name.replace(".md", "_done.md")
        old.rename(done)
        self.path = done
        return str(done)


# ── API helpers ────────────────────────────────────────────────────────────────

_no_proxy_opener = build_opener(ProxyHandler({}))

def post_json(url: str, data: dict) -> dict:
    payload = json.dumps(data).encode()
    req = Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with _no_proxy_opener.open(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}

# ── основной цикл ─────────────────────────────────────────────────────────────

class Daemon:
    def __init__(self, verbose: bool = False):
        self.verbose        = verbose
        self.session        = SessionLog()
        self.paused         = False
        self.r              = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
        self.token_warned   = set()
        self.TOKEN_WARN     = 600_000
        self.last_tok_check = 0
        self.orch           = Orchestrator()
        self.browser        = BrowserManager(log_fn=self.log)
        self.browser.snapshot_existing()
        self.shell          = ShellSkill(self.r)
        self.code_search    = CodeSearchSkill(self.r)
        self.watcher        = FileWatcher(self.r, log_fn=self.log)
        self.watcher.start()
        self.kg             = KnowledgeGraph(r=self.r)
        from agent.decision_tracker import DecisionTracker
        self.dt             = DecisionTracker(r=self.r)
        from agent.orch_tracer import OrchestratorTracer
        self.orch_tracer    = OrchestratorTracer()

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def set_status(self, status: str):
        self.r.set(STATUS_KEY, status, ex=600)

    # ── мониторинг токенов ────────────────────────────────────────────────────

    def check_tokens(self):
        now = time.time()
        if now - self.last_tok_check < 30:
            return
        self.last_tok_check = now
        try:
            resp = local_get(f"{COUNCIL_API}/api/council/tokens", timeout=3)
            tokens = resp.json().get("tokens", {})
            for model, counts in tokens.items():
                total = counts.get("sent", 0) + counts.get("recv", 0)
                pct   = int(total / self.TOKEN_WARN * 100)
                if total >= self.TOKEN_WARN and model not in self.token_warned:
                    self.token_warned.add(model)
                    self.log(f"⚠️  ТОКЕНЫ {model.upper()}: {total:,} (~{pct}% порога)")
                    self.log(f"   ❗ НУЖНО ПЕРЕСОЗДАТЬ ЧАТ {model.upper()}!")
                    self.log(f"   Пауза включена автоматически.")
                    post_json(f"{COUNCIL_API}/api/council/pause", {})
                    self.paused = True
                    if self.browser.running:
                        self.log("   🔄 Перезапускаю браузеры для новой сессии...")
                        self.browser.reset()
                elif total >= self.TOKEN_WARN * 0.8 and model not in self.token_warned:
                    self.log(f"⚡ Токены {model}: {total:,} ({pct}%) — скоро предел")
        except Exception:
            pass

    # ── обработка сообщений совета ─────────────────────────────────────────────

    def _publish_event(self, event: dict):
        """Публикует событие в список + pubsub канал для ./ctl monitor."""
        payload = json.dumps(event, ensure_ascii=False)
        self.r.lpush(MONITOR_LIST, payload)
        self.r.ltrim(MONITOR_LIST, 0, 99)   # храним последние 100
        self.r.publish(MONITOR_CH, payload)

    def on_chat(self, data: dict):
        etype = data.get("type")

        if etype == "message":
            who  = data.get("from", "?")
            text = data.get("text", "")
            name = MODEL_NAMES.get(who, who)

            if self.verbose or who != "human":
                self.log(f"💬 {name}: {text[:80]}...")

            self.session.append(data)

            if who in ("claude", "gpt", "deepseek"):
                # публикуем событие — ./ctl monitor увидит сразу
                self._publish_event({
                    "type":    "model_response",
                    "from":    who,
                    "preview": text[:150],
                    "ts":      datetime.now().strftime("%H:%M:%S"),
                })
                # ── маяк: обновляем ключ для внешнего наблюдателя ────────────
                notify = {
                    "ts":      datetime.now().isoformat(),
                    "from":    who,
                    "len":     len(text),
                    "preview": text[:300],
                    "session": self.session.topic,
                }
                self.r.set("council:notify:last", json.dumps(notify, ensure_ascii=False))
                # Считаем ответы по сессии — маякуем если все 3 модели ответили
                skey = f"council:notify:session_count:{self.session.topic}"
                self.r.sadd(skey, who)
                self.r.expire(skey, 3600)
                count = self.r.scard(skey)
                if count >= 3:
                    self.log("  🔔 ВСЕ 3 МОДЕЛИ ОТВЕТИЛИ — council:notify:all_responded")
                    self.r.set("council:notify:all_responded", json.dumps({
                        "ts": datetime.now().isoformat(),
                        "session": self.session.topic,
                    }, ensure_ascii=False))
                    self.r.delete(skey)
                    # авто-анализ: строим цепочку действий в фоне
                    self._run_bg(self._auto_chain_build)
                self.check_tokens()

        elif etype == "tokens":
            self.check_tokens()

        elif etype == "status":
            who   = data.get("who", "?")
            state = data.get("state", "?")
            if self.verbose:
                self.log(f"  ◦ {MODEL_NAMES.get(who, who)}: {state}")

    # ── обработка команд управления ───────────────────────────────────────────

    def _run_bg(self, fn, *args, **kwargs):
        """Запустить тяжёлую задачу в фоновом потоке — не блокировать pubsub."""
        import threading
        t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True)
        t.start()

    def _auto_chain_build(self):
        """Авто-анализ совета → второй датасет (цепочка действий)."""
        try:
            from agent.council_chain_builder import CouncilChainBuilder
            builder = CouncilChainBuilder(r=self.r)
            self.log("  🔗 [chain] анализирую ответы совета через Qwen3...")
            chain = builder.process_last(verbose=False)
            if chain:
                action = "?"
                try:
                    action = json.loads(chain["messages"][2]["content"]).get("recommended_action", "?")
                except Exception:
                    pass
                self.log(f"  ✓ [chain] {chain['thread_id']} | {chain['topic']} | action={action} | q={chain['quality']}")
                if chain.get("next_question"):
                    self.log(f"  💡 [chain] следующий вопрос: {chain['next_question'][:100]}")
            else:
                self.log("  [chain] тред не найден или мало ответов")
        except Exception as e:
            self.log(f"  ❌ [chain] авто-анализ: {e}")

    def on_control(self, raw: str):
        try:
            cmd = json.loads(raw)
        except Exception:
            self.log(f"[ctrl] плохой JSON: {raw}")
            return

        action = cmd.get("cmd", "")
        self.log(f"[ctrl] {action}")

        if action == "status":
            msgs = len(self.session.messages)
            self.log(f"  сессия: {self.session.topic}, {msgs} сообщений, paused={self.paused}")

        elif action == "clear_chat":
            result = post_json(f"{COUNCIL_API}/api/council/clear", {})
            if result.get("ok"):
                self.log("  🗑 история чата очищена")
            else:
                self.log(f"  ❌ ошибка очистки: {result}")

        elif action == "pause":
            self.paused = True
            post_json(f"{COUNCIL_API}/api/council/pause", {})
            self.log("  ⏸ пауза")

        elif action == "resume":
            self.paused = False
            post_json(f"{COUNCIL_API}/api/council/resume", {})
            self.log("  ▶ возобновлено")

        elif action == "broadcast":
            text = cmd.get("text", "")
            if text:
                # Unified Context: подтягиваем релевантные узлы KG
                enriched = text
                if not cmd.get("no_kg"):
                    try:
                        kg_neighbors = self.kg.related(text[:40], limit=5)
                        if kg_neighbors:
                            ctx_parts = [f"{n['label']} ({n['rel']})" for n in kg_neighbors[:3]]
                            ctx_hint = "; ".join(ctx_parts)
                            enriched = f"{text}\n\n[Контекст из памяти: {ctx_hint}]"
                    except Exception:
                        pass
                post_json(f"{COUNCIL_API}/api/council/broadcast", {"text": enriched})
                self.log(f"  📢 broadcast: {text[:60]}")

        elif action == "orch":
            task   = cmd.get("task", "")
            ctx    = cmd.get("context", "")
            self.log(f"  [orch] {task[:60]}...")
            t0     = time.time()
            result = self.orch.run(task, ctx)
            elapsed_ms = int((time.time() - t0) * 1000)
            self.log(f"  → {result['task_type']} / {result['model']}")
            self.log(f"  → {result['final'][:200]}")
            # записываем трейс для датасета оркестратора
            outcome = "fail" if result["final"].startswith("[error") else "success"
            self.orch_tracer.trace(
                task=task, task_type=result["task_type"], model=result["model"],
                result=result["final"], context=ctx, outcome=outcome,
                elapsed_ms=elapsed_ms, steps=result.get("steps"),
            )
            # кидаем результат в чат как системное сообщение
            post_json(f"{COUNCIL_API}/reply", {
                "from": "human",
                "text": f"[Оркестратор | {result['task_type']} → {result['model']}]\n\n{result['final']}",
                "turn_next": "human",
            })

        elif action == "decisions":
            self.log(self.orch.show_decisions())

        elif action == "session_start":
            topic = cmd.get("topic", "untitled")
            path  = self.session.start(topic)
            self.log(f"  📁 сессия начата: {path}")

        elif action == "session_save":
            topic = cmd.get("topic", self.session.topic)
            self.session.topic = topic
            self.log("  📊 генерирую summary...")
            summary = self.orch.summarize(self.session.messages, self.verbose)
            path = self.session.close(summary)
            self.log(f"  ✓ сессия сохранена: {path}")
            self.session = SessionLog()
            # авто-датасет после каждого сохранения сессии
            try:
                from agent.dataset_pipeline import DatasetPipeline
                dp = DatasetPipeline()
                result = dp.run(verbose=False)
                self.log(f"  📦 датасет обновлён: {result['hf_total_rows']} строк HF")
            except Exception as e:
                self.log(f"  [dataset] пропущено: {e}")
            # авто-индексация в KG
            def _kg_index_session():
                try:
                    sid   = path.stem if path else topic
                    msgs  = self.session.messages if hasattr(self.session, 'messages') else []
                    new_n = self.kg.auto_index_session(str(sid), topic, msgs)
                    self.log(f"  🔗 KG: сессия {sid[:20]} → {new_n} новых узлов")
                except Exception as e:
                    self.log(f"  [kg] авто-индекс: {e}")
            self._run_bg(_kg_index_session)

        elif action == "scribe":
            text   = cmd.get("text", "")
            result = self.orch.scribe(text, self.verbose)
            self.log(f"  ✓ scribe: {result}")

        elif action == "search":
            query  = cmd.get("query", "")
            result = self.orch.search(query, self.verbose)
            post_json(f"{COUNCIL_API}/reply", {
                "from": "human",
                "text": f"[Поиск: {query}]\n\n{result}",
                "turn_next": "human",
            })
            self.log(f"  ✓ search done, {len(result)} chars")

        elif action == "browser_open":
            model    = cmd.get("model")     # None = все три
            headless = cmd.get("headless", False)
            result   = self.browser.start(model=model, headless=headless)
            if result:
                for m, pid in result.items():
                    self.log(f"  🌐 {m} PID {pid}")
            else:
                self.log("  ❌ браузеры не запустились")

        elif action == "browser_close":
            model = cmd.get("model")        # None = все наши
            self.browser.stop(model=model)
            self.log(f"  🌐 {'все браузеры' if not model else model} закрыты")

        elif action == "browser_reset":
            model    = cmd.get("model")
            headless = cmd.get("headless", False)
            result   = self.browser.reset(model=model, headless=headless)
            if result:
                for m, pid in result.items():
                    self.log(f"  🔄 {m} перезапущен PID {pid}")
            else:
                self.log("  ❌ браузеры не перезапустились")

        elif action == "browser_setup":
            model = cmd.get("model")
            self.log(f"  🔐 SETUP {'всех' if not model else model} — ожидаю ручного логина...")
            self.log("     (демон заблокирован пока не закроешь браузер)")
            self.browser.setup(model=model)
            self.log("  ✓ setup завершён")

        elif action == "browser_status":
            self.browser.log_status()

        elif action == "block":
            who = cmd.get("who", "")
            post_json(f"{COUNCIL_API}/api/council/state", {f"{who}_blocked": True})
            self.log(f"  🔒 заблокирован: {who}")

        elif action == "unblock":
            who = cmd.get("who", "")
            post_json(f"{COUNCIL_API}/api/council/state", {f"{who}_blocked": False})
            self.log(f"  🔓 разблокирован: {who}")

        elif action == "shell":
            shell_cmd = cmd.get("run", cmd.get("cmd", ""))
            timeout   = cmd.get("timeout", 30)
            self.log(f"  [shell] $ {shell_cmd[:80]}")
            result = self.shell.run(shell_cmd, timeout=timeout)
            status = result.get("status", "?")
            out    = result.get("output", "")[:300]
            self.log(f"  [shell] {status}: {out}")

        elif action == "code_search":
            query   = cmd.get("query", "")
            pattern = cmd.get("pattern")        # None = авто из query
            mode    = cmd.get("mode", "search") # search | def | usage
            exts    = cmd.get("extensions")     # ["py","js"] или None
            reset   = cmd.get("reset", False)
            self.log(f"  [code_search] {mode}: {query[:60]}")
            if mode == "def":
                result = self.code_search.find_definition(pattern or query)
            elif mode == "usage":
                result = self.code_search.find_usage(pattern or query, exts)
            else:
                result = self.code_search.search(query, pattern, exts, reset)
            found = result.get("found_count", 0)
            files = result.get("found_files", [])[:5]
            self.log(f"  [code_search] найдено: {found} файлов → {files}")
            if result.get("report_file"):
                self.log(f"  [code_search] отчёт: {Path(result['report_file']).name}")

        elif action == "cluster":
            min_size = cmd.get("min_cluster_size", 2)
            self.log("  [cluster] → фон")
            def _do_cluster():
                try:
                    from agent.embeddings import TopicClusterer
                    result = TopicClusterer(self.r).cluster(min_cluster_size=min_size, verbose=self.verbose)
                    self.log(f"  ✓ cluster: тем={result.get('topics_found',0)} шум={result.get('noise_count',0)}")
                except Exception as e:
                    self.log(f"  ❌ cluster: {e}")
            self._run_bg(_do_cluster)

        elif action == "semantic_index":
            self.log("  [faiss] строю индекс → фон")
            def _do_index():
                try:
                    from agent.embeddings import SemanticSearch
                    result = SemanticSearch(self.r).build_index(verbose=self.verbose)
                    self.log(f"  ✓ faiss: {result.get('rows',0)} строк dim={result.get('dim',0)}")
                except Exception as e:
                    self.log(f"  ❌ faiss: {e}")
            self._run_bg(_do_index)

        elif action == "semantic_search":
            query = cmd.get("query", "")
            top_k = cmd.get("top_k", 5)
            self.log(f"  [faiss] ищу → фон: {query[:60]}")
            def _do_search(q=query, k=top_k):
                try:
                    from agent.embeddings import SemanticSearch
                    results = SemanticSearch(self.r).search(q, top_k=k)
                    for r_ in results[:3]:
                        self.log(f"  [{r_['score']:.3f}] {r_.get('session_id','?')}: {r_.get('content','')[:100]}")
                except Exception as e:
                    self.log(f"  ❌ faiss search: {e}")
            self._run_bg(_do_search)

        elif action == "validate":
            self.log("  [validate] → фон (90s, не блокирует демон)")
            def _do_validate():
                try:
                    from agent.embeddings import DatasetValidator
                    result = DatasetValidator(self.r).validate(verbose=self.verbose)
                    self.log(f"  ✓ validate: keep={result.get('keep',0)} reject={result.get('reject',0)}")
                except Exception as e:
                    self.log(f"  ❌ validate: {e}")
            self._run_bg(_do_validate)

        elif action == "sampler":
            sub = cmd.get("sub", "run")
            send_req = cmd.get("request", False)
            self.log(f"  [sampler] {sub} request={send_req} → фон")
            def _do_sampler(s=sub, req=send_req):
                try:
                    from agent.active_sampler import ActiveSampler
                    sampler = ActiveSampler(self.r)
                    if s == "analyze":
                        info = sampler.analyze()
                        topics = info["topics"]
                        sparse = info["needs_sampling"]
                        self.log(f"  ✓ sampler: тем={len(topics)} дефицит={list(sparse.keys())}")
                    elif s == "synthetic":
                        topic = cmd.get("topic", "council")
                        rows = sampler.generate_synthetic(topic)
                        path = sampler._save_synthetic(rows, topic)
                        self.log(f"  ✓ sampler synthetic [{topic}]: {len(rows)} строк → {path.name}")
                    else:
                        result = sampler.run(send_requests=req)
                        sparse = list(result["analysis"]["needs_sampling"].keys())
                        self.log(f"  ✓ sampler: синтетика={result['synthetic_rows']} пробелы={sparse}")
                except Exception as e:
                    self.log(f"  ❌ sampler: {e}")
            self._run_bg(_do_sampler)

        elif action == "failures":
            sub = cmd.get("sub", "stats")
            self.log(f"  [failures] {sub}")
            def _do_failures(s=sub):
                try:
                    from agent.failure_collector import FailureCollector
                    fc = FailureCollector(r=self.r)
                    if s == "stats":
                        st = fc.stats()
                        self.log(f"  ✓ failures: всего={st['total']} источники={st['sources']}")
                    elif s == "add":
                        bad    = cmd.get("bad", "")
                        reason = cmd.get("reason", "")
                        corrected = cmd.get("corrected", "")
                        fc.add(bad, reason, corrected=corrected, source="manual",
                               topic=cmd.get("topic", "unknown"))
                        self.log(f"  ✓ failure добавлен: {bad[:50]}")
                except Exception as e:
                    self.log(f"  ❌ failures: {e}")
            self._run_bg(_do_failures)

        elif action == "finetune":
            sub = cmd.get("sub", "status")
            self.log(f"  [finetune] {sub} → фон")
            def _do_finetune(s=sub):
                try:
                    from agent.finetune import FinetunePipeline
                    ft = FinetunePipeline(r=self.r)
                    if s == "prepare":
                        result = ft.prepare()
                        self.log(f"  ✓ finetune prepare: диалогов={result.get('total_conversations')} train={result.get('train')} val={result.get('val')}")
                    elif s == "train":
                        model_key = cmd.get("model", "qwen3-0.6b")
                        epochs    = int(cmd.get("epochs", 3))
                        result    = ft.train(model_key=model_key, epochs=epochs)
                        self.log(f"  ✓ finetune train: {result.get('run_id')} status={result.get('status')} elapsed={result.get('elapsed_sec')}s")
                    elif s == "eval":
                        run_id = cmd.get("run_id", "")
                        if not run_id:
                            runs = [r for r in ft.status() if r.get("status") == "completed"]
                            run_id = runs[0]["run_id"] if runs else ""
                        if run_id:
                            result = ft.eval(run_id)
                            self.log(f"  ✓ finetune eval: ratio={result.get('ratio')} samples={result.get('samples')}")
                        else:
                            self.log("  [finetune] нет completed runs для eval")
                    elif s == "promote":
                        run_id = cmd.get("run_id", "")
                        ft.promote(run_id)
                        self.log(f"  ✓ finetune promote: {run_id}")
                    else:
                        runs = ft.status()
                        self.log(f"  ✓ finetune status: {len(runs)} запусков")
                        for r in runs[:3]:
                            self.log(f"    [{r['run_id'][-16:]}] {r.get('model_key')} {r.get('status')} {'⭐' if r.get('promoted') else ''}")
                except Exception as e:
                    self.log(f"  ❌ finetune: {e}")
            self._run_bg(_do_finetune)

        elif action == "adversarial":
            sub   = cmd.get("sub", "status")
            claim = cmd.get("claim", "")
            topic = cmd.get("topic", "general")
            self.log(f"  [adversarial] {sub}: {claim[:50] or topic}")
            def _do_adversarial(s=sub, c=claim, t=topic):
                try:
                    from agent.adversarial import AdversarialProber
                    ap = AdversarialProber(r=self.r)
                    if s == "defend_claim":
                        result = ap.defend_claim(c, topic=t)
                        self.log(f"  ✓ adversarial: {result.get('debate_id')} rows={result.get('dataset_rows',0)}")
                    elif s == "defend_decision":
                        did    = cmd.get("decision_id", c)
                        result = ap.defend_decision(did)
                        self.log(f"  ✓ adversarial decision: {result.get('debate_id')}")
                    elif s == "stress_test":
                        result = ap.stress_test(t)
                        self.log(f"  ✓ adversarial stress: {result.get('debate_id')}")
                    else:
                        debates = ap.status()
                        self.log(f"  ✓ adversarial: {len(debates)} дебатов")
                except Exception as e:
                    self.log(f"  ❌ adversarial: {e}")
            self._run_bg(_do_adversarial)

        elif action == "versioning":
            sub = cmd.get("sub", "list")
            self.log(f"  [versioning] {sub}")
            def _do_versioning(s=sub):
                try:
                    from agent.dataset_versioning import DatasetVersionManager
                    vm = DatasetVersionManager(r=self.r)
                    if s == "stamp":
                        result = vm.stamp()
                        self.log(f"  ✓ versioning stamp: v{result.get('version')} {result.get('file')} +{result.get('diff_added')} -{result.get('diff_removed')}")
                    elif s == "stamp_all":
                        results = vm.stamp_all()
                        self.log(f"  ✓ versioning stamp_all: {len(results)} файлов")
                    elif s == "list":
                        versions = vm.list()
                        self.log(f"  ✓ versioning: {len(versions)} версий")
                        for v in versions:
                            ft = "✅" if v.get("fine_tuned") else ""
                            self.log(f"    v{v['version']} {v['file']} rows={v['rows']} {ft}")
                    elif s == "latest":
                        v = vm.latest()
                        if v:
                            self.log(f"  ✓ latest: v{v['version']} {v['file']} rows={v['rows']}")
                except Exception as e:
                    self.log(f"  ❌ versioning: {e}")
            self._run_bg(_do_versioning)

        elif action == "reflect":
            self.log("  [reflect] → фон (~10s)")
            def _do_reflect():
                try:
                    from agent.reflector import Reflector
                    rf      = Reflector(r=self.r)
                    results = rf.run(verbose=False)
                    summary = rf.summary()
                    self.log(f"  ✓ reflect: {summary}")
                    self.log(f"  📝 отчёт: {results['md_path']}")
                except Exception as e:
                    self.log(f"  ❌ reflect: {e}")
            self._run_bg(_do_reflect)

        elif action == "decision":
            sub = cmd.get("sub", "list")
            self.log(f"  [decision] {sub}")
            def _do_decision(s=sub):
                try:
                    if s == "add":
                        text     = cmd.get("text", "")
                        reason   = cmd.get("reason", "")
                        expected = cmd.get("expected", "")
                        if not text:
                            self.log("  [decision] текст обязателен")
                            return
                        rec = self.dt.add(text, reason=reason, expected=expected)
                        self.log(f"  ✓ decision add: {rec['id']} — {text[:60]}")
                    elif s == "close":
                        did     = cmd.get("id", "")
                        outcome = cmd.get("outcome", "")
                        rec = self.dt.close(did, outcome)
                        if rec:
                            self.log(f"  ✓ decision close: {rec['id']}")
                        else:
                            self.log(f"  [decision] не найдено: {did}")
                    elif s == "report":
                        rpt = self.dt.report()
                        self.log(f"  ✓ decisions: {rpt['total']} всего, {rpt['open']} открытых, {rpt['closed']} закрытых")
                        for d in rpt["open_list"][:5]:
                            self.log(f"    🔄 [{d['id'][-12:]}] {d['text'][:55]}")
                    else:
                        records = self.dt.list(status=s if s in ("open","closed","all") else "open")
                        self.log(f"  ✓ decisions [{s}]: {len(records)} записей")
                        for r in records[:5]:
                            self.log(f"    → {r['text'][:60]}")
                except Exception as e:
                    self.log(f"  ❌ decision: {e}")
            self._run_bg(_do_decision)

        elif action == "kg":
            sub = cmd.get("sub", "stats")
            self.log(f"  [kg] {sub} → фон")
            def _do_kg(s=sub):
                try:
                    if s == "stats":
                        st = self.kg.stats()
                        self.log(f"  ✓ KG: узлов={st['nodes']} рёбер={st['edges']} типы={list(st['node_types'].keys())}")
                    elif s == "index":
                        r1 = self.kg.index_datasets(verbose=False)
                        r2 = self.kg.index_sessions_md(verbose=False)
                        self.log(f"  ✓ KG index: +{r1['new_nodes']+r2['new_nodes']} узлов, итого={self.kg.G.number_of_nodes()}")
                    elif s == "query":
                        node  = cmd.get("node", "council")
                        depth = int(cmd.get("depth", 2))
                        result = self.kg.query(node, depth=depth)
                        neighbors = result.get("neighbors", [])
                        self.log(f"  ✓ KG query [{node}]: {len(neighbors)} соседей")
                        for nb in neighbors[:5]:
                            self.log(f"    → [{nb['type']}] {nb['label']} ({nb['rel']}, w={nb['weight']:.1f})")
                    elif s == "search":
                        q      = cmd.get("query", "")
                        results = self.kg.search(q, limit=10)
                        self.log(f"  ✓ KG search [{q}]: {len(results)} совпадений")
                        for r in results[:5]:
                            self.log(f"    → [{r['type']}] {r['label']}")
                    elif s == "add_decision":
                        text     = cmd.get("text", "")
                        reason   = cmd.get("reason", "")
                        expected = cmd.get("expected", "")
                        did      = f"dec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                        new = self.kg.add_decision(did, text, reason, expected)
                        self.log(f"  ✓ KG decision: {did} ({'новое' if new else 'уже есть'})")
                except Exception as e:
                    self.log(f"  ❌ kg: {e}")
            self._run_bg(_do_kg)

        elif action == "dataset":
            sub = cmd.get("sub", "run")
            self.log(f"  [dataset] {sub} → фон")
            def _do_dataset(s=sub):
                try:
                    from agent.dataset_pipeline import DatasetPipeline
                    dp = DatasetPipeline()
                    if s == "build":
                        path = dp.build(verbose=self.verbose)
                        self.log(f"  ✓ черновик: {path.name}")
                    elif s == "filter":
                        path = dp.filter(verbose=self.verbose)
                        self.log(f"  ✓ финал: {path.name}")
                    elif s == "stats":
                        st = dp.writer.stats()
                        self.log(f"  черновиков={st['drafts']} финалов={st['finals']} строк_HF={st['hf_total_rows']}")
                    else:
                        result = dp.run(verbose=self.verbose)
                        self.log(f"  ✓ dataset: draft={Path(result['draft']).name} HF={result['hf_total_rows']} строк")
                except Exception as e:
                    self.log(f"  ❌ dataset: {e}")
            self._run_bg(_do_dataset)

        elif action == "dashboard":
            sub = cmd.get("sub", "status")
            if sub == "start":
                import subprocess, os, sys
                env = os.environ.copy()
                env["DASHBOARD_PORT"] = str(cmd.get("port", 9011))
                proc = subprocess.Popen(
                    [sys.executable, str(BASE / "assistant" / "dashboard.py")],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self.r.set("council:dashboard:pid", proc.pid)
                self.log(f"  ✓ dashboard запущен pid={proc.pid} port={env['DASHBOARD_PORT']}")
            elif sub == "stop":
                pid = self.r.get("council:dashboard:pid")
                if pid:
                    import signal, os
                    try:
                        os.kill(int(pid), signal.SIGTERM)
                        self.r.delete("council:dashboard:pid")
                        self.log(f"  ✓ dashboard остановлен pid={pid}")
                    except ProcessLookupError:
                        self.r.delete("council:dashboard:pid")
                        self.log("  [dashboard] процесс уже завершён")
                else:
                    self.log("  [dashboard] нет запущенного процесса")
            else:
                pid = self.r.get("council:dashboard:pid")
                self.log(f"  [dashboard] pid={pid or 'нет'} http://127.0.0.1:9011")

        elif action == "policy":
            sub = cmd.get("sub", "stats")
            target = cmd.get("target", "")
            self.log(f"  [policy] {sub} {target}")
            def _do_policy(s=sub, t=target):
                try:
                    from agent.policy import PolicyEngine, CommandAudit
                    engine = PolicyEngine()
                    audit  = CommandAudit()
                    if s == "scan":
                        findings = engine.scanner.scan_dir(BASE) if not t else engine.scanner.scan_file(Path(t))
                        self.log(f"  ✓ policy scan: {len(findings)} совпадений{'  ✓ чисто' if not findings else ''}")
                        if findings:
                            for f in findings[:3]:
                                self.log(f"    ⚠ [{f['type']}] {f['source']}: {f['match'][:50]}")
                    elif s == "check":
                        result = engine.check_shell(t)
                        self.log(f"  ✓ policy check [{t[:40]}]: {'OK' if result['allowed'] else 'BLOCKED — ' + result['reason']}")
                    elif s == "audit":
                        records = audit.tail(10)
                        st = audit.stats()
                        self.log(f"  ✓ audit: total={st['total']} allowed={st['allowed']} blocked={st['blocked']}")
                    else:
                        st = audit.stats()
                        self.log(f"  ✓ policy stats: audit={st['total']} blocked={st.get('blocked',0)}")
                except Exception as e:
                    self.log(f"  ❌ policy: {e}")
            self._run_bg(_do_policy)

        elif action == "ast":
            sub = cmd.get("sub", "stats")
            target = cmd.get("target", "")
            self.log(f"  [ast] {sub} {target}")
            def _do_ast(s=sub, t=target):
                try:
                    from agent.ast_intelligence import ASTIndex
                    idx = ASTIndex()
                    if s == "index":
                        result = idx.build(verbose=False)
                        self.log(f"  ✓ ast index: {result['modules']} модулей {result['elapsed']}s")
                    elif s == "impact":
                        result = idx.impact(t)
                        affected = result.get("affected", [])
                        self.log(f"  ✓ ast impact [{t}]: затронуто {len(affected)} → {', '.join(affected[:5])}")
                    elif s == "deps":
                        result = idx.deps(t)
                        deps = result.get("imports", [])
                        self.log(f"  ✓ ast deps [{t}]: {len(deps)} зависимостей → {', '.join(deps[:5])}")
                    elif s == "callers":
                        result = idx.callers(t)
                        self.log(f"  ✓ ast callers [{t}]: {result.get('callers', [])}")
                    elif s == "find":
                        result = idx.find_definition(t)
                        self.log(f"  ✓ ast find [{t}]: {result}")
                    else:
                        result = idx.stats()
                        self.log(f"  ✓ ast stats: {result['total_modules']} модулей, {result['total_edges']} рёбер")
                except Exception as e:
                    self.log(f"  ❌ ast: {e}")
            self._run_bg(_do_ast)

        elif action == "chain":
            sub = cmd.get("sub", "last")
            self.log(f"  [chain] {sub} → фон")
            def _do_chain(s=sub):
                try:
                    from agent.council_chain_builder import CouncilChainBuilder
                    builder = CouncilChainBuilder(r=self.r)
                    if s == "last":
                        chain = builder.process_last(verbose=False)
                        if chain:
                            action_val = "?"
                            try:
                                action_val = json.loads(chain["messages"][2]["content"]).get("recommended_action", "?")
                            except Exception:
                                pass
                            self.log(f"  ✓ chain last: {chain['thread_id']} тема={chain['topic']} action={action_val} q={chain['quality']}")
                            if chain.get("next_question"):
                                self.log(f"  💡 след. вопрос: {chain['next_question'][:100]}")
                        else:
                            self.log("  [chain] тред не найден")
                    elif s == "run":
                        result = builder.run_all(verbose=False)
                        self.log(f"  ✓ chain run: обработано={result['processed']} пропущено={result['skipped']} треды={result['found_threads']}")
                    elif s == "stats":
                        st = builder.stats()
                        self.log(f"  ✓ chain stats: всего={st['total']} avg_q={st['avg_quality']}")
                        self.log(f"    темы={st['by_topic']}  действия={st['by_action']}")
                    elif s == "tail":
                        chains = builder.tail(3)
                        for c in chains:
                            self.log(f"  [{c['thread_id']}] {c['topic']} q={c['quality']}")
                except Exception as e:
                    self.log(f"  ❌ chain: {e}")
            self._run_bg(_do_chain)

        elif action == "orch_dataset":
            sub = cmd.get("sub", "stats")
            self.log(f"  [orch_dataset] {sub} → фон")
            def _do_orch_dataset(s=sub):
                try:
                    from agent.orch_dataset import OrchDatasetBuilder
                    builder = OrchDatasetBuilder()
                    if s == "export":
                        result = builder.export(verbose=False)
                        if result.get("status") == "empty":
                            self.log("  [orch_dataset] нет данных для экспорта")
                        else:
                            self.log(f"  ✓ orch_dataset export: orch={result.get('orch_rows',0)} скиллы={result.get('skills',[])}")
                    elif s == "review":
                        builder.review(n=3)
                    else:
                        st = builder.stats()
                        tr = st["traces"]
                        self.log(f"  ✓ orch_dataset stats: трейсов={tr['total']} success_rate={tr['success_rate']}")
                        self.log(f"  SFT файлов={st['sft_files']} строк={st['sft_rows']} экспортов={st['exports']}")
                        for k, v in st["targets"].items():
                            mark = "✅" if v["ready"] else f"❌ {v['have']}/{v['need']}"
                            self.log(f"    {k}: {mark}")
                except Exception as e:
                    self.log(f"  ❌ orch_dataset: {e}")
            self._run_bg(_do_orch_dataset)

        elif action == "orch_traces":
            sub = cmd.get("sub", "stats")
            n   = int(cmd.get("n", 10))
            self.log(f"  [orch_traces] {sub}")
            def _do_orch_traces(s=sub, _n=n):
                try:
                    from agent.orch_tracer import OrchestratorTracer
                    tracer = OrchestratorTracer()
                    if s == "tail":
                        traces = tracer.tail(_n)
                        for t in traces:
                            self.log(f"  [{t['ts'][:19]}] {t['task_type']}→{t['model']} [{t['outcome']}] q={t['quality']}: {t['task'][:60]}")
                    else:
                        st = tracer.stats()
                        self.log(f"  ✓ трейсов={st['total']} success_rate={st['success_rate']} avg_q={st['avg_quality']}")
                        self.log(f"  скиллы: {st['by_skill']}")
                except Exception as e:
                    self.log(f"  ❌ orch_traces: {e}")
            self._run_bg(_do_orch_traces)

        elif action == "file_search":
            # Поиск файлов по директориям, паттерну, типу
            # cmd: {"cmd":"file_search","dirs":["/path1","/path2"],"pattern":"*.md","result_key":"council:filesearch:result"}
            dirs       = cmd.get("dirs", [str(BASE)])
            pattern    = cmd.get("pattern", "*.md")
            name_like  = cmd.get("name", "")          # подстрока в имени
            result_key = cmd.get("result_key", "council:filesearch:result")
            self.log(f"  [file_search] pattern={pattern} dirs={dirs}")
            def _do_search(d=dirs, p=pattern, nl=name_like, rk=result_key):
                try:
                    from pathlib import Path as P
                    found = []
                    skip = {".git", "__pycache__", "node_modules", "target",
                            "embeddings_cache", "adapters", "browser_data",
                            ".venv", "venv", ".pytest_cache", ".cargo"}
                    for base_dir in d:
                        bp = P(base_dir)
                        if not bp.exists():
                            self.log(f"  [file_search] директория не найдена: {base_dir}")
                            continue
                        for f in bp.rglob(p):
                            if any(s in f.parts for s in skip):
                                continue
                            if nl and nl.lower() not in f.name.lower():
                                continue
                            found.append({
                                "path": str(f),
                                "name": f.name,
                                "size": f.stat().st_size,
                                "dir":  str(f.parent),
                            })
                    found.sort(key=lambda x: x["path"])
                    result = {
                        "dirs": d, "pattern": p, "name_filter": nl,
                        "total": len(found), "files": found,
                    }
                    import json as _j
                    self.r.set(rk, _j.dumps(result, ensure_ascii=False))
                    self.r.expire(rk, 3600)
                    self.log(f"  ✓ file_search: найдено {len(found)} файлов → redis[{rk}]")
                    for f in found[:5]:
                        self.log(f"    📄 {f['path']}")
                    if len(found) > 5:
                        self.log(f"    ... и ещё {len(found)-5}")
                except Exception as e:
                    self.log(f"  ❌ file_search: {e}")
            self._run_bg(_do_search)

        else:
            self.log(f"  [ctrl] неизвестная команда: {action}")

    # ── главный цикл ──────────────────────────────────────────────────────────

    def run(self):
        self.log("🤖 Daemon запущен")
        self.log(f"   Redis: {REDIS_HOST}:{REDIS_PORT}")
        self.log(f"   Каналы: {CHAT_CH}, {CTRL_CH}")
        self.log(f"   Сессия: {self.session.start('autostart')}")
        self.log("")
        self.log("Управление:")
        self.log("  redis-cli PUBLISH council:daemon:control '{\"cmd\":\"status\"}'")
        self.log("  redis-cli PUBLISH council:daemon:control '{\"cmd\":\"broadcast\",\"text\":\"вопрос\"}'")
        self.log("  redis-cli PUBLISH council:daemon:control '{\"cmd\":\"search\",\"query\":\"...\"}'")
        self.log("  redis-cli PUBLISH council:daemon:control '{\"cmd\":\"scribe\",\"text\":\"мысль\"}'")
        self.log("  redis-cli PUBLISH council:daemon:control '{\"cmd\":\"session_save\",\"topic\":\"тема\"}'")
        self.log("")

        self.set_status("online")

        pubsub = self.r.pubsub()
        pubsub.subscribe(CHAT_CH, CTRL_CH)

        last_ping = time.time()

        try:
            for raw in pubsub.listen():
                if raw["type"] != "message":
                    continue

                # keepalive
                if time.time() - last_ping > 60:
                    self.set_status("online")
                    last_ping = time.time()

                channel = raw.get("channel", "")
                data    = raw.get("data", "")

                if channel == CHAT_CH:
                    try:
                        self.on_chat(json.loads(data))
                    except Exception as e:
                        if self.verbose:
                            self.log(f"  [chat parse error] {e}")

                elif channel == CTRL_CH:
                    self.on_control(data)

        except KeyboardInterrupt:
            self.log("\n🛑 Остановка...")
            self.set_status("offline")
            if self.session.messages:
                summary = self.orch.summarize(self.session.messages, False)
                path    = self.session.close(summary)
                self.log(f"  сессия сохранена: {path}")

        except redis.exceptions.ConnectionError as e:
            self.log(f"[Redis] обрыв: {e}")
            sys.exit(1)


# ── точка входа ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # проверка Redis
    try:
        redis.Redis(host=REDIS_HOST, port=REDIS_PORT).ping()
    except Exception as e:
        print(f"[FATAL] Redis недоступен: {e}")
        sys.exit(1)

    Daemon(verbose=args.verbose).run()
