//! Распознавание событий отношений в ОДНОМ сообщении (оскорбление / извинение / обещание / заявление о выполнении) — перенос `pet/event_extraction.py`.
//! Модель НЕ решает, что произошло: она лишь указывает НОМЕРА слов-доказательства, текст цитаты код восстанавливает сам, а независимая «слепая» проверка фрагмента
//! (без названного типа) подтверждает действие. Любой сбой модели/транспорта = «события нет»; обещание принимается только как ЕДИНСТВЕННЫЙ кандидат хода.
use serde_json::{json, Map, Value};
use yandi_rs::pet_extraction::{json_text, segment_words};
use yandi_rs::py_json::{loads, PyJson};
use yandi_rs::py_text::py_strip;

pub const INSULT: &str = "insult";
pub const APOLOGY: &str = "apology";
pub const PROMISE: &str = "promise";
pub const CLAIM: &str = "fulfilment_claim";
pub const EVENT_TYPES: [&str; 4] = [INSULT, APOLOGY, PROMISE, CLAIM];
pub const MAX_WORDS: usize = 120;
pub const MAX_SPAN_WORDS: i64 = 30;
pub const MAX_EVENTS: usize = 3;
pub const EVIDENCE_MIN_CHARS: usize = 3;
pub const ACCEPTED_FRAME: &str = "current";

pub const EXTRACT_SYSTEM: &str = include_str!("event_extract_system.txt");
pub const CHECK_SYSTEM: &str = include_str!("event_check_system.txt");

/// Вызов модели: список (роль, текст) → сырой текст ответа; `Err(имя класса исключения)` — сбой транспорта.
pub type LlmCall<'a> = &'a dyn Fn(&[(String, String)]) -> Result<String, String>;

#[derive(Debug, Clone, PartialEq)]
pub struct ExtractedEvent {
    pub kind: String,
    pub evidence: String,
    pub start: usize,
    pub end: usize,
    pub severity: f64,
    pub sincerity: f64,
}

#[derive(Debug, Clone, Default)]
pub struct ExtractionResult {
    pub events: Vec<ExtractedEvent>,
    pub candidates: usize,
    pub proposed: Vec<Value>,
    pub validated: Vec<String>,
    pub rejected: Vec<String>,
    pub calls: usize,
    pub answered: bool,
    pub judged: Vec<(String, usize, usize)>,
}

impl ExtractionResult {
    pub fn to_json(&self) -> Value {
        json!({
            "events": self.events.iter().map(|e| json!({"type": e.kind, "evidence": e.evidence, "start": e.start, "end": e.end, "severity": e.severity, "sincerity": e.sincerity})).collect::<Vec<_>>(),
            "candidates": self.candidates, "proposed": self.proposed, "validated": self.validated, "rejected": self.rejected, "calls": self.calls, "answered": self.answered,
            "judged": self.judged.iter().map(|(k, s, e)| json!([k, s, e])).collect::<Vec<_>>(),
        })
    }
}

/// Результат разбора для домена жизненного цикла обиды (`agent.message_intensity.IntensityResult`).
#[derive(Debug, Clone, PartialEq)]
pub struct IntensityResult {
    pub ok: bool,
    pub is_insult: bool,
    pub is_apology: bool,
    pub severity: f64,
    pub sincerity: f64,
    pub error: String,
    pub is_promise: bool,
    pub claims_fulfilled: bool,
    pub evidence: String,
    pub spans: Vec<(String, usize, usize)>,
}

impl IntensityResult {
    pub fn to_json(&self) -> Value {
        json!({"ok": self.ok, "is_insult": self.is_insult, "is_apology": self.is_apology, "severity": self.severity, "sincerity": self.sincerity, "error": self.error,
               "is_promise": self.is_promise, "claims_fulfilled": self.claims_fulfilled, "evidence": self.evidence, "spans": self.spans.iter().map(|(k, s, e)| json!([k, s, e])).collect::<Vec<_>>()})
    }
}

/// PyJson → serde_json (для отчёта): целые как целые.
pub fn to_value(j: &PyJson) -> Value {
    match j {
        PyJson::Null => Value::Null,
        PyJson::Bool(b) => json!(b),
        PyJson::Int { as_f64, .. } => {
            if as_f64.fract() == 0.0 && as_f64.abs() < 9.0e15 {
                json!(*as_f64 as i64)
            } else {
                json!(as_f64)
            }
        }
        PyJson::Float(f) => serde_json::Number::from_f64(*f).map(Value::Number).unwrap_or(Value::Null),
        PyJson::Str(s) => json!(s),
        PyJson::List(l) => Value::Array(l.iter().map(to_value).collect()),
        PyJson::Dict(d) => {
            let mut m = Map::new();
            for (k, v) in d {
                m.insert(k.clone(), to_value(v));
            }
            Value::Object(m)
        }
    }
}

