"""Мониторинг рынка фронтенд-вакансий v2: весь hh, категории, поднятые/переоткрытые, закрытые/скрытые.

Спека: openspec/specs/market-monitor. Ловушки, которые закрывают тесты:
- в поиске hh `created_at == published_at`, новизну различаем только по `initial_created_at`;
- «переопубликованные» смешивали платные поднятия свежих вакансий и переоткрытие старых;
- 2/3 «выпавших из выдачи» возвращались через 1–2 дня — это скрытие, а не уход;
- в «фронтенд» попадали QA на TS, React Native и Node-бэкенды (38% среза).
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import market_monitor as mm  # noqa: E402

MSK = timezone(timedelta(hours=3))
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=MSK)
PREV_RUN = datetime(2026, 10, 5, 8, 0, tzinfo=MSK)

# справочник /areas в форме API hh: страны → вложенные регионы
AREAS = [
    {"id": "113", "name": "Россия", "areas": [
        {"id": "1", "name": "Москва", "areas": []},
        {"id": "2", "name": "Санкт-Петербург", "areas": []},
        {"id": "1620", "name": "Республика Марий Эл", "areas": [{"id": "1624", "name": "Йошкар-Ола", "areas": []}]},
    ]},
    {"id": "16", "name": "Беларусь", "areas": [{"id": "1002", "name": "Минск", "areas": []}]},
]
COUNTRY = mm.country_index(AREAS)


PROF_ROLES = {"categories": [
    {"id": "11", "name": "Информационные технологии", "roles": [
        {"id": "96", "name": "Программист, разработчик"}, {"id": "104", "name": "Руководитель группы разработки"},
        {"id": "34", "name": "Дизайнер, художник"}, {"id": "10", "name": "Аналитик"}, {"id": "124", "name": "Тестировщик"}]},
    {"id": "22", "name": "Рестораны", "roles": [{"id": "94", "name": "Повар, пекарь, кондитер"}]},
]}
IT_ROLES = mm.it_role_index(PROF_ROLES)


def item(vid, name="Frontend-разработчик", employer="ООО Ромашка", area="1",
         published="2026-10-05T20:00:00+0300", remote=False, salary=None, roles=("96",), snippet=""):
    """Вакансия в том виде, в каком её отдаёт поиск hh (поля сверены с живым API)."""
    return {
        "id": str(vid),
        "name": name,
        "employer": {"id": "1", "name": employer},
        "area": {"id": area, "name": "..."},
        "work_format": [{"id": "REMOTE" if remote else "ON_SITE", "name": "..."}],
        "published_at": published,
        "created_at": published,
        "salary": salary,
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "professional_roles": [{"id": r, "name": "..."} for r in roles],
        "snippet": {"requirement": snippet, "responsibility": None},
    }


def vac(vid, node_ids=frozenset(), **kw):
    return mm.parse_vacancy(item(vid, **kw), COUNTRY, IT_ROLES, node_ids)


# ── разбор и справочники ────────────────────────────────────────────────


def test_parse_dt_accepts_hh_offset_without_colon():
    assert mm.parse_dt("2026-09-30T20:00:00+0300") == datetime(2026, 9, 30, 20, 0, tzinfo=MSK)


def test_country_index_covers_nested_regions():
    assert COUNTRY["1"] == "113" and COUNTRY["1624"] == "113"
    assert COUNTRY["1002"] == "16"


def test_parse_vacancy_reads_search_item():
    v = vac(10, area="1002", remote=True, salary={"from": 3000, "to": None, "currency": "USD"})
    assert v.id == "10" and v.employer == "ООО Ромашка"
    assert v.country_id == "16" and v.remote is True
    assert v.category == "front"
    assert v.salary_from == 3000 and v.currency == "USD"
    assert v.url == "https://hh.ru/vacancy/10"


def test_parse_vacancy_survives_missing_optional_fields():
    raw = item(11)
    raw["employer"] = None
    raw["work_format"] = None
    raw["area"] = None
    raw["professional_roles"] = None
    v = mm.parse_vacancy(raw, COUNTRY, IT_ROLES)
    assert v.employer == "—" and v.remote is False and v.country_id == ""


# ── запрос и роли ───────────────────────────────────────────────────────


@pytest.mark.parametrize("term", ['"vue.js"', "vuejs", "angularjs", '"next.js"', "nextjs"])
def test_query_has_alternative_spellings(term):
    """06.10: «Vue.js разработчик» прошёл мимо запроса — hh не склеивает vue.js с vue."""
    assert term in mm.QUERY


def test_it_roles_are_whole_it_category_plus_other():
    assert {"96", "104", "34", "10", "124"} <= IT_ROLES
    assert "40" in IT_ROLES  # «Другое»: «Фронтенд инженер для десктопных приложений»
    assert "94" not in IT_ROLES


def test_non_it_role_is_never_front():
    """06.10: во «Фронтенд» попали «Повар в ресторан „La Vue“» и уборщица оттуда же."""
    assert vac(1, name='Повар в ресторан "La Vue"', roles=("94",)).category == "other"


def test_frontend_under_designer_role_is_kept():
    """HTML-верстальщиков работодатели публикуют под ролью «Дизайнер» — их не теряем."""
    assert vac(2, name="HTML-верстальщик / Frontend-верстальщик", roles=("34",)).category == "front"


def test_missing_roles_fall_back_to_title():
    assert vac(3, roles=()).category == "front"


# ── категории ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("name, expected", [
    ("Frontend-разработчик (React)", "front"),
    ("Middle Vue / Nuxt Frontend Developer", "front"),
    ("UI- разработчик Middle+/Senior", "front"),
    ("Разработчик интерфейсов", "front"),
    ("JavaScript-разработчик", "front"),
    ("AQA TypeScript", "qa"),
    ("Full-Stack QA Engineer (AI-Native, Python)", "qa"),
    ("Тестировщик JavaScript", "qa"),
    ("Full-stack аналитик (Business / System)", "nontech"),
    ("UI/UX дизайнер", "nontech"),
    ("Преподаватель по направлению Разработчик веб-приложений", "nontech"),
    ("Разработчик React Native", "front"),  # 06.10: React Native считаем фронтом
    ("Мобильный разработчик (Flutter)", "mobile"),
    ("Fullstack-разработчик (React + Node.js)", "fullstack"),
    ("Full-stack разработчик (Go+React)", "fullstack"),
    ("Frontend / Node.js developer", "fullstack"),  # фронт и бэк вместе — это fullstack
    ("Backend-разработчик (Node.js)", "backend"),
    ("Nodejs (nestjs) middle developer", "backend"),
    ("TypeScript-разработчик (backend)", "backend"),
    ("Веб-разработчик", "web"),
    ("Web-программист", "web"),
    ("Инженер-программист", "other"),
    # пойманы на живой выдаче 05.10.2026
    ("Машинист гусеничного экскаватора (JCB JS 260 и JCB JS 305)", "other"),
    ("JS-разработчик", "front"),
    ("Fullstack-разработчик (.net)", "fullstack_other"),
    ("Fullstack разработчик WordPress", "fullstack_other"),
    ("Fullstack-разработчик (Java/Kotlin + Angular)", "fullstack"),
    ("Разработчик Full Stack (Vue 3 + C++ / Python)", "fullstack"),
    ("Senior Fullstack-разработчик (Laravel + Nuxt)", "fullstack"),
    ("Backend / Fullstack Developer (NestJS / TypeScript)", "fullstack"),
    ("Fullstack-разработчик", "fullstack"),  # стек не указан — остаётся в JS-рынке
    ("Программист .net (C#, ASP.NET Core, MSSQL, Javascript)", "other"),
    ("Full-stack Developer (PHP / JS)", "fullstack"),
    ("JavaScript Trainee", "front"),
])
def test_classify(name, expected):
    assert mm.classify(name) == expected


def test_js_market_categories():
    # Node-бэкенд и общий «веб-разработчик» в отчёт не входят (решение 06.10)
    assert mm.JS_MARKET == {"front", "fullstack", "ai_js"}
    assert "fullstack_other" not in mm.JS_MARKET  # fullstack на .NET/Java/PHP — не JS-рынок


# ── стек, Node, AI ──────────────────────────────────────────────────────


def test_stack_from_title():
    assert vac(1, name="Frontend-разработчик (React)").stack == ("react",)


def test_stack_from_snippet_when_title_is_generic():
    assert vac(2, name="Frontend-разработчик", snippet="Опыт коммерческой разработки на Vue 3").stack == ("vue",)


def test_stack_several_and_none():
    assert vac(3, name="Frontend (React / Angular)").stack == ("react", "angular")
    assert vac(4, name="Frontend-разработчик", snippet="HTML, CSS, верстка").stack == ()


def test_fullstack_node_from_title_snippet_or_fulltext_query():
    assert vac(5, name="Fullstack-разработчик (React + Node.js)").node is True
    assert vac(6, name="Fullstack-разработчик", snippet="NestJS, PostgreSQL").node is True
    assert vac(7, name="Fullstack-разработчик", snippet="PHP, Laravel").node is False
    assert vac(8, name="Fullstack-разработчик", node_ids={"8"}).node is True


@pytest.mark.parametrize("name, expected", [
    ("Fullstack AI Engineer", "fullstack"),
    ("Frontend (AI-native) разработчик", "front"),
    ("Senior Frontend-разработчик (AI Agent / SDLC Automation)", "front"),
    ("Python (AI-native) разработчик", "ai"),
    ("Senior AI developer (Python)", "ai"),
    ("Vibe-coder / AI-кодер (Claude, Cursor)", "ai"),
    ("ИИ-инженер / AI-инженер", "ai"),
    ("AI-native Project Manager / Delivery Lead", "nontech"),
    ("QA-инженер (AI First, CRM)", "qa"),
    ("Продуктовый дизайнер AI Native (Senior)", "nontech"),
])
def test_classify_ai(name, expected):
    assert mm.classify(name) == expected


def test_ai_engineer_near_js_by_title_or_snippet():
    assert vac(10, name="AI-инженер (разработка голосовых и текстовых роботов, JS)").category == "ai_js"
    assert vac(11, name="Middle AI Engineer", snippet="TypeScript, Node.js, LangChain").category == "ai_js"
    assert vac(12, name="Senior AI developer (Python)", snippet="PyTorch, FastAPI").category == "ai"


@pytest.mark.parametrize("name, snippet", [
    # пойманы на живой выдаче 06.10: «веб» и «fullstack» в требованиях — не признак JS-стека
    ("AI-first Developer / Python Developer", "Разработка веб-сервисов, fullstack-подход"),
    ("Senior AI developer (Python)", "веб-сервисы, REST API"),
    ("Offensive Security Developer (Python/Go, AI/LLM)", "web security, TypeScript будет плюсом"),
    ("Python-разработчик (AI - агенты)", "React — плюс"),
    ("AI Engineer (GameDev)", "Unity, C#"),
])
def test_ai_python_and_others_are_not_js(name, snippet):
    assert vac(30, name=name, snippet=snippet).category == "ai"


def test_ai_flag_on_front_and_fullstack():
    assert vac(13, name="Fullstack AI Engineer").ai is True
    assert vac(14, name="Frontend (AI-native) разработчик").ai is True
    assert vac(15, name="Frontend-разработчик").ai is False


def test_ai_query_is_restricted_and_clean():
    assert "агент" not in mm.AI_QUERY  # приносил агентов по недвижимости
    assert "llm" in mm.AI_QUERY and '"ai engineer"' in mm.AI_QUERY


# ── v4: возраст по номеру вакансии (без детальных карточек) ─────────────
#
# Номера вакансий hh растут вместе с датой создания (0 нарушений на 166 карточках).
# Опорные точки «номер → время» берутся из поиска: максимальный номер среди опубликованных
# за час. Бэктест 06.10 на 166 карточках: новые 97/97, поднятые 48/48, ошибок 0.

EPOCH = datetime(2026, 1, 1, tzinfo=MSK)


def id_at(dt: datetime) -> int:
    """Модель нумерации hh для тестов: один номер в минуту."""
    return 100_000_000 + int((dt - EPOCH).total_seconds() // 60)


def at(s: str) -> datetime:
    return mm.parse_dt(s)


def anchors_daily(days=40, until=NOW):
    return mm.Anchors([(id_at(until - timedelta(days=d)), until - timedelta(days=d)) for d in range(days + 1)])


def test_anchor_interpolation_recovers_creation_time():
    a = anchors_daily()
    created = at("2026-09-20T15:30:00+0300")
    est = a.created(id_at(created))
    assert abs((est.estimate - created).total_seconds()) < 120


@pytest.mark.parametrize("created, published, kind", [
    ("2026-10-05T12:00:00+0300", "2026-10-05T12:00:00+0300", "new"),
    ("2026-10-05T02:00:00+0300", "2026-10-05T12:00:00+0300", "new"),       # модерация 10 ч — всё ещё новая
    ("2026-10-02T12:00:00+0300", "2026-10-05T12:00:00+0300", "bumped"),
    ("2026-09-05T13:00:00+0300", "2026-10-05T12:00:00+0300", "bumped"),    # 29,96 дня
    ("2026-09-05T11:00:00+0300", "2026-10-05T12:00:00+0300", "reopened"),  # 30,04 дня
    ("2026-08-01T12:00:00+0300", "2026-10-05T12:00:00+0300", "reopened"),
])
def test_age_kind_by_id(created, published, kind):
    k, age = mm.age_kind(id_at(at(created)), at(published), anchors_daily(days=70))
    assert k == kind


def test_older_than_all_anchors_is_reopened_only_when_provably_30_days():
    a = mm.Anchors([(id_at(at("2026-09-10T13:00:00+0300")), at("2026-09-10T13:00:00+0300"))] * 1
                   + [(id_at(NOW), NOW)])
    old = id_at(at("2026-08-01T12:00:00+0300"))
    assert mm.age_kind(old, at("2026-10-11T14:00:00+0300"), a)[0] == "reopened"  # создана до 10.09 → ≥ 31 дня
    assert mm.age_kind(old, at("2026-09-20T12:00:00+0300"), a)[0] == "unknown"   # видно лишь «≥ 10 дней»


def test_newer_than_all_anchors_is_new():
    a = mm.Anchors([(id_at(NOW - timedelta(hours=2)), NOW - timedelta(hours=2))])
    assert mm.age_kind(id_at(NOW), NOW, a)[0] == "new"
    stale = mm.Anchors([(id_at(PREV_RUN - timedelta(days=2)), PREV_RUN - timedelta(days=2))])
    assert mm.age_kind(id_at(NOW), NOW, stale)[0] == "unknown"  # точки устарели — не выдумываем


def test_anchors_prefer_cards_over_search_on_conflict():
    """Поиск даёт верхнюю оценку времени; точная дата из карточки важнее, если они спорят."""
    t0 = at("2026-10-01T13:00:00+0300")
    a = mm.Anchors([(1000, t0, "search"), (1001, t0 - timedelta(hours=2), "card"), (2000, t0 + timedelta(days=1), "search")])
    assert a.created(1001).estimate == t0 - timedelta(hours=2)
    times = [p[1] for p in a.points]
    assert times == sorted(times)  # точки монотонны


def test_anchor_windows_cover_missing_days_only():
    have = {(NOW - timedelta(days=d)).date().isoformat() for d in (1, 2, 3)}
    days = mm.missing_anchor_days(NOW, have)
    assert len(days) == mm.ANCHOR_DAYS - 3
    assert (NOW - timedelta(days=1)).date() not in days and (NOW - timedelta(days=4)).date() in days


# ── v4: закрытие по трём дням отсутствия ────────────────────────────────
#
# 30.09–05.10: из пропавших на 1–2 дня вернулись 47; из пропавших на 3+ дня — ни одна из 53.


def test_closed_after_three_days_absent_hidden_before():
    d1, d2, d3 = {"1", "2", "3", "4"}, {"1", "3", "4"}, {"1", "4"}
    today = {"1"}
    closed, hidden = mm.split_absent([d1, d2, d3], today)
    assert closed == ["2"]            # нет в d2, d3 и сегодня — три дня подряд
    assert sorted(hidden) == ["3", "4"]  # пропали 1–2 дня назад — пока «скрыты»


def test_returned_vacancy_is_neither_closed_nor_hidden():
    closed, hidden = mm.split_absent([{"1"}, set(), set()], {"1"})
    assert closed == [] and hidden == []


def test_closures_need_three_days_of_history():
    closed, hidden = mm.split_absent([{"1", "2"}, {"1"}], {"1"})
    assert closed == [] and hidden == ["2"]


# ── v4: пересчёт прошлого снимка текущим классификатором ────────────────


def test_recategorize_keeps_role_filter_and_ai_split():
    assert mm.recategorize("Уборщица в ресторан La Vue", "other", it_role=None) == "other"
    assert mm.recategorize("Frontend-разработчик", "other", it_role=0) == "other"
    assert mm.recategorize("JS стажёр", "other", it_role=1) == "front"
    assert mm.recategorize("Middle AI Engineer", "ai_js", it_role=1) == "ai_js"
    assert mm.recategorize("Разработчик React Native", "mobile", it_role=1) == "front"


# ── v4: React Native во фронте отдельной строкой стека ──────────────────


@pytest.mark.parametrize("name, category, stack", [
    ("React Native Middle Developer", "front", "react_native"),
    ("React Native разработчик", "front", "react_native"),
    ("Frontend разработчик (React/React Native)", "front", "react_native"),
    ("Мобильный разработчик React Native (Middle)", "front", "react_native"),
    ("Senior Full-stack Developer (React Native/Node)", "fullstack", "react_native"),
    ("Технический менеджер продукта (Mobile / React Native)", "nontech", "react_native"),
    ("Flutter (AI native) разработчик", "mobile", "js"),
    ("Fullstack Mobile Developer", "mobile", "js"),
])
def test_react_native_is_front_with_own_stack(name, category, stack):
    v = vac(50, name=name)
    assert (v.category, v.primary_stack) == (category, stack)


# ── v4: отчёты ──────────────────────────────────────────────────────────


def _front(i, name="Frontend-разработчик (React)", employer="Ромашка", **kw):
    return vac(i, name=name, employer=employer, **kw)


def _daily(**kw):
    listing = [
        _front(1), _front(2, name="Senior Frontend (Vue)"), _front(3, name="Team Lead Frontend"),
        _front(4, name="React Native разработчик"), _front(5, name="Frontend-разработчик"),
        vac(6, name="Fullstack (React + Node.js)"), vac(7, name="Fullstack-разработчик (TypeScript)"),
        vac(8, name="AI-инженер (JS)"), vac(9, name="AQA TypeScript"), vac(10, name="Backend Node.js"),
    ]
    base = dict(
        now=NOW, window_start=PREV_RUN, listing=listing, found=len(listing), collected=len(listing),
        prev={"front": 4, "fullstack": 2, "ai_js": 0},
        new=[(listing[0], 0.1), (listing[3], 0.0), (listing[5], 0.2), (listing[7], 0.0)],
        bumped=[(listing[1], 3)], reopened=[(listing[2], 45)], unknown=[],
        closed=[mm.ClosedVacancy("90", "Frontend (Angular)", "Озон", "front", 24)],
        hidden=[("91", "front"), ("92", "fullstack")], history_days=3,
    )
    base.update(kw)
    return mm.Report(**base)


def test_daily_js_market_block_and_stack_lines():
    text = mm.build_daily(_daily())[0]
    assert "<b>JS-рынок: 8</b> (+2 за сутки)" in text      # 5 + 2 + 1; QA и Node-бэкенд не входят
    assert "• Фронтенд: <b>5</b> (+1)" in text and "лидов 1" in text
    assert "◦ React 1" in text and "◦ Vue 1" in text and "◦ React Native 1" in text
    assert "◦ JS/TS без фреймворка 2" in text  # «Team Lead Frontend» и «Frontend-разработчик»
    assert "• Fullstack: <b>2</b> (0) · с Node.js 1" in text
    assert "• AI-инженеры на JS/TS: <b>1</b> (+1)" in text


def test_daily_stack_lines_sum_to_front():
    text = mm.build_daily(_daily())[0]
    import re as _re
    stack = sum(int(x) for x in _re.findall(r"◦ [^\d\n]+ (\d+)", text))
    assert stack == 5


def test_daily_flow_table_by_direction():
    text = mm.build_daily(_daily())[0]
    rows = {line.split()[0]: line.split()[1:] for line in text.split("<pre>")[1].split("</pre>")[0].strip().splitlines()[1:]}
    assert rows["Фронтенд"] == ["2", "1", "1", "1", "+1"]   # новые, подняли, переоткр., закрыты, прирост
    assert rows["Fullstack"] == ["1", "0", "0", "0", "0"]
    assert "Временно скрыты из поиска: 2" in text
    assert "медиана возраста 45 дн." in text and "прожили в среднем 24 дн." in text


def test_daily_closures_placeholder_until_three_days_of_history():
    text = mm.build_daily(_daily(history_days=1, closed=[]))[0]
    assert "закрытия появятся" in text


def test_daily_who_came_without_salaries_with_leads():
    text = mm.build_daily(_daily())[0]
    block = text.split("Кто пришёл во фронт")[1]
    assert "React 1 · React Native 1" in block
    assert "Грейд: лиды 0 · senior 0 · middle 0 · junior/стажёр 0 · не указан 2" in block
    assert "₽" not in text and "зарплат" not in text.lower()


def test_daily_new_front_list_block():
    msgs = mm.build_daily(_daily())
    assert any("🆕 <b>Новые во фронте</b> (2)" in m for m in msgs)


def test_daily_has_no_cities():
    text = "\n".join(mm.build_daily(_daily()))
    assert "Москва" not in text and "СПб" not in text and "Россия" not in text


def test_checks_pass_footer():
    text = mm.build_daily(_daily())[0]
    assert "✅ Проверки пройдены" in text and "Не пересылать" not in text


@pytest.mark.parametrize("kw, reason", [
    (dict(found=2500, collected=2000), "2000"),
    (dict(collected=5, found=10), "собрано 5 из 10"),
    (dict(prev={"front": 100, "fullstack": 2, "ai_js": 0}), "Фронтенд"),
    (dict(unknown=[vac(77)]), "возраст"),
])
def test_checks_fail_loudly(kw, reason):
    text = mm.build_daily(_daily(**kw))[0]
    assert text.startswith("⚠️ <b>Не пересылать") and reason in text


def test_first_day_has_no_deltas():
    text = mm.build_daily(_daily(prev=None))[0]
    assert "<b>JS-рынок: 8</b>\n" in text and "Первый снимок" in text


def test_daily_messages_fit_telegram_and_escape_html():
    many = [(_front(1000 + i, name="Frontend <React> " * 5), 0.0) for i in range(300)]
    msgs = mm.build_daily(_daily(new=many))
    assert all(len(m) <= mm.TG_MESSAGE_LIMIT for m in msgs)
    assert all(m.count("<blockquote") == m.count("</blockquote>") for m in msgs)
    assert "&lt;React&gt;" in "\n".join(msgs)


# ── v4: хранилище и сквозной прогон ─────────────────────────────────────


class FakeApi:
    """Как API hh: справочники, постраничный поиск по запросам и «якорный» поиск без текста.

    Детальных карточек больше нет — любой запрос к /vacancies/{id} валит тест.
    """

    def __init__(self, listing, extra=None):
        self.listing, self.extra, self.calls = listing, extra or {}, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/areas":
            return AREAS
        if path == "/professional_roles":
            return PROF_ROLES
        assert path == "/vacancies", f"карточки не запрашиваем: {path}"
        assert "area" not in params, "собираем весь hh, без фильтра по стране"
        if "text" not in params:  # якорь: самый свежий номер в окне публикации
            end = datetime.fromisoformat(params["date_to"]) if "date_to" in params else self.now
            return {"items": [{"id": str(id_at(end - timedelta(minutes=1)))}], "found": 1, "pages": 1}
        if params.get("text") == mm.AI_QUERY:
            assert params.get("professional_role"), "AI-запрос только по IT-ролям"
        # 06.10: при сортировке по релевантности страницы hh повторяются — из 888 собиралось 731
        assert params.get("order_by") == "publication_time", "листать только по дате публикации"
        src = self.listing if params["text"] == mm.QUERY else self.extra.get(params["text"], [])
        page, per = params.get("page", 0), params.get("per_page", 100)
        return {"items": src[page * per:(page + 1) * per], "found": len(src), "pages": max(1, -(-len(src) // per))}


def _day(api_listing, now, store, sent, extra=None):
    api = FakeApi(api_listing, extra)
    api.now = now
    mm.run(api, store, now=now, send=sent.append, sleep=lambda s: None)
    return api


def _it(created: str, published: str | None = None, **kw):
    c = at(created)
    return item(id_at(c), published=(published or created), **kw)


def test_run_four_days_end_to_end(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    sent = []
    keep = _it("2026-10-01T10:00:00+0300", name="Frontend (Vue)")
    flick = _it("2026-10-01T11:00:00+0300", name="Frontend (Angular)")
    gone = _it("2026-10-01T12:00:00+0300", name="Frontend (React)", employer="Озон")
    d1 = datetime(2026, 10, 3, 8, 0, tzinfo=MSK)
    api = _day([keep, flick, gone], d1, store, sent)
    assert "Первый снимок" in sent[0]
    assert len([c for c in api.calls if c[0] == "/vacancies" and "text" not in c[1]]) == mm.ANCHOR_DAYS + 1  # 31 день + «сейчас»

    # день 2: flick мигнул, gone пропал; новая, поднятая (3 дня) и переоткрытая (45 дней) вакансии
    new = _it("2026-10-03T12:00:00+0300", name="Senior React Developer", employer="Сбер")
    bumped = _it("2026-09-30T12:00:00+0300", "2026-10-03T13:00:00+0300", name="Frontend (Vue)")
    reopened = _it("2026-08-19T12:00:00+0300", "2026-10-03T14:00:00+0300", name="Team Lead Frontend")
    store.save_anchors([(id_at(at("2026-08-19T12:00:00+0300")) - 10, at("2026-08-19T11:50:00+0300"), "card")])
    d2 = datetime(2026, 10, 4, 8, 0, tzinfo=MSK)
    sent.clear()
    api = _day([keep, new, bumped, reopened], d2, store, sent)
    assert len([c for c in api.calls if c[0] == "/vacancies" and "text" not in c[1]]) == 2  # докачан лишь вчерашний день + «сейчас»
    rows = {l.split()[0]: l.split()[1:] for l in sent[0].split("<pre>")[1].split("</pre>")[0].strip().splitlines()[1:]}
    assert rows["Фронтенд"][:3] == ["1", "1", "1"]
    assert "Временно скрыты из поиска: 2" in sent[0] and "закрытия появятся" in sent[0]

    d3 = datetime(2026, 10, 5, 8, 0, tzinfo=MSK)
    sent.clear()
    _day([keep, flick, new, bumped, reopened], d3, store, sent)  # flick вернулся
    assert "Временно скрыты из поиска: 1" in sent[0]

    d4 = datetime(2026, 10, 6, 8, 0, tzinfo=MSK)
    sent.clear()
    _day([keep, flick, new, bumped, reopened], d4, store, sent)
    rows = {l.split()[0]: l.split()[1:] for l in sent[0].split("<pre>")[1].split("</pre>")[0].strip().splitlines()[1:]}
    assert rows["Фронтенд"][3] == "1"  # gone: нет 3 дня подряд — закрыта; flick — нет
    assert "прожили в среднем" in sent[0]
    assert store.last_run().front_total == 5


def test_review_queue_and_overrides(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    store.record_titles([vac(1, name="Странный Front Инженер"), vac(2, name="Frontend-разработчик (React)")],
                        golden={"Frontend-разработчик (React)"}, now=NOW)
    queue = store.review_queue()
    assert [q["name"] for q in queue] == ["Странный Front Инженер"]
    store.review_submit([{"name": "Странный Front Инженер", "category": "other", "grade": None}])
    assert store.review_stats() == {"reviewed": 1, "agreed": 0, "disputed": 1}
    store.review_resolve([{"name": "Странный Front Инженер", "category": "other", "grade": None}])
    fixed = store.apply_overrides([vac(1, name="Странный Front Инженер")])
    assert fixed[0].category == "other"


def test_weekly_report_from_history(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    sent = []
    base = [_it(f"2026-09-20T1{i}:00:00+0300", name=f"Frontend (React) {i}", employer="Nitka") for i in range(3)]
    for d in range(8):
        now = datetime(2026, 9, 29, 8, 0, tzinfo=MSK) + timedelta(days=d)
        fresh = [_it((now - timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%S+0300"),
                     name="Frontend Developer (React/TypeScript)", employer="Nitka")]
        base = base + fresh
        _day(list(base), now, store, sent)
    msgs = mm.build_weekly(store, datetime(2026, 10, 6, 8, 5, tzinfo=MSK))
    text = msgs[0]
    assert "неделя 29.09–05.10" in text
    assert "Фронтенд: 4 → <b>11</b> (+7)" in text
    assert "Новые во фронте по дням" in text
    assert "Nitka" in text and "1 позиция" in text


def test_send_telegram_payload_shape(monkeypatch):
    captured = {}

    class Resp:
        ok = True

    def fake_post(url, json, timeout):
        captured.update(url=url, json=json)
        return Resp()

    monkeypatch.setenv("TELEGRAM_NOTIFY_BOT_TOKEN", "t0k")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "42")
    monkeypatch.setattr(mm.requests, "post", fake_post)
    mm.send("<b>привет</b>")
    assert captured["url"].endswith("/bott0k/sendMessage")
    assert captured["json"]["parse_mode"] == "HTML"


def test_run_api_failure_keeps_window_and_reports(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    sent = []

    class Broken:
        def get(self, path, **params):
            raise requests.ConnectionError("hh недоступен")

    with pytest.raises(requests.ConnectionError):
        mm.run(Broken(), store, now=NOW, send=sent.append, sleep=lambda s: None)
    assert store.last_run() is None
    assert len(sent) == 1 and "ошибк" in sent[0].lower()


def test_store_opens_production_v2_schema(tmp_path):
    """Прод-база market_v2.db создана v2/v3 — v4 обязана открыть её и дописать колонки."""
    import shutil
    src = ROOT / "tests" / "fixtures" / "market_v2_schema.sql"
    import sqlite3
    db = sqlite3.connect(tmp_path / "m.db")
    db.executescript(src.read_text())
    db.close()
    store = mm.Store(tmp_path / "m.db")
    assert store.last_run() is not None
    assert len(store.anchors().points) >= 1  # даты из старых карточек стали опорными точками




# ── контракт с живым API hh (только где есть токен: сервер/контейнер) ──


@pytest.mark.skipif(not os.getenv("MARKET_MONITOR_LIVE"), reason="живой контракт: MARKET_MONITOR_LIVE=1 и токен")
def test_live_hh_contract():
    api = mm.Api(mm.load_token())
    country = mm.country_index(api.get("/areas"))
    it_roles = mm.it_role_index(api.get("/professional_roles"))
    assert "96" in it_roles and "94" not in it_roles
    assert country.get("1") == "113" and country.get("1002") == "16"
    r = api.get("/vacancies", text=mm.QUERY, per_page=20, page=0)
    assert 300 < r["found"] < mm.HH_RESULTS_CAP, f"объединённый запрос: found={r['found']}"
    vs = [mm.parse_vacancy(i, country, it_roles) for i in r["items"]]
    assert all(v.published_at.tzinfo for v in vs)
    assert {v.category for v in vs} & {"front", "fullstack"}
    anchor = mm.fetch_max_id(api)
    assert anchor and anchor >= max(int(v.id) for v in vs) - 1_000_000  # номер свежей вакансии hh


# ── грейд, лиды, срез JS-рынка (06.10.2026) ─────────────────────────────


@pytest.mark.parametrize("name, category, grade", [
    ("Team Lead Frontend", "front", "lead"),
    ("Тимлид фронтенд-разработки", "front", "lead"),
    ("Руководитель frontend-разработки", "front", "lead"),
    ("Head of Frontend", "front", "lead"),
    ("Frontend-архитектор", "front", "lead"),
    ("Engineering Manager (Frontend)", "front", "lead"),
    ("Tech Lead (Fullstack, Node.js)", "fullstack", "lead"),
    ("Senior Frontend Developer (React)", "front", "senior"),
    ("Ведущий frontend-разработчик", "front", "senior"),
    ("Middle Frontend-разработчик (Vue)", "front", "middle"),
    ("Junior Frontend Developer", "front", "junior"),
    ("Стажёр-фронтенд-разработчик", "front", "junior"),
    ("Frontend-разработчик", "front", None),
    ("Руководитель проектов (веб)", "nontech", "lead"),
    # пойман глазами на живом срезе 06.10: лид-слово не должно перебивать «аналитик»
    ("Старший Full-stack аналитик / Team Lead аналитиков", "nontech", "lead"),
    ("Team Lead дизайнеров (UI/UX, Frontend-команда)", "nontech", "lead"),
])
def test_grade_and_lead_category(name, category, grade):
    assert mm.classify(name) == category
    assert mm.grade(name) == grade


def test_primary_stack_counts_each_front_vacancy_once():
    assert vac(1, name="Frontend (React / Angular)").primary_stack == "react"
    assert vac(2, name="Frontend-разработчик", snippet="Vue 3").primary_stack == "vue"
    assert vac(3, name="Frontend-разработчик").primary_stack == "js"


# ── полный просмотр живого среза 06.10: ошибки фронт-категории (208 вакансий) ──


@pytest.mark.parametrize("name, expected", [
    ("React\u00a0Native Middle Developer", "front"),           # неразрывный пробел; React Native — фронт
    ("Golang + React developer", "fullstack"),
    ("Разработчик Python/FastAPI + React/TypeScript", "fullstack"),
    ("Mod Developer / Python & React (Мир Танков)", "fullstack"),
    ("Web программист PHP / React", "fullstack"),
    ("Программист на Laravel, Vue.js", "fullstack"),
    ("Программист-разработчик (Vue.js / C#)", "fullstack"),
    ("Senior Java + Angular разработчик", "fullstack"),
    ("Тимлид разработки (C#/.NET + Angular, AI-assisted development )", "fullstack"),
    ("Старший Разработчик полного цикла (Python, Golang, React) [МТС Веб Сервисы]", "fullstack"),
    ("Разработчик WEB (PHP / Java / Frontend)", "fullstack"),
    ("Fulstack-разработчик Python, Vue.js (инфраструктурные сервисы)", "fullstack"),
    ("Senior DevOps Engineer(Front-end team)", "other"),
    ("Разработчик сайтов WordPress / Верстальщик", "other"),
    ("Reverse Engineer / Researcher JavaScript", "other"),
    ("Frontend-разработчик (Angular)", "front"),
    ("Frontend-разработчик / координатор проектов", "front"),
])
def test_front_review_0610(name, expected):
    assert mm.classify(name) == expected


def test_director_is_lead():
    assert mm.grade("Director of Frontend (Vue.JS) Engineering (управление через TLeads)") == "lead"


def test_primary_stack_prefers_title_over_snippet():
    v = vac(40, name="Разработчик (Фронтенд / Vue.js) Middle+", snippet="React будет плюсом")
    assert v.primary_stack == "vue"


# ── полный просмотр живого среза 06.10: ошибки fullstack (226 вакансий) ──


@pytest.mark.parametrize("name, expected", [
    ("Руководитель группы разработки (Ведущий Fullstack Developer PHP/JS + DevOps)", "fullstack"),
    ("Junior Fullstack-разработчик / DevOps / Системный инженер", "fullstack"),
    ("Senior DevOps Engineer(Front-end team)", "other"),
    ("ИИ-инженер полного цикла", "ai"),
    ("Старший Разработчик полного цикла (Python, Golang, React) [МТС Веб Сервисы]", "fullstack"),
    ("Ведущий системный инженер (Fullstack & Embedded)", "other"),
    ("FullStack разработчик Opencart", "fullstack_other"),
    ("Fullstack/Backend-разработчик Go / PHP — рекламная сеть ttarget", "fullstack_other"),
    ("Full Stack (Frontend + Backend) IT o\u2018qituvchi", "nontech"),
    ("Backend / Fullstack Developer (NestJS / TypeScript)", "fullstack"),
    ("Staff Fullstack/Backend Engineer", "fullstack"),
])
def test_fullstack_review_0610(name, expected):
    assert mm.classify(name) == expected


def test_internal_is_not_intern():
    assert mm.grade("Fullstack Developer (Internal Products & Automation)") is None
    assert mm.grade("Intern Front-end Developer") == "junior"



@pytest.mark.parametrize("name, expected", [
    ("JS стажёр", "front"),
    ("Lead/Senior Game Developer (Pixi.JS) / Ведущий разработчик", "front"),
    ("Automation Quality Assurance Engineer (JS/Playwright)", "qa"),
])
def test_excluded_review_0610(name, expected):
    assert mm.classify(name) == expected


# ── эталон: каждая будущая правка классификатора сверяется с просмотренной живой выдачей ──

GOLDEN = Path(__file__).parent / "fixtures" / "market_golden.tsv"


def test_golden_set_classification_is_exact():
    rows = [l.rstrip("\n").split("\t") for l in GOLDEN.read_text(encoding="utf-8").splitlines()
            if l and not l.startswith("#")]
    assert len(rows) >= 640
    wrong = [(n, c, mm.classify(n)) for n, c, _ in rows if mm.classify(n) != c]
    wrong_grade = [(n, g, mm.grade(n)) for n, _, g in rows if (mm.grade(n) or "-") != g]
    assert not wrong, f"категория разошлась с эталоном у {len(wrong)}: {wrong[:10]}"
    assert not wrong_grade, f"грейд разошёлся с эталоном у {len(wrong_grade)}: {wrong_grade[:10]}"


class RelevanceApi:
    """Как hh 06.10: без сортировки по дате страницы перекрываются, часть вакансий не видна."""

    def __init__(self, n):
        self.ids = [str(1000 + i) for i in range(n)]

    def get(self, path, **params):
        page = params["page"]
        if params.get("order_by") == "publication_time":
            chunk = self.ids[page * 100:(page + 1) * 100]
        else:
            chunk = self.ids[max(0, page * 100 - 20):page * 100 + 80]  # сдвиг выдачи между страницами
        return {"items": [item(i) for i in chunk], "found": len(self.ids), "pages": -(-len(self.ids) // 100)}


def test_fetch_items_collects_whole_listing():
    items, found = mm.fetch_items(RelevanceApi(888), mm.QUERY, sleep=lambda s: None)
    assert len(items) == found == 888


# ── просмотр полной выдачи 06.10 (после починки сортировки): +118 новых названий ──


@pytest.mark.parametrize("name, category, grade, stack", [
    ("Frontend-разработчик (аngular)", "front", None, "angular"),                 # кириллическая «а»
    ("Fullstack-разработчик (TypeScript / Node.js), Middlе", "fullstack", "middle", "js"),  # кириллическая «е»
    ("Fullstack-разработчик Middle (С#)", "fullstack_other", "middle", "js"),       # кириллическая «С»
    ("Разработчик frontend, Qt", "other", None, "js"),
    ("Старший разработчик frontend, Qt", "other", "senior", "js"),
    (".NET Frontend Developer (WPF / Avalonia UI)", "other", None, "js"),
])
def test_review_full_listing_0610(name, category, grade, stack):
    v = vac(60, name=name)
    assert (v.category, v.grade, v.primary_stack) == (category, grade, stack)


def test_latinize_only_touches_mixed_words():
    assert mm.latinize("Frontend-разработчик (аngular)") == "Frontend-разработчик (angular)"
    assert mm.latinize("Фронтенд-разработчик") == "Фронтенд-разработчик"   # чисто русское слово не трогаем
