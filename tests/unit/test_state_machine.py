from app.database.repository import get_task, insert_project, insert_task
from app.models import Project, Task
from app.state_machine import TaskState, is_legal_transition
from app.state_machine.service import transition_task
from app.state_machine.transitions import LEGAL_TRANSITIONS


def _make_task(db_conn) -> Task:
    project = Project(name="projet-etats", path="C:/projet-etats")
    insert_project(db_conn, project)
    task = Task(project_id=project.id, description="tâche de test")
    insert_task(db_conn, task)
    return task


def test_legal_transition_table_covers_all_seven_states():
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


def test_done_is_terminal():
    assert LEGAL_TRANSITIONS[TaskState.DONE] == set()
