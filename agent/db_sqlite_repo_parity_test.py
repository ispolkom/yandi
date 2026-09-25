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
        return int(v) if v == v.to_integral_value() else float(v)
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
    from agent.db.sql import field_protection as fp
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
        fp.clear_key()
        fp.forget_mode()
        yandi_db.fp_clear_key()
        yandi_db.fp_forget_mode()
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
                rc = str(r.get("error", "")).split(":")[0]
                known = {"ValueError", "AssertionError", "StorageLocked", "StorageTampered", "StorageProtectionError", "KeyError"}
                ok = ("error" in p) == ("error" in r) and (p.get("error") not in known or p.get("error") == rc)
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            check(f"{label} · вызов {i} {func}", ok, f"\n args={json.dumps(kwargs, ensure_ascii=False)[:300]}\n py={json.dumps(p, ensure_ascii=False)[:2500]}\n rs={json.dumps(r, ensure_ascii=False)[:2500]}")
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

    bel = [
        ("upsert_belief", dict(belief_id="b1", topic="космос", statement="Планет восемь", confidence=0.83, evidence_for=["e1", "e2"], evidence_against=[], claim_ids=["c1"], created_at=T0, updated_at=T0)),
        ("upsert_belief", dict(belief_id="b2", topic="космос", statement="Плутон — планета", confidence=0.3, status="revised", contradiction_score=0.6, prior=0.2, likelihood=0.7, decay_factor=0.9, created_at=T1, updated_at=T1)),
        ("upsert_belief", dict(belief_id="b3", topic="история", statement="Рим пал", confidence=0.95, status="superseded", superseded_by="b4", created_at=T2, updated_at=T2)),
        ("upsert_belief", dict(belief_id="b1", topic="ДРУГАЯ", statement="Планет восемь (уточнено)", confidence=0.91, status="active", evidence_for=["e1", "e3"], claim_ids=None, contradiction_score=0.5, created_at=T2, updated_at=T2)),
        ("get_belief", dict(belief_id="b1")),
        ("get_belief", dict(belief_id="nope")),
        ("list_beliefs_by_topic", dict(topic="космос")),
        ("list_beliefs_by_topic", dict(topic="космос", statuses=["active"])),
        ("list_beliefs_by_topic", dict(topic="история", statuses=["superseded", "rejected"])),
        ("list_beliefs_by_topic", dict(topic="история")),
        ("list_all_beliefs", dict()),
        ("list_active_beliefs", dict()),
        ("list_contradictory_beliefs", dict()),
        ("list_contradictory_beliefs", dict(min_score=0.55)),
        ("list_contradictory_beliefs", dict(min_score=0.0)),
        ("get_belief_stats", dict()),
        ("record_belief_assessment", dict(belief_id="b1", change_type="challenge", old_confidence=0.83, new_confidence=0.91, reason="ы" * 300, run_id="r1", created_at=T0)),
        ("record_belief_assessment", dict(belief_id="b1", change_type="decay", created_at=T1)),
        ("record_belief_assessment", dict(belief_id="b1", change_type="decay", reason="", created_at=T1)),
        ("list_belief_history", dict(belief_id="b1")),
        ("list_belief_history", dict(belief_id="zz")),
        ("record_recheck_event", dict(family_id="f1", outcome="confirmed", run_id="r1", trigger_reason="age", started_at=T0, reason="р")),
        ("record_recheck_event", dict(family_id="f1", outcome="changed", started_at=T1)),
    ]
    scenario("E1 убеждения, история, перепроверки", bel)
    scenario("E2 статистика убеждений на пустой таблице", [("get_belief_stats", dict()), ("list_all_beliefs", dict()), ("list_active_beliefs", dict())])
    views = ev_calls[:-3] + [
        ("record_answer_version", dict(question_id=1, answer_text="v1", run_id="r1", created_at=T0)),
        ("record_answer_version", dict(question_id=1, answer_text="v2", run_id="r2", created_at=T1)),
        ("record_answer_assessment", dict(answer_id=1, run_id="r1", canonical_trust="LOW", created_at=T0)),
        ("record_answer_assessment", dict(answer_id=2, run_id="r2", canonical_trust="MID", created_at=T1)),
        ("record_answer_assessment", dict(answer_id=2, run_id="r3", canonical_trust="HIGH", diverged=True, created_at=T2)),
        ("get_or_create_claim_family", dict(family_id="fx", domain="d", canonical_text="t", created_at=T0)),
        ("link_family_member", dict(family_id="fx", claim_id="c1", linked_at=T0)),
        ("get_current_answer", dict(question_id=1)),
        ("get_current_answer", dict(question_id=99)),
        ("get_answer_history", dict(question_id=1)),
        ("get_answer_history", dict(question_id=99)),
        ("explain_answer", dict(answer_id=1)),
        ("explain_answer", dict(answer_id=2)),
        ("explain_answer", dict(answer_id=99)),
        ("get_verification_runs", dict(question_id=1)),
        ("get_sources_for_run", dict(run_id="r1")),
        ("get_sources_for_run", dict(run_id="r2")),
        ("get_claim_history", dict(family_id="fx")),
        ("get_route_history", dict(resource_id=1)),
        ("get_last_checked", dict(question_id=1)),
        ("get_last_checked", dict(question_id=99)),
        ("compare_runs", dict(run_id_a="r1", run_id_b="r2")),
        ("compare_runs", dict(run_id_a="r2", run_id_b="r1")),
        ("compare_runs", dict(run_id_a="r1", run_id_b="r3")),
    ]
    scenario("F1 API чтения локальной памяти", views)

    grv = [
        ("record_grievance", dict(grievance_id="g1", user_id="u1", event_type="insult", description="Ты сказал мне что-то обидное вчера вечером", severity=0.6, context={"turn": "t1", "текст": "привет 🌍"}, created_at=T0)),
        ("record_grievance", dict(grievance_id="g2", user_id="u1", event_type="rude", description="yp1:похоже на запечатанное", severity=0.3, created_at=T1)),
        ("record_grievance", dict(grievance_id="g3", user_id="u1", event_type="rude", description="yp0:ещё хитрее", severity=0.2, context=None, created_at=T2)),
        ("record_grievance", dict(grievance_id="g4", user_id="u2", event_type="x", description="Другой человек", severity=0.9, context=[1, 2], created_at=T0)),
        ("get_grievance", dict(grievance_id="g1")),
        ("get_grievance", dict(grievance_id="g2")),
        ("get_grievance", dict(grievance_id="g3")),
        ("get_grievance", dict(grievance_id="zz")),
        ("find_similar_open_grievance", dict(user_id="u1", description="Ты сказал мне что-то обидное ЕЩЁ РАЗ")),
        ("find_similar_open_grievance", dict(user_id="u1", description="совсем другое")),
        ("find_similar_open_grievance", dict(user_id="u3", description="Ты сказал мне что-то обидное")),
        ("update_grievance_status", dict(grievance_id="g1", status="healing", apology_sincerity=0.8, apology_at=T1, timestamp=T1)),
        ("update_grievance_status", dict(grievance_id="g1", status="understood", understood_at=T2, timestamp=T2)),
        ("get_grievance", dict(grievance_id="g1")),
        ("bump_grievance", dict(grievance_id="g1", new_severity=0.75, timestamp=T2)),
        ("get_grievance", dict(grievance_id="g1")),
        ("update_grievance_status", dict(grievance_id="g2", status="forgiven", forgiven_at=T2, timestamp=T2)),
        ("update_grievance_status", dict(grievance_id="g3", status="unforgiven", timestamp="2026-03-01 10:02:00")),
        ("find_similar_open_grievance", dict(user_id="u1", description="yp1:похоже на запечатанное")),
        ("list_active_grievances", dict(user_id="u1")),
        ("list_active_grievances", dict(user_id="u2")),
        ("list_recent_resolved_grievances", dict(user_id="u1")),
        ("list_recent_resolved_grievances", dict(user_id="u1", limit=1)),
        ("count_grievances_by_status", dict(user_id="u1", status="registered")),
        ("count_grievances_by_status", dict(user_id="u1", status="forgiven")),
        ("count_grievances_by_status", dict(user_id="nobody", status="forgiven")),
        ("get_forgiveness_capacity", dict(user_id="u1")),
        ("set_forgiveness_capacity", dict(user_id="u1", capacity=42.5, last_forgiveness=T1, timestamp=T1)),
        ("set_forgiveness_capacity", dict(user_id="u1", capacity=44.0, last_forgiveness=None, timestamp=T2)),
        ("set_forgiveness_capacity", dict(user_id="u2", capacity=10.0, timestamp=T2)),
        ("get_forgiveness_capacity", dict(user_id="u1")),
        ("get_forgiveness_capacity", dict(user_id="u2")),
    ]
    scenario("G1 обиды и ёмкость прощения (защита выключена)", grv)
    pers = [
        ("get_personality", dict()),
        ("get_or_create_personality", dict(name="Янди", version="1", traits=["добрая"], goals=["помогать"], principles=["не лгать"], limitations=[], preferences={"стиль": "тёплый", "n": 1}, created_at=T0)),
        ("get_or_create_personality", dict(name="Другая", version="2", traits=[], goals=[], principles=[], limitations=[], preferences={}, created_at=T2)),
        ("update_personality_lists", dict(traits=["добрая", "честная"], goals=None, updated_at=T1)),
        ("update_personality_lists", dict(updated_at=T2)),
        ("update_personality_lists", dict(principles=[], limitations=["x"], updated_at=T2)),
        ("increment_personality_counter", dict(counter="total_cycles", updated_at=T1)),
        ("increment_personality_counter", dict(counter="total_cycles", updated_at=T2)),
        ("increment_personality_counter", dict(counter="total_decisions", updated_at=T2)),
        ("increment_personality_counter", dict(counter="evil'; DROP TABLE personality; --", updated_at=T2)),
        ("get_personality", dict()),
        ("record_personality_change", dict(what_changed="traits", reason="опыт", created_at=T1)),
        ("record_personality_change", dict(what_changed="goals", reason=None, created_at=T2)),
        ("count_personality_changes", dict()),
    ]
    scenario("G2 личность", pers)

    TS = [f"2026-03-01 10:0{i}:00" for i in range(8)]
    epi = [
        ("record_episode", dict(episode_id="e1", event_type="chat", summary="первый", details={"k": [1, 2]}, importance=0.9, tags=["a", "б"], related_episodes=[], created_at=TS[0])),
        ("record_episode", dict(episode_id="e2", event_type="chat", summary="второй", importance=0.3, tags=["a"], created_at=TS[1])),
        ("record_episode", dict(episode_id="e3", event_type="error", summary="третий", details=None, tags=None, created_at=TS[2])),
        ("record_episode", dict(episode_id="e4", event_type="chat", summary="4", importance=0.71234, tags=["🌍"], created_at=TS[3])),
        ("get_episodes_by_type", dict(event_type="chat")),
        ("get_episodes_by_type", dict(event_type="chat", limit=2)),
        ("get_episodes_by_tag", dict(tag="a")),
        ("get_episodes_by_tag", dict(tag="🌍")),
        ("get_episodes_by_tag", dict(tag="нет")),
        ("get_episodes_by_importance", dict()),
        ("get_episodes_by_importance", dict(min_importance=0.0, limit=2)),
        ("get_recent_episodes", dict()),
        ("get_recent_episodes", dict(limit=1)),
        ("get_episode_stats", dict()),
    ]
    scenario("I1 эпизоды", epi)
    scenario("I1b статистика эпизодов на пустой таблице", [("get_episode_stats", dict()), ("get_recent_episodes", dict())])
    slf = [
        ("get_self_state", dict()),
        ("get_or_create_self_state", dict(identity="я", version="1", capabilities=["a"], limitations=[], current_uncertainties=["u"], metadata={"m": 1}, created_at=TS[0])),
        ("get_or_create_self_state", dict(identity="другой", version="2", capabilities=[], limitations=[], current_uncertainties=[], metadata={}, created_at=TS[5])),
        ("update_self_state_lists", dict(capabilities=["a", "b"], metadata={"x": None}, updated_at=TS[1])),
        ("update_self_state_lists", dict(updated_at=TS[2])),
        ("increment_self_state_counter", dict(counter="total_queries", updated_at=TS[3])),
        ("increment_self_state_counter", dict(counter="total_queries", updated_at=TS[4])),
        ("increment_self_state_counter", dict(counter="nope", updated_at=TS[4])),
        ("get_self_state", dict()),
        ("record_self_event", dict(event_id="s1", event_type="decision", description="d1", details={"a": 1}, importance=0.6, created_at=TS[0])),
        ("record_self_event", dict(event_id="s2", event_type="lesson", description="d2", created_at=TS[1])),
        ("record_self_event", dict(event_id="s3", event_type="decision", description="d3", created_at=TS[2])),
        ("get_self_events_by_type", dict(event_type="decision")),
        ("get_self_events_by_type", dict(event_type="decision", limit=1)),
        ("get_recent_self_events", dict()),
        ("count_self_events", dict()),
        ("create_reflection_policy", dict(policy_id="p1", policy_type="tone", rule="Быть добрее", confidence=0.61, created_at=TS[0])),
        ("create_reflection_policy", dict(policy_id="p2", policy_type="tone", rule="Не спорить", confidence=0.4, created_at=TS[1])),
        ("find_reflection_policy_by_rule", dict(rule="Быть добрее")),
        ("find_reflection_policy_by_rule", dict(rule="нет такого")),
        ("bump_reflection_policy_observed", dict(policy_id="p1", activate=False)),
        ("bump_reflection_policy_observed", dict(policy_id="p1", activate=True, activated_at=TS[6])),
        ("bump_reflection_policy_observed", dict(policy_id="p2", activate=False)),
        ("list_all_reflection_policies", dict()),
    ]
    scenario("I2 «я», события себя, политики", slf)
    edges = [
        ("get_or_create_claim_family", dict(family_id="fa", domain="d", canonical_text="A", created_at=TS[0])),
        ("upsert_semantic_edge", dict(edge_id="ed1", family_a="fa", family_b="fb", edge_type="depends_on", reason="потому", triggering_claim_ids=["c1", "c2", "", None], created_at=TS[1])),
        ("upsert_semantic_edge", dict(edge_id="ed9", family_a="fa", family_b="fb", edge_type="depends_on", reason="иначе", triggering_claim_ids=["c2", "c3"], created_at=TS[2])),
        ("upsert_semantic_edge", dict(edge_id="ed2", family_a="fc", family_b="fb", edge_type="contradicts", reason="r", created_at=TS[3])),
        ("upsert_semantic_edge", dict(edge_id="ed3", family_a="fc", family_b="fb", edge_type="depends_on", reason="r", triggering_claim_ids=None, created_at=TS[3])),
        ("find_semantic_edge", dict(family_a="fa", family_b="fb", edge_type="depends_on")),
        ("find_semantic_edge", dict(family_a="fa", family_b="fb", edge_type="contradicts")),
        ("list_dependents", dict(family_id="fb")),
        ("list_contradicts_edges", dict()),
        ("get_family_status", dict(family_id="fz")),
        ("upsert_family_status", dict(family_id="fz", last_status="supported", updated_at=TS[1])),
        ("upsert_family_status", dict(family_id="fz", last_status="refuted", updated_at=TS[2])),
        ("get_family_status", dict(family_id="fz")),
        ("resolve_question", dict(raw_text="q", anonymized_text=None, asked_at=TS[0])),
        ("start_run", dict(run_id="r1", occurrence_id=1, started_at=TS[0])),
        ("record_recheck_event", dict(family_id="fz", outcome="a", run_id="r1", started_at=TS[1])),
        ("record_recheck_event", dict(family_id="fz", outcome="b", started_at=TS[4])),
        ("record_recheck_event", dict(family_id="fz", outcome="c", started_at=TS[2])),
        ("get_last_recheck", dict(family_id="fz")),
        ("get_last_recheck", dict(family_id="none")),
    ]
    scenario("I3 семантические рёбра и статус семейств", edges)
    know = [
        ("upsert_knowledge_record", dict(record_id="k1", question="Q1", answer="A1", trust_level="LOW", verdict=None, topic="космос", tags=["t"], sources=["s1"], meta={"m": 1}, created_at=TS[0], updated_at=TS[0])),
        ("upsert_knowledge_record", dict(record_id="k2", question="Q2", answer="A2", trust_level="HIGH", created_at=TS[1], updated_at=TS[1])),
        ("upsert_knowledge_record", dict(record_id="k1", question="Q1b", answer="A1b", trust_level="MID", verdict="ok", topic="иное", tags=None, created_at=TS[5], updated_at=TS[5])),
        ("get_knowledge_record", dict(record_id="k1")),
        ("get_knowledge_record", dict(record_id="zz")),
        ("update_knowledge_record_trust", dict(record_id="k2", trust_level="LOW", verdict="disputed", updated_at=TS[6])),
        ("update_knowledge_record_trust", dict(record_id="k2", trust_level="MID", verdict="", updated_at=TS[7])),
        ("update_knowledge_record_trust", dict(record_id="nope", trust_level="MID", updated_at=TS[7])),
        ("list_knowledge_by_trust", dict(trust_level="MID")),
        ("list_knowledge_by_trust", dict(trust_level="MID", limit=1)),
        ("get_knowledge_stats", dict()),
        ("get_peer_config", dict()),
        ("get_or_create_peer_config", dict(updated_at=TS[0])),
        ("get_or_create_peer_config", dict(updated_at=TS[3])),
        ("get_biography", dict(user_id="u1")),
        ("get_or_create_biography", dict(user_id="u1", birth=TS[0])),
        ("get_or_create_biography", dict(user_id="u1", birth=TS[4])),
        ("bump_biography_counter", dict(user_id="u1", counter="cycles", amount=3, updated_at=TS[1])),
        ("bump_biography_counter", dict(user_id="u1", counter="saved_memories", updated_at=TS[2])),
        ("bump_biography_counter", dict(user_id="u1", counter="bogus", updated_at=TS[2])),
        ("set_biography_principles_change", dict(user_id="u1", old="старый", new="новый", updated_at=1767225600)),
        ("set_biography_principles_change", dict(user_id="u1", old="a", new="b", updated_at=1767225600.5)),
        ("set_biography_principles_change", dict(user_id="u1", old="a", new="b", updated_at=TS[3])),
        ("get_biography", dict(user_id="u1")),
        ("record_biography_event", dict(user_id="u1", event_type="milestone", payload={"a": "б", "n": [1]}, created_at=TS[0])),
        ("record_biography_event", dict(user_id="u1", event_type="milestone", payload={}, created_at=TS[2])),
        ("record_biography_event", dict(user_id="u1", event_type="other", payload={"z": 1}, created_at=TS[1])),
        ("list_biography_events", dict(user_id="u1", event_type="milestone")),
        ("list_biography_events", dict(user_id="u1", event_type="milestone", limit=1)),
        ("count_biography_events", dict(user_id="u1", event_type="milestone")),
        ("count_biography_events", dict(user_id="u2", event_type="milestone")),
    ]
    scenario("I4 знания, пиры, биография", know)
    ctx = [
        ("get_context_topic", dict(user_id="u1", topic="t")),
        ("touch_context_topic", dict(user_id="u1", topic="t", activity_at=TS[2])),
        ("touch_context_topic", dict(user_id="u1", topic="t", activity_at=TS[1])),
        ("touch_context_topic", dict(user_id="u1", topic="t", activity_at=TS[5])),
        ("touch_context_topic", dict(user_id="u1", topic="других", activity_at=TS[0])),
        ("get_context_topic", dict(user_id="u1", topic="t")),
        ("list_context_topics", dict(user_id="u1")),
        ("record_context_instance", dict(user_id="u1", topic="t", query="q1", response="r1", type_="fact", source="chat", created_at=TS[0])),
        ("record_context_instance", dict(user_id="u1", topic="t", query="q2", response="r2", created_at=TS[1])),
        ("record_context_instance", dict(user_id="u1", topic="t", query="q3", response="r3", created_at=TS[2])),
        ("list_recent_context_instances", dict(user_id="u1", topic="t")),
        ("list_recent_context_instances", dict(user_id="u1", topic="t", limit=2)),
        ("create_decision_journal_entry", dict(decision_id="d1", user_id="u1", event_type="e", event_text="text", context={"c": 1}, analysis={"a": []}, alternatives=[{"x": 1}], decision="ok", confidence=0.83, created_at=TS[0])),
        ("create_decision_journal_entry", dict(decision_id="d2", user_id="u1", event_type="e", event_text="text2", context={}, analysis={}, alternatives=[], decision="no", created_at=TS[1])),
        ("get_decision_journal_entry", dict(decision_id="d1")),
        ("get_decision_journal_entry", dict(decision_id="zz")),
        ("update_decision_journal_outcome", dict(decision_id="d1", outcome={"good": True}, updated_at=TS[3])),
        ("update_decision_journal_outcome", dict(decision_id="zz", outcome={}, updated_at=TS[3])),
        ("update_decision_journal_self_correction", dict(decision_id="d2", self_correction={"fix": "б"}, updated_at=TS[4])),
        ("list_decision_journal_entries", dict(user_id="u1")),
        ("list_decision_journal_entries", dict(user_id="u1", limit=1)),
        ("create_experience", dict(experience_id="x1", user_id="u1", speech_act="ask", topic="t", query="q", response="r", context={"a": 1}, created_at=TS[0])),
        ("create_experience", dict(experience_id="x2", user_id="u1", speech_act="tell", topic="t", query="q", response="r", created_at=TS[1])),
        ("increment_experience_used", dict(experience_id="x1")),
        ("increment_experience_used", dict(experience_id="x1")),
        ("update_experience_success", dict(experience_id="x2", user_reaction="thanks", success=0.85)),
        ("list_experiences", dict(user_id="u1")),
        ("create_secret_archive_question", dict(question_id="a1", user_id="u1", query="q", reason="r", context={"k": "v"}, created_at=TS[0])),
        ("create_secret_archive_question", dict(question_id="a2", user_id="u1", query="q2", reason="r", created_at=TS[1])),
        ("answer_secret_archive_question", dict(question_id="a1", answer="ответ", answer_time=TS[2])),
        ("answer_secret_archive_question", dict(question_id="zz", answer="x", answer_time=TS[2])),
        ("list_secret_archive_questions", dict(user_id="u1")),
        ("list_secret_archive_questions", dict(user_id="u1", answered=True)),
        ("list_secret_archive_questions", dict(user_id="u1", answered=False)),
    ]
    scenario("I5 контекст, журнал решений, опыт, секретный архив", ctx)
    inner = [
        ("get_inner_state", dict(user_id="u1")),
        ("get_or_create_inner_state", dict(user_id="u1", updated_at=TS[0])),
        ("get_or_create_inner_state", dict(user_id="u1", updated_at=TS[5])),
        ("update_inner_state", dict(user_id="u1", updated_at=TS[1], mood="grumpy", energy=0.42, trust=0.123456789, pattern="p", current_tone=None)),
        ("update_inner_state", dict(user_id="u1", updated_at=TS[2])),
        ("update_inner_state", dict(user_id="u1", updated_at=TS[2], bogus=1)),
        ("get_inner_state", dict(user_id="u1")),
        ("record_inner_state_event", dict(user_id="u1", event_type="insult", description="d1", sincerity=0.7, weight=0.25, resolved=False, created_at=TS[0])),
        ("record_inner_state_event", dict(user_id="u1", event_type="apology", description="d2", created_at=TS[1])),
        ("record_inner_state_event", dict(user_id="u1", event_type="x", description="d3", resolved=True, created_at=TS[2])),
        ("list_inner_state_events", dict(user_id="u1")),
        ("list_inner_state_events", dict(user_id="u1", limit=2)),
        ("list_inner_state_events_in_order", dict(user_id="u1")),
    ]
    scenario("I6 внутреннее состояние", inner)

    # ---- защита полей ВКЛЮЧЕНА: значения запечатаны, запись без ключа отвергается, чужой ключ — подделка ----
    core_key = bytes(range(1, 33))
    def enable_protection(mode, key=core_key, proof_key_from=core_key):
        fp.install_key(proof_key_from)
        m, nonce, proof = fp.new_mode_record(mode)
        fp.clear_key()
        cur.execute("INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES (%s,%s,%s,%s)", (m, nonce, proof, T0))
        yandi_db.exec_sql(state["h"], "INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES (?,?,?,?)", json.dumps([m, nonce, {"hex": proof.hex()}, T0]))
        fp.forget_mode(); yandi_db.fp_forget_mode()

    def with_key(k):
        fp.install_key(k)
        yandi_db.fp_install_key(k.hex())

    def scenario_protected(label, mode, calls_before_key, calls_with_key, key=core_key):
        nonlocal n
        reset()
        n += 1
        enable_protection(mode)
        seq = [(f, kw, False) for f, kw in calls_before_key] + [(f, kw, True) for f, kw in calls_with_key]
        keyed = False
        for i, (func, kwargs, need_key) in enumerate(seq):
            if need_key and not keyed:
                with_key(key)
                keyed = True
            p, r = py_call(func, kwargs), rs_call(func, kwargs)
            if "error" in p or "error" in r:
                rc = str(r.get("error", "")).split(":")[0]
                ok = ("error" in p) == ("error" in r) and p.get("error") == rc
            else:
                ok = canon(p["ok"]) == canon(r["ok"])
            check(f"{label} · вызов {i} {func}", ok, f"\n args={json.dumps(kwargs, ensure_ascii=False)[:200]}\n py={json.dumps(p, ensure_ascii=False)[:500]}\n rs={json.dumps(r, ensure_ascii=False)[:500]}")
        # перекрёстная проверка: то, что запечатал Rust, открывает Python и наоборот (значения на диске у каждой стороны свои, шум nonce)
        for t, col, keyc in (("grievance", "description", "id"),):
            cur.execute(f"SELECT {keyc} AS k, {col} AS v FROM {t}")
            pv = {r["k"]: r["v"] for r in cur.fetchall()}
            rv = {r["k"]: r["v"] for r in json.loads(yandi_db.query(state["h"], f"SELECT {keyc} AS k, {col} AS v FROM {t}"))}
            for k, v in rv.items():
                if isinstance(v, str) and v.startswith("yp1:") and k in pv:
                    try:
                        opened = fp.open_with(fp.storage_key(), t, col, {keyc: k}, v)
                        expected = fp.open_with(fp.storage_key(), t, col, {keyc: k}, pv[k])
                        check(f"{label} · перекрёстное открытие {t}.{col}[{k}]", opened == expected, f"{opened!r} != {expected!r}")
                    except Exception as e:  # noqa: BLE001
                        check(f"{label} · перекрёстное открытие {t}.{col}[{k}]", False, repr(e))
        fp.clear_key(); yandi_db.fp_clear_key()

    sealed_calls = [
        ("record_grievance", dict(grievance_id="g1", user_id="u1", event_type="insult", description="Секретное слово человека", severity=0.5, context={"a": "б"}, created_at=T0)),
        ("record_grievance", dict(grievance_id="g2", user_id="u1", event_type="insult", description="yp1:подделка-под-запечатанное", severity=0.5, created_at=T1)),
        ("get_grievance", dict(grievance_id="g1")),
        ("get_grievance", dict(grievance_id="g2")),
        ("find_similar_open_grievance", dict(user_id="u1", description="Секретное слово человека!")),
        ("list_active_grievances", dict(user_id="u1")),
        ("update_grievance_status", dict(grievance_id="g1", status="forgiven", forgiven_at=T2, timestamp=T2)),
        ("list_recent_resolved_grievances", dict(user_id="u1")),
    ]
    scenario_protected("H1 защита ВКЛЮЧЕНА, ключ есть", "on", [], sealed_calls)
    scenario_protected("H2 защита ВКЛЮЧЕНА, ключа нет: запись отвергается", "on", [
        ("record_grievance", dict(grievance_id="g1", user_id="u1", event_type="insult", description="слова", severity=0.5, created_at=T0)),
        ("get_grievance", dict(grievance_id="g1")),
        ("list_active_grievances", dict(user_id="u1")),
    ], [])
    scenario_protected("H3 миграция: запись запечатана, чтение принимает оба вида", "migrating", [], sealed_calls[:4])
    # открытое значение при включённой защите = подделка мимо приложения; запечатанное без ключа = заперто
    nonlocal_n = n
    reset()
    n += 1
    enable_protection("on")
    for stmt, params in (("INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,'registered',%s,%s)", ("gx", "u1", "e", "открытый текст мимо защиты", 0.1, T0, T0)),):
        cur.execute(stmt, params)
        yandi_db.exec_sql(state["h"], stmt.replace("%s", "?"), json.dumps(list(params)))
    with_key(core_key)
    for i, (func, kwargs) in enumerate([("get_grievance", dict(grievance_id="gx")), ("list_active_grievances", dict(user_id="u1"))]):
        p, r = py_call(func, kwargs), rs_call(func, kwargs)
        rc = str(r.get("error", "")).split(":")[0]
        check(f"H4 подделка мимо защиты · {func}", p.get("error") == "StorageTampered" and rc == "StorageTampered", f"py={p} rs={r}")
    fp.clear_key(); yandi_db.fp_clear_key()
    # неверный ключ и неверная запись режима
    reset()
    n += 1
    enable_protection("on")
    with_key(core_key)
    for func, kwargs in (("record_grievance", dict(grievance_id="g1", user_id="u1", event_type="e", description="слова", severity=0.1, created_at=T0)),):
        py_call(func, kwargs), rs_call(func, kwargs)
    fp.clear_key(); yandi_db.fp_clear_key()
    with_key(bytes(range(50, 82)))  # другой ключ: запись режима не проверяется
    for func, kwargs in (("get_grievance", dict(grievance_id="g1")),):
        p, r = py_call(func, kwargs), rs_call(func, kwargs)
        rc = str(r.get("error", "")).split(":")[0]
        check(f"H5 чужой ключ · {func}", p.get("error") == "StorageTampered" and rc == "StorageTampered", f"py={p} rs={r}")
    fp.clear_key(); yandi_db.fp_clear_key()
    with_key(core_key)
    cur.execute("INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES ('on', 'ab', UNHEX(REPEAT('00',32)), %s)", (T2,)); yandi_db.exec_sql(state["h"], "INSERT INTO storage_protection_event (mode, nonce, proof, created_at) VALUES ('on', 'ab', ?, ?)", json.dumps([{"hex": "00" * 32}, T2]))
    fp.forget_mode(); yandi_db.fp_forget_mode()
    ins = "INSERT INTO grievance (id, user_id, event_type, description, severity, status, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,'registered',%s,%s)"
    plain = ("gy", "u1", "e", "открытый текст", 0.1, T0, T0)
    cur.execute(ins, plain); yandi_db.exec_sql(state["h"], ins.replace("%s", "?"), json.dumps(list(plain)))
    for func, kwargs in (("get_grievance", dict(grievance_id="gy")),):
        p, r = py_call(func, kwargs), rs_call(func, kwargs)
        rc = str(r.get("error", "")).split(":")[0]
        check(f"H6 подделанная запись режима · {func}", p.get("error") == "StorageTampered" and rc == "StorageTampered", f"py={p} rs={r}")
    fp.clear_key(); yandi_db.fp_clear_key()

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
