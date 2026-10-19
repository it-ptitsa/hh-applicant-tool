"""Письмо через подписку Claude (openai_cover_letter.provider = claude-cli)
и сквозная сборка письма в цикле откликов."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hh_applicant_tool.ai.claude_cli import ClaudeCliChat, ClaudeCliError

from .test_apply_limit_shutdown import _make_operation, _make_vacancy


def _fake_claude(tmp_path: Path, out: str, code: int = 0) -> Path:
    log = tmp_path / "calls.json"
    log.write_text("[]")
    s = tmp_path / "claude"
    s.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"calls = json.load(open({str(log)!r}))\n"
        "calls.append({'argv': sys.argv[1:], 'stdin': sys.stdin.read()})\n"
        f"json.dump(calls, open({str(log)!r}, 'w'))\n"
        f"print({out!r})\nsys.exit({code})\n"
    )
    s.chmod(s.stat().st_mode | stat.S_IEXEC)
    return s


def test_chat_passes_system_prompt_model_and_no_tools(tmp_path):
    bin_ = _fake_claude(tmp_path, "Абзац письма")
    chat = ClaudeCliChat(system_prompt="Ты пишешь абзац", model="opus", claude_bin=str(bin_))
    assert chat.complete("вакансия X") == "Абзац письма"
    call = json.loads((tmp_path / "calls.json").read_text())[0]
    argv = call["argv"]
    assert argv[:3] == ["-p", "--model", "opus"]
    assert argv[argv.index("--system-prompt") + 1] == "Ты пишешь абзац"
    assert argv[argv.index("--tools") + 1] == ""
    assert call["stdin"] == "вакансия X"


def test_chat_nonzero_exit_raises(tmp_path):
    chat = ClaudeCliChat(system_prompt="s", claude_bin=str(_fake_claude(tmp_path, "x", 1)))
    with pytest.raises(ClaudeCliError):
        chat.complete("m")


def test_get_cover_letter_ai_claude_cli(tmp_path):
    from hh_applicant_tool.main import HHApplicantTool

    t = HHApplicantTool()
    t._assign_args(t._parser.parse_args(["--config-dir", str(tmp_path), "test-session"]))
    t.config_path.mkdir(parents=True, exist_ok=True)
    (t.config_path / "config.json").write_text(
        json.dumps({"openai_cover_letter": {"provider": "claude-cli", "claude_model": "opus"}})
    )
    ai = t.get_cover_letter_ai("системный")
    assert isinstance(ai, ClaudeCliChat) and ai.model == "opus" and ai.system_prompt == "системный"


def test_apply_loop_sends_template_letter_with_ai_paragraph():
    op = _make_operation(max_responses=0)
    op.force_message = True
    op.message_prompt = "Напиши абзац"
    op.cover_letter = "Здравствуйте!\n\n%(ai_match)s\n\nСемь лет во фронтенде."
    op.cover_letter_ai = MagicMock()
    op.cover_letter_ai.complete.return_value = (
        "Увидел у вас Next.js и SSR — в Базаре я переводил публичные страницы на SSR со стримингом."
    )
    op._get_vacancies = lambda resume_id=None, resume_title="": iter([_make_vacancy(1)])
    resume = {"id": "r1", "title": "Dev", "alternate_url": "u"}
    user = {"first_name": "A", "last_name": "B", "email": "a@b.c", "phone": ""}
    op._apply_resume(resume=resume, user=user, seen_employers=set())

    params = op.tool.api_client.post.call_args.args[1]
    letter = params["message"]
    assert letter.startswith("Здравствуйте!\n\n") and letter.endswith("Семь лет во фронтенде.")
    assert "Next.js и SSR – в Базаре" in letter and "—" not in letter
    # модели ушла вакансия, а не просьба переписать всё письмо
    msg = op.cover_letter_ai.complete.call_args.args[0]
    assert "Vacancy 1" in msg and "Напиши абзац" in msg
