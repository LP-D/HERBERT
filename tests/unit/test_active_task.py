import pytest

from app.active_task import (
    activate_task,
    deactivate_task,
    deactivate_task_if_active,
    get_active_task_id,
    resolve_active_task_id_for_hook,
    resolve_project_by_cwd,
)
from app.database.repository import insert_project, insert_task
from app.models import Project, Task
from app.state_machine.states import TaskState


def _make_project_and_task(conn, project_name="projet-actif", path="C:/projet-actif", description="tâche"):
    project = Project(name=project_name, path=path)
    insert_project(conn, project)
    task = Task(project_id=project.id, description=description)
    insert_task(conn, task)
    return project, task


def test_resolve_project_by_cwd_matches_resolved_path(db_conn, tmp_path):
    real_dir = tmp_path / "mon-projet"
    real_dir.mkdir()
    project, _task = _make_project_and_task(db_conn, path=str(real_dir))

    # chemin avec un slash final différent + casse différente : doit quand
    # même matcher, comparaison canonique pas textuelle brute.
    found = resolve_project_by_cwd(db_conn, str(real_dir) + "/")
    assert found is not None
    assert found.id == project.id


def test_resolve_project_by_cwd_no_match_returns_none(db_conn, tmp_path):
    _make_project_and_task(db_conn, path=str(tmp_path / "mon-projet"))
    unrelated = tmp_path / "autre-dossier-jamais-enregistre"
    unrelated.mkdir()

    assert resolve_project_by_cwd(db_conn, str(unrelated)) is None


def test_get_active_task_id_none_by_default(db_conn):
    project, _task = _make_project_and_task(db_conn)
    assert get_active_task_id(db_conn, project.id) is None


def test_activate_task_sets_active_task_id(db_conn, tmp_path):
    project, task = _make_project_and_task(db_conn)

    result = activate_task(db_conn, task.id, tmp_path / "logs")

    assert result.project.id == project.id
    assert result.task.id == task.id
    assert result.previous_task_id is None
    assert get_active_task_id(db_conn, project.id) == task.id


def test_activate_task_overwrites_previous_and_reports_it(db_conn, tmp_path):
    project = Project(name="projet-double", path="C:/projet-double")
    insert_project(db_conn, project)
    task_a = Task(project_id=project.id, description="tâche A")
    task_b = Task(project_id=project.id, description="tâche B")
    insert_task(db_conn, task_a)
    insert_task(db_conn, task_b)

    activate_task(db_conn, task_a.id, tmp_path / "logs")
    result = activate_task(db_conn, task_b.id, tmp_path / "logs")

    assert result.previous_task_id == task_a.id
    assert get_active_task_id(db_conn, project.id) == task_b.id


def test_activate_task_unknown_task_raises(db_conn, tmp_path):
    with pytest.raises(ValueError, match="introuvable"):
        activate_task(db_conn, "inexistant", tmp_path / "logs")


def test_activate_task_refuses_terminal_states(db_conn, tmp_path):
    """Garde d'état : activer une tâche DONE/PROMOTED/ROLLED_BACK est refusé
    avec un message clair, jamais silencieusement accepté."""
    project = Project(name="projet-terminal", path="C:/projet-terminal")
    insert_project(db_conn, project)
    for state in (TaskState.DONE, TaskState.PROMOTED, TaskState.ROLLED_BACK):
        task = Task(project_id=project.id, description=f"tâche {state.value}", status=state)
        insert_task(db_conn, task)

        with pytest.raises(ValueError, match=state.value):
            activate_task(db_conn, task.id, tmp_path / "logs")

        assert get_active_task_id(db_conn, project.id) is None  # jamais activée


def test_activate_task_allows_non_terminal_states(db_conn, tmp_path):
    """Non-régression : les états de travail actif restent activables."""
    project = Project(name="projet-actif-2", path="C:/projet-actif-2")
    insert_project(db_conn, project)
    for state in (TaskState.RECEIVED, TaskState.EXECUTING, TaskState.TESTING, TaskState.FAILED, TaskState.BLOCKED, TaskState.HUMAN_REQUIRED):
        task = Task(project_id=project.id, description=f"tâche {state.value}", status=state)
        insert_task(db_conn, task)
        activate_task(db_conn, task.id, tmp_path / "logs")
        assert get_active_task_id(db_conn, project.id) == task.id


def test_deactivate_task_clears_when_matching(db_conn, tmp_path):
    project, task = _make_project_and_task(db_conn)
    activate_task(db_conn, task.id, tmp_path / "logs")

    cleared = deactivate_task(db_conn, task.id, tmp_path / "logs")

    assert cleared is True
    assert get_active_task_id(db_conn, project.id) is None


