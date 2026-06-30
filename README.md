YANDI — You & I

License: MIT
Status: Alpha
PRs: Welcome

ENGLISH

What is YANDI?

YANDI is a distributed intelligence network where knowledge is not concentrated in a single model, but emerges from a network of local, specialized AI nodes.

Key idea: YANDI does not store all knowledge. It stores paths to verified answers. Instead of giving a fish, it teaches how to fish.

Every node learns how to find, not just know.
Reputation grows from successful paths, not from authority.
Trust is built through consensus, not through a single source.

How Trust Works

1. User sends a query.
2. Local node receives it.
3. Node searches for trusted paths, not just facts.
4. Each path has a reputation score.
5. Node follows the highest-reputation path.
6. Answer is returned with a confidence score.
7. Path is saved as a trace for future learning.

Why It Matters

Old AI gives answers.
YANDI gives paths to answers.
Old AI uses one model.
YANDI uses a network of nodes.
Old AI is centralized.
YANDI is distributed.
Old AI trusts by default.
YANDI trusts by reputation.
Old AI stores static knowledge.
YANDI is a living ecosystem.

Philosophy

The network grows smarter not because one model becomes larger, but because more experts join the network.

YANDI follows the principle of distributed intelligence:

Local First — execution on user devices by default.
Privacy by Default — no data collection without permission.
User-Owned Memory — knowledge belongs to its creator.
Permission-Based Sharing — users control what they contribute.
No Single Point of Control — the network survives even if parts fail.

What's Inside

P2P node in Rust — DHT, encryption, port rotation, DPI bypass.
AI orchestrator in Python — local AI on GPU, Qwen 9B.
Council Chat Server in FastAPI — multi-model AI chat.
Firefox extension — bridge between browser and AI models.
Knowledge base — test dataset across 14 topics.
Reputation system — tracks trust of nodes, models, and paths.

Getting Started

git clone https://github.com/ispolkom/yandi.git
cd yandi
pip install -r requirements.txt
./start.sh

Details in STATUS.md and AGENT_TOOLS_PLAN.md.

How to Help

Code — open a Pull Request.
Ideas — open an Issue.
Data — run a node and share your traces.
Testing — try YANDI and report bugs.
Knowledge — verify answers and improve the network.

Looking for Partners

We are looking for:

AI providers with API, CLI, or models for integration.
Developers to help with Rust or Python.
Researchers interested in distributed AI.

If you share our vision, reach out.

License

MIT License — free distribution, modification, use for any purpose.

Author: Feodor Alekseevich Muntyan

Contact

GitHub: ispolkom
Email: mtgarant@gmail.com

RUSSIAN

Что такое YANDI?

YANDI — это распределённая сеть интеллекта, где знания не сосредоточены в одной модели, а возникают в сети локальных специализированных ИИ-узлов.

Главная идея: YANDI не хранит все знания. Он хранит пути к проверенным ответам. Вместо того чтобы дать рыбу, он учит ловить рыбу.

Каждая нода учится находить, а не просто знать.
Репутация растёт от успешных путей, а не от авторитета.
Доверие строится через консенсус, а не через один источник.

Как работает доверие

1. Нода получает запрос.
2. Она ищет проверенные пути, а не просто факты.
3. Каждый путь имеет репутацию.
4. Нода следует по пути с наивысшей репутацией.
5. Ответ возвращается с уровнем доверия.
6. Путь сохраняется как трейс для будущего обучения.

Почему это важно

Старый ИИ даёт ответы.
YANDI даёт пути к ответам.
Старый ИИ использует одну модель.
YANDI использует сеть узлов.
Старый ИИ централизован.
YANDI распределён.
Старый ИИ доверяет по умолчанию.
YANDI доверяет через репутацию.
Старый ИИ хранит статичное знание.
YANDI — это живая экосистема.

Философия

Сеть становится умнее не потому, что одна модель становится больше, а потому что к сети присоединяется больше экспертов.

YANDI следует принципу распределённого интеллекта:

Локальность по умолчанию — выполнение на устройстве пользователя.
Конфиденциальность по умолчанию — никакого сбора данных без разрешения.
Память принадлежит пользователю — знания остаются у создателя.
Обмен по разрешению — пользователь контролирует свой вклад.
Нет единой точки контроля — сеть живёт, даже если часть узлов отключена.

Что внутри

P2P нода на Rust — DHT, шифрование, ротация портов, обход DPI.
AI оркестратор на Python — локальный AI на GPU, Qwen 9B.
Council Chat Server на FastAPI — многомодельный чат с AI.
Расширение для Firefox — мост между браузером и AI-моделями.
База знаний — тестовый датасет по 14 темам.
Система репутации — отслеживает доверие к узлам, моделям и путям.

Установка и запуск

git clone https://github.com/ispolkom/yandi.git
cd yandi
pip install -r requirements.txt
./start.sh

Подробности в STATUS.md и AGENT_TOOLS_PLAN.md.

Как помочь

Код — открывай Pull Request.
Идеи — создавай Issue.
Данные — запусти ноду и делись трейсами.
Тестирование — пробуй YANDI и сообщай об ошибках.
Знания — верифицируй ответы и улучшай сеть.

Ищем партнёров

Мы ищем:

Поставщиков AI с API, CLI или моделями для интеграции.
Разработчиков для помощи с Rust или Python.
Исследователей, которым интересен распределённый ИИ.

Если вы разделяете наше видение, напишите нам.

Лицензия

MIT License — свободное распространение, модификация, использование в любых целях.

Автор: Фёдор Алексеевич Мунтян

Контакты

GitHub: ispolkom
Email: mtgarant@gmail.com

YANDI — You & I. Together we build a world without control.
YANDI — Ты и Я. Вместе мы строим мир без контроля.
