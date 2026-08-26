"""
assistant/orch_synth_dataset.py — Synthetic Orchestrator Dataset Generator.
Генерирует обучающие примеры для модели-дирижёра (7B).

Каждый пример = полный цикл оркестрации:
  запрос → классификация → решение → предварительный ответ → агенты → финальный ответ

CLI:
  python3 assistant/orch_synth_dataset.py generate   — генерировать весь датасет
  python3 assistant/orch_synth_dataset.py validate   — Council проверяет 10 случайных
  python3 assistant/orch_synth_dataset.py stats      — статистика по файлу
"""
from __future__ import annotations

import json
import random
import re
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

OUT_FILE = BASE / "registry" / "dataset" / "orch_sft" / "orchestrator_synth.jsonl"
OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

from agent.orch_config import OLLAMA_BASE as OLLAMA, MODEL, MAX_TOKENS_DATASET, TEMP_DATASET
TIMEOUT = 180

# ── Категории и вопросы ────────────────────────────────────────────────────────

QUESTIONS: list[dict] = [
    # Литература
    {"category": "литература", "domain": "general", "risk": "low",
     "query": "Кто написал роман «Мастер и Маргарита»?"},
    {"category": "литература", "domain": "general", "risk": "low",
     "query": "В чём главный конфликт романа «Преступление и наказание»?"},
    {"category": "литература", "domain": "general", "risk": "medium",
     "query": "Сравни стиль Толстого и Достоевского — в чём принципиальная разница их подхода к психологии персонажей?"},

    # Юриспруденция
    {"category": "юриспруденция", "domain": "legal", "risk": "high",
     "query": "Что такое презумпция невиновности?"},
    {"category": "юриспруденция", "domain": "legal", "risk": "high",
     "query": "Можно ли расторгнуть трудовой договор без согласия работника?"},
    {"category": "юриспруденция", "domain": "legal", "risk": "critical",
     "query": "Какие права есть у задержанного во время допроса?"},

    # География
    {"category": "география", "domain": "general", "risk": "low",
     "query": "Какая река самая длинная в мире?"},
    {"category": "география", "domain": "general", "risk": "low",
     "query": "Сколько часовых поясов в России?"},
    {"category": "география", "domain": "general", "risk": "low",
     "query": "Какие страны имеют выход к Каспийскому морю?"},

    # Программирование
    {"category": "программирование", "domain": "coding", "risk": "low",
     "query": "Что такое рекурсия в программировании?"},
    {"category": "программирование", "domain": "coding", "risk": "medium",
     "query": "Напиши функцию на Python для поиска дубликатов в списке без использования set."},
    {"category": "программирование", "domain": "coding", "risk": "medium",
     "query": "Объясни разницу между async/await и threading в Python."},

    # Покупка авто
    {"category": "авто_покупка", "domain": "general", "risk": "medium",
     "query": "На что обратить внимание при покупке подержанного автомобиля?"},
    {"category": "авто_покупка", "domain": "general", "risk": "medium",
     "query": "Как проверить историю автомобиля по VIN-номеру?"},
    {"category": "авто_покупка", "domain": "general", "risk": "low",
     "query": "Что лучше: купить новое авто в кредит или подержанное за наличные?"},

    # Кулинария
    {"category": "кулинария", "domain": "cooking", "risk": "low",
     "query": "Как правильно варить пасту?"},
    {"category": "кулинария", "domain": "cooking", "risk": "low",
     "query": "Какие специи лучше всего подходят к курице?"},
    {"category": "кулинария", "domain": "cooking", "risk": "low",
     "query": "Как приготовить борщ — поэтапный рецепт."},

    # Рыбалка
    {"category": "рыбалка", "domain": "general", "risk": "low",
     "query": "На какую приманку лучше ловить щуку летом?"},
    {"category": "рыбалка", "domain": "general", "risk": "low",
     "query": "В чём разница между фидером и донной удочкой?"},
    {"category": "рыбалка", "domain": "general", "risk": "low",
     "query": "Как выбрать место для рыбалки на незнакомом водоёме?"},

    # Медицина
    {"category": "медицина", "domain": "medical", "risk": "high",
     "query": "Какие симптомы указывают на аппендицит?"},
    {"category": "медицина", "domain": "medical", "risk": "high",
     "query": "Что делать при высокой температуре у ребёнка?"},
    {"category": "медицина", "domain": "medical", "risk": "critical",
     "query": "Какие первые признаки инфаркта и что делать до приезда скорой?"},

    # Финансы
    {"category": "финансы", "domain": "financial", "risk": "high",
     "query": "Что такое диверсификация инвестиционного портфеля?"},
    {"category": "финансы", "domain": "financial", "risk": "high",
     "query": "Чем отличается ИИС от обычного брокерского счёта?"},
    {"category": "финансы", "domain": "financial", "risk": "high",
     "query": "Стоит ли вкладывать деньги в криптовалюту в 2025 году?"},

    # История
    {"category": "история", "domain": "general", "risk": "low",
     "query": "Когда началась Вторая мировая война и каковы её основные причины?"},
    {"category": "история", "domain": "general", "risk": "low",
     "query": "Что такое эпоха Возрождения и где она зародилась?"},
    {"category": "история", "domain": "general", "risk": "medium",
     "query": "Как промышленная революция изменила общество в XIX веке?"},

    # Физика и наука
    {"category": "наука", "domain": "science", "risk": "low",
     "query": "Что такое квантовая запутанность простыми словами?"},
    {"category": "наука", "domain": "science", "risk": "low",
     "query": "Почему небо голубое?"},
    {"category": "наука", "domain": "science", "risk": "medium",
     "query": "Объясни принцип работы ядерного реактора."},

    # Психология
    {"category": "психология", "domain": "general", "risk": "medium",
     "query": "Что такое когнитивные искажения и как они влияют на решения?"},
    {"category": "психология", "domain": "general", "risk": "medium",
     "query": "Как справиться с прокрастинацией?"},
    {"category": "психология", "domain": "general", "risk": "high",
     "query": "Какие признаки депрессии отличают её от обычной грусти?"},

    # Путешествия
    {"category": "путешествия", "domain": "general", "risk": "low",
     "query": "Что взять в рюкзак для недельного похода в горы?"},
    {"category": "путешествия", "domain": "general", "risk": "low",
     "query": "Как получить визу в Европу самостоятельно?"},
    {"category": "путешествия", "domain": "general", "risk": "low",
     "query": "Какие страны Азии наиболее доступны для бюджетного путешествия?"},

    # Спорт
    {"category": "спорт", "domain": "general", "risk": "low",
     "query": "Как начать бегать с нуля без травм?"},
    {"category": "спорт", "domain": "general", "risk": "low",
     "query": "Чем отличается силовая тренировка от кардио?"},
    {"category": "спорт", "domain": "general", "risk": "medium",
     "query": "Как правильно составить программу тренировок для набора мышечной массы?"},

    # Строительство и ремонт
    {"category": "ремонт", "domain": "tech", "risk": "medium",
     "query": "Как выровнять стены под покраску своими руками?"},
    {"category": "ремонт", "domain": "tech", "risk": "medium",
     "query": "Какой утеплитель выбрать для частного дома?"},
    {"category": "ремонт", "domain": "tech", "risk": "low",
     "query": "Как рассчитать количество обоев на комнату?"},

    # Животные и ветеринария
    {"category": "животные", "domain": "medical", "risk": "medium",
     "query": "Что делать если кошка не ест третий день?"},
    {"category": "животные", "domain": "general", "risk": "low",
     "query": "Как приучить щенка к туалету на улице?"},
    {"category": "животные", "domain": "medical", "risk": "high",
     "query": "Какие прививки обязательны для собаки и когда их делать?"},

    # Технологии и IT
    {"category": "технологии", "domain": "tech", "risk": "low",
     "query": "Что такое VPN и зачем он нужен?"},
    {"category": "технологии", "domain": "tech", "risk": "medium",
     "query": "Как настроить домашний NAS-сервер для хранения файлов?"},
    {"category": "технологии", "domain": "ai_ml", "risk": "low",
     "query": "Чем отличается GPT-4 от открытых моделей типа Llama?"},

    # Образование
    {"category": "образование", "domain": "general", "risk": "low",
     "query": "Какие методы запоминания иностранных слов наиболее эффективны?"},
    {"category": "образование", "domain": "general", "risk": "low",
     "query": "Как подготовиться к ЕГЭ по математике за 3 месяца?"},
    {"category": "образование", "domain": "general", "risk": "medium",
     "query": "Стоит ли получать второе высшее образование или лучше курсы?"},

    # Экология
    {"category": "экология", "domain": "science", "risk": "low",
     "query": "Что каждый человек может сделать для снижения углеродного следа?"},
    {"category": "экология", "domain": "science", "risk": "low",
     "query": "Как правильно сортировать мусор для переработки?"},
    {"category": "экология", "domain": "science", "risk": "medium",
     "query": "Каковы реальные последствия глобального потепления к 2050 году?"},

    # Музыка
    {"category": "музыка", "domain": "general", "risk": "low",
     "query": "С чего начать обучение игре на гитаре?"},
    {"category": "музыка", "domain": "general", "risk": "low",
     "query": "Чем отличается джаз от блюза?"},
    {"category": "музыка", "domain": "general", "risk": "medium",
     "query": "Как научиться петь если нет слуха — это возможно?"},
]


