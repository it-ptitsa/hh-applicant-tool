"""chat_watch.sh пробрасывает в контейнер claude и токен подписки, если они есть на хосте.

Без этого у аккаунта с provider claude-cli ответы в чатах падали с «не найден claude»
(Синицын, 20.10.2026). Токен уходит через окружение, а не в аргументах docker:
аргументы видны в ps.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "chat_watch.sh"


def _run(tmp_path, *, with_claude: bool) -> list[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    # docker пишет свои аргументы и окружение и «нет новых сообщений» (код 1) — скрипт выходит
    (bin_dir / "docker").write_text(
        f'#!/bin/bash\nprintf "%s\\n" "$@" > {log}\n'
        f'echo "ENV_TOKEN=${{CLAUDE_CODE_OAUTH_TOKEN:-}}" >> {log}\nexit 1\n'
    )
    (bin_dir / "flock").write_text("#!/bin/bash\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("HH_CMD", "CLAUDE_CODE_OAUTH_TOKEN")}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", HH_PROFILE="acc5")
    claude_bin = tmp_path / "claude"
    token_file = tmp_path / "token"
    if with_claude:
        claude_bin.write_text("#!/bin/bash\n")
        claude_bin.chmod(0o755)
        token_file.write_text("secret-oauth-token\n")
    env.update(CLAUDE_BIN=str(claude_bin), CLAUDE_TOKEN_FILE=str(token_file))
    subprocess.run(["bash", str(SCRIPT)], env=env, check=True, timeout=30)
    return log.read_text().splitlines()


def test_mounts_claude_and_passes_token_via_env(tmp_path):
    lines = _run(tmp_path, with_claude=True)
    args = lines[:-1]
    assert f"{tmp_path / 'claude'}:/usr/local/bin/claude:ro" in args
    assert "CLAUDE_CODE_OAUTH_TOKEN" in args  # имя без значения: docker берёт из окружения
    assert not any("secret-oauth-token" in a for a in args)
    assert lines[-1] == "ENV_TOKEN=secret-oauth-token"
    assert args[-3:] == ["--profile-id", "acc5", "watch-chats"]


def test_without_claude_on_host_command_unchanged(tmp_path):
    lines = _run(tmp_path, with_claude=False)
    assert not any("claude" in a for a in lines[:-1])
    assert lines[-1] == "ENV_TOKEN="
