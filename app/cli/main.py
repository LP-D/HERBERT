import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.active_task import activate_task, deactivate_task, deactivate_task_if_active  # noqa: E402
from app.change_proof_builder import build_change_proof, detect_regressions  # noqa: E402
from app.cli.doctor import run_all_checks  # noqa: E402
from app.config import load_config, write_default_config  # noqa: E402
from app.database.connection import get_connection  # noqa: E402
from app.database.migrate import apply_migrations, current_schema_version  # noqa: E402
from app.database.repository import (  # noqa: E402
    archive_project,
    archive_task,
    count_unarchived_tasks,
    get_latest_audit_log_details,
    get_latest_change_proof,
    get_latest_promotion_for_task,
    get_latest_test_result_for_task,
    get_latest_verified_pass_test_result_for_project,
    get_project,
    get_project_by_name,
    get_task,
    insert_audit_log,
    insert_change_proof,
    insert_project,
    insert_promotion,
    insert_task,
    insert_test_result,
    list_projects,
    update_promotion_status,
)
from app.logging_utils import append_jsonl_event  # noqa: E402
from app.report_builder import write_report  # noqa: E402
from app.models import Project, Promotion, Task  # noqa: E402
from app.models.enums import LogStatus  # noqa: E402
from app.models.promotion import PromotionStatus  # noqa: E402
from app.models.test_result import TestResultStatus  # noqa: E402
from app.git_wrapper import (  # noqa: E402
    GitWrapperError,
    checkout_branch,
    create_candidate_branch,
    get_current_branch,
    get_head_commit,
    merge_branch,
    push_to_origin,
    revert_commit,
    rollback_to_commit,
)
from app.pytest_runner import run_pytest_for_project  # noqa: E402
from app.reporting.dashboard_builder import build_dashboard, open_dashboard  # noqa: E402
from app.state_machine import TaskState, is_legal_transition  # noqa: E402
from app.state_machine.service import TaskNotFoundError, advance_after_test_result, transition_task  # noqa: E402
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


def _regenerate_dashboard(conn: sqlite3.Connection) -> None:
    """Best-effort : reconstruit dashboard/ après toute commande qui modifie
    l'état d'une tâche. Assouplissement DÉLIBÉRÉ et documenté du principe
    "aucun sous-effet automatique" appliqué à `engine sync push` (voir
    docs/DASHBOARD.md) : régénérer un fichier HTML local est sans risque —
    pas d'effet externe, pas d'irréversibilité — contrairement à un push
    vers un dépôt distant. Ne doit JAMAIS faire échouer ni bloquer la
    commande CLI appelante : toute exception ici est avalée et journalisée
    avec un statut UNAVAILABLE, jamais propagée."""
    try:
        build_dashboard(conn, REPO_ROOT / "dashboard", _logs_dir())
    except Exception as exc:
        try:
            append_jsonl_event(
                _logs_dir(),
                component="cli.dashboard_auto_regen",
                event="régénération automatique du dashboard échouée",
                level="WARNING",
                status=LogStatus.UNAVAILABLE.value,
                details={"error": str(exc)},
            )
        except Exception:
            pass  # journalisation elle-même best-effort : ne jamais lever ici


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
    projects = list_projects(conn, include_archived=args.include_archived)
    conn.close()

    if not projects:
        print("[VERIFIED] aucun projet enregistré")
        return 0

    for p in projects:
        archived_suffix = f"  [ARCHIVÉ le {p.archived_at.isoformat()}]" if p.archived_at else ""
        print(f"[VERIFIED] {p.id}  {p.name}  {p.path}  {p.created_at.isoformat()}{archived_suffix}")
    return 0


