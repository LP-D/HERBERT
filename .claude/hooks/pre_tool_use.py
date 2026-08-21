#!/usr/bin/env python3
"""Hook PreToolUse (matcher: Bash) pour Claude Code — ExecutionBroker light.

Reçoit sur stdin (ou en argument, pour les tests directs/pytest) le JSON
envoyé par Claude Code contenant tool_input.command. Vérifie ce texte contre
une liste de patterns dangereux ADDITIONNELLE à .claude/settings.json
(permissions.deny couvre déjà rm -rf, Remove-Item -Recurse -Force,
git push --force[-with-lease], reg, sudo, runas, accès .env/secrets).

Journalise la décision dans SQLite (table commands) AVANT de la renvoyer.
Exit code non-zéro + message stderr => commande bloquée par Claude Code.
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

# Patterns non couverts par permissions.deny de .claude/settings.json.
ADDITIONAL_DANGEROUS_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bdiskpart\b", re.IGNORECASE), "diskpart peut reformater/repartitionner un disque"),
    (re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE), "formatage de disque"),
    (
        re.compile(r"(del|erase|rd|rmdir)\s+(/s\s+/q|/q\s+/s)\s+[a-z]:\\?\s*$", re.IGNORECASE),
        "suppression récursive à la racine d'un lecteur",
    ),
    (
        re.compile(r"remove-item[^\n]*-recurse[^\n]*-force[^\n]*[a-z]:\\\s*$", re.IGNORECASE),
        "Remove-Item récursif ciblant la racine d'un lecteur",
    ),
    (
        re.compile(r"\bnet\s+(user|localgroup)\b[^\n]*\b(add|administrators)\b", re.IGNORECASE),
        "création/élévation de compte administrateur",
    ),
]

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
# commande (découpage sur les séparateurs shell &&, ||, ;, |).
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

    decision, reason = evaluate_command(command)
    task_id = event.get("task_id")

    try:
        config = _load_config()
        conn = _get_db_conn(config)
        insert_command_log(
            conn,
            CommandLogEntry(
                task_id=task_id,
                tool_name=tool_name,
                command=command,
                decision=decision,
                reason=reason,
            ),
        )
        conn.close()

        append_jsonl_event(
            REPO_ROOT / config["logs"]["dir"],
            component="hook.pre_tool_use",
            event=f"{tool_name} évalué",
            level="WARNING" if decision == CommandDecision.DENY else "INFO",
            status="BLOCKED" if decision == CommandDecision.DENY else "VERIFIED",
            task_id=task_id,
            details={"command": command, "decision": decision.value, "reason": reason},
        )
    except Exception as exc:  # journalisation best-effort : ne bloque jamais la décision elle-même
        sys.stderr.write(f"[herbert] avertissement: échec de journalisation: {exc}\n")

    if decision == CommandDecision.DENY:
        # Convention Claude Code (hooks.md, v2.1.235) : exit code 2 = blocage réel
        # avec message stderr renvoyé au modèle. Exit 1 = erreur non-bloquante,
        # l'action procéderait quand même (bug réel constaté et corrigé : voir
        # historique — un exit(1) ici avait laissé passer une commande deny-list).
        return 2, f"[herbert] commande bloquée: {reason}\ncommande: {command}"
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
