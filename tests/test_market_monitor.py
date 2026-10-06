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
    ("Разработчик React Native", "mobile"),
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
    assert mm.JS_MARKET == {"front", "fullstack", "backend", "web", "ai_js"}
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


# ── окно и классификация по возрасту ────────────────────────────────────


def test_first_run_window_is_last_24h():
    assert mm.window_start(None, NOW) == NOW - timedelta(hours=24)


def test_next_run_window_starts_at_previous_run():
    assert mm.window_start(PREV_RUN, NOW) == PREV_RUN


def test_candidates_are_published_inside_window():
    vs = [vac(1, published="2026-10-05T09:00:00+0300"), vac(2, published="2026-10-05T07:59:00+0300")]
    assert [v.id for v in mm.candidates(vs, PREV_RUN)] == ["1"]


def test_split_by_age_new_bumped_reopened():
    new = vac(1, published="2026-10-05T10:00:00+0300")
    bumped = vac(2, published="2026-10-05T11:00:00+0300")
    reopened = vac(3, published="2026-10-05T12:00:00+0300")
    unknown = vac(4, published="2026-10-05T13:00:00+0300")
    initial = {
        "1": mm.parse_dt("2026-10-05T10:00:00+0300"),
        "2": mm.parse_dt("2026-10-02T11:00:00+0300"),  # 3 дня — поднятие
        "3": mm.parse_dt("2026-08-21T12:00:00+0300"),  # 45 дней — переоткрытие
    }
    n, b, r = mm.split_by_age([new, bumped, reopened, unknown], initial, PREV_RUN)
    assert [v.id for v in n] == ["1", "4"]  # без карточки — считаем новой
    assert [(v.id, a) for v, a in b] == [("2", 3)]
    assert [(v.id, a) for v, a in r] == [("3", 45)]


def test_reopen_threshold_is_30_days():
    v = vac(5, published="2026-10-05T12:00:00+0300")
    exactly = {"5": mm.parse_dt("2026-09-05T12:00:00+0300")}
    _, bumped, reopened = mm.split_by_age([v], exactly, PREV_RUN)
    assert bumped == [] and [(x.id, a) for x, a in reopened] == [("5", 30)]


def test_gone_ids_are_in_previous_snapshot_only():
    assert sorted(mm.gone_ids({"1", "2", "4"}, [vac(1), vac(3)])) == ["2", "4"]


def test_split_gone_closed_vs_hidden():
    details = {"2": {"archived": True}, "4": {"archived": False}, "5": None}
    closed, hidden = mm.split_gone(["2", "4", "5"], details)
    assert sorted(closed) == ["2", "5"]
    assert hidden == ["4"]


def test_unique_positions_collapses_multi_city_copies():
    """05.10: Nitka разместила одну позицию в 8 городах подряд идущими id — это 1 позиция."""
    copies = [vac(i, name="Frontend Developer (React/TypeScript)", employer="Nitka") for i in range(8)]
    other = [vac(100, name="Frontend Developer (React/TypeScript)", employer="Другая")]
    assert mm.unique_positions(copies + other) == 2


def test_summary_shows_unique_positions_when_duplicates():
    dup = [vac(i, name="Frontend Developer", employer="Nitka") for i in range(5)]
    text = mm.build_messages(_report(new=dup))[0]
    assert "Новые: <b>5</b> (уникальных позиций 1)" in text


def test_top_employers():
    vs = [vac(i, employer=e) for i, e in enumerate("АБАВАБ")]
    assert mm.top_employers(vs, 2) == [("А", 3), ("Б", 2)]


# ── сводка ──────────────────────────────────────────────────────────────


def _listing():
    return [
        vac(1),                                   # front, Москва
        vac(2, area="2", remote=True),            # front, СПб, удалёнка
        vac(3, area="1002"),                      # front, Минск
        vac(4, name="Fullstack (React + Node)"),  # fullstack
        vac(5, name="AQA TypeScript"),            # qa — не в JS-рынке
    ]


def _report(**kw):
    listing = _listing()
    base = dict(
        now=NOW, window_start=PREV_RUN, listing=listing, found=len(listing),
        prev_front=2, prev_js=5,
        new=[vac(1, name="React <Senior>", employer="Яндекс"), vac(4, name="Fullstack (React + Node)")],
        bumped=[(vac(2), 3)],
        reopened=[(vac(3), 45)],
        closed=[("9", "Frontend", "Озон", "front")],
        hidden=["8"],
    )
    base.update(kw)
    return mm.Report(**base)


