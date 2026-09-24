"""
rustlib/gen_resolver_data.py — генератор rustlib/yandi_rs/src/resolver_data.rs.

Таблицы ObjectResolver (agent/object_resolver.py) и EntityResolver (agent/entity_resolver.py) берутся ПРЯМО ИЗ
Python-объектов, а не переписываются руками.
Перезапуск: python rustlib/gen_resolver_data.py > rustlib/yandi_rs/src/resolver_data.rs
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import entity_resolver as er  # noqa: E402
from agent.object_resolver import ObjectResolver  # noqa: E402


def lit(s):
    assert "\\" not in s and '"' not in s, s
    return '"' + s + '"'


def arr(name, items):
    print(f"pub static {name}: &[&str] = &[" + ", ".join(lit(i) for i in items) + "];")


print("//! АВТОГЕНЕРИРОВАНО rustlib/gen_resolver_data.py из agent/object_resolver.py и agent/entity_resolver.py — не править руками.")
print("//! Порядок типов и паттернов — как в Python dict/list (при равной уверенности выигрывает первый).")
print()
print("pub struct ObjectType { pub key: &'static str, pub patterns: &'static [&'static str], pub type_: &'static str, pub confidence: f64, pub analyzer: &'static str }")
print()
print("pub static OBJECT_TYPES: &[ObjectType] = &[")
for key, d in ObjectResolver().patterns.items():
    pats = ", ".join(lit(p) for p in d["patterns"])
    print(f'    ObjectType {{ key: {lit(key)}, patterns: &[{pats}], type_: {lit(d["type"])}, confidence: {d["confidence"]!r}, analyzer: {lit(d["analyzer"])} }},')
print("];")
print()
arr("KNOWN_GAMES", er.KNOWN_GAMES)
arr("KNOWN_GAME_TERMS", er.KNOWN_GAME_TERMS)
arr("KNOWN_MEDIA", er.KNOWN_MEDIA)
