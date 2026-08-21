import argparse
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.cli.doctor import run_all_checks  # noqa: E402
from app.config import load_config, write_default_config  # noqa: E402
from app.database.connection import get_connection  # noqa: E402
from app.database.migrate import apply_migrations, current_schema_version  # noqa: E402
from app.database.repository import (  # noqa: E402
    get_project_by_name,
    get_task,
    insert_project,
    insert_task,
    list_projects,
)
from app.logging_utils import append_jsonl_event  # noqa: E402
from app.models import Project, Task  # noqa: E402
from app.models.enums import LogStatus  # noqa: E402
from app.git_wrapper import get_current_branch, push_to_origin  # noqa: E402
from app.state_machine import TaskState  # noqa: E402
from app.state_machine.service import TaskNotFoundError, transition_task  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from scripts.verify_security import run_all_checks as run_all_security_checks  # noqa: E402


def _config_path() -> Path:
    return REPO_ROOT / "config" / "system.yaml"


def _connect() -> sqlite3.Connection:
    config = load_config(_config_path())
    db_path = REPO_ROOT / config["database"]["path"]
    conn = get_connection(db_path)
    apply_migrations(conn, REPO_ROOT / config["migrations"]["dir"])
    return conn


def _logs_dir() -> Path:
    config = load_config(_config_path())
    return REPO_ROOT / config["logs"]["dir"]


