"""Classification objective AUTO / MANUAL_REQUIRED d'un `engine sync push`
(voir cmd_sync_push dans app/cli/main.py) — le critère est un fait de code
vérifiable, pas laissé à l'appréciation d'un agent au moment du push.

Principe directeur : en cas de doute sur UN SEUL critère, la classification
est TOUJOURS MANUAL_REQUIRED. Aucun critère ne bascule vers AUTO par défaut
— chacun doit explicitement et positivement être satisfait.

Fonction pure : aucun accès disque/réseau/git ici. Toutes les données
(fichiers modifiés, lignes totales du diff, résultat réel de la suite de
tests) sont calculées par l'appelant.
"""
from dataclasses import dataclass, field

# Seuil STRICT : un diff qui atteint exactement cette valeur reste
# MANUAL_REQUIRED (voir test du cas limite) — "sous le seuil" veut dire
# strictement inférieur, pas inférieur ou égal.
MAX_AUTO_DIFF_LINES = 20

# Chemins (préfixes, relatifs à la racine du dépôt, style posix) dont toute
# modification exige une vérification manuelle — sécurité, schéma, config,
# et la logique de classification elle-même (auto-référentiel : un
# changement qui assouplirait ces règles ne doit jamais s'auto-approuver).
BLOCKED_PATH_PREFIXES: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/hooks/",
    "app/policy/",
    "migrations/",
    "config/system.yaml",
    "scripts/verify_security.py",
    "app/push_classifier.py",
    "app/push_confirmation.py",
)


@dataclass
class PushClassification:
    decision: str  # "AUTO" ou "MANUAL_REQUIRED"
    failed_criteria: list[str] = field(default_factory=list)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def _is_blocked_path(path: str) -> str | None:
    """Retourne le préfixe bloqué qui matche, ou None."""
    normalized = _normalize(path)
    for prefix in BLOCKED_PATH_PREFIXES:
        stripped = prefix.rstrip("/")
        if normalized == stripped or normalized.startswith(prefix):
            return prefix
    return None


def classify_push(files_changed: list[str], total_diff_lines: int, tests_passed: bool) -> PushClassification:
    failed: list[str] = []

    if tests_passed is not True:
        failed.append("tests: la suite complète n'est pas confirmée VERIFIED_PASS (résultat réel requis, jamais supposé)")

    if len(files_changed) != 1:
        failed.append(f"fichiers: {len(files_changed)} fichier(s) modifié(s) — exactement 1 requis pour AUTO")

    for f in files_changed:
        blocked_prefix = _is_blocked_path(f)
        if blocked_prefix is not None:
            failed.append(f"chemin sensible: {f} (liste noire: {blocked_prefix})")

    if total_diff_lines >= MAX_AUTO_DIFF_LINES:
        failed.append(
            f"taille: {total_diff_lines} ligne(s) modifiée(s) — seuil AUTO = {MAX_AUTO_DIFF_LINES} (strictement inférieur requis)"
        )

    if failed:
        return PushClassification(decision="MANUAL_REQUIRED", failed_criteria=failed)
    return PushClassification(decision="AUTO", failed_criteria=[])
