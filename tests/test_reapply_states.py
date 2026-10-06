"""--reapply-rejected + --reapply-states: какие прошлые отклики переоткликать.

По умолчанию — только отказы (discard), как было. С `--reapply-states
discard response` — ещё и отклики без ответа. Вакансии, где уже есть отклик
ЭТИМ резюме, и архивные — пропускаются.
"""

from __future__ import annotations

from types import SimpleNamespace

from hh_applicant_tool.operations.apply_vacancies import Operation


class FakeApi:
    def __init__(self, negotiations, vacancies):
        self.negotiations = negotiations
        self.vacancies = vacancies
        self.neg_params = []

    def get(self, path, params=None):
        if path == "/negotiations":
            self.neg_params.append(dict(params or {}))
            return {"items": self.negotiations, "pages": 1}
        vid = path.rsplit("/", 1)[1]
        return self.vacancies[vid]


def _neg(vid, state, resume="old"):
    return {"vacancy": {"id": vid}, "state": {"id": state}, "resume": {"id": resume}}


def _vac(vid, archived=False):
    return {"id": vid, "archived": archived, "alternate_url": f"https://hh.ru/vacancy/{vid}"}


def _op(api, states=None):
    op = Operation()
    op.tool = SimpleNamespace(api_client=api)  # api_client — свойство поверх tool
    op.reapply_experience = None
    if states is not None:
        op.reapply_states = states
    return op


NEGS = [
    _neg("1", "discard"),
    _neg("2", "response"),
    _neg("3", "invitation"),
    _neg("4", "discard"),
    _neg("4", "response", resume="new"),  # уже откликались новым резюме
    _neg("5", "response"),
]
VACS = {v: _vac(v) for v in "12345"} | {"5": _vac("5", archived=True)}


def _ids(op, resume_id="new"):
    return [v["id"] for v in op._get_rejected_vacancies(resume_id)]


def test_default_is_discard_only():
    api = FakeApi(NEGS, VACS)
    op = _op(api)
    assert _ids(op) == ["1"]


def test_discard_and_response():
    api = FakeApi(NEGS, VACS)
    op = _op(api, ["discard", "response"])
    # 3 — приглашение, 4 — уже есть отклик новым резюме, 5 — архив
    assert _ids(op) == ["1", "2"]


def test_negotiations_requested_with_all_statuses():
    api = FakeApi(NEGS, VACS)
    op = _op(api, ["discard", "response"])
    _ids(op)
    assert api.neg_params[0].get("status") == "all"
