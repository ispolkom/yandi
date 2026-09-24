"""
rustlib/gen_fcc_data.py — генератор rustlib/yandi_rs/src/fcc_data.rs.

Список стоп-слов из `_content_words` (agent/final_claim_coverage.py — локальная переменная функции) берётся ПРЯМО ИЗ
исходника Python (ast), а не переписывается руками.
Перезапуск: python rustlib/gen_fcc_data.py > rustlib/yandi_rs/src/fcc_data.rs
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
src = (ROOT / "agent" / "final_claim_coverage.py").read_text(encoding="utf-8")
words = None
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.FunctionDef) and node.name == "_content_words":
        for st in ast.walk(node):
            if isinstance(st, ast.Assign) and any(getattr(t, "id", None) == "stopwords" for t in st.targets):
                words = sorted(ast.literal_eval(st.value))
assert words
print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_fcc_data.py из agent/final_claim_coverage.py — не править руками.")
print()
print("pub static STOPWORDS: &[&str] = &[" + ", ".join('"' + w + '"' for w in words) + "];")
