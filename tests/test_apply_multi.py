"""apply_multi.sh: каждая волна — сначала основное резюме на новые вакансии, переотклики с потолком."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(tmp_path, wave, limit_hit_on=None):
    script = (ROOT / "apply_multi.sh").read_text().replace("cd /opt/hh-applicant-tool", f"cd {tmp_path}")
    (tmp_path / "apply_multi.sh").write_text(script)
    (tmp_path / "apply.env").write_text('HH_RESUME_ID=main\nHH_EXTRA_RESUME_IDS="v1 v2 v3 v4 v5 v6 v7 v8 v9"\n')
    stop = f'[ "${{RESUME_ID:-main}}" = "{limit_hit_on}" ] && echo "Лимит откликов hh.ru исчерпан"' if limit_hit_on else ":"
    (tmp_path / "apply3.sh").write_text(f'echo "CALL ${{RESUME_ID:-main}} $*" >> {tmp_path}/calls.log\n{stop}\n')
    subprocess.run(["bash", str(tmp_path / "apply_multi.sh"), str(wave)], check=True, capture_output=True)
    return (tmp_path / "calls.log").read_text().splitlines()


def test_every_wave_starts_with_main_resume_on_new_vacancies(tmp_path):
    for wave in (0, 1, 2):
        d = tmp_path / str(wave)
        d.mkdir()
        calls = _run(d, wave)
        assert calls[0] == "CALL main "  # основное — без флагов переотклика: только новые вакансии
        assert len(calls) == 4


def test_reapplies_are_capped_to_leave_room_for_new_vacancies(tmp_path):
    calls = _run(tmp_path, 1)
    for c in calls[1:]:
        assert "--reapply-states response" in c
        assert c.endswith("--max-responses 13")
    assert 9 * 13 <= 120  # 9 вариантов в сутки — остальное из лимита 200 свободно для новых


def test_limit_on_main_stops_chain(tmp_path):
    calls = _run(tmp_path, 2, limit_hit_on="main")
    assert calls == ["CALL main "]
