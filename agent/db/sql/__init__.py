"""
agent/db/sql/ — MySQL/Percona-compatible canonical epistemic memory
(P10 SQL migration, Этап 5).

Separate from agent/db/manager.py (the pre-existing SQLite-backed
KnowledgeDB) — that module is NOT touched by this package and stays
fully live during the shadow-dual-write phase. See MIGRATION_STATUS.md
in this directory for the current phase and what depends on what.
"""