fn numbered(words: &[(String, usize, usize)]) -> String {
    words.iter().enumerate().map(|(i, (w, _, _))| format!("{i}:{w}")).collect::<Vec<_>>().join(" ")
}

/// Строгий разбор JSON-объекта; единственная снисходительность — обрамляющий блок кода. `Err` — то, что в Python пролетело бы мимо `except` (RecursionError).
fn json_object(raw: &str) -> Result<Option<PyJson>, String> {
    let text = json_text(raw);
    match loads(&text) {
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

fn unit(v: Option<&PyJson>) -> Option<f64> {
    let x = match v? {
        PyJson::Int { as_f64, .. } => *as_f64,
        PyJson::Float(f) => *f,
        _ => return None,
    };
    if (0.0..=1.0).contains(&x) {
        Some(x)
    } else {
        None
    }
}

type Cand = (String, i64, i64, f64, f64);

/// `((тип, первое слово, последнее слово, тяжесть, искренность), "")` либо `(None, причина)`.
fn validate_candidate(item: &PyJson, n_words: usize) -> Result<Cand, &'static str> {
    if !item.is_dict() {
        return Err("candidate is not an object");
    }
    let kind = match item.get("type") {
        Some(PyJson::Str(s)) if EVENT_TYPES.contains(&s.as_str()) => s.clone(),
        _ => return Err("unknown event type"),
    };
    let (first, last) = match item.get("span") {
        Some(PyJson::List(l)) if l.len() == 2 && l.iter().all(|x| int_of(x).is_some()) => (int_of(&l[0]).unwrap(), int_of(&l[1]).unwrap()),
        _ => return Err("span is not two integers"),
    };
    if !(0 <= first && first <= last && last < n_words as i64) {
        return Err("span reference out of range");
    }
    if last - first + 1 > MAX_SPAN_WORDS {
        return Err("span too long to be one event");
    }
    let (mut severity, mut sincerity) = (0.0, 0.0);
    if kind == INSULT {
        severity = unit(item.get("severity")).ok_or("insult without a severity in 0..1")?;
    }
    if kind == APOLOGY {
        sincerity = unit(item.get("sincerity")).ok_or("apology without a sincerity in 0..1")?;
    }
    Ok((kind, first, last, severity, sincerity))
}

fn check_messages(message: &str, evidence: &str) -> Vec<(String, String)> {
    let user = format!("Сообщение пользователя:\n«{message}»\n\nФрагмент:\n«{evidence}»");
    vec![("system".into(), CHECK_SYSTEM.into()), ("user".into(), user)]
}

fn char_slice(s: &str, a: usize, b: usize) -> String {
    s.chars().skip(a).take(b.saturating_sub(a)).collect()
}

/// Распознать события отношений, которые пользователь совершает в ЭТОМ сообщении. Сбои модели/транспорта не бросаются: «события нет».
pub fn extract_relational_events(message: &str, llm: LlmCall) -> Result<ExtractionResult, String> {
    let mut result = ExtractionResult::default();
    let words = segment_words(message);
    if words.is_empty() {
        return Ok(result);
    }
    if words.len() > MAX_WORDS {
        result.rejected.push("message too long for event extraction".into());
        return Ok(result);
    }
    result.calls += 1;
    let user = format!("Сообщение:\n{message}\n\nСлова (номера от 0 до {}):\n{}", words.len() - 1, numbered(&words));
    let raw = match llm(&[("system".into(), EXTRACT_SYSTEM.into()), ("user".into(), user)]) {
        Ok(r) => r,
        Err(class) => {
            result.rejected.push(format!("extractor call failed: {class}"));
            return Ok(result);
        }
    };
    let data = json_object(&raw)?;
    let events = data.as_ref().and_then(|d| d.get("events"));
    let Some(PyJson::List(proposed)) = events else {
        result.rejected.push("extractor output is not the expected JSON object".into());
        return Ok(result);
    };
    result.answered = true;
    if proposed.len() > MAX_EVENTS {
        result.rejected.push("extractor proposed too many events".into());
        return Ok(result);
    }
    result.candidates = proposed.len();
    result.proposed = proposed.iter().filter(|i| i.is_dict()).map(|i| i.get("type").map(to_value).unwrap_or(Value::Null)).collect();

    let mut valid: Vec<Cand> = Vec::new();
    for item in proposed {
        match validate_candidate(item, words.len()) {
            Ok(c) => {
                result.validated.push(c.0.clone());
                valid.push(c);
            }
            Err(reason) => result.rejected.push(reason.into()),
        }
    }
    // пересекающиеся отрезки разных событий неоднозначны: одно событие не должно заверять другое
    let mut clean: Vec<Cand> = Vec::new();
    for (i, cand) in valid.iter().enumerate() {
        let overlaps = valid.iter().enumerate().any(|(j, o)| j != i && !(cand.2 < o.1 || o.2 < cand.1));
        if overlaps {
            result.rejected.push("overlapping evidence spans are ambiguous".into());
        } else {
            clean.push(cand.clone());
        }
    }
    for (kind, first, last, severity, sincerity) in clean {
        let (start, end) = (words[first as usize].1, words[last as usize].2);
        let evidence = char_slice(message, start, end);
        if py_strip(&evidence).chars().count() < EVIDENCE_MIN_CHARS {
            result.rejected.push("evidence span too short".into());
            continue;
        }
        result.calls += 1;
        let verdict = match llm(&check_messages(message, &evidence)) {
            Ok(r) => json_object(&r)?,
            Err(class) => {
                result.rejected.push(format!("check call failed: {class}"));
                continue;
            }
        };
        let empty = |v: &PyJson| matches!(v, PyJson::Dict(d) if d.is_empty());
        let Some(v) = verdict.filter(|v| !empty(v)) else {
            result.rejected.push(format!("{kind}: the blind judgement of the span disagrees"));
            continue;
        };
        if !matches!(v.get("act"), Some(PyJson::Str(a)) if *a == kind) {
            result.rejected.push(format!("{kind}: the blind judgement of the span disagrees"));
            continue;
        }
        if !matches!(v.get("frame"), Some(PyJson::Str(f)) if f == ACCEPTED_FRAME) {
            result.rejected.push(format!("{kind}: the span is not an act performed now by the user"));
            continue;
        }
        result.judged.push((kind.clone(), start, end));
        result.events.push(ExtractedEvent { kind, evidence, start, end, severity, sincerity });
    }
    // обещание/заявление допустимы ТОЛЬКО как единственный кандидат хода: обещание — именно то событие, которое нельзя угадывать
    let is_commitment = |k: &str| k == PROMISE || k == CLAIM;
    if result.candidates != 1 && result.events.iter().any(|e| is_commitment(&e.kind)) {
        result.events.retain(|e| !is_commitment(&e.kind));
        result.rejected.push("commitment event dropped: it was not the only candidate proposed".into());
    }
    Ok(result)
}

/// Перенос проверенных событий в доменный объект жизненного цикла: обещание принимается только как единственное событие хода; обещание вместе с заявлением противоречиво.
pub fn to_intensity(result: &ExtractionResult) -> IntensityResult {
    let mut events = result.events.clone();
    let is_commitment = |k: &str| k == PROMISE || k == CLAIM;
    let n_commit = events.iter().filter(|e| is_commitment(&e.kind)).count();
    if n_commit > 0 && (events.len() > n_commit || n_commit > 1) {
        events.retain(|e| !is_commitment(&e.kind));
    }
    let insult = events.iter().find(|e| e.kind == INSULT);
    let apology = events.iter().find(|e| e.kind == APOLOGY);
    let lone = if events.len() == 1 && is_commitment(&events[0].kind) { Some(&events[0]) } else { None };
    IntensityResult {
        ok: true,
        is_insult: insult.is_some(),
        is_apology: apology.is_some(),
        severity: insult.map(|e| e.severity).unwrap_or(0.0),
        sincerity: apology.map(|e| e.sincerity).unwrap_or(0.0),
        error: String::new(),
        is_promise: lone.map(|e| e.kind == PROMISE).unwrap_or(false),
        claims_fulfilled: lone.map(|e| e.kind == CLAIM).unwrap_or(false),
        evidence: match lone {
            Some(e) => e.evidence.clone(),
            None => events.first().map(|e| e.evidence.clone()).unwrap_or_default(),
        },
        spans: events.iter().map(|e| (e.kind.clone(), e.start, e.end)).collect(),
    }
}
