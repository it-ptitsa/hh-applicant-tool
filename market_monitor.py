#!/usr/bin/env python3
"""Ежедневный мониторинг рынка фронтенд-вакансий на hh → сводка в Telegram.

Снимает всю выдачу поиска (фронт-слова в названии, Россия), сравнивает с прошлым
снимком и делит вакансии на новые, переопубликованные и ушедшие.

Почему нужна детальная карточка: в выдаче поиска hh `created_at` всегда равен
`published_at`, а при переопубликации оба обнуляются. Настоящая дата создания есть
только в карточке (`initial_created_at`), её и кэшируем. 30.09.2026 из 40 вакансий
«за сутки» новых было 19, остальные 21 — переопубликованные старые.

Спека: openspec/specs/market-monitor. Запуск на сервере — market_monitor.sh (крон хоста).
Отклики не отправляет, состояние аккаунта не меняет: только GET к API.
"""

from __future__ import annotations

import argparse
import html
import os
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

# Без «верстальщик»: 30.09.2026 он добавлял 79 вакансий из 399, почти все — полиграфия
# (дизайнер-верстальщик, препресс, вёрстка книг). Без «next.js»: hh режет его на «next» и
# «js», и под «js» попадают Node.js-бэкенды; фронтовые Next.js-вакансии ловятся по react.
QUERY = "NAME:(frontend OR фронтенд OR front-end OR react OR vue OR angular OR typescript OR javascript)"
AREA = 113  # Россия
AREA_MOSCOW = "1"
AREA_SPB = "2"
PER_PAGE = 100
HH_RESULTS_CAP = 2000  # поиск hh не отдаёт больше 2000 результатов на запрос
DETAIL_DELAY = 0.3  # пауза между детальными карточками, сек

MSK = timezone(timedelta(hours=3))
TG_MESSAGE_LIMIT = 4000
LIST_TITLE_LIMIT = 90


# ── модель ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Vacancy:
    id: str
    name: str
    employer: str
    area_id: str
    remote: bool
    published_at: datetime
    salary_from: int | None
    salary_to: int | None
    currency: str | None
    url: str


@dataclass
class Report:
    now: datetime
    window_start: datetime
    total: int
    prev_total: int | None
    moscow: int
    spb: int
    remote: int
    new: list[Vacancy] = field(default_factory=list)
    republished: list[tuple[Vacancy, int]] = field(default_factory=list)
    closed: list[tuple[str, str, str]] = field(default_factory=list)  # (id, название, работодатель)
    dropped: list[tuple[str, str, str]] = field(default_factory=list)


class Run(NamedTuple):
    id: int
    run_at: datetime
    total: int


def parse_dt(value: str) -> datetime:
    """Дата из API hh: `2026-09-30T20:00:00+0300` (смещение без двоеточия)."""
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")


def parse_vacancy(item: dict) -> Vacancy:
    employer = (item.get("employer") or {}).get("name") or "—"
    salary = item.get("salary") or {}
    formats = item.get("work_format") or []
    return Vacancy(
        id=str(item["id"]),
        name=item.get("name") or "—",
        employer=employer,
        area_id=str((item.get("area") or {}).get("id") or ""),
        remote=any(f.get("id") == "REMOTE" for f in formats),
        published_at=parse_dt(item["published_at"]),
        salary_from=salary.get("from"),
        salary_to=salary.get("to"),
        currency=salary.get("currency"),
        url=item.get("alternate_url") or f"https://hh.ru/vacancy/{item['id']}",
    )


# ── чистая логика ───────────────────────────────────────────────────────


def window_start(last_run_at: datetime | None, now: datetime) -> datetime:
    """Окно — от прошлого запуска; при первом запуске — последние сутки."""
    return last_run_at if last_run_at is not None else now - timedelta(hours=24)


