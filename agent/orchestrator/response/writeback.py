"""
Optimistic respond ([10]) — extracted from agent/orchestrator_v2.py's
process(): background-validation kickoff, cache write, query archive
write, V3 memory/reflection/dataset write-back, trust banner selection,
and the final OrchestratorResponse return. This is the tail end of
process() itself — after this extraction, process() becomes a thin
sequence of calls into pre_pipeline/pipeline/synthesis/claims*/epistemic*
/this module.

Structural extraction only: no side-effect ordering, thresholds, or log
markers changed. `_background_validate` and `_build_tags`/`_DOMAIN_TAG`
move here too — each had exactly one call site, all inside this block.

Free-variable / locals()-check audit (done by direct inspection, this
block was small enough not to need a dedicated fork): most of the
original `'X' in locals()` guards here are provably always-true given
what already flows into this function (e.g. `claims_data`/`evidence_data`
always have a real value — `[]` at worst, from pre_pipeline.py's
defaults — so they're passed as plain required parameters, not Optional).
Three are NOT always-true and are kept as genuine optional parameters
with `None` defaults, because their source call sites in orchestrator_v2.py
are themselves conditionally executed:

- `supporting_ids`, `technical_errors`: both come from calls inside the
  `if synthesis_timed_out or synthesis_result is None: ... else: <call>`
  branch in orchestrator_v2.py — on a synthesis timeout, neither is ever
  assigned. `claims_data`/`evidence_data` differ from these because they
  have an unconditional `[]` default set earlier, in pre_pipeline.py,
  that survives the timeout branch; `supporting_ids`/`technical_errors`
  have no such earlier default anywhere in the codebase.
- `claims_accepted`/`claims_rejected`/`total_claims`: come from
  `evaluate_claim_status_gate()`, itself guarded by
  `if not skip_rag and not is_subjective_answer and synthesis_result:` in
  orchestrator_v2.py (kept there deliberately — see that function's own
  docstring for why the guard couldn't move with it).

`reflection_result` needed no such treatment: it's assigned earlier in
this SAME function (inside the same non-branching `try:`), so by the time
anything reads it via `'reflection_result' in locals()`, it is always a
real local of this function too — the check is preserved completely
unchanged (still testing this function's own scope, exactly as before).
"""

import threading
import time
from datetime import datetime

from agent.orch_optimistic import get_responder
from agent.orch_node_selector import select_nodes, select_nodes_federated, _should_use_federation
from agent.orch_validator import validate_parallel
from agent.orch_arbiter import arbitrate
from agent.orch_knowledge_writer import write_from_arbiter
from agent.orch_monitoring import record as mon_record
from agent.orch_reputation import add_decision_event
from agent.orch_query_archive import record_query as archive_query
from agent.orch_schemas import OrchestratorResponse, OutcomeRecord
from agent.experience_memory import get_experience_memory
from agent.dataset_builder import get_dataset_builder
from agent.orchestrator.epistemic.trust_gate import _calculate_delta_factors
from agent.orchestrator.epistemic.canonical_trust import compute_canonical_trust
from agent.orchestrator.runtime.profiling import report_pipeline_profile
from agent.db.sql.shadow_write import shadow_complete_run

_DOMAIN_TAG: dict[str, str] = {
    "general": "general",
    "legal": "legal",
    "medical": "health:medical",
    "financial": "finance",
    "coding": "tech:coding",
    "science": "science",
    "tech": "tech",
    "ai_ml": "tech:ai",
    "cooking": "lifestyle:cooking",
    "travel": "travel",
    "sport": "lifestyle:sport",
    "music": "culture:music",
    "history": "culture:history",
    "education": "education",
    "ecology": "science:ecology",
    "psychology": "health:psychology",
    "geography": "science:geography",
    "literature": "culture:literature",
}


def _build_tags(intent_result, enrich_result, query: str = "") -> list[str]:
    domain = getattr(intent_result, "intent", "general") or "general"
    base = _DOMAIN_TAG.get(domain, domain)
    if base == "general" and query:
        q_lower = query.lower()
        keywords = [
            ("рецепт", "lifestyle:cooking"),
            ("путешест", "travel:tourism"),
            ("гора", "science:geography"),
            ("закон", "legal"),
            ("симптом", "health:medical"),
            ("акция", "finance"),
            ("код", "tech:coding"),
            ("нейросет", "tech:ai"),
        ]
        for kw, tag in keywords:
            if kw in q_lower:
                base = tag
                break
    tags = [base]
    return tags[:3]


