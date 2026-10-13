"""Ответы роботу-рекрутеру кнопками в чатах hh.

Бот работодателя («Робот-рекрутер») задаёт вопросы с готовыми вариантами ответа (actions.text_buttons)
и не принимает свободный текст. Публичный API кнопок не показывает, поэтому reply-employers уводит такие
чаты в «Анкета — обработай вручную». Команда читает веб-чат chatik.hh.ru веб-сессией аккаунта (те же
куки, что для каптчи), просит AI выбрать вариант по фактам резюме и настройкам кандидата и отправляет
точный текст кнопки обычным сообщением.

Проверено вживую 12.10.2026: с metadata веб-чата (source = chat_text_button) /chatik/api/send отвечает
500, точный текст кнопки без неё — 200, бот засчитывает ответ и завершает скрининг.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import uuid
from typing import TYPE_CHECKING

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

CHATIK = "https://chatik.hh.ru"
SKIP = "__SKIP__"
PREFS_FILENAME = "candidate_prefs.txt"

SYSTEM_PROMPT = """Ты отвечаешь роботу-рекрутеру работодателя от имени соискателя. Робот задаёт вопрос и
даёт варианты ответа. Выбери ОДИН вариант, который правдиво соответствует фактам резюме и настройкам
кандидата. Выведи только номер варианта (1, 2, 3…). Если ни резюме, ни настройки не дают однозначного
ответа (личная договорённость о деньгах, сроках, тестовом), выведи строго __SKIP__.
Не выбирай ответ, который противоречит резюме или настройкам."""


def pending_question(data: dict) -> dict | None:
    """Вопрос ждёт ответа: последнее сообщение чата — от собеседника и с кнопками."""
    chat = data.get("chat") or {}
    items = (chat.get("messages") or {}).get("items") or []
    if not items:
        return None
    me = str(chat.get("currentParticipantId"))
    last = items[-1]
    buttons = (last.get("actions") or {}).get("text_buttons") or []
    if str(last.get("participantId")) == me or not buttons:
        return None
    history = [
        f"{'Я' if str(m.get('participantId')) == me else 'Работодатель'}: {m['text']}"
        for m in items[-8:]
        if m.get("text")
    ]
    return {
        "chat_id": chat["id"],
        "message_id": last["id"],
        "vacancy_id": ((chat.get("resources") or {}).get("VACANCY") or [None])[0],
        "question": last.get("text") or "",
        "options": [b["text"] for b in buttons],
        "history": history,
    }


def parse_choice(answer: str, n: int) -> int | None:
    """Номер варианта из ответа AI → индекс; __SKIP__, мусор и номер вне диапазона → None."""
    if SKIP in answer:
        return None
    m = re.search(r"\d+", answer)
    if not m:
        return None
    k = int(m.group(0))
    return k - 1 if 1 <= k <= n else None


def send_payload(chat_id: int, text: str, key: str | None = None) -> dict:
    """Точный текст кнопки обычным сообщением (без metadata — см. docstring модуля)."""
    return {"chatId": chat_id, "idempotencyKey": key or str(uuid.uuid4()), "text": text}


def iter_unique_chats(fetch_page, max_pages: int):
    """Чаты без повторов. Веб-чат на разных страницах отдаёт одни и те же чаты (13.10: 12 → 3), поэтому
    дубли отбрасываем, а страницу без новых чатов считаем концом списка."""
    seen = set()
    for page in range(max_pages):
        items = fetch_page(page)
        fresh = [c for c in items if c["id"] not in seen]
        if not fresh:
            return
        for c in fresh:
            seen.add(c["id"])
            yield c


def build_query(q: dict, resume: str, prefs: str) -> str:
    opts = "\n".join(f"{i}. {o}" for i, o in enumerate(q["options"], 1))
    return (
        f"=== РЕЗЮМЕ (факты) ===\n{resume}\n\n=== НАСТРОЙКИ КАНДИДАТА ===\n{prefs}\n\n"
        "=== ПЕРЕПИСКА ===\n" + "\n".join(q["history"]) + f"\n\n=== ВОПРОС ===\n{q['question']}\n\n"
        f"=== ВАРИАНТЫ ===\n{opts}\n\nНомер варианта или __SKIP__:"
    )


def resume_context(resume: dict) -> str:
    head = {k: resume.get(k) for k in ("title", "area", "relocation", "business_trip_readiness",
                                       "skills", "skill_set", "schedules", "employments")}
    jobs = "\n".join(
        f"{e.get('company')} ({e.get('start')} — {e.get('end') or 'н.в.'}), {e.get('position')}"
        for e in resume.get("experience") or []
    )
    return json.dumps(head, ensure_ascii=False)[:6000] + "\n" + jobs


class Namespace(BaseNamespace):
    send: bool
    pages: int
    prefs_file: str | None


class Operation(BaseOperation):
    """Ответить роботу-рекрутеру кнопками в чатах (по умолчанию — только показать выбор)."""

    __aliases__ = ["buttons"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--send", action="store_true",
                            help="Отправить ответы. Без флага — только показать, что будет выбрано")
        parser.add_argument("--pages", type=int, default=3, help="Сколько страниц чатов по 50 просмотреть")
        parser.add_argument("--prefs-file", help=f"Настройки кандидата (по умолчанию {PREFS_FILENAME} в папке профиля)")

    def run(self, tool: HHApplicantTool, args: Namespace) -> None:
        s = tool.session
        s.headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
        prefs_path = args.prefs_file or str(tool.config_path / PREFS_FILENAME)
        try:
            prefs = open(prefs_path, encoding="utf-8").read()
        except FileNotFoundError:
            logger.warning("Нет настроек кандидата %s — AI решает только по резюме", prefs_path)
            prefs = "(настройки не заданы)"
        resumes = tool.get_resumes()
        if not resumes:
            print("Нет резюме")
            return
        resume = resume_context(tool.api_client.get(f"/resumes/{resumes[0]['id']}"))
        ai = tool.get_cover_letter_ai(SYSTEM_PROMPT)

        sent = skipped = chosen = 0
        def fetch_page(page: int) -> list[dict]:
            return s.get(f"{CHATIK}/chatik/api/chats", params={"page": page, "perPage": 50},
                         timeout=30).json()["chats"]["items"]

        for ch in iter_unique_chats(fetch_page, args.pages):
                if ch.get("type") != "NEGOTIATION":
                    continue
                data = s.get(f"{CHATIK}/chatik/api/chat_data", params={"chatId": ch["id"]}, timeout=30).json()
                q = pending_question(data)
                if not q:
                    continue
                k = parse_choice(ai.complete(build_query(q, resume, prefs)), len(q["options"]))
                url = f"https://hh.ru/vacancy/{q['vacancy_id']}"
                if k is None:
                    skipped += 1
                    print(f"⏸  {url} | {q['question'][:90]} | варианты: {q['options']} → решай сам", flush=True)
                    continue
                chosen += 1
                print(f"{'✅' if args.send else '🧪'} {url} | {q['question'][:90]} → «{q['options'][k]}»", flush=True)
                if not args.send:
                    continue
                xsrf = next((c.value for c in s.cookies if c.name == "_xsrf"), "")
                r = s.post(f"{CHATIK}/chatik/api/send", params={"hhtmSourceLabel": "chat", "hhtmSource": "chat"},
                           json=send_payload(q["chat_id"], q["options"][k]),
                           headers={"X-Xsrftoken": xsrf, "Content-Type": "application/json", "Origin": CHATIK,
                                    "Referer": f"{CHATIK}/chat/{q['chat_id']}"}, timeout=30)
                if r.status_code >= 300:
                    print(f"   ⚠️ отправка не прошла: {r.status_code}", flush=True)
                    continue
                sent += 1
        print(f"Итого: {'отправлено ' + str(sent) if args.send else 'выбрано ' + str(chosen) + ' (без --send)'}, "
              f"оставлено тебе {skipped}", flush=True)
