"""Résolution de la "tâche active" par projet (V0.5, point 1) — permet à
`.claude/hooks/pre_tool_use.py`/`post_tool_use.py` d'attribuer un `task_id`
aux commandes/événements sans que Claude Code n'envoie jamais ce champ
lui-même (il n'a aucune notion de "tâche HERBERT").

Stockage : deux colonnes additives sur `projects` (`active_task_id`,
`active_task_activated_at`), pas une table séparée — l'isolation par projet
vient du fait que le marqueur vit sur la ligne du projet concerné, jamais
d'un état global à synchroniser. Voir migrations/0005_active_task.sql.

Best-effort par construction : toute résolution échouée (chemin invalide,
projet non enregistré, aucune tâche active) retombe sur task_id=None —
jamais bloquant, jamais une erreur remontée au hook appelant. Même principe
que la protection anti-auto-modification de settings.json : ne jamais
deviner, et ne jamais faire échouer le chemin de sécurité pour une
fonctionnalité additive.

Limite connue, documentée plutôt que masquée par un délai arbitraire :
aucune expiration par durée. Une tâche activée puis oubliée reste active
jusqu'à désactivation explicite (`engine task deactivate`) ou jusqu'à l'un
des 3 points de sortie réellement terminaux du cycle de vie d'une tâche
(health check post-promotion réussi, AUTO_ROLLBACK, rollback manuel) — voir
`deactivate_task_if_active`, appelée depuis ces 3 points dans
app/cli/main.py.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.database.repository import get_project, get_task, list_projects
from app.logging_utils import append_jsonl_event
from app.models import Project, Task
from app.models.enums import LogStatus
from app.state_machine.states import TaskState

# États pour lesquels activer une tâche n'a plus de sens : le travail actif
# attribuable à des commandes est terminé. DONE reste théoriquement légal
# vers PROMOTED (LEGAL_TRANSITIONS), mais la suite (`task promote`) est
# exécutée par HERBERT lui-même, pas par des commandes humaines à attribuer
# — l'activer n'aurait aucun effet utile. PROMOTED est inclus pour un cas
# réel, pas hypothétique : si le revert d'un AUTO_ROLLBACK échoue lui-même
# (ROLLBACK_FAILED, voir cmd_task_promote), la tâche reste bloquée à
# PROMOTED indéfiniment jusqu'à intervention manuelle — un état de repos
# durable, pas transitoire, dans ce cas précis.
_NON_ACTIVATABLE_STATES = {TaskState.DONE, TaskState.PROMOTED, TaskState.ROLLED_BACK}


@dataclass
class ActivationResult:
    project: Project
    task: Task
    previous_task_id: str | None


def _resolve_path(raw: str) -> Path | None:
    try:
        return Path(raw).resolve()
    except (OSError, ValueError):
        return None


def resolve_project_by_cwd(conn: sqlite3.Connection, cwd: str) -> Project | None:
    """Trouve le projet enregistré dont `path` correspond à `cwd`, par
    comparaison de chemins CANONIQUES — jamais une correspondance textuelle
    brute (un `cwd` avec un slash final ou une casse différente sur Windows
    doit quand même matcher). Retourne None si aucun projet enregistré ne
    correspond (ex. session sur herbert/ lui-même, ou répertoire jamais
    enregistré via `engine project add`)."""
    resolved_cwd = _resolve_path(cwd)
    if resolved_cwd is None:
        return None
    for project in list_projects(conn):
        if _resolve_path(project.path) == resolved_cwd:
            return project
    return None


def get_active_task_id(conn: sqlite3.Connection, project_id: str) -> str | None:
    row = conn.execute("SELECT active_task_id FROM projects WHERE id = ?", (project_id,)).fetchone()
    return row["active_task_id"] if row else None


def resolve_active_task_id_for_hook(conn: sqlite3.Connection, cwd: str | None) -> str | None:
    """Point d'entrée UNIQUE utilisé par les hooks. Contrat: ne lève
    jamais — toute étape manquante ou en échec (cwd absent, projet non
    enregistré, aucune tâche active, erreur SQLite imprévue) retombe sur
    None, exactement le comportement d'avant cette fonctionnalité. Le
    filet de sécurité est ici, pas supposé porté par l'appelant."""
    try:
        if not cwd:
            return None
        project = resolve_project_by_cwd(conn, cwd)
        if project is None:
            return None
        return get_active_task_id(conn, project.id)
    except Exception:
        return None


