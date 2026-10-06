#!/usr/bin/env python3
"""Ежедневный мониторинг рынка фронтенд-вакансий на hh → сводка в Telegram.

Снимает выдачу по всему hh одним запросом-объединением (фронт, fullstack, Node, общий
веб), сам присваивает каждой вакансии категорию по названию и сравнивает с прошлым
снимком: новые, поднятые, переоткрытые, закрытые, временно скрытые.

Почему так (подтверждено данными 30.09–05.10.2026):
- в поиске hh `created_at == published_at`, новизна видна только по `initial_created_at`
  из детальной карточки — её и кэшируем;
- «переопубликованные» — это два явления: платное поднятие свежей вакансии и переоткрытие
  старой (≥ 30 дней). Считаем отдельно;
- 2/3 вакансий, «выпавших из выдачи», возвращались через 1–2 дня с той же датой —
  hh временно их прячет. Ушедшими считаем только закрытые (архив / карточка недоступна);
- фронт-слова в названии цепляют QA на TS, React Native и Node-бэкенды (38% среза) —
  поэтому собираем шире, а «Фронтенд» отбираем классификатором.

Спека: openspec/specs/market-monitor. Запуск на сервере — market_monitor.sh (крон хоста).
Отклики не отправляет, состояние аккаунта не меняет: только GET к API.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sqlite3
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, NamedTuple

import requests

from chat_digest import CONFIG_DIR, Api, load_token

# Объединение срезов в один запрос (вместе ~850 вакансий — в пределах 2000 выдачи hh).
# Без «верстальщик» (полиграфия). Написания vue.js/vuejs/angularjs hh не склеивает с vue/angular —
# 06.10 «Vue.js разработчик» прошёл мимо. «next.js» в кавычках 06.10: 13 вакансий, шума 0.
QUERY = (
    "NAME:(frontend OR фронтенд OR front-end OR react OR vue OR angular OR typescript OR javascript"
    ' OR "vue.js" OR vuejs OR angularjs OR "next.js" OR nextjs'
    ' OR nuxt OR svelte OR "ui разработчик" OR "ui-разработчик" OR "разработчик интерфейсов" OR "ui developer"'
    ' OR fullstack OR "full stack" OR full-stack OR фулстек OR фуллстек'
    " OR node OR nodejs OR nestjs"
    ' OR "веб-разработчик" OR "web-разработчик" OR "web разработчик" OR "веб разработчик"'
    ' OR "web developer" OR "веб-программист" OR "web-программист" OR "веб программист")'
)
RUSSIA = "113"
AREA_MOSCOW = "1"
AREA_SPB = "2"
PER_PAGE = 100
HH_RESULTS_CAP = 2000
DETAIL_DELAY = 0.3
REOPEN_AGE_DAYS = 30

MSK = timezone(timedelta(hours=3))
TG_MESSAGE_LIMIT = 4000
LIST_TITLE_LIMIT = 90

# ── категории по названию ───────────────────────────────────────────────

_QA = re.compile(r"\bqa\b|aqa|тестиров|автотест|\bsdet\b|test engineer", re.I)
_NONTECH = re.compile(
    r"аналитик|analyst|дизайнер|designer|менеджер|manager|преподават|учител|наставник|ментор"
    r"|teacher|tutor|рекрутер|recruit|product owner|продакт|scrum", re.I)
_MOBILE = re.compile(r"react native|android|\bios\b|flutter|mobile|мобильн", re.I)
_FULLSTACK = re.compile(r"full.?stack|ful+.?стек|фул+.?стек", re.I)
_BACKEND = re.compile(r"backend|back-end|бэкенд|бекенд|серверн|\bnode|\bnest", re.I)
_FRONT_STRONG = re.compile(
    r"front|фронт|react|\bvue|angular|svelte|nuxt|next\.?js|ui\s*-?\s*(разработ|developer)"
    r"|интерфейс|верст|html", re.I)
# Голое «JS» не годится: 05.10 во фронт попал «Машинист экскаватора (JCB JS 260)».
_FRONT_LANG = re.compile(
    r"typescript|javascript|\bjs\s*-?\s*(разработ|программ|developer|engineer)"
    r"|\bts\s*-?\s*(разработ|developer)", re.I)
# Fullstack на чужом бэкенде без фронт-стека в названии — не JS-рынок.
_NON_JS_STACK = re.compile(
    r"\.net|c#|java\b|kotlin|python|django|php|laravel|golang|\bgo\b|c\+\+|ruby|delphi"
    r"|битрикс|bitrix|wordpress|\b1с\b|\b1c\b", re.I)
_CMS = re.compile(r"битрикс|bitrix|wordpress|\b1с\b|\b1c\b|drupal|opencart|joomla|modx|tilda", re.I)
_WEB = re.compile(r"веб|web", re.I)

JS_MARKET = {"front", "fullstack", "backend", "web"}
CATEGORY_LABEL = {"front": "фронт", "fullstack": "fullstack", "backend": "Node/бэкенд", "web": "веб"}


def classify(name: str) -> str:
    """Категория вакансии по названию. Порядок важен: QA на TS — это QA, а не фронт."""
    if _QA.search(name):
        return "qa"
    if _NONTECH.search(name):
        return "nontech"
    if _MOBILE.search(name):
        return "mobile"
    backend = bool(_BACKEND.search(name))
    strong = bool(_FRONT_STRONG.search(name))
    if _FULLSTACK.search(name):
        # внутри fullstack голое «JS» — это JavaScript («Full-stack Developer (PHP / JS)»)
        js_stack = strong or backend or _FRONT_LANG.search(name) or re.search(r"\bjs\b", name, re.I)
        if not js_stack and _NON_JS_STACK.search(name):
            return "fullstack_other"
        return "fullstack"
    if backend and strong:  # «Frontend / Node.js developer» — это fullstack
        return "fullstack"
    if backend:  # язык (TypeScript/JavaScript) без явного фронта — бэкенд
        return "backend"
    if strong:
        return "front"
    if _FRONT_LANG.search(name):
        # «Программист .net (C#, …, Javascript)» — JS довеском к чужому стеку, не фронт
        return "other" if _NON_JS_STACK.search(name) else "front"
    if _CMS.search(name):  # Битрикс, WordPress — PHP/CMS, не JS-рынок
        return "other"
    if _WEB.search(name):
        return "web"
    return "other"


# ── модель ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Vacancy:
    id: str
    name: str
    employer: str
    area_id: str
    country_id: str
    remote: bool
    category: str
    published_at: datetime
    salary_from: int | None
    salary_to: int | None
    currency: str | None
    url: str


Closed = tuple  # (id, название, работодатель, категория)


@dataclass
class Report:
    now: datetime
    window_start: datetime
    listing: list[Vacancy]
    found: int
    prev_front: int | None
    prev_js: int | None
    new: list[Vacancy] = field(default_factory=list)
    bumped: list[tuple[Vacancy, int]] = field(default_factory=list)
    reopened: list[tuple[Vacancy, int]] = field(default_factory=list)
    closed: list[Closed] = field(default_factory=list)
    hidden: list = field(default_factory=list)  # id или (id, категория)

    @property
    def front(self) -> list[Vacancy]:
        return [v for v in self.listing if v.category == "front"]

    @property
    def js(self) -> list[Vacancy]:
        return [v for v in self.listing if v.category in JS_MARKET]

    def geo(self) -> dict[str, int]:
        f = self.front
        russia = [v for v in f if v.country_id == RUSSIA]
        return {
            "russia": len(russia),
            "moscow": sum(v.area_id == AREA_MOSCOW for v in russia),
            "spb": sum(v.area_id == AREA_SPB for v in russia),
            "other": len(f) - len(russia),
            "remote": sum(v.remote for v in f),
        }

    def hidden_in(self, cats: set[str] | None = None) -> int:
        out = 0
        for h in self.hidden:
            cat = h[1] if isinstance(h, tuple) else None
            if cats is None or cat is None or cat in cats:
                out += 1
        return out


class Run(NamedTuple):
    id: int
    run_at: datetime
    front_total: int
    js_total: int


def parse_dt(value: str) -> datetime:
    """Дата из API hh: `2026-09-30T20:00:00+0300` (смещение без двоеточия)."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def country_index(areas: list[dict]) -> dict[str, str]:
    """id любого региона → id его страны по справочнику /areas."""
    index: dict[str, str] = {}

    def walk(nodes: list[dict], country: str) -> None:
        for node in nodes:
            index[str(node["id"])] = country
            walk(node.get("areas") or [], country)

    for c in areas:
        index[str(c["id"])] = str(c["id"])
        walk(c.get("areas") or [], str(c["id"]))
    return index


