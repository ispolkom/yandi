# Agent Tools — Plan & Status

## Цель
Дать агенту "руки" — набор инструментов для работы с файлами, системой, Redis и AI чатами.
Агент сможет получить задачу → построить план через AI → выполнить шаги → протестировать.

## Архитектура

```
agent/tools/
  __init__.py      router: tool("fs.read", path=...) → нужный модуль
  tool_fs.py       файловая система (read/write/mkdir/list/delete)
  tool_search.py   поиск файлов и содержимого (find/grep)
  tool_system.py   состояние системы (CPU/RAM/диск/процессы/Debian)
  tool_redis.py    Redis (keys/get/stats/queues)
  tool_shell.py    sandbox bash (белый список команд)
  tool_ai.py       отправить задачу в DeepSeek/Kimi/Claude через pet API

pet/
  api_tools.py     HTTP эндпоинты для вызова tools из браузера/агента
```

## Agentic Loop

```
Задача (текст)
  → tool_ai: DeepSeek строит пошаговый план (JSON)
  → Executor: выполняет шаги один за одним
      каждый шаг = вызов tool_*
  → tool_ai: проверить результат
  → если fail → вернуть в планировщик с контекстом ошибки
  → если ok → сохранить в registry/decisions/
```

## Ограничения безопасности
- tool_fs: только внутри PROJECT_ROOT (yandi/)
- tool_shell: whitelist команд: ls, find, cat, mkdir, python, pytest, cargo test
- tool_ai: timeout 180s, max 3 retry
- Нет rm -rf, нет curl, нет выхода за пределы проекта

## Статус задач

| # | Модуль | Статус | Описание |
|---|--------|--------|----------|
| 1 | `tool_fs.py` | ✅ Готово | read/write/mkdir/list/delete/exists |
| 2 | `tool_redis.py` | ✅ Готово | keys/get/set/delete/stats/list_range |
| 3 | `tool_search.py` | ✅ Готово | find файлов, grep по содержимому |
| 4 | `tool_system.py` | ✅ Готово | CPU/RAM/диск/процессы/OS info |
| 5 | `tool_shell.py` | ✅ Готово | sandbox bash, whitelist |
| 6 | `tool_ai.py` | ✅ Готово | вызов pet API → DeepSeek/Kimi/Claude |
| 7 | `__init__.py` (router) | ✅ Готово | единая точка входа tool(name, **kwargs) |
| 8 | HTTP `/api/tools/*` | ✅ Готово | list/run/plan/execute/run_task |
| 9 | `executor.py` | ✅ Готово | execute_plan + run_task (agentic loop) |
| 10 | Тест всех tools | ✅ Готово | 9/9 passed |
| 11 | CLI permissions | ✅ Готово | --allow-path / --allow-shell-full / --allow-net |
