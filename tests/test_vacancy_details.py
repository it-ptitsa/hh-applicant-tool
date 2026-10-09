"""Сбор полных карточек вакансий (описание, key_skills) для подбора групп резюме.

Пилот 09.10.2026: фронтенд с основным стеком React или Vue. Карточки — только через API (сайт
без входа вакансии больше не показывает), капчу решаем и продолжаем; без решения — стоп.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import market_monitor as mm  # noqa: E402
import vacancy_details as vd  # noqa: E402
from test_market_monitor import NOW, vac, _daily  # noqa: E402


def card(vid, skills=("React", "TypeScript"), exp="between1And3"):
    return {"id": str(vid), "name": f"Frontend {vid}", "employer": {"name": "Ромашка"},
            "description": "<p>Пишем на <b>React</b> и TypeScript</p>",
            "key_skills": [{"name": s} for s in skills], "experience": {"id": exp, "name": "1–3 года"},
            "work_format": [{"id": "REMOTE", "name": "Удалённо"}], "schedule": {"id": "fullDay"},
            "professional_roles": [{"id": "96"}], "salary": None, "archived": False}


CAPTCHA = {"errors": [{"value": "captcha_required", "captcha_url": "https://hh.ru/account/captcha?state=x"}]}


@pytest.fixture
def store(tmp_path):
    s = mm.Store(tmp_path / "m.db")
    listing = [vac(1, name="Frontend (React)"), vac(2, name="Frontend (Vue)"), vac(3, name="Frontend (Angular)"),
               vac(4, name="React Native разработчик"), vac(5, name="Fullstack (React + Node.js)"),
               vac(6, name="Frontend-разработчик", snippet="Vue 3")]
    s.save_run(_daily(listing=listing, found=6, collected=6, prev=None, flow=None, new=[], bumped=[],
                      reopened=[], closed=[], expired=[], hidden=[]), {})
    return s


def test_targets_are_front_react_and_vue_without_card(store):
    assert vd.targets(store) == ["1", "2", "6"]          # Angular, React Native и fullstack — не в пилоте
    store.save_detail("1", 200, card(1), NOW)
    assert vd.targets(store) == ["2", "6"]


def test_collect_saves_cards_with_pause(store):
    pauses = []
    stats = vd.collect(store, get=lambda vid: (200, card(vid)), solve=lambda url: True,
                       sleep=pauses.append, now=NOW, limit=10)
    assert stats == {"ok": 3, "gone": 0, "captcha_solved": 0}
    assert len(pauses) == 3 and all(vd.PAUSE[0] <= p <= vd.PAUSE[1] for p in pauses)
    row = store.detail("1")
    assert row["key_skills"] == ["React", "TypeScript"] and "React" in row["description_text"]
    assert json.loads(row["raw"])["experience"]["id"] == "between1And3"


def test_limit_is_respected(store):
    stats = vd.collect(store, get=lambda vid: (200, card(vid)), solve=lambda url: True,
                       sleep=lambda s: None, now=NOW, limit=2)
    assert stats["ok"] == 2


def test_gone_vacancy_is_marked_and_not_retried(store):
    vd.collect(store, get=lambda vid: (404, {}) if vid == "2" else (200, card(vid)), solve=lambda u: True,
               sleep=lambda s: None, now=NOW, limit=10)
    assert store.detail("2")["status"] == 404 and "2" not in vd.targets(store)


def test_captcha_solved_then_retried(store):
    calls = {"n": 0}

    def get(vid):
        calls["n"] += 1
        return (403, CAPTCHA) if calls["n"] == 1 else (200, card(vid))

    stats = vd.collect(store, get=get, solve=lambda url: url.endswith("state=x"), sleep=lambda s: None,
                       now=NOW, limit=10)
    assert stats == {"ok": 3, "gone": 0, "captcha_solved": 1}


def test_unsolved_captcha_stops_and_keeps_progress(store):
    def get(vid):
        return (200, card(vid)) if vid == "1" else (403, CAPTCHA)

    with pytest.raises(vd.CaptchaUnsolved):
        vd.collect(store, get=get, solve=lambda url: False, sleep=lambda s: None, now=NOW, limit=10)
    assert store.detail("1")["status"] == 200 and store.detail("2") is None   # не помечена «ушедшей»


def test_description_text_strips_html():
    assert vd.html_to_text("<p>Пишем на <b>React</b>&nbsp;и&amp;TS</p><ul><li>Vue</li></ul>") == "Пишем на React и&TS\nVue"


@pytest.mark.skipif(not __import__("os").getenv("MARKET_MONITOR_LIVE"), reason="живой контракт: MARKET_MONITOR_LIVE=1")
def test_live_card_contract():
    """Граница с API hh: карточка отдаёт поля, на которых строится анализ групп."""
    get, _ = vd.live_get_and_solve()
    api = mm.Api(mm.load_token())
    vid = api.get("/vacancies", text="frontend react", per_page=1, order_by="publication_time")["items"][0]["id"]
    status, body = get(vid)
    assert status == 200, body
    assert {"description", "key_skills", "experience", "name", "employer"} <= set(body)
