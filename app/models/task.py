import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from app.state_machine.states import TaskState


class Task(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    project_id: str
    description: str
    status: TaskState = TaskState.RECEIVED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("description")
    @classmethod
    def description_must_be_valid(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("la description de la tâche ne peut pas être vide")
        return v
