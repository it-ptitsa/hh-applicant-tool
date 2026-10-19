"""Сопроводительное по шаблону: модель пишет только абзац про пересечение с вакансией.

Требования Александра (19.10.2026): приветствие → что увидел у них и где
пересекаемся → коротко о себе; только короткие тире «–», без следов AI.
"""

from __future__ import annotations

import pytest

from hh_applicant_tool.ai.letter import (
    AI_MATCH_PLACEHOLDER,
    clean_letter_text,
    compose_letter,
    letter_ai_traces,
)

TEMPLATE = "Здравствуйте!\n\n%(ai_match)s\n\nСемь лет во фронтенде, сейчас Базар."


def test_clean_replaces_long_dashes_and_quotes():
    out = clean_letter_text("Делал «Базар» — соцсеть ― и **SSR**.")
    assert "—" not in out and "―" not in out and "«" not in out and "*" not in out
    assert "Базар – соцсеть" in out


def test_clean_collapses_spaces_and_strips_wrapping_quotes():
    assert clean_letter_text('  "Текст  абзаца."  ') == "Текст абзаца."


@pytest.mark.parametrize(
    "text",
    [
        "Буду рад обсудить детали на собеседовании.",
        "С уважением, Александр.",
        "Уверен, что мой опыт будет полезен вашей команде.",
        "Это позволило ускорить работу, что позволило команде расти.",
        "Знаю React, TypeScript и Next.js, работал с MobX, Redux и Zustand.",  # две тройки
    ],
)
def test_traces_found(text):
    assert letter_ai_traces(text)


def test_clean_paragraph_has_no_traces():
    text = ("Увидел, что у вас Next.js с SSR и упор на скорость публичных страниц. "
            "В Базаре я как раз переводил посты и карточки инструментов на SSR со стримингом.")
    assert letter_ai_traces(text) == []


def test_too_long_is_trace():
    assert letter_ai_traces("Слово. " * 200)


class FakeAI:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0

    def complete(self, message):
        self.calls += 1
        return self.answers.pop(0)


GOOD = "Увидел у вас Next.js и SSR – в Базаре я переводил публичные страницы на SSR со стримингом."


def test_compose_inserts_cleaned_paragraph():
    ai = FakeAI(["Увидел у вас Next.js и SSR — в Базаре я переводил публичные страницы на SSR со стримингом."])
    letter = compose_letter(TEMPLATE, {}, ai, "вакансия")
    assert letter.startswith("Здравствуйте!\n\n") and "—" not in letter
    assert "в Базаре я переводил" in letter and letter.endswith("сейчас Базар.")


def test_compose_retries_on_traces():
    ai = FakeAI(["Буду рад обсудить. Уверен, что подойду.", GOOD])
    letter = compose_letter(TEMPLATE, {}, ai, "вакансия")
    assert ai.calls == 2 and "в Базаре я переводил" in letter


def test_compose_falls_back_without_paragraph():
    ai = FakeAI(["Буду рад обсудить.", "С уважением, Александр."])
    letter = compose_letter(TEMPLATE, {}, ai, "вакансия")
    assert letter == "Здравствуйте!\n\nСемь лет во фронтенде, сейчас Базар."


def test_compose_ai_error_falls_back():
    class Boom:
        def complete(self, m):
            raise RuntimeError("claude timeout")

    assert compose_letter(TEMPLATE, {}, Boom(), "в") == "Здравствуйте!\n\nСемь лет во фронтенде, сейчас Базар."


def test_compose_renders_other_placeholders_and_variants():
    tpl = "{Здравствуйте|Добрый день}!\n\n%(ai_match)s\n\nПро вакансию %(vacancy_name)s."
    letter = compose_letter(tpl, {"vacancy_name": "Frontend"}, FakeAI([GOOD]), "в")
    assert letter.split("!")[0] in ("Здравствуйте", "Добрый день")
    assert letter.endswith("Про вакансию Frontend.")


def test_placeholder_constant():
    assert AI_MATCH_PLACEHOLDER == "%(ai_match)s"


def test_paragraph_with_percent_and_braces_is_inserted_verbatim():
    p = "Видел у вас A/B-тесты {GrowthBook} – в Базаре поднял онбординг с 61% до 74% через эксперименты."
    letter = compose_letter(TEMPLATE, {}, FakeAI([p]), "в")
    assert "с 61% до 74%" in letter and "{GrowthBook}" in letter


def test_percent_in_template_text_is_literal():
    tpl = "Здравствуйте!\n\n%(ai_match)s\n\nПоднял онбординг с 61% до 74%, 100% уверенно."
    letter = compose_letter(tpl, {}, FakeAI([GOOD]), "в")
    assert letter.endswith("Поднял онбординг с 61% до 74%, 100% уверенно.")
    assert compose_letter(tpl, {}, FakeAI(["плохо", "плохо"]), "в").endswith("с 61% до 74%, 100% уверенно.")
