import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.models import (
    ChangeProof,
    CommandLogEntry,
    HeadlessInvocationStatus,
    HeadlessIteration,
    Project,
    Promotion,
    PromotionStatus,
    StateTransition,
    Task,
    TestCaseOutcome,
    TestCaseResult,
    TestResult,
    TestResultStatus,
)
from app.state_machine.states import TaskState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- projects ---------------------------------------------------------

def insert_project(conn: sqlite3.Connection, project: Project) -> None:
    conn.execute(
        "INSERT INTO projects (id, name, path, created_at) VALUES (?, ?, ?, ?)",
        (project.id, project.name, project.path, project.created_at.isoformat()),
    )
    conn.commit()


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=row["id"],
        name=row["name"],
        path=row["path"],
        created_at=row["created_at"],
        archived_at=row["archived_at"],
    )


def get_project_by_name(conn: sqlite3.Connection, name: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return _project_from_row(row)


def list_projects(conn: sqlite3.Connection, include_archived: bool = False) -> list[Project]:
    """Par défaut, exclut les projets archivés (voir archive_project) —
    jamais supprimés physiquement, juste hors des vues de listing par
    défaut. `include_archived=True` (ex. `engine project list
    --include-archived`) les inclut aussi."""
    if include_archived:
        rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY created_at").fetchall()
    return [_project_from_row(r) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    """Lookup par id : TOUJOURS retourné, archivé ou non — l'archivage
    masque des LISTES, jamais une entité déjà identifiée par son id
    (« reste interrogeable », voir migrations/0006_archived_at.sql)."""
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    return _project_from_row(row)


def archive_project(conn: sqlite3.Connection, project_id: str, archived_at: str) -> None:
    """Soft delete : ne supprime RIEN physiquement, ne touche à aucune
    table d'audit (audit_log, state_transitions, commands, change_proofs
    restent intacts, append-only). Le garde-fou (refuser si des tâches
    non archivées existent) est appliqué par l'appelant (CLI), pas ici —
    cette fonction est la primitive, pas la politique."""
    conn.execute("UPDATE projects SET archived_at = ? WHERE id = ?", (archived_at, project_id))
    conn.commit()


def count_unarchived_tasks(conn: sqlite3.Connection, project_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE project_id = ? AND archived_at IS NULL", (project_id,)
    ).fetchone()
    return row["n"]


# --- tasks --------------------------------------------------------------

def insert_task(conn: sqlite3.Connection, task: Task) -> None:
    conn.execute(
        """INSERT INTO tasks (id, project_id, description, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            task.id,
            task.project_id,
            task.description,
            task.status.value,
            task.created_at.isoformat(),
            task.updated_at.isoformat(),
        ),
    )
    conn.commit()


def get_task(conn: sqlite3.Connection, task_id: str) -> Task | None:
    """Lookup par id : TOUJOURS retourné, archivée ou non — voir
    get_project() pour la même règle côté projet."""
    row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return None
    return Task(
        id=row["id"],
        project_id=row["project_id"],
        description=row["description"],
        status=TaskState(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
    )


def update_task_status(conn: sqlite3.Connection, task_id: str, new_status: TaskState) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (new_status.value, _utc_now_iso(), task_id),
    )
    conn.commit()


def archive_task(conn: sqlite3.Connection, task_id: str, archived_at: str) -> None:
    """Soft delete : ne supprime rien, ne touche à aucune table d'audit."""
    conn.execute("UPDATE tasks SET archived_at = ? WHERE id = ?", (archived_at, task_id))
    conn.commit()


# --- state_transitions ----------------------------------------------------

def insert_state_transition(conn: sqlite3.Connection, transition: StateTransition) -> None:
    conn.execute(
        """INSERT INTO state_transitions
           (id, task_id, from_state, to_state, allowed, reason, is_human_decision, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            transition.id,
            transition.task_id,
            transition.from_state.value,
            transition.to_state.value,
            int(transition.allowed),
            transition.reason,
            int(transition.is_human_decision),
            transition.created_at.isoformat(),
        ),
    )
    conn.commit()


# --- commands ---------------------------------------------------------

def insert_command_log(conn: sqlite3.Connection, entry: CommandLogEntry) -> None:
    conn.execute(
        """INSERT INTO commands (id, task_id, tool_name, command, decision, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            entry.id,
            entry.task_id,
            entry.tool_name,
            entry.command,
            entry.decision.value,
            entry.reason,
            entry.created_at.isoformat(),
        ),
    )
    conn.commit()


# --- change_proofs ------------------------------------------------------

def insert_change_proof(conn: sqlite3.Connection, proof: ChangeProof) -> None:
    conn.execute(
        """INSERT INTO change_proofs
           (id, task_id, files_changed, commands_executed, tests_passed, tests_failed, regressions, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            proof.id,
            proof.task_id,
            json.dumps(proof.files_changed),
            json.dumps(proof.commands_executed),
            proof.tests_passed,
            proof.tests_failed,
            json.dumps(proof.regressions),
            proof.status.value,
            proof.created_at.isoformat(),
        ),
    )
    conn.commit()


def get_latest_change_proof(conn: sqlite3.Connection, task_id: str) -> ChangeProof | None:
    row = conn.execute(
        "SELECT * FROM change_proofs WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    if row is None:
        return None
    return ChangeProof(
        id=row["id"],
        task_id=row["task_id"],
        files_changed=json.loads(row["files_changed"]),
        commands_executed=json.loads(row["commands_executed"]),
        tests_passed=row["tests_passed"],
        tests_failed=row["tests_failed"],
        regressions=json.loads(row["regressions"]),
        status=TaskState(row["status"]),
        created_at=row["created_at"],
    )


# --- test_results ---------------------------------------------------------

def _test_result_from_row(row: sqlite3.Row) -> TestResult:
    return TestResult(
        id=row["id"],
        task_id=row["task_id"],
        status=TestResultStatus(row["status"]),
        total=row["total"],
        passed=row["passed"],
        failed=row["failed"],
        errors=row["errors"],
        duration_seconds=row["duration_seconds"],
        raw_output=row["raw_output"] or "",
        test_cases=[
            TestCaseResult(name=tc["name"], outcome=TestCaseOutcome(tc["outcome"]))
            for tc in json.loads(row["test_cases"])
        ],
        created_at=row["created_at"],
    )


def insert_test_result(conn: sqlite3.Connection, result: TestResult) -> None:
    conn.execute(
        """INSERT INTO test_results
           (id, task_id, status, total, passed, failed, errors, duration_seconds, raw_output, test_cases, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            result.id,
            result.task_id,
            result.status.value,
            result.total,
            result.passed,
            result.failed,
            result.errors,
            result.duration_seconds,
            result.raw_output,
            json.dumps([{"name": tc.name, "outcome": tc.outcome.value} for tc in result.test_cases]),
            result.created_at.isoformat(),
        ),
    )
    conn.commit()


def get_latest_test_result_for_task(conn: sqlite3.Connection, task_id: str) -> TestResult | None:
    row = conn.execute(
        "SELECT * FROM test_results WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    if row is None:
        return None
    return _test_result_from_row(row)


def get_latest_verified_pass_test_result_for_project(conn: sqlite3.Connection, project_id: str) -> TestResult | None:
    row = conn.execute(
        """SELECT tr.* FROM test_results tr
           JOIN tasks t ON t.id = tr.task_id
           WHERE t.project_id = ? AND tr.status = 'VERIFIED_PASS'
           ORDER BY tr.created_at DESC LIMIT 1""",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return _test_result_from_row(row)


# --- commands (lecture pour ChangeProof) ---------------------------------

def list_commands_for_task(conn: sqlite3.Connection, task_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT command FROM commands WHERE task_id = ? ORDER BY created_at", (task_id,)
    ).fetchall()
    return [r["command"] for r in rows]


# --- audit_log ----------------------------------------------------------

def insert_audit_log(
    conn: sqlite3.Connection,
    component: str,
    event: str,
    level: str,
    status: str,
    task_id: str | None = None,
    details: dict | None = None,
) -> None:
    conn.execute(
        """INSERT INTO audit_log (id, task_id, component, event, level, status, details, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            task_id,
            component,
            event,
            level,
            status,
            json.dumps(details) if details is not None else None,
            _utc_now_iso(),
        ),
    )
    conn.commit()


def get_latest_audit_log_details(conn: sqlite3.Connection, task_id: str, component: str) -> dict | None:
    """Utilisé pour retrouver le `base_commit`/`branch` enregistrés par
    `engine task branch` (pas de table dédiée pour ça — audit_log suffit)."""
    row = conn.execute(
        """SELECT details FROM audit_log WHERE task_id = ? AND component = ?
           ORDER BY created_at DESC LIMIT 1""",
        (task_id, component),
    ).fetchone()
    if row is None or row["details"] is None:
        return None
    return json.loads(row["details"])


# --- headless_iterations (pivot orchestration headless) -----------------

def insert_headless_iteration(conn: sqlite3.Connection, iteration: HeadlessIteration) -> None:
    conn.execute(
        """INSERT INTO headless_iterations
           (id, task_id, iteration_number, prompt_sent, raw_result, session_id,
            num_turns, invocation_status, tests_passed, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            iteration.id,
            iteration.task_id,
            iteration.iteration_number,
            iteration.prompt_sent,
            iteration.raw_result,
            iteration.session_id,
            iteration.num_turns,
            iteration.invocation_status.value,
            iteration.tests_passed,
            iteration.created_at.isoformat(),
        ),
    )
    conn.commit()


def _headless_iteration_from_row(row: sqlite3.Row) -> HeadlessIteration:
    return HeadlessIteration(
        id=row["id"],
        task_id=row["task_id"],
        iteration_number=row["iteration_number"],
        prompt_sent=row["prompt_sent"],
        raw_result=row["raw_result"],
        session_id=row["session_id"],
        num_turns=row["num_turns"],
        invocation_status=HeadlessInvocationStatus(row["invocation_status"]),
        tests_passed=row["tests_passed"],
        created_at=row["created_at"],
    )


def list_headless_iterations_for_task(conn: sqlite3.Connection, task_id: str) -> list[HeadlessIteration]:
    rows = conn.execute(
        "SELECT * FROM headless_iterations WHERE task_id = ? ORDER BY iteration_number", (task_id,)
    ).fetchall()
    return [_headless_iteration_from_row(r) for r in rows]


# --- promotions (V0.3) ---------------------------------------------------

def _promotion_from_row(row: sqlite3.Row) -> Promotion:
    return Promotion(
        id=row["id"],
        task_id=row["task_id"],
        stable_branch=row["stable_branch"],
        candidate_branch=row["candidate_branch"],
        commit_before=row["commit_before"],
        commit_after=row["commit_after"],
        status=PromotionStatus(row["status"]),
        created_at=row["created_at"],
    )


def insert_promotion(conn: sqlite3.Connection, promotion: Promotion) -> None:
    conn.execute(
        """INSERT INTO promotions
           (id, task_id, stable_branch, candidate_branch, commit_before, commit_after, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            promotion.id,
            promotion.task_id,
            promotion.stable_branch,
            promotion.candidate_branch,
            promotion.commit_before,
            promotion.commit_after,
            promotion.status.value,
            promotion.created_at.isoformat(),
        ),
    )
    conn.commit()


def update_promotion_status(conn: sqlite3.Connection, promotion_id: str, status: PromotionStatus) -> None:
    conn.execute("UPDATE promotions SET status = ? WHERE id = ?", (status.value, promotion_id))
    conn.commit()


def get_latest_promotion_for_task(conn: sqlite3.Connection, task_id: str) -> Promotion | None:
    row = conn.execute(
        "SELECT * FROM promotions WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task_id,)
    ).fetchone()
    if row is None:
        return None
    return _promotion_from_row(row)