OTHER_ROLE = "40"  # «Другое» — туда кладут, например, «Фронтенд инженер для десктопных приложений»


def it_role_index(professional_roles: dict) -> set[str]:
    """Роли IT-категории hh из справочника /professional_roles плюс «Другое».

    Ролевой фильтр нужен против слов-совпадений: 06.10 во «Фронтенд» попали «Повар в
    ресторан „La Vue“» и уборщица оттуда же. Одной роли «Программист» мало — HTML-
    верстальщиков публикуют под «Дизайнером» и «Аналитиком».
    """
    roles = {OTHER_ROLE}
    for cat in professional_roles.get("categories", []):
        if "информац" in (cat.get("name") or "").lower():
            roles |= {str(r["id"]) for r in cat.get("roles", [])}
    return roles


def parse_vacancy(item: dict, country_of: dict[str, str], it_roles: set[str] | None = None) -> Vacancy:
    employer = (item.get("employer") or {}).get("name") or "—"
    salary = item.get("salary") or {}
    area_id = str((item.get("area") or {}).get("id") or "")
    name = item.get("name") or "—"
    return Vacancy(
        id=str(item["id"]),
        name=name,
        employer=employer,
        area_id=area_id,
        country_id=country_of.get(area_id, ""),
        remote=any(f.get("id") == "REMOTE" for f in item.get("work_format") or []),
        category=_category(item, name, it_roles),
        published_at=parse_dt(item["published_at"]),
        salary_from=salary.get("from"),
        salary_to=salary.get("to"),
        currency=salary.get("currency"),
        url=item.get("alternate_url") or f"https://hh.ru/vacancy/{item['id']}",
    )


