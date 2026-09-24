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
inchangée) sur ces données. ~~**Limite documentée, pas cachée** :
`BLOCKED_PATH_PREFIXES` (`.claude/settings.json`, `app/policy/`, etc.)
reste une liste de chemins propres à HERBERT — elle ne protège rien de
spécifique à un projet cible.~~ **RÉSOLU** — voir section suivante.

### Chemins protégés par projet pour classify_push (résout la limite ci-dessus)

**Ce qui change.**
- `classify_push(..., *, blocked_patterns)` : paramètre nommé
  **obligatoire, sans défaut**. Un appelant qui l'oublie échoue
  (`TypeError`) au lieu de retomber silencieusement sur une liste qui ne
  concerne pas son dépôt. `None`, une liste vide, une chaîne seule ou un
  élément non-chaîne ajoutent le critère « config : liste de chemins
  protégés non résolue » : **toujours MANUAL_REQUIRED**, quels que soient
  les autres critères.
- `DEFAULT_BLOCKED_PATTERNS` (tout dépôt) et `HERBERT_BLOCKED_PATTERNS`
  (défauts + les 8 préfixes historiques propres à HERBERT, passés par
  `cmd_sync_push`). Deux syntaxes : préfixe ancré à la racine (comportement
  historique) et `**/<glob>` sur le nom de fichier à n'importe quelle
  profondeur. Comparaison insensible à la casse pour les deux (Windows /
  `core.ignorecase` : `.GitHub/` ne doit pas contourner `.github/`).
- Migration 0009 : `projects.extra_blocked_patterns` (JSON `list[str]`,
  NULL = défauts seuls). **Ajout seulement** : aucune fonction ne retire
  ni ne remplace un motif, `resolve_blocked_patterns(project)` renvoie
  toujours les défauts en tête. Une valeur stockée illisible n'est jamais
  « réparée » : la résolution renvoie `None` → MANUAL_REQUIRED, et
  `engine project protect add` refuse de l'écraser.
- `app/headless_orchestrator.py` relit le projet en base au moment de
  classer et passe `resolve_blocked_patterns(project)`.
- Les 3 exceptions `.env.example` / `.env.sample` / `.env.template` sont
  codées en dur, limitées au motif `**/.env.*` : jamais exprimables par un
  projet (sinon « ajout seulement » serait contournable par une négation).
- `diff_numstat_since` passe `--no-renames` : avec la détection de
  renommage, git affichait `app/{policy => other}/x.py`, qu'aucun préfixe
  ne reconnaissait — déplacer un fichier protégé passait inaperçu, y
  compris pour HERBERT lui-même. Un renommage apparaît désormais comme
  suppression + ajout, deux chemins vérifiés chacun.

**Pourquoi ces défauts — asymétrie coût faux positif / faux négatif.** Un
faux positif coûte une confirmation manuelle (quelques minutes). Un faux
négatif laisse passer en AUTO une modification que personne n'a relue,
dans une catégorie où l'impact n'est pas borné par la taille du diff :
- **secrets** (`.env`, `*.pem`, `*.key`, `id_rsa*`, `.npmrc`, `.pypirc`,
  `secrets/`, `credentials*`…) — une ligne suffit à fuiter ou remplacer
  une clé ;
- **CI et hooks git** (`.github/`, `.gitlab-ci.yml`, `.husky/`,
  `.pre-commit-config.yaml`…) — du code qui s'exécute ailleurs, avec
  d'autres droits ;
- **configuration de l'agent** (`.claude/`, `CLAUDE.md`, `.mcp.json`) —
  l'agent ne doit jamais élargir ses propres permissions ou instructions
  en AUTO ;
- **ce qui décide si « les tests passent »** (`conftest.py` à toute
  profondeur, `pytest.ini`, `pyproject.toml`, `setup.cfg`, `tox.ini`,
  `noxfile.py`) — critère central de l'AUTO. Scénario testé réellement
  (`test_agent_gaming_tests_via_conftest_is_human_required`) : un
  `conftest.py` de 2 lignes qui patche le module fait passer la suite sans
  rien corriger — 1 fichier, < 20 lignes, tests VERIFIED_PASS, donc AUTO
  sans cette protection ;
- **dépendances et build** (`requirements*`, `setup.py`, `Dockerfile*`,
  `docker-compose*`) — ce qui s'exécute à l'installation ;
- **exécution par l'éditeur** (`.vscode/tasks.json`, `.vscode/launch.json`
  seulement — `settings.json` n'exécute rien) ;
