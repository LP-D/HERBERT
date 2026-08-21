import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.state_machine.states import TaskState


class StateTransition(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    from_state: TaskState
    to_state: TaskState
    allowed: bool
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
