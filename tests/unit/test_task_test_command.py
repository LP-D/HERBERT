import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import (
    get_latest_change_proof,
    get_latest_test_result_for_task,
    insert_project,
    insert_task,
)
from app.models import Project, Task
from app.models.test_result import TestResultStatus


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_target_project(tmp_path, with_failing_test: bool):
    project_dir = tmp_path / "target_project"
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")

    test_body = "def test_one():\n    assert 1 + 1 == 2\n"
    if with_failing_test:
        test_body += "\n\ndef test_two_fails():\n    assert 1 == 2\n"
    (project_dir / "test_sample.py").write_text(test_body, encoding="utf-8")

    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial du projet cible")

    return project_dir


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def test_task_test_command_captures_real_pytest_run(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path, with_failing_test=True)

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-1", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tester le projet cible")
    insert_task(conn, task)
    conn.close()

    exit_code = cli_main.cmd_task_test(argparse.Namespace(task_id=task.id))
    assert exit_code == 1  # un test échoue exprès

    conn = get_connection(_db_path(isolated_repo_root))
    result = get_latest_test_result_for_task(conn, task.id)
    assert result is not None
    assert result.status == TestResultStatus.VERIFIED_FAIL
    assert result.total == 2
    assert result.passed == 1
    assert result.failed == 1

    proof = get_latest_change_proof(conn, task.id)
    conn.close()
    assert proof is not None
    assert proof.tests_passed == 1
    assert proof.tests_failed == 1


def test_task_test_command_all_pass_returns_zero(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path, with_failing_test=False)

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-2", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tester le projet cible qui passe")
    insert_task(conn, task)
    conn.close()

    exit_code = cli_main.cmd_task_test(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    result = get_latest_test_result_for_task(conn, task.id)
    conn.close()
    assert result.status == TestResultStatus.VERIFIED_PASS


def test_task_test_command_unknown_task_fails_cleanly(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_task_test(argparse.Namespace(task_id="inexistant"))
    assert exit_code == 1
