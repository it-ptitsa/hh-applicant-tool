#!/usr/bin/env python3
"""Ежедневный мониторинг рынка фронтенд-вакансий на hh → сводка в Telegram.

Снимает выдачу по всему hh одним запросом-объединением (фронт, fullstack, Node, общий
веб), сам присваивает каждой вакансии категорию по названию и сравнивает с прошлыми
снимками: новые, поднятые, переоткрытые, закрытые, временно скрытые.

Почему так (подтверждено данными 30.09–06.10.2026):
- в поиске hh `created_at == published_at`; детальные карточки (initial_created_at) ловят
  капчу на аккаунт. v4 обходится без них: номера вакансий растут вместе с датой создания,
  возраст оцениваем по опорным точкам «номер → время» из поиска (бэктест на 166 карточках:
  ошибок 0);
- «переопубликованные» — это два явления: платное поднятие свежей вакансии и переоткрытие
  старой (≥ 30 дней). Считаем отдельно;
- вакансии «мигают»: из пропавших на 1–2 дня вернулись 47, из пропавших на 3+ — ни одна
  из 53. Закрытой считаем вакансию, которой нет в выдаче 3 дня подряд;
- фронт-слова в названии цепляют QA на TS, React Native и Node-бэкенды (38% среза) —
  поэтому собираем шире, а «Фронтенд» отбираем классификатором.

Перед отправкой отчёт проходит проверки (полнота выдачи, суммы, скачки, возраст) — при сбое
в шапке «Не пересылать». Новые названия копятся в title_review на независимую проверку агентом.

Спека: openspec/specs/market-monitor. Запуск на сервере — market_monitor.sh (крон хоста).
Отклики не отправляет, состояние аккаунта не меняет: только GET к API.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import html
import json
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
# AI-вакансии — отдельным запросом и только по ролям IT-категории: без ролей запрос упирается в
# предел выдачи и тащит «агентов по недвижимости» (06.10: 3490 найдено, почти всё мусор).
AI_QUERY = (
    'NAME:("ai engineer" OR "ai-engineer" OR "ai инженер" OR "ai-инженер" OR "ии-инженер" OR "ии инженер"'
    ' OR "ai разработчик" OR "ai-разработчик" OR "ии-разработчик" OR "ai developer" OR llm OR "ai-native"'
    ' OR "ai native" OR "ai-first" OR вайбкод OR "vibe coding" OR "vibe-coder" OR "prompt engineer")'
)
# Фрагмент требований в выдаче обрезан — Node во fullstack добираем полнотекстовым запросом.
FULL_NODE_QUERY = (
    'NAME:(fullstack OR "full stack" OR full-stack OR фулстек OR фуллстек) AND (node OR nodejs OR nestjs OR express)'
)
SLICE_VERSION = 4  # 1 — Россия; 2 — весь hh; 3 — + AI, стек, Node; 4 — без карточек, React Native во фронте
PER_PAGE = 100
HH_RESULTS_CAP = 2000
REQUEST_DELAY = 0.3
NEW_AGE_DAYS = 1         # создана меньше суток назад — новая (модерация бывает часами)
REOPEN_AGE_DAYS = 30
CLOSE_AFTER_DAYS = 3     # нет в выдаче 3 дня подряд — закрыта
ANCHOR_DAYS = 31         # на сколько дней назад держим опорные точки (дальше поиск hh не отдаёт)
ANCHOR_HOUR = 12         # окно 12:00–13:00 МСК: рабочее время, публикаций много
COMPLETENESS = 0.98      # собрано меньше 98% найденного — отчёт не пересылать
JUMP_LIMIT = 0.15        # скачок больше 15% за сутки — проверить вручную
JUMP_MIN_BASE = 50
GOLDEN_PATH = Path(__file__).resolve().parent / "tests" / "fixtures" / "market_golden.tsv"

MSK = timezone(timedelta(hours=3))
TG_MESSAGE_LIMIT = 4000
LIST_TITLE_LIMIT = 90

# ── категории по названию ───────────────────────────────────────────────

_QA = re.compile(r"\bqa\b|aqa|тестиров|автотест|\bsdet\b|test engineer|quality assurance", re.I)
# Нетех-профессии, которые не перебиваются лид-словом: «Team Lead аналитиков» — аналитик.
_NONTECH_STRICT = re.compile(
    r"аналитик|analyst|дизайнер|designer|преподават|учител|наставник|ментор"
    r"|teacher|tutor|рекрутер|recruit|product owner|продакт|scrum|o.qituvchi", re.I)
# «менеджер» — нетех, если это не руководитель разработки («Engineering Manager (Frontend)»).
_MANAGER = re.compile(r"менеджер|manager", re.I)
# \s, а не пробел: в названиях hh встречается неразрывный пробел («React\xa0Native»)
_MOBILE = re.compile(r"android|\bios\b|flutter|mobile|мобильн", re.I)
_RN = re.compile(r"react\s*native", re.I)  # \s: в названиях hh бывает неразрывный пробел
_FULLSTACK = re.compile(r"ful+.?stack|ful+.?стек|фул+.?стек|разработчик полного цикла", re.I)  # и «Fulstack»
_BACKEND = re.compile(r"backend|back-end|бэкенд|бекенд|серверн|\bnode|\bnest", re.I)
_FRONT_STRONG = re.compile(
    r"front|фронт|react|\bvue|angular|svelte|nuxt|next\.?js|ui\s*-?\s*(разработ|developer)"
    r"|интерфейс|верст|html", re.I)
# Голое «JS» не годится: 05.10 во фронт попал «Машинист экскаватора (JCB JS 260)».
_FRONT_LANG = re.compile(
    r"typescript|javascript|pixi\.?js|\bjs\s*-?\s*(разработ|программ|developer|engineer|стаж|intern)"
    r"|\bts\s*-?\s*(разработ|developer)", re.I)
# Fullstack на чужом бэкенде без фронт-стека в названии — не JS-рынок.
_NON_JS_STACK = re.compile(
    r"\.net|c#|java\b|kotlin|python|django|php|laravel|golang|\bgo\b|c\+\+|ruby|delphi"
    r"|битрикс|bitrix|wordpress|opencart|drupal|joomla|modx|\b1с\b|\b1c\b", re.I)
_CMS = re.compile(r"битрикс|bitrix|wordpress|\b1с\b|\b1c\b|drupal|opencart|joomla|modx|tilda", re.I)
_WEB = re.compile(r"веб|web", re.I)
_FRAMEWORK = re.compile(r"react|\bvue|angular|svelte|nuxt|next\.?js", re.I)
_LEAD = re.compile(
    r"team.?lead|тимлид|tech.?lead|техлид|\blead\b|\bлид\b|руководител|head of|engineering manager"
    r"|архитект|architect|director|директор|\bcto\b", re.I)
_OTHER_IT = re.compile(r"devops|\bsre\b|reverse engineer|researcher|исследовател|embedded|встраива", re.I)
# Десктоп-интерфейсы на C++/.NET называют «frontend»: «Разработчик frontend, Qt», «.NET Frontend (WPF)».
_DESKTOP_UI = re.compile(r"\bqt\b|\bqml\b|\bwpf\b|avalonia|winforms|windows forms", re.I)
# Кириллические двойники латинских букв: «аngular», «Middlе», «С#» встречаются в названиях hh.
_HOMOGLYPHS = str.maketrans("аеорсхуАЕОРСХКМТВН", "aeopcxyAEOPCXKMTBH")
_LETTER_RUN = re.compile(r"[A-Za-zА-Яа-яЁё]+")


def latinize(name: str) -> str:
    """Меняет кириллических двойников на латиницу внутри сплошного буквенного отрезка, где есть
    латиница («аngular» → «angular», «Middlе» → «Middle»), и «С#» → «C#». «Fullstack-аналитик» —
    два отрезка, русское слово не трогаем."""
    name = re.sub(r"С(?=#)", "C", name)
    return _LETTER_RUN.sub(
        lambda m: m.group(0).translate(_HOMOGLYPHS) if re.search(r"[A-Za-z]", m.group(0)) else m.group(0), name)
_PM = re.compile(r"руководитель проект|project manager|руководитель отдела продаж", re.I)
_SENIOR = re.compile(r"senior|старш|ведущ|сеньор|сениор", re.I)
_MIDDLE = re.compile(r"middle|мидл", re.I)
_JUNIOR = re.compile(r"junior|младш|джун|стаж|\bintern\b|trainee", re.I)  # не «Internal»
_AI = re.compile(r"\bai\b|\bии\b|llm|vibe|вайб|prompt|промпт|agentic", re.I)
_JS_NEAR = re.compile(
    r"front|фронт|react|\bvue|angular|svelte|typescript|javascript|\bjs\b|node|nest|next\.?js"
    r"|full.?stack|ful+.?стек|фул+.?стек|\bweb|веб", re.I)
_NODE = re.compile(r"\bnode|\bnest|express", re.I)
_NODE_TITLE = re.compile(r"\bnode|\bnest", re.I)
_JS_STRONG = re.compile(
    r"typescript|javascript|react|\bvue|angular|svelte|\bnode|\bnest|next\.?js|frontend|фронтенд", re.I)
_AI_NON_JS = re.compile(r"unity|\bml\b|ml-|gamedev|data scien|computer vision|\bcv\b|nlp", re.I)
STACKS = [("react_native", _RN), ("react", re.compile(r"react(?!\s*native)", re.I)), ("vue", re.compile(r"\bvue", re.I)),
          ("angular", re.compile(r"angular", re.I)), ("svelte", re.compile(r"svelte", re.I))]

# Отчёт: фронт, fullstack на JS и AI-инженеры на JS/TS. Node-бэкенд и общий «веб-разработчик» не входят.
JS_MARKET = {"front", "fullstack", "ai_js"}


def classify(name: str) -> str:
    """Категория вакансии по названию. Порядок важен: QA на TS — это QA, а не фронт."""
    name = latinize(name)
    if _QA.search(name):
        return "qa"
    if _PM.search(name):
        return "nontech"
    if _NONTECH_STRICT.search(name):
        return "nontech"
    dev_lead = _LEAD.search(name) and (_FRONT_STRONG.search(name) or _FULLSTACK.search(name) or _BACKEND.search(name))
    if _MANAGER.search(name) and not dev_lead:  # «Engineering Manager (Frontend)» — это лид фронта
        return "nontech"
    if _DESKTOP_UI.search(name):
        return "other"
    if _RN.search(name):  # 06.10: React Native — во фронт (отдельной строкой стека), с fullstack — fullstack
        return "fullstack" if _FULLSTACK.search(name) else "front"
    if _MOBILE.search(name):
        return "mobile"
    backend = bool(_BACKEND.search(name))
    strong = bool(_FRONT_STRONG.search(name))
    if _FULLSTACK.search(name):
        if re.search(r"embedded|встраива", name, re.I):  # «Fullstack & Embedded» — не веб
            return "other"
        # голое «JS» внутри fullstack — JavaScript; голое «Backend» — НЕ признак JS-стека
        js_stack = (strong or _NODE_TITLE.search(name) or _FRONT_LANG.search(name)
                    or re.search(r"\bjs\b", name, re.I))
        if not js_stack and _NON_JS_STACK.search(name):
            return "fullstack_other"
        return "fullstack"
    if _OTHER_IT.search(name):  # «Senior DevOps Engineer (Front-end team)» — DevOps, не фронт
        return "other"
    if backend and strong:  # «Frontend / Node.js developer» — это fullstack
        return "fullstack"
    # фронт-фреймворк + чужой бэкенд-язык без слова fullstack: «Golang + React», «Java + Angular»
    if strong and _NON_JS_STACK.search(name) and not _CMS.search(name):
        return "fullstack"
    if _CMS.search(name) and not _FRAMEWORK.search(name):  # «Сайты WordPress / Верстальщик»
        return "other"
    if _AI.search(name) and not strong:  # AI-инженер без фронт-слов; рядом ли JS — решит _category
        return "ai"
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


def grade(name: str) -> str | None:
    """Грейд по названию; лид проверяется первым — «Senior Team Lead» это лид."""
    name = latinize(name)
    if _LEAD.search(name):
        return "lead"
    if _SENIOR.search(name):
        return "senior"
    if _MIDDLE.search(name):
        return "middle"
    if _JUNIOR.search(name):
        return "junior"
    return None


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
    stack: tuple[str, ...] = ()
    node: bool = False
    ai: bool = False
    grade_fix: str | None = None  # решение по спорному названию; «-» — грейда нет

    @property
    def grade(self) -> str | None:
        if self.grade_fix is not None:
            return None if self.grade_fix == "-" else self.grade_fix
        return grade(self.name)

    @property
    def primary_stack(self) -> str:
        """Один фреймворк на вакансию — чтобы стек складывался в итог фронта."""
        return self.stack[0] if self.stack else "js"


class Run(NamedTuple):
    id: int
    run_at: datetime
    front_total: int
    js_total: int
    slice_version: int = 2


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


def _snippet(item: dict) -> str:
    sn = item.get("snippet") or {}
    return " ".join(x for x in (sn.get("requirement"), sn.get("responsibility")) if x)


def _stack(name: str, text: str) -> tuple[str, ...]:
    """Фреймворки: сначала из названия (оно точнее), затем добавочные из требований."""
    name, text = latinize(name), latinize(text)
    in_title = [s for s, rx in STACKS if rx.search(name)]
    in_text = [s for s, rx in STACKS if rx.search(text) and s not in in_title]
    return tuple(in_title + in_text)


def parse_vacancy(item: dict, country_of: dict[str, str], it_roles: set[str] | None = None,
                  node_ids: Iterable[str] = frozenset()) -> Vacancy:
    employer = (item.get("employer") or {}).get("name") or "—"
    salary = item.get("salary") or {}
    area_id = str((item.get("area") or {}).get("id") or "")
    name = item.get("name") or "—"
    text = f"{name} {_snippet(item)}"
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
        stack=_stack(name, text),
        node=bool(_NODE.search(text)) or str(item["id"]) in set(node_ids),
        ai=bool(_AI.search(name)),
    )


def _category(item: dict, name: str, it_roles: set[str] | None) -> str:
    roles = {str(r.get("id")) for r in item.get("professional_roles") or []}
    if it_roles and roles and not roles & it_roles:
        return "other"  # не IT-вакансия, сколько бы фронт-слов ни было в названии
    category = classify(name)
    if category == "ai":
        return "ai_js" if _ai_near_js(name, _snippet(item)) else "ai"
    return category


def _ai_near_js(name: str, snippet: str) -> bool:
    """AI-вакансия близка к фронту/JS?

    06.10: признак, найденный в требованиях по словам «веб»/«fullstack», записал в JS
    «Senior AI developer (Python)» и «Offensive Security Developer (Python/Go)». Поэтому:
    JS в названии → да; чужой язык в названии → нет; в требованиях — только явный JS-стек.
    """
    if _JS_NEAR.search(name):
        return True
    if _NON_JS_STACK.search(name) or _AI_NON_JS.search(name):
        return False
    return bool(_JS_STRONG.search(snippet))


# ── возраст вакансии по её номеру ───────────────────────────────────────


class Estimate(NamedTuple):
    estimate: datetime | None   # оценка времени создания
    not_after: datetime | None  # номер старше всех опорных точек: создана не позже этого времени
    not_before: datetime | None = None  # номер новее всех точек: создана после этого времени


@dataclass
class Anchors:
    """Опорные точки «номер вакансии → время создания».

    Номера hh растут вместе с датой создания (0 нарушений на 166 карточках, 06.10.2026).
    Источники: «card» — точная дата из старых детальных карточек; «search» и «now» — максимальный
    номер среди опубликованных за час (верхняя оценка времени). При конфликте карточка важнее.
    """

    raw: list[tuple] = field(default_factory=list)

    def __post_init__(self) -> None:
        rank = {"card": 0, "search": 1, "now": 1}
        pts = sorted(((int(p[0]), p[1], p[2] if len(p) > 2 else "search") for p in self.raw),
                     key=lambda p: (p[0], rank.get(p[2], 1)))
        kept: list[tuple[int, datetime, str]] = []
        for p in pts:
            if kept and p[0] == kept[-1][0]:
                continue
            while kept and p[1] < kept[-1][1] and p[2] == "card" and kept[-1][2] != "card":
                kept.pop()  # поисковая точка завысила время — уступает карточке
            if kept and p[1] < kept[-1][1]:
                continue
            kept.append(p)
        self.points = [(i, t) for i, t, _ in kept]
        self._ids = [i for i, _ in self.points]

    def created(self, vid: int) -> Estimate:
        if not self.points:
            return Estimate(None, None)
        k = bisect.bisect_left(self._ids, vid)
        if k < len(self.points) and self._ids[k] == vid:
            return Estimate(self.points[k][1], None)
        if k == 0:
            return Estimate(None, self.points[0][1])
        if k == len(self.points):
            return Estimate(None, None, self.points[-1][1])
        (i0, t0), (i1, t1) = self.points[k - 1], self.points[k]
        return Estimate(t0 + (t1 - t0) * ((vid - i0) / (i1 - i0)), None)


def age_kind(vid: int, published: datetime, anchors: Anchors) -> tuple[str, float | None]:
    """new (< 1 дня от создания), bumped (< 30 дней), reopened (≥ 30), unknown (не доказать)."""
    est = anchors.created(vid)
    if est.estimate is not None:
        age = max(0.0, (published - est.estimate).total_seconds() / 86400)
        return ("new" if age < NEW_AGE_DAYS else "bumped" if age < REOPEN_AGE_DAYS else "reopened"), age
    if est.not_before is not None:  # новее всех точек; точка «сейчас» снимается перед выдачей
        at_most = (published - est.not_before).total_seconds() / 86400
        return ("new", 0.0) if at_most < NEW_AGE_DAYS else ("unknown", None)
    if est.not_after is not None:
        at_least = (published - est.not_after).total_seconds() / 86400
        if at_least >= REOPEN_AGE_DAYS:
            return "reopened", at_least
    return "unknown", None


def missing_anchor_days(now: datetime, have: set[str]) -> list:
    today = now.astimezone(MSK).date()
    days = [today - timedelta(days=d) for d in range(1, ANCHOR_DAYS + 1)]
    return [d for d in days if d.isoformat() not in have]


def fetch_max_id(api, start: datetime | None = None, end: datetime | None = None) -> int | None:
    """Самый большой номер среди последних опубликованных (по всему hh или в окне времени)."""
    params = dict(per_page=PER_PAGE, order_by="publication_time")
    if start is not None:
        params.update(date_from=start.isoformat(), date_to=end.isoformat())
    items = api.get("/vacancies", **params).get("items", [])
    return max((int(i["id"]) for i in items), default=None)


# ── исчезновения: скрыта или закрыта ────────────────────────────────────


def split_absent(history: list[set[str]], today: set[str]) -> tuple[list[str], list[str]]:
    """history — снимки прошлых дней, старые → новые.

    Закрыта — нет в выдаче CLOSE_AFTER_DAYS дней подряд (включая сегодня); отчитываемся один раз.
    Скрыта — пропала 1–2 дня назад: 30.09–05.10 такие вернулись в 47 случаях, а из пропавших
    на 3+ дня — ни одна из 53.
    """
    n = CLOSE_AFTER_DAYS
    closed: set[str] = set()
    if len(history) >= n:
        later = set().union(*history[-(n - 1):]) if n > 1 else set()
        closed = history[-n] - later - today
    recent = set().union(*history[-(n - 1):]) if history and n > 1 else set()
    hidden = recent - today
    return sorted(closed), sorted(hidden)


def recategorize(name: str, stored: str, it_role: int | None) -> str:
    """Категория из прошлого снимка по текущему классификатору — чтобы дельты были сравнимы."""
    if it_role == 0:
        return "other"
    category = classify(name)
    if it_role is None and stored == "other" and category != "other":
        return "other"  # старая запись без флага роли: «other» мог дать ролевой фильтр (La Vue)
    if category == "ai":
        return stored if stored in ("ai", "ai_js") else "ai"
    return category


def unique_positions(vacancies: Iterable[Vacancy]) -> int:
    """Одна позиция, размещённая в нескольких городах, — это одна позиция, а не N."""
    return len({(v.employer.strip().lower(), " ".join(v.name.lower().split())) for v in vacancies})


def top_employers(vacancies: Iterable[Vacancy], limit: int = 5) -> list[tuple[str, int]]:
    return Counter(v.employer for v in vacancies if v.employer != "—").most_common(limit)


# ── отчёты ──────────────────────────────────────────────────────────────


class ClosedVacancy(NamedTuple):
    id: str
    name: str
    employer: str
    category: str
    lifetime_days: int | None


@dataclass
class Report:
    now: datetime
    window_start: datetime
    listing: list[Vacancy]
    found: int
    collected: int
    prev: dict[str, int] | None
    new: list[tuple[Vacancy, float]] = field(default_factory=list)
    bumped: list[tuple[Vacancy, float]] = field(default_factory=list)
    reopened: list[tuple[Vacancy, float]] = field(default_factory=list)
    unknown: list[Vacancy] = field(default_factory=list)
    closed: list[ClosedVacancy] = field(default_factory=list)
    hidden: list[tuple[str, str]] = field(default_factory=list)
    history_days: int = 0

    def of(self, cat: str) -> list[Vacancy]:
        return [v for v in self.listing if v.category == cat]


DIRECTIONS = [("front", "Фронтенд"), ("fullstack", "Fullstack"), ("ai_js", "AI на JS/TS")]
STACK_LABEL = [("react", "React"), ("vue", "Vue"), ("angular", "Angular"), ("react_native", "React Native"),
               ("svelte", "Svelte"), ("js", "JS/TS без фреймворка")]
WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(MSK).strftime("%d.%m %H:%M")


def _title(name: str) -> str:
    name = name if len(name) <= LIST_TITLE_LIMIT else name[: LIST_TITLE_LIMIT - 1] + "…"
    return html.escape(name)


def _d(now: int, prev: int | None) -> str:
    return "" if prev is None else f" ({now - prev:+d})".replace("(+0)", "(0)")


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


def _stack_lines(front: list[Vacancy]) -> list[str]:
    st = Counter(v.primary_stack for v in front)
    return [f"   ◦ {label} {st[key]}" for key, label in STACK_LABEL
            if st[key] or key in ("react", "vue", "angular", "js")]


def _grade_line(vs: list[Vacancy], pct: bool = False) -> str:
    g = Counter(v.grade for v in vs)
    n = len(vs) or 1
    f = (lambda k: f"{round(100 * g[k] / n)}%") if pct else (lambda k: str(g[k]))
    return (f"Грейд: лиды {f('lead')} · senior {f('senior')} · middle {f('middle')}"
            f" · junior/стажёр {f('junior')} · не указан {f(None)}")


def _stack_short(vs: list[Vacancy]) -> str:
    st = Counter(v.primary_stack for v in vs)
    return " · ".join(f"{('JS/TS' if k == 'js' else label)} {st[k]}" for k, label in STACK_LABEL if st[k])


def _table(rows: list[tuple[str, list[str]]]) -> str:
    head = f"{'':12}{'новые':>6}{'подняли':>8}{'переоткр':>9}{'закрыты':>8}{'прирост':>8}"
    body = [f"{name:12}{c[0]:>6}{c[1]:>8}{c[2]:>9}{c[3]:>8}{c[4]:>8}" for name, c in rows]
    return "<pre>" + "\n".join([head, *body]) + "</pre>"


def quality_checks(r: Report) -> list[str]:
    """Причины не пересылать отчёт. Пусто — проверки пройдены."""
    out = []
    if r.found > HH_RESULTS_CAP:
        out.append(f"hh отдаёт не больше {HH_RESULTS_CAP} из {r.found} — хвост выдачи не виден")
    elif r.collected < r.found * COMPLETENESS:
        out.append(f"собрано {r.collected} из {r.found} — выдача неполная")
    if r.prev:
        for cat, label in DIRECTIONS[:2]:
            p, n = r.prev.get(cat, 0), len(r.of(cat))
            if p >= JUMP_MIN_BASE and abs(n - p) / p > JUMP_LIMIT:
                out.append(f"{label}: {p} → {n} за сутки (больше {round(JUMP_LIMIT * 100)}%) — проверить вручную")
    if r.unknown:
        out.append(f"у {len(r.unknown)} вакансий не удалось определить возраст — нет опорных точек")
    front = r.of("front")
    if sum(Counter(v.primary_stack for v in front).values()) != len(front):
        out.append("стек фронта не сходится с итогом")
    return out


def build_daily(r: Report) -> list[str]:
    front, full, ai_js = r.of("front"), r.of("fullstack"), r.of("ai_js")
    prev = r.prev or {}
    p = (lambda c: prev.get(c)) if r.prev else (lambda c: None)  # noqa: E731
    js_now = len(front) + len(full) + len(ai_js)
    js_prev = sum(prev.get(c, 0) for c, _ in DIRECTIONS) if r.prev else None
    leads = lambda vs: sum(v.grade == "lead" for v in vs)  # noqa: E731
    day = r.now.astimezone(MSK)

    checks = quality_checks(r)
    lines = []
    if checks:
        lines += ["⚠️ <b>Не пересылать: проверка не пройдена</b>", *[f"• {html.escape(c)}" for c in checks], ""]
    lines += [
        f"📊 <b>JS-рынок на hh · {day.strftime('%d.%m.%Y')}, {WEEKDAYS[day.weekday()]}</b>",
        "",
        f"<b>JS-рынок: {js_now}</b>" + (f" ({js_now - js_prev:+d} за сутки)" if js_prev is not None else ""),
        f"• Фронтенд: <b>{len(front)}</b>{_d(len(front), p('front'))} · уникальных позиций {unique_positions(front)}"
        f" · с AI {sum(v.ai for v in front)} · лидов {leads(front)}",
        *_stack_lines(front),
        f"• Fullstack: <b>{len(full)}</b>{_d(len(full), p('fullstack'))} · с Node.js {sum(v.node for v in full)}"
        f" · с AI {sum(v.ai for v in full)} · лидов {leads(full)}",
        f"• AI-инженеры на JS/TS: <b>{len(ai_js)}</b>{_d(len(ai_js), p('ai_js'))}",
        "",
    ]
    if r.prev is None:
        lines += ["<i>Первый снимок — изменения за сутки появятся завтра.</i>", ""]

    rows = []
    for cat, label in DIRECTIONS:
        k = lambda events: sum(v.category == cat for v, _ in events)  # noqa: E731
        closed = sum(c.category == cat for c in r.closed) if r.history_days >= CLOSE_AFTER_DAYS else "—"
        growth = f"{len(r.of(cat)) - prev.get(cat, 0):+d}".replace("+0", "0") if r.prev else "—"
        rows.append((label, [str(k(r.new)), str(k(r.bumped)), str(k(r.reopened)), str(closed), growth]))
    lines += [f"<b>За сутки</b> ({_fmt_dt(r.window_start)} → {_fmt_dt(r.now)}):", _table(rows)]

    notes = []
    f_reopened = [a for v, a in r.reopened if v.category in JS_MARKET]
    if f_reopened:
        notes.append(f"Переоткрытые — медиана возраста {round(statistics.median(f_reopened))} дн.")
    lifetimes = [c.lifetime_days for c in r.closed if c.category in JS_MARKET and c.lifetime_days is not None]
    if lifetimes:
        notes.append(f"закрытые прожили в среднем {round(statistics.mean(lifetimes))} дн.")
    if notes:
        lines.append(" · ".join(notes))
    if r.history_days < CLOSE_AFTER_DAYS:
        lines.append(f"<i>Закрытые считаем, когда вакансии нет {CLOSE_AFTER_DAYS} дня подряд —"
                     " закрытия появятся, когда накопится история.</i>")
    f_new = [v for v, _ in r.new if v.category == "front"]
    hidden = sum(cat in JS_MARKET for _, cat in r.hidden)
    lines.append(f"Временно скрыты из поиска: {hidden} · новых уникальных позиций во фронте:"
                 f" {unique_positions(f_new)} из {len(f_new)}")

    if f_new:
        top = top_employers(f_new, 3)
        lines += ["", "<b>Кто пришёл во фронт:</b>", f"Стек: {_stack_short(f_new)}", _grade_line(f_new)]
        if top and top[0][1] > 1:
            lines.append("Больше всего новых: " + " · ".join(f"{html.escape(e)} {n}" for e, n in top if n > 1))

    lines.append("")
    if not checks:
        lines.append(f"<i>✅ Проверки пройдены: собрано {r.collected} из {r.found}, суммы сходятся,"
                     " резких скачков нет.</i>")
    lines.append("<i>Срез: вакансии с фронт/JS-стеком в названии по всему hh; QA, мобильная разработка"
                 " (кроме React Native) и fullstack на чужом стеке не входят.</i>")
    messages = ["\n".join(lines)]

    def vline(v: Vacancy) -> str:
        tail = " · ".join(x for x in (dict(STACK_LABEL).get(v.primary_stack) if v.stack else None, v.grade) if x)
        return (f'• <a href="{html.escape(v.url)}">{_title(v.name)}</a> — {html.escape(v.employer)}'
                + (f" · {tail}" if tail else ""))

    messages += _pack(f"🆕 <b>Новые во фронте</b> ({len(f_new)})", [vline(v) for v in f_new])
    return messages


# ── хранилище ───────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL, window_start TEXT NOT NULL, found INTEGER,
    front_total INTEGER NOT NULL, js_total INTEGER NOT NULL,
    russia INTEGER, moscow INTEGER, spb INTEGER, other_countries INTEGER, remote INTEGER,
    new_front INTEGER, bumped_front INTEGER, reopened_front INTEGER, closed_front INTEGER, hidden INTEGER,
    slice_version INTEGER
);
CREATE TABLE IF NOT EXISTS snapshot (
    run_id INTEGER NOT NULL REFERENCES runs(id),
    vacancy_id TEXT NOT NULL,
    name TEXT, employer TEXT, category TEXT, area_id TEXT, country_id TEXT, remote INTEGER,
    published_at TEXT, salary_from INTEGER, salary_to INTEGER, currency TEXT,
    stack TEXT, node INTEGER, ai INTEGER,
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
CREATE TABLE IF NOT EXISTS id_anchors (
    max_id INTEGER NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL,
    PRIMARY KEY (source, at)
);
CREATE TABLE IF NOT EXISTS title_review (
    name TEXT PRIMARY KEY, auto_category TEXT, auto_grade TEXT, first_seen TEXT,
    agent_category TEXT, agent_grade TEXT, final_category TEXT, final_grade TEXT,
    status TEXT NOT NULL DEFAULT 'new'
);
"""
CATEGORIES = {"front", "fullstack", "fullstack_other", "backend", "web", "ai", "ai_js", "mobile", "qa",
              "nontech", "other"}


