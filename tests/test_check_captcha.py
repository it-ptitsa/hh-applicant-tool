"""check-captcha: проверка распознавания каптчи без откликов (для учеников)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hh_applicant_tool.operations import check_captcha as cc
from hh_applicant_tool.operations.check_captcha import (
    SAMPLES,
    Operation,
    evaluate_samples,
)

BY_BYTES = {p.read_bytes(): a for p, a in SAMPLES}


def test_samples_bundled_with_package():
    assert len(SAMPLES) >= 2
    for path, answer in SAMPLES:
        assert path.exists(), path
        assert path.read_bytes()[:4] == b"\x89PNG"
        assert len(answer.split()) == 2


def test_evaluate_normalizes_answers():
    ok, details = evaluate_samples(lambda img: "**" + BY_BYTES[img].upper() + ".**")
    assert ok == len(SAMPLES) and all(d["ok"] for d in details)


def test_evaluate_errors_are_reported_not_raised():
    def boom(img):
        raise RuntimeError("402 Payment Required")

    ok, details = evaluate_samples(boom)
    assert ok == 0 and "402" in details[0]["error"]


def _tool(cfg: dict, recognize=None, model="m", fallback=None):
    calls = []

    def solve(img, language):
        calls.append(language)
        return recognize(img)

    ai = SimpleNamespace(model=model, solve_captcha=solve, fallback=fallback)
    tool = SimpleNamespace(captcha_config=lambda: cfg, get_captcha_ai=lambda: ai)
    return tool, calls


def test_nothing_configured_prints_both_ways(capsys, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda b: "/usr/bin/claude")
    tool, _ = _tool({})
    assert Operation().run(tool, SimpleNamespace()) == 1
    out = capsys.readouterr().out
    assert "openai_captcha.provider claude-cli" in out
    assert "openai_captcha.api_key" in out
    # claude установлен — подписка предлагается первой
    assert out.index("claude-cli") < out.index("openrouter.ai")


def test_openrouter_first_when_no_claude(capsys, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda b: None)
    tool, _ = _tool({})
    Operation().run(tool, SimpleNamespace())
    out = capsys.readouterr().out
    assert out.index("openrouter.ai") < out.index("provider claude-cli")


def test_claude_cli_without_binary(capsys, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda b: None)
    tool, _ = _tool({"provider": "claude-cli"})
    assert Operation().run(tool, SimpleNamespace()) == 1
    assert "не найден в PATH" in capsys.readouterr().out


def test_claude_cli_all_ok(capsys, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda b: "/usr/bin/claude")
    tool, calls = _tool({"provider": "claude-cli"}, lambda img: BY_BYTES[img], model="opus")
    assert Operation().run(tool, SimpleNamespace()) == 0
    out = capsys.readouterr().out
    assert "подписка Claude" in out and "✅ Каптча настроена" in out
    assert calls == ["ru", "ru"]  # образцы кириллические


def test_openrouter_missing_base_url_names_it(capsys):
    tool, _ = _tool({"api_key": "k", "model": "m"})
    assert Operation().run(tool, SimpleNamespace()) == 1
    assert "base_url" in capsys.readouterr().out


def test_openrouter_wrong_answers(capsys):
    tool, _ = _tool({"base_url": "u", "api_key": "k", "model": "openai/gpt-4o-mini"}, lambda img: "мимо мимо")
    assert Operation().run(tool, SimpleNamespace()) == 1
    assert "Распознано 0/" in capsys.readouterr().out


@pytest.mark.parametrize("err,phrase", [("HTTP 402", "402"), ("HTTP 429", "429")])
def test_openrouter_errors_explained(capsys, err, phrase):
    def boom(img):
        raise RuntimeError(err)

    tool, _ = _tool({"base_url": "u", "api_key": "k", "model": "m"}, boom)
    assert Operation().run(tool, SimpleNamespace()) == 1
    assert phrase in capsys.readouterr().out