def _category(item: dict, name: str, it_roles: set[str] | None) -> str:
    roles = {str(r.get("id")) for r in item.get("professional_roles") or []}
    if it_roles and roles and not roles & it_roles:
        return "other"  # не IT-вакансия, сколько бы фронт-слов ни было в названии
    return classify(name)


# ── чистая логика ───────────────────────────────────────────────────────


def window_start(last_run_at: datetime | None, now: datetime) -> datetime:
    return last_run_at if last_run_at is not None else now - timedelta(hours=24)


def candidates(vacancies: Iterable[Vacancy], start: datetime) -> list[Vacancy]:
    return [v for v in vacancies if v.published_at >= start]


def split_by_age(cands, initial: dict[str, datetime], start: datetime):
    """Новые (созданы в окне), поднятые (< 30 дней), переоткрытые (≥ 30 дней).

    Без карточки вакансия считается новой: свидетельств, что она старая, нет.
    """
    new, bumped, reopened = [], [], []
    for v in cands:
        created = initial.get(v.id)
        if created is None or created >= start:
            new.append(v)
            continue
        age = (v.published_at - created).days
        (reopened if age >= REOPEN_AGE_DAYS else bumped).append((v, age))
    return new, bumped, reopened


def gone_ids(prev_ids: set[str], today: Iterable[Vacancy]) -> list[str]:
    return sorted(prev_ids - {v.id for v in today})


def split_gone(ids, details: dict[str, dict | None]) -> tuple[list[str], list[str]]:
    """Закрытые — архив или карточка недоступна; скрытые — карточка открыта."""
    closed, hidden = [], []
    for vid in ids:
        d = details.get(vid)
        (closed if d is None or d.get("archived") else hidden).append(vid)
    return closed, hidden


def unique_positions(vacancies: Iterable[Vacancy]) -> int:
    """Одна позиция, размещённая в нескольких городах, — это одна позиция, а не N."""
    return len({(v.employer.strip().lower(), " ".join(v.name.lower().split())) for v in vacancies})


def _with_unique(vacancies: list[Vacancy]) -> str:
    u = unique_positions(vacancies)
    return f"<b>{len(vacancies)}</b>" + (f" (уникальных позиций {u})" if u != len(vacancies) else "")


def top_employers(vacancies: Iterable[Vacancy], limit: int = 5) -> list[tuple[str, int]]:
    return Counter(v.employer for v in vacancies if v.employer != "—").most_common(limit)


# ── сводка ──────────────────────────────────────────────────────────────


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _title(name: str) -> str:
    name = name if len(name) <= LIST_TITLE_LIMIT else name[: LIST_TITLE_LIMIT - 1] + "…"
    return html.escape(name)


def _delta(now: int, prev: int | None) -> str:
    return "(первый снимок)" if prev is None else f"({now - prev:+d} к прошлому снимку)"


