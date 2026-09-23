-- Migration 0009 : chemins protégés par projet pour classify_push.
-- Colonne additive sur projects : tableau JSON de motifs (list[str])
-- AJOUTÉS aux défauts de app/push_classifier.py::DEFAULT_BLOCKED_PATTERNS
-- — jamais un remplacement, jamais un retrait (voir
-- app/database/repository.py::add_project_blocked_patterns). NULL = aucun
-- ajout, les défauts seuls s'appliquent (pas un échec de résolution).

ALTER TABLE projects ADD COLUMN extra_blocked_patterns TEXT NULL;
