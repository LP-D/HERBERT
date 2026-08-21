import uuid
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class TestCaseOutcome(str, Enum):
    __test__ = False  # évite un avertissement de collecte pytest (nom en "Test")
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    SKIPPED = "SKIPPED"


class TestResultStatus(str, Enum):
    __test__ = False
    VERIFIED_PASS = "VERIFIED_PASS"
    VERIFIED_FAIL = "VERIFIED_FAIL"
    NOT_EXECUTED = "NOT_EXECUTED"
    UNAVAILABLE = "UNAVAILABLE"


class TestCaseResult(BaseModel):
    __test__ = False
    name: str
    outcome: TestCaseOutcome


class TestResult(BaseModel):
    __test__ = False
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str
    status: TestResultStatus
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    duration_seconds: float = 0.0
    raw_output: str = ""
    test_cases: list[TestCaseResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
