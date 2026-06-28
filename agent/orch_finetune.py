"""
assistant/orch_finetune.py — LoRA Fine-Tuning рецепт для оркестратора.

Требования:
  - GPU с ≥16GB VRAM (A100 / RTX 4090)
  - unsloth: pip install unsloth
  - Модели: Qwen3:7B (исполнители) / Qwen3:14B (оркестратор)

Режимы:
  python3 assistant/orch_finetune.py prepare          — подготовить датасет
  python3 assistant/orch_finetune.py train --model 7b — запустить обучение
  python3 assistant/orch_finetune.py eval             — A/B тест base vs fine-tuned
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE    = Path(__file__).parent.parent
SFT_DIR = BASE / "registry" / "dataset" / "orch_sft"
OUT_DIR = BASE / "registry" / "dataset" / "orch_finetune"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ── Конфиг ───────────────────────────────────────────────────────────────────

CONFIGS = {
    "7b": {
        "base_model":    "unsloth/Qwen3-7B-unsloth-bnb-4bit",
        "dataset":       SFT_DIR / "orch_train.jsonl",
        "output_dir":    OUT_DIR / "qwen3-7b-orch-lora",
        "max_seq_len":   2048,
        "lora_r":        16,
        "lora_alpha":    32,
        "lora_dropout":  0.05,
        "target_modules": ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
        "learning_rate": 2e-4,
        "per_device_train_batch_size": 2,
        "gradient_accumulation_steps": 4,
        "num_train_epochs": 3,
        "warmup_ratio":    0.03,
        "fp16": True,
    },
    "14b": {
        "base_model":    "unsloth/Qwen3-14B-unsloth-bnb-4bit",
        "dataset":       SFT_DIR / "orch_train.jsonl",
        "output_dir":    OUT_DIR / "qwen3-14b-orch-lora",
        "max_seq_len":   4096,
        "lora_r":        32,
        "lora_alpha":    64,
        "lora_dropout":  0.05,
        "target_modules": ["q_proj","k_proj","v_proj","o_proj","gate_proj","up_proj","down_proj"],
        "learning_rate": 1e-4,
        "per_device_train_batch_size": 1,
        "gradient_accumulation_steps": 8,
        "num_train_epochs": 3,
        "warmup_ratio":    0.03,
        "fp16": True,
    },
}


def prepare_dataset(config_key: str = "7b") -> dict:
    """Подготовить датасет для обучения (HuggingFace format)."""
    cfg  = CONFIGS[config_key]
    src  = cfg["dataset"]
    if not src.exists():
        print(f"Датасет не найден: {src}")
        print("Запустите: python3 assistant/orch_dataset.py export")
        return {}

    rows = []
    for line in src.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
            msgs = d.get("messages", [])
            if len(msgs) >= 2:
                rows.append({"messages": msgs, "quality": d.get("quality", 0.7)})
        except Exception:
            pass

    # Сохранить в формате HuggingFace datasets
    out_file = OUT_DIR / f"train_{config_key}.jsonl"
    with out_file.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(f"Подготовлено {len(rows)} примеров → {out_file}")
    return {"count": len(rows), "file": str(out_file)}


def train(config_key: str = "7b"):
    """Запустить LoRA fine-tuning (требует unsloth + GPU)."""
    cfg = CONFIGS[config_key]
    print(f"Начало обучения: {config_key}")
    print(f"  Base model: {cfg['base_model']}")
    print(f"  Output:     {cfg['output_dir']}")
    print(f"  LoRA r={cfg['lora_r']}, alpha={cfg['lora_alpha']}")
    print(f"  LR={cfg['learning_rate']}, epochs={cfg['num_train_epochs']}")
    print()

    try:
        from unsloth import FastLanguageModel
    except ImportError:
        print("unsloth не установлен. Установка:")
        print("  pip install unsloth")
        print("\nЗапуск на CPU невозможен. Нужна GPU ≥16GB VRAM.")
        return

    model, tokenizer = FastLanguageModel.from_pretrained(
        model_name=cfg["base_model"],
        max_seq_length=cfg["max_seq_len"],
        load_in_4bit=True,
    )
    model = FastLanguageModel.get_peft_model(
        model,
        r=cfg["lora_r"],
        target_modules=cfg["target_modules"],
        lora_alpha=cfg["lora_alpha"],
        lora_dropout=cfg["lora_dropout"],
        bias="none",
        use_gradient_checkpointing="unsloth",
    )

    from datasets import load_dataset
    dataset_file = OUT_DIR / f"train_{config_key}.jsonl"
    if not dataset_file.exists():
        prepare_dataset(config_key)
    dataset = load_dataset("json", data_files=str(dataset_file), split="train")

    from trl import SFTTrainer
    from transformers import TrainingArguments

    trainer = SFTTrainer(
        model=model,
        tokenizer=tokenizer,
        train_dataset=dataset,
        dataset_text_field="messages",
        max_seq_length=cfg["max_seq_len"],
        args=TrainingArguments(
            output_dir=str(cfg["output_dir"]),
            num_train_epochs=cfg["num_train_epochs"],
            per_device_train_batch_size=cfg["per_device_train_batch_size"],
            gradient_accumulation_steps=cfg["gradient_accumulation_steps"],
            learning_rate=cfg["learning_rate"],
            warmup_ratio=cfg["warmup_ratio"],
            fp16=cfg["fp16"],
            logging_steps=10,
            save_steps=100,
            save_total_limit=2,
        ),
    )
    trainer.train()
    model.save_pretrained(str(cfg["output_dir"]) + "_final")
    print(f"\n✓ Обучение завершено → {cfg['output_dir']}_final")


def eval_ab(question: str = "Что такое DHT?"):
    """A/B тест: base Qwen3:14b vs fine-tuned оркестратор."""
    import requests
    session = requests.Session()
    session.trust_env = False

    OLLAMA = "http://127.0.0.1:11434"
    models = ["qwen3:14b"]

    from agent.orch_schemas import OrchestratorRequest
    from agent.orchestrator_v2 import process

    print(f"Вопрос: {question}\n{'='*50}")

    # Base model (прямой вызов)
    print("\n[BASE Qwen3:14b — прямой вызов]")
    try:
        r = session.post(
            f"{OLLAMA}/api/generate",
            json={"model": "qwen3:14b", "prompt": question, "stream": False,
                  "options": {"temperature": 0.7, "num_predict": 300}},
            timeout=60,
        )
        base_answer = r.json().get("response", "").strip()
        print(base_answer[:400])
    except Exception as e:
        print(f"Ошибка: {e}")
        base_answer = ""

    # Orchestrator v2
    print(f"\n[Orchestrator v2]")
    req  = OrchestratorRequest(query=question)
    resp = process(req, verbose=False)
    print(resp.answer[:400])
    print(f"Trust: {resp.trust_level} | Steps: {len(resp.steps_taken)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "prepare"

    if cmd == "prepare":
        model_key = sys.argv[2] if len(sys.argv) > 2 else "7b"
        prepare_dataset(model_key)

    elif cmd == "train":
        model_key = "14b" if "--model" in sys.argv and "14b" in sys.argv else "7b"
        train(model_key)

    elif cmd == "eval":
        q = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Что такое DHT?"
        eval_ab(q)

    else:
        print("Команды: prepare [7b|14b], train [--model 7b|14b], eval [вопрос]")