def test_summary_front_counts_and_geography():
    text = mm.build_messages(_report())[0]
    assert "Фронтенд: <b>3</b> (+1" in text
    assert "Россия 2 (Москва 1 · СПб 1) · другие страны 1 · удалёнка 1" in text
    assert "JS-рынок" in text and "<b>4</b> (-1" in text  # front 3 + fullstack 1; qa не входит


def test_summary_front_events_split():
    text = mm.build_messages(_report())[0]
    assert "Новые: <b>1</b>" in text           # только фронт; fullstack-новая — в строке JS-рынка
    assert "Подняли: <b>1</b>" in text
    assert "Переоткрыли: <b>1</b>" in text and "45 дн." in text
    assert "Закрыты: <b>1</b>" in text
    assert "Временно скрыты из поиска: 1" in text


def test_summary_js_market_line():
    text = mm.build_messages(_report())[0]
    assert "JS-рынок за то же время: новые 2" in text


def _report_mix():
    listing = [
        vac(1, name="Frontend (React)"), vac(2, name="Frontend", snippet="Vue 3"),
        vac(3, name="Frontend (React / Angular)"), vac(4, name="Frontend-разработчик"),
        vac(5, name="Frontend (AI-native) разработчик"),
        vac(6, name="Fullstack (React + Node.js)"), vac(7, name="Fullstack", snippet="PHP"),
        vac(8, name="Fullstack AI Engineer", snippet="Node.js"),
        vac(9, name="AI-инженер (JS)"), vac(10, name="Senior AI developer (Python)"),
    ]
    return _report(listing=listing, found=len(listing), prev_front=None, prev_js=None,
                   new=[], bumped=[], reopened=[], closed=[], hidden=[])


def test_summary_front_stack_line():
    text = mm.build_messages(_report_mix())[0]
    assert "Стек фронта: React 1 · Vue 1 · Angular 0 · Svelte 0 · несколько 1 · не указан 2" in text


def test_summary_fullstack_with_node():
    text = mm.build_messages(_report_mix())[0]
    assert "Fullstack: <b>3</b> · с Node.js 2" in text


def test_summary_ai_block():
    text = mm.build_messages(_report_mix())[0]
    assert "AI ближе к фронту: <b>3</b>" in text
    assert "фронт с AI 1 · fullstack с AI 1 · AI-инженеры на JS/TS 1" in text
    assert "всего AI-вакансий в IT: 4" in text


def test_first_run_has_no_delta():
    text = mm.build_messages(_report(prev_front=None, prev_js=None))[0]
    assert "первый снимок" in text


def test_cap_warning_when_hh_hides_tail():
    text = mm.build_messages(_report(found=2500))[0]
    assert "2000" in text


def test_html_in_titles_is_escaped():
    joined = "\n".join(mm.build_messages(_report()))
    assert "React &lt;Senior&gt;" in joined and "<Senior>" not in joined


def test_every_message_fits_telegram_limit():
    many = [vac(i, name="Очень длинное название фронтенд " * 3) for i in range(300)]
    msgs = mm.build_messages(_report(new=many, reopened=[(v, 40) for v in many]))
    assert len(msgs) > 2
    assert all(len(m) <= mm.TG_MESSAGE_LIMIT for m in msgs)
    assert all(m.count("<blockquote") == m.count("</blockquote>") for m in msgs)


# ── хранилище: настоящая SQLite ─────────────────────────────────────────


def test_store_roundtrip(tmp_path):
    store = mm.Store(tmp_path / "market_v2.db")
    assert store.last_run() is None
    rid = store.save_run(_report(), _listing())
    store.save_initial({"1": mm.parse_dt("2026-09-01T10:00:00+0300")})

    reopened = mm.Store(tmp_path / "market_v2.db")
    last = reopened.last_run()
    assert last.id == rid and last.run_at == NOW
    assert last.front_total == 3 and last.js_total == 4
    assert reopened.snapshot_ids(rid) == {"1", "2", "3", "4", "5"}
    assert reopened.snapshot_titles(rid, ["4"]) == {"4": ("Fullstack (React + Node)", "ООО Ромашка", "fullstack")}
    assert reopened.cached_initial(["1", "2"]) == {"1": mm.parse_dt("2026-09-01T10:00:00+0300")}


