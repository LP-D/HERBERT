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

## Décision — Pivot orchestration headless V0.6 (2026-09-04)

**Le principe "Claude Code jamais une dépendance de HERBERT" (README.md,
V0.1) est levé, explicitement, dans ce commit.** HERBERT peut désormais
invoquer `claude` en headless pour une tâche (`engine task run-headless`),
avec relance automatique bornée en cas d'échec de tests. Décidé et
implémenté sur demande explicite, pas une dérive de périmètre silencieuse
— contrairement aux 6 points ABSENTS de l'audit précédent (entrée
ci-dessus), qui restent ABSENTS et non concernés par ce pivot (pas de
scoring de risque, pas de Trust Budget, pas de Docker/sandbox, pas de
SupplyChainManager, pas d'auto-amélioration du code de HERBERT
lui-même). Ce pivot est un `AgentBackend` délibérément étroit — une
seule boucle, un seul modèle fixé, un plafond strict — pas
l'orchestration multi-agent générique qui restait explicitement exclue.

### Correction de fait (demandée explicitement, vérifiée avant d'écrire cette ligne)

Une note de mémoire antérieure mentionnait "state machine 7 états" — 
vérifié par lecture directe de `app/state_machine/states.py` : la
state machine a **9 états** (`RECEIVED, EXECUTING, TESTING, DONE, FAILED,
BLOCKED, HUMAN_REQUIRED, PROMOTED, ROLLED_BACK`), pas 7. Le chiffre "7"
était erroné avant même ce pivot.

### Ce qui change concrètement

- **Nouveau module `app/claude_headless.py`** : construction de prompt
  contextuel borné (tâche, projet, branche candidate, échec précédent le
  cas échéant — jamais de citation d'un document externe non lisible par
  ce code, voir ci-dessous) + invocation réelle (`subprocess`, calqué sur
  `app/pytest_runner.py`) de `claude -p ... --output-format json --model
  <model> --permission-mode bypassPermissions`.