def _pack(header: str, lines: list[str]) -> list[str]:
    """Режет список на сообщения ≤ лимита Telegram; каждое — закрытый blockquote."""
    if not lines:
        return []
    opening, closing = "\n<blockquote expandable>", "</blockquote>"
    messages, chunk = [], []
    for line in lines:
        candidate = header + opening + "\n".join(chunk + [line]) + closing
        if chunk and len(candidate) > TG_MESSAGE_LIMIT:
            messages.append(header + opening + "\n".join(chunk) + closing)
            chunk = []
        chunk.append(line)
    messages.append(header + opening + "\n".join(chunk) + closing)
    return messages


def build_messages(r: Report) -> list[str]:
    front, js = r.front, r.js
    g = r.geo()
    is_front = lambda v: v.category == "front"  # noqa: E731
    in_js = lambda v: v.category in JS_MARKET  # noqa: E731

    f_new = [v for v in r.new if is_front(v)]
    f_bumped = [(v, a) for v, a in r.bumped if is_front(v)]
    f_reopened = [(v, a) for v, a in r.reopened if is_front(v)]
    f_closed = [c for c in r.closed if c[3] == "front"]
    f_hidden = r.hidden_in({"front"})

    reopen_line = f"♻️ Переоткрыли: <b>{len(f_reopened)}</b> (старше {REOPEN_AGE_DAYS} дней)"
    if f_reopened:
        reopen_line += f" · медиана возраста {round(statistics.median(a for _, a in f_reopened))} дн."

    lines = [
        f"📊 <b>Рынок фронтенда · {r.now.astimezone(MSK).strftime('%d.%m.%Y')}</b> · весь hh",
        "",
        f"Фронтенд: <b>{len(front)}</b> {_delta(len(front), r.prev_front)}"
        + (f" · уникальных позиций {unique_positions(front)}" if unique_positions(front) != len(front) else ""),
        f"Россия {g['russia']} (Москва {g['moscow']} · СПб {g['spb']}) · другие страны {g['other']}"
        f" · удалёнка {g['remote']}",
        f"JS-рынок (фронт + fullstack + Node + веб): <b>{len(js)}</b> {_delta(len(js), r.prev_js)}",
    ]
    if r.found > HH_RESULTS_CAP:
        lines.append(f"⚠️ hh отдаёт не больше {HH_RESULTS_CAP} из {r.found} — хвост выдачи не виден")
    lines += [
        "",
        f"С {_fmt_dt(r.window_start)} по {_fmt_dt(r.now)}, фронтенд:",
        f"🆕 Новые: {_with_unique(f_new)}",
        f"⬆️ Подняли: <b>{len(f_bumped)}</b> (свежие, до {REOPEN_AGE_DAYS} дней)",
        reopen_line,
        f"🚪 Закрыты: <b>{len(f_closed)}</b>",
    ]
    if f_hidden:
        lines.append(f"👻 Временно скрыты из поиска: {f_hidden} (обычно возвращаются через 1–2 дня)")
    lines += [
        "",
        "JS-рынок за то же время: "
        f"новые {sum(in_js(v) for v in r.new)} · подняли {sum(in_js(v) for v, _ in r.bumped)}"
        f" · переоткрыли {sum(in_js(v) for v, _ in r.reopened)}"
        f" · закрыты {sum(c[3] in JS_MARKET for c in r.closed)}",
    ]
    top = top_employers(f_new)
    if top:
        lines += ["", "Больше всего новых во фронте: " + " · ".join(f"{html.escape(e)} {n}" for e, n in top)]
    messages = ["\n".join(lines)]

    def vline(v: Vacancy, suffix: str = "") -> str:
        return f'• <a href="{html.escape(v.url)}">{_title(v.name)}</a> — {html.escape(v.employer)}{suffix}'

    messages += _pack(f"🆕 <b>Новые во фронте</b> ({len(f_new)})", [vline(v) for v in f_new])
    messages += _pack(
        f"♻️ <b>Переоткрыли во фронте</b> ({len(f_reopened)})",
        [vline(v, f" · {a} дн.") for v, a in sorted(f_reopened, key=lambda x: -x[1])],
    )
    js_other = [v for v in r.new if in_js(v) and not is_front(v)]
    messages += _pack(
        f"🆕 <b>Новые в JS-рынке кроме фронта</b> ({len(js_other)})",
        [vline(v, f" · {CATEGORY_LABEL[v.category]}") for v in js_other],
    )
    messages += _pack(
        f"🚪 <b>Закрыты во фронте</b> ({len(f_closed)})",
        [f"• {_title(n)} — {html.escape(e)}" for _, n, e, _ in f_closed],
    )
    return messages


