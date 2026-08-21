import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.enums import CommandDecision


class CommandLogEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str | None = None
    tool_name: str
    command: str
    decision: CommandDecision
    reason: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
