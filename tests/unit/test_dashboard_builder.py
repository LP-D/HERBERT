import re
from pathlib import Path

from app.database.repository import (
    archive_project,
    archive_task,
    insert_audit_log,
    insert_project,
    insert_state_transition,
    insert_task,
)
from app.models import Project, StateTransition, Task
from app.reporting.dashboard_builder import build_dashboard, render_state_diagram_svg
from app.state_machine.states import TaskState

_HREF_SRC_RE = re.compile(r'(?:href|src)="([^"]+)"')


def _seed(conn):
    project = Project(name="projet-dash", path="C:/projet-dash")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche à générer", status=TaskState.DONE)
    insert_task(conn, task)
    insert_state_transition(
        conn, StateTransition(task_id=task.id, from_state=TaskState.RECEIVED, to_state=TaskState.EXECUTING, allowed=True)
    )
    insert_state_transition(
        conn, StateTransition(task_id=task.id, from_state=TaskState.EXECUTING, to_state=TaskState.TESTING, allowed=True)
    )
    insert_state_transition(
        conn, StateTransition(task_id=task.id, from_state=TaskState.TESTING, to_state=TaskState.DONE, allowed=True)
    )
    insert_state_transition(
        conn,
        StateTransition(task_id=task.id, from_state=TaskState.DONE, to_state=TaskState.FAILED, allowed=False, reason="illégal"),
    )
    insert_audit_log(
        conn, component="cli.task_test", event="test run", level="INFO", status="VERIFIED",
        task_id=task.id, details={"total": 5},
    )
    return project, task


def test_build_dashboard_writes_all_expected_pages(db_conn, tmp_path):
    project, task = _seed(db_conn)

    result = build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    expected = {
        "index.html",
        f"project/{project.id}.html",
        f"task/{task.id}.html",
        "decisions.html",
        "audit.html",
    }
    assert expected.issubset(set(result["pages"]))
    for rel in expected:
        assert (tmp_path / "dashboard" / rel).exists()

    assert (tmp_path / "dashboard" / "assets" / "style.css").exists()
    assert (tmp_path / "dashboard" / "assets" / "app.js").exists()


def test_build_dashboard_overwrites_stale_content(db_conn, tmp_path):
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    stale = dashboard_dir / "task" / "stale-task-id.html"
    stale.parent.mkdir(parents=True)
    stale.write_text("<html>ancien contenu</html>", encoding="utf-8")

    _seed(db_conn)
    build_dashboard(db_conn, dashboard_dir, tmp_path / "logs")

    assert not stale.exists()  # écrasé, pas accumulé


def test_build_dashboard_no_dead_internal_links(db_conn, tmp_path):
    _seed(db_conn)
    dashboard_dir = tmp_path / "dashboard"
    build_dashboard(db_conn, dashboard_dir, tmp_path / "logs")

    all_files = {p.relative_to(dashboard_dir).as_posix() for p in dashboard_dir.rglob("*") if p.is_file()}

    for html_file in dashboard_dir.rglob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        base_dir = html_file.parent
        for link in _HREF_SRC_RE.findall(content):
            if link.startswith(("http://", "https://", "#")):
                continue
            resolved = (base_dir / link).resolve()
            rel = resolved.relative_to(dashboard_dir.resolve()).as_posix()
            assert rel in all_files, f"lien mort dans {html_file.name}: {link} -> {rel}"


def test_task_page_includes_state_diagram_and_dashboard_nav(db_conn, tmp_path):
    project, task = _seed(db_conn)
    build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    content = (tmp_path / "dashboard" / "task" / f"{task.id}.html").read_text(encoding="utf-8")

    assert "<svg" in content
    assert "Chemin d'état réellement suivi" in content
    assert 'class="hb-nav"' in content
    assert task.description in content  # contenu de engine report toujours présent (réutilisé)


def test_render_state_diagram_svg_highlights_followed_path():
    transitions = [
        {"from_state": "RECEIVED", "to_state": "EXECUTING", "allowed": True, "created_at": "t1"},
        {"from_state": "EXECUTING", "to_state": "BLOCKED", "allowed": False, "created_at": "t2"},
    ]
    svg = render_state_diagram_svg(transitions, current_state="EXECUTING")

    assert svg.startswith("<svg")
    assert "arrow-blue" in svg  # chemin suivi
    assert "stroke-dasharray" in svg  # tentative refusée marquée distinctement
    assert "RECEIVED" in svg and "EXECUTING" in svg