# ── хранилище ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL, window_start TEXT NOT NULL, found INTEGER,
    front_total INTEGER NOT NULL, js_total INTEGER NOT NULL,
    russia INTEGER, moscow INTEGER, spb INTEGER, other_countries INTEGER, remote INTEGER,
    new_front INTEGER, bumped_front INTEGER, reopened_front INTEGER, closed_front INTEGER, hidden INTEGER
);
CREATE TABLE IF NOT EXISTS snapshot (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    name TEXT, employer TEXT, category TEXT, area_id TEXT, country_id TEXT, remote INTEGER,
    published_at TEXT, salary_from INTEGER, salary_to INTEGER, currency TEXT,
    PRIMARY KEY (run_id, vacancy_id)
);
CREATE TABLE IF NOT EXISTS vacancy_initial (
    vacancy_id TEXT PRIMARY KEY,
    initial_created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('new', 'bumped', 'reopened', 'closed', 'hidden')),
    category TEXT,
    age_days INTEGER
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def last_run(self) -> Run | None:
        row = self.db.execute(
            "SELECT id, run_at, front_total, js_total FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return None if row is None else Run(row[0], datetime.fromisoformat(row[1]), row[2], row[3])

    def snapshot_ids(self, run_id: int) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT vacancy_id FROM snapshot WHERE run_id = ?", (run_id,))}

    def snapshot_titles(self, run_id: int, ids: Iterable[str]) -> dict[str, tuple[str, str, str]]:
        ids = list(ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT vacancy_id, name, employer, category FROM snapshot"
            f" WHERE run_id = ? AND vacancy_id IN ({marks})", (run_id, *ids))
        return {r[0]: (r[1], r[2], r[3]) for r in rows}

    def cached_initial(self, ids: Iterable[str]) -> dict[str, datetime]:
        ids = list(ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT vacancy_id, initial_created_at FROM vacancy_initial WHERE vacancy_id IN ({marks})", ids)
        return {r[0]: datetime.fromisoformat(r[1]) for r in rows}

    def save_initial(self, values: dict[str, datetime]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO vacancy_initial VALUES (?, ?)",
                                [(k, v.isoformat()) for k, v in values.items()])

    def save_run(self, r: Report, vacancies: Iterable[Vacancy]) -> int:
        g = r.geo()
        front = lambda v: v.category == "front"  # noqa: E731
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (run_at, window_start, found, front_total, js_total, russia, moscow,"
                " spb, other_countries, remote, new_front, bumped_front, reopened_front, closed_front,"
                " hidden) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.now.isoformat(), r.window_start.isoformat(), r.found, len(r.front), len(r.js),
                 g["russia"], g["moscow"], g["spb"], g["other"], g["remote"],
                 sum(front(v) for v in r.new), sum(front(v) for v, _ in r.bumped),
                 sum(front(v) for v, _ in r.reopened), sum(c[3] == "front" for c in r.closed),
                 len(r.hidden)))
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, v.id, v.name, v.employer, v.category, v.area_id, v.country_id, int(v.remote),
                  v.published_at.isoformat(), v.salary_from, v.salary_to, v.currency) for v in vacancies])
            hidden = [h if isinstance(h, tuple) else (h, None) for h in r.hidden]
            events = (
                [(run_id, v.id, "new", v.category, None) for v in r.new]
                + [(run_id, v.id, "bumped", v.category, a) for v, a in r.bumped]
                + [(run_id, v.id, "reopened", v.category, a) for v, a in r.reopened]
                + [(run_id, c[0], "closed", c[3], None) for c in r.closed]
                + [(run_id, vid, "hidden", cat, None) for vid, cat in hidden]
            )
            self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", events)
        return run_id


# ── обращение к hh ──────────────────────────────────────────────────────


def fetch_listing(api, country_of: dict[str, str], sleep=time.sleep,
                  it_roles: set[str] | None = None) -> tuple[list[Vacancy], int]:
    found: dict[str, Vacancy] = {}
    page, total = 0, 0
    while True:
        resp = api.get("/vacancies", text=QUERY, per_page=PER_PAGE, page=page)
        total = resp.get("found", 0)
        for item in resp.get("items", []):
            v = parse_vacancy(item, country_of, it_roles)
            found[v.id] = v  # выдача может сдвигаться между страницами — дубли схлопываем
        if page >= resp.get("pages", 1) - 1:
            break
        page += 1
        sleep(DETAIL_DELAY)
    return list(found.values()), total


