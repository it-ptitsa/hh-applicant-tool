"""Проверка распознавания каптчи hh без откликов.

С сентября 2026 hh требует каптчу примерно после 7 откликов подряд. Решатель
справляется, только если настроено распознавание: подписка Claude Code
(`provider = claude-cli`) или vision-модель через OpenRouter. Команда
прогоняет вложенные образцы каптчи с известным ответом через выбранный способ
и говорит, готова ли рассылка к каптче.

Образцы кириллические. Боевую каптчу кликер просит латиницей (её модели
читают лучше), так что прошедший проверку способ справится и с ней.
"""

from __future__ import annotations

import argparse
import logging
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..main import BaseNamespace, BaseOperation

if TYPE_CHECKING:
    from ..main import HHApplicantTool

logger = logging.getLogger(__package__)

RECOMMENDED_MODEL = "google/gemini-2.5-flash"
SAMPLE_LANGUAGE = "ru"
_DATA = Path(__file__).resolve().parent.parent / "data" / "captcha_samples"
SAMPLES: list[tuple[Path, str]] = [
    (_DATA / "sample_1.png", "плюс откозыряла"),
    (_DATA / "sample_2.png", "мареммах лессу"),
]

CMD = "hh-applicant-tool config -s"
HINT_CLAUDE = f"""\
Способ 1 — подписка Claude (Claude Code установлен и вы в него вошли), денег на API не нужно:
  {CMD} openai_captcha.provider claude-cli
  {CMD} openai_captcha.claude_model opus"""
HINT_OPENROUTER = f"""\
Способ 2 — OpenRouter (ключ с балансом, $5 хватит на месяцы):
  {CMD} openai_captcha.base_url https://openrouter.ai/api/v1/chat/completions
  {CMD} openai_captcha.api_key sk-or-v1-КЛЮЧ
  {CMD} openai_captcha.model {RECOMMENDED_MODEL}
Бесплатные модели (:free, Groq) для каптчи не годятся: лимиты частоты и низкая точность."""


class Namespace(BaseNamespace):
    pass


def normalize(text: str | None) -> str:
    text = (text or "").lower().replace("ё", "е")
    return " ".join(re.sub(r"[*_`\"'«»“”„.,:;!?]", " ", text).split())


def evaluate_samples(recognize: Callable[[bytes], str]) -> tuple[int, list[dict]]:
    ok = 0
    details = []
    for path, expected in SAMPLES:
        item = {"sample": path.name, "expected": expected, "got": "", "ok": False, "error": ""}
        try:
            item["got"] = normalize(recognize(path.read_bytes()))
            item["ok"] = item["got"] == expected
        except Exception as ex:
            item["error"] = str(ex)
        ok += item["ok"]
        details.append(item)
    return ok, details


def print_hints() -> None:
    # Есть claude в PATH — подписка проще: ни ключа, ни баланса
    hints = [HINT_CLAUDE, HINT_OPENROUTER]
    if not shutil.which("claude"):
        hints.reverse()
    print("\n\n".join(hints))


class Operation(BaseOperation):
    """Проверит, что распознавание каптчи hh настроено (без откликов)"""

    __aliases__: list[str] = ["test-captcha"]

    def setup_parser(self, parser: argparse.ArgumentParser) -> None:
        pass

    def run(self, tool: HHApplicantTool, args: BaseNamespace) -> int:
        c = tool.captcha_config()
        provider = c.get("provider")

        if provider == "claude-cli":
            claude_bin = c.get("claude_bin") or "claude"
            if not shutil.which(claude_bin):
                print(f"❌ provider = claude-cli, но {claude_bin} не найден в PATH.")
                print("Установите Claude Code и войдите в подписку, либо настройте OpenRouter.\n")
                print_hints()
                return 1
        else:
            missing = [k for k in ("base_url", "api_key", "model") if not c.get(k)]
            if missing:
                print("❌ Распознавание каптчи не настроено: нет openai_captcha." + ", ".join(missing))
                print_hints()
                return 1

        ai = tool.get_captcha_ai()
        fallback = getattr(ai, "fallback", None)
        print(
            f"Способ: {'подписка Claude (claude-cli)' if provider == 'claude-cli' else 'OpenRouter/OpenAI'}, "
            f"модель: {ai.model}"
            + (f", запасной: {fallback.model}" if fallback is not None else "")
        )
        ok, details = evaluate_samples(
            lambda img: ai.solve_captcha(img, language=SAMPLE_LANGUAGE)
        )
        for d in details:
            mark = "✅" if d["ok"] else "❌"
            got = d["error"] or d["got"] or "(пусто)"
            print(f"  {mark} ожидалось «{d['expected']}», получено: {got}")

        if ok == len(details):
            print(
                f"✅ Каптча настроена: {ok}/{len(details)}. Решатель использует "
                "веб-сессию — если test-session ругается, сделайте authorize."
            )
            return 0

        errors = " ".join(d["error"] for d in details)
        if "402" in errors:
            print("❌ OpenRouter вернул 402 — на ключе нет денег. Пополните баланс.")
        elif "429" in errors:
            print("❌ 429 — лимит частоты (частая беда бесплатных моделей).")
        elif provider == "claude-cli" and errors:
            print("❌ claude не ответил как надо (не вошли в подписку? отказ модели?). Запустите `claude` и проверьте вход.")
        print(f"❌ Распознано {ok}/{len(details)}.\n")
        print_hints()
        return 1
