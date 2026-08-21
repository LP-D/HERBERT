import pytest
from pydantic import ValidationError

from app.database.repository import get_task, insert_project, insert_task
from app.models import Project, Task
from app.state_machine.states import TaskState


def test_create_task(db_conn):
    project = Project(name="projet-taches", path="C:/projet-taches")
    insert_project(db_conn, project)

    task = Task(project_id=project.id, description="implémenter la fonctionnalité X")
    insert_task(db_conn, task)

    stored = get_task(db_conn, task.id)
    assert stored is not None
    assert stored.project_id == project.id
    assert stored.description == "implémenter la fonctionnalité X"
    assert stored.status == TaskState.RECEIVED


def test_reject_empty_task_description():
    with pytest.raises(ValidationError):
        Task(project_id="whatever", description="   ")
