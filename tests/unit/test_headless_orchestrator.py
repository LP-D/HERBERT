import argparse
import subprocess

import pytest

from app import headless_orchestrator
from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import (
    get_task,
    insert_project,
    insert_task,
    list_headless_iterations_for_task,
)
from app.headless_orchestrator import HeadlessOrchestrationError, run_headless_task
from app.claude_headless import HeadlessInvocationResult
from app.models import Project, Task
from app.models.enums import HeadlessInvocationStatus
from app.state_machine.states import TaskState

MODEL = "claude-sonnet-5"


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _db_path(isolated_repo_root):
    return isolated_repo_root / "data" / "herbert.db"


def _make_target_project(tmp_path, name="target_project", initial_value=2):
    """calc.py retourne délibérément 2 (test_calc.py attend 1) : la tâche
    démarre "cassée", comme une vraie tâche à corriger — chaque test
    contrôle explicitement si/quand le fake invoke_claude_headless
    "corrige" le fichier, pour un signal honnête via pytest réel."""
    project_dir = tmp_path / name
    project_dir.mkdir()
    _run_git(project_dir, "init")
    _run_git(project_dir, "config", "user.email", "test@herbert.local")
    _run_git(project_dir, "config", "user.name", "HERBERT Test")
    (project_dir / "calc.py").write_text(f"def value():\n    return {initial_value}\n", encoding="utf-8")
    (project_dir / "test_calc.py").write_text(
        "from calc import value\n\n\ndef test_value():\n    assert value() == 1\n", encoding="utf-8"
    )
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "commit initial (calc cassé)")
    return project_dir


