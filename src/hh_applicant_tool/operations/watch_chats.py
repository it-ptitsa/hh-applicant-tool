"""Сторож чатов: есть ли новое сообщение от работодателя (13.10.2026).

Чтобы отвечать в чатах оперативно, а не дважды в день, раз в 5 минут делаем ОДИН лёгкий запрос — первая
страница списка чатов веб-чата hh — и сравниваем время последнего входящего сообщения с запомненным.
Тяжёлые reply-employers и answer-buttons запускаются, только если пришло новое (см. chat_watch.sh).
Счётчик непрочитанных не годится: он упирается в 100 и без чтения чатов не уменьшается.

Код выхода: 0 — есть новое (время записано как pending), 1 — нового нет.
`watch-chats --commit` после успешных ответов переносит pending в seen.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

CHATIK = "https://chatik.hh.ru"
STATE_FILENAME = "chat_watch.json"


def _ts(value: str) -> datetime:
    # веб-чат отдаёт наносекунды («…:37.344164632+03:00») — fromisoformat понимает до микросекунд
    return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))


def newest_incoming(chats: list[dict]) -> str | None:
    """Время самого свежего последнего сообщения, написанного НЕ нами."""
    best = None
    for ch in chats:
        last = ch.get("lastMessage") or {}
        when = last.get("creationTime")
        if not when or str(last.get("participantId")) == str(ch.get("currentParticipantId")):
            continue
        if best is None or _ts(when) > _ts(best):
            best = when
    return best


def should_run(newest: str | None, seen: str | None) -> bool:
    if newest is None:
        return False
    return seen is None or _ts(newest) > _ts(seen)


class Namespace(BaseNamespace):
    commit: bool


class Operation(BaseOperation):
    """Проверить, написал ли работодатель что-то новое (для оперативных ответов по крону)."""

    __aliases__ = []

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--commit", action="store_true",
                            help="Отметить найденное новое как обработанное (после успешных ответов)")

    def run(self, tool: HHApplicantTool, args: Namespace) -> int:
        path = tool.config_path / STATE_FILENAME
        try:
            state = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            state = {}
        if args.commit:
            if state.get("pending"):
                state["seen"] = state.pop("pending")
                path.write_text(json.dumps(state))
            return 0
        s = tool.session
        s.headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        items = s.get(f"{CHATIK}/chatik/api/chats", params={"page": 0, "perPage": 50}, timeout=30).json()["chats"]["items"]
        newest = newest_incoming(items)
        if not should_run(newest, state.get("seen")):
            return 1
        state["pending"] = newest
        path.write_text(json.dumps(state))
        print(f"🔔 Новое сообщение в чатах: {newest}")
        return 0
