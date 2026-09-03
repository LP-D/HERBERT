"""Wrapper Git minimal : branche candidate, commit, rollback simple.

Aucun push automatique, jamais. Toute opération est un appel `git` réel via
subprocess ; aucune opération n'est jamais affirmée comme réussie sans que
le code de retour du process réel ait été vérifié.
"""
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GitResult:
    ok: bool
    stdout: str
    stderr: str
    returncode: int


class GitWrapperError(Exception):
    pass


def _run_git(repo_path: str | Path, args: list[str]) -> GitResult:
    proc = subprocess.run(
        ["git", "-C", str(repo_path), *args],
        capture_output=True,
        text=True,
    )
    return GitResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout.strip(),
        stderr=proc.stderr.strip(),
        returncode=proc.returncode,
    )


def get_current_branch(repo_path: str | Path) -> str:
    result = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not result.ok:
        raise GitWrapperError(f"impossible de déterminer la branche courante: {result.stderr}")
    return result.stdout


def create_candidate_branch(repo_path: str | Path, branch_name: str) -> GitResult:
    """Crée et bascule sur une branche candidate depuis la branche courante."""
    return _run_git(repo_path, ["checkout", "-b", branch_name])


def commit_candidate(repo_path: str | Path, message: str) -> GitResult:
    """Stage tout le répertoire de travail et commit sur la branche courante."""
    add_result = _run_git(repo_path, ["add", "-A"])
    if not add_result.ok:
        return add_result
    return _run_git(repo_path, ["commit", "-m", message])


def rollback_to_commit(repo_path: str | Path, commit_ref: str) -> GitResult:
    """git reset --hard vers un commit/ref stable donné. Irréversible : appelant
    responsable de confirmer avant d'invoquer cette fonction."""
    return _run_git(repo_path, ["reset", "--hard", commit_ref])


def checkout_branch(repo_path: str | Path, branch_name: str) -> GitResult:
    """Retour sur une branche existante (ex: la branche d'origine)."""
    return _run_git(repo_path, ["checkout", branch_name])


def get_head_commit(repo_path: str | Path) -> str:
    result = _run_git(repo_path, ["rev-parse", "HEAD"])
    if not result.ok:
        raise GitWrapperError(f"impossible de lire HEAD: {result.stderr}")
    return result.stdout


def diff_files_since(repo_path: str | Path, base_commit: str) -> list[str]:
    """Fichiers modifiés entre `base_commit` et HEAD (candidate branch).
    Équivalent structuré de `git diff --stat` : on veut une liste de
    chemins pour ChangeProof.files_changed, pas du texte à re-parser."""
    result = _run_git(repo_path, ["diff", "--name-only", base_commit, "HEAD"])
    if not result.ok:
        raise GitWrapperError(f"impossible de calculer le diff depuis {base_commit}: {result.stderr}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def merge_branch(repo_path: str | Path, source_branch: str, message: str | None = None, no_ff: bool = True) -> GitResult:
    """Merge `source_branch` DANS la branche déjà checkout (l'appelant doit
    avoir fait checkout_branch(stable_branch) avant). --no-ff par défaut :
    garde un commit de merge identifiable, nécessaire pour `revert_commit`
    (un revert de merge a besoin de -m <mainline>, donc d'un vrai commit de
    merge, pas d'un fast-forward)."""
    args = ["merge"]
    if no_ff:
        args.append("--no-ff")
    if message:
        args.extend(["-m", message])
    args.append(source_branch)
    return _run_git(repo_path, args)


def revert_commit(repo_path: str | Path, commit_ref: str, mainline: int | None = None) -> GitResult:
    """git revert : annule un commit via un NOUVEAU commit, sans réécrire
    l'historique (contrairement à rollback_to_commit/reset --hard). Utilisé
    pour l'AUTO_ROLLBACK d'un merge de promotion en échec de health check —
    mainline=1 pour un commit de merge (garde la branche stable comme
    parent principal)."""
    args = ["revert", "--no-edit"]
    if mainline is not None:
        args.extend(["-m", str(mainline)])
    args.append(commit_ref)
    return _run_git(repo_path, args)


# SHA1 constant de l'arbre vide Git (universel, indépendant du dépôt) —
# utilisé comme référence de diff quand aucune branche amont n'existe
# encore (tout premier push d'une branche) : tout le contenu de HEAD est
# alors "le diff à pousser", sans cas particulier fragile.
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def get_upstream_ref(repo_path: str | Path) -> str | None:
    """`origin/<branche>` si une branche amont est configurée (déjà
    poussée au moins une fois avec suivi), None sinon — ex. tout premier
    push d'une branche, où @{u} n'a rien à résoudre."""
    result = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not result.ok:
        return None
    return result.stdout


def diff_numstat_since(repo_path: str | Path, ref: str) -> tuple[list[str], int]:
    """Fichiers modifiés + total de lignes changées (ajouts+suppressions)
    entre `ref` et HEAD — utilisé par la classification AUTO/MANUAL_REQUIRED
    de `engine sync push` (app/push_classifier.py). `ref` peut être
    EMPTY_TREE_SHA pour le tout premier push d'une branche."""
    result = _run_git(repo_path, ["diff", "--numstat", ref, "HEAD"])
    if not result.ok:
        raise GitWrapperError(f"impossible de calculer le diff numstat depuis {ref}: {result.stderr}")

    files: list[str] = []
    total_lines = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added_str, deleted_str, path = parts
        # Fichier binaire : git rapporte "-" pour added/deleted (pas
        # mesurable en lignes). Traité prudemment comme un total élevé
        # pour forcer MANUAL_REQUIRED plutôt que de sous-compter
        # silencieusement à 0 — le doute ne bénéficie jamais à l'auto-push.
        added = int(added_str) if added_str.isdigit() else 10_000
        deleted = int(deleted_str) if deleted_str.isdigit() else 10_000
        total_lines += added + deleted
        files.append(path)
    return files, total_lines


def push_to_origin(repo_path: str | Path, branch: str | None = None) -> GitResult:
    """Push explicite vers origin/<branch>. N'est appelé nulle part ailleurs
    dans HERBERT que par la commande CLI `engine sync push` — jamais en
    sous-effet d'une autre opération (commit, task create, etc.).

    `-u` (--set-upstream) : sans lui, `git push origin <branch>` ne configure
    JAMAIS le tracking amont (@{u}) — bug réel constaté le 2026-09-03 sur
    une branche jamais poussée avec -u, où get_upstream_ref() retombait en
    permanence sur EMPTY_TREE_SHA, faussant silencieusement le calcul du
    diff pour la classification AUTO/MANUAL_REQUIRED (app/push_classifier.py)
    à CHAQUE push suivant, pas seulement le premier. Idempotent : ré-exécuter
    -u sur une branche déjà trackée ne change rien d'observable."""
    if branch is None:
        branch = get_current_branch(repo_path)
    return _run_git(repo_path, ["push", "-u", "origin", branch])
