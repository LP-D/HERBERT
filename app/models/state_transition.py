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
    # Garanti par le schéma (V0.5, point 3), pas une heuristique déduite de
    # `reason` — déterminé par l'appelant de transition_task() selon
    # l'origine réelle de la transition (CLI humaine directe vs
    # automatique). Défaut False : voir migrations/0007_is_human_decision.sql.
    is_human_decision: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