def test_index_page_shows_project_and_state_badges(db_conn, tmp_path):
    project, _task = _seed(db_conn)
    build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    content = (tmp_path / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert project.name in content
    assert "DONE" in content


def test_project_page_lists_tasks_with_sort_and_filter_js(db_conn, tmp_path):
    project, task = _seed(db_conn)
    build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    content = (tmp_path / "dashboard" / "project" / f"{project.id}.html").read_text(encoding="utf-8")
    assert task.description in content
    assert "hbInitSortableTable" in content
    assert "hbInitFilter" in content


def test_decisions_page_documents_schema_guarantee(db_conn, tmp_path):
    """V0.5 point 3 : la page ne prétend plus se fonder sur une heuristique
    de `reason` — elle documente la garantie de schéma is_human_decision."""
    _seed(db_conn)
    build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    content = (tmp_path / "dashboard" / "decisions.html").read_text(encoding="utf-8")
    assert "is_human_decision" in content
    assert "task status --to X" in content


def test_empty_database_generates_dashboard_without_crashing(db_conn, tmp_path):
    result = build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    assert "index.html" in result["pages"]
    content = (tmp_path / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert "hb-empty" in content or "Aucun" in content


# --- soft delete (V0.5, point 2) ------------------------------------------

def test_archived_project_hidden_from_index_but_page_still_generated(db_conn, tmp_path):
    project, task = _seed(db_conn)
    archive_project(db_conn, project.id, "2026-08-23T00:00:00+00:00")

    result = build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    # toujours générée (« reste interrogeable »), avec une mention explicite
    assert f"project/{project.id}.html" in result["pages"]
    project_page = (tmp_path / "dashboard" / "project" / f"{project.id}.html").read_text(encoding="utf-8")
    assert "archivé" in project_page.lower()

    # mais absente de l'index par défaut
    index_content = (tmp_path / "dashboard" / "index.html").read_text(encoding="utf-8")
    assert project.name not in index_content


def test_archived_task_hidden_from_project_table_but_page_still_generated(db_conn, tmp_path):
    project, task = _seed(db_conn)
    archive_task(db_conn, task.id, "2026-08-23T00:00:00+00:00")

    result = build_dashboard(db_conn, tmp_path / "dashboard", tmp_path / "logs")

    assert f"task/{task.id}.html" in result["pages"]
    task_page = (tmp_path / "dashboard" / "task" / f"{task.id}.html").read_text(encoding="utf-8")
    assert "archivé" in task_page.lower()

    project_page = (tmp_path / "dashboard" / "project" / f"{project.id}.html").read_text(encoding="utf-8")
    assert task.description not in project_page  # absente du tableau par défaut


def test_no_dead_links_with_archived_entities_present(db_conn, tmp_path):
    """Renforce test_build_dashboard_no_dead_internal_links : un lien vers
    une tâche/un projet archivé (ex. depuis decisions.html) ne doit JAMAIS
    devenir un lien mort — c'est précisément le risque identifié en
    conception pour le point 2."""
    project, task = _seed(db_conn)
    archive_project(db_conn, project.id, "2026-08-23T00:00:00+00:00")
    archive_task(db_conn, task.id, "2026-08-23T00:00:00+00:00")

    dashboard_dir = tmp_path / "dashboard"
    build_dashboard(db_conn, dashboard_dir, tmp_path / "logs")

    all_files = {p.relative_to(dashboard_dir).as_posix() for p in dashboard_dir.rglob("*") if p.is_file()}
    for html_file in dashboard_dir.rglob("*.html"):
        content = html_file.read_text(encoding="utf-8")
        base_dir = html_file.parent
        for link in _HREF_SRC_RE.findall(content):
            if link.startswith(("http://", "https://", "#")):
                continue
            resolved = (base_dir / link).resolve()
            rel = resolved.relative_to(dashboard_dir.resolve()).as_posix()
            assert rel in all_files, f"lien mort dans {html_file.name}: {link} -> {rel}"
