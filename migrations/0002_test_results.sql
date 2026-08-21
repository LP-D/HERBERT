-- Migration 0002 : table test_results pour `engine task test` (V0.2).
-- test_cases stocke un JSON list [{"name": ..., "outcome": ...}, ...],
-- nécessaire pour la détection de régression par nom de test individuel.

CREATE TABLE test_results (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    passed INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    raw_output TEXT,
    test_cases TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX idx_test_results_task_id ON test_results(task_id);
