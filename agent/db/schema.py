"""
agent/db/schema.py — SQL-схемы для knowledge и traces баз.

Версия схемы: 1
Изменения версий фиксируются в schema_version и применяются через migrate.py.
"""

SCHEMA_VERSION = 1

# ── index.db ──────────────────────────────────────────────────────────────────
# Лёгкий индекс — всегда быстрый, синхронизируется между нодами первым.

INDEX_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS knowledge_index (
    id          TEXT    PRIMARY KEY,            -- md5(query.lower().strip())[:8]
    tag         TEXT    NOT NULL,               -- полный тег: science:astronomy
    category    TEXT    NOT NULL,               -- верхний уровень: science
    trust_level TEXT    NOT NULL DEFAULT 'UNVERIFIED',
    node_id     TEXT    NOT NULL DEFAULT '',    -- нода-источник (для сети)
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ki_category  ON knowledge_index(category);
CREATE INDEX IF NOT EXISTS idx_ki_tag       ON knowledge_index(tag);
CREATE INDEX IF NOT EXISTS idx_ki_trust     ON knowledge_index(trust_level);
CREATE INDEX IF NOT EXISTS idx_ki_node      ON knowledge_index(node_id);
CREATE INDEX IF NOT EXISTS idx_ki_updated   ON knowledge_index(updated_at);

CREATE TABLE IF NOT EXISTS traces_index (
    id          TEXT    PRIMARY KEY,            -- тот же id что в knowledge_index
    tag         TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    verdict     TEXT    NOT NULL DEFAULT 'UNVERIFIED',
    model_chain TEXT    NOT NULL DEFAULT '',    -- например: qwen→gpt-5.5
    node_id     TEXT    NOT NULL DEFAULT '',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ti_category  ON traces_index(category);
CREATE INDEX IF NOT EXISTS idx_ti_verdict   ON traces_index(verdict);
CREATE INDEX IF NOT EXISTS idx_ti_node      ON traces_index(node_id);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT    NOT NULL DEFAULT ''
);

-- P5 (verification memory) — lookup-only ACCELERATOR, not a second
-- epistemic store. registry/dataset/orch_traces/*.jsonl stays the sole
-- source of truth for claims/evidence/relations/Trust; this table only
-- answers "where do I find the record for this claim?" (locator =
-- jsonl filename + byte offset the trace was appended at, so a lookup
-- is one seek+readline, not a full-file scan). No Trust, no relation,
-- no evidence content, no quality metadata lives here — see
-- agent/verification_memory.py. Additive-only (CREATE TABLE IF NOT
-- EXISTS) — no SCHEMA_VERSION bump needed for a purely additive table.
CREATE TABLE IF NOT EXISTS claim_verification_index (
    trace_id            TEXT    NOT NULL,
    claim_id            TEXT    NOT NULL,
    content_hash        TEXT,                        -- exact-match key (agent/claim_identity.py)
    semantic_family_id  TEXT,                         -- fallback key (agent/claim_family_registry.py)
    jsonl_file          TEXT    NOT NULL,              -- e.g. "20260828.jsonl", relative to orch_traces/
    byte_offset         INTEGER NOT NULL,              -- f.tell() at the start of this trace's line
    observed_at         REAL    NOT NULL,
    PRIMARY KEY (trace_id, claim_id)
);
CREATE INDEX IF NOT EXISTS idx_cvi_content_hash ON claim_verification_index(content_hash);
CREATE INDEX IF NOT EXISTS idx_cvi_family       ON claim_verification_index(semantic_family_id);
CREATE INDEX IF NOT EXISTS idx_cvi_observed     ON claim_verification_index(observed_at);
"""

# ── knowledge/{category}.db ───────────────────────────────────────────────────
# Q&A база по категории. Синхронизируется между нодами кластера.

KNOWLEDGE_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS knowledge (
    id          TEXT    PRIMARY KEY,            -- md5(query.lower().strip())[:8]
    query       TEXT    NOT NULL,               -- оригинальный вопрос
    answer      TEXT    NOT NULL,               -- верифицированный ответ
    tag         TEXT    NOT NULL,               -- полный тег: science:astronomy
    trust_level TEXT    NOT NULL DEFAULT 'UNVERIFIED',
    confidence  REAL    NOT NULL DEFAULT 0.0,
    sources     TEXT    NOT NULL DEFAULT '[]',  -- JSON: список URL источников
    node_id     TEXT    NOT NULL DEFAULT '',    -- нода-источник
    version     INTEGER NOT NULL DEFAULT 1,     -- версия для разрешения конфликтов
    meta        TEXT    NOT NULL DEFAULT '{}',  -- JSON: расширяемые поля
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_k_tag        ON knowledge(tag);
CREATE INDEX IF NOT EXISTS idx_k_trust      ON knowledge(trust_level);
CREATE INDEX IF NOT EXISTS idx_k_node       ON knowledge(node_id);
CREATE INDEX IF NOT EXISTS idx_k_updated    ON knowledge(updated_at);
CREATE INDEX IF NOT EXISTS idx_k_confidence ON knowledge(confidence);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT    NOT NULL DEFAULT ''
);
"""

# ── traces/{category}.db ──────────────────────────────────────────────────────
# Трейсы получения ответов. Остаются локально, используются для дообучения.

TRACES_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS traces (
    id          TEXT    PRIMARY KEY,            -- тот же id что в knowledge
    question    TEXT    NOT NULL,               -- исходный вопрос
    steps       TEXT    NOT NULL DEFAULT '[]',  -- JSON: цепочка шагов
    verdict     TEXT    NOT NULL DEFAULT 'UNVERIFIED',
    model_chain TEXT    NOT NULL DEFAULT '',    -- модели участвовавшие в цепочке
    node_id     TEXT    NOT NULL DEFAULT '',
    meta        TEXT    NOT NULL DEFAULT '{}',  -- JSON: расширяемые поля
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_t_verdict    ON traces(verdict);
CREATE INDEX IF NOT EXISTS idx_t_node       ON traces(node_id);
CREATE INDEX IF NOT EXISTS idx_t_created    ON traces(created_at);

CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER PRIMARY KEY,
    applied_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    description TEXT    NOT NULL DEFAULT ''
);
"""
