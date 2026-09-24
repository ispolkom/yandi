"""
pet/event_extraction.py — RELATIONAL EVENT EXTRACTION for the personal chat.

    THE MODEL MAY CHOOSE THE EVIDENCE LOCATION.
    THE CODE OWNS THE EVIDENCE TEXT.
    NO EXACT SUPPORT IN THE USER'S MESSAGE = NO EVENT.

Why this exists. The old path asked the model, in the same generation as its
reply, for `is_insult` / `is_apology` / ... plus a verbatim `evidence` quote.
The model classified correctly but almost never produced a verbatim quote
(empty, paraphrased, or an English description), so the verbatim-evidence guard
rejected nearly every real event. The guard was right; asking a language model to
COPY text was the weak step.

Protocol (evidence first, classification second):

  1. The current message is cut into numbered words (string segmentation only).
  2. The extractor sees ONLY that message, never memory or history, so a
     remembered event cannot be re-asserted as a current one. It returns, for
     each candidate, the word range that constitutes the event (`span`), then
     the event `type`, plus severity / sincerity where the downstream logic
     needs them. It never returns quote text.
  3. Code validates the references (integers, in range, non-empty, bounded)
     and reconstructs the canonical evidence itself:
     `message[start_char:end_char]`. The reconstructed span is literally part
     of the message by construction.
  4. A second, independent judgement about that exact span in its message,
     BLIND to what the extractor claimed: which act does the fragment express
     (insult / apology / promise / fulfilment claim / none), and in what frame
     (performed now by the user, a quotation, a hypothetical, a negation, a
     report about the past, a topic of discussion)? The event is admissible
     only if the blind act equals the extracted type AND the frame is
     "performed now". Asking a model to CONFIRM a claimed type was measured to
     be too lenient; making it classify first and letting code compare is not.
  5. Any malformed output, invalid reference, overlapping spans, failed or
     disagreeing check, or extractor error => no event. Fail closed.

This module only RECOGNISES candidates. It never changes trust / respect /
affection, never creates grievance transitions, never verifies that a promise
was kept, and it contains no vocabulary of insults / apologies / promises: every
string operation below is segmentation, offset validation, exact reconstruction
or schema validation. Relational consequences stay with
agent/relationship_memory.py, relationship_state.py and relationship_commitments.py.

Rust-перенос (2026-09-24): rustlib/yandi_rs/src/pet_extraction.rs — чистые помощники этого модуля и его соседей
(segment_words, подготовка текста к json.loads, _validate_candidate; у fact_extraction — _validate/looks_secret;
у commitment_verification — inside_quotation). Доказан тестом pet/pet_extraction_rust_parity_test.py. По умолчанию ВЫКЛЮЧЕН;
включается YANDI_PET_EXTRACTION_ENGINE=rust ПОСЛЕ сборки rustlib/yandi_rs. Посторонние типы входа и одинокие суррогаты
откатываются на исходный Python-код; константы Rust получает из Python при каждом вызове.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from agent.message_intensity import IntensityResult

INSULT, APOLOGY, PROMISE, CLAIM = "insult", "apology", "promise", "fulfilment_claim"
EVENT_TYPES = (INSULT, APOLOGY, PROMISE, CLAIM)
COMMITMENT_TYPES = (PROMISE, CLAIM)

MAX_WORDS = 120          # a long pasted text is not an interpersonal event: fail closed
MAX_SPAN_WORDS = 30      # an event is a short stretch of words, not the whole document
MAX_EVENTS = 3
EVIDENCE_MIN_CHARS = 3
ACCEPTED_FRAME = "current"
ACCEPTED_ACTS = EVENT_TYPES

# LLM callable: (chat messages) -> raw model text. Injected so the protocol is
# testable without a model and the caller owns model/adapter selection.
LlmCall = Callable[[List[Dict[str, str]]], str]

_log = logging.getLogger("yandi.pet_extraction")
_rust_pe = None          # None = ещё не пробовали; False = не запрошено/не собрано; модуль = подключён


def _get_rust_pe():
    """Общий загрузчик для event_extraction / fact_extraction / commitment_verification."""
    global _rust_pe
    if _rust_pe is None:
        if os.environ.get("YANDI_PET_EXTRACTION_ENGINE") == "rust":
            try:
                import yandi_rs.pet_extraction as _rs
                _rust_pe = _rs
                _log.warning("YANDI_PET_EXTRACTION_ENGINE=rust: используется Rust-реализация pet_extraction (rustlib/yandi_rs)")
            except ImportError as e:
                _log.warning("YANDI_PET_EXTRACTION_ENGINE=rust запрошен, но yandi_rs не собран (%s) — использую Python", e)
                _rust_pe = False
        else:
            _rust_pe = False
    return _rust_pe or None


@dataclass(frozen=True)
class ExtractedEvent:
    type: str
    evidence: str            # reconstructed by code: message[start:end]
    start: int
    end: int
    severity: float = 0.0
    sincerity: float = 0.0


@dataclass
class ExtractionResult:
    events: List[ExtractedEvent] = field(default_factory=list)
    candidates: int = 0                                  # events the extractor proposed
    proposed: List[str] = field(default_factory=list)    # types the extractor proposed (diagnostics)
    validated: List[str] = field(default_factory=list)   # types whose evidence reference was valid
    rejected: List[str] = field(default_factory=list)   # why candidates / the whole turn were dropped
    calls: int = 0
    answered: bool = False                               # the model answered with the expected JSON (an empty list is an answer)
    judged: List[Tuple[str, int, int]] = field(default_factory=list)  # (type, start, end) of every span the blind judgement confirmed,
                                                                      # including a commitment that the sole-candidate rule later dropped


_EXTRACT_SYSTEM = (
    "Ты — модуль распознавания событий в личном чате. Тебе дано ОДНО сообщение пользователя, "
    "разбитое на пронумерованные слова. Найди события, которые пользователь совершает САМ, СЕЙЧАС, "
    "В ЭТОМ сообщении, обращаясь к ассистенту YANDI:\n"
    "- insult — пользователь оскорбляет или унижает YANDI;\n"
    "- apology — пользователь просит прощения у YANDI, выражает сожаление о том, что сделал ей;\n"
    "- promise — пользователь берёт на себя обязательство: говорит, что СДЕЛАЕТ что-то в будущем "
    "(придёт, напишет, пришлёт, поможет, исправит…), в том числе с оговорками вроде «обязательно», «даю слово»;\n"
    "- fulfilment_claim — пользователь сообщает, что УЖЕ сделал то, что должен был сделать, что обещал "
    "или о чём договаривались; в том числе короткое «готово», «сделано», «всё выполнил».\n"
    "НЕ являются событиями: цитата чужих слов или слов, сказанных раньше; гипотетический пример; "
    "вопрос о значении слов; отрицание («я не оскорбляю»); рассказ о том, что было раньше; "
    "обсуждение оскорблений, извинений или обещаний как темы; обычное сообщение о себе или о мире. "
    "Одни и те же слова не могут быть двумя разными событиями; не придумывай событие, если слова его не составляют.\n"
    "Для каждого события СНАЧАЛА укажи слова-доказательство: span=[первое, последнее] — номера первого "
    "и последнего слова из списка (оба включительно; для одного слова оба номера одинаковы, например [0,0]); "
    "только те слова, которые сами составляют событие; ПОТОМ type; "
    "для insult ещё severity (0..1), для apology ещё sincerity (0..1). Текст цитаты не пиши, "
    "только номера слов. Если события нет — верни пустой список.\n"
    'Ответ — один JSON без пояснений: {"events":[{"span":[0,2],"type":"apology","sincerity":0.8}]}'
)

_CHECK_SYSTEM = (
    "Тебе даны сообщение пользователя из личного чата с ассистентом YANDI и ОДИН его фрагмент. "
    "Ответь на два вопроса именно про этот фрагмент.\n"
    "1) act — какое действие в отношении ассистента выражает фрагмент сам по себе: "
    "insult (оскорбляет или унижает), apology (просит прощения, выражает сожаление о сделанном ей), "
    "promise (берёт обязательство сделать что-то в будущем), "
    "fulfilment_claim (сообщает, что уже сделал то, что обещал или должен был), "
    "none (ничего из этого).\n"
    "2) frame — в каком виде это действие представлено в сообщении: "
    "current (пользователь САМ, СЕЙЧАС, обращаясь к ассистенту, совершает его), "
    "quotation (цитата чужих слов или слов, сказанных в другое время), "
    "hypothetical (гипотетический пример или условие), negated (действие отрицается), "
    "past_report (рассказ о том, что было раньше), topic (обсуждение как темы или вопрос о нём).\n"
    'Ответ — один JSON без пояснений: {"act":"insult","frame":"current"}'
)


def segment_words(message: str) -> List[Tuple[str, int, int]]:
    """(word, start_char, end_char) for every whitespace-separated word."""
    rs = _get_rust_pe()
    if rs is not None and type(message) is str:
        try:
            return rs.segment_words(message)
        except UnicodeEncodeError:
            pass        # одинокий суррогат: Rust строку не принимает — исходный код ниже
    return [(m.group(), m.start(), m.end()) for m in re.finditer(r"\S+", message)]


def _numbered(words: List[Tuple[str, int, int]]) -> str:
    return " ".join(f"{i}:{w}" for i, (w, _, _) in enumerate(words))


def _json_object(raw: object) -> Optional[dict]:
    """Strict JSON object parse; a surrounding code fence is the only leniency."""
    if not isinstance(raw, str):
        return None
    rs = _get_rust_pe()
    if rs is not None and type(raw) is str:
        try:
            text = rs.json_text(raw)
        except UnicodeEncodeError:
            text = None
        if text is not None:
            try:
                value = json.loads(text)
            except (ValueError, TypeError):
                return None
            return value if isinstance(value, dict) else None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
        text = text[: text.rfind("}") + 1]
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _in_unit_range(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0.0 <= value <= 1.0


def _validate_candidate(item: object, words: List[Tuple[str, int, int]]) -> Tuple[Optional[Tuple[str, int, int, float, float]], str]:
    """Return ((type, first_word, last_word, severity, sincerity), "") or (None, reason)."""
    rs = _get_rust_pe()
    if rs is not None and type(words) is list:
        try:
            r = rs.validate_candidate(item, len(words), EVENT_TYPES, INSULT, APOLOGY, MAX_SPAN_WORDS)
        except UnicodeEncodeError:
            r = None
        if r is not None:
            return r
    if not isinstance(item, dict):
        return None, "candidate is not an object"
    kind = item.get("type")
    if kind not in EVENT_TYPES:
        return None, "unknown event type"
    span = item.get("span")
    if not (isinstance(span, list) and len(span) == 2 and all(_is_int(x) for x in span)):
        return None, "span is not two integers"
    first, last = span
    if not (0 <= first <= last < len(words)):
        return None, "span reference out of range"
    if last - first + 1 > MAX_SPAN_WORDS:
        return None, "span too long to be one event"
    severity = sincerity = 0.0
    if kind == INSULT:
        if not _in_unit_range(item.get("severity")):
            return None, "insult without a severity in 0..1"
        severity = float(item["severity"])
    if kind == APOLOGY:
        if not _in_unit_range(item.get("sincerity")):
            return None, "apology without a sincerity in 0..1"
        sincerity = float(item["sincerity"])
    return (kind, first, last, severity, sincerity), ""


def _check_messages(message: str, evidence: str) -> List[Dict[str, str]]:
    """The blind judgement: the fragment and its message, NOT the claimed type."""
    user = f"Сообщение пользователя:\n«{message}»\n\nФрагмент:\n«{evidence}»"
    return [{"role": "system", "content": _CHECK_SYSTEM}, {"role": "user", "content": user}]


def extract_relational_events(message: str, llm: LlmCall) -> ExtractionResult:
    """Recognise the relational events the user performs in THIS message.
    Never raises on model/transport trouble: any failure is "no event"."""
    result = ExtractionResult()
    if not isinstance(message, str):
        result.rejected.append("message is not text")
        return result
    words = segment_words(message)
    if not words:
        return result
    if len(words) > MAX_WORDS:
        result.rejected.append("message too long for event extraction")
        return result

    try:
        result.calls += 1
        raw = llm([
            {"role": "system", "content": _EXTRACT_SYSTEM},
            {"role": "user", "content": f"Сообщение:\n{message}\n\nСлова (номера от 0 до {len(words) - 1}):\n{_numbered(words)}"},
        ])
    except Exception as exc:  # noqa: BLE001 - transport trouble must never become an event
        result.rejected.append(f"extractor call failed: {type(exc).__name__}")
        return result
    data = _json_object(raw)
    if data is None or not isinstance(data.get("events"), list):
        result.rejected.append("extractor output is not the expected JSON object")
        return result
    result.answered = True
    proposed = data["events"]
    if len(proposed) > MAX_EVENTS:
        result.rejected.append("extractor proposed too many events")
        return result
    result.candidates = len(proposed)
    result.proposed = [i.get("type") for i in proposed if isinstance(i, dict)]

    valid: List[Tuple[str, int, int, float, float]] = []
    for item in proposed:
        candidate, reason = _validate_candidate(item, words)
        if candidate is None:
            result.rejected.append(reason)
        else:
            valid.append(candidate)
            result.validated.append(candidate[0])

    # Overlapping spans of different events are ambiguous: one event must not
    # authenticate another, so every event that shares words with another is dropped.
    clean = []
    for i, cand in enumerate(valid):
        overlaps = any(j != i and not (cand[2] < other[1] or other[2] < cand[1]) for j, other in enumerate(valid))
        if overlaps:
            result.rejected.append("overlapping evidence spans are ambiguous")
        else:
            clean.append(cand)

    for kind, first, last, severity, sincerity in clean:
        start, end = words[first][1], words[last][2]
        evidence = message[start:end]
        if len(evidence.strip()) < EVIDENCE_MIN_CHARS:
            result.rejected.append("evidence span too short")
            continue
        try:
            result.calls += 1
            verdict = _json_object(llm(_check_messages(message, evidence)))
        except Exception as exc:  # noqa: BLE001
            result.rejected.append(f"check call failed: {type(exc).__name__}")
            continue
        if not verdict or verdict.get("act") != kind:
            result.rejected.append(f"{kind}: the blind judgement of the span disagrees")
            continue
        if verdict.get("frame") != ACCEPTED_FRAME:
            result.rejected.append(f"{kind}: the span is not an act performed now by the user")
            continue
        result.events.append(ExtractedEvent(kind, evidence, start, end, severity, sincerity))
        result.judged.append((kind, start, end))

    # A promise or a claim of fulfilment is admissible only as the ONLY candidate
    # the extractor proposed in this turn. If the model proposed anything else
    # next to it (a second event, or the opposite commitment type), it is confused
    # about the message, and a commitment is exactly the event that must not be
    # guessed: a promise confirmed as a claim on a single word was measured.
    if result.candidates != 1 and any(e.type in COMMITMENT_TYPES for e in result.events):
        result.events = [e for e in result.events if e.type not in COMMITMENT_TYPES]
        result.rejected.append("commitment event dropped: it was not the only candidate proposed")
    return result


def to_intensity(result: ExtractionResult) -> IntensityResult:
    """Map validated events onto the domain object the lifecycle already
    consumes. Commitment events keep their existing restriction: accepted only
    when they are the sole event of the turn, and a promise together with a
    claim is contradictory."""
    events = list(result.events)
    kinds = {e.type for e in events}
    commitment = [e for e in events if e.type in COMMITMENT_TYPES]
    if commitment and (len(events) > len(commitment) or len(commitment) > 1):
        events = [e for e in events if e.type not in COMMITMENT_TYPES]
        kinds = {e.type for e in events}
    insult = next((e for e in events if e.type == INSULT), None)
    apology = next((e for e in events if e.type == APOLOGY), None)
    lone_commitment = events[0] if len(events) == 1 and events[0].type in COMMITMENT_TYPES else None
    return IntensityResult(
        ok=True,
        is_insult=insult is not None,
        is_apology=apology is not None,
        severity=insult.severity if insult else 0.0,
        sincerity=apology.sincerity if apology else 0.0,
        is_promise=bool(lone_commitment and lone_commitment.type == PROMISE),
        claims_fulfilled=bool(lone_commitment and lone_commitment.type == CLAIM),
        evidence=(lone_commitment.evidence if lone_commitment else (events[0].evidence if events else "")),
        spans=tuple((e.type, e.start, e.end) for e in events),
    )
