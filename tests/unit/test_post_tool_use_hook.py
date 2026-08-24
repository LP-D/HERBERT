import json

from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import insert_project, insert_task
from app.models import Project, Task


def test_post_tool_use_logs_success_in_audit_log(post_tool_use_module, isolated_repo_root, monkeypatch):
    monkeypatch.setattr(post_tool_use_module, "REPO_ROOT", isolated_repo_root)

    # audit_log.task_id référence tasks(id) (foreign key réelle) : on crée
    # donc une vraie tâche plutôt que d'utiliser un id inventé.
    db_path = isolated_repo_root / "data" / "herbert.db"
    setup_conn = get_connection(db_path)
    apply_migrations(setup_conn, isolated_repo_root / "migrations")
    project = Project(name="proj-hook-test", path="C:/proj-hook-test")
    insert_project(setup_conn, project)
    task = Task(project_id=project.id, description="tâche pour test hook")
    insert_task(setup_conn, task)
    setup_conn.close()

    event = {
        "tool_name": "Write",
        "task_id": task.id,
        "tool_input": {"file_path": "app/models/task.py"},
        "tool_response": {"is_error": False},
    }

    result = post_tool_use_module.handle_event(event)
    assert result["status"] == "VERIFIED"

    conn = get_connection(db_path)
    row = conn.execute(
        "SELECT * FROM audit_log WHERE task_id = ? ORDER BY created_at DESC LIMIT 1", (task.id,)
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["status"] == "VERIFIED"
    assert row["component"] == "hook.post_tool_use"
    details = json.loads(row["details"])
    assert details["target"] == "app/models/task.py"

    # même trace attendue dans logs/*.jsonl, pas seulement en SQLite
    jsonl_lines = []
    for path in (isolated_repo_root / "logs").glob("*.jsonl"):
        jsonl_lines.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    matching = [e for e in jsonl_lines if e.get("component") == "hook.post_tool_use" and e.get("task_id") == task.id]
    assert matching, "aucune trace JSONL trouvée pour l'événement PostToolUse"
    assert matching[-1]["status"] == "VERIFIED"


def test_post_tool_use_logs_failure_in_audit_log(post_tool_use_module, isolated_repo_root, monkeypatch):
    monkeypatch.setattr(post_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "pytest"},
        "tool_response": {"is_error": True, "error": "1 test failed"},
    }

    result = post_tool_use_module.handle_event(event)
    assert result["status"] == "FAILED"

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT * FROM audit_log WHERE status = 'FAILED' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None


def test_post_tool_use_main_never_blocks_on_invalid_json(post_tool_use_module):
    exit_code = post_tool_use_module.main(["not valid json"])
    assert exit_code == 0


def test_post_tool_use_attributes_task_id_from_active_task(post_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    """V0.5 point 1 : même résolution que côté PreToolUse, pour audit_log."""
    monkeypatch.setattr(post_tool_use_module, "REPO_ROOT", isolated_repo_root)

    from app.active_task import activate_task

    project_dir = tmp_path / "projet-post-hook-actif"
    project_dir.mkdir()

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="projet-post-hook-actif", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche active pour post_tool_use")
    insert_task(conn, task)
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

    event = {
        "tool_name": "Bash",
        "cwd": str(project_dir),
        "tool_input": {"command": "git status"},
        "tool_response": {"is_error": False},
        # PAS de task_id — exactement ce que Claude Code envoie réellement.
    }
    result = post_tool_use_module.handle_event(event)
    assert result["status"] == "VERIFIED"

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT task_id FROM audit_log WHERE component = 'hook.post_tool_use' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row["task_id"] == task.id
