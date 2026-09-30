"""Решатель каптчи hh: чистые функции и цикл попыток на поддельной странице.

Сквозная проверка в настоящем браузере — tests/test_captcha_browser.py.
"""

from __future__ import annotations

import asyncio
from http.cookiejar import Cookie

from hh_applicant_tool.captcha import (
    CAPTCHA_PATH,
    browser_cookies_from_jar,
    normalize_captcha_answer,
    solve_captcha_on_page,
)
from hh_applicant_tool.utils.cookiejar import HHOnlyCookieJar


def _cookie(name, value, domain=".hh.ru", expires=None):
    return Cookie(
        0, name, value, None, False, domain, True, domain.startswith("."),
        "/", True, True, expires, False, None, None, {},
    )


# --- чистые функции -------------------------------------------------------


def test_normalize_strips_quotes_case_and_yo():
    assert normalize_captcha_answer("«Плюс Откозыряла.»") == "плюс откозыряла"
    assert normalize_captcha_answer('  "бьёте   мусью"\n') == "бьете мусью"


def test_normalize_empty():
    assert normalize_captcha_answer("") == ""
    assert normalize_captcha_answer(None) == ""


def test_browser_cookies_from_mozilla_jar(tmp_path):
    jar = HHOnlyCookieJar(str(tmp_path / "c.txt"))
    jar.set_cookie(_cookie("hhtoken", "abc", expires=2_000_000_000))
    jar.set_cookie(_cookie("_xsrf", "x"))
    out = {c["name"]: c for c in browser_cookies_from_jar(jar)}
    assert out["hhtoken"]["value"] == "abc"
    assert out["hhtoken"]["domain"] == ".hh.ru"
    assert out["hhtoken"]["path"] == "/"
    assert out["hhtoken"]["expires"] == 2_000_000_000
    # сессионная кука: Playwright ждёт -1, а не None
    assert out["_xsrf"]["expires"] == -1


# --- цикл попыток ----------------------------------------------------------


class FakeElement:
    def __init__(self, page):
        self.page = page

    async def screenshot(self):
        return f"img-{self.page.image_no}".encode()

    async def get_attribute(self, name):
        return f"/captcha/picture?key={self.page.image_no}"


class FakePage:
    """Страница каптчи: принимает ответ, если он равен answers[image_no]."""

    def __init__(self, answers: dict[int, str]):
        self.answers = answers
        self.image_no = 0
        self.url = f"https://hh.ru{CAPTCHA_PATH}?state=s"
        self.typed = None
        self.renews = 0

    async def wait_for_selector(self, selector, **kw):
        return FakeElement(self)

    async def fill(self, selector, text):
        self.typed = text

    async def click(self, selector):
        if "renew" in selector:
            self.image_no += 1
            self.renews += 1
            return
        # отправка формы
        if self.answers.get(self.image_no) == self.typed:
            self.url = "https://hh.ru/"
        else:
            self.image_no += 1  # hh показывает новую картинку после ошибки

    async def wait_for_url(self, predicate, timeout=None):
        if not predicate(self.url):
            raise TimeoutError("still on captcha")


def _run(page, recognize, attempts=3):
    return asyncio.run(
        solve_captcha_on_page(page, recognize, max_attempts=attempts, success_timeout=10)
    )


def test_success_first_try():
    page = FakePage({0: "плюс откозыряла"})
    assert _run(page, lambda img: "Плюс откозыряла") is True
    assert page.typed == "плюс откозыряла"


def test_wrong_then_right():
    page = FakePage({1: "тесемкой сгладь"})
    answers = iter(["тесемкой взглядь", "тесемкой сгладь"])
    assert _run(page, lambda img: next(answers)) is True
    assert page.image_no == 1


def test_all_attempts_fail():
    page = FakePage({})
    calls = []
    assert _run(page, lambda img: calls.append(img) or "мимо", attempts=3) is False
    assert len(calls) == 3


def test_empty_answer_requests_new_image():
    page = FakePage({1: "очутятся удал"})
    answers = iter(["", "очутятся удал"])
    assert _run(page, lambda img: next(answers)) is True
    assert page.renews == 1


def test_recognizer_exception_counts_as_attempt():
    page = FakePage({1: "посунула ловили"})
    state = {"n": 0}

    def recognize(img):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("OpenRouter 502")
        return "посунула ловили"

    assert _run(page, recognize) is True