def cmd_init(args: argparse.Namespace) -> int:
    created = []

    for rel in ["app/cli", "app/models", "app/state_machine", "app/database", "trusted", ".claude/hooks", "migrations", "tests/unit", "logs"]:
        path = REPO_ROOT / rel
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created.append(rel)

    config_path = _config_path()
    if not config_path.exists():
        write_default_config(config_path)
        created.append("config/system.yaml")

    conn = _connect()
    version = current_schema_version(conn)
    conn.close()

    print(f"[VERIFIED] structure initialisée. Éléments créés: {created or 'aucun (déjà présent)'}")
    print(f"[VERIFIED] schéma SQLite à la version {version}")

    append_jsonl_event(
        _logs_dir(),
        component="cli.init",
        event="engine init exécuté",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        details={"created": created, "schema_version": version},
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    results = run_all_checks(REPO_ROOT)
    for r in results:
        print(f"[{r.status}] {r.name}: {r.detail}")

    security_results = []
    if getattr(args, "security", False):
        security_results = run_all_security_checks()
        print()
        print("--- vérification de sécurité (hooks) ---")
        for r in security_results:
            print(f"[{r.status}] {r.name}: {r.detail}")

    failed = [r for r in results if r.status == "FAILED"] + [r for r in security_results if r.status == "FAILED"]

    append_jsonl_event(
        _logs_dir(),
        component="cli.doctor",
        event="engine doctor exécuté",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        details={
            "results": [r.__dict__ for r in results],
            "security_results": [r.__dict__ for r in security_results],
        },
    )

    return 1 if failed else 0


def cmd_project_add(args: argparse.Namespace) -> int:
    try:
        project = Project(name=args.name, path=args.path)
    except ValidationError as exc:
        print(f"[FAILED] projet invalide: {exc}")
        return 1

    conn = _connect()
    try:
        insert_project(conn, project)
    except sqlite3.IntegrityError as exc:
        print(f"[FAILED] impossible de créer le projet (probablement un nom déjà utilisé): {exc}")
        conn.close()
        return 1

    stored = get_project_by_name(conn, project.name)
    conn.close()

    if stored is None:
        print("[FAILED] le projet n'a pas pu être relu après insertion")
        return 1

    print(f"[VERIFIED] projet créé: id={stored.id} name={stored.name} path={stored.path}")
    return 0


def cmd_project_list(args: argparse.Namespace) -> int:
    conn = _connect()
    projects = list_projects(conn)
    conn.close()

    if not projects:
        print("[VERIFIED] aucun projet enregistré")
        return 0

    for p in projects:
        print(f"[VERIFIED] {p.id}  {p.name}  {p.path}  {p.created_at.isoformat()}")
    return 0


def cmd_task_create(args: argparse.Namespace) -> int:
    conn = _connect()
    project = get_project_by_name(conn, args.project)
    if project is None:
        conn.close()
        print(f"[FAILED] projet introuvable: {args.project}")
        return 1

    try:
        task = Task(project_id=project.id, description=args.description)
    except ValidationError as exc:
        conn.close()
        print(f"[FAILED] tâche invalide: {exc}")
        return 1

    insert_task(conn, task)
    stored = get_task(conn, task.id)
    conn.close()

    if stored is None:
        print("[FAILED] la tâche n'a pas pu être relue après insertion")
        return 1

    print(f"[VERIFIED] tâche créée: id={stored.id} project={project.name} status={stored.status.value}")
    return 0


def cmd_task_status(args: argparse.Namespace) -> int:
    conn = _connect()

    if args.to is None:
        task = get_task(conn, args.id)
        conn.close()
        if task is None:
            print(f"[FAILED] tâche introuvable: {args.id}")
            return 1
        print(f"[VERIFIED] tâche {task.id}: status={task.status.value}")
        return 0

    try:
        to_state = TaskState(args.to)
    except ValueError:
        conn.close()
        valid = ", ".join(s.value for s in TaskState)
        print(f"[FAILED] état inconnu '{args.to}'. États valides: {valid}")
        return 1

    try:
        allowed, task, transition = transition_task(conn, args.id, to_state, reason=args.reason)
    except TaskNotFoundError as exc:
        conn.close()
        print(f"[FAILED] {exc}")
        return 1
    conn.close()

    if allowed:
        print(f"[VERIFIED] transition acceptée: {transition.from_state.value} -> {transition.to_state.value}")
        return 0

    print(
        f"[BLOCKED] transition refusée et journalisée: "
        f"{transition.from_state.value} -> {transition.to_state.value} (illégale)"
    )
    return 1


def cmd_sync_push(args: argparse.Namespace) -> int:
    """Push explicite vers origin. C'est la SEULE fonction de ce module qui
    appelle push_to_origin() — aucune autre commande (task create, task
    status, project add, etc.) n'y fait référence, directement ou
    indirectement. Un push reste toujours un acte volontaire de
    l'utilisateur, jamais un sous-effet d'une autre commande."""
    security_results = run_all_security_checks()
    failed = [r for r in security_results if r.status == "FAILED"]

    if failed:
        print("[BLOCKED] push refusé : la vérification de sécurité des hooks a échoué.")
        for r in failed:
            print(f"  [FAILED] {r.name}: {r.detail}")

        if not args.force_unsafe:
            print("Relancez avec --force-unsafe pour outrepasser (confirmation explicite requise).")
            return 1

        print("--force-unsafe demandé malgré l'échec ci-dessus.")
        confirmation = input("Tapez exactement OUI pour confirmer le push malgré cet échec: ")
        if confirmation.strip() != "OUI":
            print("[BLOCKED] confirmation non reçue telle quelle, push annulé.")
            return 1
        print("[CLAIMED] push forcé malgré un échec de vérification sécurité, confirmé explicitement.")

    branch = get_current_branch(REPO_ROOT)
    result = push_to_origin(REPO_ROOT, branch)

    append_jsonl_event(
        _logs_dir(),
        component="cli.sync_push",
        event="engine sync push exécuté",
        level="INFO" if result.ok else "ERROR",
        status=LogStatus.VERIFIED.value if result.ok else LogStatus.FAILED.value,
        details={
            "branch": branch,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "security_checks_failed": len(failed),
            "force_unsafe": args.force_unsafe,
        },
    )

    if not result.ok:
        print(f"[FAILED] git push a échoué (code {result.returncode}): {result.stderr}")
        return 1

    print(f"[VERIFIED] push vers origin/{branch} réussi.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="engine", description="HERBERT V0.1 - LOCAL CORE")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="crée structure, migrations, config par défaut").set_defaults(func=cmd_init)

    doctor_parser = subparsers.add_parser("doctor", help="vérifie réellement l'environnement")
    doctor_parser.add_argument(
        "--security", action="store_true", default=False, help="inclut la vérification de sécurité des hooks"
    )
    doctor_parser.set_defaults(func=cmd_doctor)

    project_parser = subparsers.add_parser("project", help="gestion des projets")
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)

    project_add = project_sub.add_parser("add", help="ajoute un projet")
    project_add.add_argument("--name", required=True)
    project_add.add_argument("--path", required=True)
    project_add.set_defaults(func=cmd_project_add)

    project_list = project_sub.add_parser("list", help="liste les projets")
    project_list.set_defaults(func=cmd_project_list)

    task_parser = subparsers.add_parser("task", help="gestion des tâches")
    task_sub = task_parser.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create", help="crée une tâche")
    task_create.add_argument("--project", required=True)
    task_create.add_argument("--description", required=True)
    task_create.set_defaults(func=cmd_task_create)

    task_status = task_sub.add_parser("status", help="affiche ou change le statut d'une tâche")
    task_status.add_argument("--id", required=True)
    task_status.add_argument("--to", required=False, default=None, help="nouvel état souhaité (optionnel)")
    task_status.add_argument("--reason", required=False, default=None)
    task_status.set_defaults(func=cmd_task_status)

    sync_parser = subparsers.add_parser("sync", help="synchronisation avec le remote (push explicite uniquement)")
    sync_sub = sync_parser.add_subparsers(dest="sync_command", required=True)

    sync_push = sync_sub.add_parser(
        "push", help="push explicite vers origin — jamais appelé implicitement par une autre commande"
    )
    sync_push.add_argument(
        "--force-unsafe",
        action="store_true",
        default=False,
        help="outrepasse un échec de vérification sécurité (une confirmation explicite reste requise)",
    )
    sync_push.set_defaults(func=cmd_sync_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
