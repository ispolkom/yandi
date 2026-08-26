"""
agent/trace_diff.py — Trace Diff Engine
Сравнение трассировок разных нод для обучения
"""

from typing import Dict, List, Any, Optional
from dataclasses import dataclass
import numpy as np


@dataclass
class DiffResult:
    """Результат сравнения двух трассировок"""
    section: str
    local_value: Any
    remote_value: Any
    similarity: float  # 0-1
    insight: Optional[str] = None


class TraceDiffEngine:
    """Сравнение трассировок для извлечения уроков"""
    
    def compare_traces(self, local_trace: Dict, remote_trace: Dict) -> Dict:
        """Сравнить локальный и удаленный трейсы"""
        diff_results = []
        
        # 1. Сравнение этапов выполнения
        diff_results.extend(self._compare_execution_steps(
            local_trace.get('execution', []),
            remote_trace.get('execution', [])
        ))
        
        # 2. Сравнение Claims
        diff_results.extend(self._compare_claims(
            local_trace.get('claims', []),
            remote_trace.get('claims', [])
        ))
        
        # 3. Сравнение Evidence
        diff_results.extend(self._compare_evidence(
            local_trace.get('evidence', []),
            remote_trace.get('evidence', [])
        ))
        
        # 4. Сравнение решений
        diff_results.extend(self._compare_decisions(
            local_trace.get('reasoning', []),
            remote_trace.get('reasoning', [])
        ))
        
        # 5. Сравнение уверенности
        local_conf = local_trace.get('trust_score', 0)
        remote_conf = remote_trace.get('trust_score', 0)
        diff_results.append(DiffResult(
            section='confidence',
            local_value=local_conf,
            remote_value=remote_conf,
            similarity=1 - abs(local_conf - remote_conf),
            insight=f"Confidence gap: {abs(local_conf - remote_conf):.2f}" if abs(local_conf - remote_conf) > 0.2 else None
        ))
        
        # Генерация инсайтов
        insights = self._generate_insights(diff_results, local_trace, remote_trace)
        
        return {
            'diffs': [d.__dict__ for d in diff_results],
            'insights': insights,
            'overall_similarity': float(np.mean([d.similarity for d in diff_results])),
            'summary': self._generate_summary(diff_results, insights)
        }
    
    def _compare_execution_steps(self, local_steps: List, remote_steps: List) -> List[DiffResult]:
        """Сравнение шагов выполнения"""
        results = []
        local_steps_map = {s.get('step'): s for s in local_steps if isinstance(s, dict)}
        remote_steps_map = {s.get('step'): s for s in remote_steps if isinstance(s, dict)}
        
        all_steps = set(local_steps_map.keys()) | set(remote_steps_map.keys())
        
        for step in all_steps:
            local = local_steps_map.get(step)
            remote = remote_steps_map.get(step)
            
            if local and remote:
                same_status = local.get('status') == remote.get('status')
                same_duration = abs(local.get('duration_ms', 0) - remote.get('duration_ms', 0)) < 1000
                similarity = (0.6 if same_status else 0) + (0.4 if same_duration else 0)
                results.append(DiffResult(
                    section=f'execution_{step}',
                    local_value=local.get('status', 'unknown'),
                    remote_value=remote.get('status', 'unknown'),
                    similarity=similarity,
                    insight=f"Step {step}: {'same path' if same_status else 'different path'}" if similarity < 0.8 else None
                ))
            else:
                results.append(DiffResult(
                    section=f'execution_{step}',
                    local_value='present' if local else 'absent',
                    remote_value='present' if remote else 'absent',
                    similarity=0.0,
                    insight=f"Step {step} {'only local' if local else 'only remote'}"
                ))
        
        return results
    
    def _compare_claims(self, local_claims: List, remote_claims: List) -> List[DiffResult]:
        """Сравнение Claims"""
        local_texts = set(c.get('claim_text', '') for c in local_claims if isinstance(c, dict))
        remote_texts = set(c.get('claim_text', '') for c in remote_claims if isinstance(c, dict))
        
        overlap = len(local_texts & remote_texts)
        total = len(local_texts | remote_texts)
        
        similarity = overlap / max(1, total)
        
        insight = None
        if similarity < 0.5:
            insight = f"Low claims overlap ({similarity*100:.0f}%) - different facts"
        elif similarity < 0.8:
            insight = f"Partial claims overlap ({similarity*100:.0f}%)"
        
        return [DiffResult(
            section='claims',
            local_value=f"{len(local_texts)} claims",
            remote_value=f"{len(remote_texts)} claims",
            similarity=similarity,
            insight=insight
        )]
    
    def _compare_evidence(self, local_evidence: List, remote_evidence: List) -> List[DiffResult]:
        """Сравнение Evidence"""
        local_sources = set(e.get('source_uri', '') for e in local_evidence if isinstance(e, dict))
        remote_sources = set(e.get('source_uri', '') for e in remote_evidence if isinstance(e, dict))
        
        overlap = len(local_sources & remote_sources)
        total = len(local_sources | remote_sources)
        
        similarity = overlap / max(1, total)
        
        insight = None
        if similarity < 0.3:
            insight = "Completely different sources - cross-check valuable"
        elif similarity < 0.7:
            insight = f"Partial source overlap ({overlap}/{total})"
        
        return [DiffResult(
            section='evidence',
            local_value=f"{len(local_sources)} sources",
            remote_value=f"{len(remote_sources)} sources",
            similarity=similarity,
            insight=insight
        )]
    
    def _compare_decisions(self, local_decisions: List, remote_decisions: List) -> List[DiffResult]:
        """Сравнение решений"""
        if not isinstance(local_decisions, list):
            local_decisions = []
        if not isinstance(remote_decisions, list):
            remote_decisions = []
            
        local_decisions_map = {d.get('step'): d.get('decision') for d in local_decisions if isinstance(d, dict)}
        remote_decisions_map = {d.get('step'): d.get('decision') for d in remote_decisions if isinstance(d, dict)}
        
        results = []
        all_steps = set(local_decisions_map.keys()) | set(remote_decisions_map.keys())
        
        for step in all_steps:
            local = local_decisions_map.get(step, 'none')
            remote = remote_decisions_map.get(step, 'none')
            similarity = 1.0 if local == remote else 0.3
            
            results.append(DiffResult(
                section=f'decision_{step}',
                local_value=local,
                remote_value=remote,
                similarity=similarity,
                insight=f"Different decision for {step}: {local} vs {remote}" if similarity < 0.5 else None
            ))
        
        return results
    
    def _generate_insights(self, diffs: List[DiffResult], local_trace: Dict, remote_trace: Dict) -> List[str]:
        """Генерация инсайтов для обучения"""
        insights = []
        
        # Поиск значимых различий
        for diff in diffs:
            if diff.similarity < 0.3 and diff.insight:
                insights.append(f"🔴 {diff.insight}")
            elif diff.similarity < 0.7 and diff.insight:
                insights.append(f"🟡 {diff.insight}")
        
        # Анализ причин расхождений
        if len(insights) > 3:
            insights.append("📊 Multiple differences detected - consider federation learning")
        
        # Если разошлись в уверенности
        conf_diff = next((d for d in diffs if d.section == 'confidence'), None)
        if conf_diff and conf_diff.similarity < 0.7:
            insights.append(f"🎯 Confidence mismatch: local={conf_diff.local_value:.2f}, remote={conf_diff.remote_value:.2f}")
        
        return insights
    
    def _generate_summary(self, diffs: List[DiffResult], insights: List[str]) -> str:
        """Генерация краткого резюме"""
        total = len(diffs)
        different = sum(1 for d in diffs if d.similarity < 0.5)
        
        if different == 0:
            return "✅ Traces are very similar - high agreement"
        elif different < total * 0.3:
            return f"🟡 Traces mostly similar ({total - different}/{total} sections agree)"
        else:
            return f"🔴 Significant differences ({different}/{total} sections differ)"
