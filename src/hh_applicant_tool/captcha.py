"""Решение каптчи hh.ru от имени аккаунта.

С 23.09.2026 API hh отвечает `captcha_required` примерно после 7 откликов подряд.
Каптча снимает блокировку, только если решена в браузере с куками сессии
аккаунта (опыт 30.09.2026). Здесь: перенос кук в браузер и обратно, промпт и
нормализация ответа, цикл попыток с проверкой, что ответ принят.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import logging
import re
from typing import Any, Awaitable, Callable, Iterable

logger = logging.getLogger(__package__)

CAPTCHA_PATH = "/account/captcha"
SEL_IMAGE = 'img[data-qa="account-captcha-picture"]'
SEL_INPUT = 'input[data-qa="account-captcha-input"]'
SEL_SUBMIT = 'button[data-qa="account-captcha-submit"]'
SEL_RENEW = 'button[data-qa="captcha-renew-text"]'

# Формат каптчи hh: два реальных русских слова (часто редкие словоформы)
# строчными по дуге на шумном фоне. На 30.09.2026 gemini-2.5-flash с этим
# промптом — 20/20 на размеченных каптчах, gpt-4o-mini со старым — 5–6/10.
CAPTCHA_SYSTEM_PROMPT = (
    "Это каптча hh.ru: ровно два русских слова строчными буквами, написанные по "
    "дуге на шумном фоне. Слова — реальные словоформы русского языка, часто "
    "редкие (причастия, падежные формы, редкие существительные). Читай "
    "посимвольно, не подменяй слово более частым похожим."
)
CAPTCHA_USER_PROMPT = (
    "Верни только эти два слова через один пробел, строчными, без кавычек и пояснений."
)

Recognizer = Callable[[bytes], str]


def normalize_captcha_answer(text: str | None) -> str:
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[\"'«»“”„.,:;!?`]", " ", text)
    return " ".join(text.split())


def browser_cookies_from_jar(jar: Iterable) -> list[dict[str, Any]]:
    """Куки HTTP-сессии в формате Playwright `context.add_cookies`."""
    out = []
    for c in jar:
        if not c.name or not c.domain:
            continue
        out.append(
            {
                "name": c.name,
                "value": c.value or "",
                "domain": c.domain,
                "path": c.path or "/",
                "secure": bool(c.secure),
                "expires": float(c.expires) if c.expires else -1,
            }
        )
    return out


def store_browser_cookies(jar, browser_cookies) -> None:
    """Переносит куки из Playwright-контекста в джар сессии.

    `session.cookies` здесь — MozillaCookieJar (точнее HHOnlyCookieJar), а у него
    нет метода `.set()`: это API requests-джара. Из-за прямого вызова `.set()`
    решение капчи падало с «'HHOnlyCookieJar' object has no attribute 'set'»
    уже ПОСЛЕ успешного распознавания текста, и прогон обрывался (24.09.2026 —
    на 7 откликах). Поддерживаем оба вида джара.
    """
    for c in browser_cookies:
        name, value = c.get("name"), c.get("value")
        if not name:
            continue
        domain = c.get("domain") or ""
        path = c.get("path") or "/"

        setter = getattr(jar, "set", None)
        if callable(setter):
            setter(name, value, domain=domain, path=path)
            continue

        jar.set_cookie(
            http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=bool(domain),
                domain_initial_dot=domain.startswith("."),
                path=path,
                path_specified=True,
                secure=bool(c.get("secure")),
                expires=int(c["expires"]) if c.get("expires", -1) and c.get("expires", -1) > 0 else None,
                discard=False,
                comment=None,
                comment_url=None,
                rest={},
            )
        )

async def solve_captcha_on_page(
    page,
    recognize: Recognizer,
    *,
    max_attempts: int = 5,
    success_timeout: int = 15000,
) -> bool:
    """Решает каптчу на открытой странице. True — только если страница ушла
    с /account/captcha (ответ принят). Неверный или пустой ответ — новая
    картинка и новая попытка."""
    for attempt in range(1, max_attempts + 1):
        image = await page.wait_for_selector(SEL_IMAGE, state="visible", timeout=10000)
        img_bytes = await image.screenshot()
        try:
            raw = await asyncio.to_thread(recognize, img_bytes)
        except Exception as ex:  # сеть/лимиты модели — это попытка, не авария
            logger.warning("Каптча, попытка %d: ошибка распознавания: %s", attempt, ex)
            raw = ""
        answer = normalize_captcha_answer(raw)
        if not answer:
            logger.warning("Каптча, попытка %d: пустой ответ, беру новую картинку", attempt)
            await page.click(SEL_RENEW)
            continue

        logger.info("Каптча, попытка %d: ввожу «%s»", attempt, answer)
        await page.fill(SEL_INPUT, answer)
        await page.click(SEL_SUBMIT)
        try:
            await page.wait_for_url(
                lambda url: CAPTCHA_PATH not in url, timeout=success_timeout
            )
        except Exception:
            logger.warning("Каптча, попытка %d: ответ не принят", attempt)
            continue
        logger.info("Каптча принята с попытки %d", attempt)
        return True
    logger.error("Каптча не решена за %d попыток", max_attempts)
    return False


async def solve_captcha_in_browser(
    captcha_url: str,
    jar,
    recognize: Recognizer,
    *,
    headless: bool = True,
    max_attempts: int = 5,
    success_timeout: int = 15000,
    _context_hook: Callable[[Any], Awaitable[None]] | None = None,
) -> bool:
    """Открывает каптчу в Chromium С КУКАМИ сессии аккаунта, решает и
    переносит куки браузера обратно в джар сессии."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            context = await browser.new_context()
            if _context_hook:
                await _context_hook(context)
            await context.add_cookies(browser_cookies_from_jar(jar))
            page = await context.new_page()
            await page.goto(captcha_url, timeout=30000)
            ok = await solve_captcha_on_page(
                page, recognize, max_attempts=max_attempts, success_timeout=success_timeout
            )
            store_browser_cookies(jar, await context.cookies())
            return ok
        finally:
            await browser.close()
