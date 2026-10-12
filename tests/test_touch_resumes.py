"""Ежедневная мелкая правка резюме (правило Александра 12.10.2026: активность аккаунта)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import touch_resumes as tr  # noqa: E402


def test_toggle_adds_and_removes_final_dot():
    assert tr.toggle_dot("Telegram: @it_devel_it") == "Telegram: @it_devel_it."
    assert tr.toggle_dot("Telegram: @it_devel_it.") == "Telegram: @it_devel_it"


def test_toggle_twice_returns_original():
    s = "О себе.\n\nКонтакты"
    assert tr.toggle_dot(tr.toggle_dot(s)) == s


class FakeApi:
    def __init__(self, resumes):
        self.resumes, self.puts = resumes, []

    def get(self, path):
        if path == "/resumes/mine":
            return {"items": [{"id": r["id"], "status": {"id": r["status"]}} for r in self.resumes]}
        return next(r for r in self.resumes if path.endswith(r["id"]))

    def put(self, path, body):
        self.puts.append((path, body))


def test_touch_only_published_and_reports():
    api = FakeApi([{"id": "a", "status": "published", "skills": "О себе"},
                   {"id": "b", "status": "not_published", "skills": "x"}])
    done = tr.touch_all(api)
    assert done == ["a"] and api.puts == [("/resumes/a", {"skills": "О себе."})]
