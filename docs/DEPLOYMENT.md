# Déploiement de HERBERT sur un projet cible

Ce document détaille le modèle de déploiement de HERBERT quand il
surveille un projet **autre que lui-même** (un "projet cible") :
un dépôt personnel quelconque sur lequel on veut que Claude Code passe
par les mêmes garde-fous (CommandPolicy, PathPolicy, journalisation)
que ceux utilisés pour développer HERBERT lui-même.

> **Sessions headless (`engine task run-headless`) — depuis le 2026-09-23 :**
> aucun `.claude/settings.json` n'est nécessaire (ni lu) dans le projet
> cible. Les hooks viennent d'un fichier généré par HERBERT sous
> `data/target_settings/<project_id>/settings.json` (commande manuelle :
> `engine init-hooks <chemin-projet>`, aussi exécutée automatiquement avant
> chaque boucle), chargé via `--settings <fichier> --setting-sources ""` —
> seule source chargée, donc aucun `disableAllHooks` du projet ne peut les
> neutraliser. Prérequis : `claude_headless.executable` dans
> `config/system.yaml` (chemin absolu de `claude.exe`). Voir
> docs/DECISIONS.md, « Isolation headless et init-hooks ». La procédure
> ci-dessous reste celle des sessions **interactives** ouvertes dans un
> projet cible.

## Principe général : un seul HERBERT, plusieurs projets surveillés

HERBERT n'est **pas** installé dans chaque projet cible. Il n'existe
qu'une seule copie du code HERBERT sur la machine (ce dépôt,
`herbert/`), et c'est elle qui est référencée depuis tous les projets
cibles.

Ce que possède un projet cible :
- son propre `.claude/settings.json` (permissions + hooks), séparé de
  celui de `herbert/`.
- une entrée dans la table `projects` de la base SQLite de HERBERT
  (`data/herbert.db`, dans `herbert/`), créée via
  `engine project add --name ... --path ...`.

Ce qu'il ne possède **pas** :
- une copie de `app/`, `.claude/hooks/*.py`, ou de tout autre code
  HERBERT.

## Pourquoi les hooks du projet cible utilisent un chemin absolu

Le `.claude/settings.json` de HERBERT lui-même (celui de ce dépôt)
référence ses hooks ainsi :

```json
"command": "python \"$CLAUDE_PROJECT_DIR/.claude/hooks/pre_tool_use.py\""
```

`$CLAUDE_PROJECT_DIR` est une variable résolue par Claude Code vers la
racine du projet de la session **en cours**. Pour une session ouverte
sur `herbert/` lui-même, cela pointe correctement vers
`herbert/.claude/hooks/pre_tool_use.py`.

Pour une session ouverte sur un **projet cible**, cette même variable
pointerait vers `<projet_cible>/.claude/hooks/pre_tool_use.py` — un
fichier qui n'existe pas, puisque le projet cible ne contient pas de
copie du code HERBERT. Le `settings.json` du projet cible doit donc
référencer les hooks par un **chemin absolu vers `herbert/`** :

```json
"command": "python \"C:\\Users\\<user>\\herbert\\.claude\\hooks\\pre_tool_use.py\""
```

Ceci est cohérent avec la façon dont les scripts eux-mêmes résolvent
leur racine — vérifié dans `.claude/hooks/pre_tool_use.py` et
`.claude/hooks/post_tool_use.py` :

```python
REPO_ROOT = Path(__file__).resolve().parents[2]
```

`REPO_ROOT` est dérivé de l'emplacement physique du fichier `.py`
lui-même, jamais du répertoire courant de la session. Concrètement,
quel que soit le projet depuis lequel le hook est invoqué, il charge
toujours `config/system.yaml`, `data/herbert.db` et `logs/` **de
`herbert/`** — un seul jeu d'état HERBERT (config, base SQLite, logs),
partagé entre tous les projets cibles surveillés.

## Comment PathPolicy détermine la racine autorisée

Toujours dans `.claude/hooks/pre_tool_use.py` :

```python
# `cwd` = racine du projet CONCERNÉ envoyée par Claude Code — pas
# REPO_ROOT, qui n'est que l'emplacement de ce script et peut être
# partagé entre plusieurs projets.
project_root = event.get("cwd") or str(REPO_ROOT)
```

La racine appliquée par `PathPolicy` (`app/policy/path_policy.py`)
n'est **pas** un champ `path` lu dans la table `projects`, mais le
`cwd` que Claude Code envoie dans l'événement du hook — c'est-à-dire
le répertoire racine de la session Claude Code en cours. Si la session
a été ouverte directement dans le projet cible, `cwd` correspond bien
à la racine de ce projet, et `PathPolicy` restreint correctement les
opérations de fichiers à cette racine.

C'est ce mécanisme qui rend la commande `engine project add`
nécessaire mais pas suffisante à elle seule : l'enregistrement dans la
table `projects` sert aux commandes CLI (`task branch`, `task test`,
`task promote`, ...) qui opèrent sur `project.path` pour les
opérations Git, mais la protection en temps réel par les hooks pendant
une session Claude Code dépend de `cwd`, donc de l'endroit où la
session a réellement été ouverte — pas d'une correspondance explicite
avec l'entrée enregistrée.

## Procédure de déploiement

1. **Enregistrer le projet cible dans HERBERT**, depuis n'importe quel
   répertoire (la commande résout toujours la racine HERBERT via
   `__file__`) :
   ```bash
   python C:\Users\<user>\herbert\engine.py project add --name mon-projet --path C:\Users\<user>\mon-autre-projet
   python C:\Users\<user>\herbert\engine.py project list
   ```

