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
# ÉCRITURE réellement dirigée vers CE fichier précis (chemin canonique
# résolu par rapport à project_root), jamais sur une simple
# correspondance textuelle "la commande contient un indicateur d'écriture
# ET la chaîne settings.json quelque part". Trois faux positifs réels
# constatés avec l'ancienne approche textuelle (voir tests_pre_tool_use_hook.py
# pour les régressions correspondantes) :
#   1. Lecture pure (`python -m json.tool .claude/settings.json`) —
#      corrigé une première fois en exigeant un indicateur d'écriture,
#      mais ça restait fragile face aux deux cas suivants.
#   2. "settings.json" apparaissant dans le CONTENU écrit (heredoc) vers
#      un AUTRE fichier settings.json (autre projet) — la cible réelle de
#      l'écriture (le token qui suit l'opérateur de redirection) n'était
#      jamais isolée du reste de la commande/du contenu.
#   3. `2>&1` (duplication de descripteur de fichier, pas une écriture
#      fichier) confondu avec une redirection `>` réelle simplement parce
#      qu'il contient le caractère '>'.
# La logique ci-dessous isole donc la CIBLE réelle de chaque opération
# d'écriture repérée (redirection shell, ou commande à chemin explicite
# type Set-Content/Remove-Item/cp/mv/sed -i), la résout en chemin
# canonique par rapport à project_root, et ne bloque que si elle est
# EXACTEMENT .claude/settings.json de CE projet.
SHELL_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")

# `>` / `>>` (avec préfixe optionnel de descripteur de fichier numérique,
# ex. `2>`) — jamais précédé de `-` (exclut `->` dans du texte) — ou
# `&>` / `&>>` (redirection combinée stdout+stderr vers un fichier réel).
_REDIRECT_OP_RE = re.compile(r"(?<!-)(?:\d{0,2}>{1,2}|&>{1,2})")
# `2>&1`, `>&2`, ... : duplication de descripteur vers un AUTRE descripteur,
# jamais un fichier — ne doit jamais être traité comme une cible d'écriture.
_FD_DUP_RE = re.compile(r"\s*&\d+\b")
_TARGET_TOKEN_RE = re.compile(r"\s*([^\s&|;<>]+)")

_PATH_FLAG_RE = re.compile(r"^-(?:path|literalpath|filepath)$", re.IGNORECASE)
# Commandes où le chemin cible est le DERNIER argument positionnel
# (destination d'une copie/déplacement, ou fichier écrit par le cmdlet).
_WRITE_DESTINATION_COMMANDS = {"set-content", "out-file", "add-content", "new-item", "cp", "copy", "mv", "move"}
# Commandes où TOUS les arguments positionnels sont des cibles (suppression).
_WRITE_DELETE_COMMANDS = {"remove-item", "ri", "rm", "del", "erase"}


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _tokenize(segment: str) -> list[str]:
    return re.findall(r"'[^']*'|\"[^\"]*\"|\S+", segment)


def _extract_redirect_targets(segment: str) -> list[str]:
    """Cible réelle de chaque `>`/`>>`/`&>`/`&>>` du segment — jamais une
    duplication de descripteur (`2>&1`, `>&2`, ...), qui n'écrit dans aucun
    fichier."""
    targets = []
    for match in _REDIRECT_OP_RE.finditer(segment):
        op_text = match.group(0)
        rest = segment[match.end():]
        if not op_text.startswith("&"):
            if _FD_DUP_RE.match(rest):
                continue
            rest = re.sub(r"^\s*&", "", rest)  # `N>&fichier` (bash) : flux combinés vers un vrai fichier
        target_match = _TARGET_TOKEN_RE.match(rest)
        if target_match:
            targets.append(_strip_quotes(target_match.group(1)))
    return targets


def _command_basename(token: str) -> str:
    name = _strip_quotes(token).replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _extract_command_write_targets(segment: str) -> list[str]:
    """Cible réelle des commandes à chemin de fichier explicite
    (Set-Content, Remove-Item, cp/mv, sed -i, ...) — jamais une
    correspondance textuelle brute sur tout le segment."""
    tokens = _tokenize(segment)
    targets: list[str] = []
    for idx, raw in enumerate(tokens):
        cmd = _command_basename(raw)
        is_sed_inplace = cmd == "sed" and any(_strip_quotes(t).startswith("-i") for t in tokens[idx + 1 : idx + 3])
        if cmd not in _WRITE_DESTINATION_COMMANDS and cmd not in _WRITE_DELETE_COMMANDS and not is_sed_inplace:
            continue

        rest = tokens[idx + 1 :]
        for pos, tok in enumerate(rest):
            if _PATH_FLAG_RE.match(_strip_quotes(tok)) and pos + 1 < len(rest):
                targets.append(_strip_quotes(rest[pos + 1]))

        positional = [_strip_quotes(t) for t in rest if not t.startswith("-")]
        if not positional:
            continue
        if cmd in _WRITE_DELETE_COMMANDS:
            targets.extend(positional)
        else:
            targets.append(positional[-1])
    return targets


def _resolve(candidate: str, project_root: str) -> Path | None:
    candidate = candidate.strip()
    if not candidate:
        return None
    try:
        path = Path(candidate)
        if not path.is_absolute():
            path = Path(project_root) / path
        return path.resolve()
    except (OSError, ValueError):
        return None


def _is_protected_settings_json(candidate: str, project_root: str) -> bool:
    """Vrai seulement si `candidate`, résolu par rapport à project_root,
    est EXACTEMENT .claude/settings.json DE CE PROJET — jamais un autre
    fichier settings.json (autre projet, autre chemin), jamais une simple
    correspondance de sous-chaîne."""
    resolved = _resolve(candidate, project_root)
    if resolved is None:
        return False
    protected = (Path(project_root) / ".claude" / "settings.json").resolve()
    return resolved == protected


def _targets_settings_json_for_write(command: str, project_root: str) -> bool:
    for segment in SHELL_SEGMENT_SPLIT_RE.split(command):
        candidates = _extract_redirect_targets(segment) + _extract_command_write_targets(segment)
        if any(_is_protected_settings_json(c, project_root) for c in candidates):
            return True
    return False


def evaluate_command(command: str, project_root: str) -> tuple[CommandDecision, str]:
    if _targets_settings_json_for_write(command, project_root):
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

    decision, reason = (
        evaluate_command(command, project_root) if command else (CommandDecision.ALLOW, "aucune commande Bash à évaluer")
    )

    # Auto-protection de settings.json : un Edit/Write direct sur ce
    # fichier a le même effet qu'une écriture shell (`>>`, Set-Content...)
    # — même règle, sinon étendre le matcher à Write|Edit pour PathPolicy
    # créerait une fausse impression de protection sur ce point précis.
    # Même résolution canonique que côté Bash (pas une correspondance de
    # suffixe textuel) : un file_path absolu pointant vers le
    # .claude/settings.json d'un AUTRE projet ne doit pas être bloqué ici.
    if file_path and tool_name in ("Write", "Edit") and decision != CommandDecision.DENY:
        if _is_protected_settings_json(file_path, project_root):
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
