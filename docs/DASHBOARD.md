# Dashboard visuel local (V0.4)

HTML statique généré localement depuis le SQLite existant. Aucun serveur,
aucun port ouvert : `dashboard/` est un dossier de fichiers `.html`/`.css`/`.js`
qu'on ouvre directement dans un navigateur (`file://`). Lecture seule —
aucune nouvelle table, aucune migration ; tout vient des tables déjà
utilisées par `engine report`, `engine task ...`, etc.

## Lancer le dashboard

```bash
python engine.py dashboard build   # régénère TOUT dashboard/ depuis SQLite (écrase, n'accumule pas)
python engine.py dashboard open    # ouvre dashboard/index.html dans le navigateur par défaut (webbrowser, aucun serveur)
```

`dashboard build` reste disponible comme commande manuelle (après restauration
d'un snapshot, pour forcer un rafraîchissement) — en plus de la régénération
automatique décrite plus bas.

## Ce qu'il montre

- **`index.html`** — tous les projets enregistrés, avec un badge par état de
  tâche et son compteur.
- **`project/<project_id>.html`** — tâches d'un projet, triables (clic sur
  une colonne) et filtrables (champ texte) côté client, en JS vanilla.
- **`task/<task_id>.html`** — étend le rapport existant de `engine report`
  (même fonction `build_report_html`, réutilisée telle quelle, jamais
  dupliquée) en y ajoutant un diagramme SVG généré en Python (pas de
  librairie de charting) : structure complète de la state machine en gris,
  chemin réellement suivi par cette tâche en bleu, tentatives de transition
  refusées en rouge pointillé.
- **`decisions.html`** — historique des décisions humaines (voir limite
  connue ci-dessous).
- **`audit.html`** — journal d'audit, toutes tâches/projets confondus,
  filtrable côté client ; jusqu'à 1000 entrées embarquées dans la page,
  affichées par lots de 200 via un bouton « Charger plus » qui ne fait
  **aucune** requête réseau (tout est déjà dans le HTML, juste
  masqué/révélé en JS — cohérent avec la contrainte "aucun serveur").

## Régénération automatique — assouplissement documenté

`README.md` établit que `engine sync push` est **la seule** commande à avoir
un effet de bord non explicitement demandé (elle-même toujours volontaire).
Le dashboard fait une exception délibérée à ce principe : après toute
commande qui modifie l'état d'une tâche (`task create`, `task test`,
`task branch`, `task promote`, `task rollback`, `task status --to ...`),
`dashboard/` est régénéré automatiquement et silencieusement.

**Pourquoi c'est différent de `sync push`** : régénérer un fichier HTML
local n'a aucun effet externe et n'est pas irréversible — contrairement à un
push vers un dépôt distant, qui rend du travail visible ailleurs et ne peut
pas être "annulé" proprement. Le risque que `sync push` protège contre
n'existe pas ici.

**Contrainte stricte, testée explicitement** (voir
`tests/unit/test_dashboard_auto_regen.py::test_task_test_succeeds_even_if_dashboard_regen_raises`) :
si la régénération échoue pour n'importe quelle raison (bug du générateur,
SQLite verrouillé, disque plein), la commande CLI qui l'a déclenchée
**termine normalement avec son propre résultat** — jamais bloquée, jamais
mise en échec par un problème du dashboard. L'échec est journalisé dans
`logs/*.jsonl` (pas une nouvelle destination de log) sous le composant
`cli.dashboard_auto_regen`, statut `UNAVAILABLE`.

## Décision Phase 0c : GSAP vendorisé, ou CSS pur ?

**Choix retenu : CSS pur.** Aucun fichier GSAP n'est vendorisé.

La licence a été vérifiée en direct sur gsap.com le jour de l'implémentation
(pas supposée) : GSAP est passé "Standard No Charge License" (rachat par
Webflow), qui autorise explicitement "use, reproduce, display, and implement
GSAP Products" pour tout usage sauf construire un concurrent de Webflow —
la redistribution du fichier minifié dans ce dépôt aurait donc été permise.

