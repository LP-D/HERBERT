import pytest
from pydantic import ValidationError

from app.database.repository import get_project_by_name, insert_project, list_projects
from app.models import Project


def test_create_valid_project(db_conn):
    project = Project(name="mon-projet", path="C:/Users/Dufour/mon-projet")
    insert_project(db_conn, project)

    stored = get_project_by_name(db_conn, "mon-projet")
    assert stored is not None
    assert stored.id == project.id
    assert stored.path == "C:/Users/Dufour/mon-projet"


def test_reject_invalid_project_name():
    with pytest.raises(ValidationError):
        Project(name="", path="C:/somewhere")


def test_reject_project_name_with_forbidden_characters():
    with pytest.raises(ValidationError):
        Project(name="mon/projet:invalide", path="C:/somewhere")


def test_duplicate_project_name_rejected_by_db(db_conn):
    import sqlite3

    insert_project(db_conn, Project(name="dupliqué", path="C:/a"))
    with pytest.raises(sqlite3.IntegrityError):
        insert_project(db_conn, Project(name="dupliqué", path="C:/b"))


def test_new_project_has_no_extra_blocked_patterns_stored_as_null(db_conn):
    project = Project(name="sans-ajout", path="C:/sans-ajout")
    assert project.extra_blocked_patterns == []
    insert_project(db_conn, project)

    raw = db_conn.execute("SELECT extra_blocked_patterns FROM projects WHERE id = ?", (project.id,)).fetchone()[0]
    assert raw is None
    assert get_project_by_name(db_conn, "sans-ajout").extra_blocked_patterns == []


def test_extra_blocked_patterns_roundtrip_as_json_list(db_conn):
    insert_project(db_conn, Project(name="avec-ajout", path="C:/avec-ajout", extra_blocked_patterns=["data/", "**/*.db"]))

    raw = db_conn.execute("SELECT extra_blocked_patterns FROM projects WHERE name = 'avec-ajout'").fetchone()[0]
    assert raw == '["data/", "**/*.db"]'
    assert get_project_by_name(db_conn, "avec-ajout").extra_blocked_patterns == ["data/", "**/*.db"]


def test_insert_refuses_unreadable_marker_instead_of_storing_null(db_conn):
    with pytest.raises(ValueError):
        insert_project(db_conn, Project(name="marqueur", path="C:/m", extra_blocked_patterns=None))
    assert get_project_by_name(db_conn, "marqueur") is None


def test_unreadable_extra_blocked_patterns_does_not_break_project_listing(db_conn):
    """Valeur corrompue : le projet reste listable (dashboard, CLI), avec
    None pour signaler la résolution impossible — jamais une exception."""
    insert_project(db_conn, Project(name="corrompu", path="C:/corrompu"))
    db_conn.execute("UPDATE projects SET extra_blocked_patterns = 'pas json' WHERE name = 'corrompu'")
    db_conn.commit()

    listed = {p.name: p for p in list_projects(db_conn)}
    assert listed["corrompu"].extra_blocked_patterns is None


def test_list_projects(db_conn):
    insert_project(db_conn, Project(name="p1", path="C:/p1"))
    insert_project(db_conn, Project(name="p2", path="C:/p2"))

    projects = list_projects(db_conn)
    names = {p.name for p in projects}
    assert names == {"p1", "p2"}
