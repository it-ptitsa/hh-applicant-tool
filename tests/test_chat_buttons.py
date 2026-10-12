"""Ответы на вопросы робота-рекрутера кнопками (12.10.2026).

Бот работодателя («Робот-рекрутер») задаёт вопрос с кнопками actions.text_buttons; свободный текст он не
принимает. Веб-чат hh отправляет нажатие кнопки как сообщение с текстом кнопки и metadata.source =
chat_text_button + messageId вопроса (POST chatik.hh.ru/chatik/api/send).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import chat_buttons as cb  # noqa: E402

ME = "100"


def msg(mid, text, pid="200", buttons=None, bot=True):
    m = {"id": mid, "text": text, "participantId": pid, "participantDisplay": {"isBot": bot, "name": "Робот-рекрутер"}}
    if buttons is not None:
        m["actions"] = {"text_buttons": [{"size": "full", "text": b} for b in buttons]}
    return m


def chat(messages, vacancy="777"):
    return {"chat": {"id": 5, "type": "NEGOTIATION", "currentParticipantId": ME, "resources": {"VACANCY": [vacancy]},
                     "messages": {"items": messages}}}


def test_pending_question_is_last_message_with_buttons_from_other_side():
    q = cb.pending_question(chat([msg(1, "Привет"), msg(2, "Удобен офис на Остоженке?", buttons=["да", "нет"])]))
    assert q == {"chat_id": 5, "message_id": 2, "vacancy_id": "777", "question": "Удобен офис на Остоженке?",
                 "options": ["да", "нет"], "history": ["Работодатель: Привет", "Работодатель: Удобен офис на Остоженке?"]}


def test_answered_or_no_buttons_is_not_pending():
    assert cb.pending_question(chat([msg(2, "Вопрос", buttons=["да"]), msg(3, "да", pid=ME)])) is None
    assert cb.pending_question(chat([msg(2, "Просто текст")])) is None


def test_parse_choice():
    assert cb.parse_choice("2", 3) == 1
    assert cb.parse_choice("Ответ: 1", 2) == 0
    assert cb.parse_choice("__SKIP__", 2) is None
    assert cb.parse_choice("5", 2) is None
    assert cb.parse_choice("не знаю", 2) is None


def test_send_payload_marks_button_source():
    p = cb.send_payload(chat_id=5, message_id=2, text="да", key="k1")
    assert p["chatId"] == 5 and p["text"] == "да" and p["idempotencyKey"] == "k1"
    assert p["metadata"]["source"] == {"source_type": "chat_text_button"} and p["metadata"]["messageId"] == 2
    assert p["metadata"]["chatType"] == "NEGOTIATION"
