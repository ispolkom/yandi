"""
Structural claim validation — extracted from agent/orchestrator_v2.py [8]
("STRUCTURAL CLAIM VALIDATION" block).

Thin orchestration wrapper around agent.claim_validator's
ClaimValidator.filter_claims() (the V6 singleton, `_claim_validator` in
orchestrator_v2.py, owned by `_init_v3()`) — this module does not
reimplement any validation logic, only the surrounding diagnostics/trace
bookkeeping that orchestrator_v2.py previously inlined.

Порядок принципиален:

  Normalize
      ↓
  Structural Validator
      ├── rejected → diagnostic trace only
      ↓
  accepted claims
      ↓
  Mapper → NLI → Epistemic Status

Structural rejection НЕ означает ложность утверждения.
Это означает только: объект непригоден как атомарный claim.
"""


def apply_structural_claim_validation(claims_data, claim_validator, reasoning_info, trace, log, verbose):
    """
    Returns (claims_data, rejected_structural_claims).

    claims_data: the filtered/accepted claim list (claim_validator.filter_claims()
    output), or unchanged if claim_validator is falsy.
    rejected_structural_claims: claims claim_validator marked rejected, kept
    for diagnostic trace only (never dropped silently).
    """
    # P0 (YANDI_CLAIM_LIFECYCLE_DISAPPEARANCE_AUDIT.md): диагностический
    # boundary-трейс, без изменения поведения. synthesized — то, что
    # реально вернул synthesize() в reasoning_info["claims"]; lifecycle/
    # validator_input — тот же claims_data непосредственно перед тем,
    # как он передаётся в ClaimValidator.filter_claims(). Если
    # lifecycle>0, а validator_input==0 когда-нибудь снова — это уже
    # не может быть тем же багом (claims теперь сохраняются даже при
    # позднем сбое synthesize()), значит источник новый.
    if verbose:
        log(
            "[Claim Pipeline Boundary] "
            f"synthesized={len(reasoning_info.get('claims', [])) if isinstance(reasoning_info, dict) else 0} "
            f"lifecycle={len(claims_data)} "
            f"validator_input={len(claims_data)}"
        )

    rejected_structural_claims = []

    if claim_validator:
        try:
            pre_validation_claims = list(claims_data)

            claims_data = claim_validator.filter_claims(
                pre_validation_claims
            )

            rejected_structural_claims = [
                claim
                for claim in pre_validation_claims
                if (
                    claim.get("structural_validation") == "rejected"
                    or claim.get("_rejected") is True
                )
            ]

            # Rejected claims не исчезают.
            # Они сохраняются отдельно как диагностические объекты.
            for claim in rejected_structural_claims:
                trace.rejected_claims.append({
                    "claim_id": claim.get(
                        "claim_id",
                        "unknown",
                    ),
                    "claim_text": (
                        claim.get("claim_text", "") or ""
                    )[:200],
                    "claim_type": claim.get(
                        "claim_type",
                        "unknown",
                    ),
                    "rejection_reason": claim.get(
                        "_rejected_reason",
                        "structural_validation",
                    ),
                })

            if verbose:
                log(
                    f"[Claim Validator] "
                    f"accepted={len(claims_data)} "
                    f"rejected={len(rejected_structural_claims)} "
                    f"reasons={claim_validator.rejection_reasons}"
                )

                for claim in claims_data:
                    log(
                        "[Claim Validator] ACCEPT: "
                        f"{claim.get('claim_text', '')[:250]}"
                    )

                for claim in rejected_structural_claims:
                    log(
                        "[Claim Validator] REJECT: "
                        f"reason={claim.get('_rejected_reason', 'unknown')} "
                        f"text={claim.get('claim_text', '')[:250]}"
                    )

        except Exception as e:
            if verbose:
                log(
                    f"[Claim Validator] error={e}"
                )

    return claims_data, rejected_structural_claims
