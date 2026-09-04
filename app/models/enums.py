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


class HeadlessInvocationStatus(str, Enum):
    """Statut réel d'une tentative `claude -p ... --output-format json`
    (app/claude_headless.py) — même philosophie honnête que TestResultStatus :
    distingue la cause plutôt qu'un simple succès/échec.

    VERIFIED         : JSON parsé, is_error=false — invocation réussie.
    INVOCATION_FAILED : JSON parsé, is_error=true (ex: erreur d'authentification,
                        erreur API côté Claude Code) — le process a tourné et
                        répondu, mais la session a échoué.
    TIMED_OUT         : subprocess tué après config.claude_headless.timeout_seconds.
    NOT_EXECUTED      : le binaire `claude` est introuvable/impossible à lancer
                        (FileNotFoundError/OSError) — même sémantique que
                        pytest_runner.py pour pytest absent.
    UNAVAILABLE       : le process s'est terminé mais stdout n'est pas un JSON
                        exploitable (absent, tronqué, ou sans les clés attendues).
    """

    VERIFIED = "VERIFIED"
    INVOCATION_FAILED = "INVOCATION_FAILED"
    TIMED_OUT = "TIMED_OUT"
    NOT_EXECUTED = "NOT_EXECUTED"
    UNAVAILABLE = "UNAVAILABLE"
