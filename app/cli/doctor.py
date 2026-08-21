"""engine doctor — vérifications réelles de l'environnement, jamais inventées.

Chaque vérification exécute réellement la commande/l'import concerné et
rapporte un statut parmi VERIFIED / UNAVAILABLE / FAILED. Rien n'est
supposé : une vérification qui ne peut pas s'exécuter est UNAVAILABLE, pas
"probablement bon".
"""
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class CheckResult:
    name: str
    status: str  # VERIFIED | UNAVAILABLE | FAILED
    detail: str


def _run_version_check(binary: str, args: list[str], name: str) -> CheckResult:
    resolved = shutil.which(binary)
    if resolved is None:
        return CheckResult(name, "UNAVAILABLE", f"'{binary}' introuvable dans le PATH")
    try:
        # Sous Windows, subprocess (sans shell=True) n'applique pas la résolution
        # PATHEXT que fait cmd.exe : il faut le chemin complet résolu par
        # shutil.which (ex: claude.CMD pour une install npm), pas le nom nu.
        proc = subprocess.run([resolved, *args], capture_output=True, text=True, timeout=15)
    except Exception as exc:
        return CheckResult(name, "FAILED", f"échec d'exécution de '{binary}' (résolu: {resolved}): {exc}")
    if proc.returncode != 0:
        return CheckResult(name, "FAILED", f"'{binary} {' '.join(args)}' a retourné le code {proc.returncode}")
    return CheckResult(name, "VERIFIED", proc.stdout.strip() or proc.stderr.strip())


def check_python() -> CheckResult:
    return CheckResult("python", "VERIFIED", sys.version.split()[0])


def check_git() -> CheckResult:
    return _run_version_check("git", ["--version"], "git")


def check_sqlite() -> CheckResult:
    import sqlite3

    return CheckResult("sqlite3 (module Python)", "VERIFIED", sqlite3.sqlite_version)


def check_docker() -> CheckResult:
    result = _run_version_check("docker", ["--version"], "docker")
    if result.status == "UNAVAILABLE":
        result.detail += " (info seulement — non utilisé en V0.1)"
    return result


def check_claude_code() -> CheckResult:
    return _run_version_check("claude", ["--version"], "claude (Claude Code)")


def check_anthropic_api_key() -> CheckResult:
    present = "ANTHROPIC_API_KEY" in os.environ and bool(os.environ["ANTHROPIC_API_KEY"])
    detail = "définie (valeur non affichée, non utilisée par HERBERT)" if present else "non définie"
    return CheckResult("ANTHROPIC_API_KEY", "VERIFIED", detail)


def check_pytest() -> CheckResult:
    if importlib.util.find_spec("pytest") is None:
        return CheckResult("pytest", "UNAVAILABLE", "module 'pytest' non installé")
    import pytest

    return CheckResult("pytest", "VERIFIED", pytest.__version__)


def check_pydantic() -> CheckResult:
    if importlib.util.find_spec("pydantic") is None:
        return CheckResult("pydantic", "UNAVAILABLE", "module 'pydantic' non installé")
    import pydantic

    return CheckResult("pydantic", "VERIFIED", pydantic.VERSION)


def check_repo_structure(repo_root: Path) -> list[CheckResult]:
    expected = [
        "app/cli",
        "app/models",
        "app/state_machine",
        "app/database",
        "trusted",
        ".claude/settings.json",
        ".claude/hooks",
        "config/system.yaml",
        "migrations",
        "tests/unit",
        "logs",
    ]
    results = []
    for rel in expected:
        path = repo_root / rel
        if path.exists():
            results.append(CheckResult(f"structure: {rel}", "VERIFIED", "présent"))
        else:
            results.append(CheckResult(f"structure: {rel}", "FAILED", "absent"))
    return results


def run_all_checks(repo_root: Path) -> list[CheckResult]:
    results = [
        check_python(),
        check_git(),
        check_sqlite(),
        check_docker(),
        check_claude_code(),
        check_anthropic_api_key(),
        check_pytest(),
        check_pydantic(),
    ]
    results.extend(check_repo_structure(repo_root))
    return results