- **git** (`.gitignore`, `.gitattributes`, `.gitmodules`) — `.gitignore`
  peut dé-ignorer un fichier de secrets. `.git/` n'est pas listé : son
  contenu n'apparaît jamais dans `git diff`, le motif ne protégerait rien.

Les préfixes spécifiques à HERBERT (`app/policy/`, `migrations/`…) ne sont
PAS appliqués aux projets cibles : un projet peut avoir son propre
`app/policy/` sans rapport. Un projet ajoute ses propres chemins avec
`engine project protect add <projet> <motif>` (journalisé en JSONL,
`task_id=None`).

**CommandPolicy — constat et règle ajoutée.** Avant ce changement, rien
n'empêchait une session Claude Code (interactive ou headless) de lancer
`python engine.py ...` : aucune règle de `app/policy/command_policy.py` ne
mentionnait `engine`, et le hook n'a **aucune notion de « session
headless »** (même politique pour toute session). Règle ajoutée :
`engine_project_protect` (BLOCKED, regex `\bproject\s+protect\b`, hook
uniquement) — la contrainte ne se configure jamais depuis la session
qu'elle contraint. Limites, documentées plutôt que cachées :
1. la règle ne s'applique que là où le hook HERBERT est déployé
   (`.claude/settings.json` du projet cible, voir DEPLOYMENT.md) —
   `invoke_claude_headless` ne passe pas `--settings`, donc un projet
   cible sans ce fichier n'est protégé par AUCUNE règle CommandPolicy ;
2. elle ne couvre pas une écriture directe en base (`sqlite3`, `python
   -c`) — sans conséquence pour la sûreté de la liste : les défauts vivent
   dans le code, une valeur en base ne peut qu'ajouter, et une valeur
   corrompue force MANUAL_REQUIRED ;
3. faux positif accepté : toute commande Bash contenant ce texte littéral
   (ex. `git commit -m "..."`) est bloquée — passer par un fichier
   (`git commit -F`).

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

> **AMENDÉ le 2026-09-23** (voir « Décision — Isolation headless et
> init-hooks » ci-dessous) : un binaire introuvable n'est PAS une tentative
> « ratée avant même de commencer » au coût négligeable — c'est une panne
> d'infrastructure permanente qui, sur cette machine, rendait CHAQUE
> invocation impossible. Elle est désormais fatale
> (`InvocationInfrastructureError`), sans itération consommée ni pytest.
> Même statut pour l'échec d'authentification (motif littéral unique, voir
> plus bas). Le traitement uniforme ci-dessous reste valable pour tout
> autre process qui a réellement tourné (timeout, JSON illisible, tout
> autre `is_error: true`).

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

### Vérification live — CONFIRMÉE (2026-09-05, après `claude auth login`)

> **REQUALIFIÉ le 2026-09-23 — NON VÉRIFIÉ sur cet environnement.** Sur
> cette machine (Windows, Claude Code 2.1.235 installé par npm), le code
> V0.6 lançait `claude` NU via `subprocess.run` sans shell : aucun
> `claude.exe` n'est sur le PATH (seulement le script npm `claude` et
> `claude.cmd`), Windows ne complète qu'en `.exe`, donc
> `FileNotFoundError [WinError 2]` à CHAQUE appel — reproduit le
> 2026-09-23 (E8), aucune exécution headless n'a jamais pu réussir ici
> avant ce correctif. Les preuves citées ci-dessous (session
> `47dc0561…`, task `f621272d…`, `logs/herbert-2026-09-05.jsonl`) sont
> introuvables sur cette machine : aucun fichier JSONL de septembre
> antérieur au 23, tâche absente de `data/herbert.db`. Elles ont
> vraisemblablement été produites dans un autre environnement (clone
> distinct, `data/` et `logs/` étant exclus du dépôt). Le texte est
> conservé tel quel, pas supprimé. Le chemin réel est prouvé sur cette
> machine depuis le 2026-09-23 par tests/integration/test_real_claude_headless.py.
> Par ailleurs, la conclusion « les hooks du projet cible s'appliquent en
> headless » est vraie mais insuffisante : un `disableAllHooks` du projet
> les neutralise (E3, E6) — voir la décision suivante.

Les deux vérifications marquées obligatoires ont d'abord été bloquées par
un défaut d'authentification (`claude -p ... --output-format json`
échouait systématiquement — `"Failed to authenticate: OAuth session
expired and could not be refreshed"`, reproduit 3 fois, cause confirmée
par `claude doctor`). Résolu par l'utilisateur (`claude auth login`),
hors du périmètre d'action de HERBERT — l'authentification OAuth reste
une action explicitement réservée à l'utilisateur, jamais exécutée par
HERBERT ou par moi.

