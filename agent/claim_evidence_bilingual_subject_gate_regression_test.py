"""
agent/claim_evidence_bilingual_subject_gate_regression_test.py

Subject Gate architecture change: bilingual (RU+EN) content anchors,
replacing the "add another hand-maintained alias entry" approach that
produced the earlier ES/NATO false-anchor bug (see agent/claim_identity.py
and agent/final_epistemic_regression_test.py's "PRODUCTION BUG" section
for that fix, unaffected/preserved here).

Covers:
    - extract_content_anchors() (agent/claim_identity.py): content words
      beyond proper nouns, stopword/filler filtering, short-token
      exclusion (keeps "ес" out of the content path too).
    - bilingual_claim_anchor_tiers(): named vs content tier split,
      content minus named dedup, translation-call short-circuit for
      already-Latin text (no wasted local LLM call — mandate: "не
      удваивать web fetch budget").
    - _anchor_hit(): word-boundary matching — "sun" must not match
      inside "sunlight" (the false positive found while building this).
    - _subject_anchor_matches() end-to-end with the two tiers: Mars/
      Earth/solar-power(technology) evidence still REJECTed for a Sun
      claim; Wikipedia Sun/NASA solar evidence PASSes; the original
      production bug's exact reproduction (named-anchor-empty edge
      case) still PASSes real Solar core/Interior evidence.
    - Backward compatibility: calling _subject_anchor_matches() without
      the new named_anchors/content_anchors kwargs still behaves exactly
      like before (old callers/tests unaffected).
    - Translation failure returns "" (never the untranslated original).

Run: /home/iam/venv/bin/python3 -m agent.claim_evidence_bilingual_subject_gate_regression_test
"""
from __future__ import annotations

from unittest.mock import patch

import agent.claim_evidence_retriever as cer
from agent.claim_identity import extract_content_anchors

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"OK   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


# ============================================================
# extract_content_anchors()
# ============================================================

check(
    "1. RU content anchors include ordinary nouns, not just proper nouns",
    set(extract_content_anchors("Есть ли жизнь на Солнце?")) == {"жизнь", "солнце"},
    f"{extract_content_anchors('Есть ли жизнь на Солнце?')}",
)
check(
    "1b. EN content anchors likewise",
    set(extract_content_anchors("Is there life on the Sun?")) == {"life", "sun"},
    f"{extract_content_anchors('Is there life on the Sun?')}",
)
check(
    "2. retrieval-filler vocabulary (research/evidence/study/...) excluded",
    not (set(extract_content_anchors(
        "primary evidence research study data observations confirmed detection"
    ))),
    f"{extract_content_anchors('primary evidence research study data observations confirmed detection')}",
)
check(
    "3. short alias-only keys ('ес') stay out of the generic content path "
    "(still handled exclusively by extract_subject_anchors' whole-word fix)",
    "ес" not in extract_content_anchors("Если посмотреть на это, естественно, есть нюанс."),
    f"{extract_content_anchors('Если посмотреть на это, естественно, есть нюанс.')}",
)

print()

# ============================================================
# bilingual_claim_anchor_tiers()
# ============================================================

_TRANSLATIONS = {
    "Есть ли жизнь на Солнце?": "Is there life on the Sun?",
    "Температура поверхности Солнца около 5500°C": "The surface temperature of the Sun is about 5500 C",
    "Солнце: есть ли у него экстремальное давление в его недрах?": "The Sun: does it have extreme pressure in its interior?",
}


def _fake_translate(text: str) -> str:
    return _TRANSLATIONS.get(text, "")


with patch.object(cer, "_translate_claim_to_english", side_effect=_fake_translate):
    named1, content1 = cer.bilingual_claim_anchor_tiers("Есть ли жизнь на Солнце?")
    check(
        "4. named tier = bilingual proper-noun union",
        set(named1) == {"солнце", "sun"},
        f"named={named1}",
    )
    check(
        "4b. content tier = bilingual content words, MINUS named anchors",
        set(content1) == {"жизнь", "life"},
        f"content={content1}",
    )

    named2, content2 = cer.bilingual_claim_anchor_tiers(
        "Температура поверхности Солнца около 5500°C"
    )
    check(
        "5. second example matches mandate's own worked case",
        set(named2) == {"солнца", "sun"}
        and {"температура", "поверхности", "surface", "temperature"} <= set(content2),
        f"named={named2} content={content2}",
    )

print()

# ============================================================
# Translation call discipline (no wasted / doubled LLM calls)
# ============================================================

_calls = []


def _tracking_call_ollama(prompt):
    _calls.append(prompt)
    return '{"en": "unused"}'


with patch.object(cer, "_call_ollama", side_effect=_tracking_call_ollama):
    _calls.clear()
    result = cer._translate_claim_to_english("The Sun is a star.")
    check(
        "6. already-Latin text short-circuits — NO local LLM call made",
        result == "The Sun is a star." and len(_calls) == 0,
        f"result={result!r} calls={len(_calls)}",
    )

    _calls.clear()
    result = cer._translate_claim_to_english("Солнце — это звезда.")
    check(
        "6b. Cyrillic text DOES trigger exactly one translation call",
        len(_calls) == 1,
        f"calls={len(_calls)}",
    )

    _calls.clear()
    result = cer._translate_claim_to_english("")
    check(
        "6c. empty text short-circuits — no call, returns ''",
        result == "" and len(_calls) == 0,
    )


