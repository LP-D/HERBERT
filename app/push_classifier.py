"""Classification objective AUTO / MANUAL_REQUIRED d'un diff à pousser —
le critère est un fait de code vérifiable, pas laissé à l'appréciation
d'un agent au moment du push. Deux appelants : `cmd_sync_push`
(app/cli/main.py, dépôt HERBERT lui-même, HERBERT_BLOCKED_PATTERNS) et
`run_headless_task` (app/headless_orchestrator.py, projet cible, liste
résolue par resolve_blocked_patterns(project)).

Principe directeur : en cas de doute sur UN SEUL critère, la classification
est TOUJOURS MANUAL_REQUIRED. Aucun critère ne bascule vers AUTO par défaut
— chacun doit explicitement et positivement être satisfait. Une liste de
chemins protégés absente ou invalide est un doute, jamais "rien à protéger".

Fonction pure : aucun accès disque/réseau/git ici. Toutes les données
(fichiers modifiés, lignes totales du diff, résultat réel de la suite de
tests, motifs protégés) sont calculées par l'appelant.

Syntaxe des motifs (chaînes, style posix, comparaison insensible à la
casse — Windows/core.ignorecase : `.GitHub/` ne doit pas contourner
`.github/`) :
- `**/<glob>` : glob fnmatch sur le NOM DE FICHIER, à n'importe quelle
  profondeur (`**/.env` bloque `.env` comme `config/.env`).
- tout autre motif : préfixe ancré à la racine du dépôt (comportement
  historique : `x/` bloque tout le dossier, `x` bloque `x` et tout ce qui
  commence par `x`, ex. `requirements` couvre `requirements-dev.txt`).
"""
from dataclasses import dataclass, field
from fnmatch import fnmatchcase

# Seuil STRICT : un diff qui atteint exactement cette valeur reste
# MANUAL_REQUIRED (voir test du cas limite) — "sous le seuil" veut dire
# strictement inférieur, pas inférieur ou égal.
MAX_AUTO_DIFF_LINES = 20

BASENAME_PATTERN_MARKER = "**/"

# Défauts valables pour TOUT dépôt (projet cible comme HERBERT). Asymétrie
# assumée (voir docs/DECISIONS.md) : un faux positif coûte une
# confirmation manuelle, un faux négatif laisse passer en AUTO une
# modification de secret, de CI, de la config de l'agent ou de ce qui
# décide si "les tests passent" — la liste penche donc volontairement vers
# le blocage.
DEFAULT_BLOCKED_PATTERNS: tuple[str, ...] = (
    # --- préfixes ancrés à la racine ---
    # Git (le contenu de .git/ n'apparaît jamais dans `git diff`, inutile ici)
    ".gitmodules",
    ".gitattributes",
    ".gitignore",
    ".githooks/",
    ".husky/",
    ".pre-commit-config.yaml",
    # CI
    ".github/",
    ".gitlab-ci.yml",
    ".gitlab/",
    ".circleci/",
    ".travis.yml",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".buildkite/",
    # Configuration de l'agent lui-même (permissions, hooks, instructions)
    ".claude/",
    "CLAUDE.md",
    ".mcp.json",
    # Exécution de code par l'éditeur
    ".vscode/tasks.json",
    ".vscode/launch.json",
    # Secrets
    "secrets/",
    ".secrets/",
    "credentials/",
    # Configuration de tests/build : décide de ce que "tests passés" veut
    # dire et de ce qui s'exécute à l'installation
    "pytest.ini",
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "tox.ini",
    "noxfile.py",
    "requirements",
    "Dockerfile",
    "docker-compose",
    # --- noms de fichier, à n'importe quelle profondeur ---
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
    "**/*.p12",
    "**/*.pfx",
    "**/*.jks",
    "**/*.keystore",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.npmrc",
    "**/.pypirc",
    "**/.netrc",
    "**/.htpasswd",
    "**/credentials*",
    "**/secrets.*",
    "**/conftest.py",
)

# Exceptions CODÉES EN DUR, limitées à un seul motif : jamais exprimables
# par un projet (la liste d'un projet ne peut qu'ajouter, voir
# resolve_blocked_patterns). Gabarits de configuration sans secret.
_BASENAME_EXCEPTIONS: dict[str, frozenset[str]] = {
    "**/.env.*": frozenset({".env.example", ".env.sample", ".env.template"}),
}

