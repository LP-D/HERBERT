-- Migration 0008 : table headless_iterations (pivot orchestration headless,
-- voir docs/DECISIONS.md). Une ligne par tentative d'invocation `claude -p`
-- pilotée par app/headless_orchestrator.py — jamais écrasée, append-only,
-- même principe que commands/audit_log/state_transitions.
--
-- invocation_status reprend la même philosophie que TestResultStatus
-- (app/models/test_result.py) : des statuts explicites et honnêtes plutôt
-- qu'un booléen success/failure qui masquerait la cause réelle (timeout,
-- binaire introuvable, JSON illisible, erreur applicative renvoyée par
-- Claude Code, ou succès réel) — nécessaire pour diagnostiquer une
-- itération sans deviner depuis raw_result.
--
-- prompt_sent et raw_result sont stockés en clair (mêmes garanties que
-- raw_output dans test_results) : ni l'un ni l'autre ne contient de secret
-- par construction (le prompt est généré par build_context_prompt() à
-- partir de la tâche/du projet, jamais d'entrée utilisateur libre).

CREATE TABLE headless_iterations (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    iteration_number INTEGER NOT NULL,
    prompt_sent TEXT NOT NULL,
    raw_result TEXT,
    session_id TEXT,
    num_turns INTEGER,
    invocation_status TEXT NOT NULL,
    tests_passed BOOLEAN,
    created_at TEXT NOT NULL
);
