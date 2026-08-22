import argparse
import subprocess

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import (
    get_latest_promotion_for_task,
    insert_change_proof,
    insert_project,
    insert_task,
)
from app.git_wrapper import get_head_commit
from app.models import ChangeProof, Project, Task
from app.models.promotion import PromotionStatus
from app.state_machine.states import TaskState


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path, name="target_project"):
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


def _make_task(isolated_repo_root, project_dir, name="cible-promote"):
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name=name, path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche pour test promote")
    insert_task(conn, task)
    conn.close()
    return task, project


def test_promote_refuses_without_change_proof(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    exit_code = cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id))
    assert exit_code == 1


def test_promote_refuses_when_change_proof_failed(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    conn = get_connection(_db_path(isolated_repo_root))
    insert_change_proof(
        conn,
        ChangeProof(task_id=task.id, tests_passed=1, tests_failed=1, status=TaskState.FAILED),
    )
    conn.close()

    exit_code = cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id))
    assert exit_code == 1


def test_promote_refuses_when_change_proof_has_regressions(isolated_repo_root, tmp_path, monkeypatch):
    """Cas défensif : même avec status=DONE (ne devrait normalement pas
    coexister avec des régressions dans le flux réel), promote doit quand
    même refuser si regressions n'est pas vide."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    conn = get_connection(_db_path(isolated_repo_root))
    insert_change_proof(
        conn,
        ChangeProof(
            task_id=task.id, tests_passed=2, tests_failed=0, regressions=["test_calc::test_value"], status=TaskState.DONE
        ),
    )
    conn.close()

    exit_code = cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id))
    assert exit_code == 1


def test_promote_merges_for_real_and_file_content_present_on_stable(isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0

    (project_dir / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (project_dir / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "ajout de feature.py sur la candidate")

    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0

    exit_code = cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id))
    assert exit_code == 0

    # vérifie le CONTENU du fichier après merge, pas juste l'absence d'erreur
    assert _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"
    assert (project_dir / "feature.py").exists()
    assert "return 'ok'" in (project_dir / "feature.py").read_text(encoding="utf-8")

    conn = get_connection(_db_path(isolated_repo_root))
    promotion = get_latest_promotion_for_task(conn, task.id)
    stored_task = cli_main.get_task(conn, task.id)
    conn.close()

    assert promotion is not None
    assert promotion.status == PromotionStatus.HEALTH_CHECK_PASSED
    assert promotion.stable_branch == "master"
    assert promotion.candidate_branch == f"candidate/{task.id}"
    assert stored_task.status == TaskState.DONE


def test_promote_health_check_failure_triggers_auto_rollback_and_restores_file(
    isolated_repo_root, tmp_path, monkeypatch
):
    """Le merge lui-même réussit techniquement (pas de conflit textuel :
    candidate n'ajoute qu'un fichier nouveau), mais stable a divergé
    indépendamment entre-temps (calc.py cassé) — combiné, un test casse
    après le merge alors que la candidate seule passait. Vérifie l'AUTO_
    ROLLBACK comme pour un rollback manuel : lecture de fichier réelle +
    git rev-parse, pas une supposition."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    project_dir = _make_target_project(tmp_path)
    task, _project = _make_task(isolated_repo_root, project_dir)

    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id)) == 0
    candidate_branch = f"candidate/{task.id}"

    # candidate : ajoute seulement un fichier nouveau, ne touche pas calc.py
    (project_dir / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (project_dir / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "ajout de feature.py sur la candidate")

    assert cli_main.cmd_task_test(argparse.Namespace(task_id=task.id)) == 0  # candidate seule : tout passe

    # pendant ce temps, stable a divergé indépendamment (simulé ici) : casse calc.py
    _run_git(project_dir, "checkout", "master")
    (project_dir / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "changement indépendant sur stable qui casse calc.py")
    _run_git(project_dir, "checkout", candidate_branch)

    commit_before_promote_attempt = get_head_commit(project_dir)  # sur candidate, pas utilisé directement

    exit_code = cli_main.cmd_task_promote(argparse.Namespace(task_id=task.id))
    assert exit_code == 1  # health check en échec -> AUTO_ROLLBACK, promote lui-même signale un échec

    # --- Vérifications réelles, pas une supposition ---
    assert _run_git(project_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "master"

    # feature.py doit avoir disparu (jamais présent sur stable avant le merge)
    assert not (project_dir / "feature.py").exists()
    # calc.py doit être revenu à l'état de stable (value() == 2, cassé mais inchangé par le revert)
    assert "return 2" in (project_dir / "calc.py").read_text(encoding="utf-8")

    conn = get_connection(_db_path(isolated_repo_root))
    promotion = get_latest_promotion_for_task(conn, task.id)
    stored_task = cli_main.get_task(conn, task.id)
    conn.close()

    assert promotion is not None
    assert promotion.status == PromotionStatus.AUTO_ROLLBACK

    # le rollback est un NOUVEAU commit (revert), pas un reset destructeur :
    # HEAD doit différer du commit de merge ET de commit_before (nouveau commit ajouté au-dessus)
    head_after_rollback = get_head_commit(project_dir)
    assert head_after_rollback != promotion.commit_after
    assert head_after_rollback != promotion.commit_before

    # le contenu de l'arbre doit néanmoins être identique à l'état de stable
    # avant le merge (le revert a exactement annulé ce que le merge a ajouté)
    diff = _run_git(project_dir, "diff", "--stat", promotion.commit_before, "HEAD").stdout.strip()
    assert diff == ""

    # la tâche est passée à ROLLED_BACK (pas restée PROMOTED : le merge a
    # réellement été reverté, PROMOTED serait trompeur ici — pas DONE non
    # plus, le health check a échoué)
    assert stored_task.status == TaskState.ROLLED_BACK


def test_manual_rollback_and_auto_rollback_are_distinguishable_in_logs(isolated_repo_root, tmp_path, monkeypatch):
    """Un rollback manuel (V0.2, `engine task rollback`) et un AUTO_ROLLBACK
    (V0.3, déclenché par un health check en échec) doivent être
    distinguables en base — jamais confondus dans les logs."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    # --- Scénario manuel (V0.2) ---
    manual_project = _make_target_project(tmp_path, name="manual_project")
    manual_task, _ = _make_task(isolated_repo_root, manual_project, name="cible-manuelle")
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=manual_task.id)) == 0
    (manual_project / "change.txt").write_text("x", encoding="utf-8")
    _run_git(manual_project, "add", "-A")
    _run_git(manual_project, "commit", "-m", "changement candidat")
    monkeypatch.setattr("builtins.input", lambda prompt="": "OUI")
    assert cli_main.cmd_task_rollback(argparse.Namespace(task_id=manual_task.id)) == 0

    # --- Scénario AUTO_ROLLBACK (V0.3) ---
    auto_project = _make_target_project(tmp_path, name="auto_project")
    auto_task, _ = _make_task(isolated_repo_root, auto_project, name="cible-auto")
    assert cli_main.cmd_task_branch(argparse.Namespace(task_id=auto_task.id)) == 0
    auto_candidate = f"candidate/{auto_task.id}"
    (auto_project / "feature.py").write_text("def feature():\n    return 'ok'\n", encoding="utf-8")
    (auto_project / "test_feature.py").write_text(
        "from feature import feature\n\n\ndef test_feature():\n    assert feature() == 'ok'\n", encoding="utf-8"
    )
    _run_git(auto_project, "add", "-A")
    _run_git(auto_project, "commit", "-m", "ajout de feature.py sur la candidate")
    assert cli_main.cmd_task_test(argparse.Namespace(task_id=auto_task.id)) == 0
    _run_git(auto_project, "checkout", "master")
    (auto_project / "calc.py").write_text("def value():\n    return 2\n", encoding="utf-8")
    _run_git(auto_project, "add", "-A")
    _run_git(auto_project, "commit", "-m", "changement indépendant sur stable")
    _run_git(auto_project, "checkout", auto_candidate)
    assert cli_main.cmd_task_promote(argparse.Namespace(task_id=auto_task.id)) == 1

    # --- Vérification de la distinction en base ---
    conn = get_connection(_db_path(isolated_repo_root))
    manual_audit = conn.execute(
        "SELECT component FROM audit_log WHERE task_id = ? AND component = 'cli.task_rollback'", (manual_task.id,)
    ).fetchall()
    auto_audit = conn.execute(
        "SELECT component FROM audit_log WHERE task_id = ? AND component = 'cli.task_promote_auto_rollback'",
        (auto_task.id,),
    ).fetchall()
    # un rollback manuel ne doit JAMAIS apparaître comme AUTO_ROLLBACK et vice-versa
    cross_contamination_1 = conn.execute(
        "SELECT component FROM audit_log WHERE task_id = ? AND component = 'cli.task_promote_auto_rollback'",
        (manual_task.id,),
    ).fetchall()
    cross_contamination_2 = conn.execute(
        "SELECT component FROM audit_log WHERE task_id = ? AND component = 'cli.task_rollback'", (auto_task.id,)
    ).fetchall()
    conn.close()

    assert len(manual_audit) == 1
    assert len(auto_audit) == 1
    assert len(cross_contamination_1) == 0
    assert len(cross_contamination_2) == 0
