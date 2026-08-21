-- Migration 0003 : ChangeProof.regressions (V0.2).
-- Liste JSON des noms de tests qui passaient au dernier run VERIFIED_PASS
-- du même projet et échouent maintenant. Comparaison factuelle, pas de
-- scoring. Migration séparée de 0002 : ne jamais éditer une migration déjà
-- appliquée (bug réel constaté — voir historique du projet).

ALTER TABLE change_proofs ADD COLUMN regressions TEXT NOT NULL DEFAULT '[]';
