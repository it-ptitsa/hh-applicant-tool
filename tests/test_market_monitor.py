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
         published="2026-10-05T20:00:00+0300", remote=False, salary=None, roles=("96",)):
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
    }


def vac(vid, **kw):
    return mm.parse_vacancy(item(vid, **kw), COUNTRY, IT_ROLES)


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
    assert mm.JS_MARKET == {"front", "fullstack", "backend", "web"}
    assert "fullstack_other" not in mm.JS_MARKET  # fullstack на .NET/Java/PHP — не JS-рынок


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


# ── сквозной run(): фейковый API hh, настоящая SQLite, перехват Telegram ─


class FakeApi:
    """Отвечает как API hh: /areas, постраничный поиск, детальные карточки, 404 для удалённых."""

    def __init__(self, listing, details):
        self.listing, self.details, self.calls = listing, details, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/areas":
            return AREAS
        if path == "/professional_roles":
            return PROF_ROLES
        if path == "/vacancies":
            assert "area" not in params, "собираем весь hh, без фильтра по стране"
            page, per = params.get("page", 0), params.get("per_page", 100)
            pages = max(1, -(-len(self.listing) // per))
            return {"items": self.listing[page * per:(page + 1) * per],
                    "found": len(self.listing), "pages": pages, "page": page}
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
    api = FakeApi(day2, details)
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
    assert last.run_at == NOW and last.front_total == 4 and last.js_total == 5


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
