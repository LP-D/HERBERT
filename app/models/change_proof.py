import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.state_machine.states import TaskState


class ChangeProof(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    files_changed: list[str] = Field(default_factory=list)
    commands_executed: list[str] = Field(default_factory=list)
    tests_passed: int = 0
    tests_failed: int = 0
    regressions: list[str] = Field(default_factory=list)
    status: TaskState
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