def test_store_migrates_old_v2_schema(tmp_path):
    """market_v2.db на проде создан без колонок stack/node/ai — открытие не должно падать."""
    import sqlite3
    path = tmp_path / "market_v2.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE snapshot (run_id INTEGER, vacancy_id TEXT, name TEXT, employer TEXT, category TEXT,"
               " area_id TEXT, country_id TEXT, remote INTEGER, published_at TEXT, salary_from INTEGER,"
               " salary_to INTEGER, currency TEXT, PRIMARY KEY (run_id, vacancy_id))")
    db.commit(); db.close()
    store = mm.Store(path)
    rid = store.save_run(_report(), _listing())
    assert store.snapshot_ids(rid) == {"1", "2", "3", "4", "5"}


# ── сквозной run(): фейковый API hh, настоящая SQLite, перехват Telegram ─


class FakeApi:
    """Отвечает как API hh: /areas, постраничный поиск, детальные карточки, 404 для удалённых."""

    def __init__(self, listing, details, extra=None):
        self.listing, self.details, self.calls = listing, details, []
        self.extra = extra or {}

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/areas":
            return AREAS
        if path == "/professional_roles":
            return PROF_ROLES
        if path == "/vacancies":
            assert "area" not in params, "собираем весь hh, без фильтра по стране"
            if params.get("text") == mm.AI_QUERY:
                assert params.get("professional_role"), "AI-запрос только по IT-ролям"
            src = self.listing if params.get("text") == mm.QUERY else self.extra.get(params.get("text"), [])
            page, per = params.get("page", 0), params.get("per_page", 100)
            pages = max(1, -(-len(src) // per))
            return {"items": src[page * per:(page + 1) * per], "found": len(src), "pages": pages, "page": page}
        vid = path.rsplit("/", 1)[-1]
        if vid not in self.details:
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError("404", response=resp)
        return self.details[vid]


def test_run_two_days_end_to_end(tmp_path):
    store = mm.Store(tmp_path / "market_v2.db")
    sent = []
    old = {"initial_created_at": "2026-08-01T10:00:00+0300"}

    day1 = [item(1, published="2026-10-05T07:00:00+0300"), item(2), item(3), item(6, name="AQA TypeScript")]
    mm.run(FakeApi(day1, {k: old for k in "1236"}), store, now=PREV_RUN, send=sent.append, sleep=lambda s: None)
    assert "первый снимок" in sent[0]

    # день 2: 1 осталась · 2 закрыта · 3 мигнула · 4 новая · 5 поднятая · 7 переоткрытая · 8 новый fullstack
    day2 = [
        item(1, published="2026-10-05T07:00:00+0300"),
        item(4, published="2026-10-05T12:00:00+0300", remote=True),
        item(5, published="2026-10-05T13:00:00+0300", area="2"),
        item(7, published="2026-10-05T14:00:00+0300", area="1002"),
        item(8, name="Fullstack (React + Node.js)", published="2026-10-05T15:00:00+0300"),
        item(6, name="AQA TypeScript", published="2026-10-05T07:00:00+0300"),
    ]
    details = {
        "2": {"archived": True},
        "3": {"archived": False},
        "4": {"initial_created_at": "2026-10-05T12:00:00+0300"},
        "5": {"initial_created_at": "2026-10-02T13:00:00+0300"},
        "7": {"initial_created_at": "2026-08-21T14:00:00+0300"},
        "8": {"initial_created_at": "2026-10-05T15:00:00+0300"},
    }
    extra = {
        mm.AI_QUERY: [item(20, name="AI-инженер (JS)", published="2026-10-05T07:00:00+0300"),
                      item(21, name="Senior AI developer (Python)", published="2026-10-05T07:00:00+0300")],
        mm.FULL_NODE_QUERY: [item(8, name="Fullstack (React + Node.js)", published="2026-10-05T15:00:00+0300")],
    }
    api = FakeApi(day2, details, extra)
    sent.clear()
    mm.run(api, store, now=NOW, send=sent.append, sleep=lambda s: None)

    s = sent[0]
    assert "Фронтенд: <b>4</b> (+1" in s                 # 1, 4, 5, 7 (было 1, 2, 3)
    assert "Новые: <b>1</b>" in s                        # 4
    assert "Подняли: <b>1</b>" in s                      # 5, 3 дня
    assert "Переоткрыли: <b>1</b>" in s and "45 дн." in s  # 7
    assert "Закрыты: <b>1</b>" in s                      # 2
    assert "Временно скрыты из поиска: 1" in s           # 3
    assert "Россия 3 (Москва 2 · СПб 1) · другие страны 1 · удалёнка 1" in s
    assert "JS-рынок за то же время: новые 2" in s       # 4 и fullstack 8

    detail_paths = [p for p, _ in api.calls if p.startswith("/vacancies/")]
    assert "/vacancies/1" not in detail_paths  # вне окна — карточка не нужна
    last = store.last_run()
    assert "Fullstack: <b>1</b> · с Node.js 1" in s
    assert "AI-инженеры на JS/TS 1" in s and "всего AI-вакансий в IT: 2" in s
    assert last.run_at == NOW and last.front_total == 4 and last.js_total == 6  # + ai_js


def _http_error(status, body):
    resp = requests.Response()
    resp.status_code = status
    resp._content = body.encode()
    return requests.HTTPError(str(status), response=resp)


class CaptchaApi(FakeApi):
    """05.10.2026: поиск отвечает, а карточки — 403 captcha_required на весь аккаунт."""

    def get(self, path, **params):
        if path.startswith("/vacancies/"):
            raise _http_error(403, '{"errors":[{"value":"captcha_required","captcha_url":"https://hh.ru/account/captcha?state=x"}]}')
        return super().get(path, **params)


def test_captcha_on_details_fails_loudly_instead_of_marking_new(tmp_path):
    """Раньше 403 капчи считался «карточка недоступна»: все кандидаты → новые, пропавшие → закрытые."""
    store = mm.Store(tmp_path / "market_v2.db")
    sent = []
    with pytest.raises(mm.CaptchaRequired):
        mm.run(CaptchaApi([item(1)], {}), store, now=NOW, send=sent.append, sleep=lambda s: None)
    assert store.last_run() is None  # испорченный снимок не сохраняется
    assert len(sent) == 1 and "капч" in sent[0].lower()


def test_fetch_detail_404_is_gone_but_captcha_raises():
    class Api404:
        def get(self, path, **params):
            raise _http_error(404, '{"errors":[{"type":"not_found"}]}')

    assert mm.fetch_detail(Api404(), "1") is None
    with pytest.raises(mm.CaptchaRequired):
        mm.fetch_detail(CaptchaApi([], {}), "1")


def test_run_api_failure_keeps_window_and_reports(tmp_path):
    store = mm.Store(tmp_path / "market_v2.db")
    sent = []

    class Broken:
        def get(self, path, **params):
            raise requests.ConnectionError("hh недоступен")

    with pytest.raises(requests.ConnectionError):
        mm.run(Broken(), store, now=NOW, send=sent.append, sleep=lambda s: None)
    assert store.last_run() is None
    assert len(sent) == 1 and "ошибк" in sent[0].lower()


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
    assert captured["json"]["disable_web_page_preview"] is True


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
    detail = api.get(f"/vacancies/{vs[0].id}")
    assert mm.parse_dt(detail["initial_created_at"]) <= vs[0].published_at


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


def test_snapshot_message_sums_and_lines():
    listing = [
        vac(1, name="Frontend (React)"), vac(2, name="Team Lead Frontend", snippet="Vue"),
        vac(3, name="Frontend (AI-native) разработчик"),
        vac(4, name="Fullstack (React + Node.js)"), vac(5, name="Tech Lead (Fullstack, Node.js)"),
        vac(6, name="AI-инженер (JS)"), vac(7, name="AQA TypeScript"), vac(8, name="Backend-разработчик (Node.js)"),
    ]
    text = mm.build_snapshot_message(listing, NOW)
    assert "JS-рынок: <b>6</b>" in text                       # 3 фронт + 2 fullstack + 1 AI
    assert "Фронтенд: <b>3</b>" in text and "лидов 1" in text and "с AI 1" in text
    assert "React 1" in text and "Vue 1" in text and "Angular 0" in text and "JS/TS без фреймворка 1" in text
    assert "Fullstack: <b>2</b> · с Node.js 2" in text
    assert "AI-инженеры на JS/TS: <b>1</b>" in text
    assert "Москва" not in text and "Россия" not in text      # без городов
    assert "lead 1" in text or "лиды 1" in text


# ── полный просмотр живого среза 06.10: ошибки фронт-категории (208 вакансий) ──


@pytest.mark.parametrize("name, expected", [
    ("React\u00a0Native Middle Developer", "mobile"),          # неразрывный пробел в названии hh
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
