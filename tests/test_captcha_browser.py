"""Сквозной тест решателя каптчи в настоящем Chromium.

Страницы hh подменяются через context.route: каптча принимается, только если
ответ верный И запрос несёт куку сессии аккаунта. Так проверяется главный
дефект старого решателя — пустой браузер без кук аккаунта (30.09.2026).
"""

from __future__ import annotations

import asyncio
import base64
from http.cookiejar import Cookie
from urllib.parse import parse_qs

import pytest

playwright = pytest.importorskip("playwright.async_api")

from hh_applicant_tool.captcha import solve_captcha_in_browser  # noqa: E402
from hh_applicant_tool.utils.cookiejar import HHOnlyCookieJar  # noqa: E402

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
CAPTCHA_URL = "https://hh.ru/account/captcha?state=test"


def _page(key: int, error: bool = False) -> str:
    err = '<div data-qa="captcha-error">Неверный текст</div>' if error else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
<form method="POST" action="/account/captcha?state=test">
  <img data-qa="account-captcha-picture" src="/captcha/picture?key={key}" width="200" height="60">
  <button type="button" data-qa="captcha-renew-text"
    onclick="var i=document.querySelector('img');i.src='/captcha/picture?key='+(Date.now())">↻</button>
  <input data-qa="account-captcha-input" name="captchaText">
  {err}
  <button type="submit" data-qa="account-captcha-submit">Отправить</button>
</form></body></html>"""


def _jar(tmp_path, with_session: bool) -> HHOnlyCookieJar:
    jar = HHOnlyCookieJar(str(tmp_path / "cookies.txt"))
    if with_session:
        jar.set_cookie(
            Cookie(0, "hhtoken", "acc4-session", None, False, ".hh.ru", True,
                   True, "/", True, True, None, False, None, None, {})
        )
    return jar


async def _fake_hh(route, request, log):
    url = request.url
    if url.startswith("https://hh.ru/captcha/picture"):
        return await route.fulfill(status=200, content_type="image/png", body=PNG)
    if url.startswith("https://hh.ru/account/captcha"):
        if request.method == "POST":
            text = parse_qs(request.post_data or "").get("captchaText", [""])[0]
            cookie = (await request.all_headers()).get("cookie", "")
            log.append((text, "hhtoken=acc4-session" in cookie))
            if text == "верно" and "hhtoken=acc4-session" in cookie:
                return await route.fulfill(
                    status=302, headers={"Location": "https://hh.ru/applicant"},
                    body="",
                )
            return await route.fulfill(status=200, content_type="text/html", body=_page(len(log), error=True))
        return await route.fulfill(status=200, content_type="text/html", body=_page(0))
    return await route.fulfill(status=200, content_type="text/html", body="<html>hh</html>")


def _solve(jar, recognize):
    log: list = []

    async def setup(context):
        await context.route("https://hh.ru/**", lambda r, req: _fake_hh(r, req, log))

    ok = asyncio.run(
        solve_captcha_in_browser(
            CAPTCHA_URL, jar, recognize, headless=True, max_attempts=3,
            success_timeout=5000, _context_hook=setup,
        )
    )
    return ok, log


def _chromium_available() -> bool:
    async def probe():
        async with playwright.async_playwright() as pw:
            b = await pw.chromium.launch(headless=True)
            await b.close()
    try:
        asyncio.run(probe())
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="нет Chromium для Playwright")


def test_accepted_with_account_cookies(tmp_path):
    ok, log = _solve(_jar(tmp_path, with_session=True), lambda img: "Верно.")
    assert ok is True
    assert log == [("верно", True)]


def test_rejected_without_account_cookies(tmp_path):
    ok, log = _solve(_jar(tmp_path, with_session=False), lambda img: "верно")
    assert ok is False
    assert len(log) == 3 and all(has_cookie is False for _, has_cookie in log)


def test_wrong_answer_then_right(tmp_path):
    answers = iter(["неверно", "верно"])
    ok, log = _solve(_jar(tmp_path, with_session=True), lambda img: next(answers))
    assert ok is True
    assert [t for t, _ in log] == ["неверно", "верно"]


def test_cookies_from_browser_stored_back(tmp_path):
    jar = _jar(tmp_path, with_session=True)
    ok, _ = _solve(jar, lambda img: "верно")
    assert ok is True
    assert any(c.name == "hhtoken" for c in jar)
