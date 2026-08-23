-- Migration 0007 : distinction garantie par le schéma entre une transition
-- déclenchée par un humain et une transition automatique (V0.5, point 3).
-- Remplace l'heuristique de app/reporting/dashboard_data.py (déduction sur
-- `reason`), qui reste documentée comme approximative pour les lignes
-- créées AVANT cette migration.
--
-- DEFAULT 0 (faux) : à la date de cette migration, les VRAIS sites d'appel
-- de transition_task() dans app/state_machine/service.py sont TOUS
-- automatiques (advance_after_test_result, appelée depuis `engine task
-- test`, ne prend jamais de saisie humaine) — le seul site humain
-- (cmd_task_status dans app/cli/main.py, --to/--reason tapés par un
-- humain) passe désormais is_human_decision=True explicitement à chaque
-- appel. DEFAULT 0 reflète donc fidèlement l'usage majoritaire réel du
-- code, pas une supposition. Pour les lignes déjà en base AVANT cette
-- migration, 0 est une CONVENTION rétroactive, pas une affirmation de
-- fait vérifiée transition par transition — voir docs/DASHBOARD.md.

ALTER TABLE state_transitions ADD COLUMN is_human_decision BOOLEAN NOT NULL DEFAULT 0;
