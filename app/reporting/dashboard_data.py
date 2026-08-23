"""Couche de données du dashboard V0.4 — LECTURE SEULE sur le SQLite
existant, y compris les colonnes ajoutées par des migrations ultérieures
(active_task_id/archived_at/is_human_decision, V0.5) : ce module lit le
schéma tel qu'il existe, il ne le fait jamais évoluer lui-même.

`list_human_decisions` distingue une transition humaine d'une transition
automatique via la colonne `is_human_decision` (garantie par le schéma
depuis migrations/0007_is_human_decision.sql), plus une heuristique — voir
la docstring de cette fonction pour la limite résiduelle sur l'historique
pré-migration.
"""
import json
import sqlite3

from app.state_machine.states import TaskState


def list_projects_with_task_counts(conn: sqlite3.Connection, include_archived: bool = False) -> list[dict]:
    """Un projet par entrée, avec un compteur de tâches par état (0 pour les
    états absents — pas seulement les états présents).

    Soft delete (V0.5, point 2) : exclut par défaut les projets archivés
    (archived_at IS NOT NULL) — `include_archived=True` les inclut aussi
    (chaque entrée porte `archived_at`, pour que l'appelant décide quoi en
    faire — ex. générer quand même la page de détail d'un projet archivé,
    tout en l'excluant des cartes de index.html)."""
    if include_archived:
        projects = conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
    else:
        projects = conn.execute("SELECT * FROM projects WHERE archived_at IS NULL ORDER BY name").fetchall()

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
                "archived_at": p["archived_at"],
                "task_counts": counts,
                "total_tasks": sum(counts.values()),
            }
        )
    return result


def list_tasks_for_project(conn: sqlite3.Connection, project_id: str, include_archived: bool = False) -> list[dict]:
    """Tâches d'un projet, plus récentes en premier (par updated_at).

    Soft delete (V0.5, point 2) : exclut par défaut les tâches archivées —
    `include_archived=True` les inclut aussi (chaque entrée porte
    `archived_at`)."""
    if include_archived:
        rows = conn.execute(
            "SELECT id, description, status, created_at, updated_at, archived_at "
            "FROM tasks WHERE project_id = ? ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, description, status, created_at, updated_at, archived_at "
            "FROM tasks WHERE project_id = ? AND archived_at IS NULL ORDER BY updated_at DESC",
            (project_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "description": r["description"],
            "status": r["status"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "archived_at": r["archived_at"],
        }
        for r in rows
    ]


def list_human_decisions(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Transitions d'état réellement déclenchées par un humain via
    `engine task status --to X --reason "..."`.

    Garanti par le schéma depuis V0.5 (migrations/0007_is_human_decision.sql,
    colonne is_human_decision) — plus une heuristique déduite de `reason`.
    Déterminé au moment de chaque transition par l'appelant réel de
    transition_task() (voir app/state_machine/service.py), pas deviné après
    coup. Limite résiduelle, honnête : les lignes créées AVANT cette
    migration ont is_human_decision=0 par convention rétroactive (voir le
    commentaire de la migration), pas une vérification transition par
    transition de l'historique pré-existant.
    """
    rows = conn.execute(
        """SELECT st.id, st.task_id, st.from_state, st.to_state, st.allowed, st.reason, st.created_at,
                  t.description AS task_description, t.project_id, p.name AS project_name
           FROM state_transitions st
           JOIN tasks t ON t.id = st.task_id
           JOIN projects p ON p.id = t.project_id
           WHERE st.is_human_decision = 1
           ORDER BY st.created_at DESC
           LIMIT ?""",
        (limit,),
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
