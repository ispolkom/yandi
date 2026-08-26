#!/usr/bin/env python3
"""
assistant/active_sampler.py — активное обучение: анализ пробелов в датасете,
запросы к совету, генерация синтетических диалогов.

ActiveSampler:
  analyze()           — распределение тем, выявление пробелов
  request_topics()    — рассылает запрос совету на обсуждение дефицитных тем
  generate_synthetic()— template-based синтетика для конкретной темы
  run()               — полный цикл, пишет отчёт + synthetic JSONL

Команды:
  python3 active_sampler.py analyze
  python3 active_sampler.py request
  python3 active_sampler.py synthetic [topic]
  python3 active_sampler.py run
"""

from __future__ import annotations

import json
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import redis

BASE          = Path(__file__).parent.parent
DATASET_DIR   = BASE / "registry" / "dataset"
FINAL_DIR     = DATASET_DIR / "final"
SYNTHETIC_DIR = DATASET_DIR / "synthetic"
REPORT_KEY    = "council:skill:reports"
REPORT_CH     = "council:skill:report"
CHAT_CH       = "council:chat:pubsub"
CTRL_CH       = "council:daemon:control"

SYNTHETIC_DIR.mkdir(parents=True, exist_ok=True)

# ── тематические затравки ─────────────────────────────────────────────────────