def _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch, name="cible-headless"):
    """Reproduit exactement le flux réel : `engine task branch` (réutilisé
    tel quel) crée candidate/<task_id> + l'entrée audit_log dont
    run_headless_task a besoin (get_latest_audit_log_details)."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name=name, path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="corriger calc.value()")
    insert_task(conn, task)
    conn.close()

    exit_code = cli_main.cmd_task_branch(argparse.Namespace(task_id=task.id))
    assert exit_code is None or exit_code == 0

    return task, project


def _fix_calc(project_dir) -> None:
    """Corrige ET committe — HERBERT classifie le diff COMMITÉ sur la
    branche candidate (git diff --numstat base_commit HEAD), jamais les
    changements de working tree seuls (voir build_context_prompt, qui
    instruit explicitement Claude Code de committer)."""
    (project_dir / "calc.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "fix: calc.value() retourne 1")


def _bloat_calc(project_dir) -> None:
    """Modifie ET committe calc.py pour dépasser MAX_AUTO_DIFF_LINES (20)
    tout en laissant le test passer — déclenche MANUAL_REQUIRED côté
    classify_push."""
    padding = "\n".join(f"    # ligne de remplissage {i}" for i in range(25))
    (project_dir / "calc.py").write_text(f"def value():\n{padding}\n    return 1\n", encoding="utf-8")
    _run_git(project_dir, "add", "-A")
    _run_git(project_dir, "commit", "-m", "fix: calc.value() (diff volumineux)")


@pytest.fixture
def logs_dir(isolated_repo_root):
    d = isolated_repo_root / "logs"
    d.mkdir(exist_ok=True)
    return d


def _success_result(session_id="sess-1", num_turns=3) -> HeadlessInvocationResult:
    return HeadlessInvocationResult(
        invocation_status=HeadlessInvocationStatus.VERIFIED,
        is_error=False,
        result_text="terminé",
        session_id=session_id,
        num_turns=num_turns,
        raw_stdout='{"type": "result", "is_error": false}',
    )


def test_success_on_first_iteration_auto(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        _fix_calc(project_dir)  # corrige ET committe dès la 1re invocation
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL)

    assert result_task.status == TaskState.DONE
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 1
    assert iterations[0].tests_passed is True
    assert iterations[0].invocation_status == HeadlessInvocationStatus.VERIFIED
    conn.close()


def test_failure_then_success_second_iteration_includes_previous_failure_context(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)  # cassé au départ
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    calls = []

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        calls.append(prompt)
        if len(calls) == 2:
            _fix_calc(project_dir)
        return _success_result(session_id=f"sess-{len(calls)}")

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL)

    assert result_task.status == TaskState.DONE
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 2
    assert iterations[0].tests_passed is False
    assert iterations[1].tests_passed is True

    # Le 2e prompt doit contenir le contexte d'échec de la 1re tentative.
    assert "test_value" in calls[1]
    assert "échec" in calls[1].lower() or "échoué" in calls[1].lower()
    conn.close()


def test_three_consecutive_failures_blocked_never_a_fourth_attempt(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)  # jamais corrigé
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        return _success_result()  # invocation "réussie" mais ne corrige jamais calc.py

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, max_iterations=3)

    assert result_task.status == TaskState.BLOCKED
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 3
    assert all(it.tests_passed is False for it in iterations)

    row = conn.execute(
        "SELECT from_state, to_state, reason FROM state_transitions WHERE task_id = ? AND to_state = 'BLOCKED'",
        (task.id,),
    ).fetchone()
    assert row["from_state"] == "FAILED"
    assert "3" in row["reason"]

    # Relancer l'orchestration sur cette même tâche doit être refusé, pas
    # silencieusement déclencher une 4e itération.
    with pytest.raises(HeadlessOrchestrationError):
        run_headless_task(conn, task.id, logs_dir, model=MODEL, max_iterations=3)
    assert len(list_headless_iterations_for_task(conn, task.id)) == 3
    conn.close()


def test_success_manual_required_classification_stops_at_human_required(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        _bloat_calc(project_dir)  # tests passent, mais diff > 20 lignes -> MANUAL_REQUIRED
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL)

    assert result_task.status == TaskState.HUMAN_REQUIRED

    row = conn.execute(
        "SELECT from_state, to_state, reason FROM state_transitions WHERE task_id = ? AND to_state = 'HUMAN_REQUIRED'",
        (task.id,),
    ).fetchone()
    assert row["from_state"] == "DONE"
    assert "MANUAL_REQUIRED" in row["reason"]

    # Jamais de tentative de push automatique.
    log_text = "".join(f.read_text(encoding="utf-8") for f in logs_dir.glob("*.jsonl"))
    assert "push_to_origin" not in log_text
    conn.close()


def test_timeout_iteration_has_distinct_reason_and_consumes_budget(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.TIMED_OUT,
            error_detail=f"invocation headless expirée après {timeout_seconds}s",
        )

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, max_iterations=3, timeout_seconds=42)

    assert result_task.status == TaskState.BLOCKED
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 3
    assert all(it.invocation_status == HeadlessInvocationStatus.TIMED_OUT for it in iterations)

    row = conn.execute(
        "SELECT reason FROM state_transitions WHERE task_id = ? AND to_state = 'FAILED' ORDER BY created_at LIMIT 1",
        (task.id,),
    ).fetchone()
    assert "expirée" in row["reason"]
    assert "42s" in row["reason"]
    conn.close()


def test_invocation_crash_has_distinct_reason_and_consumes_budget(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.INVOCATION_FAILED,
            is_error=True,
            error_detail="Failed to authenticate: OAuth session expired and could not be refreshed",
            raw_stdout='{"type": "result", "is_error": true, "result": "Failed to authenticate"}',
        )

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, max_iterations=3)

    assert result_task.status == TaskState.BLOCKED
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 3
    assert all(it.invocation_status == HeadlessInvocationStatus.INVOCATION_FAILED for it in iterations)

    row = conn.execute(
        "SELECT reason FROM state_transitions WHERE task_id = ? AND to_state = 'FAILED' ORDER BY created_at LIMIT 1",
        (task.id,),
    ).fetchone()
    assert "échec d'invocation" in row["reason"]
    conn.close()


def test_run_headless_task_never_calls_push_to_origin(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    """Vérifie l'invariant par instrumentation directe, pas seulement par
    absence dans les logs : push_to_origin lève si jamais appelé."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit JAMAIS être appelé par headless_orchestrator")

    def fake_invoke(prompt, cwd, model, timeout_seconds, permission_mode="bypassPermissions"):
        _fix_calc(project_dir)
        return _success_result()

    monkeypatch.setattr("app.git_wrapper.push_to_origin", fail_if_called)
    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL)
    assert result_task.status == TaskState.DONE
    conn.close()
