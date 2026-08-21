import argparse

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import (
    insert_change_proof,
    insert_project,
    insert_state_transition,
    insert_task,
)
from app.models import ChangeProof, Project, StateTransition, Task
from app.report_builder import build_report_html, write_report
from app.state_machine.states import TaskState


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_task_with_change_proof(isolated_repo_root, regressions=None):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")

    project = Project(name="projet-rapport", path="C:/projet-rapport")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="Implémenter la fonctionnalité de rapport")
    insert_task(conn, task)

    insert_state_transition(
        conn,
        StateTransition(task_id=task.id, from_state=TaskState.RECEIVED, to_state=TaskState.EXECUTING, allowed=True),
    )
    insert_state_transition(
        conn,
        StateTransition(task_id=task.id, from_state=TaskState.EXECUTING, to_state=TaskState.DONE, allowed=False, reason="test illégal"),
    )

    proof = ChangeProof(
        task_id=task.id,
        files_changed=["app/report_builder.py", "tests/unit/test_report.py"],
        commands_executed=[],
        tests_passed=5,
        tests_failed=1,
        regressions=regressions or [],
        status=TaskState.FAILED,
    )
    insert_change_proof(conn, proof)
    conn.close()

    return task, project, proof


def test_report_html_contains_expected_fields(isolated_repo_root):
    task, project, proof = _make_task_with_change_proof(isolated_repo_root, regressions=["test_sample::test_beta"])

    conn = get_connection(_db_path(isolated_repo_root))
    html_content = build_report_html(conn, task, isolated_repo_root / "logs")
    conn.close()

    assert task.description in html_content
    assert project.name in html_content
    assert "EXECUTING" in html_content or task.status.value in html_content
    assert "app/report_builder.py" in html_content
    assert "5" in html_content  # tests_passed
    assert "test_sample::test_beta" in html_content  # régression signalée
    assert "commands_executed" in html_content
    assert "vide" in html_content.lower()  # limite documentée, pas cachée
    assert "RECEIVED" in html_content and "EXECUTING" in html_content
    assert "refusée" in html_content  # transition illégale signalée


def test_report_html_without_change_proof_says_so_clearly(isolated_repo_root):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="projet-sans-proof", path="C:/sans-proof")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="Tâche sans ChangeProof")
    insert_task(conn, task)
    conn.close()

    conn = get_connection(_db_path(isolated_repo_root))
    html_content = build_report_html(conn, task, isolated_repo_root / "logs")
    conn.close()

    assert "Aucun ChangeProof" in html_content


def test_write_report_creates_file_on_disk(isolated_repo_root):
    task, _project, _proof = _make_task_with_change_proof(isolated_repo_root)

    conn = get_connection(_db_path(isolated_repo_root))
    output_path = write_report(conn, task, isolated_repo_root / "logs", isolated_repo_root / "reports")
    conn.close()

    assert output_path.exists()
    assert output_path.name == f"{task.id}.html"
    content = output_path.read_text(encoding="utf-8")
    assert "<!doctype html>" in content.lower()
    assert task.description in content


def test_cmd_report_via_cli(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    task, _project, _proof = _make_task_with_change_proof(isolated_repo_root)

    exit_code = cli_main.cmd_report(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    expected_path = isolated_repo_root / "reports" / f"{task.id}.html"
    assert expected_path.exists()


def test_cmd_report_unknown_task_fails_cleanly(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_report(argparse.Namespace(task_id="inexistant"))
    assert exit_code == 1


def test_report_html_escapes_dangerous_content(isolated_repo_root):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="projet-xss", path="C:/xss")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="<script>alert('x')</script>")
    insert_task(conn, task)
    conn.close()

    conn = get_connection(_db_path(isolated_repo_root))
    html_content = build_report_html(conn, task, isolated_repo_root / "logs")
    conn.close()

    assert "<script>alert" not in html_content
    assert "&lt;script&gt;" in html_content
