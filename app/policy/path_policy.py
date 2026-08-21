"""PathPolicy : valide qu'un chemin de fichier reste dans la racine
autorisée d'un projet (celle enregistrée dans la table `projects`, pas
la racine du dépôt HERBERT lui-même — voir le câblage dans
.claude/hooks/pre_tool_use.py qui utilise `cwd` envoyé par Claude Code
pour déterminer quelle racine s'applique).

Toute violation détectée ici doit être traitée comme BLOCKED et
journalisée exactement comme les décisions de CommandPolicy — c'est
une vérification supplémentaire, pas un mécanisme séparé.
"""
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath

RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class PathViolation(str, Enum):
    TRAVERSAL = "TRAVERSAL"
    OUTSIDE_ROOT = "OUTSIDE_ROOT"
    RESERVED_NAME = "RESERVED_NAME"


@dataclass
class PathCheckResult:
    allowed: bool
    reason: str
    violation: PathViolation | None = None
    resolved_path: str | None = None
    # signalé mais ne bloque pas à lui seul (sauf si allowed=False pour
    # une autre raison) : un lien symbolique/jonction pointant hors de la
    # racine sera de toute façon rattrapé par OUTSIDE_ROOT ci-dessus.
    is_symlink_or_junction: bool = False


def _is_reserved_windows_name(path: Path) -> bool:
    name_no_ext = path.name.upper()
    stem = path.stem.upper()
    return name_no_ext in RESERVED_WINDOWS_NAMES or stem in RESERVED_WINDOWS_NAMES


def validate_path(candidate: str, project_root: str) -> PathCheckResult:
    """`candidate` peut être relatif ou absolu. `project_root` est la racine
    de projet enregistrée (table `projects`), pas nécessairement le dépôt
    HERBERT lui-même."""
    root = Path(project_root).resolve()

    # Détection du path traversal AVANT résolution : un segment '..' est
    # rejeté même si le chemin résolu final retombe "par hasard" dans la
    # racine autorisée (résoudre d'abord masquerait l'intention).
    raw_parts = PureWindowsPath(candidate).parts
    if ".." in raw_parts:
        return PathCheckResult(
            allowed=False,
            violation=PathViolation.TRAVERSAL,
            reason=f"segment '..' détecté dans le chemin: {candidate}",
        )

    candidate_path = Path(candidate)
    resolved = candidate_path.resolve() if candidate_path.is_absolute() else (root / candidate_path).resolve()

    if not resolved.is_relative_to(root):
        return PathCheckResult(
            allowed=False,
            violation=PathViolation.OUTSIDE_ROOT,
            reason=f"chemin hors de la racine autorisée ({root}): {resolved}",
            resolved_path=str(resolved),
        )

    if _is_reserved_windows_name(resolved):
        return PathCheckResult(
            allowed=False,
            violation=PathViolation.RESERVED_NAME,
            reason=f"nom de fichier réservé Windows: {resolved.name}",
            resolved_path=str(resolved),
        )

    is_link = resolved.exists() and resolved.is_symlink()

    return PathCheckResult(
        allowed=True,
        reason="chemin valide",
        resolved_path=str(resolved),
        is_symlink_or_junction=is_link,
    )
