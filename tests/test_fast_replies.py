"""Оперативные ответы в чатах (13.10.2026): сторож раз в 5 минут + ответы только в чатах с новыми сообщениями."""

from types import SimpleNamespace

from hh_applicant_tool.operations import reply_employers as re_op
from hh_applicant_tool.operations import watch_chats as wc


# ── сторож: есть ли новое сообщение от собеседника ──────────────────────

def _chat(cid, when, from_me=False):
    me, other = "1", "2"
    return {"id": cid, "currentParticipantId": me,
            "lastMessage": {"participantId": me if from_me else other, "creationTime": when}}


def test_newest_message_from_other_side_only():
    chats = [_chat(1, "2026-10-13T10:00:00+03:00"), _chat(2, "2026-10-13T11:00:00+03:00", from_me=True),
             _chat(3, "2026-10-13T09:00:00+03:00")]
    assert wc.newest_incoming(chats) == "2026-10-13T10:00:00+03:00"


def test_trigger_only_when_newer_than_saved():
    assert wc.should_run("2026-10-13T10:00:00+03:00", None) is True
    assert wc.should_run("2026-10-13T10:00:00+03:00", "2026-10-13T09:59:00+03:00") is True
    assert wc.should_run("2026-10-13T10:00:00+03:00", "2026-10-13T10:00:00+03:00") is False
    assert wc.should_run(None, "2026-10-13T10:00:00+03:00") is False


# ── reply-employers --only-new: переписку читаем только там, где есть новые сообщения ─

class Api:
    def __init__(self):
        self.gets, self.posts = [], []

    def get(self, path, **params):
        self.gets.append(path)
        return {"items": [{"text": "Когда сможете выйти?", "author": {"participant_type": "employer"},
                           "created_at": "2026-10-13T10:00:00+0300"}], "pages": 1}

    def post(self, path, **kw):
        self.posts.append(path)


def _neg(nid, new):
    return {"id": nid, "has_new_messages": new, "resume": {"id": "r"}, "state": {"id": "response"},
            "updated_at": "2026-10-13T10:00:00+0300", "vacancy": {"id": nid, "name": "Frontend", "alternate_url": "u"}}


def _op(only_new):
    op = re_op.Operation()
    api = Api()
    op.tool = SimpleNamespace(get_negotiations=lambda: [_neg("a", True), _neg("b", False)], api_client=api)
    op.api_client = api
    op.only_new, op.period, op.only_invitations, op.dry_run = only_new, None, False, False
    op.reply_message, op.cover_letter_ai, op.resume_id = "Добрый день!", None, None
    return op, api


def test_only_new_skips_chats_without_new_messages():
    op, api = _op(only_new=True)
    op._reply_chats(user={}, resumes=[{"id": "r", "title": "Frontend"}], blacklist=set(), manual_skip=set())
    assert api.gets == ["/negotiations/a/messages"]
    assert api.posts == ["/negotiations/a/messages"]


def test_without_flag_all_chats_are_read():
    op, api = _op(only_new=False)
    op._reply_chats(user={}, resumes=[{"id": "r", "title": "Frontend"}], blacklist=set(), manual_skip=set())
    assert api.gets == ["/negotiations/a/messages", "/negotiations/b/messages"]


def test_cli_flags():
    from hh_applicant_tool.main import HHApplicantTool
    t = HHApplicantTool()
    assert t._parser.parse_args(["reply-employers", "--only-new"]).only_new is True
    assert t._parser.parse_args(["watch-chats"]).operation_run is not None
