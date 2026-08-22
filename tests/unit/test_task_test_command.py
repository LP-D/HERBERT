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


def test_task_test_checks_out_its_own_candidate_branch_even_if_another_is_currently_checked_out(
    isolated_repo_root, tmp_path, monkeypatch
):
    """Bug réel trouvé pendant la démonstration bout-en-bout V0.3 : sans
    checkout explicite, `engine task test` teste l'état ACTUEL du dépôt,
    pas forcément la branche candidate de LA tâche demandée. Invisible tant
    qu'une seule candidate existe (cas de tous les autres tests de ce
    fichier) — mais faux dès que deux candidates du même projet coexistent."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path, with_failing_test=False)

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-double-candidate", path=str(project_dir))
    insert_project(conn, project)
    task_a = Task(project_id=project.id, description="candidate A")
    task_b = Task(project_id=project.id, description="candidate B")
    insert_task(conn, task_a)
    insert_task(conn, task_b)
    conn.close()

    # branche A : depuis master
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task_a.id)) == 0
    _run_git(project_dir, "checkout", "master")
    # branche B : depuis la MÊME base master, pas depuis A
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task_b.id)) == 0

    # sur B, casse volontairement un test — A n'a jamais ce changement
    test_file = project_dir / "test_sample.py"
    test_file.write_text(
        test_file.read_text(encoding="utf-8") + "\n\ndef test_only_on_b_and_broken():\n    assert False\n",
        encoding="utf-8",
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "B casse un test exprès")

    # le dépôt est actuellement sur la branche B ; tester A doit basculer
    # dessus tout seul et NE PAS voir le test cassé de B
    exit_code_a = cli_main.cmd_task_test(argparse.Namespace(task_id=task_a.id))
    assert exit_code_a == 0, "tester A ne doit jamais voir le test cassé introduit uniquement sur B"
    assert _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == f"candidate/{task_a.id}"

    conn = get_connection(_db_path(isolated_repo_root))
    result_a = get_latest_test_result_for_task(conn, task_a.id)
    conn.close()
    assert result_a.status == TestResultStatus.VERIFIED_PASS
    assert result_a.total == 1  # uniquement test_one, pas le test cassé de B

    # tester B doit, lui, voir son propre test cassé
    exit_code_b = cli_main.cmd_task_test(argparse.Namespace(task_id=task_b.id))
    assert exit_code_b == 1
    assert _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == f"candidate/{task_b.id}"

    conn = get_connection(_db_path(isolated_repo_root))
    result_b = get_latest_test_result_for_task(conn, task_b.id)
    conn.close()
    assert result_b.status == TestResultStatus.VERIFIED_FAIL
    assert result_b.total == 2
