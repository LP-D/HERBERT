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


def test_list_projects(db_conn):
    insert_project(db_conn, Project(name="p1", path="C:/p1"))
    insert_project(db_conn, Project(name="p2", path="C:/p2"))

    projects = list_projects(db_conn)
    names = {p.name for p in projects}
    assert names == {"p1", "p2"}
