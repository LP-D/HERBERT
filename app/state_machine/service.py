import sqlite3

from app.database.repository import get_task, insert_state_transition, update_task_status
from app.models.state_transition import StateTransition
from app.models.task import Task
from app.state_machine.states import TaskState
from app.state_machine.transitions import is_legal_transition


class TaskNotFoundError(Exception):
    pass


def transition_task(
    conn: sqlite3.Connection,
    task_id: str,
    to_state: TaskState,
    reason: str | None = None,
) -> tuple[bool, Task, StateTransition]:
    """Tente de faire transiter une tâche vers `to_state`.

    La transition est TOUJOURS journalisée dans state_transitions, qu'elle
    soit légale ou non. Si elle est illégale, le statut de la tâche n'est
    pas modifié et allowed=False est retourné (jamais d'échec silencieux).
    """
    task = get_task(conn, task_id)
    if task is None:
        raise TaskNotFoundError(f"tâche introuvable: {task_id}")

    allowed = is_legal_transition(task.status, to_state)

    transition = StateTransition(
        task_id=task_id,
        from_state=task.status,
        to_state=to_state,
        allowed=allowed,
        reason=reason,
    )
    insert_state_transition(conn, transition)

    if allowed:
        update_task_status(conn, task_id, to_state)
        task = get_task(conn, task_id)

    return allowed, task, transition
