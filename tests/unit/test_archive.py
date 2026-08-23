import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import (
    archive_project,
    archive_task,
    count_unarchived_tasks,
    get_project,
    get_task,
    insert_project,
    insert_task,
    list_projects,
)
from app.active_task import activate_task, get_active_task_id
from app.models import Project, Task


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path, name="cible-archive"):
    project_dir = tmp_path / name
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")
    (project_dir / "f.txt").write_text("x", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial")
    return project_dir


def _make_task(isolated_repo_root, project_dir, name="cible-archive"):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name=name, path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche pour test archive")
    insert_task(conn, task)
    conn.close()
    return task, project


# --- repository : primitives ---------------------------------------------

def test_archive_project_sets_archived_at(db_conn):
    project = Project(name="p-archive", path="C:/p-archive")
    insert_project(db_conn, project)

    archive_project(db_conn, project.id, "2026-08-23T00:00:00+00:00")

    reloaded = get_project(db_conn, project.id)
    assert reloaded.archived_at is not None


def test_archive_task_sets_archived_at(db_conn):
    project = Project(name="p-archive-task", path="C:/p-archive-task")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="t")
    insert_task(db_conn, task)

    archive_task(db_conn, task.id, "2026-08-23T00:00:00+00:00")

    reloaded = get_task(db_conn, task.id)
    assert reloaded.archived_at is not None


def test_get_project_and_get_task_always_return_archived_entities(db_conn):
    """Lookup par id : jamais masqué, archivé ou non — « reste
    interrogeable »."""
    project = Project(name="p-lookup", path="C:/p-lookup")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="t")
    insert_task(db_conn, task)
    archive_project(db_conn, project.id, "2026-08-23T00:00:00+00:00")
    archive_task(db_conn, task.id, "2026-08-23T00:00:00+00:00")

    assert get_project(db_conn, project.id) is not None
    assert get_task(db_conn, task.id) is not None


def test_list_projects_excludes_archived_by_default(db_conn):
    active = Project(name="p-actif", path="C:/p-actif")
    archived = Project(name="p-archive-liste", path="C:/p-archive-liste")
    insert_project(db_conn, active)
    insert_project(db_conn, archived)
    archive_project(db_conn, archived.id, "2026-08-23T00:00:00+00:00")

    default_list = list_projects(db_conn)
    assert active.id in {p.id for p in default_list}
    assert archived.id not in {p.id for p in default_list}

    full_list = list_projects(db_conn, include_archived=True)
    assert archived.id in {p.id for p in full_list}


def test_count_unarchived_tasks(db_conn):
    project = Project(name="p-count", path="C:/p-count")
    insert_project(db_conn, project)
    t1 = Task(project_id=project.id, description="t1")
    t2 = Task(project_id=project.id, description="t2")
    insert_task(db_conn, t1)
    insert_task(db_conn, t2)

    assert count_unarchived_tasks(db_conn, project.id) == 2
    archive_task(db_conn, t1.id, "2026-08-23T00:00:00+00:00")
    assert count_unarchived_tasks(db_conn, project.id) == 1


# --- CLI : engine project archive ----------------------------------------

def test_cmd_project_archive_refuses_with_unarchived_tasks(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=False))
    assert exit_code == 1

    conn = get_connection(_db_path(isolated_repo_root))
    reloaded = get_project(conn, project.id)
    conn.close()
    assert reloaded.archived_at is None  # pas archivé


def test_cmd_project_archive_succeeds_after_archiving_tasks(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_archive(argparse.Namespace(task_id=task.id)) == 0
    exit_code = cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=False))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    reloaded = get_project(conn, project.id)
    conn.close()
    assert reloaded.archived_at is not None


def test_cmd_project_archive_with_force_requires_exact_confirmation(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    monkeypatch.setattr("builtins.input", lambda prompt="": "non merci")
    exit_code = cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=True))
    assert exit_code == 1  # confirmation refusée, rien archivé

    conn = get_connection(_db_path(isolated_repo_root))
    reloaded = get_project(conn, project.id)
    conn.close()
    assert reloaded.archived_at is None


def test_cmd_project_archive_with_force_and_exact_confirmation(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    monkeypatch.setattr("builtins.input", lambda prompt="": "OUI")
    exit_code = cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=True))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    reloaded = get_project(conn, project.id)
    conn.close()
    assert reloaded.archived_at is not None


def test_cmd_project_archive_unknown_project_fails_cleanly(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_project_archive(argparse.Namespace(project_id="inexistant", force=False))
    assert exit_code == 1


# --- CLI : engine task archive --------------------------------------------

def test_cmd_task_archive_deactivates_if_it_was_active(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)

    conn = get_connection(_db_path(isolated_repo_root))
    activate_task(conn, task.id)
    assert get_active_task_id(conn, project.id) == task.id
    conn.close()

    exit_code = cli_main.cmd_task_archive(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    reloaded = get_task(conn, task.id)
    assert reloaded.archived_at is not None
    assert get_active_task_id(conn, project.id) is None  # désactivée automatiquement
    conn.close()


def test_cmd_task_archive_unknown_task_fails_cleanly(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_task_archive(argparse.Namespace(task_id="inexistant"))
    assert exit_code == 1


# --- CLI : --include-archived ---------------------------------------------

def test_cmd_project_list_include_archived_flag(isolated_repo_root, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)
    cli_main.cmd_task_archive(argparse.Namespace(task_id=task.id))
    cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=False))

    capsys.readouterr()
    cli_main.cmd_project_list(argparse.Namespace(include_archived=False))
    out_default = capsys.readouterr().out
    assert project.name not in out_default

    cli_main.cmd_project_list(argparse.Namespace(include_archived=True))
    out_all = capsys.readouterr().out
    assert project.name in out_all
    assert "ARCHIVÉ" in out_all


def test_cmd_task_status_hides_archived_task_by_default(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)
    cli_main.cmd_task_archive(argparse.Namespace(task_id=task.id))

    exit_code = cli_main.cmd_task_status(
        argparse.Namespace(id=task.id, to=None, reason=None, include_archived=False)
    )
    assert exit_code == 1

    exit_code = cli_main.cmd_task_status(
        argparse.Namespace(id=task.id, to=None, reason=None, include_archived=True)
    )
    assert exit_code == 0


def test_cmd_task_status_hides_task_when_project_archived(isolated_repo_root, tmp_path, monkeypatch):
    """Une tâche non archivée elle-même, mais dont le PROJET est archivé,
    doit aussi être invisible par défaut."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, project = _make_task(isolated_repo_root, project_dir)
    cli_main.cmd_task_archive(argparse.Namespace(task_id=task.id))
    cli_main.cmd_project_archive(argparse.Namespace(project_id=project.id, force=False))

    exit_code = cli_main.cmd_task_status(
        argparse.Namespace(id=task.id, to=None, reason=None, include_archived=False)
    )
    assert exit_code == 1


def test_cmd_task_status_unknown_task_unaffected_by_archive_check(isolated_repo_root, monkeypatch):
    """Le pré-check d'archivage ne doit pas masquer le message normal
    « tâche introuvable » pour une tâche qui n'existe simplement pas."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_task_status(
        argparse.Namespace(id="inexistant", to=None, reason=None, include_archived=False)
    )
    assert exit_code == 1
