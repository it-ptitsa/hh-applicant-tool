"""Проверка распознавания каптчи hh без откликов.

С 23.09.2026 hh требует каптчу примерно после 7 откликов подряд. Решатель
справляется, только если в конфиге задана подходящая vision-модель. Эта команда
прогоняет вложенные образцы каптчи с известным ответом через модель из секции
`openai_captcha` и говорит, готова ли рассылка к каптче.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..captcha import normalize_captcha_answer
from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

RECOMMENDED_MODEL = "google/gemini-2.5-flash"
_DATA = Path(__file__).resolve().parent.parent / "data" / "captcha_samples"
SAMPLES: list[tuple[Path, str]] = [
    (_DATA / "sample_1.png", "плюс откозыряла"),
    (_DATA / "sample_2.png", "мареммах лессу"),
]

SETUP_HINT = f"""\
Настройте распознавание каптчи (секция openai_captcha), например через OpenRouter:
  hh-applicant-tool config -s openai_captcha.base_url https://openrouter.ai/api/v1/chat/completions
  hh-applicant-tool config -s openai_captcha.api_key sk-or-v1-КЛЮЧ
  hh-applicant-tool config -s openai_captcha.model {RECOMMENDED_MODEL}
Бесплатные модели (:free, Groq) для каптчи не годятся: лимиты частоты и низкая точность."""


class Namespace(BaseNamespace):
    pass


def evaluate_samples(recognize: Callable[[bytes], str]) -> tuple[int, list[dict]]:
    ok = 0
    details = []
    for path, expected in SAMPLES:
        item = {"sample": path.name, "expected": expected, "got": "", "ok": False, "error": ""}
        try:
            item["got"] = normalize_captcha_answer(recognize(path.read_bytes()))
            item["ok"] = item["got"] == expected
        except Exception as ex:
            item["error"] = str(ex)
        ok += item["ok"]
        details.append(item)
    return ok, details


class Operation(BaseOperation):
    """Проверит, что распознавание каптчи hh настроено (без откликов)"""

    __aliases__: list[str] = ["test-captcha"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, tool: HHApplicantTool, args: BaseNamespace) -> int:
        section = tool.config.get("openai_captcha") or {}
        missing = [k for k in ("base_url", "api_key", "model") if not section.get(k)]
        if missing:
            print("❌ Распознавание каптчи не настроено: нет openai_captcha." + ", ".join(missing))
            print(SETUP_HINT)
            return 1

        ai = tool.get_captcha_ai()
        ok, details = evaluate_samples(ai.solve_captcha)
        print(f"Модель: {ai.model}")
        for d in details:
            mark = "✅" if d["ok"] else "❌"
            got = d["error"] or d["got"] or "(пусто)"
            print(f"  {mark} ожидалось «{d['expected']}», получено: {got}")

        errors = " ".join(d["error"] for d in details)
        if ok == len(details):
            print(f"✅ Каптча настроена: {ok}/{len(details)}. Решатель использует веб-сессию — если test-session ругается, сделайте authorize.")
            return 0
        if "402" in errors:
            print("❌ OpenRouter вернул 402 — на ключе нет денег. Пополните баланс ($5 хватит надолго).")
        elif "429" in errors:
            print("❌ 429 — лимит частоты (частая беда бесплатных моделей).")
        print(f"❌ Распознано {ok}/{len(details)}. Рекомендуемая модель: {RECOMMENDED_MODEL}")
        print(SETUP_HINT)
        return 1