def _background_validate(
    question: str,
    answer: str,
    synthesis,
    risk,
    intent_result,
    validation_id: str,
    decision_id: str,
    trace_id: str,
    domain: str,
    search_result,
    web_used: bool,
    verbose: bool,
):
    def log(msg):
        if verbose:
            print(msg, flush=True)

    log(f"\n[BG:{validation_id[:6]}] Старт валидации...")

    try:
        add_decision_event(
            event_type="ValidationStarted",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict="VERIFYING",
            reason=f"ValidationStarted: {validation_id}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        nodes = select_nodes_federated(risk, domain=domain) if _should_use_federation() else select_nodes(risk, domain=domain)
        log(f"[BG] Ноды: {[n.node_id for n in nodes.nodes]}")

        val_result = validate_parallel(question, answer, nodes, domain=domain)
        log(f"[BG] agree={val_result.agree_count} disagree={val_result.disagree_count}")

        use_llm = risk.risk_level in ("medium", "high", "critical")
        arb = arbitrate(question, answer, val_result, use_llm=use_llm)

        verdict = arb.verdict
        log(f"[BG] Вердикт: {verdict} — {arb.explanation}")

        add_decision_event(
            event_type="ValidationFinished",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            reason=f"ValidationFinished: verdict={verdict}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        if verdict in ("VERIFIED", "PARTIALLY_VERIFIED"):
            write_from_arbiter(question, synthesis, arb, topic=domain)
            log(f"[BG] Записано в knowledge registry ({verdict})")
            add_decision_event(
                event_type="KnowledgeStored",
                trace_id=trace_id,
                entity_type="knowledge",
                entity_id="registry",
                verdict=verdict,
                reason=f"KnowledgeStored: {verdict}",
                domain=domain,
                meta={"decision_id": decision_id}
            )

        delta_factors = _calculate_delta_factors(
            verification_verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            has_sources=bool(synthesis.sources if synthesis else []),
            consensus_agreement=val_result.agree_count,
            total_nodes=len(nodes.nodes) if nodes else 0,
        )

        add_decision_event(
            event_type="ReputationUpdated",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            delta=delta_factors["total"],
            delta_factors=delta_factors,
            reason=f"ReputationUpdated: verdict={verdict}",
            domain=domain,
            meta={
                "decision_id": decision_id,
                "agree_count": val_result.agree_count,
                "disagree_count": val_result.disagree_count,
            }
        )

        add_decision_event(
            event_type="ReputationUpdated",
            trace_id=trace_id,
            entity_type="model",
            entity_id="heretic:q8",
            verdict=verdict,
            confidence=synthesis.confidence if synthesis else 0.5,
            delta=delta_factors["total"],
            reason=f"ReputationUpdated: verdict={verdict}",
            domain=domain,
            meta={"decision_id": decision_id}
        )

        if search_result and search_result.docs:
            add_decision_event(
                event_type="ReputationUpdated",
                trace_id=trace_id,
                entity_type="source",
                entity_id="local_registry",
                verdict=verdict,
                confidence=synthesis.confidence if synthesis else 0.5,
                delta=delta_factors["total"],
                reason=f"ReputationUpdated: docs={len(search_result.docs)}",
                domain=domain,
                meta={"decision_id": decision_id, "docs_count": len(search_result.docs)}
            )

        if web_used:
            add_decision_event(
                event_type="ReputationUpdated",
                trace_id=trace_id,
                entity_type="source",
                entity_id="web_search",
                verdict=verdict,
                confidence=synthesis.confidence if synthesis else 0.5,
                delta=delta_factors["total"],
                reason=f"ReputationUpdated: web_used=True",
                domain=domain,
                meta={"decision_id": decision_id, "web_used": True}
            )

        get_responder().on_validation_done(validation_id, verdict, arb.explanation)

        for v in val_result.validations:
            mon_record("validate", v.latency, v.verdict != "disagree")

        log(f"[BG] Репутация обновлена: {delta_factors['total']:+.3f}")

    except Exception as e:
        log(f"[BG] Ошибка валидации: {e}")
        add_decision_event(
            event_type="ValidationFailed",
            trace_id=trace_id,
            entity_type="route",
            entity_id="registry_first",
            verdict="REJECTED",
            reason=f"ValidationFailed: {str(e)[:100]}",
            domain=domain,
            meta={"decision_id": decision_id}
        )


def run_optimistic_respond(
    request,
    verbose,
    enable_validation,
    enable_cache,
    t_start,
    query_frame,
    log,
    trace,
    trace_id,
    decision_id,
    cost,
    cache,
    request_fetch_cache,
    query_to_use,
    skip_rag,
    is_subjective_answer,
    epistemic_result,
    synthesis_result,
    risk_result,
    intent_result,
    search_result,
    web_used,
    claims_data,
    evidence_data,
    self_model,
    memory,
    reflection,
    motivation,
    core_loop,
    reasoning_info,
    intent_type,
    intent_confidence,
    bad_state_prefix,
    entity,
    enrich_result,
    tracer,
    supporting_ids=None,
    technical_errors=None,
    claims_accepted=None,
    claims_rejected=None,
    total_claims=None,
    epistemic_trust_gate_label=None,
    sql_question_id=None,
):
    log("[10] Optimistic respond...")
    responder = get_responder()
    validation_id = ""

    def _start_bg_validation(val_id: str):
        nonlocal validation_id
        validation_id = val_id
        if not enable_validation:
            query_frame["external_validation_performed"] = False
            return
        if skip_rag or is_subjective_answer:
            query_frame["external_validation_performed"] = False
            log(f"  · Валидация пропущена для субъективного интента")
            return
        if epistemic_result.testability in ["interpretive", "non_falsifiable"] and epistemic_result.domain != "media_interpretation":
            query_frame["external_validation_performed"] = False
            log(f"  · Валидация пропущена для {epistemic_result.testability} утверждения")
            return

        try:
            from agent.ai_validator_redis import send_to_deepseek
            ai_task_id = send_to_deepseek(
                query=query_to_use,
                answer=synthesis_result.answer,
                frame={"epistemic": epistemic_result.__dict__},
                sources=synthesis_result.sources,
            )
            query_frame["external_validation_performed"] = True
            log(f"  · AI валидация отправлена в DeepSeek (ID: {ai_task_id})")
        except Exception as e:
            query_frame["external_validation_performed"] = False
            log(f"  · Ошибка AI валидации: {e}")

        t = threading.Thread(
            target=_background_validate,
            args=(
                query_to_use,
                synthesis_result.answer,
                synthesis_result,
                risk_result,
                intent_result,
                val_id,
                decision_id,
                trace_id,
                intent_result.intent if intent_result else "general",
                search_result,
                web_used,
                verbose
            ),
            daemon=False,
        )
        t.start()
    optimistic = responder.respond(synthesis_result, start_validation=_start_bg_validation)

    if enable_validation and not skip_rag and not is_subjective_answer and epistemic_result.testability not in ["interpretive", "non_falsifiable"]:
        log(f"  · Фоновая валидация запущена (ID: {validation_id[:8]})")
    else:
        log("  · Валидация отключена или пропущена")

    if (
        enable_cache
        and synthesis_result.confidence > 0.3
        and not skip_rag
        and not is_subjective_answer
    ):
        try:
            cache.put_from_synthesis(
                query=query_to_use,
                synthesis_result=synthesis_result,
                epistemic=epistemic_result.__dict__,
                claims=claims_data,
                evidence=evidence_data,
            )
        except Exception as e:
            if verbose:
                log(f"[V6] Ошибка сохранения в кэш: {e}")

    total = round(time.time() - t_start, 2)
    cost["total_ms"] = total * 1000
    mon_record("full_request", total, success=True)

    # Pipeline wall-clock profile report — extracted to
    # agent/orchestrator/runtime/profiling.py (structural extraction;
    # behavior unchanged).
    report_pipeline_profile(cost, total, request_fetch_cache, log, verbose)

    log(f"\n✓ Готово за {total}s")

    tags = _build_tags(intent_result, enrich_result, query_to_use)
    primary_tag = tags[0] if tags else "general"

    try:
        archive_query(
            query=query_to_use,
            tag=primary_tag,
            answer=synthesis_result.answer,
            confidence=synthesis_result.confidence,
            trust_level=synthesis_result.trust_level,
            session_id=request.session_id or "",
            sources=synthesis_result.sources,
        )
    except Exception:
        pass

    trace.cost = cost
    trace.final_answer = synthesis_result.answer
    trace.add_observation("intent_type", intent_type)
    trace.add_observation("intent_confidence", intent_confidence)

    if synthesis_result:
        outcome = OutcomeRecord(
            final_answer=synthesis_result.answer[:500],
            final_answer_type="direct_answer",
            trust_label=synthesis_result.trust_level,
            trust_score=synthesis_result.confidence,
            coverage_ratio=0.5 if len(synthesis_result.answer) > 100 else 0.0,
            latency_ms=cost["total_ms"],
            learning_tags=[primary_tag] if primary_tag else [],
            supporting_claim_ids=supporting_ids if supporting_ids is not None else [],
        )
        trace.set_outcome(outcome)

    # ---- YANDI V6: ЗАПИСЬ В ПАМЯТЬ И РЕФЛЕКСИЯ ----
    if self_model and memory and reflection and motivation and core_loop:
        try:
            self_model.add_decision({
                "query": query_to_use,
                "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                "trust": synthesis_result.trust_level,
                "confidence": synthesis_result.confidence,
                "reason": epistemic_result.reason if not is_subjective_answer else "subjective_analysis",
                "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
            })
            self_model.increment_queries()

            memory.add_query(
                query=query_to_use,
                domain="subjective_analysis" if is_subjective_answer else epistemic_result.domain,
                answer_mode="analysis" if is_subjective_answer else epistemic_result.answer_mode,
                trust=synthesis_result.trust_level,
                confidence=synthesis_result.confidence
            )

            if verbose:
                log("[V3] Запуск рефлексии...")

            evidence_count = len(reasoning_info.get("evidence_records", []))
            reflection_result = reflection.reflect_on_query(
                query=query_to_use,
                response=synthesis_result.answer,
                epistemic={
                    "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                    "testability": epistemic_result.testability if not is_subjective_answer else "subjective",
                    "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                    "should_use_web": epistemic_result.should_use_web if not is_subjective_answer else False,
                    "reason": epistemic_result.reason if not is_subjective_answer else "subjective_analysis",
                    "evidence_count": evidence_count,
                    "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                    "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
                },
                trust=synthesis_result.trust_level,
                confidence=synthesis_result.confidence,
                errors=[] if synthesis_result.confidence > 0.3 else ["low_confidence"],
                validation_result={
                    "performed": bool(query_frame.get("external_validation_performed", False)),
                    "accepted": (
                        claims_accepted
                        if query_frame.get("external_validation_performed", False)
                        and claims_accepted is not None
                        else 0
                    ),
                    "rejected": (
                        claims_rejected
                        if query_frame.get("external_validation_performed", False)
                        and claims_rejected is not None
                        else 0
                    ),
                    "total": (
                        total_claims
                        if query_frame.get("external_validation_performed", False)
                        and total_claims is not None
                        else 0
                    ),
                },
            )

            if verbose and reflection_result.mistakes:
                log(f"[V3] Рефлексия: ошибки: {reflection_result.mistakes}")
            if verbose and reflection_result.lessons:
                log(f"[V3] Уроки: {reflection_result.lessons}")
            # ---- КОРРЕКТИРОВКА TRUST НА ОСНОВЕ РЕФЛЕКСИИ (синхронная) ----
            if reflection_result.mistakes:
                old_conf = synthesis_result.confidence
                synthesis_result.confidence = max(0.1, old_conf - 0.15)
                if synthesis_result.trust_level == "STRONGLY_SUPPORTED":
                    synthesis_result.trust_level = "PARTIALLY_SUPPORTED"
                elif synthesis_result.trust_level == "PARTIALLY_SUPPORTED":
                    synthesis_result.trust_level = "WEAKLY_SUPPORTED"
                if verbose:
                    log(f"[V3] Рефлексия: confidence {old_conf:.2f} → {synthesis_result.confidence:.2f}, trust → {synthesis_result.trust_level}")
                query_frame["reflection_verdict"] = {
                    "mistakes": reflection_result.mistakes,
                    "lessons": reflection_result.lessons,
                    "confidence_adjustment": -0.15,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                query_frame["reflection_verdict"] = {
                    "mistakes": [],
                    "lessons": reflection_result.lessons,
                    "confidence_adjustment": 0.0,
                    "timestamp": datetime.now().isoformat()
                }

            # Foundation Repair P0-2 (YANDI_SELF_LEARNING_RECONCILIATION_AUDIT.md
            # P0-2 / YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md section 9):
            # compute canonical Trust HERE — synthesis_result.trust_level is
            # now final for strand 1 (claim status gate + existence contract,
            # both applied upstream in pipeline.py, plus the reflection-mistake
            # downgrade just above are all already folded in), and
            # epistemic_trust_gate_label (strand 2) has been available since
            # function entry. This is the same MIN-of-two-strands computation
            # the original cutover below performs; computing it once here and
            # reusing `_canonical_result` at that cutover avoids a duplicate
            # calculation and duplicate trace.add_observation() calls.
            # Dataset/Experience consumers below (future ExperienceRecord
            # material) must see the FINAL canonical Trust, not the
            # pre-cutover synthesizer-strand value — that was the proven
            # source of systematic trust divergence in persisted learning
            # data (audit P0-2). self_model/memory/reflection above
            # deliberately keep seeing the pre-cutover value: reflection's
            # own mistake-downgrade is itself one of strand 1's inputs, so
            # reflection running before canonicalization is required, not a
            # bug (see the consolidation report's "Legacy paths remaining").
            _canonical_result = compute_canonical_trust(
                synthesis_result.trust_level,
                epistemic_trust_gate_label,
                log,
                verbose,
            )
            _canonical_trust_for_learning = _canonical_result["canonical_trust"]

            # ---- СОХРАНЕНИЕ ОПЫТА В ПАМЯТЬ (асинхронное обучение) ----
            try:
                experience_memory = get_experience_memory()
                if experience_memory:
                    # Определяем speech_act на основе интента
                    speech_act = intent_result.intent if intent_result else "general"
                    # Определяем topic на основе домена
                    topic = epistemic_result.domain if not is_subjective_answer else "subjective"
                    # Сохраняем опыт
                    exp_id = experience_memory.add_experience(
                        speech_act=speech_act,
                        topic=topic,
                        query=query_to_use,
                        response=synthesis_result.answer[:500],  # обрезаем до 500 символов
                        context={
                            "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                            "trust": _canonical_trust_for_learning,
                            "confidence": synthesis_result.confidence,
                            "mistakes": reflection_result.mistakes,
                            "lessons": reflection_result.lessons,
                            "policy_changes": reflection_result.policy_changes if hasattr(reflection_result, "policy_changes") else [],
                            "timestamp": datetime.now().isoformat()
                        }
                    )
                    if verbose:
                        log(f"[V3] Опыт сохранён в память (ID: {exp_id})")
            except Exception as e:
                if verbose:
                    log(f"[V3] Ошибка сохранения опыта: {e}")

            motivation.update_from_experience({
                "was_useful": synthesis_result.confidence > 0.5,
                "was_correct": synthesis_result.trust_level not in ["UNVERIFIED", "REJECTED"],
                "had_conflict": False,
            })
            # ---- СОХРАНЕНИЕ ЭПИЗОДА В DATASET ----
            try:
                dataset_builder = get_dataset_builder()
                dataset_builder.record_episode({
                    # Foundation Repair (episode<->trace identity): reuse the
                    # existing Trace identity (trace_id, already generated
                    # upstream and threaded through this whole function) as
                    # the join key back to registry/dataset/orch_traces/*.jsonl
                    # instead of leaving episodes unlinkable except by fragile
                    # timestamp proximity (proven ~309s drift in the audit).
                    "trace_id": trace_id,
                    "query": query_to_use,
                    "intent": intent_result.intent if intent_result else "unknown",
                    "domain": epistemic_result.domain if not is_subjective_answer else "subjective",
                    "trust": _canonical_trust_for_learning,
                    "confidence": synthesis_result.confidence,
                    "mistakes": reflection_result.mistakes if "reflection_result" in locals() else [],
                    "lessons": reflection_result.lessons if "reflection_result" in locals() else [],
                    "validation": {
                        "accepted": claims_accepted if claims_accepted is not None else 0,
                        "rejected": claims_rejected if claims_rejected is not None else 0,
                        "total": total_claims if total_claims is not None else 0,
                    },  # Закрываем validation
                    "technical_errors": technical_errors if technical_errors is not None else [],
                    "answer": synthesis_result.answer[:500] if synthesis_result.answer else "",
                })
                if verbose:
                    log("[V3] Эпизод сохранён в Dataset")
            except Exception as e:
                if verbose:
                    log(f"[V3] Ошибка сохранения Dataset: {e}")

            if not core_loop.state.is_running:
                core_loop.run_cycle({
                    "query": query_to_use,
                    "epistemic": {
                        "domain": epistemic_result.domain if not is_subjective_answer else "subjective_analysis",
                        "testability": epistemic_result.testability if not is_subjective_answer else "subjective",
                        "answer_mode": epistemic_result.answer_mode if not is_subjective_answer else "analysis",
                        "trust": synthesis_result.trust_level,
                        "confidence": synthesis_result.confidence,
                        "objectivity_score": epistemic_result.objectivity_score if not is_subjective_answer else 0.5,
                        "is_science_as_model": epistemic_result.is_science_as_model if not is_subjective_answer else False,
                    }
                })

            if verbose:
                log(f"[V3] Состояние: цикл {core_loop.state.cycle_number}, "
                    f"запросов {self_model.state.total_queries}, "
                    f"эпизодов {memory.get_stats()['total_episodes']}")

        except Exception as e:
            if verbose:
                log(f"[V3] Ошибка V3: {e}")

    # Epistemic Core v1 Phase 14: canonical Trust CUTOVER — the single
    # point where the final user-facing Trust is decided. Computed here —
    # after EVERY existing mutation of synthesis_result.trust_level (claim
    # status gate, existence contract, and this function's own
    # reflection-mistake downgrade above — all already applied by this
    # point) — and assigned back onto synthesis_result.trust_level, which
    # is exactly what the OrchestratorResponse constructed at the end of
    # this function reads. Nothing downstream of this line recomputes
    # Trust again. See canonical_trust.py's module docstring for the full
    # audit and why this is a MIN over two already-existing, already-
    # monotonic Trust strands — not a new formula — and
    # YANDI_EPISTEMIC_TRUST_CONSOLIDATION_REPORT.md's "Legacy paths
    # remaining" section for the consumers (OutcomeRecord, archive_query,
    # self_model/memory/reflection inputs above) that read
    # synthesis_result.trust_level EARLIER than this point and therefore
    # still see the pre-cutover value — a deliberate, documented scope
    # limit, not an oversight.
    if synthesis_result:
        # Этап 5 (SQL shadow write): pure read, captured BEFORE the
        # overwrite two lines below — the exact synthesizer-strand value
        # that fed this call, for answer_assessment's diagnostic columns.
        # No existing control flow or value touched.
        _sql_synthesizer_strand = synthesis_result.trust_level

        _canonical_result = compute_canonical_trust(
            synthesis_result.trust_level,
            epistemic_trust_gate_label,
            log,
            verbose,
        )
        synthesis_result.trust_level = _canonical_result["canonical_trust"]
        trace.trust = _canonical_result["canonical_trust"]
        trace.trust_reason = _canonical_result["reason"]
        trace.add_observation("canonical_trust", _canonical_result["canonical_trust"])
        trace.add_observation("canonical_trust_diverged", _canonical_result["diverged"])
        trace.add_observation("canonical_trust_stricter_strand", _canonical_result["stricter_strand"])

        # Foundation Repair P0-2: trace.outcome (OutcomeRecord, set further
        # above from the pre-cutover trust snapshot) is still an in-memory
        # object at this point — tracer.save_trace() below is what actually
        # persists it. Patch it to the canonical value here so the PERSISTED
        # trace (registry/dataset/orch_traces/*.jsonl) never carries a
        # trust_label that disagrees with trace.trust in the same file —
        # this was the audit's proven concrete divergence (P0-2). Fixing the
        # source object in place, not masking the earlier snapshot.
        if trace.outcome is not None:
            trace.outcome.trust_label = _canonical_result["canonical_trust"]

    if bad_state_prefix:
        synthesis_result.answer = bad_state_prefix + synthesis_result.answer

    # ---- БАННЕР ----
    if is_subjective_answer:
        banner = "[МНЕНИЕ ЯНДИ • СУБЪЕКТИВНАЯ ИНТЕРПРЕТАЦИЯ]"
    elif epistemic_result.domain == "media_interpretation" and not is_subjective_answer:
        if entity:
            banner = f"[РАЗБОР ФИЛЬМА • {entity.get('title', '')}]"
        else:
            banner = "[РАЗБОР ФИЛЬМА • ТРЕБУЕТСЯ УТОЧНЕНИЕ]"
    elif epistemic_result.testability in ["interpretive", "non_falsifiable"] and not is_subjective_answer:
        banner = "[ИНТЕРПРЕТАТИВНЫЙ ОТВЕТ • ОБЗОР РАМОК]"
    elif epistemic_result.answer_mode == "pluralistic_contextual" and not is_subjective_answer:
        banner = "[МНОГОПЕРСПЕКТИВНЫЙ ОТВЕТ • НЕТ ЕДИНСТВЕННОЙ ТРАКТОВКИ]"
    elif epistemic_result.is_science_as_model and not is_subjective_answer:
        banner = "[НАУЧНАЯ МОДЕЛЬ • ЭТО ТЕОРИЯ, НЕ ИСТИНА]"
    else:
        banner = "[ПРЕДВАРИТЕЛЬНЫЙ • ⏳ На проверке]"

    if not optimistic.text.startswith(banner):
        optimistic.text = f"{banner}\n\n{optimistic.text}"

    # P0 (storage audit, delivered-answer correctness): capture the
    # LITERAL delivered text — byte-identical to what OrchestratorResponse
    # .answer returns below — as its own trace observation, saved BEFORE
    # tracer.save_trace() so it actually lands in the persisted JSONL line
    # (previously this banner/prefix block ran AFTER save_trace, so
    # nothing persisted ever saw it). trace.final_answer (set earlier from
    # synthesis_result.answer, pre-optimistic-wrapping) intentionally KEEPS
    # its existing "clean prose, no badges/banner/source-list" meaning for
    # any consumer that wants that instead — this is additive, not a
    # replacement, because the two are now provably different: the trust
    # badge baked into optimistic.text at responder.respond() (above,
    # earlier in this function) reflects synthesis_result.trust_level as
    # it stood BEFORE this function's own reflection-mistake downgrade and
    # the canonical-Trust cutover just above — see this module's
    # regression test for a concrete case where they diverge.
    trace.add_observation("delivered_answer_text", optimistic.text)

    # Этап 5 (SQL shadow write): fail-open, never touches the JSON path
    # above or below it — see agent/db/sql/shadow_write.py's module
    # docstring and agent/db_sql_shadow_write_regression_test.py.
    shadow_complete_run(
        run_id=trace_id,
        question_id=sql_question_id,
        delivered_answer_text=optimistic.text,
        completed_at=datetime.now(),
        canonical_trust=(_canonical_result["canonical_trust"] if synthesis_result else "UNVERIFIED"),
        synthesizer_strand=(_sql_synthesizer_strand if synthesis_result else None),
        trust_gate_strand=epistemic_trust_gate_label,
        diverged=(_canonical_result["diverged"] if synthesis_result else False),
        stricter_strand=(_canonical_result.get("stricter_strand") if synthesis_result else None),
        reason=(_canonical_result.get("reason") if synthesis_result else None),
        log=log, verbose=verbose,
    )

    tracer.save_trace(trace)
    log(f"  · Трейс сохранен: {trace_id}")

    return OrchestratorResponse(
        answer=optimistic.text,
        trust_level=synthesis_result.trust_level,
        preliminary=True,
        sources=synthesis_result.sources,
        steps_taken=[],
        latency_total=total,
        session_id=request.session_id,
        trace_id=trace_id,
    )
