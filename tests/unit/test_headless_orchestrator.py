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
# Jamais lancé : invoke_claude_headless est remplacé dans ces tests unitaires
# (le vrai binaire est couvert par tests/integration/test_real_claude_headless.py).
FAKE_EXE = "C:/fake/claude.exe"


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

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        _fix_calc(project_dir)  # corrige ET committe dès la 1re invocation
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

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

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        calls.append(prompt)
        if len(calls) == 2:
            _fix_calc(project_dir)
        return _success_result(session_id=f"sess-{len(calls)}")

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

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

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        return _success_result()  # invocation "réussie" mais ne corrige jamais calc.py

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=3)

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
        run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=3)
    assert len(list_headless_iterations_for_task(conn, task.id)) == 3
    conn.close()


def test_success_manual_required_classification_stops_at_human_required(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        _bloat_calc(project_dir)  # tests passent, mais diff > 20 lignes -> MANUAL_REQUIRED
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

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


def _human_required_reason(conn, task_id):
    row = conn.execute(
        "SELECT from_state, reason FROM state_transitions WHERE task_id = ? AND to_state = 'HUMAN_REQUIRED'",
        (task_id,),
    ).fetchone()
    assert row is not None, "transition DONE -> HUMAN_REQUIRED attendue"
    assert row["from_state"] == "DONE"
    return row["reason"]


def test_agent_gaming_tests_via_conftest_is_human_required(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    """Scénario réel visé par les défauts : l'agent ne corrige pas calc.py
    mais fait passer le test en ajoutant un conftest.py qui patche le module
    — 1 seul fichier, 2 lignes, tests VERIFIED_PASS. Sans chemins protégés
    par projet, ce diff serait classé AUTO."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        (project_dir / "conftest.py").write_text("import calc\ncalc.value = lambda: 1\n", encoding="utf-8")
        _run_git(project_dir, "add", "-A")
        _run_git(project_dir, "commit", "-m", "tests verts (via conftest)")
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert result_task.status == TaskState.HUMAN_REQUIRED
    reason = _human_required_reason(conn, task.id)
    assert "chemin sensible: conftest.py" in reason
    assert "fichiers:" not in reason  # seul le critère de chemin a échoué
    conn.close()


def test_agent_touching_env_file_is_human_required(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        (project_dir / "config").mkdir()
        (project_dir / "config" / ".env").write_text("API_KEY=xyz\n", encoding="utf-8")
        _fix_calc(project_dir)  # committe calc.py ET config/.env
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert result_task.status == TaskState.HUMAN_REQUIRED
    assert "chemin sensible: config/.env" in _human_required_reason(conn, task.id)
    conn.close()


def test_unreadable_project_patterns_force_human_required_on_otherwise_auto_diff(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    """Même diff que test_success_on_first_iteration_auto (classé AUTO),
    mais la liste du projet est illisible en base : jamais AUTO par défaut."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        _fix_calc(project_dir)
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    conn.execute("UPDATE projects SET extra_blocked_patterns = ? WHERE id = ?", ("{cassé", project.id))
    conn.commit()
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert result_task.status == TaskState.HUMAN_REQUIRED
    assert "liste de chemins protégés non résolue" in _human_required_reason(conn, task.id)
    conn.close()


def test_timeout_iteration_has_distinct_reason_and_consumes_budget(isolated_repo_root, tmp_path, monkeypatch, logs_dir):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.TIMED_OUT,
            error_detail=f"invocation headless expirée après {timeout_seconds}s",
        )

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=3, timeout_seconds=42)

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

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.INVOCATION_FAILED,
            is_error=True,
            error_detail="Failed to authenticate: OAuth session expired and could not be refreshed",
            raw_stdout='{"type": "result", "is_error": true, "result": "Failed to authenticate"}',
        )

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=3)

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

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        _fix_calc(project_dir)
        return _success_result()

    monkeypatch.setattr("app.git_wrapper.push_to_origin", fail_if_called)
    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)
    assert result_task.status == TaskState.DONE
    conn.close()


# --- CLI `engine task run-headless` : validation de l'exécutable au démarrage