class CaptchaRequired(RuntimeError):
    """hh требует капчу на аккаунт — карточки не отдаются, считать дальше нельзя."""


def _is_captcha(response) -> bool:
    try:
        errors = response.json().get("errors") or []
    except Exception:
        return False
    return any(e.get("value") == "captcha_required" for e in errors)


def fetch_detail(api, vacancy_id: str) -> dict | None:
    """Детальная карточка; None, только если вакансия удалена или скрыта работодателем.

    403 captcha_required — НЕ «карточка недоступна»: 05.10.2026 из-за этого все кандидаты
    молча стали «новыми», а пропавшие — «закрытыми». Капча прерывает прогон целиком.
    """
    try:
        return api.get(f"/vacancies/{vacancy_id}")
    except requests.HTTPError as ex:
        response = ex.response
        if response is not None and _is_captcha(response):
            raise CaptchaRequired(
                "hh требует капчу на аккаунт: детальные карточки не отдаются. Новые и переоткрытые"
                " без них не различить — снимок пропущен, следующий запуск досчитает."
            ) from ex
        if getattr(response, "status_code", None) in (403, 404):
            return None
        raise


def send(text: str) -> None:
    token = os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID")
    if not (token and chat_id):
        print(text, end="\n\n")
        return
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", getattr(r, "text", r), file=sys.stderr)


# ── сценарий ────────────────────────────────────────────────────────────


def build_report(api, store: Store, now: datetime, sleep) -> tuple[Report, dict[str, datetime]]:
    last = store.last_run()
    start = window_start(last.run_at if last else None, now)
    country_of = country_index(api.get("/areas"))
    it_roles = it_role_index(api.get("/professional_roles"))
    listing, found = fetch_listing(api, country_of, sleep, it_roles)

    cands = candidates(listing, start)
    initial = store.cached_initial(v.id for v in cands)
    fetched: dict[str, datetime] = {}
    for v in cands:
        if v.id in initial:
            continue
        detail = fetch_detail(api, v.id)
        sleep(DETAIL_DELAY)
        if detail and detail.get("initial_created_at"):
            fetched[v.id] = parse_dt(detail["initial_created_at"])
    new, bumped, reopened = split_by_age(cands, {**initial, **fetched}, start)

    closed: list[Closed] = []
    hidden: list[tuple[str, str]] = []
    if last is not None:
        gone = gone_ids(store.snapshot_ids(last.id), listing)
        details = {}
        for vid in gone:
            details[vid] = fetch_detail(api, vid)
            sleep(DETAIL_DELAY)
        closed_ids, hidden_ids = split_gone(gone, details)
        titles = store.snapshot_titles(last.id, gone)
        closed = [(vid, *titles.get(vid, ("—", "—", "other"))) for vid in closed_ids]
        hidden = [(vid, titles.get(vid, ("—", "—", "other"))[2]) for vid in hidden_ids]

    report = Report(
        now=now, window_start=start, listing=listing, found=found,
        prev_front=last.front_total if last else None, prev_js=last.js_total if last else None,
        new=new, bumped=bumped, reopened=reopened, closed=closed, hidden=hidden,
    )
    return report, fetched


def run(api, store: Store, now: datetime | None = None, send=send, sleep=time.sleep, dry_run=False) -> Report:
    now = now or datetime.now(MSK)
    try:
        report, fetched = build_report(api, store, now, sleep)
    except Exception as ex:
        send(
            "⚠️ <b>Мониторинг рынка: ошибка</b>\n"
            f"{html.escape(type(ex).__name__)}: {html.escape(str(ex))[:300]}\n"
            "Снимок не сохранён, окно не сдвинуто — следующий запуск досчитает."
        )
        raise
    if not dry_run:
        store.save_initial(fetched)
        store.save_run(report, report.listing)
    for message in build_messages(report):
        send(message)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=CONFIG_DIR / "market_v2.db")
    parser.add_argument("--dry-run", action="store_true", help="посчитать и напечатать, не сохраняя")
    args = parser.parse_args()
    sender = (lambda text: print(text, end="\n\n")) if args.dry_run else send
    run(Api(load_token()), Store(args.db), send=sender, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
