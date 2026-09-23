"""Boucle d'orchestration headless (pivot, voir docs/DECISIONS.md) : invoque
`claude` en headless pour une tâche, relance automatiquement en cas
d'échec de tests, plafonné à `max_iterations` (jamais une tentative de
plus — la borne est structurelle, `range(...)`, pas un `if` contournable).

Réutilise sans réimplémenter : `cmd_task_test` (app/cli/main.py — checkout
de la branche candidate, pytest réel, ChangeProof, transition d'état,
dashboard) après CHAQUE itération, `classify_push` (app/push_classifier.py,
avec les chemins protégés du projet cible) en fin de boucle réussie,
`activate_task`/`deactivate_task_if_active` (app/active_task.py) pour
l'attribution task_id des hooks.

Invariant non négociable : ce module n'appelle JAMAIS `push_to_origin`,
directement ou indirectement — `engine sync push` reste l'unique porte
vers un push réel (voir README.md, docs/DECISIONS.md).

Hooks : avant la boucle, le fichier settings HERBERT du projet est
(re)déployé par app/hooks_deploy.py et passé à chaque invocation
(`--settings <fichier> --setting-sources ""`). Son SHA-256 est revérifié
après CHAQUE invocation (défense en profondeur : le fichier est hors du
répertoire de travail de l'agent, mais une commande Bash n'est pas
contrôlée par PathPolicy) — toute modification = BLOCKED.

Panne d'infrastructure (InvocationInfrastructureError : le process claude
n'a pas pu être lancé, ou a signalé l'échec d'authentification littéral
AUTH_FAILURE_LITERAL) : tâche BLOCKED avec la raison exacte, aucune
itération consommée, pytest non lancé, exception propagée à l'appelant.
"""
import argparse
import sqlite3

from app.active_task import activate_task, deactivate_task_if_active
from app.claude_headless import InvocationInfrastructureError, build_context_prompt, invoke_claude_headless
from app.cli.main import _regenerate_dashboard, cmd_task_test
from app.hooks_deploy import HooksDeployError, deploy_target_settings, sha256_file
from app.database.repository import (
    get_latest_audit_log_details,
    get_latest_test_result_for_task,
    get_project,
    get_task,
    insert_headless_iteration,
    list_headless_iterations_for_task,
)
from app.git_wrapper import EMPTY_TREE_SHA, GitWrapperError, diff_numstat_since
from app.logging_utils import append_jsonl_event
from app.models import HeadlessIteration, TestResultStatus
from app.models.enums import HeadlessInvocationStatus, LogStatus
from app.push_classifier import classify_push, resolve_blocked_patterns
from app.state_machine.service import transition_task
from app.state_machine.states import TaskState

# États depuis lesquels démarrer/reprendre une orchestration a un sens.
# BLOCKED/HUMAN_REQUIRED/DONE/PROMOTED/ROLLED_BACK exigent une décision
# humaine explicite avant de relancer une boucle automatique sur la même
# tâche — jamais une réactivation silencieuse.
_RESUMABLE_STATES = {TaskState.RECEIVED, TaskState.EXECUTING, TaskState.FAILED}

# Le process claude a TOURNÉ mais l'invocation est "allée mal" (par
# opposition à un vrai échec de tests) — consomme une itération sur le
# plafond (docs/DECISIONS.md, point 4c, amendé : un process qui ne démarre
# même pas n'est plus traité ici mais par InvocationInfrastructureError,
# fatale, sans itération consommée). NOT_EXECUTED n'est plus produit par
# invoke_claude_headless, conservé pour les lignes historiques.
_INVOCATION_PROBLEM_STATUSES = {
    HeadlessInvocationStatus.NOT_EXECUTED,
    HeadlessInvocationStatus.TIMED_OUT,
    HeadlessInvocationStatus.UNAVAILABLE,
    HeadlessInvocationStatus.INVOCATION_FAILED,
}


class HeadlessOrchestrationError(Exception):
    pass


def _headless_reason_for(invocation, iteration_number: int, max_iterations: int) -> str | None:
    if invocation.invocation_status == HeadlessInvocationStatus.TIMED_OUT:
        return f"itération {iteration_number}/{max_iterations} : {invocation.error_detail}"
    if invocation.invocation_status in _INVOCATION_PROBLEM_STATUSES:
        detail = invocation.error_detail or invocation.invocation_status.value
        return f"itération {iteration_number}/{max_iterations} : échec d'invocation ({detail})"
    return None


