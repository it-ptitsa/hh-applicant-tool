"""check-captcha: проверка распознавания каптчи без откликов (для учеников)."""

from __future__ import annotations

from types import SimpleNamespace

from hh_applicant_tool.operations.check_captcha import (
    SAMPLES,
    Operation,
    evaluate_samples,
)


def test_samples_bundled_with_package():
    assert len(SAMPLES) >= 2
    for path, answer in SAMPLES:
        assert path.exists(), path
        assert path.read_bytes()[:4] == b"\x89PNG"
        assert len(answer.split()) == 2


def test_evaluate_all_correct():
    answers = {p.name: a for p, a in SAMPLES}
    by_bytes = {p.read_bytes(): a for p, a in SAMPLES}
    ok, details = evaluate_samples(lambda img: by_bytes[img].upper() + ".")
    assert ok == len(SAMPLES)
    assert all(d["ok"] for d in details)


def test_evaluate_errors_are_reported_not_raised():
    def boom(img):
        raise RuntimeError("402 Payment Required")

    ok, details = evaluate_samples(boom)
    assert ok == 0
    assert "402" in details[0]["error"]


class FakeConfig(dict):
    pass


def _tool(section, recognize=None):
    return SimpleNamespace(
        config=FakeConfig({"openai_captcha": section} if section is not None else {}),
        get_captcha_ai=lambda: SimpleNamespace(
            model=(section or {}).get("model"), solve_captcha=recognize
        ),
    )


def test_run_without_config_returns_error(capsys):
    rc = Operation().run(_tool(None), SimpleNamespace())
    assert rc == 1
    assert "openai_captcha" in capsys.readouterr().out


def test_run_all_ok(capsys):
    by_bytes = {p.read_bytes(): a for p, a in SAMPLES}
    tool = _tool({"base_url": "u", "api_key": "k", "model": "google/gemini-2.5-flash"}, lambda img: by_bytes[img])
    assert Operation().run(tool, SimpleNamespace()) == 0
    assert "✅" in capsys.readouterr().out


def test_run_all_wrong_suggests_model(capsys):
    tool = _tool({"base_url": "u", "api_key": "k", "model": "openai/gpt-4o-mini"}, lambda img: "мимо мимо")
    assert Operation().run(tool, SimpleNamespace()) == 1
    assert "google/gemini-2.5-flash" in capsys.readouterr().out


def test_run_without_base_url_names_it(capsys):
    rc = Operation().run(_tool({"api_key": "k", "model": "m"}), SimpleNamespace())
    assert rc == 1
    assert "base_url" in capsys.readouterr().out