Le choix de ne pas l'utiliser vient d'ailleurs :
- Le skill `design-taste-frontend` (installé en Phase 0 pour guider les
  micro-interactions) déclare lui-même, dans son propre `SKILL.md` :
  *"Not dashboards, not data tables, not multi-step product UI"* — il cible
  les landing pages/portfolios, pas ce produit.
- Ce même skill précise, section 5.C : *"Save GSAP for actual pin/scrub
  work"* — les seules interactions nécessaires ici (survol, ouverture de
  section) n'en font pas partie ; il recommande explicitement l'alternative
  plus légère (transitions natives) pour ce cas.
- Cohérent avec la philosophie zéro-dépendance déjà en place dans HERBERT
  (aucune dépendance npm/JS ailleurs dans le projet, stdlib Python
  uniquement côté serveur).

Les micro-interactions du dashboard (survol des cartes projet, feedback
tactile `:active` sur les boutons, transitions de `<details>`) sont donc en
`transition`/`animation` CSS natives dans `dashboard/assets/style.css`,
générées par `dashboard_builder.py`. `dashboard/assets/vendor/` existe (créé
vide à chaque build) pour un usage futur si un vendoring devient un jour
nécessaire, mais n'est utilisé par rien actuellement.

## Outillage de conception (Phase 0a/0b) — dev-time uniquement

`ui-ux-pro-max-cli` (npm, `nextlevelbuilder/ui-ux-pro-max-skill`) et
`design-taste-frontend` (`github.com/Leonxlnx/taste-skill`, via
`npx skills add`) ont été installés pour guider la palette/typo/motion
pendant l'écriture du code. Vérifié après coup (`git status --porcelain`
avant/après chaque install) : seuls `.claude/skills/`, `.agents/`,
`skills-lock.json` ont été créés — **rien** dans `dashboard/` ni dans aucun
fichier livré. Ce sont des outils de conception, jamais une dépendance du
produit final : le dashboard généré ne les référence nulle part et fonctionne
identiquement si ces dossiers sont supprimés.

Palette retenue (voir `_STATE_COLORS` dans `dashboard_builder.py`) : 9
teintes distinctes par état de tâche, contraste vérifié par calcul direct
(luminance relative WCAG) plutôt que supposé — toutes ≥ 7.9:1 en mode clair
(minimum requis : 4.5:1). Aucun violet ("AI purple" — cliché explicitement
déconseillé par `design-taste-frontend`).

## Bugs trouvés et corrigés en Phase 6 (validation sur données réelles)

Aucun des trois bugs suivants n'était détectable par les tests synthétiques
(Phase 2/5) ni par les tests unitaires Python en général — chacun n'existe
qu'au contact de vraies données ou d'un vrai navigateur :

1. **`audit.html` à 706 Ko pour 51 entrées** (voir plus bas) — `details`
   non tronqué, jusqu'à 41 Ko de texte brut par entrée.
2. **`UnicodeEncodeError` fatal à l'écriture** — un caractère de
   substitution isolé dans des données réellement capturées par le hook
   PostToolUse faisait planter `write_text(..., encoding="utf-8")`.
