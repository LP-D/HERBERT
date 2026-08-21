from app.cli import doctor
from conftest import REPO_ROOT


def test_doctor_runs_without_raising_against_real_environment():
    results = doctor.run_all_checks(REPO_ROOT)
    assert len(results) > 0
    for r in results:
        assert r.status in {"VERIFIED", "UNAVAILABLE", "FAILED"}


def test_doctor_degrades_gracefully_without_docker_or_claude(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda _binary: None)

    docker_result = doctor.check_docker()
    claude_result = doctor.check_claude_code()
    git_result = doctor.check_git()

    assert docker_result.status == "UNAVAILABLE"
    assert claude_result.status == "UNAVAILABLE"
    assert git_result.status == "UNAVAILABLE"


def test_doctor_python_and_sqlite_always_verified():
    assert doctor.check_python().status == "VERIFIED"
    assert doctor.check_sqlite().status == "VERIFIED"


def test_doctor_repo_structure_reports_missing_paths(tmp_path):
    results = doctor.check_repo_structure(tmp_path)
    assert all(r.status == "FAILED" for r in results)