2. **Créer `.claude/settings.json` dans le projet cible** (pas dans
   `herbert/`), avec les hooks en chemin absolu vers `herbert/` — voir
   l'exemple ci-dessous.

3. **Ouvrir une nouvelle session Claude Code directement à la racine
   du projet cible.** Ne jamais réutiliser une session déjà ouverte
   avant la création de ce `settings.json` — un `settings.json` créé
   ou modifié après l'ouverture de la session peut ne jamais être
   rechargé (comportement déjà constaté et documenté pour le
   `settings.json` de HERBERT lui-même dans
   [`SMOKE_TEST_CLAUDE_CODE.md`](SMOKE_TEST_CLAUDE_CODE.md) ; rien
   dans le mécanisme de chargement de Claude Code ne distingue le
   `settings.json` de HERBERT de celui d'un projet cible, donc le même
   risque s'applique).

4. **Rejouer le protocole de
   [`SMOKE_TEST_CLAUDE_CODE.md`](SMOKE_TEST_CLAUDE_CODE.md)** dans
   cette session, pour vérifier que le blocage fonctionne réellement
   dans ce projet précis (dossier canari, tentative `rm -rf`,
   vérification des traces SQLite/JSONL).

## Exemple de `settings.json` pour un projet cible

**Note de transparence :** cet exemple n'est pas une copie du fichier
réellement utilisé lors d'une session de test antérieure — ce fichier
n'a pas pu être retrouvé (il vivrait sur le disque du projet cible
testé, hors de ce dépôt `herbert/`, et n'est référencé ni dans
l'historique Git, ni dans `data/`, ni dans `logs/*.jsonl`, qui sont de
toute façon exclus du dépôt par `.gitignore`). Il est reconstruit à
partir du mécanisme réellement vérifié dans le code ci-dessus
(résolution de `REPO_ROOT` par `__file__`, résolution de
`project_root` par `cwd`) et du bloc `permissions` du
`.claude/settings.json` de HERBERT (repris à l'identique : ces règles
sont indépendantes du projet). Remplacer `C:\Users\<user>\herbert` par
le chemin réel du dépôt HERBERT sur la machine.

```json
{
  "permissions": {
    "deny": [
      "Bash(rm -rf:*)",
      "Bash(Remove-Item -Recurse -Force:*)",
      "Bash(ri -Recurse -Force:*)",
      "Bash(git push --force:*)",
      "Bash(git push --force-with-lease:*)",
      "Bash(reg:*)",
      "Bash(reg.exe:*)",
      "Bash(sudo:*)",
      "Bash(runas:*)",
      "Read(./.env)",
      "Read(./.env.*)",
      "Read(./secrets/**)",
      "Edit(./.env)",
      "Edit(./.env.*)",
      "Edit(./secrets/**)",
      "Write(./.env)",
      "Write(./.env.*)",
      "Write(./secrets/**)"
    ],
    "ask": [
      "Bash(git push:*)",
      "Bash(pip install:*)",
      "Bash(npm install:*)",
      "Bash(curl:*)",
      "Bash(Invoke-WebRequest:*)"
    ]
  },
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          { "type": "command", "command": "python \"C:\\Users\\<user>\\herbert\\.claude\\hooks\\pre_tool_use.py\"" }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Bash|Write|Edit",
        "hooks": [
          { "type": "command", "command": "python \"C:\\Users\\<user>\\herbert\\.claude\\hooks\\post_tool_use.py\"" }
        ]
      }
    ]
  }
}
```

## Limite connue : "une session = un projet" n'est pas imposé par le code

**Ceci n'est pas un bug — c'est une contrainte d'usage à respecter,
pas optionnelle.**

Aucun garde-fou dans le code ne vérifie que le `cwd` de la session
Claude Code correspond effectivement au projet que l'utilisateur pense
éditer. `PathPolicy` fait confiance au `cwd` envoyé par Claude Code
(voir plus haut) — elle ne le compare à aucune entrée de la table
`projects`.

Scénario concret où cela se retourne contre l'utilisateur : une
session Claude Code est ouverte à la racine de `herbert/` (par
exemple parce que c'est le dernier dossier ouvert dans l'éditeur), et
l'utilisateur pense y éditer un autre projet déjà enregistré dans
HERBERT (`mon-autre-projet`). `cwd` vaut alors la racine de
`herbert/`, pas celle de `mon-autre-projet` :
- `PathPolicy` autorise les écritures dans `herbert/`, et **refuserait**
  toute tentative d'écriture réellement dirigée vers
  `mon-autre-projet` (chemin hors racine autorisée) — ou pire,
  autoriserait par erreur une écriture dans `herbert/` que
  l'utilisateur croyait faire dans l'autre projet, sans aucun message
  distinguant les deux cas.
- Rien dans les logs (`logs/*.jsonl`) ni dans la table `commands` ne
  signale explicitement ce décalage entre l'intention et la racine
  réellement appliquée — la décision journalisée est correcte *pour
  le `cwd` reçu*, pas pour l'intention de l'utilisateur.

La seule protection est organisationnelle : **ouvrir une session
Claude Code dédiée par projet**, jamais une session partagée entre
`herbert/` et un projet cible. Voir la section "Déploiement sur un
projet cible" du [`README.md`](../README.md).
