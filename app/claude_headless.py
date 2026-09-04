"""Construction du prompt contextuel + invocation headless de `claude`
(pivot orchestration, voir docs/DECISIONS.md) pour
app/headless_orchestrator.py.

Contexte minimal et ciblé (tâche, projet, branche candidate, échec
précédent le cas échéant) — jamais de prompt gigantesque, jamais de
référence à une source externe non lisible par ce code lui-même (voir
docs/DECISIONS.md : aucune citation de document non vérifiable par
HERBERT).

Le modèle est TOUJOURS fixé explicitement (jamais le défaut implicite du
CLI `claude`, qui dépend de ~/.claude/settings.json et peut être "haiku" —
insuffisant pour une boucle de correction de code réelle). Voir
config/system.yaml, section claude_headless.model.

Statuts honnêtes (HeadlessInvocationStatus, app/models/enums.py) : le
process peut échouer de plusieurs façons distinctes (binaire introuvable,
timeout, sortie non-JSON, erreur applicative renvoyée par Claude Code
lui-même) — jamais réduites à un simple booléen, pour rester
diagnosticable (voir app/pytest_runner.py pour le même principe côté
tests).
"""
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from app.models import Project, TestResult
from app.models.enums import HeadlessInvocationStatus

# Champs minimaux dont la présence a été vérifiée empiriquement le
# 2026-09-04 sur une réponse RÉELLE de `claude --output-format json`
# (cas d'échec d'authentification — voir docs/DECISIONS.md, aucune
# réponse de succès n'a pu être calibrée dans cet environnement, auth
# absente). Une sortie qui ne contient pas au moins "type" et "is_error"
# est traitée comme UNAVAILABLE plutôt que supposée conforme.
_REQUIRED_JSON_KEYS = ("type", "is_error")


@dataclass
class HeadlessInvocationResult:
    invocation_status: HeadlessInvocationStatus
    is_error: bool | None = None
    result_text: str | None = None
    session_id: str | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    duration_api_ms: int | None = None
    total_cost_usd: float | None = None
    terminal_reason: str | None = None
    stop_reason: str | None = None
    permission_denials: list = field(default_factory=list)
    raw_stdout: str = ""
    error_detail: str | None = None


def build_context_prompt(
    task_description: str,
    project: Project,
    candidate_branch: str,
    previous_failure: TestResult | None = None,
) -> str:
    """Contexte pertinent et borné — jamais gigantesque. `previous_failure`
    fourni seulement à partir de la 2e itération (voir
    app/headless_orchestrator.py)."""
    lines = [
        f"Tâche HERBERT : {task_description}",
        f"Répertoire du projet : {project.path}",
        f"Branche candidate active (déjà checkoutée) : {candidate_branch}",
        "",
        "Contraintes : ne modifie que ce qui est nécessaire pour cette tâche. "
        "Les tests du projet doivent passer (`pytest`) à la fin de ton intervention. "
        "Committe ton travail sur cette branche avant de terminer (git commit) — "
        "HERBERT évalue le diff commité sur cette branche, pas les changements non commités.",
    ]

    if previous_failure is not None:
        failed_names = [tc.name for tc in previous_failure.test_cases if tc.outcome.value in ("FAILED", "ERROR")]
        lines += [
            "",
            "Tentative précédente : les tests ont échoué. Corrige le problème réel, "
            "ne contourne pas les tests (pas de skip, pas d'assertion affaiblie sans justification).",
            f"Tests en échec ({len(failed_names)}) : {', '.join(failed_names) or 'liste indisponible'}",
        ]
        if previous_failure.raw_output:
            # Déjà tronqué en amont par pytest_runner.py (max_output_chars) —
            # pas de troncature supplémentaire ici, juste un rappel du format.
            lines.append(f"Extrait de la sortie pytest précédente :\n{previous_failure.raw_output[:2000]}")

    return "\n".join(lines)


def invoke_claude_headless(
    prompt: str,
    cwd: str | Path,
    model: str,
    timeout_seconds: int,
    permission_mode: str = "bypassPermissions",
) -> HeadlessInvocationResult:
    """Lance `claude -p <prompt> --output-format json --model <model>
    --permission-mode <permission_mode>` dans `cwd`, capture et parse la
    sortie structurée.

    Ne lève jamais : toute défaillance (binaire absent, timeout, JSON
    illisible) retourne un HeadlessInvocationResult avec un
    invocation_status explicite — même pattern que
    app/pytest_runner.py::run_pytest_for_project.

    Les hooks PreToolUse/PostToolUse du projet cible (.claude/settings.json,
    voir docs/DEPLOYMENT.md) s'appliquent identiquement en mode headless
    qu'en session interactive — `--permission-mode` ne désactive que les
    invites de confirmation natives de Claude Code, pas les hooks (vérifié
    empiriquement, voir le test dédié dans tests/unit/test_claude_headless.py
    et le test hooks-en-headless-via-HERBERT)."""
    argv = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "json",
        "--model",
        model,
        "--permission-mode",
        permission_mode,
    ]

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run tue le process avant de lever cette exception
        # (comportement garanti par la stdlib, pas d'action de nettoyage
        # supplémentaire nécessaire côté HERBERT) — aucun risque de zombie.
        partial_stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.TIMED_OUT,
            raw_stdout=partial_stdout,
            error_detail=f"invocation headless expirée après {timeout_seconds}s",
        )
    except (FileNotFoundError, OSError) as exc:
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.NOT_EXECUTED,
            error_detail=f"impossible de lancer claude: {exc}",
        )

    raw_stdout = proc.stdout or ""

    try:
        payload = json.loads(raw_stdout)
    except (json.JSONDecodeError, ValueError):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.UNAVAILABLE,
            raw_stdout=raw_stdout,
            error_detail="sortie non-JSON ou vide (voir raw_stdout)",
        )

    if not isinstance(payload, dict) or not all(k in payload for k in _REQUIRED_JSON_KEYS):
        return HeadlessInvocationResult(
            invocation_status=HeadlessInvocationStatus.UNAVAILABLE,
            raw_stdout=raw_stdout,
            error_detail=f"JSON sans les clés attendues {_REQUIRED_JSON_KEYS}",
        )

    is_error = bool(payload.get("is_error"))
    status = HeadlessInvocationStatus.INVOCATION_FAILED if is_error else HeadlessInvocationStatus.VERIFIED

    return HeadlessInvocationResult(
        invocation_status=status,
        is_error=is_error,
        result_text=payload.get("result"),
        session_id=payload.get("session_id"),
        num_turns=payload.get("num_turns"),
        duration_ms=payload.get("duration_ms"),
        duration_api_ms=payload.get("duration_api_ms"),
        total_cost_usd=payload.get("total_cost_usd"),
        terminal_reason=payload.get("terminal_reason"),
        stop_reason=payload.get("stop_reason"),
        permission_denials=payload.get("permission_denials") or [],
        raw_stdout=raw_stdout,
        error_detail=payload.get("result") if is_error else None,
    )
