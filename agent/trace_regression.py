"""
agent/trace_regression.py — Регрессионное тестирование YANDI.

Сравнивает две версии системы (базовую и новую) по набору тестов.
Использует metrics.jsonl для анализа изменений.

Запуск:
    python -m agent.trace_regression.py --baseline registry/dataset/metrics.jsonl --new registry/dataset/metrics_new.jsonl
    python -m agent.trace_regression.py --compare --threshold 0.05
    python -m agent.trace_regression.py --report

Использование в коде:
    from agent.trace_regression import RegressionAnalyzer
    
    analyzer = RegressionAnalyzer()
    report = analyzer.compare(baseline_file, new_file)
    print(report.summary)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

METRICS_DIR = BASE / "registry" / "dataset"
DEFAULT_BASELINE = METRICS_DIR / "metrics.jsonl"


@dataclass
class StageDelta:
    """Изменение по одному этапу."""
    stage: str
    baseline_avg: float
    new_avg: float
    delta: float
    percent_change: float
    improved: bool
    significance: str  # "significant", "minor", "negligible"


@dataclass
class RegressionReport:
    """Отчёт о регрессии."""
    timestamp: str
    baseline_file: str
    new_file: str
    baseline_count: int
    new_count: int
    total_delta: float
    percent_change: float
    improved: bool
    significance: str
    stage_deltas: List[StageDelta]
    summary: str
    recommendations: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "baseline_file": self.baseline_file,
            "new_file": self.new_file,
            "baseline_count": self.baseline_count,
            "new_count": self.new_count,
            "total_delta": self.total_delta,
            "percent_change": self.percent_change,
            "improved": self.improved,
            "significance": self.significance,
            "summary": self.summary,
            "recommendations": self.recommendations,
            "stages": [
                {
                    "stage": s.stage,
                    "baseline": s.baseline_avg,
                    "new": s.new_avg,
                    "delta": s.delta,
                    "percent": s.percent_change,
                    "improved": s.improved,
                    "significance": s.significance
                }
                for s in self.stage_deltas
            ]
        }


class RegressionAnalyzer:
    """
    Анализ регрессии между двумя версиями системы.
    """
    
    def __init__(self, threshold: float = 0.05):
        self.threshold = threshold  # минимальное изменение для сигнала
        self.stage_names = [
            "intent_score",
            "epistemic_score",
            "planner_score",
            "evidence_score",
            "claim_score",
            "belief_score",
            "answer_score",
            "reflection_score"
        ]
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
    
    def load_metrics(self, file_path: Path) -> List[Dict[str, Any]]:
        """Загрузить метрики из файла."""
        if not file_path.exists():
            return []
        
        metrics = []
        with open(file_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                    metrics.append(data)
                except:
                    pass
        return metrics
    
    def _avg_stage(self, metrics: List[Dict[str, Any]], stage: str) -> float:
        """Среднее значение по этапу."""
        scores = [m.get(stage, 0) for m in metrics if m.get(stage) is not None]
        return sum(scores) / len(scores) if scores else 0.0
    
    def compare(self, baseline_file: Path, new_file: Path) -> RegressionReport:
        """
        Сравнить две версии.
        
        Args:
            baseline_file: файл с метриками базовой версии
            new_file: файл с метриками новой версии
        
        Returns:
            RegressionReport
        """
        baseline = self.load_metrics(baseline_file)
        new = self.load_metrics(new_file)
        
        if not baseline:
            return RegressionReport(
                timestamp=datetime.now().isoformat(),
                baseline_file=str(baseline_file),
                new_file=str(new_file),
                baseline_count=0,
                new_count=len(new),
                total_delta=0.0,
                percent_change=0.0,
                improved=False,
                significance="insufficient_data",
                stage_deltas=[],
                summary="❌ Нет данных для baseline"
            )
        
        if not new:
            return RegressionReport(
                timestamp=datetime.now().isoformat(),
                baseline_file=str(baseline_file),
                new_file=str(new_file),
                baseline_count=len(baseline),
                new_count=0,
                total_delta=0.0,
                percent_change=0.0,
                improved=False,
                significance="insufficient_data",
                stage_deltas=[],
                summary="❌ Нет данных для новой версии"
            )
        
        # Общий балл
        baseline_total = sum(m.get('total_score', 0) for m in baseline) / len(baseline)
        new_total = sum(m.get('total_score', 0) for m in new) / len(new)
        total_delta = new_total - baseline_total
        percent_change = (total_delta / baseline_total * 100) if baseline_total > 0 else 0
        
        # Поэтапное сравнение
        stage_deltas = []
        for stage in self.stage_names:
            baseline_avg = self._avg_stage(baseline, stage)
            new_avg = self._avg_stage(new, stage)
            delta = new_avg - baseline_avg
            
            # Определяем значимость
            abs_delta = abs(delta)
            if abs_delta > 0.1:
                significance = "significant"
            elif abs_delta > 0.05:
                significance = "minor"
            else:
                significance = "negligible"
            
            stage_deltas.append(StageDelta(
                stage=self.stage_labels.get(stage, stage),
                baseline_avg=baseline_avg,
                new_avg=new_avg,
                delta=delta,
                percent_change=(delta / baseline_avg * 100) if baseline_avg > 0 else 0,
                improved=delta > 0,
                significance=significance
            ))
        
        # Общая значимость
        if abs(percent_change) > 10:
            significance = "significant"
        elif abs(percent_change) > 5:
            significance = "minor"
        else:
            significance = "negligible"
        
        # Рекомендации
        recommendations = []
        for sd in stage_deltas:
            if sd.significance == "significant" and not sd.improved:
                recommendations.append(f"⚠️ {sd.stage} ухудшился на {abs(sd.delta):.3f} ({abs(sd.percent_change):.1f}%)")
            elif sd.significance == "significant" and sd.improved:
                recommendations.append(f"✅ {sd.stage} улучшился на {sd.delta:.3f} ({sd.percent_change:.1f}%)")
        
        # Сводка
        if total_delta > 0:
            status = "✅ УЛУЧШЕНИЕ"
        elif total_delta < 0:
            status = "❌ УХУДШЕНИЕ"
        else:
            status = "➖ БЕЗ ИЗМЕНЕНИЙ"
        
        summary = f"{status} (общий: {baseline_total:.3f} → {new_total:.3f}, {percent_change:+.1f}%)"
        
        return RegressionReport(
            timestamp=datetime.now().isoformat(),
            baseline_file=str(baseline_file),
            new_file=str(new_file),
            baseline_count=len(baseline),
            new_count=len(new),
            total_delta=total_delta,
            percent_change=percent_change,
            improved=total_delta > 0,
            significance=significance,
            stage_deltas=stage_deltas,
            summary=summary,
            recommendations=recommendations[:5]
        )
    
    def print_report(self, report: RegressionReport):
        """Вывести отчёт в консоль."""
        print("\n" + "=" * 70)
        print("📊 РЕГРЕССИОННЫЙ ОТЧЁТ")
        print("=" * 70)
        print(f"  Baseline: {report.baseline_file} ({report.baseline_count} записей)")
        print(f"  New:      {report.new_file} ({report.new_count} записей)")
        print(f"  Дата:     {report.timestamp}")
        print()
        print(f"  {report.summary}")
        print()
        print("  ─── ПОЭТАПНО ───")
        print(f"  {'Этап':<15} {'Было':>10} {'Стало':>10} {'Дельта':>10} {'Статус':>12}")
        print("  " + "-" * 65)
        
        for sd in report.stage_deltas:
            status = "✅" if sd.improved and sd.significance != "negligible" else "❌" if not sd.improved and sd.significance != "negligible" else "➖"
            print(f"  {sd.stage:<15} {sd.baseline_avg:>10.3f} {sd.new_avg:>10.3f} {sd.delta:>+10.3f} {status:>12}")
        
        print()
        if report.recommendations:
            print("  ─── РЕКОМЕНДАЦИИ ───")
            for rec in report.recommendations:
                print(f"    {rec}")
        
        print("=" * 70)


def main():
    """CLI для регрессионного анализа."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Регрессионный анализ YANDI")
    parser.add_argument("--baseline", type=str, help="Путь к файлу с baseline метриками")
    parser.add_argument("--new", type=str, help="Путь к файлу с новыми метриками")
    parser.add_argument("--compare", action="store_true", help="Сравнить два файла")
    parser.add_argument("--threshold", type=float, default=0.05, help="Порог значимости")
    parser.add_argument("--report", action="store_true", help="Показать отчёт")
    
    args = parser.parse_args()
    
    if args.compare or args.report:
        baseline_file = Path(args.baseline) if args.baseline else DEFAULT_BASELINE
        new_file = Path(args.new) if args.new else METRICS_DIR / "metrics_new.jsonl"
        
        if not baseline_file.exists():
            print(f"❌ Baseline не найден: {baseline_file}")
            sys.exit(1)
        
        if not new_file.exists():
            print(f"❌ New файл не найден: {new_file}")
            sys.exit(1)
        
        analyzer = RegressionAnalyzer(threshold=args.threshold)
        report = analyzer.compare(baseline_file, new_file)
        analyzer.print_report(report)
    
    else:
        # Статистика по метрикам
        if DEFAULT_BASELINE.exists():
            analyzer = RegressionAnalyzer()
            metrics = analyzer.load_metrics(DEFAULT_BASELINE)
            
            print(f"\n📊 СТАТИСТИКА МЕТРИК ({DEFAULT_BASELINE})")
            print("=" * 50)
            print(f"  Всего записей: {len(metrics)}")
            
            if metrics:
                total = sum(m.get('total_score', 0) for m in metrics) / len(metrics)
                print(f"  Средний балл:  {total:.3f}")
                
                # Этапы
                stages = ["intent_score", "epistemic_score", "planner_score", 
                         "evidence_score", "claim_score", "belief_score", 
                         "answer_score", "reflection_score"]
                labels = ["Intent", "Epistemic", "Planner", "Evidence", 
                         "Claims", "Beliefs", "Answer", "Reflection"]
                
                print("\n  ─── ПОЭТАПНО ───")
                for stage, label in zip(stages, labels):
                    avg = analyzer._avg_stage(metrics, stage)
                    print(f"    {label:<12}: {avg:.3f}")
        else:
            print(f"❌ Файл метрик не найден: {DEFAULT_BASELINE}")
            print("   Сначала запустите orch_dataset_runner для сбора метрик")


if __name__ == "__main__":
    main()