def candidates(vacancies: Iterable[Vacancy], start: datetime) -> list[Vacancy]:
    """Опубликованные (или переопубликованные) после начала окна."""
    return [v for v in vacancies if v.published_at >= start]


def split_new_republished(
    cands: Iterable[Vacancy], initial: dict[str, datetime], start: datetime
) -> tuple[list[Vacancy], list[tuple[Vacancy, int]]]:
    """Новая — создана внутри окна; иначе переопубликована (с возрастом в днях).

    Если карточку получить не удалось, считаем вакансию новой: свидетельств, что она
    старая, нет.
    """
    new: list[Vacancy] = []
    republished: list[tuple[Vacancy, int]] = []
    for v in cands:
        created = initial.get(v.id)
        if created is None or created >= start:
            new.append(v)
        else:
            republished.append((v, (v.published_at - created).days))
    return new, republished


def gone_ids(prev_ids: set[str], today: Iterable[Vacancy]) -> list[str]:
    return sorted(prev_ids - {v.id for v in today})


def split_gone(ids: Iterable[str], details: dict[str, dict | None]) -> tuple[list[str], list[str]]:
    """Закрытые — в архиве или карточка недоступна; выпавшие — открыты, но не под запрос."""
    closed, dropped = [], []
    for vid in ids:
        detail = details.get(vid)
        if detail is None or detail.get("archived"):
            closed.append(vid)
        else:
            dropped.append(vid)
    return closed, dropped


def top_employers(vacancies: Iterable[Vacancy], limit: int = 5) -> list[tuple[str, int]]:
    counts = Counter(v.employer for v in vacancies if v.employer != "—")
    return counts.most_common(limit)


# ── сводка ──────────────────────────────────────────────────────────────


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _title(name: str) -> str:
    name = name if len(name) <= LIST_TITLE_LIMIT else name[: LIST_TITLE_LIMIT - 1] + "…"
    return html.escape(name)


def _pack(header: str, lines: list[str]) -> list[str]:
    """Режет список на сообщения ≤ лимита Telegram; каждое — закрытый blockquote."""
    if not lines:
        return []
    messages, chunk = [], []
    opening, closing = "\n<blockquote expandable>", "</blockquote>"

    def size(items: list[str]) -> int:
        return len(header) + len(opening) + len("\n".join(items)) + len(closing)

    for line in lines:
        if chunk and size(chunk + [line]) > TG_MESSAGE_LIMIT:
            messages.append(header + opening + "\n".join(chunk) + closing)
            chunk = []
        chunk.append(line)
    messages.append(header + opening + "\n".join(chunk) + closing)
    return messages


def build_messages(r: Report) -> list[str]:
    if r.prev_total is None:
        delta = "(первый снимок)"
    else:
        delta = f"({r.total - r.prev_total:+d} к прошлому снимку)"

    gone_total = len(r.closed) + len(r.dropped)
    rep_line = f"🔁 Переопубликованы: <b>{len(r.republished)}</b>"
    if r.republished:
        median = round(statistics.median(age for _, age in r.republished))
        rep_line += f" · медиана возраста {median} дн."
    gone_line = f"🚪 Ушли: <b>{gone_total}</b>"
    if gone_total:
        gone_line += f" · закрыты {len(r.closed)}, выпали из выдачи {len(r.dropped)}"

    lines = [
        f"📊 <b>Рынок фронтенда · {r.now.astimezone(MSK).strftime('%d.%m.%Y')}</b>",
        "Россия, фронт-слова в названии вакансии",
        "",
        f"Всего в выдаче: <b>{r.total}</b> {delta}",
        f"Москва {r.moscow} · СПб {r.spb} · удалёнка {r.remote}",
        "",
        f"С {_fmt_dt(r.window_start)} по {_fmt_dt(r.now)}:",
        f"🆕 Новые: <b>{len(r.new)}</b>",
        rep_line,
        gone_line,
    ]
    top = top_employers(r.new)
    if top:
        lines += ["", "Больше всего новых: " + " · ".join(f"{html.escape(e)} {n}" for e, n in top)]
    messages = ["\n".join(lines)]

    def vac_line(v: Vacancy, suffix: str = "") -> str:
        return f'• <a href="{html.escape(v.url)}">{_title(v.name)}</a> — {html.escape(v.employer)}{suffix}'

    messages += _pack(f"🆕 <b>Новые</b> ({len(r.new)})", [vac_line(v) for v in r.new])
    messages += _pack(
        f"🔁 <b>Переопубликованы</b> ({len(r.republished)})",
        [vac_line(v, f" · {age} дн.") for v, age in sorted(r.republished, key=lambda x: -x[1])],
    )
    messages += _pack(
        f"🚪 <b>Ушли</b> ({gone_total})",
        [f"• {_title(n)} — {html.escape(e)} · закрыта" for _, n, e in r.closed]
        + [f"• {_title(n)} — {html.escape(e)} · выпала из выдачи" for _, n, e in r.dropped],
    )
    return messages


