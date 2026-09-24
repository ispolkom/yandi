//! АВТОГЕНЕРИРОВАНО rustlib/gen_trust_data.py из agent/orchestrator/epistemic/trust_gate.py — не править руками.

pub static TRUST_ORDER: &[(&str, i64)] = &[
    ("STRONGLY_SUPPORTED", 5),
    ("SUPPORTED", 4),
    ("VERIFIED", 4),
    ("PARTIALLY_SUPPORTED", 3),
    ("PARTIAL", 3),
    ("EMPIRICALLY_SUPPORTED", 4),
    ("EMPIRICALLY_UNTESTABLE", 2),
    ("UNVERIFIED", 1),
    ("HYPOTHESIS", 1),
    ("RELIGIOUS_CLAIM", 2),
    ("METAPHYSICAL_UNTESTABLE", 2),
    ("VALUE_FRAMEWORK", 2),
    ("BOUNDARY_QUESTION", 2),
    ("ONTOLOGICAL_INQUIRY", 2),
    ("NORMATIVE_POSITION", 2),
    ("CONTESTED", 2),
    ("WEAKLY_SUPPORTED", 2),
];
