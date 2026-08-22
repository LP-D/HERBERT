# HERBERT V0.1 — LOCAL CORE

Moteur local qui structure et sécurise l'usage de Claude Code sur des
projets personnels. Pas d'agent, pas de Docker, aucun appel API
Anthropic — Claude Code est un *consommateur* de HERBERT (via hooks),
jamais une dépendance de HERBERT. Voir la charte du projet pour le
détail des contraintes (NO FAKE, ZERO_EXTRA_COST).

## Installation / initialisation

```bash
python engine.py init
```

Crée la structure manquante, écrit `config/system.yaml` s'il n'existe
pas, applique les migrations SQLite (`data/herbert.db`).

## Commandes CLI

```bash
python engine.py doctor                 # vérifie l'environnement réel
python engine.py doctor --security      # + vérification des hooks de sécurité
python engine.py project add --name X --path Y
python engine.py project list
python engine.py task create --project X --description "..."
python engine.py task status --id X [--to ETAT] [--reason "..."]
python engine.py sync push [--force-unsafe]
```

## Le push reste toujours une action volontaire

**Principe du projet, pas une limitation technique** : `engine sync push`
est la seule commande qui pousse vers `origin`, et c'est la seule
fonction du code qui appelle `push_to_origin()` — aucune autre commande
(`task create`, `task status`, `project add`, un commit, une transition
d'état...) ne déclenche jamais un push, ni directement ni en sous-effet.
Ce choix est délibéré : un outil qui pousse automatiquement au moindre
changement d'état retire à l'utilisateur le contrôle du moment où son
travail devient visible ailleurs — HERBERT ne fait jamais ça.

Avant de pousser, `engine sync push` exécute
`scripts/verify_security.py` (vérification des hooks de sécurité). Si
elle échoue, le push est refusé avec le détail de l'échec. Il est
possible d'outrepasser avec `--force-unsafe`, mais cela déclenche une
demande de confirmation tapée explicitement (`OUI`, en toutes lettres)
avant tout push — jamais silencieux.

Aucun push automatique n'existe ailleurs dans le code : voir
`app/git_wrapper.py` (le wrapper Git ne fait que créer des branches,
committer et faire un rollback local) et `app/cli/main.py`
(`push_to_origin` n'est importée et appelée que dans `cmd_sync_push`).

## Vérification de sécurité des hooks

```bash
python scripts/verify_security.py       # run direct, rejouable en une commande
pytest tests/unit/test_security_consolidated.py
```

Teste que le hook `PreToolUse` bloque bien ses patterns dangereux
additionnels (diskpart, formatage disque, écriture sur
`.claude/settings.json`, création de compte administrateur), que la
lecture de `settings.json` reste autorisée (non-régression), et que
chaque décision est journalisée à la fois dans SQLite (`commands`) et
dans `logs/*.jsonl`.

Ce script ne teste **pas** les patterns déjà couverts par
`permissions.deny` dans `.claude/settings.json` (`rm -rf`,
`git push --force`, etc.) — ceux-là sont délégués au moteur de
permissions natif de Claude Code, qui ne peut être vérifié que dans une
vraie session. Voir [`docs/SMOKE_TEST_CLAUDE_CODE.md`](docs/SMOKE_TEST_CLAUDE_CODE.md)
pour ce protocole manuel, à refaire après toute modification de
`.claude/settings.json` ou des hooks eux-mêmes (pas à chaque commit).

## Tests

```bash
pytest tests/
```

## Déploiement sur un projet cible

HERBERT protège d'autres projets que lui-même : un projet cible (par
exemple `C:\Users\<user>\mon-autre-projet`) est enregistré dans HERBERT
et surveillé par ses hooks, sans jamais contenir de copie du code
HERBERT.

- **`.claude/settings.json` séparé par projet.** Le projet cible a son
  propre `.claude/settings.json`, indépendant de celui de `herbert/`
  (ce fichier-ci, à la racine de ce dépôt).
- **Hooks en chemin ABSOLU vers les scripts HERBERT.** Contrairement au
  `.claude/settings.json` de HERBERT lui-même (qui utilise
  `$CLAUDE_PROJECT_DIR/.claude/hooks/...`, une variable résolue par
  Claude Code vers la racine de la session en cours), le
  `settings.json` d'un projet cible **ne peut pas** utiliser
  `$CLAUDE_PROJECT_DIR` pour ses hooks : dans une session ouverte sur
  le projet cible, cette variable pointe vers la racine du projet
  cible, qui n'a pas de `.claude/hooks/pre_tool_use.py` à lui — le
  code des hooks n'existe que dans `herbert/`. Le `command` de chaque
  hook doit donc pointer en chemin absolu vers les scripts de CE dépôt
  HERBERT (ex. `C:\Users\<user>\herbert\.claude\hooks\pre_tool_use.py`).
  Les scripts eux-mêmes résolvent leur propre racine via
  `Path(__file__).resolve().parents[2]` — donc la config, la base
  SQLite (`data/herbert.db`) et les logs restent ceux de HERBERT,
  partagés entre tous les projets cibles surveillés.
- **Une session Claude Code = un projet.** N'ouvre jamais une session
  à la racine de `herbert/` pour éditer un projet cible enregistré.
  C'est le seul scénario où PathPolicy peut se tromper de racine —
  voir la limite connue ci-dessous.
- **Enregistrement préalable obligatoire.** Le projet cible doit être
  ajouté avant tout usage via :
  ```bash
  python C:\Users\<user>\herbert\engine.py project add --name mon-projet --path C:\Users\<user>\mon-autre-projet
  ```
  Cette commande peut être lancée depuis n'importe quel répertoire
  (elle résout toujours la racine HERBERT via `__file__`, jamais le
  répertoire courant) ; c'est `--path` qui détermine sur quel dépôt
  git les commandes `task branch` / `task test` / `task promote`
  agiront ensuite pour ce projet.

Exemple minimal de `settings.json` pour un projet cible (chemins
génériques à adapter — voir [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)
pour le détail complet et une note de transparence sur cet exemple) :

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

Après création ou modification de ce fichier : redémarrer la session
Claude Code du projet cible (pas seulement relancer une commande dans
la session existante) et rejouer le protocole de
[`docs/SMOKE_TEST_CLAUDE_CODE.md`](docs/SMOKE_TEST_CLAUDE_CODE.md) —
ce document a été écrit pour le `settings.json` de HERBERT lui-même,
mais le même risque (session déjà ouverte avant l'écriture du fichier,
qui ne recharge pas la config) s'applique de la même façon à un
`settings.json` de projet cible nouvellement créé.

## Limites connues (V0.1)

- Le filtrage des commandes dangereuses est basé sur des patterns
  textuels, pas sur une analyse sémantique — une commande équivalente
  formulée différemment peut contourner le filtre.
- Pas de Docker, pas de sandbox, pas de scoring de risque — prévu pour
  des projets personnels de confiance, pas du code tiers non fiable.
- **Aucun garde-fou de cohérence entre le `cwd` de la session et le
  projet réellement visé.** PathPolicy résout la racine autorisée à
  partir du `cwd` envoyé par Claude Code dans l'événement du hook
  (voir `app/policy/path_policy.py` et `.claude/hooks/pre_tool_use.py`),
  pas à partir d'une correspondance explicite avec un projet enregistré
  dans la table `projects`. Si une session Claude Code est ouverte par
  erreur à la racine de `herbert/` alors que l'utilisateur pense éditer
  un autre projet enregistré, PathPolicy applique la racine de
  `herbert/` — pas celle du projet visé — sans avertissement. Ce n'est
  pas un bug à corriger : c'est une contrainte d'usage du modèle "une
  session Claude Code = un projet" (voir "Déploiement sur un projet
  cible" ci-dessus), à respecter, pas optionnelle.