# ── хранилище ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    window_start TEXT NOT NULL,
    total INTEGER NOT NULL,
    moscow INTEGER, spb INTEGER, remote INTEGER,
    new_cnt INTEGER, republished_cnt INTEGER, closed_cnt INTEGER, dropped_cnt INTEGER
);
CREATE TABLE IF NOT EXISTS snapshot (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    name TEXT, employer TEXT, area_id TEXT, remote INTEGER,
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
    kind TEXT NOT NULL CHECK (kind IN ('new', 'republished', 'closed', 'dropped')),
    age_days INTEGER
);
"""


class Store:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def last_run(self) -> Run | None:
        row = self.db.execute("SELECT id, run_at, total FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            return None
        return Run(row[0], datetime.fromisoformat(row[1]), row[2])

    def snapshot_ids(self, run_id: int) -> set[str]:
        rows = self.db.execute("SELECT vacancy_id FROM snapshot WHERE run_id = ?", (run_id,))
        return {r[0] for r in rows}

    def snapshot_titles(self, run_id: int, ids: Iterable[str]) -> dict[str, tuple[str, str]]:
        ids = list(ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT vacancy_id, name, employer FROM snapshot WHERE run_id = ? AND vacancy_id IN ({marks})",
            (run_id, *ids),
        )
        return {r[0]: (r[1], r[2]) for r in rows}

    def cached_initial(self, ids: Iterable[str]) -> dict[str, datetime]:
        ids = list(ids)
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.db.execute(
            f"SELECT vacancy_id, initial_created_at FROM vacancy_initial WHERE vacancy_id IN ({marks})",
            ids,
        )
        return {r[0]: datetime.fromisoformat(r[1]) for r in rows}

    def save_initial(self, values: dict[str, datetime]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO vacancy_initial VALUES (?, ?)",
                [(vid, dt.isoformat()) for vid, dt in values.items()],
            )

    def save_run(self, r: Report, vacancies: Iterable[Vacancy]) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (run_at, window_start, total, moscow, spb, remote,"
                " new_cnt, republished_cnt, closed_cnt, dropped_cnt) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    r.now.isoformat(), r.window_start.isoformat(), r.total, r.moscow, r.spb,
                    r.remote, len(r.new), len(r.republished), len(r.closed), len(r.dropped),
                ),
            )
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        run_id, v.id, v.name, v.employer, v.area_id, int(v.remote),
                        v.published_at.isoformat(), v.salary_from, v.salary_to, v.currency,
                    )
                    for v in vacancies
                ],
            )
            events = (
                [(run_id, v.id, "new", None) for v in r.new]
                + [(run_id, v.id, "republished", age) for v, age in r.republished]
                + [(run_id, vid, "closed", None) for vid, _, _ in r.closed]
                + [(run_id, vid, "dropped", None) for vid, _, _ in r.dropped]
            )
            self.db.executemany("INSERT INTO events VALUES (?,?,?,?)", events)
        return run_id


# ── обращение к hh ──────────────────────────────────────────────────────


def fetch_listing(api, sleep: Callable[[float], None] = time.sleep) -> list[Vacancy]:
    found: dict[str, Vacancy] = {}
    page = 0
    while True:
        resp = api.get("/vacancies", text=QUERY, area=AREA, per_page=PER_PAGE, page=page)
        for item in resp.get("items", []):
            v = parse_vacancy(item)
            found[v.id] = v  # выдача сдвигается между страницами — дубли схлопываем
        if resp.get("found", 0) > HH_RESULTS_CAP:
            print(f"⚠️ found={resp['found']} > {HH_RESULTS_CAP}: хвост выдачи не виден", file=sys.stderr)
        if page >= resp.get("pages", 1) - 1:
            break
        page += 1
        sleep(DETAIL_DELAY)
    return list(found.values())


def fetch_detail(api, vacancy_id: str) -> dict | None:
    """Детальная карточка; None, если вакансия удалена или закрыта для просмотра."""
    try:
        return api.get(f"/vacancies/{vacancy_id}")
    except requests.HTTPError as ex:
        status = getattr(ex.response, "status_code", None)
        if status in (403, 404):
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
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    if not r.ok:
        print("Telegram error:", getattr(r, "text", r), file=sys.stderr)


# ── сценарий ────────────────────────────────────────────────────────────


def build_report(api, store: Store, now: datetime, sleep) -> tuple[Report, list[Vacancy], dict]:
    last = store.last_run()
    start = window_start(last.run_at if last else None, now)
    listing = fetch_listing(api, sleep)

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
    new, republished = split_new_republished(cands, {**initial, **fetched}, start)

    closed: list[tuple[str, str, str]] = []
    dropped: list[tuple[str, str, str]] = []
    if last is not None:
        gone = gone_ids(store.snapshot_ids(last.id), listing)
        details = {}
        for vid in gone:
            details[vid] = fetch_detail(api, vid)
            sleep(DETAIL_DELAY)
        closed_ids, dropped_ids = split_gone(gone, details)
        titles = store.snapshot_titles(last.id, gone)
        closed = [(vid, *titles.get(vid, ("—", "—"))) for vid in closed_ids]
        dropped = [(vid, *titles.get(vid, ("—", "—"))) for vid in dropped_ids]

    report = Report(
        now=now,
        window_start=start,
        total=len(listing),
        prev_total=last.total if last else None,
        moscow=sum(v.area_id == AREA_MOSCOW for v in listing),
        spb=sum(v.area_id == AREA_SPB for v in listing),
        remote=sum(v.remote for v in listing),
        new=new,
        republished=republished,
        closed=closed,
        dropped=dropped,
    )
    return report, listing, fetched


def run(api, store: Store, now: datetime | None = None, send=send, sleep=time.sleep, dry_run=False) -> Report:
    now = now or datetime.now(MSK)
    try:
        report, listing, fetched = build_report(api, store, now, sleep)
    except Exception as ex:
        send(
            "⚠️ <b>Мониторинг рынка: ошибка</b>\n"
            f"{html.escape(type(ex).__name__)}: {html.escape(str(ex))[:300]}\n"
            "Снимок не сохранён, окно не сдвинуто — следующий запуск досчитает."
        )
        raise
    if not dry_run:
        store.save_initial(fetched)
        store.save_run(report, listing)
    for message in build_messages(report):
        send(message)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=CONFIG_DIR / "market.db")
    parser.add_argument("--dry-run", action="store_true", help="посчитать и напечатать, не сохраняя")
    args = parser.parse_args()
    store = Store(args.db)
    sender = (lambda text: print(text, end="\n\n")) if args.dry_run else send
    run(Api(load_token()), store, send=sender, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
