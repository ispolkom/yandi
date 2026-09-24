//! Перенос agent/intent_router.py — определение типа запроса (detect_intent) по таблице
//! regex-паттернов + справочные функции (should_use_rag, get_intent_action, get_intent_description,
//! get_intent_explanation). Вызывается из orchestrator/pre_pipeline.py на КАЖДЫЙ запрос.
//!
//! Fidelity:
//! * Порядок типов и паттернов в таблице — как в Python dict/list (Vec, не HashMap): при равной
//!   уверенности выигрывает ПЕРВЫЙ (строгое `confidence > best_confidence`).
//! * confidence = min(1.0, 0.5 + len(pattern)/100): len — число СИМВОЛОВ исходной строки паттерна
//!   (включая служебные `?$`), не байтов.
//! * Ветка "unknown": `len(q) < 10` — символы, не байты (кириллица 2 байта/символ).
//! * `q = query.lower().strip()` — Python strip (py_strip: пробелы включают U+001C..1F).
//! * Паттерны применяются с `re.IGNORECASE` к уже приведённой к нижнему регистру строке; здесь
//!   `(?i)` + тот же lower(). Паттерны не используют `\s`/`\w`/`\b` (проверено).
//! * Пустой запрос ("" / None) обрабатывает Python-обёртка ДО вызова Rust (truthiness).
//!
//! Статус (2026-09-24): построено и проверено на параллельность с Python; в бою по умолчанию
//! ВЫКЛЮЧЕНО — переключатель YANDI_INTENT_ROUTER_ENGINE=rust (см. agent/intent_router.py).

use crate::py_text::PyLowerExt;
use once_cell::sync::Lazy;
use pyo3::prelude::*;
use regex::Regex;

struct Intent {
    name: &'static str,
    patterns: &'static [&'static str],
    action: &'static str,
    description: &'static str,
}

// agent/intent_router.py::INTENT_PATTERNS — порядок сохранён.
static INTENTS: &[Intent] = &[
    Intent {
        name: "social_dialog",
        patterns: &[
            "замуж", "жениться", "выйти за", "выйдешь",
            "предложение руки", "heart", "люблю", "любовь",
            "как дела", "что нового", "привет", "здравствуй",
            "как жизнь", "как настроение", "как ты",
            "рада тебя видеть", "скучала", "хорошо",
            "плохо", "грустно", "весело",
        ],
        action: "conversation",
        description: "социальный диалог, личные вопросы",
    },
    Intent {
        name: "subjective_interpretation",
        patterns: &[
            "твоё мнение", "как ты считаешь", "что ты думаешь",
            "твой взгляд", "твоё видение", "твоя интерпретация",
            "как ты понимаешь", "что для тебя", "с твоей точки зрения",
            "ты чувствуешь", "ты ощущаешь", "твоё отношение",
            "как бы ты", "что бы ты сказал", "твоя позиция",
            "я хочу знать твоё мнение",
            "анализ песни", "смысл песни", "интерпретация песни",
            "о чём песня", "идея песни", "посыл песни",
            "твой анализ", "твоя логика", "как ты понимаешь эту песню",
            "разбор песни", "глубинный смысл", "что хотел сказать",
            "о чём эта песня", "в чём суть песни", "логика песни",
            "смысл текста", "идея текста", "посыл текста",
            "что значит песня", "как понять песню",
        ],
        action: "personal_opinion",
        description: "анализ, интерпретация, мнение",
    },
    Intent {
        name: "objective_information",
        patterns: &[
            "сколько", "когда", "где", "кто такой", "что такое",
            "определение", "факты?$", "напиши", "расскажи о",
            "объясни", "как работает", "почему происходит",
            "информация о", "данные по", "статистика",
            "история", "биография", "википедия",
            "как установить", "как настроить", "инструкция",
            "найди", "поищи", "найти", "поиск",
        ],
        action: "search_and_fact",
        description: "фактический вопрос, ответ из источников",
    },
    Intent {
        name: "self_reflection",
        patterns: &[
            "кто ты", "расскажи о себе", "какая ты",
            "твоя суть", "твоё предназначение", "зачем ты",
            "как ты устроена", "что ты чувствуешь",
            "ты боишься", "ты рада", "ты обиделась",
            "почему ты так отвечаешь", "что с тобой",
            "ты меняешься", "ты развиваешься",
            "ты женщина", "ты девушка",
            "первая цифровая", "цифровая личность",
            "если бы ты была", "ты бы хотела",
            "чего бы тебе хотелось", "чего ты хочешь",
            "твои чувства", "твой характер",
            "что ты чувствуешь", "какая ты",
            "опиши себя", "твоё состояние", "как ты себя",
            "YANDI", "Янди", "ты цифровая",
        ],
        action: "look_inside",
        description: "вопрос о себе, своих мыслях, чувствах",
    },
    Intent {
        name: "creative_reasoning",
        patterns: &[
            "придумай", "представь", "вообрази",
            "что если", "а что будет", "давай пофантазируем",
            "творческое задание", "напиши стих", "расскажи историю",
            "сочини", "придумай идею", "генерация идей",
            "как бы выглядел", "альтернативная реальность",
            "напиши мне", "сочини мне", "придумай мне",
            "творческая задача",
        ],
        action: "generate_ideas",
        description: "творческий вопрос, генерация идей",
    },
    Intent {
        name: "help_request",
        patterns: &[
            "помоги", "подскажи", "что делать", "как быть",
            "я не знаю", "объясни мне", "научи меня",
            "ты можешь помочь", "посоветуй",
        ],
        action: "provide_help",
        description: "просьба о помощи",
    },
];