# Chemins propres au dépôt HERBERT : sécurité, schéma, config, et la
# logique de classification elle-même (auto-référentiel : un changement
# qui assouplirait ces règles ne doit jamais s'auto-approuver).
_HERBERT_SPECIFIC_PREFIXES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/hooks/",
    "app/policy/",
    "migrations/",
    "config/system.yaml",
    "scripts/verify_security.py",
    "app/push_classifier.py",
    "app/push_confirmation.py",
)

HERBERT_BLOCKED_PATTERNS: tuple[str, ...] = DEFAULT_BLOCKED_PATTERNS + _HERBERT_SPECIFIC_PREFIXES

UNRESOLVED_PATTERNS_CRITERION = "config : liste de chemins protégés non résolue"


@dataclass
class PushClassification:
    decision: str  # "AUTO" ou "MANUAL_REQUIRED"
    failed_criteria: list[str] = field(default_factory=list)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_valid_pattern_list(patterns) -> bool:
    """Liste/tuple NON VIDE de chaînes non vides. Une chaîne seule (itérable
    caractère par caractère) ou une liste vide ne sont jamais acceptées :
    "rien à protéger" n'est jamais une conclusion par défaut."""
    if not isinstance(patterns, (list, tuple)) or not patterns:
        return False
    return all(isinstance(p, str) and p.strip() for p in patterns)


def _matches(normalized_path: str, pattern: str) -> bool:
    path_cf = normalized_path.casefold()
    if pattern.startswith(BASENAME_PATTERN_MARKER):
        glob = pattern[len(BASENAME_PATTERN_MARKER):]
        basename = normalized_path.rsplit("/", 1)[-1].casefold()
        if basename in _BASENAME_EXCEPTIONS.get(pattern, frozenset()):
            return False
        return fnmatchcase(basename, glob.casefold())
    prefix = _normalize(pattern).casefold()
    return path_cf == prefix.rstrip("/") or path_cf.startswith(prefix)


def _is_blocked_path(path: str, blocked_patterns) -> str | None:
    """Retourne le motif bloqué qui matche, ou None."""
    normalized = _normalize(path)
    for pattern in blocked_patterns:
        if _matches(normalized, pattern):
            return pattern
    return None


def resolve_blocked_patterns(project) -> tuple[str, ...] | None:
    """Défauts + `extra_blocked_patterns` du projet (ajout seulement, doublons
    retirés, ordre préservé). Colonne NULL / liste vide = défauts seuls, pas
    un échec. Retourne None — donc MANUAL_REQUIRED côté classify_push —
    uniquement si le projet est absent ou si la valeur stockée est illisible
    (JSON invalide, pas une liste de chaînes : `extra_blocked_patterns` vaut
    alors None, voir repository._parse_extra_blocked_patterns)."""
    if project is None:
        return None
    extras = getattr(project, "extra_blocked_patterns", None)
    if not isinstance(extras, (list, tuple)):
        return None
    if not all(isinstance(p, str) and p.strip() for p in extras):
        return None
    merged: list[str] = list(DEFAULT_BLOCKED_PATTERNS)
    for p in extras:
        if p not in merged:
            merged.append(p)
    return tuple(merged)


def classify_push(
    files_changed: list[str], total_diff_lines: int, tests_passed: bool, *, blocked_patterns
) -> PushClassification:
    """`blocked_patterns` est obligatoire et sans défaut : un appelant qui
    l'oublie échoue (TypeError) plutôt que de retomber silencieusement sur
    une liste qui ne concerne pas son dépôt. None ou une valeur invalide =
    MANUAL_REQUIRED, quels que soient les autres critères."""
    failed: list[str] = []

    patterns_ok = _is_valid_pattern_list(blocked_patterns)
    if not patterns_ok:
        failed.append(UNRESOLVED_PATTERNS_CRITERION)

    if tests_passed is not True:
        failed.append("tests: la suite complète n'est pas confirmée VERIFIED_PASS (résultat réel requis, jamais supposé)")

    if len(files_changed) != 1:
        failed.append(f"fichiers: {len(files_changed)} fichier(s) modifié(s) — exactement 1 requis pour AUTO")

    if patterns_ok:
        for f in files_changed:
            blocked = _is_blocked_path(f, blocked_patterns)
            if blocked is not None:
                failed.append(f"chemin sensible: {f} (liste noire: {blocked})")

    if total_diff_lines >= MAX_AUTO_DIFF_LINES:
        failed.append(
            f"taille: {total_diff_lines} ligne(s) modifiée(s) — seuil AUTO = {MAX_AUTO_DIFF_LINES} (strictement inférieur requis)"
        )

    if failed:
        return PushClassification(decision="MANUAL_REQUIRED", failed_criteria=failed)
    return PushClassification(decision="AUTO", failed_criteria=[])