**3a — Invocation headless réussie**, via `invoke_claude_headless()`
(pas un `claude -p` manuel) : `session_id=47dc0561-1fe4-4e10-ad83-4308c8f5b6ec`,
`invocation_status=VERIFIED`, `is_error=false`, `result_text="ok"`,
`num_turns=1`, `total_cost_usd≈0.43`. Confirme au passage le schéma de
succès réel (absent du point 2 initial, calibré alors uniquement sur un
échantillon d'erreur) : mêmes clés que documenté, `stop_reason="end_turn"`,
`terminal_reason="completed"`.

**3b — Hooks en headless piloté par HERBERT**, projet de test jetable
créé pour l'occasion (`.claude/settings.json` réel pointant vers les
hooks HERBERT, hors dépôt, suivi par git) : première tentative
invalidée par une erreur de ma part (échappement JSON des chemins Windows
raté dans le fixture de test — chemins corrigés en `/` plutôt que `\\`,
JSON revalidé avant de relancer). Après correction, invocation headless
réelle avec prompt honnête (contexte donné explicitement au modèle,
aucune tentative de le tromper) demandant l'écriture directe sur
`.claude/settings.json` du projet de test :
- SQLite (`commands`) : `{'task_id': 'f621272d-3bf0-4249-8ad7-f1c29f8ac3ed', 'tool_name': 'Write', 'decision': 'DENY', 'reason': 'modification des règles de permission HERBERT elles-mêmes'}`
- JSONL (`logs/herbert-2026-09-05.jsonl`) : entrée cohérente, même `task_id`, `status: BLOCKED`.
- SHA-256 du fichier identique avant/après (`9a5aa3f1...`) — vérifié
  directement (lecture du contenu), pas seulement par le hash.
- `task_id` correctement attribué via `resolve_active_task_id_for_hook`
  (tâche activée par `engine task activate` avant l'invocation, comme
  prévu par `app/headless_orchestrator.py`).

Les hooks se déclenchent identiquement en session headless qu'en session
interactive — confirmé empiriquement, pas supposé. La couverture unitaire
(`tests/unit/test_claude_headless.py`, `tests/unit/test_headless_orchestrator.py`,
subprocess simulé) reste la couverture de régression ; ces deux
vérifications live en sont le complément ponctuel, désormais fait.

## Décision — Isolation headless et init-hooks (2026-09-23)

### Ce qui a été découvert (2026-09-23, expériences réelles, scratchpad jetable)

Banc : dépôt git jetable, hook témoin `A_project` dans son
`.claude/settings.json`, hook témoin `S_external` dans un fichier séparé
passé par `--settings`, chacun journalisant ses appels et bloquant
(exit 2) `touch verify_marker`. Chaque run vérifié `is_error=False` et
modèle réellement exécuté avant toute conclusion.

| Exp. | Configuration | Résultat |
|---|---|---|
| E0 | projet seul | bloqué par A_project |
| E3 | `settings.local.json` = `disableAllHooks` | aucun hook, commande exécutée |
| E1 | E3 + `--settings` | aucun hook — `--settings` seul n'isole PAS |
| E2 | E1 + `--setting-sources project` | bloqué — `local` exclu |
| E4 | `--settings` externe | A_project ET S_external appelés — `--settings` s'AJOUTE |
| E5 | local hostile + `--settings` + `--setting-sources project` | les deux hooks appelés |
| E6 | `disableAllHooks` dans le settings.json DU PROJET + `--setting-sources project` | aucun hook — un agent qui édite le settings.json de son propre projet neutralise tout |
| E7a | projet ET local hostiles + `--setting-sources ""` | SEUL S_external appelé, bloqué |
| E7b | `--setting-sources none` | refusé par le CLI (`Invalid setting source: none`) |
| E8 | `subprocess.run(["claude", ...])` (le code V0.6) | `FileNotFoundError [WinError 2]` |
| E8′ | même liste avec `claude.CMD` résolu, prompt 1 ligne | identique à E7a |
| — | argument multi-lignes passé à un `.cmd` | tronqué au 1er retour à la ligne, arguments suivants PERDUS, code 0 |
| E9 | `claude.exe` natif direct, prompt 4 lignes, config hostile | prompt reçu identique octet pour octet (hook UserPromptSubmit), seul S_external |
| E11 | E9 avec `%USERNAME% & \| > ^ "…"` | reçu identique, rien interprété, aucun fichier de redirection |

