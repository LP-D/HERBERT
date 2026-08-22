from app.database.repository import get_task, insert_project, insert_task
from app.models import Project, Task
from app.state_machine import TaskState, is_legal_transition
from app.state_machine.service import advance_after_test_result, transition_task
from app.state_machine.transitions import LEGAL_TRANSITIONS


def _make_task(db_conn) -> Task:
    project = Project(name="projet-etats", path="C:/projet-etats")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="tâche de test")
    insert_task(db_conn, task)
    return task


def test_legal_transition_table_covers_all_states():
    assert set(LEGAL_TRANSITIONS.keys()) == set(TaskState)


def test_legal_transition_is_accepted(db_conn):
    task = _make_task(db_conn)
    assert is_legal_transition(TaskState.RECEIVED, TaskState.EXECUTING)

    allowed, updated_task, transition = transition_task(db_conn, task.id, TaskState.EXECUTING)

    assert allowed is True
    assert transition.allowed is True
    assert updated_task.status == TaskState.EXECUTING

    stored = get_task(db_conn, task.id)
    assert stored.status == TaskState.EXECUTING

    row = db_conn.execute(
        "SELECT * FROM state_transitions WHERE task_id = ?", (task.id,)
    ).fetchone()
    assert row["allowed"] == 1
    assert row["from_state"] == "RECEIVED"
    assert row["to_state"] == "EXECUTING"


def test_illegal_transition_is_refused_and_logged(db_conn):
    task = _make_task(db_conn)
    assert not is_legal_transition(TaskState.RECEIVED, TaskState.DONE)

    allowed, updated_task, transition = transition_task(db_conn, task.id, TaskState.DONE)

    assert allowed is False
    assert transition.allowed is False
    # le statut de la tâche ne doit PAS avoir changé
    assert updated_task.status == TaskState.RECEIVED

    stored = get_task(db_conn, task.id)
    assert stored.status == TaskState.RECEIVED

    # la transition refusée doit être journalisée, pas juste ignorée
    row = db_conn.execute(
        "SELECT * FROM state_transitions WHERE task_id = ? AND allowed = 0", (task.id,)
    ).fetchone()
    assert row is not None
    assert row["from_state"] == "RECEIVED"
    assert row["to_state"] == "DONE"


def test_done_can_only_go_to_promoted():
    """DONE n'est plus strictement terminal depuis V0.3 : `engine task
    promote` doit pouvoir l'amener à PROMOTED — mais rien d'autre."""
    assert LEGAL_TRANSITIONS[TaskState.DONE] == {TaskState.PROMOTED}


def test_promoted_can_go_to_done_or_rolled_back():
    """PROMOTED -> DONE si le health check réussit. PROMOTED ->
    ROLLED_BACK si le health check échoue ET que l'AUTO_ROLLBACK (revert)
    a réellement réussi — laisser la tâche à PROMOTED serait trompeur une
    fois le merge effectivement reverté dans le dépôt."""
    assert LEGAL_TRANSITIONS[TaskState.PROMOTED] == {TaskState.DONE, TaskState.ROLLED_BACK}


def test_rolled_back_is_terminal():
    assert LEGAL_TRANSITIONS[TaskState.ROLLED_BACK] == set()


def test_advance_after_test_result_from_received_to_done(db_conn):
    task = _make_task(db_conn)
    updated = advance_after_test_result(db_conn, task, passed=True)
    assert updated.status == TaskState.DONE

    transitions = db_conn.execute(
        "SELECT from_state, to_state FROM state_transitions WHERE task_id = ? ORDER BY created_at", (task.id,)
    ).fetchall()
    path = [(t["from_state"], t["to_state"]) for t in transitions]
    assert path == [("RECEIVED", "EXECUTING"), ("EXECUTING", "TESTING"), ("TESTING", "DONE")]


def test_advance_after_test_result_from_received_to_failed(db_conn):
    task = _make_task(db_conn)
    updated = advance_after_test_result(db_conn, task, passed=False)
    assert updated.status == TaskState.FAILED


def test_advance_after_test_result_does_not_force_from_promoted(db_conn):
    task = _make_task(db_conn)
    advance_after_test_result(db_conn, task, passed=True)  # -> DONE
    allowed, task, _ = transition_task(db_conn, task.id, TaskState.PROMOTED)
    assert allowed is True

    # ré-exécuter engine task test sur une tâche déjà PROMOTED ne doit rien
    # forcer silencieusement
    updated = advance_after_test_result(db_conn, task, passed=True)
    assert updated.status == TaskState.PROMOTED
