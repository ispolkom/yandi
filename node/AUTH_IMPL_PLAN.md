# YANDI — Auth System Implementation Plan

**Создан:** 2026-05-14  
**Статус:** в работе  
**Статус-файл:** `AUTH_IMPL_STATUS.md`

---

## Архитектура

```
Первый старт (нет auth.json):
  Браузер → /  → middleware → редирект /setup
  Форма: [login_password] [master_password]
  Предупреждение: "Мастер-пароль не восстанавливается. Запишите на бумаге."
  POST /api/auth/setup → создаёт auth.json → редирект /

Повторный старт (есть auth.json):
  main.rs → читает auth.json → расшифровывает master_key (machine-id)
           → использует master_key для identity + chat storage
  Браузер → /  → middleware → нет сессии → редирект /login
  Форма: [login_password] [✓ запомнить браузером]
  POST /api/auth/login → создаёт сессию → cookie → редирект /

Другое железо (machine-id не совпал):
  main.rs → auth.json есть, но расшифровка master_key не удалась
           → web запускается в режиме "re-bind"
  Форма: [master_password] — "Новое устройство. Введите мастер-пароль."
  POST /api/auth/rebind → перешифровывает master_key под новый machine-id
```

## Файлы на диске

```
~/.yandi_keys/auth.json:
{
  "version": 1,
  "login_hash": "argon2id encoded string",
  "master_key_encrypted": {
    "machine_salt": "hex 32 bytes",
    "nonce": "hex 12 bytes",
    "ciphertext": "hex 48 bytes (32 key + 16 tag)"
  }
}
```

## Деривация ключей

```
master_password → Argon2id(32MB, 2iter) → master_key [32 bytes]
master_key → HKDF("identity") → identity_key  (шифрует ~/.yandi_keys/)
master_key → HKDF("chat")     → chat_key      (шифрует ~/.yandi/chats/)
master_key → HKDF("session")  → (не используется, сессии в RAM)

machine_id → Argon2id → machine_key
master_key → AES-GCM(machine_key) → хранится в auth.json
```

## Сессии

- Токен: 32 случайных байта, hex-encoded
- Хранение: `HashMap<String, SessionInfo>` в памяти (`Arc<Mutex<...>>`)
- Cookie: `yandi_session=<hex>; HttpOnly; SameSite=Strict; Path=/`
- Remember me: `Max-Age=2592000` (30 дней)
- Без remember me: session cookie (живёт пока открыт браузер)

---

## Этапы реализации

### Этап 1 — Auth модуль `src/web/auth.rs`
- [ ] `AuthStore` — загрузка/сохранение auth.json
- [ ] `SessionStore` — in-memory сессии
- [ ] `AuthState` — объединяет оба + master_key в памяти
- [ ] Функции: `setup()`, `verify_login()`, `create_session()`, `verify_session()`
- [ ] Cargo check

### Этап 2 — HTML страницы
- [ ] `src/web/ui/login.html` — форма входа + remember me
- [ ] `src/web/ui/setup.html` — первичная настройка с предупреждением
- [ ] Cargo check (compile-time include_str!)

### Этап 3 — Middleware + роуты в server.rs
- [ ] Добавить `AuthState` в `AppState`
- [ ] Axum middleware: `auth_middleware` (проверяет сессию)
- [ ] Роуты: `GET /login`, `GET /setup`, `POST /api/auth/login`, `POST /api/auth/setup`, `GET /api/auth/logout`
- [ ] Исключения из middleware: /login, /setup, /api/auth/*
- [ ] Кнопка "Выйти" в основном UI
- [ ] Cargo check

### Этап 4 — Интеграция в main.rs
- [ ] При старте: читать auth.json
- [ ] Auto-decrypt master_key через machine-id
- [ ] Передавать master_key в NodeIdentity (вместо YANDI_KEY_PASSWORD)
- [ ] Передавать master_key в ChatStorage (для деривации chat_key)
- [ ] Режимы: "полный старт", "первый запуск (setup)", "rebind (другое железо)"
- [ ] Cargo check

### Этап 5 — Улучшение шифрования чата
- [ ] `ChatStorage::new_with_key(node_id, master_key)` 
- [ ] `get_encryption_key()` → HKDF(master_key, "chat", peer_id)
- [ ] Старые чаты (ключ = node_id): читать со старым ключом, пересохранять с новым при открытии
- [ ] Cargo check

### Этап 6 — Экспорт контактов
- [ ] `GET /api/contacts/export` → JSON файл с контактами
- [ ] Кнопка в UI настроек
- [ ] Cargo check

---

## Что НЕ входит в эту итерацию
- Шифрование файлов в downloads/ (следующая итерация)
- Полный backup bundle .ynd (следующая итерация)
- Multi-user (нет необходимости)
