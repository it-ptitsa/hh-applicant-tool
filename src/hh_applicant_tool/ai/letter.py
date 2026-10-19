"""Сопроводительное письмо по шаблону: модель пишет только абзац про вакансию.

Шаблон письма (-L) содержит `%(ai_match)s`. Приветствие и рассказ о себе —
живой текст кандидата, его модель не трогает. Модель пишет один абзац: что
кандидат увидел в вакансии и где пересекается его опыт. Так у нейроследа
почти нет площади.

Ответ модели чистится кодом (только короткие тире «–», без ёлочек и
разметки) и проверяется на улики AI. Улика — перегенерация; после второй
неудачи письмо уходит без абзаца, отклик не теряется.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..utils.string import rand_text, render_template

logger = logging.getLogger(__package__)

AI_MATCH_PLACEHOLDER = "%(ai_match)s"
MAX_ATTEMPTS = 2
MIN_LEN, MAX_LEN = 60, 600

# Штампы, по которым письмо читается как нейросетевое (анти-AI раздел скилла resume)
STOP_PHRASES = (
    "буду рад", "буду рада", "с уважением", "уверен, что", "уверена, что",
    "обеспечил", "что позволило", "тем самым", "в рамках", "эффективн",
    "ключев", "идеально", "впечатлил", "вдохновля", "амбициозн", "динамичн",
    "синерги", "являюсь", "данной вакансии", "вашей компании", "ваша компания",
    "уважаем", "не могу не", "хотел бы отметить", "стремлюсь", "увлечён",
    "увлечен", "мой опыт будет полезен", "внести вклад", "внести свой вклад",
)
_TRIPLE_RE = re.compile(r"[\w.+#-]+, [\w.+#-]+ и [\w.+#-]+", re.IGNORECASE)


def clean_letter_text(text: str) -> str:
    """Только короткие тире, без ёлочек, кавычек-обёрток и markdown."""
    text = (text or "").replace("—", "–").replace("―", "–")
    text = re.sub(r"[«»“”„]", "", text)
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.strip().splitlines())
    return text.strip().strip('"').strip("'").strip()


def letter_ai_traces(text: str) -> list[str]:
    """Улики AI в абзаце. Пустой список — абзац можно отправлять."""
    traces = []
    low = text.lower()
    traces += [f"штамп: {p}" for p in STOP_PHRASES if p in low]
    if len(_TRIPLE_RE.findall(text)) > 1:
        traces.append("тройки перечислений подряд")
    if "—" in text or "«" in text:
        traces.append("длинное тире или ёлочки")
    if not MIN_LEN <= len(text) <= MAX_LEN:
        traces.append(f"длина {len(text)} вне {MIN_LEN}–{MAX_LEN}")
    if text.count("!") > 0:
        traces.append("восклицания")
    return traces


def _render(template: str, placeholders: dict[str, Any], paragraph: str) -> str:
    # Сначала шаблон (варианты {a|b} и плейсхолдеры), потом абзац модели:
    # иначе «61%» или «{…}» в её тексте ломают подстановку
    marker = "\x00AIMARK\x00"  # без «_»: clean_letter_text снимает его как markdown
    if not paragraph:
        template = re.sub(r"\n*\s*%\(ai_match\)s\s*\n*", "\n\n", template)
    # «61%» в тексте шаблона — литерал, а не начало поля %(…)s
    template = re.sub(r"%(?!\()", "%%", template)
    body = render_template(
        rand_text(template), {**placeholders, "ai_match": marker}, "сопроводительном письме"
    )
    return clean_letter_text(body).replace(marker, paragraph)


def compose_letter(
    template: str,
    placeholders: dict[str, Any],
    ai,
    message: str,
) -> str:
    """Собирает письмо: шаблон + абзац модели (с проверкой) или без него."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            paragraph = clean_letter_text(ai.complete(message))
        except Exception as ex:
            logger.warning("Абзац письма не получен (%s), попытка %d", ex, attempt)
            continue
        traces = letter_ai_traces(paragraph)
        if not traces:
            return _render(template, placeholders, paragraph)
        logger.info("Улики AI в абзаце письма (попытка %d): %s", attempt, traces)
    logger.warning("Абзац письма не прошёл проверку — отправляю письмо без него")
    return _render(template, placeholders, "")