def _raising_call_ollama(prompt):
    raise RuntimeError("ollama unreachable")


with patch.object(cer, "_call_ollama", side_effect=_raising_call_ollama):
    result = cer._translate_claim_to_english("Солнце — это звезда.")
    check(
        "7. translation failure returns '' — never the untranslated "
        "Cyrillic original (which could never match English evidence)",
        result == "",
        f"result={result!r}",
    )

print()

# ============================================================
# _anchor_hit() word-boundary matching
# ============================================================

check(
    "8. 'sun' does NOT match inside 'sunlight' (the false positive found "
    "while building this)",
    cer._anchor_hit("sun", "solar power panels convert sunlight into electricity") is False,
)
check(
    "8b. 'sun' DOES match as a real standalone word",
    cer._anchor_hit("sun", "the sun is bright today") is True,
)
check(
    "8c. multi-word anchor ('европейский союз') still matches as a phrase",
    cer._anchor_hit("европейский союз", "решение принял европейский союз вчера") is True,
)

print()

# ============================================================
# End-to-end Subject Gate: Mars/Earth/solar-power reject,
# Wikipedia Sun/NASA pass, original production bug stays fixed.
# ============================================================

with patch.object(cer, "_translate_claim_to_english", side_effect=_fake_translate):
    named1, content1 = cer.bilingual_claim_anchor_tiers("Есть ли жизнь на Солнце?")

    matched, fields = cer._subject_anchor_matches(
        "x", "No sign of life was found on Mars during rover missions.",
        title="Life on Mars - Wikipedia", url="https://en.wikipedia.org/wiki/Mars",
        named_anchors=named1, content_anchors=content1,
    )
    check("9. Mars-only evidence REJECTed for a Sun-life claim", matched is False, f"{matched} {fields}")

    matched, fields = cer._subject_anchor_matches(
        "x", "The Sun supports no known life forms.",
        title="Sun - Wikipedia", url="https://en.wikipedia.org/wiki/Sun",
        named_anchors=named1, content_anchors=content1,
    )
    check("9b. Wikipedia Sun evidence PASSes", matched is True, f"{matched} {fields}")

    named2, content2 = cer.bilingual_claim_anchor_tiers(
        "Температура поверхности Солнца около 5500°C"
    )

    matched, fields = cer._subject_anchor_matches(
        "x", "Solar power panels convert sunlight into electricity efficiently.",
        title="Solar power - Wikipedia", url="https://en.wikipedia.org/wiki/Solar_power",
        named_anchors=named2, content_anchors=content2,
    )
    check(
        "10. solar-power (technology) evidence REJECTed despite sharing "
        "the word 'sunlight' with a Sun claim",
        matched is False, f"{matched} {fields}",
    )

    matched, fields = cer._subject_anchor_matches(
        "x", "The surface temperature of the Sun photosphere is about 5500 C.",
        title="Sun Facts - NASA Science", url="https://science.nasa.gov/sun/facts/",
        named_anchors=named2, content_anchors=content2,
    )
    check("10b. NASA solar-temperature evidence PASSes", matched is True, f"{matched} {fields}")

    matched, fields = cer._subject_anchor_matches(
        "x", "Earth atmosphere maintains stable pressure at sea level.",
        title="Atmosphere of Earth - Wikipedia", url="https://en.wikipedia.org/wiki/Atmosphere_of_Earth",
        named_anchors=named2, content_anchors=content2,
    )
    check("10c. Earth-only evidence REJECTed", matched is False, f"{matched} {fields}")

    # Original production bug's own pathological case: the RU sun
    # reference is sentence-initial in both claim_text and query_context,
    # so the RU pass of extract_subject_anchors() alone yields no named
    # anchor — the EN translation ("The Sun: ...") happens to recover
    # one anyway (an emergent benefit of the bilingual approach, not
    # something to assert away); what matters for THIS bug is that the
    # real evidence still passes either way.
    named3, content3 = cer.bilingual_claim_anchor_tiers(
        "Солнце: есть ли у него экстремальное давление в его недрах?"
    )
    for title, url, passage in [
        ("Solar core - Wikipedia", "https://en.wikipedia.org/wiki/Solar_core",
         "The solar core is the region of extreme pressure and temperature within the Sun."),
        ("Solar Interior | National Solar Observatory", "https://nso.edu/for-public/solar-interior/",
         "Pressure inside the Sun interior reaches extreme values near the core."),
    ]:
        matched, fields = cer._subject_anchor_matches(
            "x", passage, title=title, url=url,
            named_anchors=named3, content_anchors=content3,
        )
        check(f"11b. {title!r} PASSes via content-tier fallback", matched is True, f"{matched} {fields}")

print()

# ============================================================
# Backward compatibility: old call shape (no new kwargs) unchanged.
# ============================================================

matched, fields = cer._subject_anchor_matches(
    "Ни один телескоп не зафиксировал сигнала на Юпитере.",
    "Europa's icy surface has intrigued astronomers for decades.",
    title="Europa (moon) - Wikipedia", url="https://en.wikipedia.org/wiki/Europa_(moon)",
)
check(
    "12. legacy call (no named_anchors/content_anchors) still rejects "
    "Europa evidence for a Jupiter claim, exactly as before this change",
    matched is False, f"{matched} {fields}",
)

print()
print(f"РЕЗУЛЬТАТ: {PASS} passed, {FAIL} failed")
if FAIL:
    raise SystemExit(1)
print("все проверки пройдены")
