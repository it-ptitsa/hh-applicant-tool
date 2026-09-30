"""Мониторинг рынка фронтенд-вакансий: снимок выдачи → новые / переопубликованные / ушедшие.

Спека: openspec/changes/add-market-monitor. Главная ловушка, которую закрывают тесты:
в выдаче поиска hh `created_at == published_at` всегда, поэтому «новая» и
«переопубликованная» различаются только по `initial_created_at` из детальной карточки.
30.09.2026 из 40 вакансий «за сутки» новыми были 19, остальные 21 — переопубликованные.
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
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=MSK)
PREV_RUN = datetime(2026, 9, 30, 8, 0, tzinfo=MSK)


def item(
    vid,
    name="Frontend-разработчик",
    employer="ООО Ромашка",
    area="1",
    published="2026-09-30T20:00:00+0300",
    remote=False,
    salary=None,
):
    """Вакансия в том виде, в каком её отдаёт поиск hh (поля сверены с живым API 30.09)."""
    return {
        "id": str(vid),
        "name": name,
        "employer": {"id": "1", "name": employer},
        "area": {"id": area, "name": "Москва" if area == "1" else "Другой"},
        "work_format": [{"id": "REMOTE" if remote else "ON_SITE", "name": "..."}],
        "published_at": published,
        "created_at": published,
        "salary": salary,
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
    }


# ── разбор ──────────────────────────────────────────────────────────────


def test_parse_dt_accepts_hh_offset_without_colon():
    assert mm.parse_dt("2026-09-30T20:00:00+0300") == datetime(2026, 9, 30, 20, 0, tzinfo=MSK)


def test_parse_vacancy_reads_search_item():
    v = mm.parse_vacancy(
        item(10, remote=True, salary={"from": 200000, "to": None, "currency": "RUR"})
    )
    assert v.id == "10"
    assert v.employer == "ООО Ромашка"
    assert v.area_id == "1"
    assert v.remote is True
    assert v.salary_from == 200000 and v.salary_to is None and v.currency == "RUR"
    assert v.url == "https://hh.ru/vacancy/10"


def test_parse_vacancy_survives_missing_optional_fields():
    raw = item(11)
    raw["employer"] = None
    raw["work_format"] = None
    v = mm.parse_vacancy(raw)
    assert v.employer == "—"
    assert v.remote is False


# ── окно и классификация ────────────────────────────────────────────────


def test_first_run_window_is_last_24h():
    assert mm.window_start(None, NOW) == NOW - timedelta(hours=24)


def test_next_run_window_starts_at_previous_run():
    assert mm.window_start(PREV_RUN, NOW) == PREV_RUN


def test_candidates_are_published_inside_window():
    vs = [
        mm.parse_vacancy(item(1, published="2026-09-30T09:00:00+0300")),
        mm.parse_vacancy(item(2, published="2026-09-30T07:59:00+0300")),
    ]
    assert [v.id for v in mm.candidates(vs, PREV_RUN)] == ["1"]


def test_split_new_vs_republished_by_initial_created_at():
    new = mm.parse_vacancy(item(1, published="2026-09-30T10:00:00+0300"))
    old = mm.parse_vacancy(item(2, published="2026-09-30T11:00:00+0300"))
    initial = {
        "1": mm.parse_dt("2026-09-30T10:00:00+0300"),
        "2": mm.parse_dt("2026-09-09T11:00:00+0300"),
    }
    fresh, republished = mm.split_new_republished([new, old], initial, PREV_RUN)
    assert [v.id for v in fresh] == ["1"]
    assert [(v.id, age) for v, age in republished] == [("2", 21)]


def test_gone_ids_are_in_previous_snapshot_only():
    today = [mm.parse_vacancy(item(1)), mm.parse_vacancy(item(3))]
    assert sorted(mm.gone_ids({"1", "2", "4"}, today)) == ["2", "4"]


def test_split_gone_closed_vs_dropped():
    details = {"2": {"archived": True}, "4": {"archived": False}, "5": None}
    closed, dropped = mm.split_gone(["2", "4", "5"], details)
    assert sorted(closed) == ["2", "5"]  # архив или карточка недоступна
    assert dropped == ["4"]


# ── сводка ──────────────────────────────────────────────────────────────


def _report(**kw):
    base = dict(
        now=NOW,
        window_start=PREV_RUN,
        total=402,
        prev_total=397,
        moscow=228,
        spb=54,
        remote=185,
        new=[mm.parse_vacancy(item(1, name="React <Senior>", employer="Яндекс"))],
        republished=[(mm.parse_vacancy(item(2, employer="Сбер")), 21)],
        closed=[("3", "Frontend", "Озон")],
        dropped=[],
    )
    base.update(kw)
    return mm.Report(**base)


def test_summary_has_counts_delta_and_geography():
    text = mm.build_messages(_report())[0]
    assert "402" in text and "+5" in text
    assert "Москва 228" in text and "СПб 54" in text and "удалёнка 185" in text
    assert "Новые: <b>1</b>" in text
    assert "Переопубликованы: <b>1</b>" in text and "21 дн." in text
    assert "Ушли: <b>1</b>" in text


def test_first_run_has_no_delta():
    text = mm.build_messages(_report(prev_total=None))[0]
    assert "первый снимок" in text


def test_html_in_titles_is_escaped():
    joined = "\n".join(mm.build_messages(_report()))
    assert "React &lt;Senior&gt;" in joined
    assert "<Senior>" not in joined


def test_every_message_fits_telegram_limit():
    many = [mm.parse_vacancy(item(i, name="Очень длинное название " * 3)) for i in range(200)]
    msgs = mm.build_messages(_report(new=many, republished=[(v, 10) for v in many]))
    assert len(msgs) > 2
    assert all(len(m) <= mm.TG_MESSAGE_LIMIT for m in msgs)
    # каждый кусок списка — закрытый blockquote, HTML не рвётся посередине
    assert all(m.count("<blockquote") == m.count("</blockquote>") for m in msgs)


def test_top_employers_among_new():
    new = [mm.parse_vacancy(item(i, employer=e)) for i, e in enumerate(["А", "Б", "А", "В", "А", "Б"])]
    assert mm.top_employers(new, 2) == [("А", 3), ("Б", 2)]


# ── хранилище: настоящая SQLite ─────────────────────────────────────────


def test_store_roundtrip(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    assert store.last_run() is None

    vs = [mm.parse_vacancy(item(1)), mm.parse_vacancy(item(2))]
    rid = store.save_run(_report(), vs)
    store.save_initial({"1": mm.parse_dt("2026-09-01T10:00:00+0300")})

    reopened = mm.Store(tmp_path / "market.db")
    last = reopened.last_run()
    assert last.id == rid and last.run_at == NOW and last.total == 402
    assert reopened.snapshot_ids(rid) == {"1", "2"}
    assert reopened.snapshot_titles(rid, ["2"]) == {"2": ("Frontend-разработчик", "ООО Ромашка")}
    assert reopened.cached_initial(["1", "2"]) == {"1": mm.parse_dt("2026-09-01T10:00:00+0300")}


# ── сквозной run(): фейковый API hh, настоящая SQLite, перехват Telegram ─


class FakeApi:
    """Отвечает как API hh: постраничный поиск и детальные карточки, 404 для удалённых."""

    def __init__(self, listing, details):
        self.listing = listing
        self.details = details
        self.calls = []

    def get(self, path, **params):
        self.calls.append((path, params))
        if path == "/vacancies":
            page, per = params.get("page", 0), params.get("per_page", 100)
            chunk = self.listing[page * per : (page + 1) * per]
            pages = max(1, -(-len(self.listing) // per))
            return {"items": chunk, "found": len(self.listing), "pages": pages, "page": page}
        vid = path.rsplit("/", 1)[-1]
        if vid not in self.details:
            resp = requests.Response()
            resp.status_code = 404
            raise requests.HTTPError("404", response=resp)
        return self.details[vid]


def test_run_two_days_end_to_end(tmp_path):
    store = mm.Store(tmp_path / "market.db")
    sent = []

    # день 1: первый снимок
    day1 = [item(1, published="2026-09-30T07:00:00+0300"), item(2), item(3)]
    api = FakeApi(day1, {vid: {"initial_created_at": "2026-09-01T10:00:00+0300"} for vid in "123"})
    mm.run(api, store, now=PREV_RUN, send=sent.append, sleep=lambda s: None)
    assert "первый снимок" in sent[0]

    # день 2: 1 осталась, 2 ушла в архив, 3 выпала из выдачи, 4 новая, 5 переопубликована
    day2 = [
        item(1, published="2026-09-30T07:00:00+0300"),
        item(4, published="2026-09-30T12:00:00+0300", remote=True),
        item(5, published="2026-09-30T13:00:00+0300", area="2"),
    ]
    details = {
        "2": {"archived": True},
        "3": {"archived": False},
        "4": {"initial_created_at": "2026-09-30T12:00:00+0300"},
        "5": {"initial_created_at": "2026-09-09T13:00:00+0300"},
    }
    api = FakeApi(day2, details)
    sent.clear()
    mm.run(api, store, now=NOW, send=sent.append, sleep=lambda s: None)

    summary = sent[0]
    assert "Всего в выдаче: <b>3</b>" in summary and "(+0" in summary
    assert "Новые: <b>1</b>" in summary
    assert "Переопубликованы: <b>1</b>" in summary and "21 дн." in summary
    assert "Ушли: <b>2</b>" in summary and "закрыты 1" in summary and "выпали из выдачи 1" in summary
    assert "удалёнка 1" in summary and "СПб 1" in summary

    # карточку вакансии 1 не запрашивали: она вне окна, и initial уже в кэше не нужен
    detail_paths = [p for p, _ in api.calls if p != "/vacancies"]
    assert "/vacancies/1" not in detail_paths

    last = store.last_run()
    assert last.run_at == NOW and store.snapshot_ids(last.id) == {"1", "4", "5"}


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


def test_send_telegram_payload_shape(monkeypatch):
    """Граница с Telegram: HTML-разметка и отключённое превью ссылок."""
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
    assert captured["json"]["chat_id"] == "42"
    assert captured["json"]["parse_mode"] == "HTML"
    assert captured["json"]["disable_web_page_preview"] is True


# ── контракт с живым API hh (только где есть токен: сервер/контейнер) ──


def _token():
    try:
        return mm.load_token()
    except BaseException:  # load_token делает sys.exit без токена
        return None


@pytest.mark.skipif(
    not os.getenv("MARKET_MONITOR_LIVE"), reason="живой контракт: MARKET_MONITOR_LIVE=1 и токен"
)
def test_live_hh_contract():
    token = _token()
    assert token, "нет токена в CONFIG_DIR/config.json"
    api = mm.Api(token)
    r = api.get("/vacancies", text=mm.QUERY, area=mm.AREA, per_page=5, page=0)
    assert r["found"] > 50, "фронтенд-выдача подозрительно пустая"
    v = mm.parse_vacancy(r["items"][0])
    assert v.published_at.tzinfo is not None
    detail = api.get(f"/vacancies/{v.id}")
    assert mm.parse_dt(detail["initial_created_at"]) <= v.published_at
