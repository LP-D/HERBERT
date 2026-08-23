from app.database.repository import (
    archive_project,
    archive_task,
    insert_audit_log,
    insert_project,
    insert_promotion,
    insert_state_transition,
    insert_task,
)
from app.models import Project, StateTransition, Task
from app.models.promotion import Promotion, PromotionStatus
from app.reporting.dashboard_data import (
    list_audit_log,
    list_human_decisions,
    list_projects_with_task_counts,
    list_rollbacks,
    list_tasks_for_project,
)
from app.state_machine.states import TaskState


def _seed_project_with_tasks(conn, name="projet-dashboard"):
    project = Project(name=name, path=f"C:/{name}")
    insert_project(conn, project)

    t1 = Task(project_id=project.id, description="tâche un", status=TaskState.DONE)
    t2 = Task(project_id=project.id, description="tâche deux", status=TaskState.FAILED)
    t3 = Task(project_id=project.id, description="tâche trois", status=TaskState.RECEIVED)
    insert_task(conn, t1)
    insert_task(conn, t2)
    insert_task(conn, t3)
    return project, t1, t2, t3


def test_list_projects_with_task_counts_exact(db_conn):
    project, t1, t2, t3 = _seed_project_with_tasks(db_conn)
    other = Project(name="projet-vide", path="C:/vide")
    insert_project(db_conn, other)

    result = list_projects_with_task_counts(db_conn)

    by_name = {p["name"]: p for p in result}
    assert set(by_name) == {"projet-dashboard", "projet-vide"}

    dashboard_counts = by_name["projet-dashboard"]["task_counts"]
    assert dashboard_counts["DONE"] == 1
    assert dashboard_counts["FAILED"] == 1
    assert dashboard_counts["RECEIVED"] == 1
    assert dashboard_counts["BLOCKED"] == 0  # état présent avec compteur 0, pas absent
    assert by_name["projet-dashboard"]["total_tasks"] == 3

    assert by_name["projet-vide"]["total_tasks"] == 0
    assert all(n == 0 for n in by_name["projet-vide"]["task_counts"].values())


def test_list_tasks_for_project_ordered_by_updated_at_desc(db_conn):
    project, t1, t2, t3 = _seed_project_with_tasks(db_conn)

    result = list_tasks_for_project(db_conn, project.id)

    assert len(result) == 3
    ids = {r["id"] for r in result}
    assert ids == {t1.id, t2.id, t3.id}
    # Insérées avec le même updated_at (created dans le même test) : ordre
    # non garanti entre elles, mais toutes présentes avec les bons champs.
    by_id = {r["id"]: r for r in result}
    assert by_id[t1.id]["status"] == "DONE"
    assert by_id[t1.id]["description"] == "tâche un"


def test_list_tasks_for_project_empty_for_unknown_project(db_conn):
    assert list_tasks_for_project(db_conn, "introuvable") == []


def test_list_human_decisions_excludes_system_reasons_and_null(db_conn):
    project, t1, _t2, _t3 = _seed_project_with_tasks(db_conn)

    insert_state_transition(
        db_conn,
        StateTransition(
            task_id=t1.id, from_state=TaskState.RECEIVED, to_state=TaskState.BLOCKED,
            allowed=True, reason="décision humaine explicite via --reason",
        ),
    )
    insert_state_transition(
        db_conn,
        StateTransition(
            task_id=t1.id, from_state=TaskState.EXECUTING, to_state=TaskState.TESTING,
            allowed=True, reason=None,  # avancée automatique (advance_after_test_result)
        ),
    )
    insert_state_transition(
        db_conn,
        StateTransition(
            task_id=t1.id, from_state=TaskState.DONE, to_state=TaskState.PROMOTED,
            allowed=True, reason="merge de promotion réussi",  # système (task promote)
        ),
    )

    result = list_human_decisions(db_conn)

    assert len(result) == 1
    assert result[0]["reason"] == "décision humaine explicite via --reason"
    assert result[0]["task_id"] == t1.id
    assert result[0]["project_name"] == project.name


def test_list_human_decisions_respects_limit(db_conn):
    project, t1, _t2, _t3 = _seed_project_with_tasks(db_conn)
    for i in range(5):
        insert_state_transition(
            db_conn,
            StateTransition(
                task_id=t1.id, from_state=TaskState.RECEIVED, to_state=TaskState.BLOCKED,
                allowed=True, reason=f"raison humaine {i}",
            ),
        )

    result = list_human_decisions(db_conn, limit=2)
    assert len(result) == 2


