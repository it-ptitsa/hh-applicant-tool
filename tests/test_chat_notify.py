"""Уведомления сторожа чатов группируются по вакансии (22.10.2026).

На аккаунте несколько резюме (эксперимент «все резюме на каждую вакансию»), у каждого свой чат с
работодателем. Робот-рекрутер шлёт одну и ту же анкету в каждый чат — и сторож писал в ТГ по строке на
резюме: «📋 Анкета — обработай вручную» ×5 по одной вакансии. Теперь одна строка на вакансию с числом
резюме, а то, о чём уже писали за сутки, не повторяется.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import chat_notify as cn  # noqa: E402

V1 = "https://hh.ru/vacancy/137975987"
V2 = "https://hh.ru/vacancy/138605254"
NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)

RUN = f"""📋 Анкета — обработай вручную: {V1}
📋 Анкета — обработай вручную: {V2}
📋 Анкета — обработай вручную: {V1}
📋 Анкета — обработай вручную: {V1}
📝 Сообщения разосланы!
⏸  {V2} | Используем эти ответы? | варианты: ['Да. Используем те же ответы', 'Нет. Отвечу заново'] → решай сам
⏸  {V2} | Используем эти ответы? | варианты: ['Да. Используем те же ответы', 'Нет. Отвечу заново'] → решай сам
✅ {V1} | Удобен офис на Остоженке? → «да»
✅ {V1} | Удобен офис на Остоженке? → «да»
   ⚠️ отправка не прошла: 500
