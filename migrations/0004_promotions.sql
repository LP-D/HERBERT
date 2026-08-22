-- Migration 0004 : table promotions pour `engine task promote` (V0.3).
-- status distingue notamment AUTO_ROLLBACK (health check post-merge en
-- échec, rollback déclenché automatiquement) d'un rollback MANUEL
-- (`engine task rollback`, journalisé dans audit_log via component
-- 'cli.task_rollback' depuis V0.2) — deux mécanismes distincts,
-- traçables séparément.

CREATE TABLE promotions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    stable_branch TEXT NOT NULL,
    candidate_branch TEXT NOT NULL,
    commit_before TEXT NOT NULL,
    commit_after TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX idx_promotions_task_id ON promotions(task_id);
