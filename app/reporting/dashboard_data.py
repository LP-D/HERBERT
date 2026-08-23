"""Couche de données du dashboard V0.4 — LECTURE SEULE sur le SQLite
existant. Aucune nouvelle table, aucune migration : uniquement des
requêtes sur le schéma déjà en place (voir migrations/).

Limite connue et documentée (voir docs/DASHBOARD.md) : `state_transitions`
n'a aucune colonne distinguant une transition déclenchée par un humain
(`engine task status --to X`) d'une transition automatique
(`advance_after_test_result`, `engine task promote`). `list_human_decisions`
utilise donc une heuristique sur `reason` (exclut les libellés système fixes
connus ET les lignes sans `reason`), pas une distinction garantie par le
schéma — voir la docstring de cette fonction.
"""
import json
import sqlite3

from app.state_machine.states import TaskState

# Libellés `reason` générés par du code système (pas tapés par un humain) —
# doivent rester synchronisés avec app/cli/main.py::cmd_task_promote. Aucune
# source unique de vérité pour ces chaînes actuellement (limite connue).
_SYSTEM_TRANSITION_REASONS = {
    "merge de promotion réussi",
    "health check post-promotion réussi",
    "AUTO_ROLLBACK: merge reverté après échec du health check",
}


def list_projects_with_task_counts(conn: sqlite3.Connection) -> list[dict]:
    """Un projet par entrée, avec un compteur de tâches par état (0 pour les
    états absents — pas seulement les états présents)."""
    projects = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()

    result = []
    for p in projects:
        counts = {state.value: 0 for state in TaskState}
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks WHERE project_id = ? GROUP BY status", (p["id"],)
        ).fetchall()
        for r in rows:
            counts[r["status"]] = r["n"]

        result.append(
            {
                "id": p["id"],
                "name": p["name"],
                "path": p["path"],
                "created_at": p["created_at"],
                "task_counts": counts,
                "total_tasks": sum(counts.values()),
            }
        )
    return result


def list_tasks_for_project(conn: sqlite3.Connection, project_id: str) -> list[dict]:
    """Tâches d'un projet, plus récentes en premier (par updated_at)."""
    rows = conn.execute(
        """SELECT id, description, status, created_at, updated_at
           FROM tasks WHERE project_id = ? ORDER BY updated_at DESC""",
        (project_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "description": r["description"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


def list_human_decisions(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Transitions d'état probablement déclenchées par un humain via
    `engine task status --to X --reason "..."`.

    Heuristique, pas une distinction garantie par le schéma (voir docstring
    du module) : exclut les lignes dont `reason` est NULL (indiscernables
    des avancées automatiques de `advance_after_test_result`, qui utilisent
    toujours reason=NULL) et les lignes dont `reason` correspond à un
    libellé système fixe connu (`_SYSTEM_TRANSITION_REASONS`). Une décision
    humaine prise SANS `--reason` n'apparaîtra donc pas ici — c'est une
    conséquence directe de cette limite, pas un bug de la requête.
    """
    placeholders = ",".join("?" for _ in _SYSTEM_TRANSITION_REASONS)
    rows = conn.execute(
        f"""SELECT st.id, st.task_id, st.from_state, st.to_state, st.allowed, st.reason, st.created_at,
                   t.description AS task_description, t.project_id, p.name AS project_name
            FROM state_transitions st
            JOIN tasks t ON t.id = st.task_id
            JOIN projects p ON p.id = t.project_id
            WHERE st.reason IS NOT NULL AND st.reason NOT IN ({placeholders})
            ORDER BY st.created_at DESC
            LIMIT ?""",
        (*_SYSTEM_TRANSITION_REASONS, limit),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "task_id": r["task_id"],
            "task_description": r["task_description"],
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "from_state": r["from_state"],
            "to_state": r["to_state"],
            "allowed": bool(r["allowed"]),
            "reason": r["reason"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def list_rollbacks(conn: sqlite3.Connection) -> list[dict]:
    """Historique des rollbacks — MANUEL (`engine task rollback`, journalisé
    dans audit_log sous le composant `cli.task_rollback`) et AUTOMATIQUE
    (`engine task promote`, table `promotions`, status AUTO_ROLLBACK ou
    ROLLBACK_FAILED) — deux mécanismes distincts, jamais confondus (voir
    migrations/0004_promotions.sql)."""
    manual_rows = conn.execute(
        """SELECT al.id, al.task_id, al.status, al.details, al.created_at,
                  t.description AS task_description, t.project_id, p.name AS project_name
           FROM audit_log al
           JOIN tasks t ON t.id = al.task_id
           JOIN projects p ON p.id = t.project_id
           WHERE al.component = 'cli.task_rollback'
           ORDER BY al.created_at DESC"""
    ).fetchall()

    auto_rows = conn.execute(
        """SELECT pr.id, pr.task_id, pr.status, pr.commit_before, pr.commit_after, pr.created_at,
                  t.description AS task_description, t.project_id, p.name AS project_name
           FROM promotions pr
           JOIN tasks t ON t.id = pr.task_id
           JOIN projects p ON p.id = t.project_id
           WHERE pr.status IN ('AUTO_ROLLBACK', 'ROLLBACK_FAILED')
           ORDER BY pr.created_at DESC"""
    ).fetchall()

    result = [
        {
            "id": r["id"],
            "kind": "MANUAL",
            "task_id": r["task_id"],
            "task_description": r["task_description"],
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "status": r["status"],
            "detail": json.loads(r["details"]) if r["details"] else {},
            "created_at": r["created_at"],
        }
        for r in manual_rows
    ] + [
        {
            "id": r["id"],
            "kind": r["status"],  # AUTO_ROLLBACK ou ROLLBACK_FAILED
            "task_id": r["task_id"],
            "task_description": r["task_description"],
            "project_id": r["project_id"],
            "project_name": r["project_name"],
            "status": r["status"],
            "detail": {"commit_before": r["commit_before"], "commit_after": r["commit_after"]},
            "created_at": r["created_at"],
        }
        for r in auto_rows
    ]
    result.sort(key=lambda r: r["created_at"], reverse=True)
    return result


def list_audit_log(conn: sqlite3.Connection, limit: int = 1000) -> list[dict]:
    """Entrées audit_log récentes, toutes tâches/projets confondus, plus
    récentes en premier. `limit` est un plafond d'extraction SQLite, pas une
    pagination serveur (le dashboard est un fichier statique, sans serveur —
    voir docs/DASHBOARD.md pour comment `dashboard_builder` restitue ceci
    comme un « charger plus » cliquable côté client, sans requête réseau)."""
    rows = conn.execute(
        """SELECT al.id, al.task_id, al.component, al.event, al.level, al.status, al.details, al.created_at,
                  t.description AS task_description, p.name AS project_name
           FROM audit_log al
           LEFT JOIN tasks t ON t.id = al.task_id
           LEFT JOIN projects p ON p.id = t.project_id
           ORDER BY al.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "task_id": r["task_id"],
            "task_description": r["task_description"],
            "project_name": r["project_name"],
            "component": r["component"],
            "event": r["event"],
            "level": r["level"],
            "status": r["status"],
            "details": json.loads(r["details"]) if r["details"] else {},
            "created_at": r["created_at"],
        }
        for r in rows
    ]