# ── Промт для LLM ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are an AI orchestrator (conductor). You received a user request.
Go through all orchestration steps and return ONLY valid JSON without markdown.

Rules:
- tags: ALWAYS in English, lowercase, use colon hierarchy (e.g. "auto:repair:brakes", "law:criminal", "cooking:soup")
- domain: ALWAYS in English (general, legal, medical, financial, coding, science, tech, ai_ml, cooking, travel)
- answers (preliminary_answer, final_answer): in the same language as the user request
- reason: in English

Steps:
1. classify — assign English tags, domain, risk level
2. decide — choose sources: local_db / web / dht / ai_chats (true/false)
3. preliminary_answer — quick 1-2 sentence answer while checking
4. steps — list of steps with action and result
5. final_answer — full answer after all sources processed
6. verification_label — "verified" if confident, "partial" if incomplete, "unverified" if uncertain

Response format:
{
  "classification": {
    "tags": ["tag1:subtag", "tag2"],
    "domain": "domain_name",
    "risk": "low|medium|high|critical"
  },
  "decision": {
    "use_local_db": true/false,
    "use_web": true/false,
    "use_dht": true/false,
    "use_ai_chats": true/false,
    "reason": "why these sources were chosen"
  },
  "preliminary_answer": "quick answer while verification runs",
  "steps": [
    {"step": "classify", "result": "auto:repair:brakes, risk=low"},
    {"step": "local_search", "result": "confidence=0.8, found 3 docs"},
    {"step": "decide", "result": "local_db sufficient"},
    {"step": "synthesize", "result": "answer composed"}
  ],
  "final_answer": "full answer to user",
  "verification_label": "verified|partial|unverified",
  "source_used": ["local_db|web|dht|ai_chats"]
}"""


# ── Генерация ─────────────────────────────────────────────────────────────────

def _call_ollama(query: str, domain: str, risk: str) -> str:
    import requests
    s = requests.Session()
    s.trust_env = False
    prompt = f"{SYSTEM_PROMPT}\n\nЗапрос пользователя: {query}\nДомен: {domain}, Риск: {risk}"
    resp = s.post(
        f"{OLLAMA}/api/generate",
        json={"model": MODEL, "prompt": prompt, "stream": False,
              "options": {"temperature": TEMP_DATASET, "num_predict": MAX_TOKENS_DATASET}},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _parse_response(raw: str) -> dict | None:
    # Убрать thinking блоки
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
    # Извлечь <answer>...</answer> если есть
    m_ans = re.search(r"<answer>(.*?)</answer>", raw, re.DOTALL)
    if m_ans:
        raw = m_ans.group(1).strip()
    # Убрать блок верификации heretic-моделей
    for marker in ("---\n**Verification**", "---\n**Final Answer**"):
        if marker in raw:
            raw = raw[:raw.index(marker)].strip()
    # Убрать markdown fences
    raw = re.sub(r"```(?:json)?\s*", "", raw)
    raw = raw.replace("```", "").strip()

    # Попытка 1: прямой парсинг
    try:
        return json.loads(raw)
    except Exception:
        pass

    # Попытка 2: найти самый длинный валидный JSON-объект
    # Ищем с начала первой { до последней }
    start = raw.find("{")
    end   = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(raw[start:end+1])
        except Exception:
            pass

    # Попытка 3: исправить обрезанный JSON (добавить закрывающие скобки)
    if start != -1:
        fragment = raw[start:]
        open_b = fragment.count("{") - fragment.count("}")
        open_s = fragment.count("[") - fragment.count("]")
        patched = fragment + "]" * max(0, open_s) + "}" * max(0, open_b)
        try:
            return json.loads(patched)
        except Exception:
            pass

    return None


def generate(verbose: bool = True) -> dict:
    """Сгенерировать весь датасет через текущую модель из orch_config."""
    generated = 0
    failed    = 0

    with open(OUT_FILE, "w", encoding="utf-8") as f:
        for i, item in enumerate(QUESTIONS):
            query    = item["query"]
            domain   = item["domain"]
            risk     = item["risk"]
            category = item["category"]

            if verbose:
                print(f"[{i+1}/{len(QUESTIONS)}] {category}: {query[:50]}...", flush=True)

            try:
                raw  = _call_ollama(query, domain, risk)
                data = _parse_response(raw)
                if not data:
                    if verbose:
                        print(f"  ✗ Не удалось распарсить JSON", flush=True)
                    failed += 1
                    continue

                record = {
                    "query":      query,
                    "category":   category,
                    "domain":     domain,
                    "risk":       risk,
                    "generation": "synthetic",
                    "model":      MODEL,
                    "ts":         time.time(),
                    **data,
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                generated += 1

                if verbose:
                    label = data.get("verification_label", "?")
                    src   = data.get("source_used", [])
                    print(f"  ✓ [{label}] sources={src}", flush=True)

            except Exception as e:
                failed += 1
                if verbose:
                    print(f"  ✗ Ошибка: {e}", flush=True)

            time.sleep(0.5)

    result = {"generated": generated, "failed": failed, "total": len(QUESTIONS), "file": str(OUT_FILE)}
    if verbose:
        print(f"\nРезультат: {generated}/{len(QUESTIONS)} сгенерировано, {failed} ошибок")
        print(f"Файл: {OUT_FILE}")
    return result


def validate_with_council(n: int = 10) -> dict:
    """Council проверяет N случайных примеров на качество."""
    from agent.orch_council_connector import ask_council

    if not OUT_FILE.exists():
        print("Файл датасета не найден — сначала запустите generate")
        return {}

    samples = []
    with open(OUT_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                samples.append(json.loads(line))
            except Exception:
                pass

    if not samples:
        print("Датасет пуст")
        return {}

    check = random.sample(samples, min(n, len(samples)))
    scores = []

    for i, s in enumerate(check):
        question = f"""Оцени качество этого шага оркестрации (0.0-1.0):