def cmd_project_archive(args: argparse.Namespace) -> int:
    """engine project archive <project_id> : soft delete — aucune ligne
    d'audit_log/state_transitions/commands/change_proofs n'est jamais
    supprimée. Refuse par défaut si des tâches non archivées existent
    encore sur ce projet (--force + confirmation explicite pour outrepasser)
    — pour éviter qu'un projet disparaisse des vues normales pendant que du
    travail non archivé y est encore rattaché."""
    conn = _connect()
    project = get_project(conn, args.project_id)
    if project is None:
        conn.close()
        print(f"[FAILED] projet introuvable: {args.project_id}")
        return 1
    if project.archived_at is not None:
        conn.close()
        print(f"[VERIFIED] projet déjà archivé le {project.archived_at.isoformat()}: {project.id}")
        return 0

    unarchived = count_unarchived_tasks(conn, project.id)
    if unarchived > 0 and not args.force:
        conn.close()
        print(
            f"[BLOCKED] archivage refusé: {unarchived} tâche(s) non archivée(s) existent encore sur ce projet. "
            "Archivez-les d'abord (engine task archive <task_id>), ou relancez avec --force."
        )
        return 1

    if unarchived > 0 and args.force:
        print(f"[BLOCKED] --force demandé malgré {unarchived} tâche(s) non archivée(s) sur ce projet.")
        confirmation = input("Tapez exactement OUI pour confirmer l'archivage forcé: ")
        if confirmation.strip() != "OUI":
            conn.close()
            print("[BLOCKED] confirmation non reçue telle quelle, archivage annulé.")
            return 1

    archived_at = datetime.now(timezone.utc).isoformat()
    archive_project(conn, project.id, archived_at)
    conn.close()

    print(f"[VERIFIED] projet archivé: {project.id} ({project.name})")
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
    _regenerate_dashboard(conn)
    conn.close()

    if stored is None:
        print("[FAILED] la tâche n'a pas pu être relue après insertion")
        return 1

    print(f"[VERIFIED] tâche créée: id={stored.id} project={project.name} status={stored.status.value}")
    return 0


def cmd_task_status(args: argparse.Namespace) -> int:
    conn = _connect()

    # Soft delete (V0.5, point 2) : une tâche archivée, OU dont le projet
    # est archivé, est traitée comme invisible par défaut — pas seulement
    # absente des listes, même un lookup direct par id. --include-archived
    # la rend à nouveau consultable explicitement. Ne s'applique que si la
    # tâche existe réellement (sinon le message "tâche introuvable" normal
    # ci-dessous reste inchangé).
    if not args.include_archived:
        precheck_task = get_task(conn, args.id)
        if precheck_task is not None:
            precheck_project = get_project(conn, precheck_task.project_id)
            archived = precheck_task.archived_at is not None or (
                precheck_project is not None and precheck_project.archived_at is not None
            )
            if archived:
                conn.close()
                print(
                    f"[FAILED] tâche archivée (ou son projet l'est) — invisible par défaut: {args.id}. "
                    "Relancez avec --include-archived pour la consulter quand même."
                )
                return 1

    if args.to is None:
        task = get_task(conn, args.id)
        if task is None:
            conn.close()
            print(f"[FAILED] tâche introuvable: {args.id}")
            return 1
        print(f"[VERIFIED] tâche {task.id}: status={task.status.value}")

        proof = get_latest_change_proof(conn, task.id)
        promotion = get_latest_promotion_for_task(conn, task.id)
        health_check = get_latest_test_result_for_task(conn, task.id) if promotion else None
        conn.close()

        if proof is None:
            print("[VERIFIED] aucun ChangeProof enregistré pour cette tâche (lancez `engine task test`)")
        else:
            print(
                f"[VERIFIED] dernier ChangeProof: status={proof.status.value} "
                f"tests_passed={proof.tests_passed} tests_failed={proof.tests_failed} "
                f"fichiers_modifiés={len(proof.files_changed)} "
                f"régressions={proof.regressions or 'aucune'} ({proof.created_at.isoformat()})"
            )

        if promotion is None:
            print("[VERIFIED] aucune promotion enregistrée pour cette tâche")
        else:
            print(
                f"[VERIFIED] dernière promotion: status={promotion.status.value} "
                f"{promotion.commit_before[:12]} -> {promotion.commit_after[:12]} "
                f"({promotion.stable_branch} <- {promotion.candidate_branch}) ({promotion.created_at.isoformat()})"
            )
            if health_check is not None:
                print(
                    f"[VERIFIED] health check post-promotion: status={health_check.status.value} "
                    f"passed={health_check.passed} failed={health_check.failed} errors={health_check.errors}"
                )
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
    _regenerate_dashboard(conn)
    conn.close()

    if allowed:
        print(f"[VERIFIED] transition acceptée: {transition.from_state.value} -> {transition.to_state.value}")
        return 0

    print(
        f"[BLOCKED] transition refusée et journalisée: "
        f"{transition.from_state.value} -> {transition.to_state.value} (illégale)"
    )
    return 1