TOPIC_SEEDS: dict[str, list[dict]] = {
    "council": [
        {"q": "Как устроена мультиагентная система совета ИИ?",
         "a": "Совет состоит из нескольких моделей (Claude, GPT, DeepSeek), которые получают одинаковый вопрос и дают независимые ответы. Координатор собирает ответы и формирует консенсус."},
        {"q": "Какие методы консенсуса используются в мультиагентных системах?",
         "a": "Основные подходы: мажоритарное голосование (>50%), взвешенное голосование по уверенности, итеративный дебаттинг, метод Дельфи с несколькими раундами."},
        {"q": "Как обрабатываются конфликтующие ответы моделей?",
         "a": "При расхождении запускается второй раунд с уточняющим запросом. Если консенсус не достигнут — ответ помечается флагом 'low_confidence' и отправляется на ревью человеку."},
        {"q": "Что такое валидация датасета через совет?",
         "a": "Спорные записи рассылаются всем моделям с запросом KEEP/REJECT. Запись сохраняется, если ≥2/3 моделей голосуют за KEEP. Это защищает от шума и дублей."},
        {"q": "Как реализована отказоустойчивость совета при недоступности модели?",
         "a": "При таймауте модели её голос исключается из подсчёта. Консенсус считается по фактически ответившим. Если ответила только одна модель — решение откладывается."},
        {"q": "Какова роль Redis в архитектуре совета?",
         "a": "Redis служит шиной событий (pub/sub) и хранилищем состояния. Каналы: council:chat:pubsub для сообщений, council:daemon:control для команд, council:skill:report для отчётов скиллов."},
        {"q": "Как реализован мониторинг ответов моделей в реальном времени?",
         "a": "content_scripts в браузерном расширении наблюдают DOM чата, детектируют новые ответы и публикуют их в Redis через WebSocket. Демон подписан на канал и обрабатывает события."},
        {"q": "Чем отличается broadcast от unicast в совете?",
         "a": "Broadcast рассылает сообщение всем моделям одновременно через общий Redis-канал. Unicast направлен конкретной модели по отдельному каналу — используется для уточняющих запросов."},
    ],
    "code": [
        {"q": "Как реализовать неблокирующую обработку событий в Python?",
         "a": "Используйте threading.Thread с daemon=True для фоновых задач. Или asyncio.create_task() в async-коде. Главное — не вызывать блокирующий IO в основном event loop."},
        {"q": "Что такое daemon thread в Python и зачем он нужен?",
         "a": "Daemon thread завершается автоматически при завершении главного потока, не блокируя выход программы. Используется для фоновых задач: мониторинг, очереди, watchdog."},
        {"q": "Как безопасно запускать shell-команды из Python?",
         "a": "subprocess.run() с shell=False и явным списком аргументов. Никогда не интерполируйте пользовательский ввод в строку команды — это RCE. Валидируйте prefixes командной строки."},
        {"q": "Как реализовать watchdog для мониторинга файлов?",
         "a": "Библиотека watchdog использует inotify на Linux. При недоступности — polling по mtime каждые N секунд. Для production предпочтительнее inotify как более эффективный."},
        {"q": "Чем HDBSCAN лучше K-Means для кластеризации текстов?",
         "a": "HDBSCAN не требует заранее задавать число кластеров, устойчив к выбросам (шуму), находит кластеры произвольной формы. K-Means быстрее, но чувствителен к числу K и плохо работает с неравномерными кластерами."},
        {"q": "Как работает FAISS для семантического поиска?",
         "a": "FAISS строит индекс векторов эмбеддингов и ищет ближайших соседей. IndexFlatIP — точный поиск по inner product. Для больших датасетов используют IVF (inverted file) с квантизацией."},
        {"q": "Как устроены sentence-transformers для многоязычного текста?",
         "a": "Модели типа paraphrase-multilingual-MiniLM обучены на параллельных корпусах 50+ языков. Выдают 384-мерные векторы, близкие для семантически похожих фраз независимо от языка."},
        {"q": "Как реализовать idempotent операции в распределённой системе?",
         "a": "Используйте уникальный ключ (hash входных данных или UUID) для проверки в хранилище перед выполнением. При дублированном запросе возвращайте кешированный результат без повторного выполнения."},
    ],
    "culinary": [
        {"q": "Как правильно карамелизировать лук?",
         "a": "Нарежьте тонкими полукольцами. Жарьте на среднем огне в масле, помешивая каждые 2-3 минуты, 30-40 минут. Не торопитесь — высокий огонь даёт горечь, а не сладость. Добавьте щепотку соли в начале."},
        {"q": "В чём разница между тушением и томлением?",
         "a": "Тушение — готовка в жидкости при 85-95°C, активное кипение. Томление — очень медленное приготовление при 70-80°C, жидкость едва движется. Томление даёт более нежную текстуру мяса."},
        {"q": "Как правильно темперировать шоколад?",
         "a": "Растопите до 50°C, охладите до 27°C помешиванием на мраморе (или таблинг), затем снова нагрейте до 31-32°C (тёмный) или 29-30°C (молочный). Правильная кристаллизация какао-масла даёт глянец и хруст."},
        {"q": "Что такое мирпуа и как его использовать?",
         "a": "Мирпуа — классическая французская смесь: 2 части лук : 1 часть морковь : 1 часть сельдерей. Основа большинства европейских соусов и бульонов. Обжаривается до мягкости перед добавлением жидкости."},
        {"q": "Как правильно приготовить ризотто?",
         "a": "Обжарьте рис арборио в масле 1-2 мин без жидкости. Добавляйте горячий бульон по половнику, постоянно помешивая. Крахмал высвобождается постепенно — получается кремовая консистенция. Mantecatura: в конце холодное масло + пармезан."},
        {"q": "Зачем отдыхать мясо после приготовления?",
         "a": "При нагреве мышечные волокна сжимаются, выдавливая соки к центру. За 5-10 минут отдыха соки перераспределяются обратно. Разрезанное сразу мясо теряет до 40% сока, отдохнувшее — менее 10%."},
        {"q": "Как сделать эмульсию для соуса винегрет?",
         "a": "Смешайте горчицу (эмульгатор) с уксусом, добавляйте масло тонкой струйкой при взбивании. Горчица содержит лецитин, стабилизирующий границу раздела фаз масло/вода. Пропорция масло:кислота = 3:1."},
        {"q": "Что такое sous vide и в чём преимущества метода?",
         "a": "Sous vide — приготовление в вакуумном пакете при точно контролируемой температуре (обычно 55-85°C). Преимущества: идеальная прожарка по всему объёму, сохранение соков, воспроизводимый результат, пастеризация без пересушивания."},
    ],
    "dataset": [
        {"q": "Что такое MinHash для дедупликации текста?",
         "a": "MinHash аппроксимирует сходство Жаккара между множествами шинглов (N-грамм). Вычисляется набор хеш-функций, берётся минимум каждой. Два текста схожи, если их MinHash-подписи совпадают на высокий процент."},
        {"q": "Как оценить качество обучающей выборки?",
         "a": "Метрики качества: информационная плотность (ratio уникальных токенов), распределение длин ответов, баланс тем, процент дублей, процент шума. Хороший датасет: низкий дубль (<5%), широкое покрытие тем, информативные ответы."},
        {"q": "Что такое активное обучение и зачем оно нужно?",
         "a": "Активное обучение — стратегия выбора новых обучающих примеров. Вместо случайной разметки выбираются примеры, наиболее неопределённые для текущей модели или покрывающие пробелы в датасете. Снижает стоимость разметки при том же качестве."},
        {"q": "Как устроен формат Hugging Face датасета?",
         "a": "HF датасет — JSONL или Parquet файлы с единым схемой. Каждая строка — один пример. Метаданные в dataset_dict.json. Загрузка через datasets.load_dataset(). Поддерживает streaming для больших датасетов."},
        {"q": "Зачем нужно разделение датасета на train/val/test?",
         "a": "Train — для обучения. Val — для подбора гиперпараметров и early stopping, не используется при обучении. Test — финальная оценка, используется один раз. Смешение сетов — data leakage, завышает метрики."},
    ],
}

