from enum import Enum


class CommandDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"


class LogStatus(str, Enum):
    """Statuts obligatoires (règle NO FAKE) pour toute affirmation de résultat."""

    VERIFIED = "VERIFIED"
    CLAIMED = "CLAIMED"
    ESTIMATED = "ESTIMATED"
    UNAVAILABLE = "UNAVAILABLE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