def test_deactivate_task_no_op_when_not_the_active_one(db_conn, tmp_path):
    project = Project(name="projet-deact", path="C:/projet-deact")
    insert_project(db_conn, project)
    task_a = Task(project_id=project.id, description="tâche A")
    task_b = Task(project_id=project.id, description="tâche B")
    insert_task(db_conn, task_a)
    insert_task(db_conn, task_b)
    activate_task(db_conn, task_a.id, tmp_path / "logs")

    # task_b n'est pas la tâche active : ne doit RIEN désactiver, jamais un
    # no-op silencieux fondé sur une mauvaise supposition.
    cleared = deactivate_task(db_conn, task_b.id, tmp_path / "logs")

    assert cleared is False
    assert get_active_task_id(db_conn, project.id) == task_a.id  # task_a reste active


def test_deactivate_task_if_active_never_raises_on_unknown_task(db_conn, tmp_path):
    deactivate_task_if_active(db_conn, "inexistant", tmp_path / "logs", trigger="manual")  # ne doit pas lever


# --- traçabilité JSONL (V0.5, correctif) --------------------------------

def test_activate_and_deactivate_write_jsonl_events(db_conn, tmp_path):
    """La journalisation JSONL de chaque activation/désactivation n'est pas
    facultative : ceci prouve une entrée RÉELLE dans logs/*.jsonl, pas
    seulement l'effet en base."""
    import json

    logs_dir = tmp_path / "logs"
    project, task = _make_project_and_task(db_conn)

    activate_task(db_conn, task.id, logs_dir)
    deactivate_task(db_conn, task.id, logs_dir, trigger="manual")

    log_files = list(logs_dir.glob("herbert-*.jsonl"))
    assert log_files, "aucun fichier JSONL écrit"
    events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    active_task_events = [e for e in events if e["component"] == "active_task"]

    assert len(active_task_events) == 2
    assert active_task_events[0]["event"] == "tâche activée"
    assert active_task_events[0]["task_id"] == task.id
    assert active_task_events[0]["details"]["trigger"] == "manual"
    assert active_task_events[1]["event"] == "tâche désactivée"
    assert active_task_events[1]["details"]["trigger"] == "manual"


def test_deactivate_task_if_active_writes_jsonl_with_given_trigger(db_conn, tmp_path):
    import json

    logs_dir = tmp_path / "logs"
    project, task = _make_project_and_task(db_conn)
    activate_task(db_conn, task.id, logs_dir)

    deactivate_task_if_active(db_conn, task.id, logs_dir, trigger="auto_rollback")

    log_files = list(logs_dir.glob("herbert-*.jsonl"))
    events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
    deactivation = next(e for e in events if e["component"] == "active_task" and e["event"] == "tâche désactivée")
    assert deactivation["details"]["trigger"] == "auto_rollback"


def test_deactivate_task_no_op_writes_no_jsonl_event(db_conn, tmp_path):
    """Un no-op (tâche pas active) ne doit pas produire une fausse entrée de
    désactivation — rien ne s'est réellement passé."""
    import json

    logs_dir = tmp_path / "logs"
    project = Project(name="projet-noop", path="C:/projet-noop")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="jamais activée")
    insert_task(db_conn, task)

    deactivate_task(db_conn, task.id, logs_dir, trigger="manual")

    log_files = list(logs_dir.glob("herbert-*.jsonl"))
    if log_files:
        events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
        assert not [e for e in events if e["component"] == "active_task"]


def test_resolve_active_task_id_for_hook_returns_none_without_cwd(db_conn):
    assert resolve_active_task_id_for_hook(db_conn, None) is None
    assert resolve_active_task_id_for_hook(db_conn, "") is None


def test_resolve_active_task_id_for_hook_returns_none_for_unregistered_cwd(db_conn, tmp_path):
    unrelated = tmp_path / "jamais-enregistre"
    unrelated.mkdir()
    assert resolve_active_task_id_for_hook(db_conn, str(unrelated)) is None


def test_resolve_active_task_id_for_hook_returns_active_task(db_conn, tmp_path):
    real_dir = tmp_path / "projet-hook"
    real_dir.mkdir()
    project, task = _make_project_and_task(db_conn, path=str(real_dir))
    activate_task(db_conn, task.id, tmp_path / "logs")

    assert resolve_active_task_id_for_hook(db_conn, str(real_dir)) == task.id


