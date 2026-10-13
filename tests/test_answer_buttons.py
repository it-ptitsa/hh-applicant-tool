"""Ответы на вопросы робота-рекрутера кнопками (12.10.2026).

Бот работодателя («Робот-рекрутер») задаёт вопрос с кнопками actions.text_buttons; свободный текст он не
принимает. Веб-чат hh отправляет нажатие кнопки как сообщение с текстом кнопки и metadata.source =
chat_text_button + messageId вопроса (POST chatik.hh.ru/chatik/api/send). Через API с metadata — 500,
поэтому отправляем точный текст кнопки обычным сообщением: бот его принимает.
"""

from hh_applicant_tool.operations import answer_buttons as cb

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


def test_send_payload_is_exact_button_text_without_metadata():
    """12.10 вживую: с metadata (как в веб-чате) /chatik/api/send отвечает 500; точный текст кнопки
    обычным сообщением — 200, и бот засчитывает ответ (завершил скрининг и вышел из чата)."""
    p = cb.send_payload(chat_id=5, text="да", key="k1")
    assert p == {"chatId": 5, "idempotencyKey": "k1", "text": "да"}


def test_query_lists_numbered_options_and_facts():
    q = cb.pending_question(chat([msg(2, "Удобен офис?", buttons=["да", "нет"])]))
    text = cb.build_query(q, "Опыт: React", "Офис: да")
    assert "1. да\n2. нет" in text and "Опыт: React" in text and "Офис: да" in text


def test_operation_is_registered_in_cli():
    from hh_applicant_tool.main import HHApplicantTool
    t = HHApplicantTool()
    args = t._parser.parse_args(["answer-buttons", "--send", "--pages", "2"])
    assert args.send is True and args.pages == 2


def test_iter_chats_dedupes_and_stops_on_repeated_pages():
    """13.10: веб-чат отдаёт одни и те же чаты на разных страницах — 12 «чатов» оказались 3."""
    pages = [[{"id": 1}, {"id": 2}], [{"id": 2}, {"id": 1}], [{"id": 3}]]
    got = list(cb.iter_unique_chats(lambda p: pages[p] if p < len(pages) else [], max_pages=5))
    assert [c["id"] for c in got] == [1, 2]   # вторая страница — повтор первой → обход остановлен
