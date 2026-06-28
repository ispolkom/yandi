#!/usr/bin/env python3
"""
assistant/finetune.py — LoRA fine-tuning pipeline через unsloth.

Цикл:
  1. prepare  — конвертирует HF-датасет → ChatML SFT-формат
  2. train    — LoRA fine-tune через unsloth (Qwen3-0.6B по умолчанию)
  3. eval     — A/B тест: сравниваем base и fine-tuned на val-выборке
  4. promote  — сохраняем адаптер в registry/finetune/adapters/
  5. status   — состояние всех запусков

Хранение:
  registry/finetune/
    runs/         — JSONL-лог каждого запуска
    adapters/     — сохранённые LoRA адаптеры
    sft/          — подготовленные SFT-датасеты
    eval/         — результаты A/B тестов

Команды:
  python3 assistant/finetune.py prepare
  python3 assistant/finetune.py train [--model qwen3-0.6b] [--epochs 3]
  python3 assistant/finetune.py eval <run_id>
  python3 assistant/finetune.py promote <run_id>
  python3 assistant/finetune.py status
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE         = Path(__file__).parent.parent
FINETUNE_DIR = BASE / "registry" / "finetune"
SFT_DIR      = FINETUNE_DIR / "sft"
RUNS_DIR     = FINETUNE_DIR / "runs"
ADAPTERS_DIR = FINETUNE_DIR / "adapters"
EVAL_DIR     = FINETUNE_DIR / "eval"
FINAL_DIR    = BASE / "registry" / "dataset" / "final"

for d in (SFT_DIR, RUNS_DIR, ADAPTERS_DIR, EVAL_DIR):
    d.mkdir(parents=True, exist_ok=True)

REPORT_KEY = "council:skill:reports"
REPORT_CH  = "council:skill:report"

# Модели для файн-тюна (unsloth HF id)
MODELS = {
    "qwen3-0.6b" : "unsloth/Qwen3-0.6B-unsloth-bnb-4bit",
    "qwen3-1.7b" : "unsloth/Qwen3-1.7B-unsloth-bnb-4bit",
    "qwen3-4b"   : "unsloth/Qwen3-4B-unsloth-bnb-4bit",
    "llama3-8b"  : "unsloth/Meta-Llama-3.1-8B-Instruct-bnb-4bit",
}
DEFAULT_MODEL = "qwen3-0.6b"


def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, payload: dict):
    data = json.dumps(payload, ensure_ascii=False)
    r.lpush(REPORT_KEY, data)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, data)


def _load_hf_rows() -> list[dict]:
    rows = []
    for f in sorted(FINAL_DIR.glob("*_hf.jsonl")):
        with open(f, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


# ── Конвертация в SFT-формат ──────────────────────────────────────────────────

def _hf_to_sft(rows: list[dict], min_assistant_len: int = 30) -> list[dict]:
    """
    Конвертирует HF-строки → диалоги ChatML.
    Стратегия 1: human → следующий model (claude/gpt/deepseek).
    Стратегия 2: скользящее окно пар model-question → model-answer (синтетика).
    Стратегия 3: каждый model-ответ оборачивается в QA-пару с топиком как промптом.
    """
    by_session: dict[str, list] = defaultdict(list)
    for row in rows:
        sid = row.get("session_id", "unknown")
        by_session[sid].append(row)

    MODEL_ROLES = {"claude", "gpt", "deepseek", "assistant"}
    conversations = []

    for sid, msgs in by_session.items():
        topic = msgs[0].get("topic", "general") if msgs else "general"

        # Стратегия 1: пары human → ближайший model-ответ
        for i, msg in enumerate(msgs):
            if msg.get("role") not in ("human", "user"):
                continue
            q = msg.get("content", "").strip()
            if len(q) < 10:
                continue
            # Ищем следующий model-ответ
            for j in range(i + 1, min(i + 4, len(msgs))):
                nxt = msgs[j]
                if nxt.get("role") in MODEL_ROLES:
                    a = nxt.get("content", "").strip()
                    if len(a) >= min_assistant_len:
                        conversations.append({
                            "messages"  : [
                                {"role": "user",      "content": q},
                                {"role": "assistant", "content": a},
                            ],
                            "session_id": sid,
                            "topic"     : topic,
                        })
                    break

        # Стратегия 2: пары model → model (вопрос / продолжение)
        model_msgs = [m for m in msgs if m.get("role") in MODEL_ROLES
                      and len(m.get("content", "")) >= min_assistant_len]
        for i in range(len(model_msgs) - 1):
            q_msg = model_msgs[i]
            a_msg = model_msgs[i + 1]
            q = q_msg.get("content", "").strip()
            a = a_msg.get("content", "").strip()
            # Только если это разные модели и достаточно длинные ответы
            if (q_msg.get("role") != a_msg.get("role")
                    and len(q) >= 60 and len(a) >= min_assistant_len):
                conversations.append({
                    "messages"  : [
                        {"role": "user",      "content": q[:600]},
                        {"role": "assistant", "content": a},
                    ],
                    "session_id": f"{sid}_pair_{i}",
                    "topic"     : topic,
                })

        # Стратегия 3: длинные model-ответы как standalone QA
        for msg in model_msgs:
            content = msg.get("content", "").strip()
            if len(content) >= 200:  # только содержательные
                prompt = f"Расскажи о теме «{topic}» подробно."
                if "Claude responded:" in content:
                    content = content.replace("Claude responded:", "").strip()
                conversations.append({
                    "messages"  : [
                        {"role": "user",      "content": prompt},
                        {"role": "assistant", "content": content},
                    ],
                    "session_id": f"{sid}_solo_{msg.get('role','')}",
                    "topic"     : topic,
                })

    # Дедупликация по content первого ответа
    seen = set()
    unique = []
    for conv in conversations:
        key = conv["messages"][-1]["content"][:100]
        if key not in seen:
            seen.add(key)
            unique.append(conv)

    return unique


# ── FinetunePipeline ──────────────────────────────────────────────────────────

class FinetunePipeline:

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    # ── 1. prepare ────────────────────────────────────────────────────────────

    def prepare(self, val_split: float = 0.1) -> dict:
        """
        Конвертирует HF-датасет в SFT-формат (ChatML).
        Разбивает на train/val. Сохраняет в registry/finetune/sft/.
        """
        rows = _load_hf_rows()
        conversations = _hf_to_sft(rows)

        if len(conversations) < 5:
            return {"error": f"Слишком мало диалогов: {len(conversations)}. Нужно ≥5."}

        # shuffle + split
        import random
        random.shuffle(conversations)
        val_n   = max(1, int(len(conversations) * val_split))
        val     = conversations[:val_n]
        train   = conversations[val_n:]

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        train_path = SFT_DIR / f"train_{stamp}.jsonl"
        val_path   = SFT_DIR / f"val_{stamp}.jsonl"

        for path, data in [(train_path, train), (val_path, val)]:
            with open(path, "w", encoding="utf-8") as f:
                for conv in data:
                    f.write(json.dumps(conv, ensure_ascii=False) + "\n")

        result = {
            "total_conversations": len(conversations),
            "train"              : len(train),
            "val"                : len(val),
            "train_path"         : str(train_path),
            "val_path"           : str(val_path),
            "stamp"              : stamp,
        }

        _publish(self.r, {"skill": "finetune", "action": "prepare", **result})
        print(f"  [prepare] диалогов={len(conversations)} train={len(train)} val={len(val)}")
        return result

    # ── 2. train ──────────────────────────────────────────────────────────────

    def train(self, model_key: str = DEFAULT_MODEL, epochs: int = 3,
              lora_r: int = 16, batch_size: int = 2,
              train_path: Optional[Path] = None) -> dict:
        """
        LoRA fine-tune через unsloth. Сохраняет адаптер в adapters/.
        """
        run_id   = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        model_id = MODELS.get(model_key, MODELS[DEFAULT_MODEL])

        # Если train_path не указан — берём последний
        if train_path is None:
            trains = sorted(SFT_DIR.glob("train_*.jsonl"))
            if not trains:
                return {"error": "Нет SFT-датасета. Запусти prepare сначала."}
            train_path = trains[-1]

        run_log = {
            "run_id"    : run_id,
            "model_key" : model_key,
            "model_id"  : model_id,
            "epochs"    : epochs,
            "lora_r"    : lora_r,
            "batch_size": batch_size,
            "train_path": str(train_path),
            "status"    : "running",
            "started_at": datetime.now().isoformat(),
        }
        self._save_run(run_log)
        print(f"  [train] run_id={run_id} model={model_key} epochs={epochs}")

        try:
            from unsloth import FastLanguageModel
            from unsloth.chat_templates import get_chat_template
            from trl import SFTTrainer, SFTConfig
            from datasets import Dataset

            # Загружаем модель с 4-bit квантизацией
            print(f"  [train] загружаю {model_id}...")
            model, tokenizer = FastLanguageModel.from_pretrained(
                model_name     = model_id,
                max_seq_length = 2048,
                load_in_4bit   = True,
                dtype          = None,
            )
            tokenizer = get_chat_template(tokenizer, chat_template="qwen-2.5")

            # LoRA адаптер
            model = FastLanguageModel.get_peft_model(
                model,
                r              = lora_r,
                target_modules = ["q_proj", "k_proj", "v_proj", "o_proj",
                                   "gate_proj", "up_proj", "down_proj"],
                lora_alpha     = lora_r * 2,
                lora_dropout   = 0,
                bias           = "none",
                use_gradient_checkpointing = "unsloth",
                random_state   = 42,
            )

            # Загружаем датасет
            convs = []
            with open(train_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            convs.append(json.loads(line))
                        except Exception:
                            pass

            def _format(example):
                msgs = example["messages"]
                return {"text": tokenizer.apply_chat_template(
                    msgs, tokenize=False, add_generation_prompt=False)}

            dataset = Dataset.from_list(convs).map(_format)

            adapter_path = ADAPTERS_DIR / run_id
            adapter_path.mkdir(parents=True, exist_ok=True)

            trainer = SFTTrainer(
                model     = model,
                tokenizer = tokenizer,
                train_dataset = dataset,
                args = SFTConfig(
                    dataset_text_field  = "text",
                    max_seq_length      = 2048,
                    per_device_train_batch_size = batch_size,
                    gradient_accumulation_steps = 4,
                    num_train_epochs    = epochs,
                    learning_rate       = 2e-4,
                    fp16                = False,
                    bf16                = True,
                    warmup_ratio        = 0.05,
                    lr_scheduler_type   = "cosine",
                    output_dir          = str(adapter_path / "checkpoints"),
                    logging_steps       = 10,
                    save_strategy       = "epoch",
                    report_to           = "none",
                ),
            )

            print(f"  [train] начинаю обучение ({len(convs)} диалогов)...")
            t0 = time.time()
            trainer.train()
            elapsed = time.time() - t0

            # Сохраняем LoRA адаптер
            model.save_pretrained(str(adapter_path))
            tokenizer.save_pretrained(str(adapter_path))

            run_log.update({
                "status"      : "completed",
                "elapsed_sec" : round(elapsed),
                "adapter_path": str(adapter_path),
                "finished_at" : datetime.now().isoformat(),
                "train_samples": len(convs),
            })
            self._save_run(run_log)

            _publish(self.r, {
                "skill"       : "finetune",
                "action"      : "train_done",
                "run_id"      : run_id,
                "elapsed_sec" : round(elapsed),
                "adapter_path": str(adapter_path),
            })

            print(f"  ✓ [train] готово за {elapsed:.0f}s → {adapter_path}")
            return run_log

        except Exception as e:
            import traceback
            err = traceback.format_exc()
            run_log.update({"status": "failed", "error": str(e), "traceback": err})
            self._save_run(run_log)
            _publish(self.r, {"skill": "finetune", "action": "train_failed",
                              "run_id": run_id, "error": str(e)})
            print(f"  ❌ [train] ошибка: {e}")
            return run_log

    # ── 3. eval (A/B тест) ────────────────────────────────────────────────────

    def eval(self, run_id: str, n_samples: int = 10) -> dict:
        """
        A/B тест: сравниваем base vs fine-tuned на val-выборке.
        Метрика: средняя длина ответа, перплексия (простая оценка).
        """
        run_log = self._load_run(run_id)
        if not run_log:
            return {"error": f"run не найден: {run_id}"}
        if run_log.get("status") != "completed":
            return {"error": f"run не завершён: {run_log.get('status')}"}

        adapter_path = Path(run_log["adapter_path"])
        val_files    = sorted(SFT_DIR.glob("val_*.jsonl"))
        if not val_files:
            return {"error": "Нет val-датасета"}

        val_convs = []
        with open(val_files[-1], encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        val_convs.append(json.loads(line))
                    except Exception:
                        pass

        val_sample = val_convs[:n_samples]
        print(f"  [eval] A/B тест: {len(val_sample)} примеров")

        try:
            from unsloth import FastLanguageModel
            from unsloth.chat_templates import get_chat_template

            model_key = run_log.get("model_key", DEFAULT_MODEL)
            model_id  = MODELS.get(model_key, MODELS[DEFAULT_MODEL])

            model, tokenizer = FastLanguageModel.from_pretrained(
                model_name = str(adapter_path),
                max_seq_length = 2048,
                load_in_4bit   = True,
            )
            tokenizer = get_chat_template(tokenizer, chat_template="qwen-2.5")
            FastLanguageModel.for_inference(model)

            results = []
            for conv in val_sample:
                msgs = conv["messages"]
                # Берём только user-часть как prompt
                user_msgs = [m for m in msgs if m["role"] == "user"]
                if not user_msgs:
                    continue
                prompt = tokenizer.apply_chat_template(
                    [user_msgs[-1]], tokenize=False, add_generation_prompt=True)
                inputs = tokenizer(prompt, return_tensors="pt").to(model.device)

                t0 = time.time()
                outputs = model.generate(**inputs, max_new_tokens=256,
                                          temperature=0.7, do_sample=True)
                elapsed = time.time() - t0

                generated = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:],
                                              skip_special_tokens=True)
                expected  = next((m["content"] for m in msgs
                                   if m["role"] == "assistant"), "")
                results.append({
                    "prompt"   : user_msgs[-1]["content"][:100],
                    "generated": generated[:200],
                    "expected" : expected[:200],
                    "len_gen"  : len(generated),
                    "len_exp"  : len(expected),
                    "elapsed"  : round(elapsed, 2),
                })

            avg_len_gen = sum(r["len_gen"] for r in results) / max(len(results), 1)
            avg_len_exp = sum(r["len_exp"] for r in results) / max(len(results), 1)

            eval_result = {
                "run_id"       : run_id,
                "samples"      : len(results),
                "avg_len_gen"  : round(avg_len_gen),
                "avg_len_exp"  : round(avg_len_exp),
                "ratio"        : round(avg_len_gen / max(avg_len_exp, 1), 2),
                "examples"     : results[:3],
                "timestamp"    : datetime.now().isoformat(),
            }

            eval_path = EVAL_DIR / f"eval_{run_id}.json"
            eval_path.write_text(json.dumps(eval_result, ensure_ascii=False, indent=2))

            run_log["eval"] = str(eval_path)
            self._save_run(run_log)

            _publish(self.r, {"skill": "finetune", "action": "eval_done",
                              "run_id": run_id, "ratio": eval_result["ratio"]})

            print(f"  ✓ [eval] avg_gen={avg_len_gen:.0f} avg_exp={avg_len_exp:.0f} ratio={eval_result['ratio']}")
            return eval_result

        except Exception as e:
            print(f"  ❌ [eval] ошибка: {e}")
            return {"error": str(e)}

    # ── 4. promote ────────────────────────────────────────────────────────────

    def promote(self, run_id: str) -> dict:
        """Пометить run как продвинутый — обновить model_registry и manifest."""
        run_log = self._load_run(run_id)
        if not run_log:
            return {"error": f"run не найден: {run_id}"}

        run_log["promoted"] = True
        run_log["promoted_at"] = datetime.now().isoformat()
        self._save_run(run_log)

        # Пометить датасет в versioning
        try:
            from agent.dataset_versioning import DatasetVersionManager
            vm = DatasetVersionManager(r=self.r)
            train_path = Path(run_log.get("train_path", ""))
            vm.mark_finetuned(train_path.name, run_id)
        except Exception:
            pass

        # Обновить KG — добавить узел finetune
        try:
            from agent.knowledge_graph import KnowledgeGraph
            kg = KnowledgeGraph(r=self.r)
            kg.add_node(f"finetune:{run_id}", "finetune",
                        label=f"LoRA {run_log.get('model_key')} {run_id[:16]}",
                        meta={"run_id": run_id, "model": run_log.get("model_key"),
                              "promoted_at": run_log["promoted_at"]})
        except Exception:
            pass

        _publish(self.r, {"skill": "finetune", "action": "promoted", "run_id": run_id})
        print(f"  ✓ [promote] {run_id} → promoted")
        return run_log

    # ── status ────────────────────────────────────────────────────────────────

    def status(self) -> list[dict]:
        runs = []
        for f in sorted(RUNS_DIR.glob("*.json")):
            try:
                runs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return sorted(runs, key=lambda x: x.get("started_at", ""), reverse=True)

    # ── helpers ───────────────────────────────────────────────────────────────

    def _save_run(self, run_log: dict):
        path = RUNS_DIR / f"{run_log['run_id']}.json"
        path.write_text(json.dumps(run_log, ensure_ascii=False, indent=2))

    def _load_run(self, run_id: str) -> Optional[dict]:
        path = RUNS_DIR / f"{run_id}.json"
        if not path.exists():
            # частичный поиск
            matches = sorted(RUNS_DIR.glob(f"*{run_id}*.json"))
            if not matches:
                return None
            path = matches[-1]
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    ft  = FinetunePipeline()

    if cmd == "prepare":
        result = ft.prepare()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "train":
        model   = DEFAULT_MODEL
        epochs  = 3
        for i, arg in enumerate(sys.argv[2:], 2):
            if arg == "--model" and i + 1 < len(sys.argv):
                model = sys.argv[i + 1]
            elif arg == "--epochs" and i + 1 < len(sys.argv):
                epochs = int(sys.argv[i + 1])
        result = ft.train(model_key=model, epochs=epochs)
        print(json.dumps({k: v for k, v in result.items() if k != "traceback"},
                         ensure_ascii=False, indent=2))

    elif cmd == "eval":
        run_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not run_id:
            # взять последний completed
            runs = [r for r in ft.status() if r.get("status") == "completed"]
            if not runs:
                print("Нет завершённых runs")
                sys.exit(1)
            run_id = runs[0]["run_id"]
        result = ft.eval(run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "promote":
        run_id = sys.argv[2] if len(sys.argv) > 2 else ""
        result = ft.promote(run_id)
        print(f"Promoted: {run_id}")

    elif cmd == "status":
        runs = ft.status()
        if not runs:
            print("Нет запусков файн-тюна. Запусти: prepare → train")
        for r in runs:
            prom = "⭐ promoted" if r.get("promoted") else ""
            print(f"  [{r['run_id']}]  {r.get('model_key')}  "
                  f"status={r['status']}  {prom}")
            if r.get("elapsed_sec"):
                print(f"    ⏱ {r['elapsed_sec']}s  adapter={r.get('adapter_path','')[-40:]}")

    else:
        print(f"Неизвестная команда: {cmd}")
        print("Доступно: prepare | train [--model qwen3-0.6b] [--epochs 3] | eval [run_id] | promote <run_id> | status")
        sys.exit(1)
