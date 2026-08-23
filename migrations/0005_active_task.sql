-- Migration 0005 : "tâche active" par projet (V0.5, point 1).
-- Colonnes additives sur projects, pas de nouvelle table : l'isolation par
-- projet vient du fait que active_task_id vit sur la ligne du projet
-- lui-même, pas d'un état global à synchroniser. Voir app/active_task.py.

ALTER TABLE projects ADD COLUMN active_task_id TEXT NULL REFERENCES tasks(id);
ALTER TABLE projects ADD COLUMN active_task_activated_at TEXT NULL;