def cmd_task_activate(args: argparse.Namespace) -> int:
    """engine task activate <task_id> : active cette tâche pour son projet
    (résolu automatiquement) — les hooks PreToolUse/PostToolUse attribuent
    alors task_id aux commandes/événements réels tant qu'elle reste active.
    Voir app/active_task.py pour le mécanisme complet et ses limites."""
    conn = _connect()
    try:
        result = activate_task(conn, args.task_id)
    except ValueError as exc:
        conn.close()
        print(f"[FAILED] {exc}")
        return 1
    conn.close()

    if result.previous_task_id and result.previous_task_id != result.task.id:
        print(
            f"[VERIFIED] tâche active remplacée sur le projet {result.project.name}: "
            f"{result.previous_task_id} -> {result.task.id}"
        )
    else:
        print(f"[VERIFIED] tâche active sur le projet {result.project.name}: {result.task.id}")
    return 0


def cmd_task_deactivate(args: argparse.Namespace) -> int:
    """engine task deactivate <task_id> : ne désactive QUE si cette tâche
    est bien la tâche active de son projet — jamais un no-op silencieux."""
    conn = _connect()
    try:
        cleared = deactivate_task(conn, args.task_id)
    except ValueError as exc:
        conn.close()
        print(f"[FAILED] {exc}")
        return 1
    conn.close()

    if cleared:
        print(f"[VERIFIED] tâche désactivée: {args.task_id}")
        return 0

    print(f"[FAILED] cette tâche n'était pas la tâche active de son projet, rien désactivé: {args.task_id}")
    return 1


def cmd_task_archive(args: argparse.Namespace) -> int:
    """engine task archive <task_id> : soft delete — aucune ligne
    d'audit_log/state_transitions/commands/change_proofs n'est jamais
    supprimée. Désactive aussi la tâche si elle était la tâche active de
    son projet (best-effort, voir app/active_task.py) : une tâche archivée
    n'a plus vocation à recevoir des commandes attribuées."""
    conn = _connect()
    task = get_task(conn, args.task_id)
    if task is None:
        conn.close()
        print(f"[FAILED] tâche introuvable: {args.task_id}")
        return 1
    if task.archived_at is not None:
        conn.close()
        print(f"[VERIFIED] tâche déjà archivée le {task.archived_at.isoformat()}: {task.id}")
        return 0

    archived_at = datetime.now(timezone.utc).isoformat()
    archive_task(conn, task.id, archived_at)
    deactivate_task_if_active(conn, task.id)
    _regenerate_dashboard(conn)
    conn.close()

    print(f"[VERIFIED] tâche archivée: {task.id}")
    return 0


def _get_task_and_project(conn: sqlite3.Connection, task_id: str) -> tuple[Task | None, Project | None, str | None]:
    """Retourne (task, project, message_erreur). message_erreur est None si
    tout s'est bien passé."""
    task = get_task(conn, task_id)
    if task is None:
        return None, None, f"tâche introuvable: {task_id}"
    project = get_project(conn, task.project_id)
    if project is None:
        return task, None, f"projet introuvable pour cette tâche (project_id={task.project_id})"
    return task, project, None


