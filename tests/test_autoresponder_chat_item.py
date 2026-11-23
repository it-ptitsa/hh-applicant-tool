"""Разбор элемента чата в автоответчике.

При переписке autoresponder в main потерялся хелпер get_resource, и
parse_chat_item остался с обращением к несуществующим переменным
vacancy/resume — NameError на первом же чате. Тесты ловят именно разбор
ответа hh, без обращения к сети.

Формат resources у hh меняется: в одном ответе объекты лежат картой
прямо в элементе чата, в другом — списком id в item["resources"]
["VACANCY"], а объекты в resources верхнего уровня. Проверяем оба.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import MagicMock

import requests

from hh_applicant_tool.operations.autoresponder import Operation


def _message(text: str = "Здравствуйте!") -> dict:
    return {
        "id": 9,
        "createdAt": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(),
        "text": text,
        "participantDisplay": {"name": "Иван Иванов", "isBot": False},
        "actions": {"text_buttons": [{"text": "Да, интересно"}]},
    }


def _vacancy(vacancy_id: int = 555) -> dict:
    return {
        "vacancyId": vacancy_id,
        "name": "Python разработчик",
        "company": {"id": 1, "name": "ООО Рога"},
        "links": {"desktop": "https://hh.ru/vacancy/555"},
        "compensation": {"from": 200000, "to": None, "currencyCode": "RUR"},
    }


def _resume(resume_id: int = 777) -> dict:
    return {
        "id": resume_id,
        "hash": "abc123",
        "title": "Python разработчик",
        "userId": 4242,
        "firstName": "Пётр",
        "lastName": "Петров",
    }


def _parse(operation: Operation, item: dict, **kwargs) -> object:
    params = {
        "resume_id": 777,
        "resume_hash": "abc123",
        "resume_title": "Python разработчик",
        "resume_experience": "5 лет",
        "salary": "200000",
        "skills": "Python",
    }
    params.update(kwargs)
    return operation.parse_chat_item(item, **params)


class TestResources:
    def test_resources_as_map_inside_item(self):
        """Объекты лежат картой прямо в элементе чата."""
        op = Operation()
        item = {
            "id": 1,
            "messages": {"items": [_message()]},
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }

        chat = _parse(op, item)

        assert chat is not None
        assert chat.vacancy_name == "Python разработчик"
        assert chat.company_name == "ООО Рога"
        assert chat.first_name == "Пётр"
        assert chat.applicant_id == 4242

    def test_ids_in_item_and_objects_in_common_resources(self):
        """Второй формат: id в элементе, объекты на верхнем уровне."""
        op = Operation()
        item = {
            "id": 1,
            "lastMessage": _message(),
            "resources": {"VACANCY": ["555"], "RESUME": ["777"]},
        }
        resources = {
            "vacancies": {"555": _vacancy()},
            "resumes": {"777": _resume()},
        }

        chat = _parse(op, item, resources=resources)

        assert chat is not None
        assert chat.vacancy_url == "https://hh.ru/vacancy/555"
        assert chat.vacancy_compensation

    def test_common_resources_used_without_item_ids(self):
        """Даже без id в элементе берём единственный объект."""
        op = Operation()
        item = {"id": 1, "messages": {"items": [_message()]}}
        resources = {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}}

        chat = _parse(op, item, resources=resources)

        assert chat is not None
        assert chat.vacancy_name == "Python разработчик"

    def test_resources_as_list(self):
        """Список объектов вместо карты."""
        op = Operation()
        item = {
            "id": 1,
            "messages": {"items": [_message()]},
            "resources": {
                "vacancies": [_vacancy()],
                "resumes": [_resume()],
            },
        }

        chat = _parse(op, item)

        assert chat is not None
        assert chat.last_name == "Петров"

    def test_missing_resources_skips_chat(self):
        """Нет вакансии — чат пропускаем, а не падаем."""
        op = Operation()
        item = {"id": 1, "messages": {"items": [_message()]}}

        assert _parse(op, item) is None

    def test_prefers_the_iterated_resume(self):
        """Резюме выбирается по resume_id, а не первое попавшееся."""
        op = Operation()
        other = _resume(888)
        other["firstName"] = "Чужое"
        item = {
            "id": 1,
            "messages": {"items": [_message()]},
            "resources": {
                "vacancies": {"555": _vacancy()},
                "resumes": {"888": other, "777": _resume()},
            },
        }

        chat = _parse(op, item, resume_id=777)

        assert chat is not None
        assert chat.first_name == "Пётр"


class TestLastMessage:
    def test_upstream_creation_time_field_is_accepted(self):
        op = Operation()
        message = _message()
        message["creationTime"] = message.pop("createdAt")
        item = {
            "id": 1,
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        assert _parse(op, item) is not None

    def test_recent_human_message_waits_for_manual_reply(self):
        op = Operation()
        message = _message()
        message["createdAt"] = datetime.now(timezone.utc).isoformat()
        item = {
            "id": 1,
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        assert _parse(op, item) is None

    def test_recent_bot_message_can_be_answered_immediately(self):
        op = Operation()
        message = _message()
        message["createdAt"] = datetime.now(timezone.utc).isoformat()
        message["participantDisplay"]["isBot"] = True
        item = {
            "id": 1,
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        assert _parse(op, item) is not None

    def test_recent_discard_remains_visible_for_cleanup(self):
        op = Operation()
        message = _message()
        message["createdAt"] = datetime.now(timezone.utc).isoformat()
        message["workflowTransition"] = {"applicantState": "DISCARD"}
        item = {
            "id": 1,
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        chat = _parse(op, item)
        assert chat is not None and chat.is_discard

    def test_own_last_message_does_not_trigger_another_reply(self):
        op = Operation()
        message = _message()
        message["participantId"] = "my-id"
        item = {
            "id": 1,
            "currentParticipantId": "my-id",
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        assert _parse(op, item) is None

    def test_last_message_field_is_used(self):
        """Список чатов кладёт последнее сообщение в lastMessage."""
        op = Operation()
        item = {
            "id": 1,
            "lastMessage": _message("Сколько у вас зп?"),
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }

        chat = _parse(op, item)

        assert chat is not None
        assert chat.reply_to_message == "Сколько у вас зп?"

    def test_chat_without_messages_skipped(self):
        """Нет ни одного сообщения — отвечать не на что."""
        op = Operation()
        item = {"id": 1, "resources": {"vacancies": {}, "resumes": {}}}

        assert _parse(op, item) is None

    def test_old_chat_skipped(self):
        """Чаты старше 72 часов не трогаем."""
        op = Operation()
        old = _message()
        old["createdAt"] = (
            datetime.now(timezone.utc) - timedelta(hours=100)
        ).isoformat()
        item = {
            "id": 1,
            "lastMessage": old,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }

        assert _parse(op, item) is None


class TestGetChatsAwaitingReply:
    def test_keeps_chats_for_each_published_resume(self):
        op = Operation()
        published = [
            {"id": 777, "status": {"id": "published"}, "title": "Frontend"},
            {"id": 888, "status": {"id": "published"}, "title": "Frontend"},
        ]
        data = {
            "chats": {
                "items": [
                    {"id": 1, "lastMessage": _message(), "resources": {"VACANCY": ["555"], "RESUME": ["777"]}},
                    {"id": 2, "lastMessage": _message(), "resources": {"VACANCY": ["555"], "RESUME": ["888"]}},
                ],
                "pages": 1,
            },
            "resources": {
                "vacancies": {"555": _vacancy()},
                "resumes": {"777": _resume(), "888": _resume(888)},
            },
        }
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            op.chat_url = f"http://127.0.0.1:{server.server_port}"
            op.tool = SimpleNamespace(
                session=requests.Session(),
                xsrf_token="test-token",
                get_resumes=lambda: published,
            )
            chats = op.get_chats_awaiting_reply(max_pages=1)
            assert [(chat.chat_id, chat.resume_id) for chat in chats] == [
                (1, 777),
                (2, 888),
            ]
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_chat_api_human_cooldown_and_bot_exception(self):
        human = _message("Когда удобно?")
        human.pop("createdAt")
        human["creationTime"] = datetime.now(timezone.utc).isoformat()
        bot = _message("Выберите вариант")
        bot.pop("createdAt")
        bot["creationTime"] = datetime.now(timezone.utc).isoformat()
        bot["participantDisplay"]["isBot"] = True
        resources = {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}}
        payload = {
            "chats": {
                "items": [
                    {"id": 1, "lastMessage": human, "resources": resources},
                    {"id": 2, "lastMessage": bot, "resources": resources},
                ],
                "pages": 1,
            }
        }
        requests_seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests_seen.append(self.path)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            op = Operation()
            op.chat_url = f"http://127.0.0.1:{server.server_port}"
            op.tool = SimpleNamespace(
                session=requests.Session(),
                xsrf_token="test-token",
                get_resumes=lambda: [
                    {"id": 777, "status": {"id": "published"}, "title": "Python"}
                ],
            )
            chats = op.get_chats_awaiting_reply(max_pages=1)
            assert [chat.chat_id for chat in chats] == [2]
            assert requests_seen[0].startswith("/chatik/api/chats?")
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_chat_api_bump_uses_real_response_shape(self):
        own = _message("Жду ответа")
        own["creationTime"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        own.pop("createdAt")
        own["participantId"] = "my-id"
        payload = {
            "chats": {
                "items": [{
                    "id": 9,
                    "currentParticipantId": "my-id",
                    "lastMessage": own,
                    "resources": {"VACANCY": ["555"], "RESUME": ["777"]},
                }],
                "pages": 1,
            },
            "resources": {
                "vacancies": {"555": _vacancy()},
                "resumes": {"777": _resume()},
            },
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            op = Operation()
            op.bump_enabled = True
            op.chat_url = f"http://127.0.0.1:{server.server_port}"
            op.tool = SimpleNamespace(
                session=requests.Session(),
                xsrf_token="test-token",
                get_resumes=lambda: [
                    {"id": 777, "status": {"id": "published"}, "title": "Python"}
                ],
            )
            chats = op.get_chats_awaiting_reply(max_pages=1)
            assert len(chats) == 1
            assert chats[0].chat_id == 9 and chats[0].is_bump
        finally:
            server.shutdown()
            thread.join(timeout=3)
            server.server_close()

    def test_skipped_chats_do_not_break_the_loop(self):
        """parse_chat_item возвращает None — цикл это переживает.

        Раньше тут был chat.is_discard без проверки на None, то есть
        первый же отброшенный чат ронял весь проход.
        """
        op = Operation()
        tool = MagicMock()
        tool.get_resumes.return_value = [
            {
                "id": "777",
                "hash": "abc123",
                "title": "Python разработчик",
                "status": {"id": "published"},
                "experience": None,
                "salary": None,
                "skills": None,
            }
        ]
        op.tool = tool

        op.get_chats = MagicMock(  # type: ignore[method-assign]
            return_value={
                "chats": {
                    "pages": 1,
                    "items": [
                        {"id": 1},
                        {
                            "id": 2,
                            "lastMessage": _message(),
                            "resources": {
                                "vacancies": {"555": _vacancy()},
                                "resumes": {"777": _resume()},
                            },
                        },
                    ],
                }
            }
        )

        chats = op.get_chats_awaiting_reply(1)

        assert [c.chat_id for c in chats] == [2]
def test_chat_history_formats_upstream_creation_time():
    operation = Operation.__new__(Operation)
    history = operation.format_messages([
        {
            "participantDisplay": {"name": "Работодатель"},
            "text": "Здравствуйте",
            "creationTime": "2026-11-23T12:00:00+00:00",
        }
    ])
    assert "2026-11-23" in history
    assert "Работодатель: Здравствуйте" in history


def test_old_own_message_can_be_bumped_only_when_enabled():
    message = _message("Жду вашего ответа")
    message["participantId"] = "my-id"
    message["createdAt"] = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    item = {
        "id": 1,
        "currentParticipantId": "my-id",
        "lastMessage": message,
        "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
    }
    operation = Operation()
    assert _parse(operation, item) is None
    operation.bump_enabled = True
    chat = _parse(operation, item)
    assert chat is not None and chat.is_bump
    assert "напоминание" in operation.build_user_prompt(chat, []).lower()


def test_bump_skips_recent_and_very_old_chats():
    operation = Operation()
    operation.bump_enabled = True
    for days in (1, 31):
        message = _message("Жду вашего ответа")
        message["participantId"] = "my-id"
        message["createdAt"] = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        item = {
            "id": 1,
            "currentParticipantId": "my-id",
            "lastMessage": message,
            "resources": {"vacancies": {"555": _vacancy()}, "resumes": {"777": _resume()}},
        }
        assert _parse(operation, item) is None
