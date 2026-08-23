"""Phase 3 : la régénération automatique du dashboard après une commande
task ne doit JAMAIS faire échouer ni bloquer cette commande — même si le
générateur lui-même explose. Assouplissement documenté du principe "aucun
sous-effet automatique" (voir docs/DASHBOARD.md et le docstring de
_regenerate_dashboard dans app/cli/main.py)."""
import argparse
import json
import subprocess

import pytest

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import get_latest_test_result_for_task, insert_project, insert_task
from app.models import Project, Task


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_target_project(tmp_path):
    project_dir = tmp_path / "target_project"
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")
    (project_dir / "test_sample.py").write_text("def test_one():\n    assert 1 + 1 == 2\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial")
    return project_dir


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def test_task_test_succeeds_even_if_dashboard_regen_raises(isolated_repo_root, tmp_path, monkeypatch):
    """Le point le plus important de la Phase 3 : un générateur de dashboard
    qui explose (bug, SQLite verrouillé, disque plein...) ne doit RIEN
    changer au résultat normal de `engine task test`."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    def _boom(conn, dashboard_dir, logs_dir):
        raise RuntimeError("panne simulée du générateur de dashboard")

    monkeypatch.setattr(cli_main, "build_dashboard", _boom)

    project_dir = _make_target_project(tmp_path)
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-regen", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="vérifie la non-régression du dashboard auto-régén")
    insert_task(conn, task)
    conn.close()

    # Ne doit lever AUCUNE exception, et retourner le résultat NORMAL de
    # `engine task test` (0 = tests passés), pas un code d'erreur lié au
    # dashboard.
    exit_code = cli_main.cmd_task_test(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    conn = get_connection(_db_path(isolated_repo_root))
    result = get_latest_test_result_for_task(conn, task.id)
    conn.close()
    assert result is not None
    assert result.passed == 1

    # dashboard/ n'a PAS été créé (le générateur a explosé avant d'écrire
    # quoi que ce soit) — confirme que l'échec n'a pas non plus été masqué
    # par un succès partiel silencieux.
    assert not (isolated_repo_root / "dashboard").exists()

    # L'échec DOIT être journalisé (JSONL), avec un statut UNAVAILABLE — pas
    # une nouvelle destination de log, pas un échec silencieux non plus.
    log_files = list((isolated_repo_root / "logs").glob("herbert-*.jsonl"))
    assert log_files, "aucun fichier de log JSONL trouvé"
    events = [json.loads(line) for f in log_files for line in f.read_text(encoding="utf-8").splitlines()]
    regen_events = [e for e in events if e["component"] == "cli.dashboard_auto_regen"]
    assert len(regen_events) == 1
    assert regen_events[0]["status"] == "UNAVAILABLE"
    assert "panne simulée" in regen_events[0]["details"]["error"]


def test_task_create_succeeds_even_if_dashboard_regen_raises(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    monkeypatch.setattr(
        cli_main, "build_dashboard",
        lambda conn, d, l: (_ for _ in ()).throw(RuntimeError("panne")),
    )

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-create", path="C:/cible-create")
    insert_project(conn, project)
    conn.close()

    exit_code = cli_main.cmd_task_create(
        argparse.Namespace(project="cible-create", description="tâche créée malgré panne dashboard")
    )
    assert exit_code == 0


def test_dashboard_regen_success_does_not_affect_task_test_result(isolated_repo_root, tmp_path, monkeypatch):
    """Non-régression inverse : quand le dashboard se régénère normalement,
    task test garde exactement le même comportement qu'avant la Phase 3."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="cible-ok", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="régénération dashboard normale")
    insert_task(conn, task)
    conn.close()

    exit_code = cli_main.cmd_task_test(argparse.Namespace(task_id=task.id))
    assert exit_code == 0
    assert (isolated_repo_root / "dashboard" / "index.html").exists()
    assert (isolated_repo_root / "dashboard" / "task" / f"{task.id}.html").exists()


def test_cmd_dashboard_build_and_open(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    conn.close()

    exit_code = cli_main.cmd_dashboard_build(argparse.Namespace())
    assert exit_code == 0
    assert (isolated_repo_root / "dashboard" / "index.html").exists()

    opened = {}
    monkeypatch.setattr(cli_main, "open_dashboard", lambda d: opened.setdefault("dir", d))
    exit_code = cli_main.cmd_dashboard_open(argparse.Namespace())
    assert exit_code == 0
    assert opened["dir"] == isolated_repo_root / "dashboard"


def test_cmd_dashboard_open_fails_cleanly_without_prior_build(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    exit_code = cli_main.cmd_dashboard_open(argparse.Namespace())
    assert exit_code == 1