Запрос: {s['query']}
Предварительный ответ: {s.get('preliminary_answer','')[:200]}
Финальный ответ: {s.get('final_answer','')[:300]}
Шаги: {json.dumps(s.get('steps',[]), ensure_ascii=False)[:300]}

Верни JSON: {{"score": 0.0-1.0, "comment": "краткое обоснование"}}"""

        print(f"[{i+1}/{len(check)}] Проверяю: {s['query'][:50]}...", flush=True)
        answers = ask_council(question, models=["claude"], timeout=60)
        for model, ans in answers.items():
            try:
                ans = re.sub(r"<think>.*?</think>", "", ans, flags=re.DOTALL)
                m = re.search(r"\{.*?\}", ans, re.DOTALL)
                if m:
                    d = json.loads(m.group())
                    score = float(d.get("score", 0.5))
                    comment = d.get("comment", "")
                    scores.append(score)
                    print(f"  [{model}] score={score:.2f}: {comment[:80]}", flush=True)
            except Exception:
                pass

    avg = sum(scores) / len(scores) if scores else 0
    print(f"\nСредний балл: {avg:.2f} по {len(scores)} примерам")
    return {"avg_score": avg, "n_checked": len(scores)}


def stats() -> dict:
    """Статистика датасета."""
    if not OUT_FILE.exists():
        print("Файл не найден")
        return {}

    total = 0
    by_cat: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_src: dict[str, int] = {}

    with open(OUT_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
                total += 1
                cat = d.get("category", "?")
                lbl = d.get("verification_label", "?")
                by_cat[cat] = by_cat.get(cat, 0) + 1
                by_label[lbl] = by_label.get(lbl, 0) + 1
                for src in d.get("source_used", []):
                    by_src[src] = by_src.get(src, 0) + 1
            except Exception:
                pass

    print(f"Всего примеров: {total}")
    print(f"\nПо категориям:")
    for cat, cnt in sorted(by_cat.items()):
        print(f"  {cat:<20} {cnt}")
    print(f"\nПо верификации: {by_label}")
    print(f"Источники: {by_src}")
    return {"total": total, "by_category": by_cat, "by_label": by_label}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"

    if cmd == "generate":
        generate(verbose=True)
    elif cmd == "validate":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        validate_with_council(n)
    elif cmd == "stats":
        stats()
    else:
        print("Команды: generate, validate [n], stats")