def test_cli_invalid_executable_config_is_fatal_before_touching_task(isolated_repo_root, tmp_path, monkeypatch, capsys):
    """isolated_repo_root n'a pas de config/system.yaml -> DEFAULT_CONFIG,
    sans `executable` : fatal, code 2, message nommant la clé, tâche intacte."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    monkeypatch.setattr(
        headless_orchestrator, "run_headless_task", lambda *a, **k: pytest.fail("aucune tâche ne doit être touchée")
    )

    exit_code = cli_main.cmd_task_run_headless(argparse.Namespace(task_id=task.id))

    out = capsys.readouterr().out
    assert exit_code == 2
    assert "claude_headless.executable" in out and "fatal" in out
    conn = get_connection(_db_path(isolated_repo_root))
    assert get_task(conn, task.id).status == TaskState.RECEIVED
    conn.close()


def test_cli_passes_validated_executable_and_herbert_root(isolated_repo_root, tmp_path, monkeypatch, capsys):
    from app import claude_headless

    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    monkeypatch.setattr(
        claude_headless, "validate_claude_executable",
        lambda configured: claude_headless.ClaudeExecutable(path="C:/x/claude.exe", version="2.1.235 (Claude Code)"),
    )
    received = {}

    def fake_run_headless_task(conn, task_id, logs_dir, model, **kwargs):
        received.update(kwargs)
        return get_task(conn, task_id)

    monkeypatch.setattr(headless_orchestrator, "run_headless_task", fake_run_headless_task)

    cli_main.cmd_task_run_headless(argparse.Namespace(task_id=task.id))

    assert received["executable"] == "C:/x/claude.exe"
    assert received["herbert_root"] == isolated_repo_root
    assert "--version` : 2.1.235 (Claude Code)" in capsys.readouterr().out


def test_cli_reports_infrastructure_error_distinctly(isolated_repo_root, tmp_path, monkeypatch, capsys):
    from app import claude_headless

    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    monkeypatch.setattr(
        claude_headless, "validate_claude_executable",
        lambda configured: claude_headless.ClaudeExecutable(path="C:/x/claude.exe", version="v"),
    )

    def raise_infra(*args, **kwargs):
        raise claude_headless.InvocationInfrastructureError("impossible de lancer C:/x/claude.exe: FileNotFoundError")

    monkeypatch.setattr(headless_orchestrator, "run_headless_task", raise_infra)

    assert cli_main.cmd_task_run_headless(argparse.Namespace(task_id=task.id)) == 1
    out = capsys.readouterr().out
    assert "[BLOCKED] panne d'invocation (infrastructure, pas un échec de tâche)" in out
    assert "aucune itération consommée" in out


# --- Fichier settings HERBERT + panne d'infrastructure (init-hooks) -------

def _count(conn, table, task_id):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE task_id = ?", (task_id,)).fetchone()[0]


def test_settings_file_deployed_under_herbert_data_and_passed_to_every_invocation(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    received = []

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        received.append((executable, settings_path))
        if len(received) == 2:
            _fix_calc(project_dir)
        return _success_result()

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    expected = isolated_repo_root / "data" / "target_settings" / project.id / "settings.json"
    assert result_task.status == TaskState.DONE
    assert received == [(FAKE_EXE, str(expected))] * 2
    assert expected.is_file()
    assert not (project_dir / ".claude").exists()  # rien d'écrit dans le dépôt cible
    conn.close()


def test_infrastructure_error_blocks_immediately_without_iteration_or_pytest(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    from app.claude_headless import InvocationInfrastructureError

    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    calls = []

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        calls.append(1)
        raise InvocationInfrastructureError("impossible de lancer C:/fake/claude.exe: FileNotFoundError: [WinError 2]")

    def pytest_must_not_run(*args, **kwargs):
        raise AssertionError("cmd_task_test ne doit jamais tourner après une panne d'infrastructure")

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)
    monkeypatch.setattr(headless_orchestrator, "cmd_task_test", pytest_must_not_run)

    conn = get_connection(_db_path(isolated_repo_root))
    with pytest.raises(InvocationInfrastructureError):
        run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert calls == [1]  # jamais de 2e tentative
    assert get_task(conn, task.id).status == TaskState.BLOCKED
    assert list_headless_iterations_for_task(conn, task.id) == []  # aucune itération consommée
    assert _count(conn, "test_results", task.id) == 0  # pytest jamais lancé
    row = conn.execute(
        "SELECT from_state, reason FROM state_transitions WHERE task_id = ? AND to_state = 'BLOCKED'", (task.id,)
    ).fetchone()
    assert row["from_state"] == "EXECUTING"
    assert "panne d'invocation (infrastructure, pas un échec de tâche)" in row["reason"]
    assert "WinError 2" in row["reason"]
    conn.close()


def test_normal_invocation_problem_still_consumes_iteration_and_runs_pytest(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    """Non-régression : le process a TOURNÉ (is_error renvoyé par Claude
    Code) -> comportement inchangé, une itération consommée, pytest lancé."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.INVOCATION_FAILED, is_error=True,
            error_detail="Reached max turns (4)", raw_stdout='{"type":"result","is_error":true}',
        )

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(
        conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=1
    )

    assert result_task.status == TaskState.BLOCKED
    assert len(list_headless_iterations_for_task(conn, task.id)) == 1
    assert _count(conn, "test_results", task.id) == 1
    conn.close()


