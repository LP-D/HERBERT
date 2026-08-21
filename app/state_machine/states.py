from enum import Enum


class TaskState(str, Enum):
    RECEIVED = "RECEIVED"
    EXECUTING = "EXECUTING"
    TESTING = "TESTING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
