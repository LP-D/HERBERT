#!/usr/bin/env python3
"""Hook PostToolUse (matcher: Bash|Write|Edit) pour Claude Code.

Reçoit sur stdin (ou en argument, pour les tests directs/pytest) le JSON
envoyé par Claude Code après exécution d'un outil. Journalise le résultat
RÉEL (succès/échec, cible touchée) dans SQLite, table audit_log.
Ne bloque jamais (exit 0 systématique) : ce hook est un journal, pas un
filtre.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.config import load_config  # noqa: E402
from app.database.connection import get_connection  # noqa: E402
from app.database.migrate import apply_migrations  # noqa: E402
from app.database.repository import insert_audit_log  # noqa: E402
from app.logging_utils import append_jsonl_event  # noqa: E402
from app.models.enums import LogStatus  # noqa: E402


def _load_config():
    return load_config(REPO_ROOT / "config" / "system.yaml")


def _get_db_conn(config):
    db_path = REPO_ROOT / config["database"]["path"]
    conn = get_connection(db_path)
    apply_migrations(conn, REPO_ROOT / config["migrations"]["dir"])
    return conn


def _target_from_tool_input(tool_input: dict) -> str | None:
    return tool_input.get("file_path") or tool_input.get("command")


def handle_event(event: dict) -> dict:
    """Traite un événement PostToolUse, journalise dans audit_log et retourne
    le détail journalisé (utile pour les tests)."""
    tool_name = event.get("tool_name", "") or "unknown"
    tool_input = event.get("tool_input", {}) or {}
    tool_response = event.get("tool_response", {}) or {}
    task_id = event.get("task_id")

    is_error = bool(isinstance(tool_response, dict) and tool_response.get("is_error"))
    status = LogStatus.FAILED.value if is_error else LogStatus.VERIFIED.value
    level = "ERROR" if is_error else "INFO"
    target = _target_from_tool_input(tool_input)

    details = {
        "tool_name": tool_name,
        "target": target,
        "tool_response": tool_response,
    }

    config = _load_config()

    conn = _get_db_conn(config)
    insert_audit_log(
        conn,
        component="hook.post_tool_use",
        event=f"{tool_name} exécuté",
        level=level,
        status=status,
        task_id=task_id,
        details=details,
    )
    conn.close()

    append_jsonl_event(
        REPO_ROOT / config["logs"]["dir"],
        component="hook.post_tool_use",
        event=f"{tool_name} exécuté",
        level=level,
        status=status,
        task_id=task_id,
        details=details,
    )

    return {"status": status, "level": level, "target": target}


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
        sys.stderr.write(f"[herbert] JSON invalide reçu par le hook PostToolUse: {exc}\n")
        return 0  # journal uniquement : ne bloque jamais

    try:
        handle_event(event)
    except Exception as exc:
        sys.stderr.write(f"[herbert] avertissement: échec de journalisation SQLite: {exc}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