/// Скомпилированные паттерны параллельно INTENTS (тот же порядок).
static COMPILED: Lazy<Vec<Vec<(Regex, usize, &'static str)>>> = Lazy::new(|| {
    INTENTS
        .iter()
        .map(|i| {
            i.patterns
                .iter()
                .map(|p| {
                    let re = crate::py_text::py_regex(&format!("(?i){p}"));
                    (re, p.chars().count(), *p)
                })
                .collect()
        })
        .collect()
});

/// agent/intent_router.py::detect_intent (для непустого query)
pub fn detect_intent(query: &str) -> (String, f64, String) {
    let q = crate::py_text::py_strip(&query.py_lowercase()).to_string();

    let mut best_intent = "unknown";
    let mut best_confidence = 0.0f64;
    let mut best_pattern = "";

    for (intent, pats) in INTENTS.iter().zip(COMPILED.iter()) {
        for (re, plen, src) in pats {
            if re.is_match(&q) {
                let mut confidence = 0.5 + (*plen as f64 / 100.0);
                if confidence > 1.0 {
                    confidence = 1.0;
                }
                if confidence > best_confidence {
                    best_confidence = confidence;
                    best_intent = intent.name;
                    best_pattern = src;
                }
            }
        }
    }

    if best_intent == "unknown" {
        if q.chars().count() < 10 && ["привет", "здра", "как ты", "дела"].iter().any(|w| q.contains(w)) {
            return ("social_dialog".to_string(), 0.6, "short_social".to_string());
        }
        return ("objective_information".to_string(), 0.4, "default".to_string());
    }
    (best_intent.to_string(), best_confidence, best_pattern.to_string())
}

/// agent/intent_router.py::should_use_rag
pub fn should_use_rag(intent_type: &str) -> bool {
    intent_type == "objective_information"
}

/// agent/intent_router.py::get_intent_action
pub fn get_intent_action(intent_type: &str) -> &'static str {
    INTENTS.iter().find(|i| i.name == intent_type).map(|i| i.action).unwrap_or("unknown")
}

/// agent/intent_router.py::get_intent_description
pub fn get_intent_description(intent_type: &str) -> &'static str {
    INTENTS.iter().find(|i| i.name == intent_type).map(|i| i.description).unwrap_or("неизвестный тип запроса")
}

/// agent/intent_router.py::get_intent_explanation
pub fn get_intent_explanation(intent_type: &str) -> &'static str {
    match intent_type {
        "objective_information" => "Я поняла, что ты хочешь узнать факты. Я поищу информацию.",
        "subjective_interpretation" => "Я поняла, что ты хочешь моё мнение или анализ. Я поделюсь им.",
        "self_reflection" => "Я поняла, что ты спрашиваешь обо мне. Я расскажу о себе.",
        "creative_reasoning" => "Я поняла, что ты хочешь творчества. Давай пофантазируем.",
        "social_dialog" => "Я поняла, что ты хочешь просто поговорить. Я с радостью поболтаю.",
        "help_request" => "Я поняла, что тебе нужна помощь. Я постараюсь помочь.",
        _ => "Я не совсем поняла, что ты имеешь в виду. Уточни, пожалуйста.",
    }
}

#[pyfunction]
#[pyo3(name = "detect_intent")]
fn py_detect_intent(query: &str) -> (String, f64, String) {
    detect_intent(query)
}

#[pyfunction]
#[pyo3(name = "should_use_rag")]
fn py_should_use_rag(intent_type: &str) -> bool {
    should_use_rag(intent_type)
}

#[pyfunction]
#[pyo3(name = "get_intent_action")]
fn py_get_intent_action(intent_type: &str) -> &'static str {
    get_intent_action(intent_type)
}

#[pyfunction]
#[pyo3(name = "get_intent_description")]
fn py_get_intent_description(intent_type: &str) -> &'static str {
    get_intent_description(intent_type)
}

#[pyfunction]
#[pyo3(name = "get_intent_explanation")]
fn py_get_intent_explanation(intent_type: &str) -> &'static str {
    get_intent_explanation(intent_type)
}

pub fn register(_py: Python<'_>, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(py_detect_intent, m)?)?;
    m.add_function(wrap_pyfunction!(py_should_use_rag, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_intent_action, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_intent_description, m)?)?;
    m.add_function(wrap_pyfunction!(py_get_intent_explanation, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn module_demo_queries() {
        assert_eq!(detect_intent("Пойдёшь за меня замуж?").0, "social_dialog");
        assert_eq!(detect_intent("Как работает DHT?").0, "objective_information");
        assert_eq!(detect_intent("Расскажи о себе").0, "self_reflection");
        assert_eq!(detect_intent("Придумай историю про дракона").0, "creative_reasoning");
        assert_eq!(detect_intent("Помоги мне выбрать ноутбук").0, "help_request");
    }

    #[test]
    fn default_and_short_social() {
        assert_eq!(detect_intent("xyz"), ("objective_information".to_string(), 0.4, "default".to_string()));
    }

    #[test]
    fn dollar_anchor_and_case_insensitive_latin() {
        assert_eq!(detect_intent("факты").2, "факты?$");
        assert_eq!(detect_intent("что факты дальше").0, "objective_information"); // 'факты' не в конце -> не `факты?$`, но "что" и др.
        assert_eq!(detect_intent("Расскажи про YANDI").0, "self_reflection");
    }

    #[test]
    fn helpers() {
        assert!(should_use_rag("objective_information") && !should_use_rag("help_request"));
        assert_eq!(get_intent_action("nope"), "unknown");
        assert_eq!(get_intent_description("nope"), "неизвестный тип запроса");
        assert!(get_intent_explanation("nope").starts_with("Я не совсем поняла"));
    }
}