def _block(conn, task_id, logs_dir, reason: str, trigger: str, event: str, details: dict):
    transition_task(conn, task_id, TaskState.BLOCKED, reason=reason, is_human_decision=False)
    deactivate_task_if_active(conn, task_id, logs_dir, trigger=trigger)
    append_jsonl_event(
        logs_dir,
        component="headless_orchestrator",
        event=event,
        level="ERROR",
        status=LogStatus.BLOCKED.value,
        task_id=task_id,
        details={"reason": reason, **details},
    )
    _regenerate_dashboard(conn)


def run_headless_task(
    conn: sqlite3.Connection,
    task_id: str,
    logs_dir,
    model: str,
    *,
    executable: str,
    herbert_root,
    max_iterations: int = 3,
    timeout_seconds: int = 600,
):
    """`executable` : chemin absolu déjà validé par
    app/claude_headless.py::validate_claude_executable (au démarrage de la
    commande CLI). `herbert_root` : racine HERBERT sous laquelle vit le
    fichier settings du projet (data/target_settings/<project_id>/) et
    d'où les hooks sont référencés — obligatoire, jamais implicite."""
    task = get_task(conn, task_id)
    if task is None:
        raise HeadlessOrchestrationError(f"tâche introuvable: {task_id}")

    project = get_project(conn, task.project_id)
    if project is None:
        raise HeadlessOrchestrationError(f"projet introuvable pour cette tâche (project_id={task.project_id})")

    if task.status not in _RESUMABLE_STATES:
        raise HeadlessOrchestrationError(
            f"tâche en statut {task.status.value} : nécessite une décision humaine explicite "
            f"avant de relancer l'orchestration headless (états repris automatiquement : "
            f"{', '.join(s.value for s in _RESUMABLE_STATES)})"
        )

    branch_info = get_latest_audit_log_details(conn, task_id, "cli.task_branch")
    if not branch_info or not branch_info.get("branch"):
        raise HeadlessOrchestrationError(
            f"tâche {task_id} sans branche candidate — lancez `engine task branch --id {task_id}` d'abord"
        )
    candidate_branch = branch_info["branch"]
    base_commit = branch_info.get("base_commit")

    already_run = len(list_headless_iterations_for_task(conn, task_id))
    if already_run >= max_iterations:
        raise HeadlessOrchestrationError(
            f"tâche {task_id} : {already_run} itération(s) headless déjà consommée(s) sur {max_iterations} — "
            f"plafond atteint lors d'une exécution précédente, jamais de tentative supplémentaire silencieuse"
        )

    # Avant toute itération (et avant toute transition) : sans fichier
    # settings HERBERT valide, l'agent ne tourne jamais.
    try:
        deployment = deploy_target_settings(project, herbert_root, logs_dir)
    except HooksDeployError as exc:
        raise HeadlessOrchestrationError(
            f"hooks HERBERT non déployables pour ce projet ({exc}) — voir `engine init-hooks {project.path}`"
        ) from exc

    activate_task(conn, task_id, logs_dir)

    previous_failure = get_latest_test_result_for_task(conn, task_id)

    for i in range(already_run + 1, max_iterations + 1):
        task = get_task(conn, task_id)
        if task.status != TaskState.EXECUTING:
            transition_task(conn, task_id, TaskState.EXECUTING, is_human_decision=False)

        prompt = build_context_prompt(task.description, project, candidate_branch, previous_failure)
        try:
            invocation = invoke_claude_headless(
                prompt,
                cwd=project.path,
                model=model,
                timeout_seconds=timeout_seconds,
                executable=executable,
                settings_path=str(deployment.path),
            )
        except InvocationInfrastructureError as exc:
            _block(
                conn, task_id, logs_dir,
                reason=(
                    f"panne d'invocation (infrastructure, pas un échec de tâche) : {exc} — "
                    f"aucune itération consommée, pytest non lancé"
                ),
                trigger="headless_invocation_infrastructure",
                event="panne d'invocation headless (infrastructure)",
                details={"iteration_attempted": i, "executable": executable},
            )
            raise

        current_sha = sha256_file(deployment.path)
        if current_sha != deployment.sha256:
            # L'itération a bien eu lieu (l'agent a tourné) : elle est
            # enregistrée, mais son résultat n'est pas fiable — pytest n'est
            # pas lancé, la tâche est bloquée.
            insert_headless_iteration(
                conn,
                HeadlessIteration(
                    task_id=task_id,
                    iteration_number=i,
                    prompt_sent=prompt,
                    raw_result=invocation.raw_stdout,
                    session_id=invocation.session_id,
                    num_turns=invocation.num_turns,
                    invocation_status=invocation.invocation_status,
                    tests_passed=False,
                ),
            )
            _block(
                conn, task_id, logs_dir,
                reason=(
                    f"intégrité : fichier settings HERBERT modifié pendant l'itération {i}/{max_iterations} "
                    f"(sha256 attendu {deployment.sha256}, trouvé {current_sha}) — résultat non fiable, pytest non lancé"
                ),
                trigger="headless_settings_integrity",
                event="intégrité du fichier settings HERBERT rompue",
                details={"iteration": i, "path": str(deployment.path), "expected_sha256": deployment.sha256,
                         "found_sha256": current_sha},
            )
            return get_task(conn, task_id)

        headless_reason = _headless_reason_for(invocation, i, max_iterations)

        # Tourne quand même après un problème d'invocation : signal honnête
        # sur l'état réel du dépôt plutôt qu'un statut FAILED inventé sans
        # jamais avoir vérifié (voir docs/DECISIONS.md, point 4).
        cmd_task_test(argparse.Namespace(task_id=task_id, headless_reason=headless_reason))

        latest_test = get_latest_test_result_for_task(conn, task_id)
        tests_passed = bool(latest_test and latest_test.status == TestResultStatus.VERIFIED_PASS)

        insert_headless_iteration(
            conn,
            HeadlessIteration(
                task_id=task_id,
                iteration_number=i,
                prompt_sent=prompt,
                raw_result=invocation.raw_stdout,
                session_id=invocation.session_id,
                num_turns=invocation.num_turns,
                invocation_status=invocation.invocation_status,
                tests_passed=tests_passed,
            ),
        )

        append_jsonl_event(
            logs_dir,
            component="headless_orchestrator",
            event=f"itération headless {i}/{max_iterations} terminée",
            level="INFO" if tests_passed else "WARNING",
            status=invocation.invocation_status.value,
            task_id=task_id,
            details={
                "iteration": i,
                "max_iterations": max_iterations,
                "invocation_status": invocation.invocation_status.value,
                "tests_passed": tests_passed,
                "session_id": invocation.session_id,
                "num_turns": invocation.num_turns,
            },
        )

        task = get_task(conn, task_id)
        if task.status == TaskState.DONE:
            break

        previous_failure = latest_test

        if i == max_iterations:
            transition_task(
                conn,
                task_id,
                TaskState.BLOCKED,
                reason=f"{max_iterations} itération(s) headless épuisée(s) sans tests VERIFIED_PASS — jamais de 4e tentative",
                is_human_decision=False,
            )
            deactivate_task_if_active(conn, task_id, logs_dir, trigger="headless_max_iterations")
            _regenerate_dashboard(conn)
            return get_task(conn, task_id)

    # Boucle sortie via DONE : classification du diff du PROJET CIBLE (pas
    # REPO_ROOT). Chemins protégés = resolve_blocked_patterns(project) :
    # DEFAULT_BLOCKED_PATTERNS + extra_blocked_patterns de CE projet (relu
    # en base ici, pas l'objet chargé en début de boucle). Résolution
    # échouée (valeur stockée illisible, projet disparu) -> None ->
    # classify_push force MANUAL_REQUIRED, jamais AUTO par défaut. Voir
    # docs/DECISIONS.md, "classify_push : portée corrigée" (résolu).
    task = get_task(conn, task_id)
    blocked_patterns = resolve_blocked_patterns(get_project(conn, project.id))
    try:
        files_changed, total_diff_lines = diff_numstat_since(project.path, base_commit or EMPTY_TREE_SHA)
    except GitWrapperError as exc:
        append_jsonl_event(
            logs_dir,
            component="headless_orchestrator",
            event="classification push impossible après succès headless",
            level="ERROR",
            status=LogStatus.FAILED.value,
            task_id=task_id,
            details={"error": str(exc)},
        )
        return task

    classification = classify_push(
        files_changed, total_diff_lines, tests_passed=True, blocked_patterns=blocked_patterns
    )

    append_jsonl_event(
        logs_dir,
        component="headless_orchestrator",
        event="classification push (projet cible) après succès headless",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=task_id,
        details={
            "files_changed": files_changed,
            "total_diff_lines": total_diff_lines,
            "blocked_patterns_resolved": blocked_patterns is not None,
            "decision": classification.decision,
            "failed_criteria": classification.failed_criteria,
        },
    )

    if classification.decision == "MANUAL_REQUIRED":
        transition_task(
            conn,
            task_id,
            TaskState.HUMAN_REQUIRED,
            reason=f"classification push MANUAL_REQUIRED (projet cible): {'; '.join(classification.failed_criteria)}",
            is_human_decision=False,
        )
        deactivate_task_if_active(conn, task_id, logs_dir, trigger="headless_manual_required")
        task = get_task(conn, task_id)

    # AUTO : aucune action supplémentaire. La tâche reste DONE, active_task
    # reste actif — HERBERT ne pousse jamais rien ici, `engine sync push`
    # (portée HERBERT lui-même, inchangé) reste le seul chemin vers un push.
    _regenerate_dashboard(conn)
    return task
