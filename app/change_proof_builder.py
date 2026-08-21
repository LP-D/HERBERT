"""Construit un ChangeProof réel après `engine task test`, et détecte les
régressions par comparaison factuelle avec le dernier TestResult
VERIFIED_PASS connu pour le même projet — pas de scoring, juste une
comparaison des noms de tests avant/après. Un test qui échouait déjà
n'est jamais compté comme régression.

=====================================================================
LIMITE CONNUE ET NON MINEURE — ChangeProof.commands_executed
=====================================================================
Ce champ reste VIDE en usage courant. Il est peuplé via
`list_commands_for_task(conn, task.id)`, qui lit la table `commands` —
mais cette table n'est remplie que quand le hook PreToolUse reçoit un
`task_id` dans l'événement JSON envoyé par Claude Code. Claude Code n'a
aucune notion de "tâche HERBERT" : il n'envoie jamais ce champ. Il n'y a
donc, à ce stade (V0.2), AUCUN mécanisme reliant les commandes Bash
réellement exécutées pendant une session Claude Code à un task_id
HERBERT. Ce n'est pas une limite mineure parmi d'autres : c'est un champ
du contrat de données ChangeProof qui, dans l'usage réel actuel, ne se
peuplera jamais tout seul. Le combler demanderait un mécanisme de "tâche
active" (par ex. un fichier d'état lu par le hook, ou une variable
d'environnement positionnée par `engine task branch`) — explicitement
hors du périmètre V0.2 tel que défini.
=====================================================================
"""
import sqlite3

from app.database.repository import list_commands_for_task
from app.git_wrapper import GitWrapperError, diff_files_since
from app.models import ChangeProof, Project, Task, TestResult
from app.models.test_result import TestCaseOutcome, TestResultStatus
from app.state_machine.states import TaskState


def detect_regressions(previous: TestResult | None, current: TestResult) -> list[str]:
    """Noms de tests PASSED dans `previous` (dernier VERIFIED_PASS connu
    pour le projet) et FAILED/ERROR dans `current`. Vide si `previous` est
    None (rien à comparer) — pas une absence de test qui compte comme
    régression, seulement une comparaison factuelle test-par-test."""
    if previous is None:
        return []

    previously_passed = {tc.name for tc in previous.test_cases if tc.outcome == TestCaseOutcome.PASSED}
    now_broken = {
        tc.name for tc in current.test_cases if tc.outcome in (TestCaseOutcome.FAILED, TestCaseOutcome.ERROR)
    }
    return sorted(previously_passed & now_broken)


def build_change_proof(
    conn: sqlite3.Connection,
    task: Task,
    project: Project,
    test_result: TestResult,
    base_commit: str | None,
    regressions: list[str] | None = None,
) -> ChangeProof:
    files_changed: list[str] = []
    if base_commit:
        try:
            files_changed = diff_files_since(project.path, base_commit)
        except GitWrapperError:
            # dépôt du projet non trouvable/pas de git à cet endroit :
            # ne pas inventer une liste de fichiers, la laisser vide et
            # documenter via l'absence plutôt qu'une fausse valeur.
            files_changed = []

    # Voir le docstring du module : reste vide en usage courant, Claude
    # Code n'envoyant jamais de task_id au hook PreToolUse.
    commands_executed = list_commands_for_task(conn, task.id)

    status = TaskState.DONE if test_result.status == TestResultStatus.VERIFIED_PASS else TaskState.FAILED

    return ChangeProof(
        task_id=task.id,
        files_changed=files_changed,
        commands_executed=commands_executed,
        tests_passed=test_result.passed,
        tests_failed=test_result.failed + test_result.errors,
        regressions=regressions or [],
        status=status,
    )
