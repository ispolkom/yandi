#!/usr/bin/env python3
"""
trace_inspector.py — ПОЛНЫЙ ИНСПЕКТОР КОГНИТИВНОГО ТРЕЙСА YANDI
Версия: 2.1

Запуск:
  python trace_inspector.py --full         # Полный человекочитаемый отчёт
  python trace_inspector.py --summary      # Краткий отчёт
  python trace_inspector.py --json         # Исходный JSON без изменений
  python trace_inspector.py --tree         # Дерево принятия решений
  python trace_inspector.py --graph        # Граф Claims → Evidence → Answer
  python trace_inspector.py --dag          # ПОЛНЫЙ DAG: Source → Evidence → Claim → Belief → Answer
  python trace_inspector.py --reasoning    # Только цепочка рассуждений
  python trace_inspector.py --beliefs      # Только изменение убеждений
  python trace_inspector.py --reflection   # Только рефлексия
  python trace_inspector.py --debug        # Максимально подробный режим
  python trace_inspector.py --unknown      # Показать все неизвестные поля

  python trace_inspector.py --last         # Последний трейс
  python trace_inspector.py --id <trace_id> # По ID
  python trace_inspector.py --file <path>   # Из файла
"""

import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List, Optional, Set, Tuple

BASE = Path(__file__).parent.parent
sys.path.insert(0, str(BASE))

TRACES_DIR = BASE / "registry" / "dataset" / "orch_traces"

# Известные поля для отслеживания неизвестных
KNOWN_TOP_LEVEL_FIELDS = {
    'trace_id', 'timestamp', 'query', 'goal', 'final_answer', 'execution',
    'reasoning', 'query_trace', 'evidence', 'claims', 'rejected_claims',
    'decisions', 'outcome', 'observations', 'learning', 'confidence_evolution',
    'trust', 'trust_reason', 'cost', 'epistemic', 'claims_filtered_count',
    'claims_rejected_count', 'rejected_reasoning', 'beliefs', 'belief_update',
    'reflection', 'source', 'version', 'dag'
}


