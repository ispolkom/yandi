"""
agent/orch_dataset_runner.py — Автономный сборщик датасета с оценкой качества.

Цикл без участия Claude Code:
  вопрос → Qwen → GPT-5.5 (codex) → Qwen рефайн → chain-запись в датасет
  + оценка качества через trace_metrics.py

Запуск:
  python3 -m agent.orch_dataset_runner
  python3 -m agent.orch_dataset_runner --limit 10 --pause 8
  python3 -m agent.orch_dataset_runner --compare  # регрессия
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path
from datetime import datetime

import requests

from agent.orch_codex_validator import validate as codex_validate
from agent.refiner import refine as qwen_refine
from agent.trace_metrics import TraceMetrics

# ── Пути ─────────────────────────────────────────────────────────────────────

BASE = Path(__file__).parent.parent
DATASET_DIR = BASE / "registry" / "dataset" / "orch_traces"
DATASET_DIR.mkdir(parents=True, exist_ok=True)

METRICS_FILE = BASE / "registry" / "dataset" / "metrics.jsonl"
METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)

ORCH_URL = "http://127.0.0.1:9010/api/orchestrator/ask"

# ── 200 вопросов по 20 темам ─────────────────────────────────────────────────

QUESTIONS = [
    # Астрономия
    "Почему Луна всегда повёрнута к Земле одной стороной?",
    "Что такое нейтронная звезда и чем она отличается от чёрной дыры?",
    "Почему на Юпитере не прекращается Большое красное пятно?",
    "Как образуется северное сияние и почему оно разноцветное?",
    "Что произойдёт с Солнцем через 5 миллиардов лет?",
    "Почему кольца Сатурна такие тонкие, если планета огромная?",
    "Что такое реликтовое излучение и что оно говорит о начале Вселенной?",
    "Почему на Венере температура выше, чем на Меркурии, хотя Меркурий ближе к Солнцу?",
    "Как чёрная дыра искажает время вблизи горизонта событий?",
    "Что такое экзопланеты и как учёные их обнаруживают?",

    # Физика
    "Что такое энтропия и почему она всегда растёт?",
    "Как работает квантовая запутанность?",
    "Что такое тёмная материя и как её обнаруживают?",
    "Почему лёд скользкий?",
    "Как работает интерферометр LIGO для обнаружения гравитационных волн?",
    "Что такое эффект Доплера и где он применяется?",
    "Почему небо голубое, а закат красный?",
    "Что такое сверхпроводимость и при каких температурах она возникает?",
    "Как работает квантовый компьютер?",
    "Что такое фотоэффект и как он подтверждает квантовую природу света?",

    # Биология
    "Как работает иммунная система человека?",
    "Что такое CRISPR и как он редактирует гены?",
    "Почему мы стареем на клеточном уровне?",
    "Как работает мозг во время сна?",
    "Что такое микробиом и как он влияет на здоровье?",
    "Как вирусы заражают клетки?",
    "Что такое стволовые клетки и для чего они используются?",
    "Как работает фотосинтез на молекулярном уровне?",
    "Почему у человека разные группы крови?",
    "Как работает нервная система?",

    # Химия
    "Что такое катализатор и как он ускоряет реакцию?",
    "Как работает электролиз воды?",
    "Что такое полимеры и где они используются?",
    "Почему вода расширяется при замерзании?",
    "Как работают ферменты в организме?",
    "Что такое pH и как он влияет на химические процессы?",
    "Как образуются кристаллы?",
    "Что такое коррозия металлов и как её предотвратить?",
    "Как работает химический источник тока (батарейка)?",
    "Что такое эндотермическая и экзотермическая реакции?",

    # Математика
    "Что такое фракталы и где они встречаются в природе?",
    "Как работает RSA-шифрование?",
    "Что такое теорема Гёделя о неполноте?",
    "Почему число Пи бесконечно и непериодично?",
    "Что такое теория вероятностей и как она применяется в жизни?",
    "Как работает машинное обучение на математическом уровне?",
    "Что такое комплексные числа и зачем они нужны?",
    "Как работает алгоритм сортировки?",
    "Что такое теория графов и где она применяется?",
    "Почему нельзя разделить на ноль?",

    # Технологии
    "Как работает интернет на физическом уровне?",
    "Что такое блокчейн и как он работает?",
    "Как работают нейронные сети?",
    "Что такое квантовое шифрование?",
    "Как работает GPS и почему он так точен?",
    "Что такое искусственный интеллект и чем он отличается от человека?",
    "Как работает Wi-Fi?",
    "Что такое облачные вычисления?",
    "Как работает компьютерное зрение?",
    "Что такое Интернет вещей и как он работает?",

    # Медицина
    "Как действуют антибиотики на бактерии?",
    "Что такое вакцинация и как она работает?",
    "Как диагностируется рак на ранних стадиях?",
    "Что такое генетическая терапия?",
    "Как работает сердце как насос?",
    "Почему возникает аллергия?",
    "Что такое аутоиммунные заболевания?",
    "Как работают обезболивающие препараты?",
    "Что такое стволовые клетки в медицине?",
    "Как работает диализ почек?",

    # Философия
    "Что такое сознание и где оно возникает?",
    "Существует ли свобода воли?",
    "Что такое справедливость?",
    "Что делает жизнь осмысленной?",
    "Что такое этика и почему она важна?",
    "Существует ли объективная истина?",
    "Что такое добро и зло?",
    "Может ли машина обладать сознанием?",
    "Что такое красота?",
    "Что такое время и существует ли оно объективно?",

    # История
    "Почему пала Римская империя?",
    "Что стало причиной Первой мировой войны?",
    "Что такое Великая французская революция и почему она произошла?",
    "Как Александр Македонский создал свою империю?",
    "Что такое Шёлковый путь и почему он важен?",
    "Как открыли Америку и кто был первым?",
    "Что такое эпоха Возрождения и почему она важна?",
    "Как возникла письменность?",
    "Что такое Просвещение и как оно повлияло на мир?",
    "Какие причины привели к распаду Советского Союза?",

    # Психология
    "Как работает память человека?",
    "Что такое эмоции и зачем они нужны?",
    "Как работает мотивация?",
    "Что такое когнитивные искажения?",
    "Как формируется личность?",
    "Что такое стресс и как с ним справляться?",
    "Как работает внимание?",
    "Что такое эмпатия и почему она важна?",
    "Как работает принятие решений?",
    "Что такое психические расстройства и как их лечат?",

    # Экономика
    "Что такое инфляция и почему она возникает?",
    "Что такое ВВП и как его считают?",
    "Как работают налоги?",
    "Что такое фондовый рынок?",
    "Что такое дефицит бюджета?",
    "Как работает банковская система?",
    "Что такое криптовалюта и как она работает?",
    "Что такое безработица и почему она возникает?",
    "Как работает международная торговля?",
    "Что такое экономический рост?",

    # Искусство
    "Что такое классицизм в искусстве?",
    "Как музыка влияет на мозг?",
    "Что такое архитектура как искусство?",
    "Как кино влияет на культуру?",
    "Что такое абстракционизм?",
    "Как литература отражает эпоху?",
    "Что такое театральное искусство?",
    "Как работает фотография как искусство?",
    "Что такое скульптура и как она создаётся?",
    "Что такое современное искусство?",

    # Литература
    "В чём смысл романа «Мастер и Маргарита»?",
    "Какие главные темы в романе «Преступление и наказание»?",
    "Что такое антиутопия в литературе?",
    "Какова роль природы в романе «Война и мир»?",
    "Какие черты характерны для поэзии Серебряного века?",
    "В чём смысл повести «Пиковая дама»?",
    "Что такое экзистенциализм в литературе?",
    "Как работает литературный жанр магического реализма?",
    "Какие идеи отражены в романе «1984»?",
    "Что такое поэма и чем она отличается от романа?",

    # Политика
    "Что такое демократия и как она работает?",
    "Что такое авторитаризм?",
    "Как работает ООН?",
    "Что такое Европейский союз и для чего он создан?",
    "Как работает разделение властей?",
    "Что такое гражданское общество?",
    "Что такое идеология и как она влияет на политику?",
    "Как работает выборная система?",
    "Что такое политическая партия?",
    "Что такое дипломатия?",

    # Экология
    "Что такое парниковый эффект и как он влияет на климат?",
    "Что такое глобальное потепление?",
    "Как работает экологическая система?",
    "Что такое биоразнообразие и почему оно важно?",
    "Как работает переработка отходов?",
    "Что такое альтернативная энергетика?",
    "Как изменение климата влияет на океаны?",
    "Что такое вырубка лесов и её последствия?",
    "Как работает круговорот воды в природе?",
    "Что такое экологический след человека?",

    # Языкознание
    "Что такое лингвистика и как она работает?",
    "Как возникают языки?",
    "Что такое диалект и чем он отличается от языка?",
    "Как работает перевод между языками?",
    "Что такое искусственный интеллект в лингвистике?",
    "Как дети изучают язык?",
    "Что такое фонетика?",
    "Как работает письменность?",
    "Что такое семантика в языкознании?",
    "Как язык влияет на мышление?",

    # Спорт
    "Как работает человеческое тело в спорте?",
    "Что такое тренировка и как она работает?",
    "Какие принципы лежат в основе олимпийского движения?",
    "Как спорт влияет на здоровье?",
    "Что такое командный спорт и как он развивает навыки?",
    "Как питание влияет на спортивные достижения?",
    "Что такое психология спорта?",
    "Как работают спортивные соревнования?",
    "Что такое травмы в спорте и как их предотвращают?",
    "Как спорт влияет на общество?",

    # Религия
    "Что такое религия и зачем она людям?",
    "Какие основные мировые религии существуют?",
    "Что такое христианство и как оно возникло?",
    "Что такое ислам и как он возник?",
    "Что такое буддизм и каковы его основные принципы?",
    "Что такое вера и как она работает?",
    "Как религия влияет на культуру?",
    "Что такое религиозные обряды и их значение?",
    "Как религия и наука соотносятся друг с другом?",
    "Что такое атеизм?",

    # Кулинария
    "Как работает кулинария как наука?",
    "Что такое молекулярная кулинария?",
    "Как правильно готовить мясо?",
    "Что такое кулинарные традиции разных народов?",
    "Как специи влияют на вкус и здоровье?",
    "Что такое диетология и правильное питание?",
    "Как готовить овощи, чтобы сохранить витамины?",
    "Что такое ферментация в кулинарии?",
    "Как работают дрожжи в выпечке?",
    "Что такое национальная кухня?",
]

# Инициализируем метрики один раз
_metrics = TraceMetrics()


def ask_orchestrator(query: str, timeout: int = 120) -> dict:
    """Отправить запрос в orchestrator_v2."""
    try:
        start = time.time()
        
        payload = {"query": query}
        resp = requests.post(ORCH_URL, json=payload, timeout=timeout)
        
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}", "text": resp.text}
        
        data = resp.json()
        data["latency"] = time.time() - start
        return data
    except Exception as e:
        return {"error": str(e)}


def load_last_trace(query: str) -> dict | None:
    """
    Загрузить последний трейс для вопроса.
    Использует частичное совпадение (substring), т.к. трейсы могут содержать
    полную версию вопроса, а запрос в датасете может быть обрезан.
    """
    if not DATASET_DIR.exists():
        return None
    
    # Разбиваем запрос на ключевые слова (минимальная длина 4 символа)
    keywords = [w.lower() for w in query.split() if len(w) > 3]
    
    for f in sorted(DATASET_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            with open(f, 'r', encoding='utf-8') as tf:
                for line in tf:
                    try:
                        t = json.loads(line.strip())
                        trace_query = t.get('query', '').strip()
                        
                        # 1. Точное совпадение
                        if trace_query == query.strip():
                            return t
                        
                        # 2. Частичное совпадение (substring)
                        if len(query) > 20 and query.strip() in trace_query:
                            return t
                        
                        # 3. По ключевым словам (если есть ключевые слова)
                        if keywords:
                            trace_lower = trace_query.lower()
                            match_count = sum(1 for kw in keywords if kw in trace_lower)
                            # Если совпало > 50% ключевых слов
                            if match_count >= len(keywords) * 0.5:
                                return t
                    except:
                        pass
        except:
            pass
    return None


def save_chain(record: dict) -> Path:
    """Сохранить цепочку в датасет."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"chain_{ts}_{record['id']}.json"
    path = DATASET_DIR / name
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    return path


