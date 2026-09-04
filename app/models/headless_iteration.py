import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from app.models.enums import HeadlessInvocationStatus


class HeadlessIteration(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    iteration_number: int
    prompt_sent: str
    raw_result: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    invocation_status: HeadlessInvocationStatus
    tests_passed: bool | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
