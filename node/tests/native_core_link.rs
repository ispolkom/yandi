//! Доказательство «единого бинарника»: перенесённая с Python логика (`rustlib/yandi_rs`, БЕЗ Python — `default-features = false`)
//! подключается к узлу как обычная библиотека и работает в его рабочем пространстве. Ничего в поведении узла этот тест не меняет.

#[test]
fn shell_gate_and_policy_run_natively() {
    assert!(yandi_rs::tool_shell::allowed("ls -la", false, false));
    assert!(!yandi_rs::tool_shell::allowed("rm -rf /", false, false));
    assert!(!yandi_rs::tool_shell::allowed("ls | wc", false, false));
}

#[test]
fn field_encryption_round_trips_and_binds_context() {
    let key = [7u8; 32];
    let blob = yandi_rs::crypto::encrypt_field(&key, "секрет 🌍", "question", "42", "text", 1).expect("32-байтовый ключ");
    assert_eq!(blob[0], 1);
    let ok = yandi_rs::crypto::decrypt_field(&key, &blob, "question", "42", "text").expect("тот же контекст");
    assert_eq!(ok, "секрет 🌍".as_bytes());
    assert!(yandi_rs::crypto::decrypt_field(&key, &blob, "question", "43", "text").is_none(), "чужая строка не открывается");
}

#[test]
fn python_exact_text_primitives_are_available() {
    assert_eq!(yandi_rs::py_text::py_lower("ПРИВЕТ İ"), "привет i\u{307}");
    assert_eq!(yandi_rs::py_text::py_casefold("Straße"), "strasse");
    assert!(yandi_rs::orch_tag_tree::tokenize("Как настроить Docker?").contains(&"docker".to_string()));
}
