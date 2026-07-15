#!/usr/bin/env python3
"""Сводка по чатам с работодателями → Telegram.

Смысл: бот сам отвечает в чатах (reply-employers), и живые договорённости
тонут в потоке автоответов. Скрипт вытаскивает то, что требует ЧЕЛОВЕКА:
приглашения, вопросы рекрутера, тестовые, офферы, назначенные созвоны.

Запуск (внутри контейнера утилиты, конфиг берётся из CONFIG_DIR):
    docker compose run --rm -T -u docker hh_applicant_tool \
        python /app/chat_digest.py --hours 24

Токен бота и чат — через окружение:
    TELEGRAM_NOTIFY_BOT_TOKEN, TELEGRAM_NOTIFY_CHAT_ID
Без них сводка просто печатается в stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CONFIG_DIR = Path(os.getenv("CONFIG_DIR") or Path.home() / ".config" / "hh-applicant-tool")
API_URL = "https://api.hh.ru"
TG_LIMIT = 4000

# Что считаем «требует внимания». Порядок важен: первая сработавшая — она и есть.
RULES: list[tuple[str, str, re.Pattern]] = [
    (
        "offer",
        "💰 Оффер / предложение",
        re.compile(r"\bоффер|предложение о работе|готовы предложить", re.I),
    ),
    (
        "contact",
        "✍️ Зовут в личку (Telegram/телефон/почта)",
        re.compile(
            # Работодатель уводит контакт из hh во внешний канал. Пропустить
            # это — потерять живой контакт: именно так вышло со StudyWorld.
            r"@[\w.]{3,}|"  # @username
            r"\bt\.me/|"
            r"телеграм|телеграмм|telegram|"
            r"напишите (?:мне|нам)?\s*(?:в|на)|"
            r"позвоните|перезвоните|наберите|звоните|"
            r"свяжитесь со мной|"
            r"мой (?:телефон|номер)|"
            r"\+7[\s\-(]?\d{3}|"  # телефон +7 ...
            r"\bwhats\s?app|\bватсап|\bwa\.me",
            re.I,
        ),
    ),
    (
        "interview",
        "📞 Зовут на собес / созвон",
        re.compile(
            r"собеседовани|интервью|созвон|видеозвон|созвониться|"
            r"когда вам удобно|удобно ли вам|назначим|пригласить вас|"
            r"готовы пообщаться|позна?комиться|обсудить детали|"
            r"хотели бы (?:с вами )?связаться|обсудить (?:вакансию|позицию)",
            re.I,
        ),
    ),
    (
        "test_task",
        "🧪 Тестовое задание",
        re.compile(r"тестово[ег] задани|тестовое", re.I),
    ),
    (
        "question",
        "❓ Вопрос от работодателя",
        re.compile(r"\?|расскажите|уточните|подскажите|какой у вас|ваши ожидания", re.I),
    ),
]

REJECTION = re.compile(
    r"к сожалению|не готовы предложить|отказ|не подходит|"
    r"выбрали другого|вакансия закрыта",
    re.I,
)


def load_token() -> str:
    config = json.loads((CONFIG_DIR / "config.json").read_text())
    token = config.get("token", {}).get("access_token")
    if not token:
        sys.exit("Нет access_token в config.json — сначала authorize")
    return token


class Api:
    def __init__(self, token: str) -> None:
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "User-Agent": "hh-applicant-tool chat-digest",
            }
        )

    def get(self, path: str, **params) -> dict:
        r = self.s.get(f"{API_URL}{path}", params=params, timeout=30)
        r.raise_for_status()
        return r.json()


def classify(text: str) -> tuple[str, str] | None:
    """(код, заголовок) или None, если сообщение не требует внимания."""
    if REJECTION.search(text):
        return None
    for code, title, pattern in RULES:
        if pattern.search(text):
            return code, title
    return None


def collect(api: Api, hours: int) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    interesting: list[dict] = []

    page = 0
    while True:
        res = api.get("/negotiations", page=page, per_page=100)
        for item in res.get("items", []):
            updated = datetime.fromisoformat(item["updated_at"])
            if updated < since:
                continue
            if (item.get("state") or {}).get("id") == "discard":
                continue

            messages = api.get(f"/negotiations/{item['id']}/messages").get("items", [])
            fresh = [
                m
                for m in messages
                if (m.get("author") or {}).get("participant_type") == "employer"
                and datetime.fromisoformat(m["created_at"]) >= since
            ]
            if not fresh:
                continue

            # Классифицируем по всем свежим репликам работодателя: важное могло
            # прийти не последним сообщением. Берём совпадение с наивысшим
            # приоритетом (порядок RULES).
            priority = {code: i for i, (code, *_r) in enumerate(RULES)}
            hit = None
            for m in fresh:
                cand = classify(m.get("text") or "")
                if cand and (hit is None or priority[cand[0]] < priority[hit[0]]):
                    hit = cand

            state_id = (item.get("state") or {}).get("id")
            # Приглашение (state=interview) — всегда важно, даже если текст не
            # совпал ни с одним правилом (бывает приглашение без слов-триггеров).
            if hit is None and state_id == "interview":
                hit = ("interview", "📞 Приглашение / собеседование")
            if not hit:
                continue

            vacancy = item.get("vacancy") or {}
            employer = vacancy.get("employer") or {}
            last = fresh[-1]
            my_last = [
                m
                for m in messages
                if (m.get("author") or {}).get("participant_type") == "applicant"
            ]
            answered = bool(
                my_last
                and datetime.fromisoformat(my_last[-1]["created_at"])
                > datetime.fromisoformat(last["created_at"])
            )
            # «Зовут в личку» — действие ВСЕГДА на тебе (написать в TG/позвонить),
            # даже если бот ответил в hh-чате «напишу». Иначе StudyWorld-кейс:
            # бот пообещал за тебя, а ты не в курсе.
            if hit[0] == "contact":
                answered = False

            interesting.append(
                {
                    "code": hit[0],
                    "title": hit[1],
                    "employer": employer.get("name") or "—",
                    "vacancy": vacancy.get("name") or "—",
                    "url": vacancy.get("alternate_url") or item.get("url") or "",
                    "chat_url": f"https://hh.ru/negotiations/item/{item['id']}",
                    "text": (last.get("text") or "").strip(),
                    "at": datetime.fromisoformat(last["created_at"]).strftime("%d.%m %H:%M"),
                    "answered": answered,
                    "state": (item.get("state") or {}).get("name") or "",
                }
            )

        if page >= res.get("pages", 1) - 1:
            break
        page += 1

    order = {"offer": 0, "contact": 1, "interview": 2, "test_task": 3, "question": 4}
    interesting.sort(key=lambda x: order.get(x["code"], 9))
    return interesting


def build_message(items: list[dict], hours: int) -> str:
    if not items:
        return f"🤖 hh-чаты за {hours}ч: ничего требующего внимания."

    need_answer = [i for i in items if not i["answered"]]
    head = [
        f"🤖 <b>hh-чаты за {hours}ч</b>",
        f"Требуют внимания: <b>{len(items)}</b> · без твоего ответа: <b>{len(need_answer)}</b>",
        "",
    ]

    lines: list[str] = []
    current = None
    for i in items:
        if i["title"] != current:
            current = i["title"]
            lines.append(f"\n<b>{current}</b>")
        flag = "" if i["answered"] else " 🔴"
        snippet = re.sub(r"\s+", " ", i["text"])[:180]
        lines.append(
            f"• <a href=\"{i['chat_url']}\">{i['employer']}</a> — {i['vacancy']}{flag}\n"
            f"  <i>{i['at']}:</i> {snippet}"
        )
    return "\n".join(head + lines)


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_NOTIFY_CHAT_ID")
    if not (token and chat_id):
        print(text)
        return

    for chunk_start in range(0, len(text), TG_LIMIT):
        chunk = text[chunk_start : chunk_start + TG_LIMIT]
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=30,
        )
        if not r.ok:
            print("Telegram error:", r.text, file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="Окно, за которое смотрим чаты")
    parser.add_argument("--print", action="store_true", help="Только вывести, не слать в ТГ")
    args = parser.parse_args()

    api = Api(load_token())
    items = collect(api, args.hours)
    message = build_message(items, args.hours)

    if args.print:
        print(message)
    else:
        send_telegram(message)


if __name__ == "__main__":
    main()