def _fake_claude_process(monkeypatch, is_error, result):
    """Remplace UNIQUEMENT le lancement du process dans app.claude_headless
    (pas subprocess.run global : git et pytest doivent rester réels) — le
    vrai invoke_claude_headless parse la sortie, comme en production."""
    import json
    import types

    from app import claude_headless

    stdout = json.dumps({"type": "result", "is_error": is_error, "result": result})
    fake_subprocess = types.SimpleNamespace(
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1 if is_error else 0, stdout=stdout, stderr=""),
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(claude_headless, "subprocess", fake_subprocess)


def test_real_auth_failure_output_blocks_immediately_without_iteration_or_pytest(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    from app.claude_headless import InvocationInfrastructureError

    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    _fake_claude_process(monkeypatch, True, "Failed to authenticate: OAuth session expired and could not be refreshed")

    def pytest_must_not_run(*args, **kwargs):
        raise AssertionError("cmd_task_test ne doit jamais tourner après un échec d'authentification")

    monkeypatch.setattr(headless_orchestrator, "cmd_task_test", pytest_must_not_run)

    conn = get_connection(_db_path(isolated_repo_root))
    with pytest.raises(InvocationInfrastructureError):
        run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert get_task(conn, task.id).status == TaskState.BLOCKED
    assert list_headless_iterations_for_task(conn, task.id) == []  # 0 itération consommée
    assert _count(conn, "test_results", task.id) == 0  # pytest jamais lancé
    reason = conn.execute(
        "SELECT reason FROM state_transitions WHERE task_id = ? AND to_state = 'BLOCKED'", (task.id,)
    ).fetchone()["reason"]
    assert "échec d'authentification" in reason and "claude auth login" in reason
    conn.close()


def test_legitimate_is_error_output_still_consumes_iteration_and_runs_pytest(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    _fake_claude_process(monkeypatch, True, "Tests en échec : SyntaxError: invalid syntax (calc.py, line 2)")

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(
        conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root, max_iterations=1
    )

    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 1
    assert iterations[0].invocation_status == HeadlessInvocationStatus.INVOCATION_FAILED
    assert _count(conn, "test_results", task.id) == 1  # pytest réellement lancé
    assert result_task.status == TaskState.BLOCKED  # plafond (1) atteint, comportement inchangé
    conn.close()


def test_settings_file_modified_during_iteration_blocks_without_pytest(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)

    def fake_invoke(prompt, cwd, model, timeout_seconds, executable, settings_path, permission_mode="bypassPermissions"):
        # Simule un agent qui atteint le fichier HERBERT par une commande Bash
        # (non contrôlée par PathPolicy) pour désactiver les hooks.
        with open(settings_path, "w", encoding="utf-8") as f:
            f.write('{"disableAllHooks": true}')
        _fix_calc(project_dir)
        return _success_result()

    def pytest_must_not_run(*args, **kwargs):
        raise AssertionError("résultat non fiable : pytest ne doit pas tourner")

    monkeypatch.setattr(headless_orchestrator, "invoke_claude_headless", fake_invoke)
    monkeypatch.setattr(headless_orchestrator, "cmd_task_test", pytest_must_not_run)

    conn = get_connection(_db_path(isolated_repo_root))
    result_task = run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=isolated_repo_root)

    assert result_task.status == TaskState.BLOCKED
    iterations = list_headless_iterations_for_task(conn, task.id)
    assert len(iterations) == 1 and iterations[0].tests_passed is False
    reason = conn.execute(
        "SELECT reason FROM state_transitions WHERE task_id = ? AND to_state = 'BLOCKED'", (task.id,)
    ).fetchone()["reason"]
    assert "intégrité : fichier settings HERBERT modifié" in reason
    conn.close()


def test_undeployable_settings_fails_before_any_iteration_or_transition(
    isolated_repo_root, tmp_path, monkeypatch, logs_dir
):
    """herbert_root À L'INTÉRIEUR du projet (ex. projet enregistré sur
    HERBERT lui-même) : le fichier settings tomberait dans le répertoire de
    l'agent -> refus avant toute itération, tâche inchangée."""
    project_dir = _make_target_project(tmp_path, initial_value=2)
    task, project = _make_task_with_branch(isolated_repo_root, project_dir, monkeypatch)
    monkeypatch.setattr(
        headless_orchestrator, "invoke_claude_headless", lambda *a, **k: pytest.fail("ne doit jamais invoquer")
    )

    conn = get_connection(_db_path(isolated_repo_root))
    status_before = get_task(conn, task.id).status
    with pytest.raises(HeadlessOrchestrationError) as info:
        run_headless_task(conn, task.id, logs_dir, model=MODEL, executable=FAKE_EXE, herbert_root=project_dir)

    assert "init-hooks" in str(info.value)
    assert get_task(conn, task.id).status == status_before
    assert list_headless_iterations_for_task(conn, task.id) == []
    conn.close()
