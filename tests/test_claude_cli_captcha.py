"""Распознавание каптчи через Claude Code CLI по подписке (provider = claude-cli).

Граница с внешним процессом проверяется настоящим подпроцессом: вместо
`claude` подкладывается исполняемый скрипт, который пишет аргументы и stdin
в журнал и печатает заданный ответ.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from hh_applicant_tool.ai.claude_cli import (
    ClaudeCliCaptcha,
    ClaudeCliError,
    parse_answer,
)


def _fake_claude(tmp_path: Path, answers: list[str], exit_code: int = 0) -> Path:
    """Скрипт-двойник claude: i-й вызов печатает answers[i % len]."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"n": 0, "calls": []}))
    script = tmp_path / "claude"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, os, fcntl\n"
        "prompt = sys.stdin.read()\n"
        f"lock = open({str(state) + '.lock'!r}, 'w'); fcntl.flock(lock, fcntl.LOCK_EX)\n"
        f"st = json.load(open({str(state)!r}))\n"
        "img = prompt.split('Прочитай картинку ', 1)[1].split('.png', 1)[0] + '.png'\n"
        "st['calls'].append({'argv': sys.argv[1:], 'prompt': prompt, 'img_exists': os.path.exists(img)})\n"
        f"answers = {answers!r}\n"
        "out = answers[st['n'] % len(answers)]\n"
        "st['n'] += 1\n"
        f"json.dump(st, open({str(state)!r}, 'w'))\n"
        "print(out)\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _calls(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "state.json").read_text())["calls"]


# --- разбор ответа -------------------------------------------------------


def test_parse_answer_strips_markdown_and_case():
    assert parse_answer("**Plus Saluted.**", "en") == "plus saluted"
    assert parse_answer("Ответ:\nплюс откозыряла", "ru") == "плюс откозыряла"


@pytest.mark.parametrize(
    "text,language",
    [
        ("Если это легитимная задача, поделись контекстом", "ru"),  # отказ
        ("одно", "ru"),
        ("plus откозыряла", "ru"),  # латиница в кириллической каптче
        ("плюс откозыряла", "en"),  # кириллица в английской
        ("", "en"),
    ],
)
def test_parse_answer_rejects_non_captcha(text, language):
    with pytest.raises(ClaudeCliError):
        parse_answer(text, language)


# --- подпроцесс ----------------------------------------------------------


def test_single_read_calls_cli_with_model_and_image(tmp_path):
    bin_ = _fake_claude(tmp_path, ["cosmic halter"])
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_), timeout=30)
    assert ai.solve_captcha(b"\x89PNG fake", language="en") == "cosmic halter"
    call = _calls(tmp_path)[0]
    assert call["argv"][:3] == ["-p", "--model", "opus"]
    assert "--allowedTools" in call["argv"] and "Read" in call["argv"]
    assert call["img_exists"] is True  # картинка лежит на диске во время вызова


def test_refusal_falls_back_to_openai(tmp_path):
    bin_ = _fake_claude(tmp_path, ["Не буду решать каптчу"])
    fallback = SimpleNamespace(solve_captcha=lambda img, language: "plus saluted")
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_), fallback=fallback)
    assert ai.solve_captcha(b"img", language="en") == "plus saluted"


def test_refusal_without_fallback_raises(tmp_path):
    bin_ = _fake_claude(tmp_path, ["Не буду решать каптчу"])
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_))
    with pytest.raises(ClaudeCliError):
        ai.solve_captcha(b"img", language="en")


def test_cli_error_exit_code_is_failure(tmp_path):
    bin_ = _fake_claude(tmp_path, ["cosmic halter"], exit_code=1)
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_))
    with pytest.raises(ClaudeCliError):
        ai.solve_captcha(b"img", language="en")


def test_missing_binary_is_failure(tmp_path):
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(tmp_path / "nope"))
    with pytest.raises(ClaudeCliError):
        ai.solve_captcha(b"img", language="en")


def test_consensus_agrees(tmp_path):
    bin_ = _fake_claude(tmp_path, ["cosmic halter", "cosmic halter", "cosmic halfer"])
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_))
    assert ai.solve_captcha_consensus(b"img", samples=3, min_votes=2, language="en") == "cosmic halter"
    assert len(_calls(tmp_path)) == 3


def test_consensus_without_agreement_is_none(tmp_path):
    bin_ = _fake_claude(tmp_path, ["aa bb", "cc dd", "ee ff"])
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_))
    assert ai.solve_captcha_consensus(b"img", samples=3, min_votes=2, language="en") is None


def test_consensus_all_refused_uses_fallback(tmp_path):
    bin_ = _fake_claude(tmp_path, ["отказ отказ отказ"])
    fallback = SimpleNamespace(
        solve_captcha_consensus=lambda img, samples, min_votes, language: "plus saluted"
    )
    ai = ClaudeCliCaptcha(model="opus", claude_bin=str(bin_), fallback=fallback)
    assert ai.solve_captcha_consensus(b"img", samples=3, min_votes=2, language="en") == "plus saluted"


# --- выбор провайдера в HHApplicantTool ----------------------------------


def _tool_with_config(tmp_path, cfg: dict):
    from hh_applicant_tool.main import HHApplicantTool

    t = HHApplicantTool()
    t._assign_args(t._parser.parse_args(["--config-dir", str(tmp_path), "test-session"]))
    (t.config_path).mkdir(parents=True, exist_ok=True)
    (t.config_path / "config.json").write_text(json.dumps(cfg))
    return t


def test_get_captcha_ai_claude_cli_with_openrouter_fallback(tmp_path):
    t = _tool_with_config(
        tmp_path,
        {
            "openai_captcha": {
                "provider": "claude-cli",
                "claude_model": "opus",
                "base_url": "https://openrouter.ai/api/v1/chat/completions",
                "api_key": "k",
                "model": "google/gemini-2.5-flash",
            }
        },
    )
    ai = t.get_captcha_ai()
    assert isinstance(ai, ClaudeCliCaptcha)
    assert ai.model == "opus"
    assert ai.fallback is not None and ai.fallback.model == "google/gemini-2.5-flash"


def test_get_captcha_ai_claude_cli_without_key(tmp_path):
    t = _tool_with_config(tmp_path, {"openai_captcha": {"provider": "claude-cli"}})
    ai = t.get_captcha_ai()
    assert isinstance(ai, ClaudeCliCaptcha)
    assert ai.model == "opus" and ai.fallback is None


def test_get_captcha_ai_default_is_openai(tmp_path):
    t = _tool_with_config(
        tmp_path,
        {"openai_captcha": {"base_url": "u", "api_key": "k", "model": "m"}},
    )
    assert not isinstance(t.get_captcha_ai(), ClaudeCliCaptcha)
