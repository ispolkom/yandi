"""Репозитории Rust (rustlib/yandi_db/src/repo) на встроенной SQLite против Python-репозиториев (agent/db/sql/repositories.py) на НАСТОЯЩЕМ MySQL.

Одни и те же последовательности вызовов выполняются обеими сторонами на чистых базах; сравниваются результаты каждого вызова (значения, а не только успех/ошибка)
и итоговое содержимое ВСЕХ таблиц. Нужен личный тестовый MySQL: YANDI_PARITY_MYSQL=127.0.0.1:ПОРТ (root без пароля), иначе SKIP.
Времена во всех вызовах передаются явно (детерминизм); отдельно проверяются числовые метки Unix (округление к секунде, как у MySQL DATETIME(0)).
"""
from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []
_OK = 0


def check(name, cond, detail=""):
    global _OK
    if cond:
        _OK += 1
    else:
        if len(FAILURES) < 30:
            print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILURES.append(name)


def norm(v):
    """Приведение значений Python/MySQL к общей форме для сравнения."""
    if isinstance(v, dt.datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, dict):
        return {k: norm(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    return v


def canon(v):
    return json.dumps(norm(v), ensure_ascii=False, sort_keys=True)


def main() -> int:
    target = os.environ.get("YANDI_PARITY_MYSQL", "")
    if not target:
        print("SKIP: не задан YANDI_PARITY_MYSQL (личный тестовый MySQL)")
        return 0
    try:
        import yandi_db
    except ImportError as e:
        print(f"SKIP: yandi_db не собран ({e})")
        return 0
    import pymysql

    import agent.db.sql.repositories as repo
    import agent.db.sql.schema as S
    from agent.db.sql.security_triggers import immutability_triggers

    host, port = target.rsplit(":", 1)
    kw = dict(host=host, port=int(port), user="root", password="", autocommit=True, cursorclass=pymysql.cursors.DictCursor, charset="utf8mb4")
    admin = pymysql.connect(**kw)
    ac = admin.cursor()
    ac.execute("DROP DATABASE IF EXISTS yandi_repo_parity")
    ac.execute("CREATE DATABASE yandi_repo_parity CHARACTER SET utf8mb4")
    conn = pymysql.connect(database="yandi_repo_parity", **kw)
    cur = conn.cursor()
    for _, ddl in S.ALL_TABLES_IN_ORDER:
        cur.execute(ddl)
    for _, alter in S.ALTER_STATEMENTS_IN_ORDER:
        try:
            cur.execute(alter)
        except pymysql.err.OperationalError as e:
            if e.args[0] not in (1060, 1061):
                raise
    for _, trg in immutability_triggers():
        cur.execute(trg)
    tables = [n for n, _ in S.ALL_TABLES_IN_ORDER]
    cur.execute("SELECT table_name AS table_name, column_name AS column_name FROM information_schema.columns WHERE table_schema='yandi_repo_parity' AND data_type='json'")
    json_cols = {(r["table_name"], r["column_name"]) for r in cur.fetchall()}
    cur.execute("SELECT table_name AS table_name, column_name AS column_name FROM information_schema.columns WHERE table_schema='yandi_repo_parity' AND column_key='PRI' ORDER BY table_name, ordinal_position")
    pk = {}
    for r in cur.fetchall():
        pk.setdefault(r["table_name"], []).append(r["column_name"])

    state = {"h": None}

    def reset():
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t in tables:
            cur.execute(f"TRUNCATE TABLE {t}")  # TRUNCATE не вызывает триггеры и сбрасывает AUTO_INCREMENT
        cur.execute("SET FOREIGN_KEY_CHECKS=1")
        if state["h"] is not None:
            yandi_db.close(state["h"])
        state["h"] = yandi_db.open_memory()
        # схема миграций пишется базовой записью только у Rust — у MySQL её нет; чистим для честного сравнения содержимого
        yandi_db.query(state["h"], "SELECT 1")

    def py_call(func, kwargs):
        try:
            return {"ok": norm(getattr(repo, func)(conn, **kwargs))}
        except Exception as e:  # noqa: BLE001
            return {"error": type(e).__name__, "msg": str(e)[:200]}

    def rs_call(func, kwargs):
        return json.loads(yandi_db.call(state["h"], func, json.dumps(kwargs, ensure_ascii=False)))

    def dump_py():
        out = {}
        for t in tables:
            order = ", ".join(pk.get(t, ["1"]))
            cur.execute(f"SELECT * FROM {t} ORDER BY {order}")
            rows = []
            for r in cur.fetchall():
                r = norm(r)
                for c in list(r):
                    if (t, c) in json_cols and isinstance(r[c], str):
                        r[c] = json.loads(r[c])
                rows.append(r)
            out[t] = rows
        return out

    def dump_rs():
        out = {}
        for t in tables:
            order = ", ".join(pk.get(t, ["1"]))
            rows = json.loads(yandi_db.query(state["h"], f"SELECT * FROM {t} ORDER BY {order}"))
            for r in rows:
                for c in list(r):
                    if (t, c) in json_cols and isinstance(r[c], str):
                        r[c] = json.loads(r[c])
            out[t] = rows
        return out

    n = 0

    def scenario(label, calls):
        nonlocal n
        reset()
        n += 1
        for i, (func, kwargs) in enumerate(calls):
            p, r = py_call(func, kwargs), rs_call(func, kwargs)
            if "error" in p or "error" in r:
                ok = ("error" in p) == ("error" in r) and (p.get("error") == "ValueError") == str(r.get("error", "")).startswith("ValueError") \
                    and (p.get("error") != "AssertionError" or r.get("error") == "AssertionError")
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            check(f"{label} · вызов {i} {func}", ok, f"\n args={json.dumps(kwargs, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:600]}\n rs={json.dumps(r, ensure_ascii=False)[:600]}")
            if not ok:
                return
        dp, dr = dump_py(), dump_rs()
        dr.pop("schema_migrations", None)
        dp.pop("schema_migrations", None)
        for t in tables:
            if t in dp:
                check(f"{label} · таблица {t}", canon(dp[t]) == canon(dr[t]), f"\n py={canon(dp[t])[:700]}\n rs={canon(dr[t])[:700]}")

    T0, T1, T2 = "2026-03-01 10:00:00", "2026-03-01 10:00:05", "2026-03-01 10:01:00"

    # ---- вопросы, запуски, ответы ----
    base = [
        ("resolve_question", dict(raw_text="Сколько планет в Солнечной системе?", anonymized_text=None, asked_at=T0, session_id="s1")),
        ("resolve_question", dict(raw_text="сколько ПЛАНЕТ в  солнечной системе", anonymized_text="анон", asked_at=T1, session_id=None)),
        ("resolve_question", dict(raw_text="Другой вопрос", anonymized_text=None, asked_at=T1)),
        ("start_run", dict(run_id="run-1", occurrence_id=1, started_at=T0, web_enabled=True, validation_enabled=False, pipeline_version="abc123", schema_version=3)),
        ("start_run", dict(run_id="run-2", occurrence_id=2, started_at=T1)),
        ("start_run", dict(run_id="run-3", occurrence_id=3, started_at=T2)),
        ("record_answer_version", dict(question_id=1, answer_text="Восемь", run_id="run-1", created_at=T0)),
        ("record_answer_version", dict(question_id=1, answer_text="Восемь", run_id="run-2", created_at=T1)),
        ("record_answer_version", dict(question_id=1, answer_text="Девять?", run_id="run-2", created_at=T1)),
        ("record_answer_version", dict(question_id=1, answer_text="Восемь", run_id="run-3", created_at=T2)),
        ("record_answer_assessment", dict(answer_id=1, run_id="run-1", canonical_trust="HIGH", synthesizer_strand="s", diverged=True, stricter_strand="gate", reason="почему", created_at=T0)),
        ("record_answer_assessment", dict(answer_id=2, run_id="run-2", canonical_trust="LOW", created_at=T1)),
        ("complete_run", dict(run_id="run-1", completed_at=T1, final_answer_id=1)),
        ("complete_run", dict(run_id="run-1", completed_at=T2, final_answer_id=1)),  # повторно: status уже не running → 0 строк
        ("fail_run", dict(run_id="run-2", failed_stage="synthesis", error_class="KeyError", completed_at=T2)),
        ("fail_run", dict(run_id="run-3", failed_stage="x", error_class="Y", outcome="aborted", completed_at=T2)),
        ("fail_run", dict(run_id="run-3", failed_stage="x", error_class="Y", outcome="bogus")),
    ]
    scenario("A1 вопросы/запуски/ответы", base)
    scenario("A2 числовые метки Unix и строки со временем", [
        ("resolve_question", dict(raw_text="q", anonymized_text=None, asked_at=1767225600.0)),
        ("resolve_question", dict(raw_text="q2", anonymized_text=None, asked_at=1767225600.4999)),
        ("resolve_question", dict(raw_text="q3", anonymized_text=None, asked_at=1767225600.5)),
        ("resolve_question", dict(raw_text="q4", anonymized_text=None, asked_at=1767225600.999999)),
        ("resolve_question", dict(raw_text="q5", anonymized_text=None, asked_at="2026-01-01 00:00:00.7")),
        ("resolve_question", dict(raw_text="q6", anonymized_text=None, asked_at="2026-12-31 23:59:59.6")),
        ("resolve_question", dict(raw_text="q7", anonymized_text=None, asked_at="2026-01-01T05:06:07")),
        ("resolve_question", dict(raw_text="q8", anonymized_text=None, asked_at=0)),
    ])

    # ---- семейства утверждений и вхождения ----
    fam = [
        ("resolve_question", dict(raw_text="вопрос", anonymized_text=None, asked_at=T0)),
        ("start_run", dict(run_id="r1", occurrence_id=1, started_at=T0)),
        ("start_run", dict(run_id="r2", occurrence_id=1, started_at=T2)),
        ("get_or_create_claim_family", dict(family_id="f1", domain="science", canonical_text="Земля круглая", created_at=T0)),
        ("get_or_create_claim_family", dict(family_id="f1", domain="science", canonical_text="ДРУГОЙ ТЕКСТ", created_at=T2)),
        ("get_or_create_claim_family", dict(family_id="f2", domain="science", canonical_text="Вода мокрая", created_at=T0)),
        ("get_or_create_claim_family", dict(family_id="f3", domain="history", canonical_text="Рим пал", created_at=T1)),
        ("link_family_member", dict(family_id="f1", claim_id="c1", linked_at=T0)),
        ("link_family_member", dict(family_id="f1", claim_id="c1", linked_at=T2)),
        ("link_family_member", dict(family_id="f1", claim_id="c2", linked_at=T1)),
        ("list_claim_families_by_domain", dict(domain="science")),
        ("list_claim_families_by_domain", dict(domain="none")),
        ("get_claim_family", dict(family_id="f1")),
        ("get_claim_family", dict(family_id="nope")),
        ("record_claim_occurrence", dict(claim_id="c1", run_id="r1", claim_text="Земля круглая", content_hash="h1", claim_type="fact", claim_confidence=0.7, verification_status="supported", family_id="f1", query_context="ctx", support_count=2, contradiction_count=1)),
        ("record_claim_occurrence", dict(claim_id="c1", run_id="r2", claim_text="Земля круглая", content_hash="h1", claim_type=None, claim_confidence=None, verification_status=None, family_id="f1", query_context=None)),
        ("record_claim_occurrence", dict(claim_id="c9", run_id="r1", claim_text="другое", content_hash="h9", claim_type="fact", claim_confidence=0.1, verification_status="x", family_id=None, query_context=None)),
        ("find_claim_occurrences_by_content_hash", dict(content_hash="h1")),
        ("find_claim_occurrences_by_content_hash", dict(content_hash="h1", limit=1)),
        ("find_claim_occurrences_by_content_hash", dict(content_hash="h1", exclude_run_id="r2")),
        ("find_claim_occurrences_by_content_hash", dict(content_hash="h1", limit=1, exclude_run_id="")),
        ("find_claim_occurrences_by_family", dict(family_id="f1")),
        ("find_claim_occurrences_by_family", dict(family_id="zz")),
    ]
    scenario("B1 семейства и вхождения утверждений", fam)

    # ---- ресурсы, наблюдения, цепочка происхождения, доказательства ----
    obs = [
        ("resolve_question", dict(raw_text="вопрос", anonymized_text=None, asked_at=T0)),
        ("start_run", dict(run_id="r1", occurrence_id=1, started_at=T0)),
        ("start_run", dict(run_id="r2", occurrence_id=1, started_at=T1)),
        ("start_run", dict(run_id="r3", occurrence_id=1, started_at=T2)),
        ("get_or_create_resource", dict(resource_type="internet", canonical_uri="https://example.org/a", observed_at=T0)),
        ("get_or_create_resource", dict(resource_type="internet", canonical_uri="https://example.org/a", observed_at=T2)),
        ("get_or_create_resource", dict(resource_type="internet", canonical_uri="https://example.org/b", observed_at=T0)),
        ("get_or_create_resource", dict(resource_type="network_node", canonical_uri=None, node_id="n1", validator_id="v1", model_id="m1", observed_at=T0)),
        ("get_or_create_resource", dict(resource_type="ai_chat", canonical_uri="", observed_at=T0)),
        ("get_resource", dict(resource_id=1)),
        ("get_resource", dict(resource_id=99)),
        ("record_source_observation", dict(resource_id=1, run_id="r1", observation_route="internet", observed_at=T0, source_class="news", quality_score=0.7, content_excerpt="выдержка", evidence_id="ev_1", source_title="Заголовок", retrieval_query="запрос", retrieval_rank=1, relevance_to_query=0.9, authority=0.3, traceability=0.55, primaryness=0.1, is_meta_pipeline_output=False, is_subject_matter_evidence=True, source_cluster_id="cl1", retrieval_claim_id="c1", route_side="pro", subject_entities=["Земля", "🌍"], fact_candidates=["a", "б"], supports_query_aspect=["x"])),
        ("record_source_observation", dict(resource_id=1, run_id="r2", observation_route="local_memory", origin_observation_id=1, observed_at=T1, quality_score=0.95, retrieval_claim_id="")),
        ("record_source_observation", dict(resource_id=1, run_id="r3", observation_route="local_memory", origin_observation_id=2, observed_at=T2, source_cluster_id="cl3", is_meta_pipeline_output=True, is_subject_matter_evidence=False, subject_entities=[])),
        ("record_source_observation", dict(resource_id=2, run_id="r1", observation_route="ai_chat", observed_at=T0, rejection_reason="duplicate")),
        ("record_source_observation", dict(resource_id=2, run_id="r1", observation_route="ai_chat", observed_at=T0, rejection_reason="bogus_reason")),
        ("find_observation_id_for_replay", dict(resource_id=1, origin_run_id="r1")),
        ("find_observation_id_for_replay", dict(resource_id=1, origin_run_id="r9")),
        ("find_observation_id_for_replay", dict(resource_id=1, origin_run_id=None)),
        ("find_observation_id_for_replay", dict(resource_id=1, origin_run_id="")),
        ("get_source_observation", dict(observation_id=1)),
        ("get_source_observation", dict(observation_id=3)),
        ("get_source_observation", dict(observation_id=99)),
    ]
    scenario("C1 ресурсы и наблюдения", obs)
    ev_calls = obs[:-3] + [
        ("record_claim_occurrence", dict(claim_id="c1", run_id="r1", claim_text="t", content_hash="h", claim_type=None, claim_confidence=None, verification_status=None, family_id=None, query_context=None)),
        ("record_evidence_relation", dict(claim_id="c1", observation_id=1, relation="supports", directness=0.8, evidence_eligible=True, evidence_role="primary", counted_via="direct", created_at=T0)),
        ("record_evidence_relation", dict(claim_id="c1", observation_id=2, relation="contradicts", created_at=T1)),
        ("record_evidence_relation", dict(claim_id="c1", observation_id=3, relation="supports", directness=0.0, evidence_eligible=True, created_at=T2)),
        ("record_evidence_relation", dict(claim_id="c1", observation_id=4, relation="supports", created_at=T2)),
        ("record_evidence_relation", dict(claim_id="c2", observation_id=1, relation="supports", created_at=T2)),
        ("list_evidence_for_claim", dict(claim_id="c1")),
        ("list_evidence_for_claim", dict(claim_id="c2")),
        ("list_evidence_for_claim", dict(claim_id="zzz")),
    ]
    scenario("C2 доказательства для утверждения (цепочка происхождения)", ev_calls)
    misc = [
        ("resolve_question", dict(raw_text="вопрос", anonymized_text=None, asked_at=T0)),
        ("start_run", dict(run_id="r1", occurrence_id=1, started_at=T0)),
        ("record_trace_record", dict(run_id="r1", execution={"a": 1, "б": [1, 2.5, None, True]}, reasoning="строка", cost={"tokens": 12}, epistemic=None, outcome={"ok": True}, learning=[], confidence_evolution={"k": {"x": 0.1}}, rejected_claims=["a"], claims_filtered_count=3, claims_rejected_count=1, created_at=T0)),
        ("get_trace_record", dict(run_id="r1")),
        ("get_trace_record", dict(run_id="none")),
        ("record_delayed_validation_event", dict(event_id="e1", run_id="r1", trace_found=True, original_trust="HIGH", source="peer", verdict="confirmed", reason="р" * 700, raw="x" * 2500, created_at=T0)),
        ("record_delayed_validation_event", dict(event_id="e2", run_id="", trace_found=False, original_trust=None, source="peer", verdict="unknown", reason="", raw=None, created_at=T1)),
        ("record_delayed_validation_event", dict(event_id="e3", run_id="r1", trace_found=True, original_trust=None, source="s", verdict="v", created_at=T2)),
        ("list_delayed_validation_events", dict(run_id="r1")),
        ("list_delayed_validation_events", dict(run_id="r1", limit=1)),
        ("record_run_error", dict(run_id="r1", failed_stage="s", error_class="E", short_message="м" * 600, created_at=T0)),
        ("record_run_error", dict(run_id="r1", failed_stage="s", error_class="E", created_at=T1)),
        ("record_decision_event", dict(event_id="d2", run_id="r1", event_type="t", entity_type="claim", entity_id="c1", verdict="ok", domain="d", confidence=0.61234567, delta=-0.05, delta_factors={"f": 1}, reason="r", meta={"m": [1, 2]}, parent_event_id=None, duration_ms=12, policy_snapshot={"p": None}, policy_version="v1", orchestrator_version="o1", created_at=T1)),
        ("record_decision_event", dict(event_id="d1", run_id="r1", event_type="t", entity_type="claim", entity_id="c1", created_at=T1)),
        ("record_decision_event", dict(event_id="d0", run_id="r1", event_type="t", entity_type="claim", entity_id="c1", parent_event_id="d1", created_at=T0)),
        ("get_decision_trace", dict(run_id="r1")),
        ("get_decision_trace", dict(run_id="x")),
        ("record_ai_observation", dict(provider="openai", model_id="gpt", run_id="r1", prompt_identity="p1", answer_excerpt="ответ", provenance_mode_reported="SEARCH", live_search_used_reported="YES", provenance_parse_status="ok", observed_at=T0)),
        ("record_ai_observation", dict(provider="anthropic", model_id="claude", run_id="r1", prompt_identity=None, answer_excerpt=None, observed_at=T1)),
        ("record_ai_reported_source", dict(ai_observation_id=1, ordinal=2, reported_name="B", reported_uri="https://b")),
        ("record_ai_reported_source", dict(ai_observation_id=1, ordinal=1, reported_name="A", reported_uri=None)),
        ("record_ai_reported_source", dict(ai_observation_id=1, ordinal=None, reported_name=None, reported_uri=None)),
        ("get_ai_observations_for_run", dict(run_id="r1")),
        ("get_ai_observations_for_run", dict(run_id="none")),
    ]
    scenario("D1 трассы, решения, наблюдения ИИ", misc)

    print(f"\n(сценариев: {n}; успешных проверок: {_OK} из {_OK + len(FAILURES)})")
    print("=" * 72)
    ac.execute("DROP DATABASE IF EXISTS yandi_repo_parity")
    if FAILURES:
        print(f"РЕЗУЛЬТАТ: провалено {len(FAILURES)}")
        return 1
    print("РЕЗУЛЬТАТ: все проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
