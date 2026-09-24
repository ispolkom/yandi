"""
rustlib/gen_trust_data.py — генератор rustlib/yandi_rs/src/trust_data.rs.

Таблица рангов `_TRUST_ORDER` берётся ПРЯМО ИЗ Python-модуля agent/orchestrator/epistemic/trust_gate.py (её же
импортируют canonical_trust.py и pipeline.py) — а не переписывается руками: расхождение таблиц = тихая ошибка порядка доверия.
Перезапуск: python rustlib/gen_trust_data.py > rustlib/yandi_rs/src/trust_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.orchestrator.epistemic import trust_gate  # noqa: E402

print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_trust_data.py из agent/orchestrator/epistemic/trust_gate.py — не править руками.")
print()
print(f"pub static TRUST_ORDER: &[(&str, i64)] = &[")
for k, v in trust_gate._TRUST_ORDER.items():
    assert '"' not in k and "\\" not in k
    print(f'    ("{k}", {int(v)}),')
print("];")
