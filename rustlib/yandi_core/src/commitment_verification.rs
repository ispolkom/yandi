//! Проверка НАПРЯМУЮ НАБЛЮДАЕМОГО выполнения — перенос `pet/commitment_verification.py`. «Пользователь сообщил о выполнении» ≠ «выполнение проверено». Проверяется только обещание
//! передать что-то В ЧАТЕ: когда передаваемое само появляется в позднейшем сообщении, оно перед глазами. Модель МОЖЕТ указать на доказательство, но НЕ МОЖЕТ его выдумать; код проверяет
//! отрезок и сам восстанавливает текст; слепое суждение по каждому открытому обещанию; проверено только если подходит РОВНО ОДНО (названное). Цитата и слова, подтверждённые как
//! «я сделал»/новое обещание, доставкой не считаются (структура, не словарь). Любой сбой — «не проверено».
use serde_json::{json, Value};
use yandi_rs::pet_extraction::{inside_quotation, json_text, segment_words};
use yandi_rs::py_json::{loads, PyJson};
use yandi_rs::py_text::py_strip;

use crate::event_extraction::LlmCall;
use crate::relationship_commitments::{KIND_EXTERNAL, KIND_IN_CHAT, MAX_VERIFIABLE};

pub const MAX_WORDS: usize = 120;
pub const MAX_SPAN_WORDS: i64 = 30;
pub const FRAMES: [&str; 6] = ["current", "quotation", "hypothetical", "other_person", "report", "not_delivery"];
pub const REPORT_KINDS: [&str; 2] = ["fulfilment_claim", "promise"];

const CLASSIFY_SYSTEM: &str = include_str!("commit_classify_system.txt");
const VERIFY_SYSTEM: &str = include_str!("commit_verify_system.txt");
const DELIVERS_SYSTEM: &str = include_str!("commit_delivers_system.txt");

#[derive(Debug, Clone, PartialEq)]
pub struct VerifiedDelivery {
    pub commitment_id: String,
    pub evidence: String,
    pub start: usize,
    pub end: usize,
}

#[derive(Debug, Clone, Default)]
pub struct VerificationResult {
    pub verified: Option<VerifiedDelivery>,
    pub proposed: bool,
    pub rejected: Vec<String>,
    pub calls: usize,
}

impl VerificationResult {
    pub fn to_json(&self) -> Value {
        json!({"verified": self.verified.as_ref().map(|v| json!({"commitment_id": v.commitment_id, "evidence": v.evidence, "start": v.start, "end": v.end})),
               "proposed": self.proposed, "rejected": self.rejected, "calls": self.calls})
    }
}

fn json_object(raw: &str) -> Result<Option<PyJson>, String> {
    match loads(&json_text(raw)) {
        Ok(v) if v.is_dict() => Ok(Some(v)),
        Ok(_) => Ok(None),
        Err(yandi_rs::py_json::LoadsError::Escalate(_)) => Err("RecursionError".into()),
        Err(_) => Ok(None),
    }
}

fn int_of(v: &PyJson) -> Option<i64> {
    match v {
        PyJson::Int { as_f64, .. } => Some(*as_f64 as i64),
        _ => None,
    }
}

fn char_slice(s: &str, a: usize, b: usize) -> String {
    s.chars().skip(a).take(b.saturating_sub(a)).collect()
}

fn non_empty(v: Option<PyJson>) -> Option<PyJson> {
    v.filter(|d| !matches!(d, PyJson::Dict(x) if x.is_empty()))
}

/// `KIND_IN_CHAT` только на ясный ответ in_chat; любой сбой, сомнение или иной ответ — `KIND_EXTERNAL`.
pub fn classify_commitment(message: &str, promise_evidence: &str, llm: LlmCall) -> Result<&'static str, String> {
    if promise_evidence.is_empty() {
        return Ok(KIND_EXTERNAL);
    }
    let user = format!("Сообщение пользователя:\n«{message}»\n\nФрагмент с обещанием:\n«{promise_evidence}»");
    let raw = match llm(&[("system".into(), CLASSIFY_SYSTEM.into()), ("user".into(), user)]) {
        Ok(r) => r,
        Err(_) => return Ok(KIND_EXTERNAL),
    };
    let answer = non_empty(json_object(&raw)?);
    Ok(if matches!(answer.as_ref().and_then(|a| a.get("deliverable")), Some(PyJson::Str(s)) if s == "in_chat") { KIND_IN_CHAT } else { KIND_EXTERNAL })
}