- **Nouveau module `app/headless_orchestrator.py`** : boucle plafonnée à
  `config/system.yaml:claude_headless.max_iterations` (3 par défaut),
  bornée structurellement (`range(...)`, jamais un `if` contournable —
  testé explicitement à la limite, voir tests/unit/test_headless_orchestrator.py).
  Réutilise sans réimplémenter : `cmd_task_test` (checkout, pytest réel,
  ChangeProof, transition d'état, dashboard) après CHAQUE itération,
  `classify_push` (inchangée) en fin de boucle réussie,
  `activate_task`/`deactivate_task_if_active` pour l'attribution task_id
  des hooks.
- **Nouvelle table `headless_iterations`** (migration 0008) : une ligne
  par tentative, statuts honnêtes (`HeadlessInvocationStatus` —
  VERIFIED/INVOCATION_FAILED/TIMED_OUT/NOT_EXECUTED/UNAVAILABLE), jamais
  un booléen success/failure qui masquerait la cause réelle.
- **`push_to_origin`/`cmd_sync_push` INTOUCHÉS.** Vérifié par lecture de
  code ET par un test qui fait échouer volontairement le test s'il est
  jamais appelé (`test_run_headless_task_never_calls_push_to_origin`).
  `engine sync push` reste l'unique porte vers un push réel, portée
  HERBERT lui-même (REPO_ROOT), inchangée.

### Modèle explicitement fixé (jamais un défaut implicite)

`~/.claude/settings.json` (global, hors dépôt HERBERT) fixe `"model": "haiku"`
comme défaut CLI — vérifié directement, pas supposé. Insuffisant pour une
boucle de correction de code réelle. `invoke_claude_headless()` prend
donc `model` en paramètre OBLIGATOIRE, jamais un défaut implicite du CLI
`claude` — fixé à `claude-sonnet-5` dans `config/system.yaml` (section
`claude_headless`, modifiable), cohérent avec le modèle utilisé de façon
interactive sur ce projet.

### classify_push : portée corrigée par rapport à l'usage existant

`cmd_sync_push`/`classify_push` (usage EXISTANT, `app/cli/main.py:938`)
opèrent exclusivement sur `REPO_ROOT` (le dépôt HERBERT lui-même) —
vérifié par lecture complète de la fonction, aucune notion de projet
cible. `app/headless_orchestrator.py` introduit un NOUVEAU site d'appel
qui calcule `files_changed`/`total_diff_lines` sur `project.path` (le
projet cible de la tâche, via `base_commit` enregistré par `engine task
branch`) et réutilise `classify_push()` telle quelle (fonction pure,
inchangée) sur ces données. **Limite documentée, pas cachée** :
`BLOCKED_PATH_PREFIXES` (`.claude/settings.json`, `app/policy/`, etc.)
reste une liste de chemins propres à HERBERT — elle ne protège rien de
spécifique à un projet cible. Seuls les critères génériques (exactement
1 fichier commité, < 20 lignes, tests VERIFIED_PASS) sont pleinement
pertinents hors HERBERT.

### Transitions d'état ajoutées (2 arêtes, aucun nouvel état)

- `FAILED → BLOCKED` : plafond d'itérations headless épuisé sans tests
  VERIFIED_PASS — jamais une 4e tentative silencieuse, la tâche doit
  devenir visible comme BLOCKED. Absente jusqu'ici car FAILED n'était
  atteint que par un humain (qui repart en EXECUTING ou escalade en
  HUMAN_REQUIRED).
- `DONE → HUMAN_REQUIRED` : tests passés, mais `classify_push` classe le
  diff du projet cible MANUAL_REQUIRED — la tâche reste "code
  fonctionnel" (DONE au sens fonctionnel) mais son passage à
  PROMOTED/push nécessite une décision humaine.
- Décision volontaire de réutiliser `HUMAN_REQUIRED` existant plutôt
  qu'un nouvel état `WAITING` distinct : sémantiquement équivalent
  ("nécessite une décision humaine"), évite d'alourdir une state machine
  à 9 états d'une transition/documentation/test supplémentaires sans
  bénéfice réel.

### Timeout et crash d'invocation : traitement uniforme

Un timeout (`config/system.yaml:claude_headless.timeout_seconds`, 600s
par défaut) ou un crash d'invocation (binaire introuvable, JSON
illisible, `is_error: true` renvoyé par Claude Code) consomment CHACUN
une itération sur le plafond, au même titre qu'un vrai échec de tests —
pas de court-circuit spécial : le coût réel d'une tentative "ratée avant
même de commencer" (ex. binaire absent) est négligeable (pas d'appel
réseau), donc pas de complexité de code supplémentaire pour l'éviter.
`cmd_task_test` tourne quand même après CHAQUE itération, y compris après
un problème d'invocation — signal honnête sur l'état réel du dépôt
plutôt qu'un statut inventé. La cause précise (timeout vs échec
d'invocation vs vrai échec de tests) est diagnosticable dans le dashboard
existant SANS nouveau code de rendu : `advance_after_test_result()` prend
désormais un paramètre `reason` optionnel (défaut `None`, comportement
inchangé pour tout appelant existant), renseigné explicitement par
l'orchestrateur uniquement pour les 2 cas "invocation allée mal" — un
vrai échec de tests garde `reason=None`, comme aujourd'hui.
`subprocess.run(..., timeout=...)` tue le process avant de lever
l'exception (garanti par la stdlib) : aucun risque de zombie.

### Aucune référence au PDF externe dans le code

Un document PDF de référence a été fourni hors dépôt pour orienter la
conception (gestion de contexte du prompt headless). Décision explicite :
aucune citation de ce document dans le code, les docstrings, ou cette
entrée — HERBERT ne peut pas le lire/vérifier lui-même, en faire une
référence créerait une affirmation non vérifiable par le système. Le
prompt (`build_context_prompt`) suit le principe général déjà énoncé
(contexte pertinent et borné : tâche, projet, échec précédent — jamais
un prompt gigantesque) sans dépendre de cette source.

### Vérification live — PENDING, bloqué par l'environnement, pas par le code

Deux vérifications marquées obligatoires n'ont pas pu être exécutées en
conditions réelles : une invocation headless réussie de bout en bout, et
le test hooks-en-headless-via-HERBERT (Write direct sur
`.claude/settings.json` déclenché depuis une vraie session headless).
Cause vérifiée empiriquement (pas supposée), reproduite 3 fois :
`claude -p ... --output-format json` échoue systématiquement à
l'authentification dans ce contexte (`"Failed to authenticate: OAuth
session expired and could not be refreshed"`, confirmé par
`claude doctor` : "Not signed in to claude.ai") — un sous-processus
`claude` lancé depuis cette session n'hérite pas de son authentification
et ne peut pas se réauthentifier de façon non-interactive (pas
d'`ANTHROPIC_API_KEY` disponible non plus). Hors du périmètre d'action de
HERBERT (l'authentification OAuth est une action explicitement réservée
à l'utilisateur). La couverture unitaire (`tests/unit/test_claude_headless.py`,
`tests/unit/test_headless_orchestrator.py`, subprocess simulé) couvre la
logique de boucle/parsing/transitions, mais ne remplace pas ces deux
vérifications live — à refaire dès que l'authentification headless est
en place.
