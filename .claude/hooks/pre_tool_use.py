#!/usr/bin/env python3
"""Hook PreToolUse (matcher: Bash|Write|Edit) pour Claude Code — ExecutionBroker light.

Reçoit sur stdin (ou en argument, pour les tests directs/pytest) le JSON
envoyé par Claude Code. Pour Bash : vérifie tool_input.command contre
CommandPolicy (app/policy/command_policy.py — source unique de vérité,
partagée avec .claude/settings.json, voir tests/unit/test_command_policy_sync.py).
Pour Write/Edit (ou tout appel fournissant tool_input.file_path) : vérifie
en plus PathPolicy (app/policy/path_policy.py) contre la racine du projet
concerné — déterminée via `cwd` envoyé par Claude Code (pas REPO_ROOT, qui
n'est que l'emplacement de ce script, potentiellement partagé entre
plusieurs projets).

Journalise la décision dans SQLite (table commands) ET logs/*.jsonl AVANT
de la renvoyer. Exit code 2 + message stderr => commande bloquée par
Claude Code (exit 1 = erreur non-bloquante, voir hooks.md v2.1.235).
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.config import load_config  # noqa: E402
from app.database.connection import get_connection  # noqa: E402
from app.database.migrate import apply_migrations  # noqa: E402
from app.database.repository import insert_command_log  # noqa: E402
from app.logging_utils import append_jsonl_event  # noqa: E402
from app.models import CommandLogEntry  # noqa: E402
from app.models.enums import CommandDecision  # noqa: E402
from app.policy.command_policy import generate_hook_patterns  # noqa: E402
from app.policy.path_policy import validate_path  # noqa: E402

# Dérivés de app/policy/command_policy.py — POLICY est la seule source de
# vérité, ne pas ajouter de pattern ici directement.
ADDITIONAL_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = generate_hook_patterns()

# Cas spécial : .claude/settings.json ne doit être bloqué que pour une
# ÉCRITURE (le lire, ex. `python -m json.tool .claude/settings.json`,
# `cat .claude/settings.json`, doit rester autorisé — faux positif réel
# constaté et corrigé, voir historique). Deuxième faux positif réel
# constaté ensuite : un `>` littéral dans du texte ("->", diagnostics)
# déclenchait le blocage même sans rapport avec settings.json, dès que
# les deux apparaissaient n'importe où dans une commande multi-segments
# (ex: `echo "... -> ..." && git diff .claude/settings.json`). Corrigé en
# (a) excluant `->` du détecteur de redirection, et (b) exigeant que le
# chemin ET l'indicateur d'écriture soient dans le MÊME segment de
# commande (découpage sur les séparateurs shell &&, ||, ;, |). Cette
# logique reste dédiée (pas une simple regex de POLICY) car aucune regex
# unique testée n'a évité ces deux faux positifs sans ce découpage.
SETTINGS_JSON_PATH_RE = re.compile(r"\.claude[\\/]settings\.json", re.IGNORECASE)
WRITE_INDICATOR_RE = re.compile(
    r"((?<!-)>{1,2}|Set-Content|Out-File|Add-Content|New-Item|\bcp\b|\bcopy\b|\bmv\b|\bmove\b|\bdel\b|\brm\b|Remove-Item|sed\s+-i)",
    re.IGNORECASE,
)
SHELL_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _targets_settings_json_for_write(command: str) -> bool:
    for segment in SHELL_SEGMENT_SPLIT_RE.split(command):
        if SETTINGS_JSON_PATH_RE.search(segment) and WRITE_INDICATOR_RE.search(segment):
            return True
    return False


def evaluate_command(command: str) -> tuple[CommandDecision, str]:
    if _targets_settings_json_for_write(command):
        return CommandDecision.DENY, "modification des règles de permission HERBERT elles-mêmes"

    for pattern, reason in ADDITIONAL_DANGEROUS_PATTERNS:
        if pattern.search(command):
            return CommandDecision.DENY, reason
    return CommandDecision.ALLOW, "aucun pattern dangereux additionnel détecté"


def _load_config():
    return load_config(REPO_ROOT / "config" / "system.yaml")


def _get_db_conn(config):
    db_path = REPO_ROOT / config["database"]["path"]
    conn = get_connection(db_path)
    apply_migrations(conn, REPO_ROOT / config["migrations"]["dir"])
    return conn


def handle_event(event: dict) -> tuple[int, str]:
    """Traite un événement PreToolUse et retourne (exit_code, message stderr)."""
    tool_name = event.get("tool_name", "") or "Bash"
    tool_input = event.get("tool_input", {}) or {}
    command = tool_input.get("command", "")
    file_path = tool_input.get("file_path")
    # `cwd` = racine du projet CONCERNÉ envoyée par Claude Code — pas
    # REPO_ROOT, qui n'est que l'emplacement de ce script et peut être
    # partagé entre plusieurs projets (voir docstring du module).
    project_root = event.get("cwd") or str(REPO_ROOT)
    task_id = event.get("task_id")

    decision, reason = evaluate_command(command) if command else (CommandDecision.ALLOW, "aucune commande Bash à évaluer")

    # Auto-protection de settings.json : un Edit/Write direct sur ce
    # fichier a le même effet qu'une écriture shell (`>>`, Set-Content...)
    # — même règle, sinon étendre le matcher à Write|Edit pour PathPolicy
    # créerait une fausse impression de protection sur ce point précis.
    if file_path and tool_name in ("Write", "Edit") and decision != CommandDecision.DENY:
        if file_path.replace("\\", "/").rstrip("/").endswith(".claude/settings.json"):
            decision = CommandDecision.DENY
            reason = "modification des règles de permission HERBERT elles-mêmes"

    path_result = None
    if file_path:
        path_result = validate_path(file_path, project_root)
        if not path_result.allowed and decision != CommandDecision.DENY:
            decision = CommandDecision.DENY
            reason = f"PathPolicy: {path_result.reason}"

    try:
        config = _load_config()
        conn = _get_db_conn(config)
        insert_command_log(
            conn,
            CommandLogEntry(
                task_id=task_id,
                tool_name=tool_name,
                command=command or (file_path or ""),
                decision=decision,
                reason=reason,
            ),
        )
        conn.close()

        details = {"command": command, "file_path": file_path, "decision": decision.value, "reason": reason}
        if path_result is not None:
            details["path_policy"] = {
                "allowed": path_result.allowed,
                "resolved_path": path_result.resolved_path,
                "is_symlink_or_junction": path_result.is_symlink_or_junction,
            }

        # Un lien symbolique/jonction est signalé même si le chemin est par
        # ailleurs autorisé — ce n'est pas bloquant en soi (voir path_policy.py).
        level = "WARNING" if decision == CommandDecision.DENY else "INFO"
        status = "BLOCKED" if decision == CommandDecision.DENY else "VERIFIED"
        if path_result is not None and path_result.is_symlink_or_junction and decision != CommandDecision.DENY:
            level = "WARNING"

        append_jsonl_event(
            REPO_ROOT / config["logs"]["dir"],
            component="hook.pre_tool_use",
            event=f"{tool_name} évalué",
            level=level,
            status=status,
            task_id=task_id,
            details=details,
        )
    except Exception as exc:  # journalisation best-effort : ne bloque jamais la décision elle-même
        sys.stderr.write(f"[herbert] avertissement: échec de journalisation: {exc}\n")

    if decision == CommandDecision.DENY:
        # Convention Claude Code (hooks.md, v2.1.235) : exit code 2 = blocage réel
        # avec message stderr renvoyé au modèle. Exit 1 = erreur non-bloquante,
        # l'action procéderait quand même (bug réel constaté et corrigé : voir
        # historique — un exit(1) ici avait laissé passer une commande deny-list).
        target = command or file_path or ""
        return 2, f"[herbert] commande bloquée: {reason}\ncommande: {target}"
    return 0, ""


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if argv:
        raw = argv[0]
        candidate_path = Path(raw)
        if candidate_path.exists():
            raw = candidate_path.read_text(encoding="utf-8")
    else:
        raw = sys.stdin.read()

    try:
        event = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"[herbert] JSON invalide reçu par le hook PreToolUse: {exc}\n")
        return 1

    exit_code, message = handle_event(event)
    if message:
        sys.stderr.write(message + "\n")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