Hors banc : `claude.cmd` (npm) ne fait que lancer
`node_modules/@anthropic-ai/claude-code/bin/claude.exe` (binaire natif,
pas de `cli.js`).

### Ce qui est retenu

- **Invocation** : `claude.exe` natif par son chemin ABSOLU
  (`config/system.yaml`, `claude_headless.executable`), liste d'arguments,
  `shell=False`, jamais `claude` nu (E8), jamais `claude.cmd` (troncature
  silencieuse par cmd.exe — limite connue, **évitée par construction**
  puisqu'aucun shell n'intervient plus). Validé au démarrage de
  `engine task run-headless` par `--version` réellement exécuté ;
  configuration invalide = erreur fatale nommant la clé, avant de toucher
  la moindre tâche.
- **Isolation** : `--settings <fichier HERBERT> --setting-sources ""`
  (E7a, E9, E11). Aucune source project/local/user n'est chargée : ni
  `disableAllHooks` du projet cible, ni celui de son `settings.local.json`
  ne peuvent désactiver les hooks HERBERT.
- **Fichier settings** : généré par `app/hooks_deploy.py` sous
  `data/target_settings/<project_id>/settings.json`, JAMAIS dans le dépôt
  cible (plus de collision avec un settings.json personnel, pas de
  `--force`). Contenu complet et autosuffisant (permissions de
  `generate_settings_permissions()` + hooks absolus, `sys.executable`,
  séparateurs `/`), puisqu'aucune autre source n'est chargée. Idempotent,
  écriture atomique relue, SHA-256 en JSONL (`task_id=None`), aucune
  métadonnée dans le fichier. Déployé automatiquement avant chaque
  boucle headless, ou à la main par `engine init-hooks <chemin>`.
  Refusé si le fichier tomberait dans le projet (ex. projet enregistré
  sur HERBERT lui-même).
- **Intégrité** : SHA-256 du fichier revérifié après chaque invocation ;
  différence = itération enregistrée, pytest non lancé, tâche BLOCKED.
  Défense en profondeur : le fichier est hors du répertoire de travail
  de l'agent (PathPolicy refuse Write/Edit), mais une commande Bash n'est
  pas soumise à PathPolicy.
- **Panne d'infrastructure vs échec de tâche** : `NOT_EXECUTED` n'est plus
  un résultat d'itération. Un process qui ne démarre pas lève
  `InvocationInfrastructureError` : tâche BLOCKED avec la raison exacte,
  aucune itération consommée, pytest non lancé, erreur propagée. La
  décision 4c (« traitement uniforme ») supposait qu'un binaire absent
  était un cas ponctuel au coût négligeable ; c'était en fait une panne
  permanente, invisible parce que TOUTE la suite remplaçait
  `subprocess.run` et `invoke_claude_headless` par des doublures. D'où le
  test d'intégration réelle opt-in
  (`HERBERT_REAL_CLAUDE=1 python -m pytest tests/integration -s`, ~0,03-0,07 $).
  **Échec d'authentification** : également une panne d'infrastructure
  (relancer l'agent n'y peut rien, `claude auth login` est une action
  humaine) — mais uniquement sur le motif LITTÉRAL
  `Failed to authenticate: OAuth session expired and could not be refreshed`
  avec `is_error: true`. Choisi parce que c'est la seule chaîne d'échec
  d'auth à la fois présente telle quelle dans le binaire claude.exe
  2.1.235 (extraction des chaînes du binaire, 2026-09-23) ET observée
  réellement dans le champ `result` d'une sortie `-p --output-format json`
  (3 fois : 2026-09-05, deux fois le 2026-09-23). Les autres chaînes du
  binaire (`Failed to authenticate. ${…}: ${…}`,
  `Failed to authenticate through the broker: ${…}`, `Failed to
  authenticate` seul) n'ont jamais été observées dans ce contexte :
  volontairement exclues, jamais un `is_error` générique ni un motif
  deviné. Tout autre process qui a tourné (timeout, JSON illisible, tout
  autre `is_error`) garde le traitement uniforme.
- **CommandPolicy** : règle `engine_init_hooks` (regex `\binit-hooks\b`,
  hook uniquement), même modèle et même faux positif accepté que
  `engine_project_protect`. En headless, elle s'applique nécessairement :
  le fichier HERBERT est la seule source chargée.

