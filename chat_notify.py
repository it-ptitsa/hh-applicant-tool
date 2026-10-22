#!/usr/bin/env python3
"""Уведомление сторожа чатов в Telegram: одна запись на вакансию, без повторов за сутки.

На аккаунте с несколькими резюме у каждого резюме свой чат с работодателем, и робот-рекрутер шлёт одну
и ту же анкету в каждый. reply-employers и answer-buttons печатают по строке на чат, поэтому раньше в ТГ
приходило «📋 Анкета — обработай вручную» ×5 по одной вакансии (22.10.2026). Здесь строки склеиваются
по вакансии с числом резюме, а то, о чём уже писали за последние --hours часов, не повторяется.

Вход — stdout операций (stdin), строки, которые сторож пересылал и раньше:
    📋 Анкета — обработай вручную: <url>               reply-employers, AI отказался отвечать
    📨 Отправлено для <url>                             reply-employers, ответ ушёл
    ⏸  <url> | <вопрос> | варианты: [...] → решай сам   answer-buttons, оставлено человеку
    ✅ <url> | <вопрос> → «<ответ>»                     answer-buttons, нажата кнопка
       ⚠️ отправка не прошла: <код>                     answer-buttons, относится к строке выше

    chat_watch.sh: ... | python3 chat_notify.py --label Синицын --state .chat_notify_seen_acc5.json
Токен и чат — TELEGRAM_NOTIFY_BOT_TOKEN / TELEGRAM_NOTIFY_CHAT_ID; без них сообщение печатается.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ️? — эмодзи пишут то с селектором варианта, то без; правка эмодзи в операции не должна ломать разбор
PATTERNS = [
    ("form", re.compile(r"^📋️? Анкета — обработай вручную:\s*(?P<url>\S+)")),
    ("replied", re.compile(r"^📨️? Отправлено для (?P<url>\S+)")),
    ("ask", re.compile(r"^⏸️?\s+(?P<url>\S+) \| (?P<q>.*?) \| варианты: (?P<opts>.*) → решай сам\s*$")),
    ("answered", re.compile(r"^[✅🧪]️? (?P<url>\S+) \| (?P<q>.*) → «(?P<a>.*)»\s*$")),
    ("failed", re.compile(r"^\s*⚠️?\s*отправка не прошла: (?P<code>\S+)")),
]


def _options(raw: str) -> str:
    try:
        return " / ".join(ast.literal_eval(raw))
    except (ValueError, SyntaxError, TypeError):
        return raw


def parse(lines) -> list[dict]:
    """События в порядке появления. Ключ события — что именно случилось в вакансии, без учёта резюме."""
    events: list[dict] = []
    last_url = None
    for line in lines:
        for kind, rx in PATTERNS:
            m = rx.match(line)
            if not m:
                continue
            g = m.groupdict()
            url = g.get("url") or last_url
            if not url:
                break
            last_url = url
            q = g.get("q", "")
            text = {
                "form": "📋 Анкета — обработай вручную",
                "replied": "📨 Ответил в чате",
                "ask": f"⏸ «{q}» — решай сам",
                "answered": f"✅ «{q}» → «{g.get('a')}»",
                "failed": f"⚠️ Ответ кнопкой не отправился ({g.get('code')})",
            }[kind]
            events.append({"kind": kind, "url": url, "text": text, "key": f"{url}|{text}",
                           "extra": f"   варианты: {_options(g['opts'])}" if kind == "ask" else ""})
            break
    return events


def render(events: list[dict], label: str) -> str:
    if not events:
        return ""
    by_url: dict[str, dict[str, list]] = {}
    for e in events:
        group = by_url.setdefault(e["url"], {})
        group.setdefault(e["key"], [e, 0])[1] += 1
    lines = [f"💬 Ответы в чатах hh{f' · {label}' if label else ''}"]
    for url, group in by_url.items():
        lines += ["", url]
        for e, n in group.values():
            lines.append(e["text"] + (f" · {n} резюме" if n > 1 else ""))
            if e["extra"]:
                lines.append(e["extra"])
    return "\n".join(lines)


def filter_seen(events: list[dict], seen: dict, now: datetime, hours: int) -> tuple[list[dict], dict]:
    """Убрать события, о которых уже писали за последние `hours` часов; вернуть их и новое состояние."""
    since = now - timedelta(hours=hours)
    alive = {k: ts for k, ts in seen.items() if datetime.fromisoformat(ts) >= since}
    fresh = [e for e in events if e["key"] not in alive]
    for e in fresh:
        alive[e["key"]] = now.isoformat()
    return fresh, alive


def tg_payload(chat_id: str, text: str) -> dict:
    return {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}


def send(token: str, chat_id: str, text: str) -> None:
    api = os.getenv("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
    req = urllib.request.Request(f"{api}/bot{token}/sendMessage", data=json.dumps(tg_payload(chat_id, text)).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if not body.get("ok"):
        raise RuntimeError(f"Telegram: {body}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", default="", help="Подпись аккаунта в заголовке")
    p.add_argument("--state", help="Файл с тем, о чём уже писали; без него повторы не отсекаются")
    p.add_argument("--hours", type=int, default=24, help="Сколько часов не повторять одно и то же")
    args = p.parse_args()

    now = datetime.now(timezone.utc)
    state = Path(args.state) if args.state else None
    seen = json.loads(state.read_text()) if state and state.exists() else {}
    events, seen = filter_seen(parse(sys.stdin.read().splitlines()), seen, now, args.hours)
    text = render(events, args.label.strip())
    if text:
        token, chat_id = os.getenv("TELEGRAM_NOTIFY_BOT_TOKEN"), os.getenv("TELEGRAM_NOTIFY_CHAT_ID")
        if token and chat_id:
            send(token, chat_id, text)  # упало — состояние не пишем, в следующий раз повторим
        else:
            print(text)
    if state:
        state.write_text(json.dumps(seen, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