def test_resolve_active_task_id_for_hook_returns_none_when_no_active_task(db_conn, tmp_path):
    real_dir = tmp_path / "projet-sans-tache-active"
    real_dir.mkdir()
    _make_project_and_task(db_conn, path=str(real_dir))

    assert resolve_active_task_id_for_hook(db_conn, str(real_dir)) is None


def test_resolve_active_task_id_for_hook_never_raises_on_garbage_cwd(db_conn):
    # Ne doit jamais lever, quel que soit le cwd fourni — contrat du hook.
    assert resolve_active_task_id_for_hook(db_conn, "\x00invalide\x00") is None


def test_isolation_par_projet_deux_sessions_paralleles(db_conn, tmp_path):
    """Deux projets différents, chacun avec sa propre tâche active : aucune
    contamination croisée — c'est la garantie centrale du point 1."""
    dir_a = tmp_path / "projet-a"
    dir_b = tmp_path / "projet-b"
    dir_a.mkdir()
    dir_b.mkdir()

    project_a, task_a = _make_project_and_task(db_conn, project_name="projet-a", path=str(dir_a))
    project_b, task_b = _make_project_and_task(db_conn, project_name="projet-b", path=str(dir_b))

    activate_task(db_conn, task_a.id, tmp_path / "logs")
    activate_task(db_conn, task_b.id, tmp_path / "logs")

    assert resolve_active_task_id_for_hook(db_conn, str(dir_a)) == task_a.id
    assert resolve_active_task_id_for_hook(db_conn, str(dir_b)) == task_b.id

    # désactiver A ne doit pas toucher B
    deactivate_task(db_conn, task_a.id, tmp_path / "logs")
    assert resolve_active_task_id_for_hook(db_conn, str(dir_a)) is None
    assert resolve_active_task_id_for_hook(db_conn, str(dir_b)) == task_b.id


# --- CLI : engine task activate / deactivate ---------------------------

import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path, name="cible-activation"):
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


def _make_task(isolated_repo_root, project_dir, name="cible-activation"):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name=name, path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche pour test activation")
    insert_task(conn, task)
    conn.close()
    return task, project


def test_cmd_task_activate_and_deactivate_via_cli(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_task_activate(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) == task.id
    conn.close()

    exit_code = cli_main.cmd_task_deactivate(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) is None
    conn.close()


def test_cmd_task_activate_unknown_task_fails_cleanly(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_task_activate(argparse.Namespace(task_id="inexistant"))
    assert exit_code == 1


def test_cmd_task_deactivate_wrong_task_fails_cleanly(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)
    # jamais activée : deactivate doit signaler l'échec, pas un faux succès
    exit_code = cli_main.cmd_task_deactivate(argparse.Namespace(task_id=task.id))
    assert exit_code == 1


# --- Désactivation automatique aux 3 points terminaux réels ------------

def test_auto_deactivate_on_successful_promotion_health_check(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    conn = get_connection(_db_path(isolated_repo_root))
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

    (project_dir / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (project_dir / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "ajout de feature.py")

    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0

    # active AVANT le check : la tâche doit rester active pendant un simple
    # `task test` réussi (le travail continue généralement vers `promote`).
    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) == task.id
    conn.close()

    assert cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id)) == 0

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) is None  # désactivée automatiquement
    conn.close()


def test_auto_deactivate_on_auto_rollback(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    candidate_branch = f"candidate/{task.id}"
    conn = get_connection(_db_path(isolated_repo_root))
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

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

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) is None  # désactivée automatiquement
    conn.close()


def test_no_auto_deactivate_on_plain_failed_task_test(isolated_repo_root, tmp_path, monkeypatch):
    """Un `task test` en échec ne désactive PAS : le travail continue sur
    la même tâche active."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)
    conn = get_connection(_db_path(isolated_repo_root))
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

    (project_dir / "test_calc.py").write_text(
        "from calc import value\n\n\ndef test_value():\n    assert value() == 999\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "test cassé intentionnellement")

    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 1

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) == task.id  # toujours active
    conn.close()


def test_auto_deactivate_on_manual_rollback(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    conn = get_connection(_db_path(isolated_repo_root))
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

    (project_dir / "change.txt").write_text("x", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "changement candidat")

    monkeypatch.setattr("builtins.input", lambda prompt="": "OUI")
    assert cli_main.cmd_task_rollback(argparse.Namespace(task_id=task.id)) == 0

    conn = get_connection(_db_path(isolated_repo_root))
    assert get_active_task_id(conn, project.id) is None  # désactivée automatiquement
    conn.close()