# Темы, которые могут появиться, но у нас нет затравок
KNOWN_TOPICS = set(TOPIC_SEEDS.keys()) | {"unknown", "general", "noise"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _r() -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=6379, decode_responses=True)


def _publish(r: redis.Redis, report: dict):
    payload = json.dumps(report, ensure_ascii=False)
    r.lpush(REPORT_KEY, payload)
    r.ltrim(REPORT_KEY, 0, 49)
    r.publish(REPORT_CH, payload)


def _load_all_rows() -> list[dict]:
    """Загружает все записи из HF-датасета (все финальные файлы)."""
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


def _load_synthetic_rows() -> list[dict]:
    rows = []
    for f in sorted(SYNTHETIC_DIR.glob("*.jsonl")):
        with open(f, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if line:
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        pass
    return rows


# ── ActiveSampler ─────────────────────────────────────────────────────────────

class ActiveSampler:
    """
    Анализирует финальный датасет, определяет пробелы,
    запрашивает совет и генерирует синтетические примеры.
    """

    MIN_EXAMPLES    = 5   # минимум примеров на тему
    SYNTHETIC_PER   = 5   # сколько синтетических пар генерировать на тему
    REQUEST_DELAY   = 3.0 # пауза между рассылками совету (сек)

    def __init__(self, r: Optional[redis.Redis] = None):
        self.r = r or _r()

    # ── анализ ────────────────────────────────────────────────────────────────

    def analyze(self) -> dict:
        """Возвращает статистику тем и список дефицитных."""
        rows = _load_all_rows()
        synthetic = _load_synthetic_rows()

        topic_counts: Counter = Counter()
        topic_roles: dict[str, Counter] = defaultdict(Counter)

        for row in rows + synthetic:
            t = row.get("topic", "unknown")
            role = row.get("role", "unknown")
            topic_counts[t] += 1
            topic_roles[t][role] += 1

        sparse = {t: c for t, c in topic_counts.items()
                  if c < self.MIN_EXAMPLES and t not in ("noise", "unknown")}

        # Темы с затравками, которых вообще нет в датасете
        missing = {t: 0 for t in TOPIC_SEEDS if t not in topic_counts}

        return {
            "total_rows"   : len(rows),
            "synthetic_rows": len(synthetic),
            "topics"       : dict(topic_counts),
            "topic_roles"  : {t: dict(v) for t, v in topic_roles.items()},
            "sparse_topics": sparse,
            "missing_topics": missing,
            "needs_sampling": {**sparse, **missing},
        }

    # ── запрос к совету ───────────────────────────────────────────────────────

    def request_topics(self, topics: list[str]) -> int:
        """
        Рассылает в совет запросы на обсуждение дефицитных тем.
        Возвращает количество отправленных запросов.
        """
        if not topics:
            return 0

        sent = 0
        for topic in topics:
            seeds = TOPIC_SEEDS.get(topic, [])
            if seeds:
                seed = random.choice(seeds)
                question = seed["q"]
            else:
                question = f"Расскажите подробнее о теме: {topic}"

            msg = {
                "type"   : "user_message",
                "content": f"[ActiveSampler / тема: {topic}] {question}",
                "source" : "active_sampler",
                "topic"  : topic,
            }
            self.r.publish(CHAT_CH, json.dumps(msg, ensure_ascii=False))
            sent += 1
            if sent < len(topics):
                time.sleep(self.REQUEST_DELAY)

        return sent

    # ── синтетика ─────────────────────────────────────────────────────────────

    def generate_synthetic(self, topic: str, count: int = 0) -> list[dict]:
        """
        Генерирует синтетические пары Q/A из затравок для заданной темы.
        count=0 → использовать SYNTHETIC_PER.
        """
        seeds = TOPIC_SEEDS.get(topic, [])
        if not seeds:
            return []

        n = count or self.SYNTHETIC_PER
        chosen = random.choices(seeds, k=min(n, len(seeds)))
        if n > len(seeds):
            # повторить с небольшими вариациями (перемешать порядок слов в вопросе)
            while len(chosen) < n:
                chosen.append(random.choice(seeds))

        ts = datetime.now().isoformat(timespec="seconds")
        sid = f"synthetic_{topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        rows = []
        for i, seed in enumerate(chosen):
            rows.append({
                "session_id": sid,
                "topic"     : topic,
                "time_start": ts,
                "role"      : "human",
                "content"   : seed["q"],
                "score"     : 80,
                "source"    : "active_sampler_synthetic",
            })
            rows.append({
                "session_id": sid,
                "topic"     : topic,
                "time_start": ts,
                "role"      : "assistant",
                "content"   : seed["a"],
                "score"     : 80,
                "source"    : "active_sampler_synthetic",
            })
        return rows

    def _save_synthetic(self, rows: list[dict], topic: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = SYNTHETIC_DIR / f"synthetic_{topic}_{stamp}.jsonl"
        with open(out, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return out

    # ── полный цикл ───────────────────────────────────────────────────────────

    def run(self, send_requests: bool = False) -> dict:
        """
        Полный цикл активного сэмплинга:
        1. Анализ датасета
        2. Генерация синтетики для дефицитных тем
        3. (Опционально) Рассылка запросов совету
        """
        analysis = self.analyze()
        needs = analysis["needs_sampling"]

        synthetic_files: list[str] = []
        synthetic_total = 0

        for topic, existing in needs.items():
            shortage = max(0, self.MIN_EXAMPLES - existing)
            want = min(shortage, self.SYNTHETIC_PER)
            if want <= 0 or topic not in TOPIC_SEEDS:
                continue
            rows = self.generate_synthetic(topic, want)
            if rows:
                path = self._save_synthetic(rows, topic)
                synthetic_files.append(str(path))
                synthetic_total += len(rows)

        requests_sent = 0
        if send_requests and needs:
            requests_sent = self.request_topics(
                [t for t in needs if t in TOPIC_SEEDS][:3]  # не более 3 запросов за раз
            )

        report = {
            "timestamp"      : datetime.now().isoformat(),
            "analysis"       : analysis,
            "synthetic_files": synthetic_files,
            "synthetic_rows" : synthetic_total,
            "requests_sent"  : requests_sent,
        }

        # сохранить отчёт
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rpt_path = DATASET_DIR / "validation_reports" / f"active_sampler_{stamp}.json"
        with open(rpt_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)

        _publish(self.r, {
            "skill"    : "active_sampler",
            "timestamp": report["timestamp"],
            "topics"   : analysis["topics"],
            "sparse"   : list(needs.keys()),
            "synthetic": synthetic_total,
            "requests" : requests_sent,
            "report"   : str(rpt_path),
        })

        return report


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "analyze"
    s = ActiveSampler()

    if cmd == "analyze":
        info = s.analyze()
        print(json.dumps(info, ensure_ascii=False, indent=2))

    elif cmd == "request":
        topics = list(info["needs_sampling"].keys()) if "info" in dir() else list(TOPIC_SEEDS.keys())
        n = s.request_topics(topics[:3])
        print(f"Отправлено запросов: {n}")

    elif cmd == "synthetic":
        topic = sys.argv[2] if len(sys.argv) > 2 else "council"
        rows = s.generate_synthetic(topic)
        path = s._save_synthetic(rows, topic)
        print(f"Синтетика записана: {path} ({len(rows)} строк)")

    elif cmd == "run":
        send = "--request" in sys.argv
        report = s.run(send_requests=send)
        print(json.dumps({
            "synthetic_rows" : report["synthetic_rows"],
            "synthetic_files": report["synthetic_files"],
            "sparse_topics"  : list(report["analysis"]["needs_sampling"].keys()),
            "requests_sent"  : report["requests_sent"],
        }, ensure_ascii=False, indent=2))

    else:
        print(f"Неизвестная команда: {cmd}")
        sys.exit(1)
