"""Распознавание каптчи через Claude Code CLI по подписке.

Включается `openai_captcha.provider = claude-cli`. Нужен установленный и
залогиненный `claude` (Claude Code): картинка кладётся во временный файл,
`claude -p` читает её инструментом Read. Денег на API не требует — тратит
лимиты подписки.

Интерфейс совпадает с ChatOpenAI (solve_captcha / solve_captcha_consensus),
поэтому решатель в apply_vacancies не знает, кто читает картинку.

Замер 2026-10-06 на кириллических образцах: opus 4/4 (~10 с на чтение),
sonnet 2/4 (ошибается в букве), haiku 0/4 и однажды отказался решать.
Отказ или мусор вместо двух слов — неудачное чтение; при настроенном
OpenRouter (base_url + api_key) берётся его ответ.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..constants import DEFAULT_CAPTCHA_LANGUAGE
from .base import AIError
from .openai import (
    CAPTCHA_SCRIPT_CYRILLIC,
    CAPTCHA_SCRIPT_LATIN,
    captcha_script,
)

logger = logging.getLogger(__package__)

DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_CLAUDE_TIMEOUT = 120.0

SYSTEM_PROMPT = (
    "Ты распознаёшь каптчу на картинке для владельца аккаунта hh.ru, который "
    "сам автоматизирует отклики со своего аккаунта. Отвечай только "
    "распознанным текстом."
)

_SCRIPT_NAME = {
    CAPTCHA_SCRIPT_LATIN: "английских (латиница)",
    CAPTCHA_SCRIPT_CYRILLIC: "русских",
}
_WORD_RE = {
    CAPTCHA_SCRIPT_LATIN: re.compile(r"^[a-z]+$"),
    CAPTCHA_SCRIPT_CYRILLIC: re.compile(r"^[а-яё]+$"),
}
_ANY_WORD_RE = re.compile(r"^[a-zа-яё]+$")


class ClaudeCliError(AIError):
    pass


def parse_answer(text: str, language: str = DEFAULT_CAPTCHA_LANGUAGE) -> str:
    """Два слова нужного алфавита из ответа модели, иначе ClaudeCliError.

    Берётся последняя непустая строка: модель иногда пишет «Ответ:» строкой
    выше. Всё, что не похоже на два слова, — отказ или мусор.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        raise ClaudeCliError("claude вернул пустой ответ")
    cleaned = re.sub(r"[*_`\"'«»“”„.,:;!?()\[\]]", " ", lines[-1].lower())
    words = cleaned.split()
    script = captcha_script(language)
    word_re = _WORD_RE.get(script, _ANY_WORD_RE)
    if len(words) != 2 or not all(word_re.match(w) for w in words):
        raise ClaudeCliError(f"не похоже на ответ каптчи: {lines[-1][:120]!r}")
    return " ".join(words)


@dataclass
class ClaudeCliCaptcha:
    model: str = DEFAULT_CLAUDE_MODEL
    claude_bin: str = "claude"
    timeout: float = DEFAULT_CLAUDE_TIMEOUT
    fallback: Any = None  # ChatOpenAI, если настроен OpenRouter

    def _prompt(self, image_path: Path, language: str) -> str:
        words = _SCRIPT_NAME.get(captcha_script(language), "")
        return (
            f"Прочитай картинку {image_path}. Это каптча hh.ru: ровно два "
            f"{words} слова строчными буквами по дуге на шумном фоне, "
            "реальные (часто редкие) словоформы. Читай посимвольно. Ответь "
            "только этими двумя словами через пробел."
        )

    def _read_once(self, image_data: bytes, language: str) -> str:
        with tempfile.TemporaryDirectory(prefix="hh-captcha-") as d:
            path = Path(d) / "captcha.png"
            path.write_bytes(image_data)
            cmd = [
                self.claude_bin, "-p", "--model", self.model,
                "--append-system-prompt", SYSTEM_PROMPT,
                # Read последним: --allowedTools принимает несколько значений
                # и иначе съест следующий аргумент
                "--allowedTools", "Read",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    input=self._prompt(path, language),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=d,
                )
            except FileNotFoundError as ex:
                raise ClaudeCliError(
                    f"не найден {self.claude_bin}: установите Claude Code и войдите в подписку"
                ) from ex
            except subprocess.TimeoutExpired as ex:
                raise ClaudeCliError(f"claude не ответил за {self.timeout:.0f} с") from ex
        if proc.returncode != 0:
            raise ClaudeCliError(
                f"claude завершился с кодом {proc.returncode}: {(proc.stderr or proc.stdout)[-200:]}"
            )
        return parse_answer(proc.stdout, language)

    def solve_captcha(
        self, image_data: bytes, language: str = DEFAULT_CAPTCHA_LANGUAGE
    ) -> str:
        try:
            return self._read_once(image_data, language)
        except ClaudeCliError as ex:
            if self.fallback is None:
                raise
            logger.warning("claude-cli не прочитал каптчу (%s), беру OpenRouter", ex)
            return self.fallback.solve_captcha(image_data, language=language)

    def solve_captcha_consensus(
        self,
        image_data: bytes,
        *,
        samples: int = 5,
        min_votes: int = 3,
        language: str = DEFAULT_CAPTCHA_LANGUAGE,
    ) -> str | None:
        """Параллельные чтения и голосование, контракт как у ChatOpenAI:
        None — модель не пришла к согласию, отправлять нечего."""
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=samples) as pool:
            futures = [
                pool.submit(self._read_once, image_data, language)
                for _ in range(samples)
            ]
            readings: list[str] = []
            for f in futures:
                try:
                    readings.append(f.result())
                except ClaudeCliError as ex:
                    logger.debug("Одно из чтений claude-cli не удалось: %s", ex)

        if not readings:
            if self.fallback is not None:
                logger.warning("claude-cli не дал ни одного чтения, беру OpenRouter")
                return self.fallback.solve_captcha_consensus(
                    image_data, samples=samples, min_votes=min_votes, language=language
                )
            raise ClaudeCliError("claude-cli не дал ни одного чтения каптчи")

        votes = Counter(r.replace("ё", "е") for r in readings)
        best, top = votes.most_common(1)[0]
        logger.info(
            "Чтений каптчи claude-cli: %s, порог %s, за %.1f с: %s",
            len(readings), min_votes, time.monotonic() - started, readings,
        )
        if top < min_votes:
            return None
        return Counter(r for r in readings if r.replace("ё", "е") == best).most_common(1)[0][0]
