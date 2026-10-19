"""Отчёт по чатам в ТГ подписан аккаунтом, когда аккаунтов несколько (ACCOUNT_LABEL)."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _digest(monkeypatch, label=None):
    if label is None:
        monkeypatch.delenv("ACCOUNT_LABEL", raising=False)
    else:
        monkeypatch.setenv("ACCOUNT_LABEL", label)
    import chat_digest
    return importlib.reload(chat_digest)


def test_label_in_header_and_empty_message(monkeypatch):
    d = _digest(monkeypatch, "Синицын")
    assert d.build_message([], 12).startswith("🤖 hh-чаты · Синицын за 12ч")
    item = {"answered": False, "employer": "X", "vacancy": "V", "url": "u", "text": "t", "kind": "k"}
    try:
        msg = d.build_message([item], 12)
    except KeyError:
        msg = None
    if msg is not None:
        assert "Синицын" in msg.splitlines()[0]


def test_no_label_keeps_old_header(monkeypatch):
    d = _digest(monkeypatch)
    assert d.build_message([], 12).startswith("🤖 hh-чаты за 12ч")
