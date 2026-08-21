-- Migration 0001: schéma initial HERBERT V0.1
-- 6 tables, foreign keys activées par la connexion (PRAGMA foreign_keys=ON),
-- timestamps stockés en UTC ISO8601 (TEXT), identifiants en UUID (TEXT).

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE tasks (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RECEIVED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE state_transitions (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    allowed INTEGER NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE commands (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    tool_name TEXT NOT NULL,
    command TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE change_proofs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    files_changed TEXT NOT NULL,
    commands_executed TEXT NOT NULL,
    tests_passed INTEGER NOT NULL DEFAULT 0,
    tests_failed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE audit_log (
    id TEXT PRIMARY KEY,
    task_id TEXT,
    component TEXT NOT NULL,
    event TEXT NOT NULL,
    level TEXT NOT NULL,
    status TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX idx_tasks_project_id ON tasks(project_id);
CREATE INDEX idx_state_transitions_task_id ON state_transitions(task_id);
CREATE INDEX idx_commands_task_id ON commands(task_id);
CREATE INDEX idx_change_proofs_task_id ON change_proofs(task_id);
CREATE INDEX idx_audit_log_task_id ON audit_log(task_id);