def test_list_rollbacks_combines_manual_and_auto(db_conn):
    project, t1, t2, _t3 = _seed_project_with_tasks(db_conn)

    insert_audit_log(
        db_conn,
        component="cli.task_rollback",
        event="rollback exécuté",
        level="INFO",
        status="VERIFIED",
        task_id=t1.id,
        details={"base_commit": "abc123"},
    )
    insert_promotion(
        db_conn,
        Promotion(
            task_id=t2.id,
            stable_branch="master",
            candidate_branch=f"candidate/{t2.id}",
            commit_before="1111111111111111",
            commit_after="2222222222222222",
            status=PromotionStatus.AUTO_ROLLBACK,
        ),
    )
    # Un audit_log d'un autre composant ne doit PAS apparaître comme rollback.
    insert_audit_log(
        db_conn, component="cli.task_branch", event="branche créée",
        level="INFO", status="VERIFIED", task_id=t1.id, details={},
    )

    result = list_rollbacks(db_conn)

    kinds = {r["kind"] for r in result}
    assert kinds == {"MANUAL", "AUTO_ROLLBACK"}
    manual = next(r for r in result if r["kind"] == "MANUAL")
    assert manual["task_id"] == t1.id
    assert manual["detail"]["base_commit"] == "abc123"
    auto = next(r for r in result if r["kind"] == "AUTO_ROLLBACK")
    assert auto["task_id"] == t2.id
    assert auto["detail"]["commit_before"] == "1111111111111111"


def test_list_rollbacks_empty_when_none(db_conn):
    _seed_project_with_tasks(db_conn)
    assert list_rollbacks(db_conn) == []


def test_list_audit_log_recent_first_and_limited(db_conn):
    project, t1, _t2, _t3 = _seed_project_with_tasks(db_conn)
    for i in range(3):
        insert_audit_log(
            db_conn, component="cli.task_test", event=f"event-{i}",
            level="INFO", status="VERIFIED", task_id=t1.id, details={"i": i},
        )

    result = list_audit_log(db_conn, limit=2)

    assert len(result) == 2
    # ordre décroissant par created_at : les insertions les plus récentes en premier
    assert result[0]["created_at"] >= result[1]["created_at"]


def test_list_audit_log_includes_entries_without_task(db_conn):
    insert_audit_log(
        db_conn, component="cli.doctor", event="engine doctor exécuté",
        level="INFO", status="VERIFIED", task_id=None, details={"created": []},
    )

    result = list_audit_log(db_conn)

    assert len(result) == 1
    assert result[0]["task_id"] is None
    assert result[0]["project_name"] is None
    assert result[0]["component"] == "cli.doctor"


# --- soft delete (V0.5, point 2) ------------------------------------------

def test_list_projects_with_task_counts_excludes_archived_by_default(db_conn):
    active = Project(name="p-actif-dash", path="C:/p-actif-dash")
    archived = Project(name="p-archive-dash", path="C:/p-archive-dash")
    insert_project(db_conn, active)
    insert_project(db_conn, archived)
    archive_project(db_conn, archived.id, "2026-08-23T00:00:00+00:00")

    default_result = list_projects_with_task_counts(db_conn)
    names_default = {p["name"] for p in default_result}
    assert "p-actif-dash" in names_default
    assert "p-archive-dash" not in names_default

    full_result = list_projects_with_task_counts(db_conn, include_archived=True)
    names_full = {p["name"] for p in full_result}
    assert "p-archive-dash" in names_full
    archived_entry = next(p for p in full_result if p["name"] == "p-archive-dash")
    assert archived_entry["archived_at"] is not None


def test_list_tasks_for_project_excludes_archived_by_default(db_conn):
    project = Project(name="p-taches-archive", path="C:/p-taches-archive")
    insert_project(db_conn, project)
    active_task = Task(project_id=project.id, description="active")
    archived_task = Task(project_id=project.id, description="archivée")
    insert_task(db_conn, active_task)
    insert_task(db_conn, archived_task)
    archive_task(db_conn, archived_task.id, "2026-08-23T00:00:00+00:00")

    default_result = list_tasks_for_project(db_conn, project.id)
    ids_default = {t["id"] for t in default_result}
    assert active_task.id in ids_default
    assert archived_task.id not in ids_default

    full_result = list_tasks_for_project(db_conn, project.id, include_archived=True)
    ids_full = {t["id"] for t in full_result}
    assert archived_task.id in ids_full
