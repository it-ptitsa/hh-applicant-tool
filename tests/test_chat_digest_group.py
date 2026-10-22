"""Дайджест чатов: одна запись на вакансию, а не на каждое резюме (22.10.2026).

С несколькими резюме у вакансии несколько чатов, и дайджест показывал «Онлайн-школа Фоксфорд — React
Native» семь раз подряд. Теперь одна строка с числом резюме, разными вопросами и ссылками на чаты без ответа.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _digest(monkeypatch):
    monkeypatch.delenv("ACCOUNT_LABEL", raising=False)
    import chat_digest
    return importlib.reload(chat_digest)


def item(chat, text, at, ts, answered=False, code="question", url="https://hh.ru/vacancy/1",
         employer="Онлайн-школа Фоксфорд", vacancy="React Native разработчик"):
    titles = {"question": "❓ Вопрос от работодателя", "contact": "✍️ Зовут в личку (Telegram/телефон/почта)"}
    return {"code": code, "title": titles[code], "employer": employer, "vacancy": vacancy, "url": url,
            "chat_url": f"https://hh.ru/chat/{chat}", "text": text, "at": at, "ts": ts, "answered": answered,
            "state": ""}


FOXFORD = [
    item(1, "Укажи зарплатные ожидания", "22.10 09:35", "2026-10-22T09:35:00+03:00"),
    item(2, "Используем эти ответы?", "22.10 09:09", "2026-10-22T09:09:00+03:00", answered=True),
    item(3, "Укажи зарплатные ожидания", "21.10 17:29", "2026-10-21T17:29:00+03:00"),
]


def test_one_line_per_vacancy_with_count_and_unanswered_chats(monkeypatch):
    d = _digest(monkeypatch)
    sber = item(9, "Пройдите интервью с ГигаРекрутером", "21.10 13:03", "2026-10-21T13:03:00+03:00",
                code="contact", url="https://hh.ru/vacancy/2", employer="СБЕР", vacancy="Frontend")
    msg = d.build_message([sber, *FOXFORD], 24)
    assert "Требуют внимания: <b>2</b> · без твоего ответа: <b>2</b> · чатов: 4" in msg
    assert msg.count("Онлайн-школа Фоксфорд") == 1
    assert "React Native разработчик · 3 резюме 🔴" in msg
    # разные вопросы — по разу, свежий первым
    assert msg.count("Укажи зарплатные ожидания") == 1
    assert msg.index("Укажи зарплатные ожидания") < msg.index("Используем эти ответы?")
    # главная ссылка — на свежий чат без ответа, остальные чаты без ответа — списком
    assert '<a href="https://hh.ru/chat/1">Онлайн-школа Фоксфорд</a>' in msg
    assert 'без ответа 2 из 3: <a href="https://hh.ru/chat/1">1</a> · <a href="https://hh.ru/chat/3">2</a>' in msg
    # одиночная вакансия — как раньше, без счётчика
    assert '• <a href="https://hh.ru/chat/9">СБЕР</a> — Frontend 🔴\n' in msg


def test_group_takes_most_important_category(monkeypatch):
    d = _digest(monkeypatch)
    contact = item(4, "Напишите мне в телеграм", "22.10 10:00", "2026-10-22T10:00:00+03:00", code="contact")
    msg = d.build_message([*FOXFORD, contact], 24)
    assert msg.count("Онлайн-школа Фоксфорд") == 1
    assert msg.index("✍️ Зовут в личку") < msg.index("Онлайн-школа Фоксфорд")
    assert "❓ Вопрос от работодателя" not in msg


def test_all_answered_group_has_no_red_flag(monkeypatch):
    d = _digest(monkeypatch)
    msg = d.build_message([item(1, "Вопрос?", "22.10 09:00", "a", answered=True),
                           item(2, "Вопрос?", "22.10 09:01", "b", answered=True)], 24)
    assert "· 2 резюме\n" in msg and "🔴" not in msg and "без ответа" not in msg


def test_html_in_texts_is_escaped(monkeypatch):
    d = _digest(monkeypatch)
    msg = d.build_message([item(1, "ЗП <от 200к> & выше?", "22.10 09:00", "a", employer="A&B")], 24)
    assert "ЗП &lt;от 200к&gt; &amp; выше?" in msg and ">A&amp;B</a>" in msg


def test_forbidden_chat_is_skipped_not_fatal(monkeypatch):
    """403 на переписке одного отклика (чат закрыт/удалён) раньше валил весь дайджест — сводка не приходила."""
    d = _digest(monkeypatch)
    neg = lambda nid: {"id": nid, "updated_at": "2099-01-01T00:00:00+03:00", "state": {"id": "response"},
                       "chat_id": int(nid), "vacancy": {"name": "V", "alternate_url": f"https://hh.ru/vacancy/{nid}",
                                                        "employer": {"name": "E"}}}

    class Api:
        def get(self, path, **params):
            if path == "/negotiations":
                return {"items": [neg("1"), neg("2")], "pages": 1}
            if path == "/negotiations/1/messages":
                raise requests.HTTPError("403", response=SimpleNamespace(status_code=403))
            return {"items": [{"text": "Когда удобно созвониться?", "author": {"participant_type": "employer"},
                               "created_at": "2099-01-01T00:00:00+03:00"}]}

    items = d.collect(Api(), 24)
    assert [i["chat_url"] for i in items] == ["https://hh.ru/chat/2"]
    assert items[0]["ts"] == "2099-01-01T00:00:00+03:00"
