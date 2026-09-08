"""Парсер тестов вакансии: разметка hh больше не должна ломать отклик.

08.09.2026 в apply.log 45 вакансий из 123 (37%) падали с «tests not found»,
хотя веб-сессия была жива: hh отдаёт конфиг страницы HTML-экранированным
(`{&#34;...`), а прежний парсер искал сырой маркер `,"vacancyTests":`.
Апстрим (e742566) решил это через get_redirect_config + find_key.
"""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, "/app/src")

from hh_applicant_tool.operations.apply_vacancies import Operation  # noqa: E402

TESTS_PAYLOAD = {"136626706": {"uidPk": "337564160", "guid": "abc-def", "taskList": []}}


def _op(config=None, error=None):
    op = Operation.__new__(Operation)

    def get_redirect_config(_url, check_auth=True):
        if error is not None:
            raise error
        return config

    op.tool = SimpleNamespace(get_redirect_config=get_redirect_config)
    return op


def test_tests_found_at_top_level():
    data = _op({"vacancyTests": TESTS_PAYLOAD})._get_vacancy_tests("u")
    assert data["136626706"]["uidPk"] == "337564160"


def test_tests_found_deeply_nested():
    """find_key ищет ключ рекурсивно — глубина вложенности hh может меняться."""
    config = {"redirectConfig": {"a": [{"b": {"vacancyTests": TESTS_PAYLOAD}}]}}
    data = _op(config)._get_vacancy_tests("u")
    assert data["136626706"]["guid"] == "abc-def"


def test_expired_session_gives_clear_error():
    """Протухшие куки — понятное сообщение, а не «tests not found»."""
    err = RuntimeError("Авторизация истекла требуется новая!")
    with pytest.raises(ValueError, match="Веб-сессия истекла"):
        _op(error=err)._get_vacancy_tests("u")


def test_genuinely_absent_tests():
    """Тестов реально нет — прежняя ошибка сохраняется (вызывающий код по ней
    делает fallback на обычный отклик)."""
    with pytest.raises(ValueError, match="tests not found"):
        _op({"redirectConfig": {"foo": "bar"}})._get_vacancy_tests("u")


def test_other_errors_are_not_swallowed():
    """Сетевые и прочие ошибки пробрасываются как есть."""
    with pytest.raises(RuntimeError, match="503"):
        _op(error=RuntimeError("Неожиданный код ответа: 503"))._get_vacancy_tests("u")


def test_fallback_wired_in_apply_loop():
    """В цикле отклика есть fallback: при «tests not found» вакансия не теряется."""
    import inspect

    src = inspect.getsource(Operation._apply_resume)
    assert "test_handled = False" in src
    assert "if not test_handled:" in src
    assert "как на обычную вакансию" in src
