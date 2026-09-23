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

Exécutable : TOUJOURS le binaire natif `claude.exe` par son chemin ABSOLU
(config/system.yaml, clé claude_headless.executable), validé une fois au
démarrage de `engine task run-headless` par validate_claude_executable()
— jamais `claude` nu (sans shell=True, Windows ne complète qu'en `.exe` :
FileNotFoundError systématique sur une installation npm), jamais
`claude.cmd` (le lanceur npm passe par cmd.exe, qui tronque TOUT ce qui
suit le premier retour à la ligne d'un argument, y compris les options
suivantes, silencieusement, code de sortie 0). Voir docs/DECISIONS.md.

Isolation : `--settings <fichier HERBERT> --setting-sources ""` — seul le
fichier généré par app/hooks_deploy.py est chargé ; aucun settings.json
ni settings.local.json du projet cible (ni ~/.claude/settings.json) ne
peut désactiver les hooks HERBERT (disableAllHooks). Établi par
expériences réelles E0-E11, voir docs/DECISIONS.md.

Deux catégories d'échec, jamais confondues :
- panne d'INFRASTRUCTURE (le process n'a pas pu être lancé :
  FileNotFoundError/OSError, exécutable ou settings non absolus/absents ;
  OU il a tourné mais signale l'échec d'authentification
  AUTH_FAILURE_LITERAL, motif littéral unique)
  -> InvocationInfrastructureError levée, fatale pour la boucle ;
- tout autre cas où le process a tourné -> HeadlessInvocationResult avec
  un statut honnête (VERIFIED, INVOCATION_FAILED, TIMED_OUT, UNAVAILABLE),
  comme avant — y compris tout autre is_error.
`NOT_EXECUTED` n'est plus produit ici (conservé dans l'enum pour les
lignes historiques de headless_iterations).
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

# Échec d'authentification = panne d'infrastructure (action humaine
# `claude auth login` requise, relancer l'agent ne peut rien changer).
# Motif LITTÉRAL unique, strict : présent tel quel dans le binaire
# claude.exe 2.1.235 ET observé réellement (3 fois, 2026-09-05 et
# 2026-09-23) dans le champ `result` d'une sortie `-p --output-format json`
# avec is_error=true. Les autres chaînes d'échec d'auth du binaire
# ("Failed to authenticate. ${...}: ${...}", "... through the broker: ...")
# n'ont jamais été observées dans ce contexte : volontairement NON incluses
# (jamais un is_error générique, jamais un motif deviné).
AUTH_FAILURE_LITERAL = "Failed to authenticate: OAuth session expired and could not be refreshed"

CONFIG_KEY_EXECUTABLE = "claude_headless.executable"
_VERSION_CHECK_TIMEOUT_SECONDS = 60


class ClaudeExecutableConfigError(Exception):
    """Configuration de l'exécutable invalide : fatale au démarrage, avant
    de toucher la moindre tâche."""


class InvocationInfrastructureError(Exception):
    """Le process `claude` n'a pas pu être lancé : panne d'invocation, pas
    un échec de tâche — ne consomme aucune itération, ne lance pas pytest."""


@dataclass(frozen=True)
class ClaudeExecutable:
    path: str
    version: str


def validate_claude_executable(configured) -> ClaudeExecutable:
    """Vérifie RÉELLEMENT l'exécutable configuré : chemin absolu, fichier
    existant, extension .exe, puis `<exe> --version` exécuté et capturé
    (code 0 + sortie non vide) — jamais supposé. Lève
    ClaudeExecutableConfigError, message nommant la clé attendue."""
    hint = (
        f"renseignez `{CONFIG_KEY_EXECUTABLE}` dans config/system.yaml avec le chemin ABSOLU du binaire natif "
        "claude.exe (ex. <npm root -g>/@anthropic-ai/claude-code/bin/claude.exe) — jamais `claude` nu ni claude.cmd"
    )
    if configured is None or not str(configured).strip():
        raise ClaudeExecutableConfigError(f"clé `{CONFIG_KEY_EXECUTABLE}` absente ou vide — {hint}")
    path = Path(str(configured).strip())
    if not path.is_absolute():
        raise ClaudeExecutableConfigError(f"`{CONFIG_KEY_EXECUTABLE}` n'est pas un chemin absolu ({path}) — {hint}")
    if path.suffix.lower() != ".exe":
        raise ClaudeExecutableConfigError(
            f"`{CONFIG_KEY_EXECUTABLE}` doit viser un .exe, pas {path.name} (un .cmd tronque le prompt "
            f"au premier retour à la ligne) — {hint}"
        )
    if not path.is_file():
        raise ClaudeExecutableConfigError(f"`{CONFIG_KEY_EXECUTABLE}` introuvable sur le disque ({path}) — {hint}")
    try:
        proc = subprocess.run(
            [str(path), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_VERSION_CHECK_TIMEOUT_SECONDS,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaudeExecutableConfigError(f"`{path} --version` n'a pas pu s'exécuter : {exc} — {hint}") from exc
    version = (proc.stdout or "").strip()
    if proc.returncode != 0 or not version:
        raise ClaudeExecutableConfigError(
            f"`{path} --version` a échoué (code {proc.returncode}, stdout={version!r}, "
            f"stderr={(proc.stderr or '').strip()[:200]!r}) — {hint}"
        )
    return ClaudeExecutable(path=str(path), version=version)


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
    *,
    executable: str,
    settings_path: str | Path,
    permission_mode: str = "bypassPermissions",
) -> HeadlessInvocationResult:
    """Lance le binaire `executable` (déjà validé par
    validate_claude_executable) dans `cwd` avec une LISTE d'arguments, jamais
    via un shell : les hooks chargés sont EXCLUSIVEMENT ceux de
    `settings_path` (fichier HERBERT, voir app/hooks_deploy.py) grâce à
    `--setting-sources ""`.

    Lève InvocationInfrastructureError si le process ne peut pas être lancé
    (ou si l'exécutable/les settings ne sont pas des chemins absolus
    existants : lancer l'agent sans les hooks HERBERT n'est jamais une
    option). Sinon ne lève pas : timeout, JSON illisible, erreur renvoyée
    par Claude Code -> HeadlessInvocationResult avec un statut explicite
    (même pattern que app/pytest_runner.py::run_pytest_for_project)."""
    exe = Path(executable)
    settings = Path(settings_path)
    if not exe.is_absolute():
        raise InvocationInfrastructureError(f"exécutable claude non absolu ({executable}) — jamais `claude` nu")
    if not settings.is_absolute() or not settings.is_file():
        raise InvocationInfrastructureError(
            f"fichier settings HERBERT absent ou non absolu ({settings_path}) — refus de lancer l'agent sans hooks"
        )

    argv = [
        str(exe),
        "-p",
        prompt,
        "--settings",
        str(settings),
        "--setting-sources",
        "",
        "--output-format",
        "json",
        "--permission-mode",
        permission_mode,
        "--model",
        model,
    ]

    try:
        proc = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
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
    except OSError as exc:  # FileNotFoundError, PermissionError, cwd invalide... : le process n'existe pas
        raise InvocationInfrastructureError(f"impossible de lancer {exe}: {type(exc).__name__}: {exc}") from exc

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
    result_text = payload.get("result")
    if is_error and isinstance(result_text, str) and AUTH_FAILURE_LITERAL in result_text:
        raise InvocationInfrastructureError(
            f"échec d'authentification du CLI claude ({result_text.strip()[:200]!r}) — "
            "`claude auth login` requis (action humaine), relancer l'agent ne changerait rien"
        )
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