Итого: отправлено 1, оставлено тебе 2
"""


def test_one_block_per_vacancy_with_resume_count():
    text = cn.render(cn.parse(RUN.splitlines()), label="")
    assert text.splitlines()[0] == "💬 Ответы в чатах hh"
    assert text.count(V1) == 1 and text.count(V2) == 1
    assert "📋 Анкета — обработай вручную · 3 резюме" in text
    assert "⏸ «Используем эти ответы?» — решай сам · 2 резюме\n   варианты: Да. Используем те же ответы / Нет. Отвечу заново" in text
    assert "✅ «Удобен офис на Остоженке?» → «да» · 2 резюме" in text
    assert "⚠️ Ответ кнопкой не отправился (500)" in text
    # блок вакансии: ссылка, под ней её события
    block_v2 = text.split(V2)[1].split("https://")[0]
    assert "📋 Анкета — обработай вручную\n" in block_v2 + "\n"  # одно резюме — без счётчика


def test_label_and_noise_lines_ignored():
    events = cn.parse(["📝 Сообщения разосланы!", "Итого: отправлено 0, оставлено тебе 0", "🚶 Пропускаем чат"])
    assert events == []
    assert cn.render([], label="Синицын") == ""
    text = cn.render(cn.parse([f"📨 Отправлено для {V1}"]), label="Синицын")
    assert text.splitlines()[0] == "💬 Ответы в чатах hh · Синицын"
    assert "📨 Ответил в чате" in text


def test_already_notified_within_window_is_not_repeated():
    events = cn.parse(RUN.splitlines())
    fresh, seen = cn.filter_seen(events, {}, NOW, hours=24)
    assert fresh == events

    # через 4 часа робот прислал ту же анкету в чат ещё одного резюме — молчим
    later = cn.parse([f"📋 Анкета — обработай вручную: {V1}", f"⏸  {V2} | Новый вопрос? | варианты: ['а', 'б'] → решай сам"])
    fresh2, seen2 = cn.filter_seen(later, seen, NOW + timedelta(hours=4), hours=24)
    assert [e["kind"] for e in fresh2] == ["ask"]  # новый вопрос — пишем, повтор анкеты — нет

    # через сутки напоминаем снова
    fresh3, _ = cn.filter_seen(later[:1], seen2, NOW + timedelta(hours=25), hours=24)
    assert len(fresh3) == 1


def test_seen_state_drops_expired_keys():
    _, seen = cn.filter_seen(cn.parse([f"📨 Отправлено для {V1}"]), {"old": (NOW - timedelta(days=3)).isoformat()},
                             NOW, hours=24)
    assert "old" not in seen and len(seen) == 1


def test_tg_payload_plain_text_without_preview():
    assert cn.tg_payload("42", "t") == {"chat_id": "42", "text": "t", "disable_web_page_preview": True}


# ── граница с операциями: группируем НАСТОЯЩИЙ вывод reply-employers и answer-buttons ──

def test_real_reply_employers_output_for_three_resumes(capsys):
    from hh_applicant_tool.operations import reply_employers as re_op

    class Api:
        def get(self, path, **params):
            return {"items": [{"text": "Заполните анкету: ЗП, график, тестовое", "author": {"participant_type": "employer"},
                               "created_at": "2026-10-22T10:00:00+0300"}], "pages": 1}

        def post(self, path, **kw):
            pass

    def neg(nid, rid):
        return {"id": nid, "has_new_messages": True, "resume": {"id": rid}, "state": {"id": "response"},
                "updated_at": "2026-10-22T10:00:00+0300",
                "vacancy": {"id": "137975987", "name": "Frontend", "alternate_url": V1}}

    op = re_op.Operation()
    op.tool = SimpleNamespace(get_negotiations=lambda: [neg("a", "r1"), neg("b", "r2"), neg("c", "r3")], api_client=Api())
    op.api_client = op.tool.api_client
    op.only_new, op.period, op.only_invitations, op.dry_run = True, None, False, False
    op.reply_message, op.resume_id, op.message_prompt = None, None, ""
    op.cover_letter_ai = SimpleNamespace(complete=lambda q: "__SKIP__")
    op._build_resume_context = lambda resume: ""
    op._reply_chats(user={}, resumes=[{"id": r, "title": "Frontend"} for r in ("r1", "r2", "r3")],
                    blacklist=set(), manual_skip=set())
    text = cn.render(cn.parse(capsys.readouterr().out.splitlines()), label="")
    assert text.count(V1) == 1
    assert "📋 Анкета — обработай вручную · 3 резюме" in text


def test_real_answer_buttons_output_for_two_resumes(capsys, tmp_path):
    from hh_applicant_tool.operations import answer_buttons as ab

    chats = [{"id": 11, "type": "NEGOTIATION"}, {"id": 12, "type": "NEGOTIATION"}, {"id": 13, "type": "NEGOTIATION"}]

    def chat_data(cid, question, options):
        return {"chat": {"id": cid, "currentParticipantId": "1", "resources": {"VACANCY": ["138605254"]},
                         "messages": {"items": [{"id": 1, "text": question, "participantId": "2",
                                                 "actions": {"text_buttons": [{"text": o} for o in options]}}]}}}

    data = {11: chat_data(11, "Используем эти ответы?", ["Да", "Нет"]),
            12: chat_data(12, "Используем эти ответы?", ["Да", "Нет"]),
            13: chat_data(13, "Готовы к офису?", ["да", "нет"])}

    class Resp:
        def __init__(self, body=None, status=200):
            self.body, self.status_code = body, status

        def json(self):
            return self.body

    class Session:
        headers, cookies = {}, []

        def get(self, url, params=None, timeout=None):
            if url.endswith("/chats"):
                return Resp({"chats": {"items": chats if params["page"] == 0 else []}})
            return Resp(data[params["chatId"]])

        def post(self, url, **kw):
            return Resp(status=500)

    answers = iter(["__SKIP__", "__SKIP__", "1"])
    tool = SimpleNamespace(session=Session(), config_path=tmp_path, get_resumes=lambda: [{"id": "r"}],
                           api_client=SimpleNamespace(get=lambda path: {"title": "Frontend"}),
                           get_cover_letter_ai=lambda prompt: SimpleNamespace(complete=lambda q: next(answers)))
    ab.Operation().run(tool, SimpleNamespace(send=True, pages=1, prefs_file=None))
    text = cn.render(cn.parse(capsys.readouterr().out.splitlines()), label="")
    assert text.count(V2) == 1
    assert "⏸ «Используем эти ответы?» — решай сам · 2 резюме\n   варианты: Да / Нет" in text
    assert "✅ «Готовы к офису?» → «да»" in text
    assert "⚠️ Ответ кнопкой не отправился (500)" in text


# ── сквозной: chat_watch.sh → chat_notify.py → Telegram ─────────────────────

class _Tg(BaseHTTPRequestHandler):
    sent: list = []

    def do_POST(self):  # noqa: N802
        _Tg.sent.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        body = b'{"ok": true, "result": {}}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_chat_watch_sends_one_grouped_message_and_does_not_repeat(tmp_path):
    srv = HTTPServer(("127.0.0.1", 0), _Tg)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _Tg.sent = []
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "replies.txt").write_text(RUN.split("📝")[0] + "📝 Сообщения разосланы!\n")
    (tmp_path / "buttons.txt").write_text("⏸" + RUN.split("⏸", 1)[1])
    # docker: watch-chats → «есть новое»; reply-employers/answer-buttons → вывод с несколькими резюме
    (bin_dir / "docker").write_text(
        "#!/bin/bash\n"
        f'case "$*" in *reply-employers*) cat {tmp_path / "replies.txt"};; '
        f'*answer-buttons*) cat {tmp_path / "buttons.txt"};; esac\nexit 0\n')
    (bin_dir / "flock").write_text("#!/bin/bash\nexit 0\n")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env_file = tmp_path / "digest.env"
    env_file.write_text("TELEGRAM_NOTIFY_BOT_TOKEN=T0K\nTELEGRAM_NOTIFY_CHAT_ID=42\n"
                        f"TELEGRAM_API_URL=http://127.0.0.1:{srv.server_port}\n")
    env = {k: v for k, v in os.environ.items() if k not in ("HH_CMD", "HH_PROFILE")}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", DIGEST_ENV=str(env_file), CLAUDE_BIN=str(tmp_path / "no-claude"),
               CHAT_NOTIFY_STATE=str(tmp_path / "seen.json"), ACCOUNT_LABEL="Синицын")
    try:
        for _ in range(2):
            subprocess.run(["bash", str(ROOT / "chat_watch.sh")], env=env, check=True, timeout=60,
                           capture_output=True)
    finally:
        srv.shutdown()
    assert len(_Tg.sent) == 1, "второй прогон с теми же событиями не пишет"
    path, payload = _Tg.sent[0]
    assert path == "/botT0K/sendMessage"
    assert payload["chat_id"] == "42"
    assert payload["text"].startswith("💬 Ответы в чатах hh · Синицын")
    assert payload["text"].count(V1) == 1 and "· 3 резюме" in payload["text"]
