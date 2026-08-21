import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.models import (
    ChangeProof,
    CommandLogEntry,
    Project,
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


def get_project_by_name(conn: sqlite3.Connection, name: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    if row is None:
        return None
    return Project(id=row["id"], name=row["name"], path=row["path"], created_at=row["created_at"])


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    rows = conn.execute("SELECT * FROM projects ORDER BY created_at").fetchall()
    return [Project(id=r["id"], name=r["name"], path=r["path"], created_at=r["created_at"]) for r in rows]


def get_project(conn: sqlite3.Connection, project_id: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        return None
    return Project(id=row["id"], name=row["name"], path=row["path"], created_at=row["created_at"])


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
    )


def update_task_status(conn: sqlite3.Connection, task_id: str, new_status: TaskState) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (new_status.value, _utc_now_iso(), task_id),
    )
    conn.commit()


# --- state_transitions ----------------------------------------------------

def insert_state_transition(conn: sqlite3.Connection, transition: StateTransition) -> None:
    conn.execute(
        """INSERT INTO state_transitions (id, task_id, from_state, to_state, allowed, reason, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            transition.id,
            transition.task_id,
            transition.from_state.value,
            transition.to_state.value,
            int(transition.allowed),
            transition.reason,
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
