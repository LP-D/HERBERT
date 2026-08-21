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

## Limites connues (V0.1)

- Le filtrage des commandes dangereuses est basé sur des patterns
  textuels, pas sur une analyse sémantique — une commande équivalente
  formulée différemment peut contourner le filtre.
- Pas de Docker, pas de sandbox, pas de scoring de risque — prévu pour
  des projets personnels de confiance, pas du code tiers non fiable.
