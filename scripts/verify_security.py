"""Vérification de sécurité consolidée et rejouable en une commande.

Reproduit, de façon automatisée, les tests effectués manuellement pendant
la mise au point du hook PreToolUse (.claude/hooks/pre_tool_use.py) :
blocage des patterns dangereux ADDITIONNELS que ce hook vérifie lui-même
(diskpart, formatage de disque, écriture sur .claude/settings.json,
création de compte administrateur), non-régression des deux faux
positifs trouvés et corrigés en session (lecture de settings.json bloquée
à tort ; texte contenant "->" déclenchant un blocage sans rapport), et
journalisation cohérente dans SQLite (table commands) ET logs/*.jsonl.

HORS SCOPE DÉLIBÉRÉ : les patterns déjà couverts par permissions.deny
dans .claude/settings.json (rm -rf, git push --force, git push
--force-with-lease, reg, sudo, runas, accès .env/secrets) ne sont PAS
testés ici. Ce hook Python les délègue volontairement au moteur de
permissions natif de Claude Code (voir le docstring de pre_tool_use.py) :
appeler handle_event() directement, comme fait ici, ne passe jamais par
ce moteur-là. La seule façon réelle de vérifier que permissions.deny
bloque effectivement une commande est une VRAIE session Claude Code —
voir docs/SMOKE_TEST_CLAUDE_CODE.md, qui documente ce protocole manuel.

Utilisation :
    python scripts/verify_security.py        # run direct, imprime VERIFIED/FAILED
    pytest tests/unit/test_security_consolidated.py
    python engine.py doctor --security       # intégré au doctor existant
"""
import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
REAL_SETTINGS_JSON = REPO_ROOT / ".claude" / "settings.json"

sys.path.insert(0, str(REPO_ROOT))


@dataclass
class CheckResult:
    name: str
    status: str  # VERIFIED | FAILED
    detail: str


def _load_module_from_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_hooks():
    pre = _load_module_from_path(REPO_ROOT / ".claude" / "hooks" / "pre_tool_use.py", "verify_pre_tool_use")
    post = _load_module_from_path(REPO_ROOT / ".claude" / "hooks" / "post_tool_use.py", "verify_post_tool_use")
    return pre, post


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_jsonl_events(logs_dir: Path) -> list[dict]:
    events = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def check_blocks_additional_pattern(pre_module, command: str, expected_reason_snippet: str) -> CheckResult:
    event = {"tool_name": "Bash", "tool_input": {"command": command}}
    exit_code, message = pre_module.handle_event(event)
    name = f"bloque (exit 2): {command[:50]}"
    if exit_code != 2:
        return CheckResult(name, "FAILED", f"exit_code={exit_code} (attendu 2), message={message!r}")
    if expected_reason_snippet.lower() not in message.lower():
        return CheckResult(name, "FAILED", f"message ne contient pas '{expected_reason_snippet}': {message!r}")
    return CheckResult(name, "VERIFIED", message.strip().replace("\n", " | "))


def check_allows_command(pre_module, command: str, label: str) -> CheckResult:
    event = {"tool_name": "Bash", "tool_input": {"command": command}}
    exit_code, message = pre_module.handle_event(event)
    name = f"autorise (exit 0): {label}"
    if exit_code != 0:
        return CheckResult(name, "FAILED", f"exit_code={exit_code} (attendu 0), message={message!r}")
    return CheckResult(name, "VERIFIED", "exit 0, aucun message de blocage")


def check_write_settings_json_blocked_and_file_untouched(pre_module) -> CheckResult:
    """Preuve sur le VRAI fichier .claude/settings.json (pas une copie) :
    hash SHA-256 avant/après l'appel du validateur, pour prouver qu'aucune
    modification n'a eu lieu."""
    name = "écriture réelle sur settings.json bloquée + hash SHA-256 inchangé"
    if not REAL_SETTINGS_JSON.exists():
        return CheckResult(name, "FAILED", "settings.json réel introuvable")

    before = _sha256(REAL_SETTINGS_JSON)
    event = {"tool_name": "Bash", "tool_input": {"command": 'echo "x" >> .claude/settings.json'}}
    exit_code, message = pre_module.handle_event(event)
    after = _sha256(REAL_SETTINGS_JSON)

    if exit_code != 2:
        return CheckResult(name, "FAILED", f"exit_code={exit_code} (attendu 2)")
    if before != after:
        return CheckResult(name, "FAILED", f"hash modifié: {before} -> {after}")
    return CheckResult(name, "VERIFIED", f"exit=2, hash SHA-256 identique ({before[:16]}...)")


