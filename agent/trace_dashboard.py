"""
agent/trace_dashboard.py — Консольная панель мониторинга качества YANDI.

Визуализирует метрики из metrics.jsonl в виде ASCII-графиков и таблиц.

Запуск:
    python -m agent.trace_dashboard.py          # Полная панель
    python -m agent.trace_dashboard.py --stages # Только этапы
    python -m agent.trace_dashboard.py --trend  # Тренд последних 20 записей
    python -m agent.trace_dashboard.py --watch  # Автообновление каждые 5 сек
    python -m agent.trace_dashboard.py --csv    # Экспорт в CSV
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime
from collections import deque

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

METRICS_FILE = BASE / "registry" / "dataset" / "metrics.jsonl"


class TraceDashboard:
    """
    Консольная панель для визуализации метрик.
    """
    
    def __init__(self, metrics_file: Path = METRICS_FILE):
        self.metrics_file = metrics_file
        self.stage_labels = {
            "intent_score": "Intent",
            "epistemic_score": "Epistemic",
            "planner_score": "Planner",
            "evidence_score": "Evidence",
            "claim_score": "Claims",
            "belief_score": "Beliefs",
            "answer_score": "Answer",
            "reflection_score": "Reflection"
        }
        self.stage_colors = {
            "intent_score": "\033[94m",      # синий
            "epistemic_score": "\033[96m",   # голубой
            "planner_score": "\033[92m",     # зелёный
            "evidence_score": "\033[93m",    # жёлтый
            "claim_score": "\033[95m",       # розовый
            "belief_score": "\033[91m",      # красный
            "answer_score": "\033[97m",      # белый
            "reflection_score": "\033[90m"   # серый
        }
        self.reset = "\033[0m"
    
    def load_metrics(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Загрузить метрики из файла."""
        if not self.metrics_file.exists():
            return []
        
        metrics = []
        with open(self.metrics_file, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    metrics.append(data)
                except:
                    pass
        
        if limit:
            return metrics[-limit:]
        return metrics
    
    def _bar(self, value: float, width: int = 20, label: str = "") -> str:
        """Создать ASCII-бар."""
        filled = int(value * width)
        empty = width - filled
        bar = "█" * filled + "░" * empty
        return f"{label} [{bar}] {value:.3f}"
    
    def _stage_bar(self, name: str, value: float, width: int = 30) -> str:
        """Создать цветной бар для этапа."""
        color = self.stage_colors.get(name, "\033[97m")
        filled = int(value * width)
        empty = width - filled
        bar = "█" * filled + "░" * empty
        label = self.stage_labels.get(name, name)
        return f"{color}{label:<12} [{bar}] {value:.3f}{self.reset}"
    
    def _status_icon(self, score: float) -> str:
        """Иконка статуса."""
        if score >= 0.8:
            return "🟢"
        elif score >= 0.6:
            return "🟡"
        else:
            return "🔴"
    
    def show_dashboard(self):
        """Показать полную панель."""
        metrics = self.load_metrics()
        
        if not metrics:
            print("❌ Нет данных. Сначала запустите orch_dataset_runner")
            return
        
        # Очистка экрана
        print("\033[2J\033[H")
        
        # Заголовок
        print("=" * 70)
        print("📊 YANDI TRACE DASHBOARD")
        print("=" * 70)
        print(f"  Всего записей: {len(metrics)}")
        print(f"  Обновлено:     {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 70)
        
        # Общий балл
        total = sum(m.get('total_score', 0) for m in metrics) / len(metrics)
        passed = sum(1 for m in metrics if m.get('passed', False))
        
        print(f"\n  🎯 ОБЩИЙ БАЛЛ: {total:.3f}  ({self._status_icon(total)})")
        print(f"  ✅ Пройдено:   {passed}/{len(metrics)} ({passed/len(metrics)*100:.1f}%)")
        
        # Поэтапно
        print("\n  ─── ПОЭТАПНО ───")
        stages = ["intent_score", "epistemic_score", "planner_score", 
                  "evidence_score", "claim_score", "belief_score", 
                  "answer_score", "reflection_score"]
        
        for stage in stages:
            avg = sum(m.get(stage, 0) for m in metrics) / len(metrics)
            print(f"    {self._stage_bar(stage, avg)}")
        
        print("\n" + "=" * 70)
    
    def show_stages(self):
        """Показать только этапы."""
        metrics = self.load_metrics()
        
        if not metrics:
            print("❌ Нет данных")
            return
        
        stages = ["intent_score", "epistemic_score", "planner_score", 
                  "evidence_score", "claim_score", "belief_score", 
                  "answer_score", "reflection_score"]
        
        print("\n📊 СТАТИСТИКА ПО ЭТАПАМ")
        print("=" * 50)
        
        for stage in stages:
            avg = sum(m.get(stage, 0) for m in metrics) / len(metrics)
            min_val = min(m.get(stage, 0) for m in metrics)
            max_val = max(m.get(stage, 0) for m in metrics)
            label = self.stage_labels.get(stage, stage)
            
            print(f"\n  {label}:")
            print(f"    Средний: {avg:.3f}")
            print(f"    Мин:     {min_val:.3f}")
            print(f"    Макс:    {max_val:.3f}")
            print(f"    {self._bar(avg, width=30)}")
    
    def show_trend(self, limit: int = 20):
        """Показать тренд последних записей."""
        metrics = self.load_metrics(limit=limit)
        
        if not metrics:
            print("❌ Нет данных")
            return
        
        print(f"\n📈 ТРЕНД (последние {len(metrics)} записей)")
        print("=" * 60)
        
        # График ASCII
        scores = [m.get('total_score', 0) for m in metrics]
        width = 50
        max_score = max(scores) if scores else 1
        min_score = min(scores) if scores else 0
        range_score = max(0.01, max_score - min_score)
        
        print(f"  {self._status_icon(scores[-1]) if scores else ' '}  Текущий: {scores[-1] if scores else 0:.3f}")
        print(f"  📈  Макс:    {max_score:.3f}")
        print(f"  📉  Мин:     {min_score:.3f}")
        
        # Простой ASCII-график
        print("\n  ─── ГРАФИК ───")
        for i, score in enumerate(scores[-20:]):
            bar_width = int((score - min_score) / range_score * width) if range_score > 0 else 0
            bar = "█" * bar_width
            print(f"  {i:>3}| {bar} {score:.3f}")
        
        # Среднее скользящее
        if len(scores) > 3:
            ma = sum(scores[-3:]) / 3
            print(f"\n  📊 Скользящее среднее (3): {ma:.3f}")
            if len(metrics) > 6:
                ma_prev = sum(scores[-6:-3]) / 3
                delta = ma - ma_prev
                trend = "📈 растёт" if delta > 0.01 else "📉 падает" if delta < -0.01 else "➖ стабильно"
                print(f"  📊 Тренд: {trend} ({delta:+.3f})")
    
    def watch(self, interval: int = 5):
        """Автообновление панели."""
        try:
            while True:
                self.show_dashboard()
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\n👋 Выход...")
    
    def export_csv(self, output_file: Path = Path("/tmp/trace_metrics.csv")):
        """Экспорт в CSV."""
        metrics = self.load_metrics()
        
        if not metrics:
            print("❌ Нет данных")
            return
        
        import csv
        
        # Определяем все поля
        fields = ["timestamp", "question", "total_score", "passed",
                  "intent_score", "epistemic_score", "planner_score",
                  "evidence_score", "claim_score", "belief_score",
                  "answer_score", "reflection_score"]
        
        with open(output_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for m in metrics:
                row = {k: m.get(k, "") for k in fields}
                writer.writerow(row)
        
        print(f"✅ Экспортировано {len(metrics)} записей в {output_file}")


def main():
    """CLI для дашборда."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Trace Dashboard для YANDI")
    parser.add_argument("--stages", action="store_true", help="Показать только этапы")
    parser.add_argument("--trend", action="store_true", help="Показать тренд")
    parser.add_argument("--watch", action="store_true", help="Автообновление")
    parser.add_argument("--interval", type=int, default=5, help="Интервал обновления (сек)")
    parser.add_argument("--csv", type=str, help="Экспорт в CSV")
    
    args = parser.parse_args()
    
    dashboard = TraceDashboard()
    
    if args.csv:
        dashboard.export_csv(Path(args.csv))
    elif args.watch:
        dashboard.watch(interval=args.interval)
    elif args.trend:
        dashboard.show_trend()
    elif args.stages:
        dashboard.show_stages()
    else:
        dashboard.show_dashboard()


if __name__ == "__main__":
    main()
