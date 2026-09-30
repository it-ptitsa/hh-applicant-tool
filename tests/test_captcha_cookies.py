"""Регрессия: перенос кук после решения капчи падал на HHOnlyCookieJar.

24.09.2026 решатель капчи распознал текст, но упал с
`'HHOnlyCookieJar' object has no attribute 'set'` — прогон оборвался на 7 откликах.
Причина: `session.cookies` здесь MozillaCookieJar (у него нет `.set()`,
это метод requests-джара), а код звал именно `.set()`.
"""
import sys

import pytest

sys.path.insert(0, "/app/src")

from hh_applicant_tool.operations.apply_vacancies import store_browser_cookies  # noqa: E402
from hh_applicant_tool.utils.cookiejar import HHOnlyCookieJar  # noqa: E402

BROWSER_COOKIES = [
    {"name": "hhtoken", "value": "abc123", "domain": ".hh.ru", "path": "/"},
    {"name": "hhuid", "value": "uid456", "domain": ".hh.ru", "path": "/"},
]


def test_stores_into_mozilla_jar():
    """Основной случай: боевой джар без метода .set()."""
    jar = HHOnlyCookieJar()
    store_browser_cookies(jar, BROWSER_COOKIES)
    got = {c.name: c.value for c in jar}
    assert got["hhtoken"] == "abc123"
    assert got["hhuid"] == "uid456"


def test_foreign_domains_filtered():
    """HHOnlyCookieJar пускает только hh-домены — поведение сохраняется."""
    jar = HHOnlyCookieJar()
    store_browser_cookies(jar, [{"name": "ga", "value": "x", "domain": ".google.com", "path": "/"}])
    assert len(jar) == 0


def test_requests_jar_still_works():
    """Если джар всё-таки requests-овский — используем его .set()."""
    import requests

    jar = requests.cookies.RequestsCookieJar()
    store_browser_cookies(jar, BROWSER_COOKIES)
    assert jar.get("hhtoken") == "abc123"


def test_missing_domain_does_not_crash():
    """Playwright иногда отдаёт куку без domain/path."""
    jar = HHOnlyCookieJar()
    store_browser_cookies(jar, [{"name": "x", "value": "1"}])
    assert isinstance(len(jar), int)