3. **Filtre et tri de `project/<id>.html` totalement inertes dans un vrai
   navigateur**, trouvé lors de la validation Phase 6 du 2026-08-23 sur le
   projet jetable `dashboard-demo`. Cause : dans `_page()`, le
   `<script src="assets/app.js">` (qui DÉFINIT `hbInitFilter` /
   `hbInitSortableTable` / `hbInitLoadMore`) était placé en fin de `<body>`,
   **après** le `<script>` inline de chaque page qui les APPELLE — ce
   dernier s'exécutait donc avant que ces fonctions n'existent
   (`ReferenceError` silencieuse, aucun listener jamais attaché). Invisible
   aux tests Python (`assert "hbInitFilter" in content` ne vérifie que la
   présence textuelle, jamais l'ordre d'exécution) ; invisible aussi à un
   `get_page_text()` qui lit le DOM sans déclencher d'interaction. Révélé
   uniquement en exécutant réellement le JS dans un navigateur
   (`javascript_tool`) et en observant l'état du DOM avant/après un
   événement `input` simulé. **Corrigé** en déplaçant
   `<script src="assets/app.js">` dans `<head>` (chargement synchrone,
   donc les fonctions existent avant que le body ne soit parsé) — revérifié
   après correctif : le filtre cache/réaffiche réellement les lignes, le
   tri applique réellement la classe `sorted` au clic, `hb-svg-wrap`/
   `hbInitLoadMore` inchangés (122/122 tests toujours au vert après le
   correctif).

## Limites connues

- **Lecture seule, régénération manuelle ou déclenchée par une commande
  task.** Le dashboard ne se met jamais à jour tout seul en continu (pas de
  serveur, pas de watch) — si les données SQLite changent par un autre biais
  (édition manuelle de la DB, restauration d'un snapshot), relancer
  `engine dashboard build`.
- **« Décisions humaines » : garanti par le schéma depuis V0.5, plus une
  heuristique.** `state_transitions.is_human_decision`
  (migrations/0007_is_human_decision.sql) est peuplée au moment de chaque
  transition par l'appelant réel de `transition_task()` — `True` seulement
  pour `engine task status --to X` (le seul site d'appel humain de tout le
  code), `False` pour toute transition automatique
  (`advance_after_test_result`, `engine task promote`). `decisions.html`
  filtre désormais sur cette colonne, pas sur la présence de `--reason` :
  **une décision humaine prise sans `--reason` apparaît maintenant
  correctement sur cette page**, ce qui n'était pas le cas avec
  l'heuristique V0.4.
  Limite résiduelle, honnête : les transitions créées **avant** cette
  migration ont `is_human_decision=0` par convention rétroactive (voir le
  commentaire de la migration) — le passé n'a pas été ré-analysé transition
  par transition, ce serait réintroduire une heuristique déguisée en
  garantie de schéma.
- **`audit.html` tronque les `details` volumineux.** Constaté sur données
  réelles (session de développement) : certaines entrées `audit_log`
  contiennent jusqu'à 41 Ko de texte brut (sortie d'outil capturée par le
  hook PostToolUse), ce qui produisait un `audit.html` de 706 Ko pour
  seulement 51 entrées. Chaque valeur `details` est maintenant tronquée à
  500 caractères avec un indicateur explicite du nombre de caractères
  retirés (jamais caché silencieusement) — le détail complet reste consultable
  dans `logs/*.jsonl` ou directement dans la table `audit_log`.
- **Pas de pagination réseau sur `audit.html`.** Le bouton « Charger plus »
  ne fait aucune requête : jusqu'à 1000 entrées sont embarquées dans le HTML
  à la génération, affichées par lots de 200. Au-delà de 1000, régénérer ne
  rafraîchit que les 1000 plus récentes — pas de scroll infini illimité,
  cohérent avec la contrainte "aucun serveur".
- **`dashboard/` est un artefact généré, pas versionné.** Comme
  `reports/`, il est régénéré à la demande depuis SQLite — voir
  `.gitignore` si ce n'est pas déjà le cas au moment de la lecture.
- **Aucune commande `project remove` / `task delete` dans le CLI.** Un
  projet ou une tâche enregistré (y compris à des fins de test/démo, comme
  `dashboard-demo` créé pendant la validation Phase 6) ne peut pas être
  retiré proprement — seule une intervention SQL directe sur `data/herbert.db`
  le permettrait, non exposée par `engine`. Lacune réelle du CLI, pas
  spécifique au dashboard, hors périmètre de cette fonctionnalité.
