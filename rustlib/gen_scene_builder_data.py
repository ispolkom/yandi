"""
rustlib/gen_scene_builder_data.py — генератор rustlib/yandi_rs/src/scene_builder_data.rs.

Таблицы паттернов SceneBuilder (агент/scene_builder.py) берутся ПРЯМО ИЗ ПИТОНА (атрибуты экземпляра и
литерал `topic_patterns` из тела `build`), а не переписываются руками: сотни русских слов с ё/е —
идеальное место для опечатки при ручном переносе.

Перезапуск: python rustlib/gen_scene_builder_data.py > rustlib/yandi_rs/src/scene_builder_data.rs
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent.scene_builder import SceneBuilder  # noqa: E402

sb = SceneBuilder()
src = (ROOT / "agent" / "scene_builder.py").read_text(encoding="utf-8")
topic_patterns = None
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "topic_patterns" for t in node.targets):
        topic_patterns = ast.literal_eval(node.value)
assert topic_patterns is not None


def lit(s):
    """Python-паттерн -> литерал Rust; паттерн r"\\?" — это просто символ '?'."""
    if s == r"\?":
        s = "?"
    assert "\\" not in s and '"' not in s, s
    return '"' + s + '"'


def arr(name, items, plain=True):
    print(f"pub static {name}: &[&str] = &[" + ", ".join(lit(i) for i in items) + "];")


def word_arr(name, items):
    """self_reference: r"\\bСЛОВО\\b" -> СЛОВО (границы слов реализует код)."""
    ws = []
    for p in items:
        assert p.startswith(r"\b") and p.endswith(r"\b"), p
        ws.append(p[2:-2])
    print(f"pub static {name}: &[&str] = &[" + ", ".join(lit(w) for w in ws) + "];")


def table(name, d):
    print(f"pub static {name}: &[(&str, &[&str])] = &[")
    for k, v in d.items():
        print(f'    ("{k}", &[' + ", ".join(lit(x) for x in v) + "]),")
    print("];")


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_scene_builder_data.py из agent/scene_builder.py — не править руками.")
print("//! Порядок элементов и ключей словарей — как в Python (влияет на ничьи: первый максимум побеждает).")
print()
arr("YANDI_NAMES", sb.yandi_names)
arr("AI_NAMES", sb.ai_names)
arr("TY_VERB_FORMS", sb.ty_verb_forms)
word_arr("SELF_REFERENCE_WORDS", sb.self_reference)
arr("GROUP_REFERENCE", sb.group_reference)
arr("COALITION_MARKERS", sb.coalition_markers)
table("SPEECH_ACT_PATTERNS", sb.speech_act_patterns)
table("TOPIC_PATTERNS", topic_patterns)
