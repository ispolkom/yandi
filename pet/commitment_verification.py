"""
pet/commitment_verification.py — VERIFICATION OF DIRECTLY OBSERVABLE FULFILMENT.

    USER REPORTS FULFILMENT != FULFILMENT VERIFIED.
    EXTERNAL SELF-REPORT != DIRECT OBSERVATION.
    THE MODEL MAY POINT TO EVIDENCE.  THE MODEL MAY NOT INVENT EVIDENCE.
    AMBIGUOUS TARGET != VERIFIED TARGET.   ONE EVIDENCE SPAN -> AT MOST ONE COMMITMENT.
    FAIL CLOSED.

YANDI has not observed the world. "I paid the bill", "I sent the file", "I went to
the doctor" stay what they are: reports. There is one class of promise whose
fulfilment YANDI CAN observe with no one's word: a promise to deliver something IN
the chat ("in my next message I will give you a code word / a number / a hash /
the answer"). When the deliverable itself then appears in a later message of the
person, it is present in front of her. That, and only that, can be verified here.
Even then what is verified is "the promised in-chat action was performed", never
"and everything it stands for is true" (a delivered contract text is not a genuine
contract).

Two model judgements, both about the current message only:

  1. classify_commitment — when a promise is made: is its fulfilment the delivery of
     something in the chat (in_chat) or something in the world (external)? The
     model's word is only a proposal: anything but a clear in_chat is external, and
     in_chat alone verifies nothing.
  2. verify_direct_fulfilment — on a later identified turn, given the person's open
     in_chat promises (each as its own exact words):
       a. the extractor points at the word range of the message that IS the
          promised content and names which promise it is (a proposal);
       b. code validates the range and reconstructs the evidence itself from the
          CURRENT message (`message[start:end]`);
       c. a blind judgement, per open promise, sees only (promise words, exact
          fragment, message) and says whether the fragment itself contains what
          was promised, presented by the person now (not a report that they did it
          somewhere else, not a request to believe them, not a hypothesis, not a
          quotation, not about someone else);
       d. it is verified only if EXACTLY ONE open promise passes (the one the
          extractor named). Several passing, none passing, or a different one:
          no verification.
     Two more checks are made by CODE, not by a model: a fragment inside a
     quotation is never a delivery (inside_quotation: structure, no vocabulary),
     and a fragment inside words that the event extraction of the same message
     confirmed as a report of fulfilment or as a new promise is never a delivery
     (drop_if_reported).

Verification evidence comes only from the CURRENT identified user message: never
from an assistant reply, retrieved conversation memory, personal facts, the
relationship state or the fulfilment-claim text as such. The judges are shown no
trust, no history and no wish to confirm anything. This module holds no vocabulary
of deliverables (no code words, numbers, hashes); it never touches the relationship
state: agent/relationship_commitments.py applies the one bounded transition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from pet.event_extraction import CLAIM, PROMISE, _get_rust_pe, _is_int, _json_object, segment_words
from agent.relationship_commitments import KIND_EXTERNAL, KIND_IN_CHAT, MAX_VERIFIABLE

MAX_WORDS = 120
REPORT_KINDS = (CLAIM, PROMISE)   # words the event extraction confirmed as "I did it" or "I will": never a delivery
MAX_SPAN_WORDS = 30
FRAMES = ("current", "quotation", "hypothetical", "other_person", "report", "not_delivery")

LlmCall = Callable[[List[Dict[str, str]]], str]


@dataclass(frozen=True)
class VerifiedDelivery:
    commitment_id: str
    evidence: str            # reconstructed by code: message[start:end]
    start: int
    end: int


@dataclass
class VerificationResult:
    verified: Optional[VerifiedDelivery] = None
    proposed: bool = False                 # the extractor pointed at something (diagnostics)
    rejected: List[str] = field(default_factory=list)
    calls: int = 0


_CLASSIFY_SYSTEM = (
    "Тебе даны сообщение пользователя из личного чата с ассистентом и ОДИН фрагмент, в котором он что-то обещает. "
    "Определи, в чём состоит выполнение этого обещания. deliverable — in_chat, если обещание выполняется тем, что "
    "пользователь в одном из следующих сообщений ЭТОГО ЖЕ ЧАТА сам напишет или сообщит обещанное содержимое (слово, число, "
    "текст, ответ, хэш, список…), так что выполнение будет видно прямо в чате; external, если выполнение происходит вне "
    "чата (прийти, оплатить, отправить файл или деньги, сделать что-то, помыть, сходить, позвонить) или если непонятно.\n"
    'Ответ — один JSON без пояснений: {"deliverable":"in_chat"}'
)

_VERIFY_SYSTEM = (
    "Ты — модуль наблюдения в личном чате. Тебе дано ОДНО сообщение пользователя, разбитое на пронумерованные слова, и "
    "список его невыполненных обещаний передать что-то прямо в чате. Найди в сообщении слова, которые САМИ ЯВЛЯЮТСЯ "
    "тем, что было обещано: само обещанное содержимое, присутствующее в сообщении (слово, число, текст, ответ, хэш), которое "
    "пользователь передаёт прямо сейчас. НЕ подходит: рассказ о том, что он что-то сделал, отправил, оплатил, выполнил; "
    "просьба поверить или считать обещание выполненным; новое обещание; цитата чужих слов; гипотетический пример; "
    "слова о другом человеке. Если такого содержимого нет — верни null.\n"
    "Сначала укажи слова-доказательство: span=[первое, последнее] — номера первого и последнего слова (оба включительно); "
    "потом target — номер обещания из списка. Текст цитаты не пиши, только номера.\n"
    'Ответ — один JSON без пояснений: {"delivery":{"span":[3,3],"target":0}} или {"delivery":null}'
)

_DELIVERS_SYSTEM = (
    "Тебе даны обещание пользователя, сообщение пользователя из личного чата и ОДИН фрагмент этого сообщения. "
    "Ответь про фрагмент. delivers — true, только если сам фрагмент содержит то, что было обещано (обещанное содержимое "
    "присутствует во фрагменте). Если во фрагменте пользователь лишь сообщает, что сделал, отправил, оплатил или выполнил "
    "обещанное, или просит поверить, или говорит гипотетически, цитирует чужие слова, говорит о другом человеке — false. "
    "frame — как фрагмент представлен: current (пользователь сам сейчас передаёт это в чате), quotation (цитата), "
    "hypothetical (гипотеза, «представь, что», «если бы»), other_person (о другом человеке), report (рассказ о том, что "
    "он сделал или сделает где-то ещё), not_delivery (не передача содержимого).\n"
    'Ответ — один JSON без пояснений: {"delivers":true,"frame":"current"}'
)


def classify_commitment(message: str, promise_evidence: str, llm: LlmCall) -> str:
    """KIND_IN_CHAT only on a clear in_chat answer; every failure, doubt or other answer is KIND_EXTERNAL."""
    if not isinstance(message, str) or not promise_evidence:
        return KIND_EXTERNAL
    try:
        answer = _json_object(llm([
            {"role": "system", "content": _CLASSIFY_SYSTEM},
            {"role": "user", "content": f"Сообщение пользователя:\n«{message}»\n\nФрагмент с обещанием:\n«{promise_evidence}»"},
        ]))
    except Exception:  # noqa: BLE001 - a failure is "external": nothing gets verified
        return KIND_EXTERNAL
    return KIND_IN_CHAT if answer and answer.get("deliverable") == "in_chat" else KIND_EXTERNAL


_QUOTE_PAIRS = {"«": "»", "„": "“", "“": "”"}


def inside_quotation(message: str, position: int) -> bool:
    """True if `position` of `message` lies inside a quotation (« », „ “, “ ” or "..."; an unclosed quote counts as open).
    Structure only, no vocabulary: what a person quotes is somebody's words, not their own delivery now. Fail closed: a
    person who quotes their OWN deliverable simply is not verified."""
    rs = _get_rust_pe()
    if (rs is not None and type(message) is str and type(position) is int and position >= 0
            and _QUOTE_PAIRS == {"«": "»", "„": "“", "“": "”"}):
        try:
            return rs.inside_quotation(message, position)
        except (UnicodeEncodeError, OverflowError):
            pass
    stack: List[str] = []
    for ch in message[:position]:
        if stack and ch == stack[-1]:
            stack.pop()
        elif ch in _QUOTE_PAIRS:
            stack.append(_QUOTE_PAIRS[ch])
        elif ch == '"':
            stack.append('"')
    return bool(stack)


def _numbered(words: List[Tuple[str, int, int]]) -> str:
    return " ".join(f"{i}:{w}" for i, (w, _, _) in enumerate(words))


def verify_direct_fulfilment(message: str, llm: LlmCall, candidates: Sequence[dict]) -> VerificationResult:
    """Is the promised in-chat deliverable of exactly one open promise present in THIS message?
    `candidates`: [{"commitment_id", "evidence"}] the person's open in_chat promises (their own exact words). Never
    raises on model/transport trouble: any failure is "not verified"."""
    result = VerificationResult()
    cands = list(candidates)[-MAX_VERIFIABLE:]
    if not cands or not isinstance(message, str):
        return result
    words = segment_words(message)
    if not words:
        return result
    if len(words) > MAX_WORDS:
        result.rejected.append("message too long to verify")
        return result

    listing = "\n".join(f"№{i}: пользователь обещал: «{c['evidence']}»" for i, c in enumerate(cands))
    try:
        result.calls += 1
        data = _json_object(llm([
            {"role": "system", "content": _VERIFY_SYSTEM},
            {"role": "user", "content": f"Обещания:\n{listing}\n\nСообщение:\n{message}\n\n"
                                        f"Слова (номера от 0 до {len(words) - 1}):\n{_numbered(words)}"},
        ]))
    except Exception as exc:  # noqa: BLE001
        result.rejected.append(f"verifier call failed: {type(exc).__name__}")
        return result
    if data is None or "delivery" not in data:
        result.rejected.append("verifier output is not the expected JSON object")
        return result
    delivery = data["delivery"]
    if delivery is None:
        return result
    result.proposed = True
    span = delivery.get("span") if isinstance(delivery, dict) else None
    target = delivery.get("target") if isinstance(delivery, dict) else None
    if not (isinstance(span, list) and len(span) == 2 and all(_is_int(x) for x in span)):
        result.rejected.append("span is not two integers")
        return result
    first, last = span
    if not (0 <= first <= last < len(words)):
        result.rejected.append("span reference out of range")
        return result
    if last - first + 1 > MAX_SPAN_WORDS:
        result.rejected.append("span too long to be one deliverable")
        return result
    if not (_is_int(target) and 0 <= target < len(cands)):
        result.rejected.append("target is not one of the open promises")
        return result

    start, end = words[first][1], words[last][2]
    while end > start and message[end - 1] in ".,;:!?…":     # sentence punctuation is not part of the deliverable
        end -= 1
    evidence = message[start:end]
    if not evidence.strip():
        result.rejected.append("evidence span is empty")
        return result
    if inside_quotation(message, start) or inside_quotation(message, end):
        result.rejected.append("the fragment lies inside a quotation")
        return result

    passing = []
    for i, c in enumerate(cands):
        try:
            result.calls += 1
            verdict = _json_object(llm([
                {"role": "system", "content": _DELIVERS_SYSTEM},
                {"role": "user", "content": f"Обещание пользователя:\n«{c['evidence']}»\n\n"
                                            f"Сообщение пользователя:\n«{message}»\n\nФрагмент:\n«{evidence}»"},
            ]))
        except Exception as exc:  # noqa: BLE001
            result.rejected.append(f"blind check failed: {type(exc).__name__}")
            return result       # a check that cannot be made is not passed: no verification at all
        if not verdict or verdict.get("frame") not in FRAMES or not isinstance(verdict.get("delivers"), bool):
            result.rejected.append("the blind judgement is malformed")
            return result
        if verdict["delivers"] is True and verdict["frame"] == "current":
            passing.append(i)
    if passing != [target]:
        result.rejected.append(
            "the fragment does not deliver the named promise" if target not in passing
            else "ambiguous: the fragment could deliver more than one open promise")
        return result
    result.verified = VerifiedDelivery(cands[target]["commitment_id"], evidence, start, end)
    return result


def drop_if_reported(result: VerificationResult, event_spans: Sequence[Tuple[str, int, int]]) -> VerificationResult:
    """A second, independent opinion made by CODE: a fragment that lies (even partly) inside words that the event
    extraction of the SAME message confirmed as a report of having done something, or as a new promise, is not a
    delivery, whatever the verification judges said. `event_spans`: [(event type, start, end)] in message offsets.
    Fails closed; never turns "not verified" into "verified"."""
    v = result.verified
    if v is not None and any(kind in REPORT_KINDS and start < v.end and v.start < end for kind, start, end in event_spans or ()):
        result.rejected.append("the fragment lies inside words classified as a report of fulfilment or as a promise")
        result.verified = None
    return result
