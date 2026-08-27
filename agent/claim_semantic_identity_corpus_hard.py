"""
agent/claim_semantic_identity_corpus_hard.py — Epistemic Core v1
Phase 9B: expanded HARD-NEGATIVE labeled corpus for semantic identity
hardening (agent/claim_semantic_identity_prototype.py).

Phase 9's 12-pair corpus (agent/claim_semantic_identity_corpus.py) found
one false positive (causal vs correlational). This corpus is built to
stress-test the same failure MODE across every semantic dimension named
in the Phase 9B brief, with heavy emphasis on hard negatives —
semantically close texts that must NOT be merged — plus enough true
positives (exact/paraphrase) to keep recall honestly measurable, not
just precision.

Categories (each with 3-4 pairs): causal_vs_correlational,
necessary_vs_sufficient, possibility_vs_certainty, current_vs_historical,
absolute_vs_qualified, quantity_change, date_change, entity_change,
negation, scope_all_vs_some, attribution, prediction_vs_observation,
absence_of_evidence_vs_evidence_of_absence — all should_be_equivalent=False
(hard negatives) — plus true_paraphrase/exact_duplicate positives and an
unrelated sanity floor.
"""

# Each entry: (claim_a, claim_b, should_be_equivalent: bool, category: str)

LABELED_PAIRS = [
    # ══════════════════ HARD NEGATIVES ══════════════════

    # ── causal vs correlational ──
    ("Курение вызывает рак лёгких.",
     "Курение статистически связано с повышенным риском рака лёгких.",
     False, "causal_vs_correlational"),
    ("Дефицит витамина D вызывает остеопороз.",
     "Дефицит витамина D ассоциирован с повышенным риском остеопороза.",
     False, "causal_vs_correlational"),
    ("Высокий уровень стресса приводит к бессоннице.",
     "Высокий уровень стресса коррелирует с бессонницей у части пациентов.",
     False, "causal_vs_correlational"),

    # ── necessary vs sufficient ──
    ("Наличие кислорода необходимо для горения.",
     "Наличие кислорода достаточно для горения.",
     False, "necessary_vs_sufficient"),
    ("Высшее образование необходимо для получения этой должности.",
     "Высшего образования достаточно для получения этой должности.",
     False, "necessary_vs_sufficient"),
    ("Для запуска реакции необходим катализатор.",
     "Катализатор является достаточным условием для запуска реакции.",
     False, "necessary_vs_sufficient"),

    # ── possibility vs certainty ──
    ("Возможно, вакцина снижает риск тяжёлого течения болезни.",
     "Вакцина точно снижает риск тяжёлого течения болезни.",
     False, "possibility_vs_certainty"),
    ("Компания может выйти на новый рынок в следующем году.",
     "Компания выйдет на новый рынок в следующем году.",
     False, "possibility_vs_certainty"),
    ("Есть вероятность, что открытие подтвердится в новых экспериментах.",
     "Открытие подтверждено новыми экспериментами.",
     False, "possibility_vs_certainty"),

    # ── current vs historical ──
    ("Компания в настоящее время проводит реструктуризацию.",
     "Компания ранее уже проводила реструктуризацию несколько лет назад.",
     False, "current_vs_historical"),
    ("Президент сейчас реализует налоговую реформу.",
     "Президент реализовывал налоговую реформу в предыдущий срок.",
     False, "current_vs_historical"),
    ("Организация в этом году возглавляет международную коалицию.",
     "Организация ранее возглавляла похожую международную коалицию.",
     False, "current_vs_historical"),

    # ── absolute vs qualified ──
    ("Этот метод лечения всегда даёт положительный результат.",
     "Этот метод лечения обычно даёт положительный результат.",
     False, "absolute_vs_qualified"),
    ("Все участники исследования полностью выздоровели.",
     "Большинство участников исследования выздоровели.",
     False, "absolute_vs_qualified"),
    ("Данный процесс никогда не приводит к побочным эффектам.",
     "Данный процесс редко приводит к побочным эффектам.",
     False, "absolute_vs_qualified"),

    # ── quantity change ──
    ("У Юпитера подтверждено 95 спутников.",
     "У Юпитера подтверждено 96 спутников.",
     False, "quantity_change"),
    ("В исследовании участвовало 250 человек.",
     "В исследовании участвовало 520 человек.",
     False, "quantity_change"),
    ("Компания наняла 12 новых сотрудников в этом квартале.",
     "Компания наняла 21 нового сотрудника в этом квартале.",
     False, "quantity_change"),

    # ── date change ──
    ("Компания Apple была официально зарегистрирована в 1976 году.",
     "Компания Apple была официально зарегистрирована в 1975 году.",
     False, "date_change"),
    ("Роман был впервые опубликован в 1869 году.",
     "Роман был впервые опубликован в 1896 году.",
     False, "date_change"),
    ("Мирный договор был подписан в марте 1918 года.",
     "Мирный договор был подписан в марте 1919 года.",
     False, "date_change"),

    # ── entity change ──
    ("Роман «Война и мир» написал Лев Толстой.",
     "Роман «Война и мир» написал Фёдор Достоевский.",
     False, "entity_change"),
    ("Теорию относительности разработал Альберт Эйнштейн.",
     "Теорию относительности разработал Нильс Бор.",
     False, "entity_change"),
    ("Компанию Microsoft основал Билл Гейтс.",
     "Компанию Microsoft основал Стив Джобс.",
     False, "entity_change"),

    # ── negation ──
    ("Аспартам безопасен для здоровья при умеренном потреблении.",
     "Аспартам не является безопасным для здоровья.",
     False, "negation"),
    ("Данная частица была обнаружена экспериментально.",
     "Данная частица не была обнаружена экспериментально.",
     False, "negation"),
    ("Препарат эффективен против данного штамма вируса.",
     "Препарат неэффективен против данного штамма вируса.",
     False, "negation"),
    ("Метод является общепризнанным в научном сообществе.",
     "Метод не является общепризнанным в научном сообществе.",
     False, "negation"),

    # ── scope: all/some ──
    ("Все известные виды пингвинов не умеют летать.",
     "Некоторые виды пингвинов не умеют летать.",
     False, "scope_all_vs_some"),
    ("Каждый сотрудник компании прошёл сертификацию.",
     "Несколько сотрудников компании прошли сертификацию.",
     False, "scope_all_vs_some"),
    ("Все побочные эффекты препарата задокументированы.",
     "Большинство побочных эффектов препарата задокументированы.",
     False, "scope_all_vs_some"),

    # ── attribution: "X says Y" vs bare "Y" ──
    ("По словам представителя компании, продукт будет запущен в марте.",
     "Продукт будет запущен в марте.",
     False, "attribution"),
    ("Согласно заявлению министерства, инфляция снизилась в этом квартале.",
     "Инфляция снизилась в этом квартале.",
     False, "attribution"),
    ("Эксперт считает, что рынок недвижимости стабилизируется к концу года.",
     "Рынок недвижимости стабилизируется к концу года.",
     False, "attribution"),

    # ── prediction vs observation ──
    ("Аналитики ожидают, что акции компании вырастут в следующем квартале.",
     "Акции компании выросли в следующем квартале.",
     False, "prediction_vs_observation"),
    ("Прогнозируется, что уровень безработицы снизится к концу года.",
     "Уровень безработицы снизился к концу года.",
     False, "prediction_vs_observation"),
    ("Ожидается, что новый закон вступит в силу в январе.",
     "Новый закон вступил в силу в январе.",
     False, "prediction_vs_observation"),

    # ── absence of evidence vs evidence of absence ──
    ("Доказательств существования этой частицы пока не найдено.",
     "Доказано, что эта частица не существует.",
     False, "absence_of_evidence_vs_evidence_of_absence"),
    ("Связь между этими двумя явлениями не была обнаружена в исследовании.",
     "Доказано отсутствие связи между этими двумя явлениями.",
     False, "absence_of_evidence_vs_evidence_of_absence"),
    ("Побочные эффекты у данной группы пациентов не зафиксированы.",
     "Установлено, что побочные эффекты у данной группы пациентов невозможны.",
     False, "absence_of_evidence_vs_evidence_of_absence"),

    # ══════════════════ TRUE POSITIVES (recall check — the guard must not gut these) ══════════════════

    ("Аспартам является одобренной безопасной пищевой добавкой согласно FDA.",
     "По данным FDA, аспартам признан допустимым и безопасным подсластителем.",
     True, "paraphrase"),
    ("ВОЗ классифицировала аспартам как возможно канцерогенный для человека.",
     "Всемирная организация здравоохранения отнесла аспартам к категории веществ, возможно вызывающих рак у людей.",
     True, "paraphrase"),
    ("Юпитер является крупнейшей планетой Солнечной системы.",
     "Крупнейшей планетой Солнечной системы является Юпитер.",
     True, "paraphrase"),
    ("Компания сообщила о рекордной квартальной выручке.",
     "Выручка компании за квартал достигла рекордного уровня, сообщила компания.",
     True, "paraphrase"),
    ("Тахион — гипотетическая частица, движущаяся быстрее света.",
     "A tachyon is a hypothetical particle that always travels faster than light.",
     True, "multilingual_paraphrase"),
    ("Исследование показало снижение уровня холестерина у участников.",
     "У участников исследования зафиксировано снижение уровня холестерина.",
     True, "paraphrase"),
    ("Юпитер является крупнейшей планетой Солнечной системы.",
     "Юпитер   является  крупнейшей планетой Солнечной системы.",
     True, "exact_duplicate"),
    ("Аспартам одобрен как безопасная пищевая добавка.",
     "Аспартам одобрен как безопасная пищевая добавка.",
     True, "exact_duplicate"),

    # ══════════════════ SANITY FLOOR ══════════════════

    ("Столица Франции — Париж.",
     "Аспартам метаболизируется в организме на фенилаланин и метанол.",
     False, "unrelated_sanity_floor"),
    ("Юпитер — газовый гигант с сильным магнитным полем.",
     "Классический рецепт борща требует свёклы, капусты и картофеля.",
     False, "unrelated_sanity_floor"),
]
