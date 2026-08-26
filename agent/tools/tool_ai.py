"""
tool_ai.py — отправить задачу в AI чат через pet API.
Поддерживает: deepseek, claude, gpt, kimi, qwen, local (Ollama).
"""
import time
import requests

PET_URL = "http://127.0.0.1:9010"
TIMEOUT = 180


def _s() -> requests.Session:
    s = requests.Session()
    s.trust_env = False
    return s


def ask_local(prompt: str, model: str = "heretic:q8",
              system: str = None, temperature: float = 0.7) -> dict:
    """Спросить локальную модель (Ollama) через pet."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    try:
        r = _s().post(f"{PET_URL}/api/local/chat",
                      json={"model": model, "messages": messages, "temperature": temperature},
                      timeout=TIMEOUT)
        r.raise_for_status()
        d = r.json()
        return {"ok": d.get("ok", True), "content": d.get("content", ""), "model": model}
    except Exception as e:
        return {"ok": False, "error": str(e), "content": ""}


def ask_council(prompt: str, models: list[str] = None) -> dict:
    """Послать задачу в relay-цепочку internet-чатов через pet broadcast."""
    models = models or ["deepseek"]
    try:
        r = _s().post(f"{PET_URL}/api/council/relay",
                      json={"text": prompt},
                      timeout=TIMEOUT)
        r.raise_for_status()
        return {"ok": True, "task_id": r.json().get("task_id")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


_AI_NAMES = {
    "deepseek": ["deepseek", "дипсик", "дипсика"],
    "claude":   ["claude", "клод", "клода"],
    "gpt":      ["gpt", "чатгпт", "chatgpt"],
    "kimi":     ["kimi", "кими"],
    "qwen":     ["qwen", "квен"],
}


def _detect_pattern(task: str) -> dict | None:
    """Строит план программно для известных паттернов без вызова модели."""
    import re
    tl = task.lower()

    # "спроси у deepseek/claude/... <вопрос>"
    for model, names in _AI_NAMES.items():
        for name in names:
            m = re.search(rf'спрос[иь]\s+у\s+{name}\s+(.+)', tl, re.IGNORECASE | re.DOTALL)
            if m:
                question = task[m.start(1):]
                return {"ok": True, "steps": [
                    {"step": 1, "tool": "ai.ask_council",
                     "args": {"prompt": question, "models": [model]},
                     "description": f"спросить {model}"},
                ]}

    # "спроси всех / у всех / все модели <вопрос>"
    m = re.search(r'спрос[иь]\s+(?:у\s+)?(всех|все\s+модели|всем)\s+(.+)', tl, re.IGNORECASE | re.DOTALL)
    if m:
        question = task[m.start(2):]
        return {"ok": True, "steps": [
            {"step": 1, "tool": "ai.ask_council",
             "args": {"prompt": question},
             "description": "спросить все модели"},
        ]}

    # "прочитай файл <path>"
    m = re.search(r'прочит[ай]+\s+файл\s+(\S+)', tl)
    if m:
        path = m.group(1)
        return {"ok": True, "steps": [
            {"step": 1, "tool": "fs.read", "args": {"path": path}, "description": f"читаем {path}"},
        ]}

    # "покажи состояние системы / состояние redis"
    if any(x in tl for x in ["состояние системы", "статус системы", "system report"]):
        return {"ok": True, "steps": [
            {"step": 1, "tool": "system.full_report", "args": {}, "description": "отчёт о системе"},
        ]}
    if any(x in tl for x in ["состояние redis", "ключи redis", "redis keys"]):
        return {"ok": True, "steps": [
            {"step": 1, "tool": "redis.stats", "args": {}, "description": "статистика Redis"},
            {"step": 2, "tool": "redis.keys", "args": {"pattern": "*"}, "description": "список ключей"},
        ]}

    # "проанализируй/оцени/объясни X от/через/у клода/гпт/..."
    for model, names in _AI_NAMES.items():
        for name in names:
            m = re.search(
                rf'(?:проанализируй|оцени|объясни|проверь|узнай|спроси|скажи)\s+.{{0,60}}'
                rf'(?:от|через|у|с помощью)\s+{name}',
                tl, re.IGNORECASE | re.DOTALL)
            if m:
                return {"ok": True, "steps": [
                    {"step": 1, "tool": "ai.ask_council",
                     "args": {"prompt": task, "models": [model]},
                     "description": f"отправить задачу в {model}"},
                ]}

    # "проанализируй / объясни / оцени / проверь" → локальная модель
    if re.match(r'^(проанализируй|объясни|оцени|проверь|расскажи|опиши|придумай|составь)', tl):
        return {"ok": True, "steps": [
            {"step": 1, "tool": "ai.ask_local",
             "args": {"prompt": task},
             "description": "выполнить задачу локально"},
        ]}

    # Любой вопрос (заканчивается на ?) → локальная модель
    if task.strip().endswith("?"):
        return {"ok": True, "steps": [
            {"step": 1, "tool": "ai.ask_local",
             "args": {"prompt": task},
             "description": "ответить на вопрос"},
        ]}

    return None  # паттерн не найден → идём к модели


def build_plan(task: str, context: str = "") -> dict:
    """Попросить локальную модель построить JSON-план выполнения задачи."""
    system = (
        "Ты планировщик задач. Возвращай ТОЛЬКО JSON-массив, без пояснений, без markdown.\n"
        "Формат каждого шага: {\"step\":N,\"tool\":\"...\",\"args\":{...},\"description\":\"...\"}\n\n"
        "Доступные инструменты:\n"
        "- ai.ask_local(prompt, model) — спросить локальную Ollama модель\n"
        "- ai.ask_council(prompt, models) — спросить internet AI чат (deepseek/claude/gpt/kimi/qwen)\n"
        "- fs.read(path) — прочитать файл\n"
        "- fs.write(path, content) — записать файл\n"
        "- fs.ls(path) — список файлов\n"
        "- search.find(pattern, path) — найти файлы\n"
        "- search.grep(pattern, path) — поиск в файлах\n"
        "- shell.run(cmd) — выполнить команду\n"
        "- redis.keys(pattern) — ключи Redis\n"
        "- system.full_report() — состояние системы\n\n"
        "Примеры:\n"
        "Задача: спроси у deepseek про Python\n"
        "[{\"step\":1,\"tool\":\"ai.ask_council\",\"args\":{\"prompt\":\"Расскажи про Python\",\"models\":[\"deepseek\"]},\"description\":\"спросить DeepSeek\"}]\n\n"
        "Задача: прочитай файл README и запиши summary\n"
        "[{\"step\":1,\"tool\":\"fs.read\",\"args\":{\"path\":\"README.md\"},\"description\":\"читаем файл\"},"
        "{\"step\":2,\"tool\":\"ai.ask_local\",\"args\":{\"prompt\":\"Сделай краткое summary: {{step_1_result}}\"},\"description\":\"summary через AI\"},"
        "{\"step\":3,\"tool\":\"fs.write\",\"args\":{\"path\":\"registry/knowledge/summary.md\",\"content\":\"{{step_2_result}}\"},\"description\":\"сохраняем\"}]\n\n"
        "ВАЖНО: верни ТОЛЬКО JSON-массив, без текста до и после."
    )
    # Всегда спрашиваем модель — она строит полноценный план
    msg = f"Задача: {task}"
    if context:
        msg += f"\n\nКонтекст:\n{context}"
    result = ask_local(msg, system=system, temperature=0.1)
    if not result["ok"]:
        # Модель не ответила — fallback на паттерн-детектор
        fallback = _detect_pattern(task)
        if fallback:
            return fallback
        return result
    import re, json
    content = result["content"].strip()
    # Ищем JSON-массив
    for pat in [r'\[[\s\S]*\]', r'\[.*?\]']:
        m = re.search(pat, content, re.DOTALL)
        if m:
            try:
                steps = json.loads(m.group())
                if isinstance(steps, list) and steps:
                    return {"ok": True, "steps": steps, "raw": content}
            except json.JSONDecodeError:
                pass
    # Модель не вернула JSON — fallback на паттерн-детектор
    fallback = _detect_pattern(task)
    if fallback:
        return fallback
    return {"ok": True, "steps": [
        {"step": 1, "tool": "ai.ask_local",
         "args": {"prompt": task},
         "description": "выполнить задачу локально (fallback)"},
    ], "_fallback": True, "raw": content[:300]}


def review_result(task: str, result: str) -> dict:
    """Попросить модель проверить результат выполнения задачи."""
    prompt = (
        f"Задача: {task}\n\nРезультат выполнения:\n{result}\n\n"
        "Задача выполнена? Ответь: OK или FAIL и кратко почему."
    )
    return ask_local(prompt, temperature=0.1)