/// Присутствует ли обещанное содержимое РОВНО ОДНОГО открытого обещания в ЭТОМ сообщении. `candidates`: (commitment_id, evidence) открытых обещаний `in_chat`.
pub fn verify_direct_fulfilment(message: &str, llm: LlmCall, candidates: &[(String, String)]) -> Result<VerificationResult, String> {
    let mut result = VerificationResult::default();
    let skip = candidates.len().saturating_sub(MAX_VERIFIABLE);
    let cands: Vec<&(String, String)> = candidates.iter().skip(skip).collect();
    if cands.is_empty() {
        return Ok(result);
    }
    let words = segment_words(message);
    if words.is_empty() {
        return Ok(result);
    }
    if words.len() > MAX_WORDS {
        result.rejected.push("message too long to verify".into());
        return Ok(result);
    }
    let listing = cands.iter().enumerate().map(|(i, c)| format!("№{i}: пользователь обещал: «{}»", c.1)).collect::<Vec<_>>().join("\n");
    let numbered = words.iter().enumerate().map(|(i, (w, _, _))| format!("{i}:{w}")).collect::<Vec<_>>().join(" ");
    result.calls += 1;
    let user = format!("Обещания:\n{listing}\n\nСообщение:\n{message}\n\nСлова (номера от 0 до {}):\n{numbered}", words.len() - 1);
    let raw = match llm(&[("system".into(), VERIFY_SYSTEM.into()), ("user".into(), user)]) {
        Ok(r) => r,
        Err(class) => {
            result.rejected.push(format!("verifier call failed: {class}"));
            return Ok(result);
        }
    };
    let data = json_object(&raw)?;
    let Some(delivery) = data.as_ref().and_then(|d| d.get("delivery")) else {
        result.rejected.push("verifier output is not the expected JSON object".into());
        return Ok(result);
    };
    if matches!(delivery, PyJson::Null) {
        return Ok(result);
    }
    result.proposed = true;
    let (span, target) = if delivery.is_dict() { (delivery.get("span"), delivery.get("target")) } else { (None, None) };
    let (first, last) = match span {
        Some(PyJson::List(l)) if l.len() == 2 && l.iter().all(|x| int_of(x).is_some()) => (int_of(&l[0]).unwrap(), int_of(&l[1]).unwrap()),
        _ => {
            result.rejected.push("span is not two integers".into());
            return Ok(result);
        }
    };
    if !(0 <= first && first <= last && last < words.len() as i64) {
        result.rejected.push("span reference out of range".into());
        return Ok(result);
    }
    if last - first + 1 > MAX_SPAN_WORDS {
        result.rejected.push("span too long to be one deliverable".into());
        return Ok(result);
    }
    let target = match target.and_then(int_of) {
        Some(t) if 0 <= t && (t as usize) < cands.len() => t as usize,
        _ => {
            result.rejected.push("target is not one of the open promises".into());
            return Ok(result);
        }
    };
    let start = words[first as usize].1;
    let mut end = words[last as usize].2;
    let chars: Vec<char> = message.chars().collect();
    while end > start && ".,;:!?…".contains(chars[end - 1]) {
        end -= 1;
    }
    let evidence = char_slice(message, start, end);
    if py_strip(&evidence).is_empty() {
        result.rejected.push("evidence span is empty".into());
        return Ok(result);
    }
    if inside_quotation(message, start) || inside_quotation(message, end) {
        result.rejected.push("the fragment lies inside a quotation".into());
        return Ok(result);
    }
    let mut passing: Vec<usize> = Vec::new();
    for (i, c) in cands.iter().enumerate() {
        result.calls += 1;
        let user = format!("Обещание пользователя:\n«{}»\n\nСообщение пользователя:\n«{message}»\n\nФрагмент:\n«{evidence}»", c.1);
        let verdict = match llm(&[("system".into(), DELIVERS_SYSTEM.into()), ("user".into(), user)]) {
            Ok(r) => non_empty(json_object(&r)?),
            Err(class) => {
                result.rejected.push(format!("blind check failed: {class}"));
                return Ok(result); // проверка, которую нельзя сделать, не пройдена: проверки нет вовсе
            }
        };
        let frame_ok = |v: &PyJson| matches!(v.get("frame"), Some(PyJson::Str(f)) if FRAMES.contains(&f.as_str())) && matches!(v.get("delivers"), Some(PyJson::Bool(_)));
        let Some(v) = verdict.filter(frame_ok) else {
            result.rejected.push("the blind judgement is malformed".into());
            return Ok(result);
        };
        if matches!(v.get("delivers"), Some(PyJson::Bool(true))) && matches!(v.get("frame"), Some(PyJson::Str(f)) if f == "current") {
            passing.push(i);
        }
    }
    if passing != vec![target] {
        result.rejected.push(if !passing.contains(&target) { "the fragment does not deliver the named promise" } else { "ambiguous: the fragment could deliver more than one open promise" }.into());
        return Ok(result);
    }
    result.verified = Some(VerifiedDelivery { commitment_id: cands[target].0.clone(), evidence, start, end });
    Ok(result)
}

/// Второе, независимое мнение КОДА: отрезок, лежащий (даже частично) внутри слов, которые извлечение событий того же сообщения подтвердило как «я сделал» или новое обещание, доставкой не является.
pub fn drop_if_reported(mut result: VerificationResult, event_spans: &[(String, usize, usize)]) -> VerificationResult {
    if let Some(v) = &result.verified {
        if event_spans.iter().any(|(kind, s, e)| REPORT_KINDS.contains(&kind.as_str()) && *s < v.end && v.start < *e) {
            result.rejected.push("the fragment lies inside words classified as a report of fulfilment or as a promise".into());
            result.verified = None;
        }
    }
    result
}
