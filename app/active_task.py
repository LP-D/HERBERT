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
from app.models import Project, Task


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


def activate_task(conn: sqlite3.Connection, task_id: str) -> ActivationResult:
    """`engine task activate <task_id>` : active cette tâche pour SON
    projet (résolu depuis task.project_id, jamais besoin de le préciser).
    Écrase l'activation précédente du même projet si elle existe —
    silencieusement en base, mais `previous_task_id` est retourné pour que
    l'appelant (CLI) l'affiche : jamais une bascule cachée."""
    task = get_task(conn, task_id)
    if task is None:
        raise ValueError(f"tâche introuvable: {task_id}")
    project = get_project(conn, task.project_id)
    if project is None:
        raise ValueError(f"projet introuvable pour cette tâche (project_id={task.project_id})")

    previous_task_id = get_active_task_id(conn, project.id)

    conn.execute(
        "UPDATE projects SET active_task_id = ?, active_task_activated_at = ? WHERE id = ?",
        (task.id, datetime.now(timezone.utc).isoformat(), project.id),
    )
    conn.commit()

    return ActivationResult(project=project, task=task, previous_task_id=previous_task_id)


def deactivate_task(conn: sqlite3.Connection, task_id: str) -> bool:
    """`engine task deactivate <task_id>` : ne désactive QUE si ce task_id
    est bien la tâche active de son projet — jamais un no-op silencieux
    fondé sur une mauvaise supposition. Retourne True si une désactivation
    a réellement eu lieu."""
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
    return True


def deactivate_task_if_active(conn: sqlite3.Connection, task_id: str) -> None:
    """Désactivation AUTOMATIQUE — utilisée aux 3 points de sortie
    réellement terminaux du cycle de vie d'une tâche (health check
    post-promotion réussi, AUTO_ROLLBACK, rollback manuel). Best-effort :
    ne doit JAMAIS faire échouer la commande CLI qui l'appelle, même si la
    tâche/le projet ont disparu entre-temps."""
    try:
        deactivate_task(conn, task_id)
    except Exception:
        pass