def find_last_trace() -> Optional[str]:
    """Найти последний трейс."""
    if not TRACES_DIR.exists():
        return None
    
    files = sorted(TRACES_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    
    with open(files[0], 'r') as f:
        lines = f.readlines()
        if lines:
            return lines[-1].strip()
    return None


def find_trace_by_id(trace_id: str) -> Optional[str]:
    """Найти трейс по ID."""
    if not TRACES_DIR.exists():
        return None
    
    for f in TRACES_DIR.glob("*.jsonl"):
        with open(f, 'r') as file:
            for line in file:
                try:
                    data = json.loads(line.strip())
                    if data.get('trace_id') == trace_id:
                        return line.strip()
                except:
                    continue
    return None


def load_trace(source: str) -> Optional[Dict[str, Any]]:
    """Загрузить трейс из источника."""
    try:
        if source.startswith('{') or source.startswith('['):
            return json.loads(source)
        
        path = Path(source)
        if path.exists():
            with open(path, 'r') as f:
                return json.load(f)
        
        found = find_trace_by_id(source)
        if found:
            return json.loads(found)
        
        if source == 'last' or source == '--last':
            found = find_last_trace()
            if found:
                return json.loads(found)
        
        return None
    except Exception as e:
        print(f"Ошибка загрузки трейса: {e}", file=sys.stderr)
        return None


def print_separator(char: str = '=', length: int = 80):
    print(char * length)


def print_header(title: str):
    print_separator()
    print(f"{title}")
    print_separator()


def print_subheader(title: str):
    print(f"\n{'-' * 80}")
    print(f"{title}")
    print(f"{'-' * 80}")


def print_value(key: str, value: Any, indent: str = "", max_depth: int = 10, current_depth: int = 0):
    """Рекурсивный вывод любого значения без сокращений."""
    if current_depth > max_depth:
        print(f"{indent}{key}: ... (глубина превышена)")
        return
    
    if value is None:
        print(f"{indent}{key}: None")
    elif isinstance(value, (str, int, float, bool)):
        if isinstance(value, str) and len(value) > 2000:
            print(f"{indent}{key}: (длина {len(value)} символов)")
            print(f"{indent}--- начало ---")
            print(value)
            print(f"{indent}--- конец ---")
        else:
            print(f"{indent}{key}: {value}")
    elif isinstance(value, list):
        print(f"{indent}{key}: [{len(value)} элементов]")
        for i, item in enumerate(value):
            if isinstance(item, dict):
                print(f"{indent}  [{i}] (словарь)")
                for k, v in item.items():
                    print_value(k, v, f"{indent}    ", max_depth, current_depth + 1)
            elif isinstance(item, list):
                print(f"{indent}  [{i}] (список {len(item)} элементов)")
                for j, subitem in enumerate(item[:5]):
                    print_value(f"    [{j}]", subitem, f"{indent}      ", max_depth, current_depth + 1)
                if len(item) > 5:
                    print(f"{indent}    ... и ещё {len(item) - 5} элементов")
            else:
                print(f"{indent}  [{i}] {item}")
    elif isinstance(value, dict):
        if not value:
            print(f"{indent}{key}: {{}} (пусто)")
            return
        print(f"{indent}{key}:")
        for k, v in value.items():
            print_value(k, v, f"{indent}  ", max_depth, current_depth + 1)
    else:
        print(f"{indent}{key}: {value}")


def find_unknown_fields(data: Dict[str, Any], known_fields: Set[str], prefix: str = "") -> List[str]:
    """Рекурсивно найти неизвестные поля."""
    unknown = []
    if not isinstance(data, dict):
        return unknown
    
    for key in data.keys():
        full_key = f"{prefix}.{key}" if prefix else key
        if key not in known_fields:
            unknown.append(full_key)
        if isinstance(data[key], dict):
            unknown.extend(find_unknown_fields(data[key], known_fields, full_key))
        elif isinstance(data[key], list) and data[key]:
            if isinstance(data[key][0], dict):
                unknown.extend(find_unknown_fields(data[key][0], known_fields, full_key))
    
    return unknown


def inspect_full(trace: Dict[str, Any]):
    """Полный человекочитаемый отчёт."""
    print_header("TRACE INSPECTOR — ПОЛНЫЙ КОГНИТИВНЫЙ ТРЕЙС")
    print(f"Версия трейса: {trace.get('version', 'unknown')}")
    print(f"Дата: {datetime.now().isoformat()}")
    
    # 0. TRACE META
    print_subheader("0. TRACE META")
    print_value("trace_id", trace.get('trace_id'))
    print_value("timestamp", trace.get('timestamp'))
    if trace.get('timestamp'):
        print_value("timestamp_human", datetime.fromtimestamp(trace.get('timestamp')).isoformat())
    print_value("query", trace.get('query'))
    print_value("goal", trace.get('goal'))
    print_value("final_answer (raw)", trace.get('final_answer'))
    print_value("trust", trace.get('trust'))
    print_value("trust_reason", trace.get('trust_reason'))
    print_value("claims_filtered_count", trace.get('claims_filtered_count'))
    print_value("claims_rejected_count", trace.get('claims_rejected_count'))
    
    # EPISTEMIC
    print_subheader("EPISTEMIC (основной блок)")
    epistemic = trace.get('epistemic', {})
    if epistemic:
        print_value("epistemic", epistemic)
    else:
        print("  (пусто)")
    
    # 1. RAW USER INPUT
    print_subheader("1. RAW USER INPUT")
    print_value("query", trace.get('query'))
    qt = trace.get('query_trace', {})
    if qt:
        print_value("query_trace.query_text", qt.get('query_text'))
        print_value("query_trace.query_normalized", qt.get('query_normalized'))
        print_value("query_trace.intent", qt.get('intent'))
        print_value("query_trace.query_type", qt.get('query_type'))
    
    # 2. EXECUTION
    print_subheader("2. EXECUTION (выполнение шагов)")
    execution = trace.get('execution', [])
    print_value("execution", execution)
    
    # 3. REASONING
    print_subheader("3. REASONING (почему приняты решения)")
    reasoning = trace.get('reasoning', [])
    print_value("reasoning", reasoning)
    
    # 4. OBSERVATIONS
    print_subheader("4. OBSERVATIONS (наблюдения)")
    obs = trace.get('observations', {})
    print_value("observations", obs)
    
    # 5. EVIDENCE
    print_subheader("5. EVIDENCE (доказательства)")
    evidence = trace.get('evidence', [])
    print_value("evidence", evidence)
    
    # 6. CLAIMS
    print_subheader("6. CLAIMS (утверждения)")
    claims = trace.get('claims', [])
    print_value("claims", claims)
    
    # 7. REJECTED CLAIMS
    print_subheader("7. REJECTED CLAIMS (отвергнутые утверждения)")
    rejected = trace.get('rejected_claims', [])
    print_value("rejected_claims", rejected)
    
    # 8. DECISIONS
    print_subheader("8. DECISIONS (решения)")
    decisions = trace.get('decisions', [])
    print_value("decisions", decisions)
    
    # 9. OUTCOME
    print_subheader("9. OUTCOME (результат)")
    outcome = trace.get('outcome', {})
    print_value("outcome", outcome)
    
    # 10. LEARNING
    print_subheader("10. LEARNING (обучение)")
    learning = trace.get('learning', [])
    print_value("learning", learning)
    
    # 11. CONFIDENCE EVOLUTION
    print_subheader("11. CONFIDENCE EVOLUTION (эволюция уверенности)")
    conf_evol = trace.get('confidence_evolution', [])
    print_value("confidence_evolution", conf_evol)
    
    # 12. COST
    print_subheader("12. COST (время)")
    cost = trace.get('cost', {})
    total = cost.get('total_ms', 0)
    for k, v in cost.items():
        print_value(f"  {k}", f"{v:.2f}ms" if v else "0ms")
    print_value("  TOTAL", f"{total:.2f}ms ({total/1000:.2f}s)" if total else "0ms")
    
    # 13. UNKNOWN FIELDS
    print_subheader("13. UNKNOWN FIELDS (неизвестные поля)")
    all_fields = set(trace.keys())
    unknown = all_fields - KNOWN_TOP_LEVEL_FIELDS
    if unknown:
        print(f"  Найдены неизвестные поля: {sorted(unknown)}")
        for field in sorted(unknown):
            print_value(f"  {field}", trace.get(field))
    else:
        print("  Неизвестных полей не найдено")
    
    print_separator()
    print("КОНЕЦ ОТЧЁТА")


def inspect_summary(trace: Dict[str, Any]):
    """Краткий отчёт."""
    print_header("TRACE INSPECTOR — КРАТКИЙ ОТЧЁТ")
    
    print_value("trace_id", trace.get('trace_id'))
    print_value("query", trace.get('query'))
    print_value("trust", trace.get('trust'))
    
    obs = trace.get('observations', {})
    print_value("domain", obs.get('epistemic_domain'))
    print_value("testability", obs.get('epistemic_testability'))
    print_value("answer_mode", obs.get('epistemic_answer_mode'))
    
    print_value("claims", len(trace.get('claims', [])))
    print_value("rejected_claims", len(trace.get('rejected_claims', [])))
    print_value("evidence", len(trace.get('evidence', [])))
    print_value("decisions", len(trace.get('decisions', [])))
    print_value("learning_rules", len(trace.get('learning', [])))
    
    outcome = trace.get('outcome', {})
    answer = outcome.get('final_answer', trace.get('final_answer', ''))
    print_value("answer_length", len(answer))
    print_value("latency", f"{trace.get('cost', {}).get('total_ms', 0) / 1000:.2f}s")
    
    print("\n" + "=" * 80)
    print("ПОСЛЕДНИЙ ОТВЕТ (первые 500 символов):")
    print("=" * 80)
    print(answer[:500] + ("..." if len(answer) > 500 else ""))


def inspect_json(trace: Dict[str, Any]):
    """Вывод сырого JSON."""
    print(json.dumps(trace, indent=2, ensure_ascii=False, default=str))


def inspect_tree(trace: Dict[str, Any]):
    """Дерево принятия решений."""
    print_header("TRACE INSPECTOR — ДЕРЕВО РЕШЕНИЙ")
    
    print("📝 ЗАПРОС:")
    print(f"  {trace.get('query')}")
    print()
    
    print("🔀 ДЕРЕВО РЕШЕНИЙ:")
    reasoning = trace.get('reasoning', [])
    if not reasoning:
        print("  (нет данных reasoning)")
    else:
        for i, r in enumerate(reasoning):
            indent = "  " * min(i, 3)
            step = r.get('step', f'step_{i}')
            decision = r.get('decision', 'unknown')
            print(f"{indent}├── {step}")
            print(f"{indent}│   └── решение: {decision}")
            
            observed = r.get('observed', {})
            if observed:
                print(f"{indent}│       observed:")
                for k, v in observed.items():
                    if isinstance(v, (str, int, float, bool)):
                        print(f"{indent}│           {k}: {v}")
                    elif isinstance(v, dict):
                        print(f"{indent}│           {k}: (словарь)")
                        for k2, v2 in v.items():
                            if isinstance(v2, (str, int, float, bool)):
                                print(f"{indent}│               {k2}: {v2}")
            
            alternatives = r.get('alternatives', [])
            if alternatives:
                print(f"{indent}│       alternatives:")
                for alt in alternatives:
                    if isinstance(alt, dict):
                        for k, v in alt.items():
                            print(f"{indent}│           {k}: {v}")
                    else:
                        print(f"{indent}│           {alt}")
    
    print()
    print("📊 ИТОГ:")
    print(f"  trust: {trace.get('trust')}")
    print(f"  claims: {len(trace.get('claims', []))}")
    print(f"  rejected_claims: {len(trace.get('rejected_claims', []))}")
    print(f"  evidence: {len(trace.get('evidence', []))}")
    print(f"  latency: {trace.get('cost', {}).get('total_ms', 0) / 1000:.2f}s")


def inspect_graph(trace: Dict[str, Any]):
    """Граф Claims → Evidence → Answer."""
    print_header("TRACE INSPECTOR — ГРАФ ЗНАНИЙ")
    
    claims = trace.get('claims', [])
    evidence = trace.get('evidence', [])
    rejected_claims = trace.get('rejected_claims', [])
    outcome = trace.get('outcome', {})
    supporting = outcome.get('supporting_claim_ids', [])
    
    print(f"📚 CLAIMS (принято: {len(claims)}, отклонено: {len(rejected_claims)})")
    print()
    
    if claims:
        for c in claims:
            cid = c.get('claim_id')
            is_supported = "✅" if cid in supporting else "⬜"
            claim_type = c.get('claim_type', 'unknown')
            confidence = c.get('claim_confidence', 0)
            text = c.get('claim_text', '')
            ev_ids = c.get('derived_from_evidence_ids', [])
            
            print(f"  {is_supported} [{claim_type}] {cid} (conf: {confidence})")
            print(f"      {text}")
            if ev_ids:
                print(f"      evidence: {ev_ids}")
            print()
    
    if rejected_claims:
        print("  ❌ ОТВЕРГНУТЫЕ CLAIMS:")
        for rc in rejected_claims[:5]:
            print(f"      {rc.get('claim_text', '')[:100]}")
            print(f"      причина: {rc.get('rejection_reason', 'unknown')}")
            print()
    
    print(f"📎 EVIDENCE ({len(evidence)})")
    if evidence:
        for ev in evidence[:5]:
            print(f"  📄 {ev.get('evidence_id', 'unknown')}")
            print(f"      url: {ev.get('source_uri', '')}")
            print(f"      title: {ev.get('source_title', '')}")
            print(f"      relevance: {ev.get('relevance_to_query', 0)}")
            if ev.get('rejection_reason'):
                print(f"      ❌ REJECTED: {ev.get('rejection_reason')}")
            print()
    
    print(f"💬 ANSWER (supporting: {len(supporting)} claims)")
    answer = outcome.get('final_answer', trace.get('final_answer', ''))
    print()
    print(answer)


def inspect_dag(trace: Dict[str, Any]):
    """
    ПОЛНЫЙ DAG: Source → Evidence → Claim → Belief → Answer.
    Визуализация полного пути происхождения каждого утверждения в ответе.
    """
    print_header("TRACE INSPECTOR — ПОЛНЫЙ DAG (НАПРАВЛЕННЫЙ АЦИКЛИЧЕСКИЙ ГРАФ)")
    print("Путь: Source → Evidence → Claim → Belief → Answer")
    print_separator('-')
    
    query = trace.get('query', '')
    print(f"📝 ЗАПРОС: {query}")
    print_separator('-')
    
    # 1. SOURCES (источники)
    print("\n🔍 1. SOURCES (источники данных)")
    evidence = trace.get('evidence', [])
    sources = {}
    for ev in evidence:
        source_uri = ev.get('source_uri', 'unknown')
        source_title = ev.get('source_title', '')
        if source_uri not in sources:
            sources[source_uri] = {
                'title': source_title,
                'evidence_ids': []
            }
        sources[source_uri]['evidence_ids'].append(ev.get('evidence_id', 'unknown'))
    
    for idx, (uri, info) in enumerate(sources.items(), 1):
        print(f"  [{idx}] 📄 {uri}")
        if info['title']:
            print(f"      title: {info['title']}")
        print(f"      evidence: {', '.join(info['evidence_ids'][:3])}")
        if len(info['evidence_ids']) > 3:
            print(f"      ... и ещё {len(info['evidence_ids']) - 3} evidence")
        print()
    
    # 2. EVIDENCE → CLAIMS
    print("\n🔗 2. EVIDENCE → CLAIMS (доказательства → утверждения)")
    claims = trace.get('claims', [])
    rejected_claims = trace.get('rejected_claims', [])
    outcome = trace.get('outcome', {})
    supporting = outcome.get('supporting_claim_ids', [])
    
    # Строим маппинг evidence → claims
    ev_to_claims = {}
    for claim in claims:
        claim_id = claim.get('claim_id', 'unknown')
        ev_ids = claim.get('derived_from_evidence_ids', [])
        for ev_id in ev_ids:
            if ev_id not in ev_to_claims:
                ev_to_claims[ev_id] = []
            ev_to_claims[ev_id].append(claim_id)
    
    print(f"  Всего claims: {len(claims)}")
    print(f"  Поддерживающих claims: {len(supporting)}")
    print(f"  Отклонено claims: {len(rejected_claims)}")
    print()
    
    # Показываем связи
    for ev in evidence[:10]:  # ограничим для читаемости
        ev_id = ev.get('evidence_id', 'unknown')
        if ev_id in ev_to_claims:
            claim_ids = ev_to_claims[ev_id]
            print(f"  📄 {ev_id} → {len(claim_ids)} claims:")
            for cid in claim_ids[:3]:
                # Находим текст claim
                claim_text = ''
                for claim in claims:
                    if claim.get('claim_id') == cid:
                        claim_text = claim.get('claim_text', '')[:80]
                        break
                is_supported = "✅" if cid in supporting else "⬜"
                print(f"      {is_supported} {cid}: {claim_text}...")
            if len(claim_ids) > 3:
                print(f"      ... и ещё {len(claim_ids) - 3} claims")
            print()
    
    # 3. CLAIMS → BELIEFS
    print("\n🧠 3. CLAIMS → BELIEFS (утверждения → убеждения)")
    beliefs = trace.get('beliefs', trace.get('belief_update', {}))
    
    if beliefs:
        if isinstance(beliefs, dict):
            # Показываем структуру убеждений
            for topic, belief_data in beliefs.items():
                if isinstance(belief_data, dict):
                    confidence = belief_data.get('confidence', 0)
                    old_confidence = belief_data.get('old_confidence', 0)
                    statement = belief_data.get('statement', '')
                    print(f"  📌 {topic}:")
                    print(f"      statement: {statement[:100]}")
                    print(f"      confidence: {confidence:.2f} (было: {old_confidence:.2f})")
                    # Показываем связанные claims
                    claim_ids = belief_data.get('claim_ids', [])
                    if claim_ids:
                        print(f"      claims: {', '.join(claim_ids[:3])}")
                        if len(claim_ids) > 3:
                            print(f"      ... и ещё {len(claim_ids) - 3} claims")
                    print()
        elif isinstance(beliefs, list):
            for belief in beliefs[:5]:
                if isinstance(belief, dict):
                    print(f"  📌 {belief.get('topic', 'unknown')}:")
                    print(f"      {belief.get('statement', '')[:100]}")
                    print(f"      confidence: {belief.get('confidence', 0):.2f}")
                    print()
    else:
        print("  (нет данных об убеждениях)")
    
    # 4. BELIEFS → ANSWER
    print("\n💬 4. BELIEFS → ANSWER (убеждения → ответ)")
    final_answer = outcome.get('final_answer', trace.get('final_answer', ''))
    
    # Показываем, какие claim поддерживают ответ
    supporting_claims = []
    for claim in claims:
        if claim.get('claim_id') in supporting:
            supporting_claims.append(claim)
    
    print(f"  Ответ основан на {len(supporting_claims)} поддерживающих claims:")
    for claim in supporting_claims[:5]:
        text = claim.get('claim_text', '')[:100]
        confidence = claim.get('claim_confidence', 0)
        print(f"    ✅ {text}... (conf: {confidence:.2f})")
    if len(supporting_claims) > 5:
        print(f"    ... и ещё {len(supporting_claims) - 5} claims")
    
    print()
    print("  📝 ИТОГОВЫЙ ОТВЕТ:")
    print(f"  {final_answer[:500]}{'...' if len(final_answer) > 500 else ''}")
    
    # 5. FULL DAG SUMMARY
    print("\n" + "=" * 80)
    print("📊 FULL DAG SUMMARY:")
    print("=" * 80)
    print(f"  Sources: {len(sources)}")
    print(f"  Evidence: {len(evidence)}")
    print(f"  Claims: {len(claims)} (supporting: {len(supporting_claims)}, rejected: {len(rejected_claims)})")
    if beliefs:
        if isinstance(beliefs, dict):
            print(f"  Beliefs: {len(beliefs)}")
        elif isinstance(beliefs, list):
            print(f"  Beliefs: {len(beliefs)}")
    print(f"  Answer length: {len(final_answer)} символов")
    print(f"  Trust: {trace.get('trust', 'unknown')}")
    print(f"  Latency: {trace.get('cost', {}).get('total_ms', 0) / 1000:.2f}s")
    
    # 6. TRACE PATH (полный путь от источника к ответу)
    print("\n" + "=" * 80)
    print("🔗 ПОЛНЫЙ ПУТЬ (Source → Evidence → Claim → Belief → Answer):")
    print("=" * 80)
    
    # Строим цепочку для первого поддерживающего claim
    if supporting_claims:
        first_claim = supporting_claims[0]
        claim_id = first_claim.get('claim_id', 'unknown')
        claim_text = first_claim.get('claim_text', '')[:60]
        
        # Находим evidence для этого claim
        ev_ids = first_claim.get('derived_from_evidence_ids', [])
        if ev_ids:
            first_ev = ev_ids[0]
            # Находим источник для этого evidence
            source_uri = 'unknown'
            for ev in evidence:
                if ev.get('evidence_id') == first_ev:
                    source_uri = ev.get('source_uri', 'unknown')
                    break
            
            print(f"\n  Пример цепочки для claim: \"{claim_text}...\"")
            print(f"    📄 SOURCE: {source_uri}")
            print(f"       ↓")
            print(f"    📎 EVIDENCE: {first_ev}")
            print(f"       ↓")
            print(f"    📌 CLAIM: {claim_id}")
            print(f"       ↓")
            # Находим belief для этого claim
            if beliefs:
                belief_topic = 'unknown'
                for topic, belief_data in beliefs.items():
                    if isinstance(belief_data, dict):
                        if claim_id in belief_data.get('claim_ids', []):
                            belief_topic = topic
                            break
                print(f"    🧠 BELIEF: {belief_topic}")
            print(f"       ↓")
            print(f"    💬 ANSWER: {final_answer[:60]}...")
    
    print("\n" + "=" * 80)
    print("КОНЕЦ DAG")


def inspect_reasoning(trace: Dict[str, Any]):
    """Только цепочка рассуждений."""
    print_header("TRACE INSPECTOR — ЦЕПОЧКА РАССУЖДЕНИЙ")
    
    reasoning = trace.get('reasoning', [])
    if not reasoning:
        print("  (нет данных reasoning)")
        return
    
    for i, r in enumerate(reasoning):
        print(f"\n{'─' * 60}")
        print(f"Шаг {i+1}: {r.get('step', 'unknown')}")
        print(f"Решение: {r.get('decision', 'unknown')}")
        
        if r.get('observed'):
            print("\nНаблюдения:")
            for k, v in r.get('observed', {}).items():
                if isinstance(v, dict):
                    print(f"  {k}:")
                    for k2, v2 in v.items():
                        print(f"    {k2}: {v2}")
                else:
                    print(f"  {k}: {v}")
        
        if r.get('alternatives'):
            print("\nАльтернативы:")
            for alt in r.get('alternatives', []):
                if isinstance(alt, dict):
                    for k, v in alt.items():
                        print(f"  {k}: {v}")
                else:
                    print(f"  {alt}")
        
        if r.get('expected_gain') is not None:
            print(f"\nОжидаемый выигрыш: {r.get('expected_gain')}")


def inspect_beliefs(trace: Dict[str, Any]):
    """Только изменение убеждений."""
    print_header("TRACE INSPECTOR — ИЗМЕНЕНИЕ УБЕЖДЕНИЙ")
    
    beliefs = trace.get('beliefs', trace.get('belief_update', {}))
    if beliefs:
        print_value("beliefs", beliefs)
    else:
        print("  (нет данных об убеждениях)")
    
    learning = trace.get('learning', [])
    belief_rules = [l for l in learning if l.get('type') == 'belief']
    if belief_rules:
        print("\nПравила убеждений:")
        for l in belief_rules:
            print(f"  {l.get('rule')}")
    else:
        print("\n  (нет правил убеждений)")


def inspect_reflection(trace: Dict[str, Any]):
    """Только рефлексия."""
    print_header("TRACE INSPECTOR — РЕФЛЕКСИЯ")
    
    reflection = trace.get('reflection')
    if reflection:
        print_value("reflection", reflection)
    else:
        print("  (нет данных рефлексии)")
    
    learning = trace.get('learning', [])
    reflection_rules = [l for l in learning if l.get('type') == 'reflection']
    if reflection_rules:
        print("\nПравила рефлексии:")
        for l in reflection_rules:
            print(f"  {l.get('rule')}")


def inspect_unknown(trace: Dict[str, Any]):
    """Показать все неизвестные поля."""
    print_header("TRACE INSPECTOR — НЕИЗВЕСТНЫЕ ПОЛЯ")
    
    all_fields = set(trace.keys())
    unknown = all_fields - KNOWN_TOP_LEVEL_FIELDS
    
    if unknown:
        print(f"Найдены неизвестные поля на верхнем уровне: {sorted(unknown)}")
        for field in sorted(unknown):
            print(f"\nПоле: {field}")
            print_value("  значение", trace.get(field))
    else:
        print("Неизвестных полей на верхнем уровне не найдено")
    
    # Глубокий поиск
    nested_unknown = find_unknown_fields(trace, KNOWN_TOP_LEVEL_FIELDS)
    if nested_unknown:
        print(f"\nНайдены неизвестные поля в структурах: {sorted(nested_unknown)}")
    else:
        print("\nНеизвестных полей в структурах не найдено")


def inspect_debug(trace: Dict[str, Any]):
    """Максимально подробный режим."""
    print_header("TRACE INSPECTOR — DEBUG MODE")
    print(f"Всего полей в трейсе: {len(trace.keys())}")
    print(f"Ключи: {sorted(trace.keys())}")
    print()
    print(json.dumps(trace, indent=2, ensure_ascii=False, default=str))


def main():
    parser = argparse.ArgumentParser(description='Trace Inspector для YANDI')
    parser.add_argument('--full', action='store_true', help='Полный отчёт')
    parser.add_argument('--summary', action='store_true', help='Краткий отчёт')
    parser.add_argument('--json', action='store_true', help='Сырой JSON')
    parser.add_argument('--tree', action='store_true', help='Дерево решений')
    parser.add_argument('--graph', action='store_true', help='Граф знаний')
    parser.add_argument('--dag', action='store_true', help='Полный DAG: Source → Evidence → Claim → Belief → Answer')
    parser.add_argument('--reasoning', action='store_true', help='Цепочка рассуждений')
    parser.add_argument('--beliefs', action='store_true', help='Изменение убеждений')
    parser.add_argument('--reflection', action='store_true', help='Рефлексия')
    parser.add_argument('--unknown', action='store_true', help='Показать неизвестные поля')
    parser.add_argument('--debug', action='store_true', help='Максимально подробный режим')
    parser.add_argument('--last', action='store_true', help='Последний трейс')
    parser.add_argument('--id', help='Trace ID')
    parser.add_argument('--file', help='Путь к файлу с трейсом')
    
    args = parser.parse_args()
    
    source = None
    if args.last:
        source = 'last'
    elif args.id:
        source = args.id
    elif args.file:
        source = args.file
    
    if not source:
        source = 'last'
    
    trace = load_trace(source)
    if not trace:
        print(f"Не удалось загрузить трейс из источника: {source}", file=sys.stderr)
        sys.exit(1)
    
    if args.summary:
        inspect_summary(trace)
    elif args.json:
        inspect_json(trace)
    elif args.tree:
        inspect_tree(trace)
    elif args.graph:
        inspect_graph(trace)
    elif args.dag:
        inspect_dag(trace)
    elif args.reasoning:
        inspect_reasoning(trace)
    elif args.beliefs:
        inspect_beliefs(trace)
    elif args.reflection:
        inspect_reflection(trace)
    elif args.unknown:
        inspect_unknown(trace)
    elif args.debug:
        inspect_debug(trace)
    else:
        inspect_full(trace)


if __name__ == "__main__":
    main()