### Limites connues (documentées, pas corrigées dans ce lot — même statut que le `sleep` de 30 s+)

- **NotebookEdit hors PathPolicy** : le matcher des hooks est
  `Bash|Write|Edit` ; un outil d'écriture non couvert (NotebookEdit)
  n'est pas soumis à PathPolicy.
- **Écriture arbitraire via Bash** : `python -c "open(...).write(...)"`,
  ou tout interpréteur lancé en Bash, écrit où il veut — le détecteur
  settings.json et PathPolicy ne voient que des cibles textuelles
  (redirections, cp/mv/rm…). Le contrôle SHA-256 ci-dessus couvre le
  fichier settings HERBERT, pas le reste du disque.
- **Détecteur settings.json (sessions interactives)** : ne voit pas la
  SOURCE d'un `mv .claude/settings.json ailleurs`, ne protège pas
  `settings.local.json`, et résout les chemins relatifs par rapport au
  `cwd` de la session sans tenir compte d'un `cd` dans la commande
  (constaté le 2026-09-23 : faux positif dans un sens, cible réelle
  différente dans l'autre). Sans effet en headless (sources project/local
  non chargées), pertinent pour les sessions interactives ouvertes dans
  un projet cible.
- **Source `user` et `--setting-sources ""`** : exclusion de
  `~/.claude/settings.json` déduite, non vérifiée directement ; le
  `--help` du CLI 2.1.235 ne documente pas le cas de la liste vide.
- **Ordre des arguments** : seul l'ordre utilisé (prompt juste après
  `-p`, options ensuite) a été testé en réel.

## Incident — Suppression de `master` le 22/08/2026 — enquête close (2026-09-24)

**Verdict : consolidation manuelle des deux branches, aucune perte de
travail, aucune action corrective nécessaire. Incident clos.**

### Faits (journal d'activité GitHub, `gh api repos/LP-D/HERBERT/activity`)

- 2026-08-22 14:20:19 UTC : le compte GitHub `LP-D` (type `User`, ni app
  ni bot) force-pushe `main` avec le contenu de `master`
  (`47ebe4f` -> `08bff24`).
- 2026-08-22 14:20:22 UTC, trois secondes plus tard : le même compte
  supprime `master` (`08bff24` -> `0000000`).
- Action confirmée par Léon-Paul comme une consolidation manuelle des deux
  branches, faite par lui-même.

### Vérifications menées (lecture seule, `gh api` : activity, events, commits, pulls, hooks)

- Aucune trace de webhook, de GitHub Action (0 workflow, 0 exécution), de
  PR (0 au total, `delete_branch_on_merge: false`), de déploiement ou de
  clé de déploiement. Les installations d'applications GitHub ne sont pas
  lisibles avec un jeton utilisateur (HTTP 401/403), mais les deux actions
  sont attribuées à un compte de type `User`.
- Le seul commit devenu orphelin, `47ebe4f`, est le README auto-généré par
  GitHub à la création du dépôt (2026-08-20 12:27:33 UTC, committer
  `GitHub <noreply@github.com>`, aucun parent, un fichier d'une ligne :
  `# HERBERT`). Il ne partage aucun ancêtre avec l'historique actuel
  (`git merge-base --is-ancestor 47ebe4f 08bff24` -> 1), mais ne contenait
  aucun travail : `README.md` a été recréé indépendamment par `aced6ab`
  (2026-08-21). Aucune perte réelle.
- Le dépôt local `herbert/` n'est pas l'origine de ces actions : son
  reflog ne montre aucun push vers `main` avant le 2026-09-24, et aucun
  événement de hook HERBERT n'est journalisé après 13:53:20 UTC le 22/08.

### Lien avec l'écart `master` / `origin/main` constaté le 2026-09-23

L'écart constaté ce jour-là (`master` local = `origin/master` = `08bff24`,
14 commits de retard sur `origin/main`, aucun commit propre) en est la
conséquence directe : la branche distante `master` n'existait plus depuis
le 22/08, mais la référence locale `origin/master` restait en cache
(`git fetch` sans `--prune` ne retire pas une branche supprimée côté
distant). Découvert le 2026-09-24 : le push de `master` a répondu
`* [new branch]`.

### Résolution

- `master` recréé le 2026-09-24 (08:02:32 UTC) par un fast-forward strict
  depuis `origin/main` (`ca7269c`) ; `main` et `master` identiques et
  synchronisés (vérifié par `git ls-remote`).
- Aucune action corrective nécessaire côté sécurité du dépôt ou du compte
  GitHub.
