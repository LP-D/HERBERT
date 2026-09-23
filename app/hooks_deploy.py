"""Fichier settings HERBERT par projet cible, chargé par les sessions
headless via `--settings <ce fichier> --setting-sources ""` (voir
app/claude_headless.py).

Emplacement : `<herbert>/data/target_settings/<project_id>/settings.json`,
JAMAIS dans le dépôt du projet cible — avec `--setting-sources ""`, ce
fichier n'a pas besoin d'être une source "project" : il reste hors du
répertoire de travail de l'agent (PathPolicy refuse Write/Edit hors de la
racine du projet) et ne peut entrer en collision avec aucun
.claude/settings.json personnel du projet. Un invariant le vérifie
(HooksDeployError si le fichier tomberait dans le projet, ex. un projet
enregistré sur le dépôt HERBERT lui-même).

Contenu COMPLET, rien d'implicite : `--setting-sources ""` ne charge
aucune autre source (ni project, ni local, ni user), donc permissions
(generate_settings_permissions, source unique de vérité) + hooks
PreToolUse/PostToolUse pointant en chemin absolu (séparateurs `/`, jamais
`\\`) vers les scripts de ce dépôt HERBERT, lancés par l'interpréteur
absolu `sys.executable` (jamais `python` nu, dépendant du PATH).

Métadonnées (SHA-256, statut, chemin) : uniquement dans logs/*.jsonl
(task_id=None), jamais dans le fichier généré lui-même.
"""
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.logging_utils import append_jsonl_event
from app.models import Project
from app.models.enums import LogStatus
from app.policy.command_policy import generate_settings_permissions

TARGET_SETTINGS_SUBDIR = Path("data") / "target_settings"
# Même matcher que .claude/settings.json de HERBERT (vérifié par
# tests/unit/test_init_hooks.py) : Bash + Write/Edit (PathPolicy).
HOOK_MATCHER = "Bash|Write|Edit"
_HOOK_SCRIPTS = {"PreToolUse": "pre_tool_use.py", "PostToolUse": "post_tool_use.py"}


class HooksDeployError(Exception):
    pass


@dataclass(frozen=True)
class DeployResult:
    status: str  # "created" | "unchanged" | "rewritten"
    path: Path
    sha256: str
    previous_sha256: str | None


def _posix_abs(p: str | Path) -> str:
    return str(Path(p).resolve()).replace("\\", "/")


def build_target_settings(herbert_root: str | Path, python_exe: str | Path = sys.executable) -> dict:
    root = _posix_abs(herbert_root)
    py = _posix_abs(python_exe)
    hooks = {
        event: [
            {
                "matcher": HOOK_MATCHER,
                "hooks": [{"type": "command", "command": f'"{py}" "{root}/.claude/hooks/{script}"'}],
            }
        ]
        for event, script in _HOOK_SCRIPTS.items()
    }
    return {"permissions": generate_settings_permissions(), "hooks": hooks}


def serialize_settings(settings: dict) -> bytes:
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str | None:
    """None si le fichier n'existe pas (ou plus) — jamais une exception."""
    try:
        return sha256_bytes(Path(path).read_bytes())
    except OSError:
        return None


def target_settings_path(herbert_root: str | Path, project_id: str) -> Path:
    return Path(herbert_root) / TARGET_SETTINGS_SUBDIR / project_id / "settings.json"


def deploy_target_settings(
    project: Project, herbert_root: str | Path, logs_dir: str | Path, python_exe: str | Path = sys.executable
) -> DeployResult:
    """Idempotent : identique -> rien ("unchanged") ; absent -> écrit
    ("created") ; différent -> réécrit ("rewritten" : fichier HERBERT, jamais
    partagé avec l'agent, pas de confirmation). Toute écriture est atomique
    (fichier temporaire + os.replace) puis RELUE et comparée octet par
    octet. Chaque appel est journalisé (JSONL, task_id=None, SHA-256)."""
    if project is None:
        raise HooksDeployError("projet absent")
    project_dir = Path(project.path)
    if not project_dir.is_dir():
        raise HooksDeployError(f"répertoire du projet introuvable: {project.path}")

    path = target_settings_path(herbert_root, project.id)
    if path.resolve().is_relative_to(project_dir.resolve()):
        raise HooksDeployError(
            f"le fichier settings HERBERT ({path}) tomberait dans le projet cible ({project.path}) — "
            "il doit rester hors du répertoire de travail de l'agent (ex. projet enregistré sur HERBERT lui-même)"
        )

    content = serialize_settings(build_target_settings(herbert_root, python_exe))
    sha = sha256_bytes(content)
    try:
        previous = path.read_bytes()
    except FileNotFoundError:
        previous = None
    previous_sha = sha256_bytes(previous) if previous is not None else None

    if previous == content:
        status = "unchanged"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, path)
        reread = path.read_bytes()
        if reread != content:
            raise HooksDeployError(f"relecture de {path} différente du contenu écrit")
        json.loads(reread.decode("utf-8"))  # JSON revalidé à la relecture, pas supposé
        status = "created" if previous is None else "rewritten"

    append_jsonl_event(
        logs_dir,
        component="hooks_deploy",
        event=f"settings HERBERT du projet cible : {status}",
        level="INFO",
        status=LogStatus.VERIFIED.value,
        task_id=None,
        details={
            "project_id": project.id,
            "project_name": project.name,
            "path": str(path),
            "sha256": sha,
            "previous_sha256": previous_sha,
            "status": status,
            "python_exe": _posix_abs(python_exe),
            "herbert_root": _posix_abs(herbert_root),
        },
    )
    return DeployResult(status=status, path=path, sha256=sha, previous_sha256=previous_sha)