def activate_task(conn: sqlite3.Connection, task_id: str, logs_dir: str | Path) -> ActivationResult:
    """`engine task activate <task_id>` : active cette tâche pour SON
    projet (résolu depuis task.project_id, jamais besoin de le préciser).
    Écrase l'activation précédente du même projet si elle existe —
    silencieusement en base, mais `previous_task_id` est retourné pour que
    l'appelant (CLI) l'affiche : jamais une bascule cachée.

    Refuse (ValueError) si la tâche est dans un état terminal pour ce
    concept (voir _NON_ACTIVATABLE_STATES) : activer une tâche déjà
    DONE/PROMOTED/ROLLED_BACK n'attribuerait des commandes à rien d'utile.

    `logs_dir` est obligatoire, pas une valeur par défaut cachée : chaque
    activation/désactivation est journalisée dans logs/*.jsonl (composant
    `active_task`) — traçabilité de la DÉCISION d'attribution, pas
    seulement de son effet en base."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"tâche introuvable: {task_id}")
    if task.status in _NON_ACTIVATABLE_STATES:
        raise ValueError(
            f"impossible d'activer une tâche à l'état {task.status.value} "
            "(plus rien à attribuer : ce cycle de vie est terminé, créez une nouvelle tâche si le travail reprend)"
        )
    project = get_project(conn, task.project_id)
    if project is None:
        raise ValueError(f"projet introuvable pour cette tâche (project_id={task.project_id})")

    previous_task_id = get_active_task_id(conn, project.id)

    conn.execute(
        "UPDATE projects SET active_task_id = ?, active_task_activated_at = ? WHERE id = ?",
        (task.id, datetime.now(timezone.utc).isoformat(), project.id),
    )
    conn.commit()

    append_jsonl_event(
        logs_dir,
        component="active_task",
        event="tâche activée",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=task.id,
        details={
            "project_id": project.id,
            "project_name": project.name,
            "previous_task_id": previous_task_id,
            "trigger": "manual",
        },
    )

    return ActivationResult(project=project, task=task, previous_task_id=previous_task_id)


def deactivate_task(conn: sqlite3.Connection, task_id: str, logs_dir: str | Path, trigger: str = "manual") -> bool:
    """`engine task deactivate <task_id>` (trigger="manual" par défaut) : ne
    désactive QUE si ce task_id est bien la tâche active de son projet —
    jamais un no-op silencieux fondé sur une mauvaise supposition. Retourne
    True si une désactivation a réellement eu lieu (et alors seulement,
    journalisée dans logs/*.jsonl avec le `trigger` fourni — "manual" pour
    `engine task deactivate`, une valeur distincte pour chacun des 3 points
    de sortie automatiques, voir deactivate_task_if_active)."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"tâche introuvable: {task_id}")
    project = get_project(conn, task.project_id)
    if project is None:
        raise ValueError(f"projet introuvable pour cette tâche (project_id={task.project_id})")

    if get_active_task_id(conn, project.id) != task.id:
        return False

    conn.execute(
        "UPDATE projects SET active_task_id = NULL, active_task_activated_at = NULL WHERE id = ?",
        (project.id,),
    )
    conn.commit()

    append_jsonl_event(
        logs_dir,
        component="active_task",
        event="tâche désactivée",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=task.id,
        details={"project_id": project.id, "project_name": project.name, "trigger": trigger},
    )
    return True


def deactivate_task_if_active(conn: sqlite3.Connection, task_id: str, logs_dir: str | Path, trigger: str) -> None:
    """Désactivation AUTOMATIQUE — utilisée aux 4 points de sortie
    réellement terminaux du cycle de vie d'une tâche (health check
    post-promotion réussi, AUTO_ROLLBACK, rollback manuel, archivage).
    `trigger` identifie LEQUEL dans le détail journalisé (ex.
    "promote_health_check_passed", "auto_rollback", "manual_rollback",
    "archive") — distinction humain/système du même esprit que le point 3
    (is_human_decision), appliquée ici à l'attribution plutôt qu'aux
    transitions d'état. Best-effort : ne doit JAMAIS faire échouer la
    commande CLI qui l'appelle, même si la tâche/le projet ont disparu
    entre-temps, ou si la journalisation elle-même échoue."""
    try:
        deactivate_task(conn, task_id, logs_dir, trigger=trigger)
    except Exception:
        pass
