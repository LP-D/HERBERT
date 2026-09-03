# Journal de décisions — HERBERT

## Décision — Audit de périmètre exclu (2026-09-03)

**Verdict : aucun des 6 éléments hors périmètre n'est implémenté dans le
code.** Une seule exception near : Docker apparaît en TRACE (diagnostic
non fonctionnel), déjà documenté en README — question de statu quo
laissée ouverte en fin d'entrée.

### Portée de l'audit

- **Méthode** : `Grep` sur noms de classes/fonctions/imports évocateurs
  de chaque point, + `git log --all --grep` sur l'historique complet des
  commits (aucune limite `--all-match`, donc union des 6 recherches).
- **État du dépôt au moment de l'audit** : HEAD `261f23f` (branche
  `main`), 78 fichiers suivis par git hors `.claude/skills/` (outillage
  de conception tiers, non-HERBERT), 12 commits au total à cette date.
- **Note sur le HEAD actuel** : un commit ultérieur à l'audit,
  `74146df` (2026-09-03, "fix: détecteur settings.json — segmentation
  par ligne"), a modifié `.claude/hooks/pre_tool_use.py` — correctif
  d'un 4e faux positif de `SHELL_SEGMENT_SPLIT_RE` (segmentation
  shell), découvert en conditions réelles sur le projet `motus-solver`.
  Diff relu intégralement pour cette entrée : il ne touche à aucun des
  6 points ci-dessous. Les verdicts restent donc valides au HEAD actuel
  (`74146df`).

### Verdict par point

1. **Risk Engine (scoring multi-facteurs) — ABSENT.** Aucune classe/
   fonction de scoring. Les seules occurrences du mot "scoring" sont
   des négations explicites : `app/change_proof_builder.py:3` ("pas de
   scoring, juste une comparaison des noms de tests avant/après"),
   `migrations/0003_change_proof_regressions.sql:4`, et
   `README.md:191`. La détection de régression est une comparaison
   factuelle de listes de noms de tests, pas un moteur de risque.

2. **Trust Budget — ABSENT.** Zéro occurrence dans le code, les tests,
   la doc, les migrations, la config ou les 12 commits.

3. **Multi-agent orchestration / AgentBackend / ClaudeCodeBackend
   abstrait — ABSENT.** Aucune classe/interface `AgentBackend` ou
   `ClaudeCodeBackend`. La seule occurrence du mot "orchestration" est
   un titre de section de code dans
   `app/reporting/dashboard_builder.py:501`, désignant la fonction
   `build_dashboard()` (régénération de fichiers HTML statiques) —
   sans rapport avec une orchestration d'agents. `README.md:4` énonce
   explicitement : "Pas d'agent, pas de Docker, aucun appel API
   Anthropic".

4. **Docker sandbox — TRACE.** `app/cli/doctor.py:54-58` contient
   `check_docker()`, qui exécute `docker --version` (diagnostic
   `engine doctor` uniquement, via `subprocess`/`shutil.which`). Le
   code commente lui-même : "info seulement — non utilisé en V0.1".
   Testé dans `tests/unit/test_doctor.py:12-19` (dégradation gracieuse
   si absent). Aucune logique de conteneurisation ou de sandbox.
   Cohérent avec `README.md:191` qui déclare explicitement l'absence
   de Docker/sandbox.

5. **SupplyChainManager complet — ABSENT.** Zéro occurrence, code
   comme historique de commits.

6. **Auto-amélioration contrôlée du système sur lui-même — ABSENT.**
   Zéro occurrence. Aucun mécanisme permettant à HERBERT de modifier
   son propre comportement/code de façon autonome.

### Cohérence hooks passifs

`.claude/hooks/pre_tool_use.py` évalue et peut bloquer (exit 2) une
commande via `CommandPolicy`/`PathPolicy` (patterns statiques), mais ne
pilote jamais Claude Code de façon programmatique au-delà du contrat de
hook standard. `.claude/hooks/post_tool_use.py` ne bloque jamais (exit
0 systématique — "ce hook est un journal, pas un filtre", docstring du
module). Seuls 3 fichiers dans `app/` utilisent `subprocess` :
`pytest_runner.py` (exécution de la suite de tests du projet cible),
`git_wrapper.py` (commandes git), `doctor.py` (`--version` checks,
y compris `check_claude_code()` qui vérifie juste la présence du
binaire `claude`). Aucun code ne lance ou ne pilote Claude Code de
façon interactive.

### Question ouverte — Docker (TRACE), à trancher

Deux options, non tranchées ici :

- **Statu quo** : conserver `check_docker()` tel quel — diagnostic
  d'environnement seul, non fonctionnel, déjà documenté comme tel en
  README.
- **Retrait complet** : supprimer `check_docker()` de `app/cli/doctor.py`
  (et le test associé dans `tests/unit/test_doctor.py`) pour ne laisser
  aucune trace, même non fonctionnelle, du périmètre exclu.
