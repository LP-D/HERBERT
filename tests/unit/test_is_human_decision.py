"""V0.5 point 3 : is_human_decision est garantie par le schéma, peuplée par
l'appelant réel de transition_task() — ces tests vérifient chaque site
d'appel production réel (pas seulement app/state_machine/service.py en
isolation), sur le même scénario AUTO_ROLLBACK que
tests/unit/test_task_promote.py."""
import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import insert_project, insert_task
from app.models import Project, Task
from app.state_machine.service import transition_task
from app.state_machine.states import TaskState


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path, name="cible-human-decision"):
    project_dir = tmp_path / name
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")
    (project_dir / "calc.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (project_dir / "test_calc.py").write_text(
        "from calc import value\n\n\ndef test_value():\n    assert value() == 1\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial")
    return project_dir


def _make_task(isolated_repo_root, project_dir, name="cible-human-decision"):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name=name, path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche pour test is_human_decision")
    insert_task(conn, task)
    conn.close()
    return task, project


def _transitions_for(isolated_repo_root, task_id):
    conn = get_connection(_db_path(isolated_repo_root))
    rows = conn.execute(
        "SELECT from_state, to_state, is_human_decision FROM state_transitions "
        "WHERE task_id = ? ORDER BY created_at",
        (task_id,),
    ).fetchall()
    conn.close()
    return [(r["from_state"], r["to_state"], bool(r["is_human_decision"])) for r in rows]


def test_transition_task_direct_call_respects_explicit_flag(db_conn):
    project = Project(name="p-direct", path="C:/p-direct")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="t")
    insert_task(db_conn, task)

    _, _, transition_true = transition_task(db_conn, task.id, TaskState.EXECUTING, is_human_decision=True)
    assert transition_true.is_human_decision is True

    row = db_conn.execute(
        "SELECT is_human_decision FROM state_transitions WHERE id = ?", (transition_true.id,)
    ).fetchone()
    assert row["is_human_decision"] == 1


def test_transition_task_default_is_false(db_conn):
    """Défaut choisi selon l'usage réel de service.py (voir
    migrations/0007_is_human_decision.sql) : False si non précisé."""
    project = Project(name="p-defaut", path="C:/p-defaut")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="t")
    insert_task(db_conn, task)

    _, _, transition = transition_task(db_conn, task.id, TaskState.EXECUTING)
    assert transition.is_human_decision is False


def test_cmd_task_status_to_marks_human_decision_true(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_task_status(
        argparse.Namespace(id=task.id, to="EXECUTING", reason="décision humaine réelle", include_archived=False)
    )
    assert exit_code == 0

    transitions = _transitions_for(isolated_repo_root, task.id)
    assert transitions == [("RECEIVED", "EXECUTING", True)]


def test_advance_after_test_result_marks_human_decision_false(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0

    transitions = _transitions_for(isolated_repo_root, task.id)
    # RECEIVED->EXECUTING->TESTING->DONE, toutes automatiques
    assert transitions == [
        ("RECEIVED", "EXECUTING", False),
        ("EXECUTING", "TESTING", False),
        ("TESTING", "DONE", False),
    ]


def test_promote_and_auto_rollback_mark_human_decision_false(isolated_repo_root, tmp_path, monkeypatch):
    """Reproduit le scénario AUTO_ROLLBACK réel de test_task_promote.py :
    merge -> PROMOTED, health check échoué -> ROLLED_BACK, les DEUX
    automatiques, jamais is_human_decision=True."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    candidate_branch = f"candidate/{task.id}"

    (project_dir / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (project_dir / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "ajout de feature.py")
    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0

    _run_git(project_dir, "checkout", "master")
    (project_dir / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "casse calc.py sur stable")
    _run_git(project_dir, "checkout", candidate_branch)

    assert cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id)) == 1  # AUTO_ROLLBACK

    transitions = _transitions_for(isolated_repo_root, task.id)
    promote_related = [t for t in transitions if t[1] in ("PROMOTED", "ROLLED_BACK")]
    assert ("DONE", "PROMOTED", False) in promote_related
    assert ("PROMOTED", "ROLLED_BACK", False) in promote_related
    # aucune transition de ce scénario n'est humaine
    assert all(is_human is False for _f, _t, is_human in transitions)


def test_promote_health_check_passed_marks_human_decision_false(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    (project_dir / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (project_dir / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "ajout de feature.py")
    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0

    assert cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id)) == 0

    transitions = _transitions_for(isolated_repo_root, task.id)
    assert ("DONE", "PROMOTED", False) in transitions
    assert all(is_human is False for _f, _t, is_human in transitions)
