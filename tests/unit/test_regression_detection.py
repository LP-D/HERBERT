import argparse
import subprocess

from app.change_proof_builder import detect_regressions
from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import get_latest_change_proof, insert_project, insert_task
from app.models import Project, Task
from app.models.test_result import TestCaseOutcome, TestCaseResult, TestResult, TestResultStatus


def _tr(status, cases: dict[str, str]) -> TestResult:
    test_cases = [TestCaseResult(name=name, outcome=TestCaseOutcome(outcome)) for name, outcome in cases.items()]
    return TestResult(task_id="whatever", status=status, test_cases=test_cases)


def test_no_previous_result_means_no_regressions():
    current = _tr(TestResultStatus.VERIFIED_FAIL, {"test_a": "FAILED"})
    assert detect_regressions(None, current) == []


def test_test_that_passed_before_and_fails_now_is_a_regression():
    previous = _tr(TestResultStatus.VERIFIED_PASS, {"test_a": "PASSED", "test_b": "PASSED"})
    current = _tr(TestResultStatus.VERIFIED_FAIL, {"test_a": "PASSED", "test_b": "FAILED"})
    assert detect_regressions(previous, current) == ["test_b"]


def test_test_that_was_already_failing_is_not_a_regression():
    previous = _tr(TestResultStatus.VERIFIED_PASS, {"test_a": "PASSED"})
    # test_c n'existait pas dans la baseline VERIFIED_PASS -> pas une régression
    current = _tr(TestResultStatus.VERIFIED_FAIL, {"test_a": "PASSED", "test_c": "FAILED"})
    assert detect_regressions(previous, current) == []


def test_error_outcome_also_counts_as_regression():
    previous = _tr(TestResultStatus.VERIFIED_PASS, {"test_a": "PASSED"})
    current = _tr(TestResultStatus.VERIFIED_FAIL, {"test_a": "ERROR"})
    assert detect_regressions(previous, current) == ["test_a"]


def test_multiple_regressions_sorted():
    previous = _tr(TestResultStatus.VERIFIED_PASS, {"test_z": "PASSED", "test_a": "PASSED"})
    current = _tr(TestResultStatus.VERIFIED_FAIL, {"test_z": "FAILED", "test_a": "ERROR"})
    assert detect_regressions(previous, current) == ["test_a", "test_z"]


# --- scénario bout-en-bout via `engine task test` sur deux runs réels ---

def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _write_tests(project_dir, second_test_fails: bool):
    body = "def test_alpha():\n    assert 1 + 1 == 2\n\n\n"
    body += "def test_beta():\n    assert " + ("1 == 2" if second_test_fails else "2 == 2") + "\n"
    (project_dir / "test_sample.py").write_text(body, encoding="utf-8")


def test_end_to_end_regression_detected_across_two_real_runs(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    project_dir = tmp_path / "regressing_project"
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")

    db_path = isolated_repo_root / "data" / "herbert.db"
    conn = get_connection(db_path)
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="projet-regression", path=str(project_dir))
    insert_project(conn, project)
    task_1 = Task(project_id=project.id, description="run 1 - tout doit passer")
    insert_task(conn, task_1)
    conn.close()

    # run 1 : tout passe -> VERIFIED_PASS, devient la baseline
    _write_tests(project_dir, second_test_fails=False)
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "run 1: tout passe")

    exit_code_1 = cli_main.cmd_task_test(argparse.Namespace(task_id=task_1.id))
    assert exit_code_1 == 0

    conn = get_connection(db_path)
    proof_1 = get_latest_change_proof(conn, task_1.id)
    conn.close()
    assert proof_1.regressions == []

    # run 2 : test_beta se met à échouer -> régression réelle détectée
    conn = get_connection(db_path)
    task_2 = Task(project_id=project.id, description="run 2 - test_beta casse")
    insert_task(conn, task_2)
    conn.close()

    _write_tests(project_dir, second_test_fails=True)
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "run 2: test_beta casse")

    exit_code_2 = cli_main.cmd_task_test(argparse.Namespace(task_id=task_2.id))
    assert exit_code_2 == 1

    conn = get_connection(db_path)
    proof_2 = get_latest_change_proof(conn, task_2.id)
    conn.close()
    assert "test_sample::test_beta" in proof_2.regressions
    assert "test_sample::test_alpha" not in proof_2.regressions
