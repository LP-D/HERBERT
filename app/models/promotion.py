import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class PromotionStatus(str, Enum):
    MERGED_PENDING_HEALTH_CHECK = "MERGED_PENDING_HEALTH_CHECK"
    HEALTH_CHECK_PASSED = "HEALTH_CHECK_PASSED"
    AUTO_ROLLBACK = "AUTO_ROLLBACK"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    MERGE_FAILED = "MERGE_FAILED"


class Promotion(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    stable_branch: str
    candidate_branch: str
    commit_before: str
    commit_after: str
    status: PromotionStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
