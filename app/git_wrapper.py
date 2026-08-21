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


def push_to_origin(repo_path: str | Path, branch: str | None = None) -> GitResult:
    """Push explicite vers origin/<branch>. N'est appelé nulle part ailleurs
    dans HERBERT que par la commande CLI `engine sync push` — jamais en
    sous-effet d'une autre opération (commit, task create, etc.)."""
    if branch is None:
        branch = get_current_branch(repo_path)
    return _run_git(repo_path, ["push", "origin", branch])