class Store:
    def __init__(self, path: Path | str) -> None:
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Догоняет схему базы ранних версий (без stack/node/ai/slice_version/it_role)."""
        wanted = {"snapshot": {"stack": "TEXT", "node": "INTEGER", "ai": "INTEGER", "it_role": "INTEGER"},
                  "runs": {"slice_version": "INTEGER", "collected": "INTEGER"}}
        for table, cols in wanted.items():
            have = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols.items():
                if col not in have:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        self.db.commit()

    # прогоны и снимки
    def last_run(self) -> Run | None:
        row = self.db.execute(
            "SELECT id, run_at, front_total, js_total, slice_version FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        return Run(row[0], datetime.fromisoformat(row[1]), row[2], row[3], row[4] or 2)

    def daily_runs(self) -> list[tuple[int, datetime]]:
        """Последний прогон каждого дня (МСК), старые → новые."""
        by_day: dict[str, tuple[int, datetime]] = {}
        for rid, run_at in self.db.execute("SELECT id, run_at FROM runs ORDER BY id"):
            t = datetime.fromisoformat(run_at)
            by_day[t.astimezone(MSK).date().isoformat()] = (rid, t)
        return [by_day[d] for d in sorted(by_day)]

    def snapshot_ids(self, run_id: int) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT vacancy_id FROM snapshot WHERE run_id = ?", (run_id,))}

    def snapshot_rows(self, run_id: int) -> list[sqlite3.Row]:
        self.db.row_factory = sqlite3.Row
        try:
            return list(self.db.execute("SELECT * FROM snapshot WHERE run_id = ?", (run_id,)))
        finally:
            self.db.row_factory = None

    def recount(self, run_id: int) -> dict[str, int]:
        overrides = self.overrides()
        c: Counter = Counter()
        for row in self.snapshot_rows(run_id):
            cat = overrides.get(row["name"], (None,))[0] or recategorize(row["name"], row["category"], row["it_role"])
            c[cat] += 1
        return dict(c)

    # опорные точки
    def anchors(self) -> Anchors:
        pts = [(i, datetime.fromisoformat(t), s) for i, t, s in self.db.execute("SELECT max_id, at, source FROM id_anchors")]
        pts += [(int(v), datetime.fromisoformat(t), "card")
                for v, t in self.db.execute("SELECT vacancy_id, initial_created_at FROM vacancy_initial")]
        return Anchors(pts)

    def anchor_days(self) -> set[str]:
        return {datetime.fromisoformat(t).astimezone(MSK).date().isoformat()
                for (t,) in self.db.execute("SELECT at FROM id_anchors WHERE source = 'search'")}

    def save_anchors(self, points: Iterable[tuple[int, datetime, str]]) -> None:
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO id_anchors VALUES (?, ?, ?)",
                                [(int(i), t.isoformat(), s) for i, t, s in points])

    # проверка названий
    def record_titles(self, vacancies: Iterable[Vacancy], golden: set[str], now: datetime) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO title_review (name, auto_category, auto_grade, first_seen, status)"
                " VALUES (?, ?, ?, ?, ?)",
                [(v.name, v.category, v.grade, now.isoformat(), "golden" if v.name in golden else "new")
                 for v in vacancies])

    def review_queue(self, limit: int = 200) -> list[dict]:
        rows = self.db.execute("SELECT name, auto_category, auto_grade FROM title_review WHERE status = 'new'"
                               " ORDER BY first_seen, name LIMIT ?", (limit,))
        return [{"name": n, "auto_category": c, "auto_grade": g} for n, c, g in rows]

    def review_submit(self, verdicts: list[dict]) -> None:
        """Независимый вердикт агента: совпал с классификатором — agreed, нет — disputed."""
        with self.db:
            for v in verdicts:
                if v["category"] not in CATEGORIES:
                    raise ValueError(f"неизвестная категория {v['category']!r} у {v['name']!r}")
                row = self.db.execute("SELECT auto_category, auto_grade FROM title_review WHERE name = ?",
                                      (v["name"],)).fetchone()
                if row is None:
                    continue
                # ai / ai_js различаются по требованиям, а не по названию — для сверки это одно
                norm = lambda c: "ai" if c in ("ai", "ai_js") else c  # noqa: E731
                agree = (norm(row[0]), row[1]) == (norm(v["category"]), v.get("grade"))
                self.db.execute("UPDATE title_review SET agent_category = ?, agent_grade = ?, status = ?"
                                " WHERE name = ?", (v["category"], v.get("grade"),
                                                    "agreed" if agree else "disputed", v["name"]))

    def review_resolve(self, verdicts: list[dict]) -> None:
        """Решение Александра по спорным: становится правдой и применяется к отчётам."""
        with self.db:
            for v in verdicts:
                if v["category"] not in CATEGORIES:
                    raise ValueError(f"неизвестная категория {v['category']!r}")
                self.db.execute("UPDATE title_review SET final_category = ?, final_grade = ?, status = 'resolved'"
                                " WHERE name = ?", (v["category"], v.get("grade"), v["name"]))

    def disputes(self) -> list[dict]:
        rows = self.db.execute("SELECT name, auto_category, auto_grade, agent_category, agent_grade"
                               " FROM title_review WHERE status = 'disputed' ORDER BY name")
        return [dict(zip(("name", "auto_category", "auto_grade", "agent_category", "agent_grade"), r)) for r in rows]

    def review_stats(self) -> dict[str, int]:
        c = Counter(s for (s,) in self.db.execute("SELECT status FROM title_review"))
        resolved_ok = self.db.execute(
            "SELECT count(*) FROM title_review WHERE status = 'resolved' AND final_category = auto_category"
            " AND coalesce(final_grade, '') = coalesce(auto_grade, '')").fetchone()[0]
        reviewed = c["agreed"] + c["disputed"] + c["resolved"]
        return {"reviewed": reviewed, "agreed": c["agreed"] + resolved_ok, "disputed": c["disputed"]}

    def overrides(self) -> dict[str, tuple[str, str | None]]:
        return {n: (c, g) for n, c, g in self.db.execute(
            "SELECT name, final_category, final_grade FROM title_review WHERE status = 'resolved'")}

    def apply_overrides(self, vacancies: list[Vacancy]) -> list[Vacancy]:
        ov = self.overrides()
        return [dataclasses.replace(v, category=ov[v.name][0], grade_fix=ov[v.name][1] or "-")
                if v.name in ov else v for v in vacancies]

    # запись прогона
    def save_run(self, r: Report, it_role_of: dict[str, int]) -> int:
        f = lambda events: sum(v.category == "front" for v, _ in events)  # noqa: E731
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (run_at, window_start, found, collected, front_total, js_total, new_front,"
                " bumped_front, reopened_front, closed_front, hidden, slice_version)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r.now.isoformat(), r.window_start.isoformat(), r.found, r.collected, len(r.of("front")),
                 sum(v.category in JS_MARKET for v in r.listing), f(r.new), f(r.bumped), f(r.reopened),
                 sum(c.category == "front" for c in r.closed), len(r.hidden), SLICE_VERSION))
            run_id = cur.lastrowid
            self.db.executemany(
                "INSERT OR REPLACE INTO snapshot (run_id, vacancy_id, name, employer, category, area_id,"
                " country_id, remote, published_at, salary_from, salary_to, currency, stack, node, ai, it_role)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(run_id, v.id, v.name, v.employer, v.category, v.area_id, v.country_id, int(v.remote),
                  v.published_at.isoformat(), v.salary_from, v.salary_to, v.currency,
                  ",".join(v.stack), int(v.node), int(v.ai), it_role_of.get(v.id)) for v in r.listing])
            age = lambda a: None if a is None else int(a)  # noqa: E731
            events = (
                [(run_id, v.id, "new", v.category, age(a)) for v, a in r.new]
                + [(run_id, v.id, "bumped", v.category, age(a)) for v, a in r.bumped]
                + [(run_id, v.id, "reopened", v.category, age(a)) for v, a in r.reopened]
                + [(run_id, c.id, "closed", c.category, c.lifetime_days) for c in r.closed]
                + [(run_id, vid, "hidden", cat, None) for vid, cat in r.hidden]
            )
            self.db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", events)
        return run_id


# ── обращение к hh ──────────────────────────────────────────────────────


def fetch_items(api, text: str, sleep=time.sleep, **params) -> tuple[dict[str, dict], int]:
    """Вся выдача запроса постранично.

    Только по дате публикации: при сортировке по релевантности (по умолчанию) страницы hh
    перекрываются — 06.10.2026 из 888 найденных собиралось 731 (−18% рынка во всех отчётах до v4).
    """
    items: dict[str, dict] = {}
    page, total = 0, 0
    while True:
        resp = api.get("/vacancies", text=text, per_page=PER_PAGE, page=page, order_by="publication_time",
                       **params)
        total = resp.get("found", 0)
        for item in resp.get("items", []):
            items[str(item["id"])] = item
        if page >= resp.get("pages", 1) - 1:
            break
        page += 1
        sleep(REQUEST_DELAY)
    return items, total


def fetch_listing(api, country_of: dict[str, str], sleep=time.sleep,
                  it_roles: set[str] | None = None) -> tuple[list[Vacancy], int, int, dict[str, int]]:
    """Основной запрос + AI-запрос по IT-ролям; Node во fullstack — по отдельному запросу.

    Возвращает (вакансии, найдено hh по основному запросу, собрано по нему, флаг IT-роли по id).
    """
    node_items, _ = fetch_items(api, FULL_NODE_QUERY, sleep)
    main, total = fetch_items(api, QUERY, sleep)
    ai_items: dict[str, dict] = {}
    if it_roles:
        ai_roles = sorted(r for r in it_roles if r != OTHER_ROLE)
        ai_items, _ = fetch_items(api, AI_QUERY, sleep, professional_role=ai_roles)
    merged = {**ai_items, **main}  # основная выдача приоритетнее
    it_role_of = {}
    for vid, it in merged.items():
        roles = {str(r.get("id")) for r in it.get("professional_roles") or []}
        it_role_of[vid] = int(not (it_roles and roles) or bool(roles & it_roles))
    listing = [parse_vacancy(i, country_of, it_roles, set(node_items)) for i in merged.values()]
    return listing, total, len(main), it_role_of


def refresh_anchors(api, store: Store, now: datetime, sleep) -> None:
    """Опорные точки за пропущенные дни (12:00–13:00 МСК) и «сейчас». Только поиск — без карточек."""
    points = []
    for day in missing_anchor_days(now, store.anchor_days()):
        start = datetime(day.year, day.month, day.day, ANCHOR_HOUR, tzinfo=MSK)
        end = start + timedelta(hours=1)
        max_id = fetch_max_id(api, start, end)
        if max_id:
            points.append((max_id, end, "search"))
        sleep(REQUEST_DELAY)
    current = fetch_max_id(api)
    if current:
        points.append((current, now, "now"))
    store.save_anchors(points)


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


def load_golden() -> set[str]:
    if not GOLDEN_PATH.exists():
        return set()
    return {line.split("\t")[0] for line in GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")}


# ── сценарий ────────────────────────────────────────────────────────────


def build_report(api, store: Store, now: datetime, sleep) -> tuple[Report, dict[str, int]]:
    last = store.last_run()
    start = last.run_at if last else now - timedelta(hours=24)
    country_of = country_index(api.get("/areas"))
    it_roles = it_role_index(api.get("/professional_roles"))
    refresh_anchors(api, store, now, sleep)
    listing, found, collected, it_role_of = fetch_listing(api, country_of, sleep, it_roles)
    listing = store.apply_overrides(listing)
    anchors = store.anchors()

    new, bumped, reopened, unknown = [], [], [], []
    for v in listing:
        if v.published_at < start:
            continue
        kind, age = age_kind(int(v.id), v.published_at, anchors)
        {"new": new, "bumped": bumped, "reopened": reopened}.get(kind, unknown).append(
            v if kind == "unknown" else (v, age))
    unknown = [x for x in unknown if x.category in JS_MARKET]

    today = now.astimezone(MSK).date()
    days = [(rid, t) for rid, t in store.daily_runs() if t.astimezone(MSK).date() < today]
    history = days[-CLOSE_AFTER_DAYS:]
    snaps = [store.snapshot_ids(rid) for rid, _ in history]
    closed_ids, hidden_ids = split_absent(snaps, {v.id for v in listing})
    closed, hidden = [], []
    if closed_ids:
        base_id, base_t = history[-CLOSE_AFTER_DAYS]
        rows = {row["vacancy_id"]: row for row in store.snapshot_rows(base_id)}
        for vid in closed_ids:
            row = rows[vid]
            cat = recategorize(row["name"], row["category"], row["it_role"])
            est = anchors.created(int(vid)).estimate
            life = (base_t - est).days if est else None
            closed.append(ClosedVacancy(vid, row["name"], row["employer"], cat, life))
    if hidden_ids:
        cats: dict[str, str] = {}
        for rid, _ in history[-(CLOSE_AFTER_DAYS - 1):]:
            for row in store.snapshot_rows(rid):
                if row["vacancy_id"] in hidden_ids:
                    cats[row["vacancy_id"]] = recategorize(row["name"], row["category"], row["it_role"])
        hidden = [(vid, cats.get(vid, "other")) for vid in hidden_ids]

    report = Report(
        now=now, window_start=start, listing=listing, found=found, collected=collected,
        prev=store.recount(last.id) if last else None,
        new=new, bumped=bumped, reopened=reopened, unknown=unknown, closed=closed, hidden=hidden,
        history_days=len(days),
    )
    return report, it_role_of


def run(api, store: Store, now: datetime | None = None, send=send, sleep=time.sleep, dry_run=False) -> Report:
    now = now or datetime.now(MSK)
    try:
        report, it_role_of = build_report(api, store, now, sleep)
    except Exception as ex:
        send(
            "⚠️ <b>Мониторинг рынка: ошибка</b>\n"
            f"{html.escape(type(ex).__name__)}: {html.escape(str(ex))[:300]}\n"
            "Снимок не сохранён, окно не сдвинуто — следующий запуск досчитает."
        )
        raise
    if not dry_run:
        store.save_run(report, it_role_of)
        store.record_titles(report.listing, load_golden(), now)
    for message in build_daily(report):
        send(message)
    return report


# ── неделя ──────────────────────────────────────────────────────────────


def weekly_data(store: Store, now: datetime) -> dict | None:
    days = [(rid, t) for rid, t in store.daily_runs() if t <= now]
    if len(days) < 2:
        return None
    end_id, end_t = days[-1]
    start = [(rid, t) for rid, t in days if t <= end_t - timedelta(days=7) + timedelta(hours=1)]
    start_id, start_t = start[-1] if start else days[0]
    week_runs = [rid for rid, t in days if start_t < t <= end_t]
    marks = ",".join("?" * len(week_runs))
    ov = store.overrides()

    def cat_of(row) -> str:
        return ov.get(row["name"], (None,))[0] or recategorize(row["name"], row["category"], row["it_role"])

    flows: dict[str, Counter] = {c: Counter() for c, _ in DIRECTIONS}
    new_front: list[tuple[sqlite3.Row, datetime]] = []
    lifetimes = []
    rows_by_run = {rid: {r["vacancy_id"]: r for r in store.snapshot_rows(rid)} for rid in week_runs}
    for rid, vid, kind, cat, age in store.db.execute(
            f"SELECT run_id, vacancy_id, kind, category, age_days FROM events WHERE run_id IN ({marks})", week_runs):
        row = rows_by_run[rid].get(vid)
        c = cat_of(row) if row is not None else cat
        if c in flows:
            flows[c][kind] += 1
        if kind == "new" and c == "front" and row is not None:
            new_front.append((row, datetime.fromisoformat(row["published_at"])))
        if kind == "closed" and c in JS_MARKET and age is not None:
            lifetimes.append(age)

    def counts(rid: int) -> tuple[Counter, Counter, int]:
        rows = store.snapshot_rows(rid)
        cats = Counter(cat_of(r) for r in rows)
        front = [r for r in rows if cat_of(r) == "front"]
        stacks = Counter((r["stack"] or "").split(",")[0] or "js" for r in front)
        return cats, stacks, len({(r["employer"].strip().lower(), " ".join(r["name"].lower().split())) for r in front})

    (c0, s0, u0), (c1, s1, u1) = counts(start_id), counts(end_id)
    node = lambda rid: sum(1 for r in store.snapshot_rows(rid) if cat_of(r) == "fullstack" and r["node"])  # noqa: E731
    by_day = Counter(t.astimezone(MSK).weekday() for _, t in new_front)
    emp = Counter(r["employer"] for r, _ in new_front)
    mass = []
    for e, n in emp.most_common():
        names = {" ".join(r["name"].lower().split()) for r, _ in new_front if r["employer"] == e}
        if n >= 4 and len(names) == 1:
            mass.append((e, n))
    grades = Counter(grade(r["name"]) for r, _ in new_front)
    stacks_new = Counter((r["stack"] or "").split(",")[0] or "js" for r, _ in new_front)
    return {
        "start": start_t, "end": end_t, "start_counts": c0, "end_counts": c1, "stacks0": s0, "stacks1": s1,
        "unique0": u0, "unique1": u1, "node0": node(start_id), "node1": node(end_id), "flows": flows,
        "new_by_weekday": by_day, "lifetimes": lifetimes, "new_front": len(new_front),
        "grades_new": grades, "stacks_new": stacks_new, "employers_new": emp.most_common(5), "mass": mass,
        "review": store.review_stats(),
    }


def build_weekly(store: Store, now: datetime) -> list[str]:
    w = weekly_data(store, now)
    if w is None:
        return ["📈 Недельный отчёт: в базе меньше двух дней истории — считать не из чего."]
    c0, c1 = w["start_counts"], w["end_counts"]
    js0, js1 = sum(c0[c] for c, _ in DIRECTIONS), sum(c1[c] for c, _ in DIRECTIONS)
    pct = f", {round(100 * (js1 - js0) / js0):+d}%" if js0 else ""
    first, last = w["start"], w["end"]
    f0, f1 = c0["front"] or 1, c1["front"] or 1

    def share(key: str) -> str:
        a, b = round(100 * w["stacks0"][key] / f0), round(100 * w["stacks1"][key] / f1)
        return f"{a}%" if a == b else f"{b}% ({b - a:+d} п.п.)"

    rows = []
    for cat, label in DIRECTIONS:
        fl = w["flows"][cat]
        rows.append((label, [str(fl["new"]), str(fl["bumped"]), str(fl["reopened"]), str(fl["closed"]),
                             f"{c1[cat] - c0[cat]:+d}".replace("+0", "0")]))
    by_day = w["new_by_weekday"]
    lines = [
        f"📈 <b>JS-рынок на hh · неделя {first.astimezone(MSK).strftime('%d.%m')}–"
        f"{(last - timedelta(days=1)).astimezone(MSK).strftime('%d.%m.%Y')}</b>",
        "",
        f"<b>JS-рынок: {js0} → {js1}</b> ({js1 - js0:+d}{pct})",
        f"• Фронтенд: {c0['front']} → <b>{c1['front']}</b> ({c1['front'] - c0['front']:+d})"
        f" · уникальных позиций {w['unique0']} → {w['unique1']}",
        "   ◦ " + " · ".join(f"{label} {share(key)}" for key, label in STACK_LABEL
                             if w["stacks1"][key] or w["stacks0"][key]).replace("JS/TS без фреймворка", "JS/TS"),
        f"• Fullstack: {c0['fullstack']} → <b>{c1['fullstack']}</b> ({c1['fullstack'] - c0['fullstack']:+d})"
        f" · с Node.js {w['node0']} → {w['node1']}",
        f"• AI-инженеры на JS/TS: {c0['ai_js']} → <b>{c1['ai_js']}</b> ({c1['ai_js'] - c0['ai_js']:+d})",
        "",
        "<b>Поток за неделю:</b>",
        _table(rows),
        "Новые во фронте по дням: " + " · ".join(f"{WEEKDAYS[d]} {by_day[d]}" for d in range(7)),
    ]
    if w["lifetimes"]:
        lines.append(f"Закрытые прожили в среднем {round(statistics.mean(w['lifetimes']))} дн.")
    if w["new_front"]:
        g, n = w["grades_new"], w["new_front"]
        p = lambda k: f"{round(100 * g[k] / n)}%"  # noqa: E731
        st = w["stacks_new"]
        lines += [
            "",
            "<b>Кто пришёл во фронт за неделю:</b>",
            "Стек: " + " · ".join(f"{('JS/TS' if k == 'js' else label)} {st[k]}" for k, label in STACK_LABEL if st[k]),
            f"Грейд: лиды {p('lead')} · senior {p('senior')} · middle {p('middle')}"
            f" · junior/стажёр {p('junior')} · не указан {p(None)}",
        ]
        mass = {e for e, _ in w["mass"]}
        top = [f"{html.escape(e)} {k}" for e, k in w["employers_new"] if k > 1 and e not in mass]
        line = "Работодатели недели: " + " · ".join(top) if top else ""
        if w["mass"]:
            line += (" · " if line else "") + "массово: " + ", ".join(
                f"{html.escape(e)} (1 позиция × {k})" for e, k in w["mass"])
        if line:
            lines.append(line)
    rv = w["review"]
    if rv["reviewed"]:
        lines += ["", f"<i>Точность классификатора на проверенных новых названиях: "
                      f"{100 * rv['agreed'] / rv['reviewed']:.1f}% (проверено {rv['reviewed']},"
                      f" спорных ждут решения: {rv['disputed']}).</i>"]
    return ["\n".join(lines)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=CONFIG_DIR / "market_v2.db")
    parser.add_argument("--dry-run", action="store_true", help="посчитать и напечатать, не сохраняя")
    parser.add_argument("--weekly", action="store_true", help="недельный отчёт по базе (без запросов к hh)")
    parser.add_argument("--review-queue", action="store_true", help="JSON: новые названия на проверку агенту")
    parser.add_argument("--review-submit", type=Path, help="JSON-вердикты агента [{name, category, grade}]")
    parser.add_argument("--review-resolve", type=Path, help="JSON-решения Александра по спорным")
    parser.add_argument("--disputes", action="store_true", help="JSON: спорные названия")
    parser.add_argument("--note", help="отправить текст в бот (вывод недели от агента)")
    parser.add_argument("--note-file", type=Path, help="отправить в бот текст из файла (UTF-8, HTML)")
    args = parser.parse_args()
    sender = (lambda text: print(text, end="\n\n")) if args.dry_run else send
    store = Store(args.db)
    if args.weekly:
        for m in build_weekly(store, datetime.now(MSK)):
            sender(m)
    elif args.review_queue:  # только названия: агент классифицирует независимо, не видя ответа
        print(json.dumps([q["name"] for q in store.review_queue()], ensure_ascii=False, indent=1))
    elif args.review_submit:
        store.review_submit(json.loads(args.review_submit.read_text()))
        print(json.dumps(store.review_stats(), ensure_ascii=False))
    elif args.review_resolve:
        store.review_resolve(json.loads(args.review_resolve.read_text()))
        print(json.dumps(store.review_stats(), ensure_ascii=False))
    elif args.disputes:
        print(json.dumps(store.disputes(), ensure_ascii=False, indent=1))
    elif args.note:
        sender(args.note)
    elif args.note_file:
        send(args.note_file.read_text(encoding="utf-8"))
    else:
        run(Api(load_token()), store, send=sender, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