def cmd_task_branch(args: argparse.Namespace) -> int:
    conn = _connect()
    task, project, error = _get_task_and_project(conn, args.task_id)
    if error:
        conn.close()
        print(f"[FAILED] {error}")
        return 1

    try:
        # origin_branch = branche STABLE depuis laquelle la candidate est
        # créée (ex: "master") — nécessaire pour `engine task promote`
        # (V0.3), qui doit savoir dans quelle branche merger la candidate.
        # Absent en V0.2 (seul base_commit, un hash, était enregistré).
        origin_branch = get_current_branch(project.path)
        base_commit = get_head_commit(project.path)
    except GitWrapperError as exc:
        conn.close()
        print(f"[FAILED] impossible de lire HEAD/branche courante dans {project.path}: {exc}")
        return 1

    branch_name = f"candidate/{task.id}"
    result = create_candidate_branch(project.path, branch_name)

    insert_audit_log(
        conn,
        component="cli.task_branch",
        event="branche candidate créée" if result.ok else "échec création branche candidate",
        level="INFO" if result.ok else "ERROR",
        status=LogStatus.VERIFIED.value if result.ok else LogStatus.FAILED.value,
        task_id=task.id,
        details={
            "branch": branch_name,
            "origin_branch": origin_branch,
            "base_commit": base_commit,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
    _regenerate_dashboard(conn)
    conn.close()

    if not result.ok:
        print(f"[FAILED] création de la branche candidate: {result.stderr}")
        return 1

    print(f"[VERIFIED] branche candidate créée: {branch_name} (base={base_commit[:12]})")
    return 0


def cmd_task_rollback(args: argparse.Namespace) -> int:
    conn = _connect()
    task, project, error = _get_task_and_project(conn, args.task_id)
    if error:
        conn.close()
        print(f"[FAILED] {error}")
        return 1

    branch_info = get_latest_audit_log_details(conn, task.id, "cli.task_branch")
    if not branch_info or not branch_info.get("base_commit"):
        conn.close()
        print("[FAILED] aucune branche candidate connue pour cette tâche (lancez d'abord `engine task branch`)")
        return 1

    base_commit = branch_info["base_commit"]

    print(f"[BLOCKED] rollback demandé: git reset --hard vers {base_commit[:12]} dans {project.path}")
    print("Cette opération est IRRÉVERSIBLE et écrase tout changement non commité.")
    confirmation = input("Tapez exactement OUI pour confirmer le rollback: ")
    if confirmation.strip() != "OUI":
        conn.close()
        print("[BLOCKED] confirmation non reçue telle quelle, rollback annulé.")
        return 1

    result = rollback_to_commit(project.path, base_commit)

    insert_audit_log(
        conn,
        component="cli.task_rollback",
        event="rollback exécuté" if result.ok else "échec du rollback",
        level="INFO" if result.ok else "ERROR",
        status=LogStatus.VERIFIED.value if result.ok else LogStatus.FAILED.value,
        task_id=task.id,
        details={"base_commit": base_commit, "stdout": result.stdout, "stderr": result.stderr},
    )
    if result.ok:
        # Rollback manuel réellement effectué : le travail de cette tâche
        # sur cette candidate est terminé, plus rien à attribuer après ce
        # point — désactivation automatique (best-effort, voir
        # app/active_task.py). Pas de désactivation si le rollback a
        # échoué : le travail (et donc l'attribution de commandes) continue.
        deactivate_task_if_active(conn, task.id)
    _regenerate_dashboard(conn)
    conn.close()

    if not result.ok:
        print(f"[FAILED] rollback: {result.stderr}")
        return 1

    print(f"[VERIFIED] rollback effectué vers {base_commit[:12]}")
    return 0


def cmd_task_test(args: argparse.Namespace) -> int:
    conn = _connect()
    task, project, error = _get_task_and_project(conn, args.task_id)
    if error:
        conn.close()
        print(f"[FAILED] {error}")
        return 1

    previous_pass = get_latest_verified_pass_test_result_for_project(conn, project.id)

    branch_info = get_latest_audit_log_details(conn, task.id, "cli.task_branch")
    base_commit = branch_info.get("base_commit") if branch_info else None

    # Bug réel trouvé pendant la démonstration bout-en-bout V0.3 : sans ce
    # checkout, `engine task test` teste l'état ACTUEL du dépôt, pas
    # forcément la branche candidate de CETTE tâche — invisible tant qu'on
    # ne teste qu'une seule candidate à la fois (le cas de tous les tests
    # unitaires V0.2), mais faux dès que deux candidates du même projet
    # coexistent et qu'on bascule entre elles.
    if branch_info and branch_info.get("branch"):
        checkout_result = checkout_branch(project.path, branch_info["branch"])
        if not checkout_result.ok:
            conn.close()
            print(f"[FAILED] impossible de checkout {branch_info['branch']}: {checkout_result.stderr}")
            return 1

    result = run_pytest_for_project(project.path, task.id)
    insert_test_result(conn, result)

    regressions = detect_regressions(previous_pass, result)

    proof = build_change_proof(conn, task, project, result, base_commit, regressions=regressions)
    insert_change_proof(conn, proof)

    # Comble un manque réel de V0.2 : `engine task test` ne faisait jamais
    # avancer l'état RÉEL de la tâche (elle restait bloquée à RECEIVED),
    # alors que la table de transitions prévoyait déjà TESTING->DONE/FAILED
    # sans jamais les emprunter. Nécessaire pour que `engine task promote`
    # (V0.3) ait un état DONE réel à vérifier, pas seulement un ChangeProof.
    advance_after_test_result(conn, task, passed=(result.status == TestResultStatus.VERIFIED_PASS))

    append_jsonl_event(
        _logs_dir(),
        component="cli.task_test",
        event="engine task test exécuté",
        level="INFO" if result.status == TestResultStatus.VERIFIED_PASS else "WARNING",
        status=result.status.value,
        task_id=task.id,
        details={
            "project_path": project.path,
            "total": result.total,
            "passed": result.passed,
            "failed": result.failed,
            "errors": result.errors,
            "duration_seconds": result.duration_seconds,
            "regressions": regressions,
        },
    )
    _regenerate_dashboard(conn)
    conn.close()

    print(
        f"[{result.status.value}] pytest dans {project.path}: total={result.total} "
        f"passed={result.passed} failed={result.failed} errors={result.errors} "
        f"durée={result.duration_seconds:.2f}s"
    )
    if regressions:
        print(f"[FAILED] régression(s) détectée(s) par rapport au dernier run VERIFIED_PASS: {', '.join(regressions)}")

    if result.status in (TestResultStatus.VERIFIED_FAIL, TestResultStatus.NOT_EXECUTED, TestResultStatus.UNAVAILABLE):
        return 1
    if regressions:
        return 1
    return 0


def cmd_task_promote(args: argparse.Namespace) -> int:
    """engine task promote <task_id> : merge réel de candidate/<task_id>
    vers sa branche stable d'origine, puis health check post-merge. Un
    health check en échec déclenche un AUTO_ROLLBACK (git revert du commit
    de merge, jamais un reset --hard) — distinct d'un `engine task
    rollback` manuel (V0.2) : composant de log différent
    (cli.task_promote_auto_rollback vs cli.task_rollback), et status
    AUTO_ROLLBACK dans la table promotions."""
    conn = _connect()
    task, project, error = _get_task_and_project(conn, args.task_id)
    if error:
        conn.close()
        print(f"[FAILED] {error}")
        return 1

    proof = get_latest_change_proof(conn, task.id)
    if proof is None:
        conn.close()
        print("[BLOCKED] promotion refusée: aucun ChangeProof pour cette tâche (lancez `engine task test` d'abord).")
        return 1

    if proof.status != TaskState.DONE or proof.regressions:
        conn.close()
        print(
            f"[BLOCKED] promotion refusée: dernier ChangeProof status={proof.status.value}, "
            f"tests_failed={proof.tests_failed}, régressions={proof.regressions or 'aucune'}."
        )
        return 1

    if not is_legal_transition(task.status, TaskState.PROMOTED):
        conn.close()
        print(
            f"[BLOCKED] promotion refusée: transition {task.status.value} -> PROMOTED illégale pour l'état "
            f"actuel de la tâche (ChangeProof VERIFIED_PASS ne suffit pas si la tâche elle-même n'est pas DONE)."
        )
        return 1

    branch_info = get_latest_audit_log_details(conn, task.id, "cli.task_branch")
    if not branch_info or not branch_info.get("origin_branch"):
        conn.close()
        print("[FAILED] branche d'origine inconnue pour cette tâche (lancez d'abord `engine task branch`).")
        return 1

    stable_branch = branch_info["origin_branch"]
    candidate_branch = branch_info["branch"]

    checkout_result = checkout_branch(project.path, stable_branch)
    if not checkout_result.ok:
        conn.close()
        print(f"[FAILED] impossible de checkout {stable_branch}: {checkout_result.stderr}")
        return 1

    try:
        commit_before = get_head_commit(project.path)
    except GitWrapperError as exc:
        conn.close()
        print(f"[FAILED] impossible de lire HEAD sur {stable_branch}: {exc}")
        return 1

    merge_result = merge_branch(
        project.path,
        candidate_branch,
        message=f"Merge {candidate_branch} into {stable_branch} (HERBERT promotion, task {task.id})",
    )

    if not merge_result.ok:
        insert_promotion(
            conn,
            Promotion(
                task_id=task.id,
                stable_branch=stable_branch,
                candidate_branch=candidate_branch,
                commit_before=commit_before,
                commit_after=commit_before,
                status=PromotionStatus.MERGE_FAILED,
            ),
        )
        _regenerate_dashboard(conn)
        conn.close()
        print(f"[FAILED] merge échoué: {merge_result.stderr}")
        return 1

    commit_after = get_head_commit(project.path)

    promotion = Promotion(
        task_id=task.id,
        stable_branch=stable_branch,
        candidate_branch=candidate_branch,
        commit_before=commit_before,
        commit_after=commit_after,
        status=PromotionStatus.MERGED_PENDING_HEALTH_CHECK,
    )
    insert_promotion(conn, promotion)

    transition_task(conn, task.id, TaskState.PROMOTED, reason="merge de promotion réussi")

    promote_log_details = {
        "stable_branch": stable_branch,
        "candidate_branch": candidate_branch,
        "commit_before": commit_before,
        "commit_after": commit_after,
    }
    insert_audit_log(
        conn,
        component="cli.task_promote",
        event="merge de promotion réussi, health check en cours",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=task.id,
        details=promote_log_details,
    )
    append_jsonl_event(
        _logs_dir(),
        component="cli.task_promote",
        event="merge de promotion réussi, health check en cours",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=task.id,
        details=promote_log_details,
    )
    print(f"[VERIFIED] merge réussi: {candidate_branch} -> {stable_branch} ({commit_before[:12]} -> {commit_after[:12]})")

    # --- Health check post-promotion : relance pytest sur la branche
    # stable (déjà checkout ci-dessus), PAS la candidate. ---
    health_result = run_pytest_for_project(project.path, task.id)
    insert_test_result(conn, health_result)

    if health_result.status == TestResultStatus.VERIFIED_PASS:
        update_promotion_status(conn, promotion.id, PromotionStatus.HEALTH_CHECK_PASSED)
        transition_task(conn, task.id, TaskState.DONE, reason="health check post-promotion réussi")

        health_check_details = {"total": health_result.total, "passed": health_result.passed}
        insert_audit_log(
            conn,
            component="cli.task_promote_health_check",
            event="health check post-promotion réussi",
            level="INFO",
            status=LogStatus.VERIFIED.value,
            task_id=task.id,
            details=health_check_details,
        )
        append_jsonl_event(
            _logs_dir(),
            component="cli.task_promote_health_check",
            event="health check post-promotion réussi",
            level="INFO",
            status=LogStatus.VERIFIED.value,
            task_id=task.id,
            details=health_check_details,
        )
        # Health check post-promotion réussi : le cycle de vie de cette
        # tâche est réellement terminé, plus rien à attribuer après ce
        # point (distinct du DONE normal après `task test`, qui ne
        # désactive PAS puisque le travail continue généralement vers
        # `task promote`) — voir app/active_task.py.
        deactivate_task_if_active(conn, task.id)
        _regenerate_dashboard(conn)
        conn.close()
        print(f"[VERIFIED] health check post-promotion réussi ({health_result.passed}/{health_result.total}) — tâche DONE.")
        return 0

    # --- Health check en échec : AUTO_ROLLBACK (revert, pas reset --hard) ---
    revert_result = revert_commit(project.path, commit_after, mainline=1)
    rollback_status = PromotionStatus.AUTO_ROLLBACK if revert_result.ok else PromotionStatus.ROLLBACK_FAILED
    update_promotion_status(conn, promotion.id, rollback_status)

    if revert_result.ok:
        # Le merge est réellement annulé dans le dépôt : la tâche ne doit
        # plus afficher PROMOTED, ce serait trompeur (l'état affiché ne
        # correspondrait plus à la réalité du dépôt).
        transition_task(
            conn, task.id, TaskState.ROLLED_BACK, reason="AUTO_ROLLBACK: merge reverté après échec du health check"
        )
    # sinon (ROLLBACK_FAILED) : le merge est toujours en place, la tâche
    # reste PROMOTED — c'est l'état réel, intervention manuelle requise.

    # Distinct d'un rollback manuel (component="cli.task_rollback") : ceci
    # est un rollback DE SÉCURITÉ automatique, jamais initié par une saisie
    # interactive — journalisé sous un component et un status différents
    # pour rester traçable séparément (voir tests/unit/test_task_promote.py).
    auto_rollback_details = {
        "merge_commit": commit_after,
        "health_check_total": health_result.total,
        "health_check_passed": health_result.passed,
        "health_check_failed": health_result.failed,
        "health_check_errors": health_result.errors,
        "revert_ok": revert_result.ok,
        "revert_stdout": revert_result.stdout,
        "revert_stderr": revert_result.stderr,
    }
    insert_audit_log(
        conn,
        component="cli.task_promote_auto_rollback",
        event="AUTO_ROLLBACK déclenché après échec du health check post-promotion",
        level="ERROR",
        status=rollback_status.value,
        task_id=task.id,
        details=auto_rollback_details,
    )
    append_jsonl_event(
        _logs_dir(),
        component="cli.task_promote_auto_rollback",
        event="AUTO_ROLLBACK déclenché après échec du health check post-promotion",
        level="ERROR",
        status=rollback_status.value,
        task_id=task.id,
        details=auto_rollback_details,
    )
    if revert_result.ok:
        # AUTO_ROLLBACK réellement effectué (tâche réellement ROLLED_BACK,
        # état terminal) : désactivation automatique. Pas si le revert
        # lui-même a échoué (ROLLBACK_FAILED) — la tâche reste PROMOTED,
        # intervention manuelle requise, le travail n'est pas terminé.
        deactivate_task_if_active(conn, task.id)
    _regenerate_dashboard(conn)
    conn.close()

    print(
        f"[FAILED] health check post-promotion échoué "
        f"(passed={health_result.passed} failed={health_result.failed} errors={health_result.errors})."
    )
    if revert_result.ok:
        print("[VERIFIED] AUTO_ROLLBACK: merge annulé via un nouveau commit de revert (mainline=1, pas de reset --hard).")
    else:
        print(f"[FAILED] AUTO_ROLLBACK a lui-même échoué: {revert_result.stderr} — intervention manuelle requise.")
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    """engine report <task_id> : rapport HTML statique local, généré
    uniquement à partir de données déjà en SQLite. Aucun serveur, aucun
    port ouvert — un simple fichier écrit sur disque."""
    conn = _connect()
    task = get_task(conn, args.task_id)
    if task is None:
        conn.close()
        print(f"[FAILED] tâche introuvable: {args.task_id}")
        return 1

    reports_dir = REPO_ROOT / "reports"
    output_path = write_report(conn, task, _logs_dir(), reports_dir)
    conn.close()

    print(f"[VERIFIED] rapport généré: {output_path}")
    return 0


def cmd_dashboard_build(args: argparse.Namespace) -> int:
    """engine dashboard build : régénère TOUT dashboard/ depuis SQLite
    (écrase le contenu existant, ne l'accumule pas). Reste disponible en
    commande manuelle pour une régénération à la demande (ex. après
    restauration d'un snapshot, ou pour forcer un rafraîchissement) — en
    plus de la régénération automatique déclenchée par les commandes task
    (voir _regenerate_dashboard)."""
    conn = _connect()
    result = build_dashboard(conn, REPO_ROOT / "dashboard", _logs_dir())
    conn.close()

    index_path = REPO_ROOT / "dashboard" / "index.html"
    print(f"[VERIFIED] dashboard régénéré: {len(result['pages'])} page(s), {len(result['assets'])} asset(s) — {index_path}")
    return 0


def cmd_dashboard_open(args: argparse.Namespace) -> int:
    """engine dashboard open : ouvre dashboard/index.html via le module
    stdlib `webbrowser` — aucun serveur, aucun port."""
    index_path = REPO_ROOT / "dashboard" / "index.html"
    if not index_path.exists():
        print("[FAILED] dashboard/index.html introuvable — lancez d'abord `engine dashboard build`.")
        return 1

    open_dashboard(REPO_ROOT / "dashboard")
    print(f"[VERIFIED] dashboard ouvert dans le navigateur par défaut: {index_path}")
    return 0


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
    project_list.add_argument(
        "--include-archived", action="store_true", default=False, help="inclut aussi les projets archivés"
    )
    project_list.set_defaults(func=cmd_project_list)

    project_archive = project_sub.add_parser(
        "archive", help="soft delete d'un projet (refuse si des tâches non archivées existent, sauf --force)"
    )
    project_archive.add_argument("project_id")
    project_archive.add_argument(
        "--force", action="store_true", default=False,
        help="outrepasse le refus si des tâches non archivées existent (confirmation explicite requise)",
    )
    project_archive.set_defaults(func=cmd_project_archive)

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
    task_status.add_argument(
        "--include-archived", action="store_true", default=False,
        help="consulte la tâche même si elle (ou son projet) est archivé",
    )
    task_status.set_defaults(func=cmd_task_status)

    task_archive = task_sub.add_parser("archive", help="soft delete d'une tâche")
    task_archive.add_argument("task_id")
    task_archive.set_defaults(func=cmd_task_archive)

    task_activate = task_sub.add_parser(
        "activate", help="active une tâche pour son projet (attribution task_id aux commandes des hooks)"
    )
    task_activate.add_argument("task_id")
    task_activate.set_defaults(func=cmd_task_activate)

    task_deactivate = task_sub.add_parser("deactivate", help="désactive une tâche active")
    task_deactivate.add_argument("task_id")
    task_deactivate.set_defaults(func=cmd_task_deactivate)

    task_branch = task_sub.add_parser("branch", help="crée/checkout candidate/<task_id> dans le projet lié")
    task_branch.add_argument("task_id")
    task_branch.set_defaults(func=cmd_task_branch)

    task_rollback = task_sub.add_parser(
        "rollback", help="reset --hard vers le commit stable précédent (confirmation explicite requise)"
    )
    task_rollback.add_argument("task_id")
    task_rollback.set_defaults(func=cmd_task_rollback)

    task_test = task_sub.add_parser("test", help="exécute pytest réellement dans le projet lié à la tâche")
    task_test.add_argument("task_id")
    task_test.set_defaults(func=cmd_task_test)

    task_promote = task_sub.add_parser(
        "promote", help="merge candidate/<task_id> vers la branche stable + health check post-merge"
    )
    task_promote.add_argument("task_id")
    task_promote.set_defaults(func=cmd_task_promote)

    report_parser = subparsers.add_parser(
        "report", help="génère reports/<task_id>.html (statique, données déjà en SQLite)"
    )
    report_parser.add_argument("task_id")
    report_parser.set_defaults(func=cmd_report)

    dashboard_parser = subparsers.add_parser("dashboard", help="dashboard HTML statique local (V0.4, lecture seule)")
    dashboard_sub = dashboard_parser.add_subparsers(dest="dashboard_command", required=True)

    dashboard_build = dashboard_sub.add_parser(
        "build", help="régénère dashboard/ depuis SQLite (écrase, ne l'accumule pas)"
    )
    dashboard_build.set_defaults(func=cmd_dashboard_build)

    dashboard_open = dashboard_sub.add_parser(
        "open", help="ouvre dashboard/index.html dans le navigateur (webbrowser, aucun serveur)"
    )
    dashboard_open.set_defaults(func=cmd_dashboard_open)

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
