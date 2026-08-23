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
    is_human_decision: bool = False,
) -> tuple[bool, Task, StateTransition]:
    """Tente de faire transiter une tâche vers `to_state`.

    La transition est TOUJOURS journalisée dans state_transitions, qu'elle
    soit légale ou non. Si elle est illégale, le statut de la tâche n'est
    pas modifié et allowed=False est retourné (jamais d'échec silencieux).

    `is_human_decision` (V0.5, point 3) : garanti par le schéma, PAS déduit
    de `reason`. Défaut False car les deux vrais appelants internes à ce
    module (advance_after_test_result, ci-dessous) sont TOUJOURS
    automatiques — le seul site humain (app/cli/main.py::cmd_task_status,
    --to/--reason tapés par un humain) passe True explicitement. Voir
    migrations/0007_is_human_decision.sql pour le détail du choix.
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
        is_human_decision=is_human_decision,
    )
    insert_state_transition(conn, transition)

    if allowed:
        update_task_status(conn, task_id, to_state)
        task = get_task(conn, task_id)

    return allowed, task, transition


def advance_after_test_result(conn: sqlite3.Connection, task: Task, passed: bool) -> Task:
    """Fait avancer l'état de la tâche vers TESTING puis DONE/FAILED selon
    le résultat de `engine task test`, en empruntant le chemin légal depuis
    l'état courant (V0.2 posait les tables de transition TESTING->DONE/FAILED
    sans jamais les utiliser — comblé ici, nécessaire pour que `engine task
    promote` (V0.3) ait un état DONE réel à vérifier).

    Best-effort et non bloquant : si l'état courant ne permet pas ce chemin
    (ex: la tâche est déjà PROMOTED, BLOCKED, ou HUMAN_REQUIRED), aucune
    transition n'est forcée — la tentative refusée est quand même
    journalisée par transition_task, jamais un échec silencieux."""
    current = task.status

    if current == TaskState.RECEIVED:
        path = [TaskState.EXECUTING, TaskState.TESTING]
    elif current == TaskState.EXECUTING:
        path = [TaskState.TESTING]
    elif current == TaskState.FAILED:
        path = [TaskState.EXECUTING, TaskState.TESTING]
    elif current == TaskState.TESTING:
        path = []
    else:
        # DONE, PROMOTED, BLOCKED, HUMAN_REQUIRED : pas d'auto-avancement.
        return task

    for step in path:
        # Toujours automatique : advance_after_test_result n'est jamais
        # invoquée avec une saisie humaine directe.
        allowed, task, _ = transition_task(conn, task.id, step, is_human_decision=False)
        if not allowed:
            return task

    target = TaskState.DONE if passed else TaskState.FAILED
    _, task, _ = transition_task(conn, task.id, target, is_human_decision=False)
    return task
