"""Пауза между откликами (--apply-delay-min/--apply-delay-max) — доработка форка.

Operation, собранный без парсера (как в тестах оригинала), паузу не знает и
не должен из-за этого падать: при слиянии 30.09.2026 именно это ломало тесты
--max-responses.
"""

from __future__ import annotations

from unittest.mock import patch

from hh_applicant_tool.operations.apply_vacancies import Operation


def test_no_delay_attrs_means_no_sleep() -> None:
    op = Operation()
    with patch("hh_applicant_tool.operations.apply_vacancies.time.sleep") as sleep:
        op._human_delay()
    sleep.assert_not_called()


def test_delay_within_range() -> None:
    op = Operation()
    op.apply_delay_min, op.apply_delay_max = 30, 80
    with patch("hh_applicant_tool.operations.apply_vacancies.time.sleep") as sleep:
        for _ in range(50):
            op._human_delay()
    pauses = [c.args[0] for c in sleep.call_args_list]
    assert len(pauses) == 50
    assert all(30 <= p <= 80 for p in pauses)


def test_swapped_bounds_still_work() -> None:
    op = Operation()
    op.apply_delay_min, op.apply_delay_max = 80, 30
    with patch("hh_applicant_tool.operations.apply_vacancies.time.sleep") as sleep:
        op._human_delay()
    assert 30 <= sleep.call_args.args[0] <= 80
