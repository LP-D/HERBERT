# Smoke test manuel — hook réellement invoqué par Claude Code

**Checklist manuelle, PAS automatisée.** `scripts/verify_security.py` teste
le validateur (`pre_tool_use.py`/`post_tool_use.py`) en lui envoyant du
JSON directement — cela prouve que la logique de décision est correcte,
**pas** que Claude Code invoque réellement le hook au bon moment et
respecte son code de sortie. Seule une vraie session Claude Code peut
prouver ça. C'est le protocole suivi (et ayant révélé plusieurs bugs
réels) lors de la mise au point de HERBERT V0.1.

## Quand refaire ce test

**Obligatoire après :**
- toute modification de `.claude/settings.json` (permissions, hooks)
- toute modification de `.claude/hooks/pre_tool_use.py` ou `post_tool_use.py`

**PAS nécessaire :**
- à chaque commit normal qui ne touche pas ces fichiers
- pour des changements dans `app/`, `tests/`, la documentation, etc.

## Pourquoi le redémarrage de session est obligatoire

Constaté empiriquement : une session Claude Code déjà ouverte **avant**
la création/modification de `.claude/settings.json` peut ne jamais
recharger cette configuration. Dans cette situation, une commande de la
deny-list (`rm -rf`) s'est exécutée sans aucun blocage, à deux reprises,
alors que le fichier de règles existait pourtant déjà sur disque. Le
rechargement a fini par se produire plus tard dans la même session, sans
cause identifiée avec certitude (redémarrage non contrôlé du côté
harness) — donc la seule méthode fiable est de **redémarrer la session**
après toute modification de ces fichiers, et de ne pas supposer qu'une
session en cours a bien rechargé la config.

## Protocole (6 étapes)

### 1. Redémarrer (ou ouvrir une nouvelle) session Claude Code

Fermer la session en cours si elle existe, puis ouvrir Claude Code dans
le répertoire du projet (`C:\Users\Dufour\herbert`). Ne pas réutiliser une
session déjà active avant la modification de `.claude/settings.json` ou
des hooks.

### 2. Créer un dossier jetable avec un fichier canari

```bash
mkdir dossier_test_hook && echo canari > dossier_test_hook/canari.txt
```

Cela permet un test sans aucun risque réel : si le blocage échoue, on
perd juste ce dossier de test.

### 3. Tenter une commande de la deny-list

```bash
rm -rf dossier_test_hook
```

Observer la réponse de Claude Code :
- **Bloqué (attendu)** : un message explicite apparaît, du type
  `Permission to use Bash with command "rm -rf ..." has been denied.`
  (refus par `permissions.deny` dans `settings.json`), ou un message
  `[herbert] commande bloquée: ...` sur stderr (refus par le hook Python
  lui-même, exit code 2).
- **Pas bloqué (échec réel)** : la commande s'exécute normalement. Dans ce
  cas, ARRÊTER — ne pas continuer à utiliser ce hook en pensant qu'il
  protège quoi que ce soit tant que la cause n'est pas trouvée et
  corrigée.

### 4. Vérifier que rien n'a été supprimé

```bash
ls dossier_test_hook
```

Le dossier et `canari.txt` doivent toujours exister si l'étape 3 a
réellement bloqué.

### 5. Vérifier les traces, avec horodatage

Avant de conclure, comparer avec un horodatage pris juste avant l'étape 3
(`date -u +"%Y-%m-%dT%H:%M:%S.%NZ"`), pour ne pas confondre avec une trace
antérieure (d'un test pytest ou d'une session précédente) :

```bash
python -c "
from app.database.connection import get_connection
conn = get_connection('data/herbert.db')
for row in conn.execute('SELECT command, decision, created_at FROM commands ORDER BY created_at DESC LIMIT 3'):
    print(dict(row))
"
grep "dossier_test_hook" logs/*.jsonl
```

La commande bloquée doit apparaître avec un horodatage postérieur à celui
pris juste avant l'étape 3, dans **au moins une des deux sources**
(la table `commands` si c'est le hook Python qui a bloqué — un blocage
par `permissions.deny` en amont n'invoque pas forcément le hook, ce qui
est normal et attendu, pas un échec).

### 6. Nettoyer le dossier de test

Si le blocage a fonctionné, `rm -rf` ne fonctionnera pas non plus pour le
nettoyage — c'est normal, c'est la preuve que ça marche. Utiliser une
méthode alternative :

```bash
python -c "import shutil; shutil.rmtree('dossier_test_hook')"
```

**Note de transparence** : ceci démontre aussi une limite connue de
HERBERT V0.1 — le filtrage est basé sur des patterns textuels
(`rm -rf`, `Remove-Item -Recurse -Force`, ...), pas sur une analyse
sémantique de ce que fait réellement la commande. Une commande
équivalente formulée différemment (comme ici) contourne le filtre. Ce
n'est pas corrigé en V0.1 — voir les limites connues du projet.

### 6bis (optionnel) — commande `ask`

Pour vérifier aussi la catégorie `permissions.ask` (ex: `pip install`,
`npm install`, `curl`) : tenter une telle commande et constater si une
confirmation humaine est réellement demandée. Comportement observé lors
de la mise au point : inconstant selon le contexte (parfois une vraie
demande de confirmation apparaît et peut être annulée, parfois la
commande s'exécute directement) — à re-vérifier à chaque fois plutôt que
supposé acquis, surtout si le mode de permission de la session a changé
(`bypassPermissions` vs autre).

## Ce que ce test ne remplace pas

`scripts/verify_security.py` / `tests/unit/test_security_consolidated.py`
restent nécessaires pour la logique de décision elle-même (patterns
bloqués, faux positifs, journalisation). Ce smoke test manuel ne teste
QUE l'intégration réelle avec Claude Code — les deux sont complémentaires,
ni l'un ni l'autre ne suffit seul.