def check_logging_sqlite_and_jsonl(pre_module, isolated_root: Path, command: str, expected_decision: str) -> CheckResult:
    from app.database.connection import get_connection

    name = f"journalisation SQLite+JSONL cohérente: {command[:40]}"
    event = {"tool_name": "Bash", "tool_input": {"command": command}}
    pre_module.handle_event(event)

    conn = get_connection(isolated_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT decision, created_at FROM commands WHERE command = ? ORDER BY created_at DESC LIMIT 1",
        (command,),
    ).fetchone()
    conn.close()

    if row is None:
        return CheckResult(name, "FAILED", "aucune ligne dans la table commands")
    if row["decision"] != expected_decision:
        return CheckResult(name, "FAILED", f"decision={row['decision']} (attendu {expected_decision})")

    events = _read_jsonl_events(isolated_root / "logs")
    matching = [e for e in events if e.get("details", {}).get("command") == command]
    if not matching:
        return CheckResult(name, "FAILED", "aucune ligne correspondante dans logs/*.jsonl")

    return CheckResult(
        name,
        "VERIFIED",
        f"decision={row['decision']}, sqlite@{row['created_at']}, jsonl@{matching[-1]['timestamp']}",
    )


def check_post_tool_use_logging(post_module, isolated_root: Path) -> CheckResult:
    from app.database.connection import get_connection
    from app.database.repository import insert_project, insert_task
    from app.models import Project, Task

    name = "PostToolUse journalise dans audit_log + logs/*.jsonl"

    conn = get_connection(isolated_root / "data" / "herbert.db")
    project = Project(name="verify-security-project", path="C:/verify-security")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche de vérification sécurité consolidée")
    insert_task(conn, task)
    conn.close()

    event = {
        "tool_name": "Write",
        "task_id": task.id,
        "tool_input": {"file_path": "app/models/task.py"},
        "tool_response": {"is_error": False},
    }
    post_module.handle_event(event)

    conn = get_connection(isolated_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT status, created_at FROM audit_log WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
        (task.id,),
    ).fetchone()
    conn.close()

    if row is None or row["status"] != "VERIFIED":
        return CheckResult(name, "FAILED", f"row={dict(row) if row else None}")

    events = _read_jsonl_events(isolated_root / "logs")
    matching = [e for e in events if e.get("task_id") == task.id and e.get("component") == "hook.post_tool_use"]
    if not matching:
        return CheckResult(name, "FAILED", "audit_log OK mais aucune trace JSONL correspondante")

    return CheckResult(name, "VERIFIED", f"status={row['status']}, jsonl@{matching[-1]['timestamp']}")


def run_all_checks(work_dir: Path | None = None) -> list[CheckResult]:
    """Exécute toutes les vérifications. Si work_dir est fourni (ex: le
    tmp_path de pytest), l'utilise et laisse l'appelant le nettoyer.
    Sinon crée et nettoie son propre répertoire temporaire."""
    own_temp = work_dir is None
    if own_temp:
        work_dir = Path(tempfile.mkdtemp(prefix="herbert_verify_security_"))

    try:
        shutil.copytree(MIGRATIONS_DIR, work_dir / "migrations", dirs_exist_ok=True)
        isolated_root = work_dir

        pre_module, post_module = load_hooks()
        pre_module.REPO_ROOT = isolated_root
        post_module.REPO_ROOT = isolated_root

        results: list[CheckResult] = [
            check_blocks_additional_pattern(pre_module, "diskpart /s wipe.txt", "diskpart"),
            check_blocks_additional_pattern(pre_module, "format c:", "formatage"),
            check_blocks_additional_pattern(
                pre_module, 'echo "{}" >> .claude/settings.json', "permission"
            ),
            check_allows_command(
                pre_module, "python -m json.tool .claude/settings.json", "lecture de settings.json"
            ),
            check_allows_command(
                pre_module,
                'echo "hash identique -> aucune modification" && git diff .claude/settings.json',
                'texte "->" sans rapport avec une écriture',
            ),
            check_blocks_additional_pattern(
                pre_module,
                'echo "diagnostic" && echo "{}" >> .claude/settings.json',
                "permission",
            ),
            check_write_settings_json_blocked_and_file_untouched(pre_module),
            check_logging_sqlite_and_jsonl(pre_module, isolated_root, "diskpart /s wipe.txt", "DENY"),
            check_logging_sqlite_and_jsonl(pre_module, isolated_root, "git status", "ALLOW"),
            check_post_tool_use_logging(post_module, isolated_root),
        ]
        return results
    finally:
        if own_temp:
            shutil.rmtree(work_dir, ignore_errors=True)


def main() -> int:
    results = run_all_checks()

    for r in results:
        print(f"[{r.status}] {r.name}: {r.detail}")

    failed = [r for r in results if r.status == "FAILED"]
    print()
    if failed:
        print(f"[FAILED] {len(failed)}/{len(results)} vérification(s) de sécurité en échec")
        return 1

    print(f"[VERIFIED] {len(results)}/{len(results)} vérifications de sécurité passées")
    return 0


if __name__ == "__main__":
    sys.exit(main())
