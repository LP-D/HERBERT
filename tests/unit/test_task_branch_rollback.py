import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import get_latest_audit_log_details, insert_project, insert_task
from app.git_wrapper import get_current_branch, get_head_commit
from app.models import Project, Task


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path):
    project_dir = tmp_path / "target_project"
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")
    (project_dir / "README.md").write_text("initial\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial")
    return project_dir


def _make_task(isolated_repo_root, project_dir):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-branch", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche pour test branch/rollback")
    insert_task(conn, task)
    conn.close()
    return task


def test_task_branch_creates_candidate_branch(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id))
    assert exit_code == 0
    assert get_current_branch(project_dir) == f"candidate/{task.id}"

    conn = get_connection(_db_path(isolated_repo_root))
    info = get_latest_audit_log_details(conn, task.id, "cli.task_branch")
    conn.close()
    assert info is not None
    assert info["branch"] == f"candidate/{task.id}"
    assert info["base_commit"]


def test_task_rollback_restores_previous_state_on_test_repo(isolated_repo_root, tmp_path, monkeypatch):
    """Rollback testé sur un dépôt de test jetable, jamais sur HERBERT lui-même."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task = _make_task(isolated_repo_root, project_dir)

    stable_commit = get_head_commit(project_dir)
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0

    (project_dir / "change.txt").write_text("modification candidate\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "changement candidat")
    assert get_head_commit(project_dir) != stable_commit
    assert (project_dir / "change.txt").exists()

    monkeypatch.setattr("builtins.input", lambda prompt="": "OUI")
    exit_code = cli_main.cmd_task_rollback(argparse.Namespace(task_id=task.id))

    assert exit_code == 0
    assert get_head_commit(project_dir) == stable_commit
    assert not (project_dir / "change.txt").exists()


def test_task_rollback_cancelled_without_exact_confirmation(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task = _make_task(isolated_repo_root, project_dir)

    stable_commit = get_head_commit(project_dir)
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0

    (project_dir / "change.txt").write_text("modification candidate\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "changement candidat")

    monkeypatch.setattr("builtins.input", lambda prompt="": "non merci")
    exit_code = cli_main.cmd_task_rollback(argparse.Namespace(task_id=task.id))

    assert exit_code == 1
    # rien n'a été rollback : le changement candidat doit toujours être là
    assert get_head_commit(project_dir) != stable_commit
    assert (project_dir / "change.txt").exists()


def test_task_rollback_without_branch_fails_cleanly(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_task_rollback(argparse.Namespace(task_id=task.id))
    assert exit_code == 1
