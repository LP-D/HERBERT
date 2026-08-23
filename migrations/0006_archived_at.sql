-- Migration 0006 : suppression propre / soft delete (V0.5, point 2).
-- L'audit reste append-only (audit_log, state_transitions, commands,
-- change_proofs) : cette migration n'y touche pas. Seules projects et
-- tasks reçoivent une colonne additive archived_at (NULL = actif), lue
-- par les vues de listing par défaut (engine project list,
-- dashboard_data.py) pour masquer sans jamais supprimer physiquement.

ALTER TABLE projects ADD COLUMN archived_at TEXT NULL;
ALTER TABLE tasks ADD COLUMN archived_at TEXT NULL;
