#!/usr/bin/env python3
"""Ответы на вопросы робота-рекрутера кнопками в чатах hh (12.10.2026).

Бот работодателя задаёт вопрос с готовыми вариантами (actions.text_buttons) и не принимает свободный
текст. reply-employers видит только публичный API, где кнопок нет, и уводит такие чаты в «Анкета —
обработай вручную». Этот модуль читает веб-чат (chatik.hh.ru) веб-сессией аккаунта, просит нейронку
выбрать вариант по фактам резюме и настройкам кандидата и отправляет его так же, как нажатие кнопки:
POST /chatik/api/send с metadata.source = chat_text_button (найдено в бандле веб-чата hh).

Запуск в контейнере кликера: python /app/chat_buttons.py [--send]   (без --send — только показать)
Настройки кандидата: /app/config/candidate_prefs.txt (формат, город, переезд, оформление и т. п.).
"""

from __future__ import annotations

import argparse
import re
import uuid

CHATIK = "https://chatik.hh.ru"
SKIP = "__SKIP__"


def pending_question(data: dict) -> dict | None:
    """Последнее сообщение — от собеседника, с кнопками → вопрос ждёт ответа."""
    chat = data.get("chat") or {}
    items = (chat.get("messages") or {}).get("items") or []
    if not items:
        return None
    me = str(chat.get("currentParticipantId"))
    last = items[-1]
    buttons = ((last.get("actions") or {}).get("text_buttons")) or []
    if str(last.get("participantId")) == me or not buttons:
        return None
    history = []
    for m in items[-8:]:
        if not m.get("text"):
            continue
        who = "Я" if str(m.get("participantId")) == me else "Работодатель"
        history.append(f"{who}: {m['text']}")
    return {"chat_id": chat["id"], "message_id": last["id"],
            "vacancy_id": ((chat.get("resources") or {}).get("VACANCY") or [None])[0],
            "question": last.get("text") or "", "options": [b["text"] for b in buttons], "history": history}


def parse_choice(answer: str, n: int) -> int | None:
    if SKIP in answer:
        return None
    m = re.search(r"\d+", answer)
    if not m:
        return None
    k = int(m.group(0))
    return k - 1 if 1 <= k <= n else None


def send_payload(chat_id: int, message_id: int, text: str, key: str | None = None) -> dict:
    return {"chatId": chat_id, "idempotencyKey": key or str(uuid.uuid4()), "text": text,
            "metadata": {"source": {"source_type": "chat_text_button"}, "messageId": message_id,
                         "chatType": "NEGOTIATION", "location": "chatik"}}


SYSTEM = """Ты отвечаешь роботу-рекрутеру работодателя от имени соискателя. Робот задаёт вопрос и даёт
варианты ответа. Выбери ОДИН вариант, который правдиво соответствует фактам резюме и настройкам кандидата.
Выведи только номер варианта (1, 2, 3…). Если ни резюме, ни настройки не дают однозначного ответа
(например, нужна личная договорённость о деньгах, сроках, тестовом), выведи строго __SKIP__.
Не выбирай ответ, который противоречит резюме или настройкам."""


def build_query(q: dict, resume: str, prefs: str) -> str:
    opts = "\n".join(f"{i}. {o}" for i, o in enumerate(q["options"], 1))
    return (f"=== РЕЗЮМЕ (факты) ===\n{resume}\n\n=== НАСТРОЙКИ КАНДИДАТА ===\n{prefs}\n\n"
            f"=== ПЕРЕПИСКА ===\n" + "\n".join(q["history"]) + f"\n\n=== ВОПРОС ===\n{q['question']}\n\n"
            f"=== ВАРИАНТЫ ===\n{opts}\n\nНомер варианта или __SKIP__:")


def main() -> None:
    import json
    from pathlib import Path

    from hh_applicant_tool.main import HHApplicantTool

    ap = argparse.ArgumentParser()
    ap.add_argument("--send", action="store_true", help="отправить ответы (без флага — только показать)")
    ap.add_argument("--pages", type=int, default=3)
    args = ap.parse_args()

    tool = HHApplicantTool()
    tool._parser.parse_args(["whoami"], namespace=tool)
    s = tool.session
    s.headers.update({"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"})
    prefs_path = Path("/app/config/candidate_prefs.txt")
    prefs = prefs_path.read_text(encoding="utf-8") if prefs_path.exists() else "(настройки не заданы)"
    resumes = tool.get_resumes()
    main_resume = tool.api_client.get(f"/resumes/{resumes[0]['id']}")
    resume_text = json.dumps({k: main_resume.get(k) for k in ("title", "area", "relocation", "business_trip_readiness",
                                                              "skills", "skill_set", "schedules", "employments")},
                             ensure_ascii=False)[:6000] + "\n" + "\n".join(
        f"{e.get('company')} ({e.get('start')} — {e.get('end') or 'н.в.'}), {e.get('position')}"
        for e in main_resume.get("experience") or [])
    ai = tool.get_cover_letter_ai(SYSTEM)

    sent = skipped = 0
    for page in range(args.pages):
        items = s.get(f"{CHATIK}/chatik/api/chats?page={page}&perPage=50", timeout=30).json()["chats"]["items"]
        if not items:
            break
        for ch in items:
            if ch.get("type") != "NEGOTIATION":
                continue
            data = s.get(f"{CHATIK}/chatik/api/chat_data?chatId={ch['id']}", timeout=30).json()
            q = pending_question(data)
            if not q:
                continue
            k = parse_choice(ai.complete(build_query(q, resume_text, prefs)), len(q["options"]))
            url = f"https://hh.ru/vacancy/{q['vacancy_id']}"
            if k is None:
                skipped += 1
                print(f"⏸  {url} | {q['question'][:90]} | варианты: {q['options']} → решай сам", flush=True)
                continue
            print(f"{'✅' if args.send else '🧪'} {url} | {q['question'][:90]} → «{q['options'][k]}»", flush=True)
            if args.send:
                xsrf = next((c.value for c in s.cookies if c.name == "_xsrf"), "")
                r = s.post(f"{CHATIK}/chatik/api/send", json=send_payload(q["chat_id"], q["message_id"], q["options"][k]),
                           headers={"X-Xsrftoken": xsrf, "Content-Type": "application/json",
                                    "Referer": f"{CHATIK}/chat/{q['chat_id']}"}, timeout=30)
                if r.status_code >= 300:
                    print(f"   ⚠️ отправка не прошла: {r.status_code} {r.text[:200]}", flush=True)
                    continue
                sent += 1
    print(f"Итого: {'отправлено' if args.send else 'выбрано (холостой прогон)'} {sent if args.send else '—'}, "
          f"оставлено тебе {skipped}", flush=True)


if __name__ == "__main__":
    main()