def save_metric(metric_record: dict):
    """Сохранить метрику в файл."""
    with open(METRICS_FILE, 'a', encoding='utf-8') as mf:
        mf.write(json.dumps(metric_record, ensure_ascii=False) + "\n")


def run(questions: list[str], pause: int = 15) -> dict:
    """Прогнать вопросы через оркестратор."""
    stats = {
        "total": 0,
        "skipped": 0,
        "verified": 0,
        "supplemented": 0,
        "partial": 0,
        "rejected": 0,
        "unverified": 0,
        "scores": [],
        "total_score": 0.0,
    }

    for i, question in enumerate(questions):
        print(f"\n[{i+1}/{len(questions)}] {question[:60]}...", flush=True)

        # ── ЗАПРОС К ORCHESTRATOR ──
        data = ask_orchestrator(question)
        if data.get("error"):
            print(f"  ✗ ошибка: {data['error']}")
            stats["skipped"] += 1
            continue

        answer = data.get("answer") or data.get("final_answer") or data.get("response") or data.get("final_answer_raw") or ""
        trust = data.get("trust_label") or data.get("trust") or "UNVERIFIED"
        latency = data.get("latency", 0)

        if not answer or len(answer) < 50:
            print("  ✗ ответа нет")
            stats["skipped"] += 1
            continue

        print(f"  ✓ ответ: {len(answer)} символов, {latency:.1f}s")

        # ── ОЦЕНКА КАЧЕСТВА ЧЕРЕЗ TRACE_METRICS ──
        try:
            trace = load_last_trace(question)
            if trace:
                trace_score = _metrics.evaluate(trace)
                stats["scores"].append(trace_score.total_score)
                stats["total_score"] = sum(stats["scores"]) / len(stats["scores"])
                
                # Сохраняем метрики
                metric_record = {
                    "timestamp": datetime.now().isoformat(),
                    "question": question[:100],
                    "total_score": trace_score.total_score,
                    "passed": trace_score.passed,
                    "intent_score": trace_score.intent_score,
                    "epistemic_score": trace_score.epistemic_score,
                    "planner_score": trace_score.planner_score,
                    "evidence_score": trace_score.evidence_score,
                    "claim_score": trace_score.claim_score,
                    "belief_score": trace_score.belief_score,
                    "answer_score": trace_score.answer_score,
                    "reflection_score": trace_score.reflection_score,
                    "summary": trace_score.summary,
                }
                save_metric(metric_record)
                print(f"  📊 Оценка: {trace_score.total_score:.2f} ({'✅' if trace_score.passed else '❌'})")
            else:
                print("  ⚠️ Трейс не найден для оценки")
        except Exception as e:
            print(f"  ⚠️ Ошибка оценки: {e}")

        # ── ВАЛИДАЦИЯ ЧЕРЕЗ GPT-5.5 ──
        print("  → Валидация GPT-5.5...", end="", flush=True)
        try:
            val = codex_validate(question, answer)
            verdict = val["verdict"]
            print(f" {verdict}")
            if val["correction"]:
                print(f"     ✗ {val['correction'][:120]}")
            if val["supplement"]:
                print(f"     + {val['supplement'][:120]}")

            steps = [
                {
                    "step": 1,
                    "role": "initial_answer",
                    "model": "qwen",
                    "text": answer,
                    "trust": trust,
                    "latency": latency,
                },
                {
                    "step": 2,
                    "role": "critique",
                    "model": "gpt-5.5",
                    "verdict": verdict,
                    "correction": val["correction"],
                    "supplement": val["supplement"],
                    "full_response": val["raw"],
                },
            ]

            # Шаг 3 — Qwen рефайн (если есть замечания)
            if verdict != "VERIFIED":
                print("  → Qwen рефайн...", end="", flush=True)
                refined = qwen_refine(
                    question=question,
                    initial_answer=answer,
                    correction=val["correction"],
                    supplement=val["supplement"],
                    critic_model="GPT-5.5",
                )
                if refined["ok"]:
                    print(f" {refined['latency']}s")
                    steps.append({
                        "step": 3,
                        "role": "refined_answer",
                        "model": "qwen",
                        "text": refined["text"],
                        "trust": refined["trust"],
                        "latency": refined["latency"],
                    })
                else:
                    print(f" ошибка: {refined['error']}")

            # Цепочка
            record = {
                "id": uuid.uuid4().hex[:8],
                "question": question,
                "steps": steps,
                "verdict": verdict,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

            path = save_chain(record)
            print(f"  ✓ сохранено: {path.name}")

            stats["total"] += 1
            key = verdict.lower().replace("partially_verified", "partial")
            if key in stats:
                stats[key] += 1

            if i < len(questions) - 1:
                time.sleep(pause)

        except Exception as e:
            print(f" ошибка: {e}")
            stats["skipped"] += 1
            continue

    return stats


def print_regression_report(stats: dict):
    """Вывести регрессионный отчёт."""
    print("\n" + "=" * 60)
    print("📊 РЕГРЕССИОННЫЙ ОТЧЁТ")
    print("=" * 60)
    print(f"  Всего записей: {stats['total']}")
    if stats['scores']:
        print(f"  Средний балл:  {stats['total_score']:.3f}")
        print(f"  Минимальный:   {min(stats['scores']):.3f}")
        print(f"  Максимальный:  {max(stats['scores']):.3f}")
    else:
        print("  Средний балл:  N/A")
    print(f"  VERIFIED:      {stats['verified']}")
    print(f"  SUPPLEMENTED:  {stats['supplemented']}")
    print(f"  PARTIAL:       {stats['partial']}")
    print(f"  REJECTED:      {stats['rejected']}")
    print(f"  UNVERIFIED:    {stats['unverified']}")


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Автономный сборщик датасета")
    parser.add_argument("--limit", type=int, default=len(QUESTIONS),
                        help="Сколько вопросов прогнать (по умолчанию все)")
    parser.add_argument("--offset", type=int, default=0,
                        help="Начать с вопроса N")
    parser.add_argument("--compare", action="store_true",
                        help="Сравнить с предыдущим запуском (регрессия)")
    parser.add_argument("--pause", type=int, default=15,
                        help="Пауза между вопросами (секунды)")
    args = parser.parse_args()

    qs = QUESTIONS[args.offset: args.offset + args.limit]
    print(f"Запуск: {len(qs)} вопросов, пауза {args.pause}s")
    print(f"Датасет: {DATASET_DIR}")
    print(f"Метрики: {METRICS_FILE}\n")

    stats = run(qs, pause=args.pause)

    # Если нужно сравнение с предыдущим запуском
    if args.compare and METRICS_FILE.exists():
        prev_scores = []
        try:
            with open(METRICS_FILE, 'r', encoding='utf-8') as mf:
                for line in mf:
                    try:
                        prev_scores.append(json.loads(line))
                    except:
                        pass
        except:
            pass

        if prev_scores:
            prev_avg = sum(s.get('total_score', 0) for s in prev_scores) / len(prev_scores)
            current_avg = stats['total_score'] if stats['scores'] else 0
            delta = current_avg - prev_avg

            print("\n" + "=" * 60)
            print("📈 РЕГРЕССИЯ (сравнение с предыдущим запуском)")
            print("=" * 60)
            print(f"  Предыдущий средний: {prev_avg:.3f} ({len(prev_scores)} записей)")
            print(f"  Текущий средний:    {current_avg:.3f} ({len(stats['scores'])} записей)")
            print(f"  Изменение:          {delta:+.3f} ({'✅ улучшение' if delta > 0 else '❌ ухудшение' if delta < 0 else '➖ без изменений'})")

    print("\n── Итог ──────────────────────────────────────────────")
    print(f"  Всего записей: {stats['total']}")
    print(f"  Пропущено:     {stats['skipped']}")
    print(f"  VERIFIED:      {stats['verified']}")
    print(f"  SUPPLEMENTED:  {stats['supplemented']}")
    print(f"  PARTIAL:       {stats['partial']}")
    print(f"  REJECTED:      {stats['rejected']}")
    print(f"  UNVERIFIED:    {stats['unverified']}")
    if stats['scores']:
        print(f"\n  Средний балл:  {stats['total_score']:.3f}")
        print(f"  Минимальный:   {min(stats['scores']):.3f}")
        print(f"  Максимальный:  {max(stats['scores']):.3f}")

    # Регрессионный отчёт
    if stats['scores']:
        print_regression_report(stats)
