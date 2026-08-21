import argparse
import subprocess

import pytest

from app.cli import main as cli_main
from scripts.verify_security import CheckResult


def _run_git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_with_local_bare_origin(tmp_path):
    """Un dépôt de travail avec un VRAI remote 'origin', mais un remote
    100% local (dépôt bare sur disque) — jamais GitHub. Sert à tester
    `engine sync push` sans jamais toucher au vrai LP-D/HERBERT distant."""
    bare_origin = tmp_path / "bare_origin.git"
    bare_origin.mkdir()
    _run_git(bare_origin, "init", "--bare")

    work_repo = tmp_path / "work_repo"
    work_repo.mkdir()
    _run_git(work_repo, "init")
    _run_git(work_repo, "config", "user.email", "test@herbert.local")
    _run_git(work_repo, "config", "user.name", "HERBERT Test")
    (work_repo / "README.md").write_text("test\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "commit de test")
    _run_git(work_repo, "remote", "add", "origin", str(bare_origin))

    return work_repo, bare_origin


def test_sync_push_succeeds_against_local_bare_origin_when_security_ok(repo_with_local_bare_origin, monkeypatch):
    work_repo, bare_origin = repo_with_local_bare_origin
    monkeypatch.setattr(cli_main, "REPO_ROOT", work_repo)

    args = argparse.Namespace(force_unsafe=False)
    exit_code = cli_main.cmd_sync_push(args)

    assert exit_code == 0

    # le commit doit être réellement présent dans le dépôt bare "origin"
    local_head = _run_git(work_repo, "rev-parse", "HEAD").stdout.strip()
    remote_head = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()
    assert local_head == remote_head


def test_sync_push_refuses_when_security_check_fails_and_never_pushes(
    repo_with_local_bare_origin, monkeypatch
):
    work_repo, bare_origin = repo_with_local_bare_origin
    monkeypatch.setattr(cli_main, "REPO_ROOT", work_repo)
    monkeypatch.setattr(
        cli_main,
        "run_all_security_checks",
        lambda: [CheckResult("faux check", "FAILED", "échec simulé pour le test")],
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit jamais être appelé si la vérification sécurité échoue")

    monkeypatch.setattr(cli_main, "push_to_origin", _fail_if_called)

    args = argparse.Namespace(force_unsafe=False)
    exit_code = cli_main.cmd_sync_push(args)

    assert exit_code == 1
    # le dépôt bare doit rester complètement vide (aucun ref)
    refs = _run_git(bare_origin, "for-each-ref").stdout.strip()
    assert refs == ""


def test_sync_push_force_unsafe_requires_typed_confirmation(repo_with_local_bare_origin, monkeypatch):
    work_repo, bare_origin = repo_with_local_bare_origin
    monkeypatch.setattr(cli_main, "REPO_ROOT", work_repo)
    monkeypatch.setattr(
        cli_main,
        "run_all_security_checks",
        lambda: [CheckResult("faux check", "FAILED", "échec simulé pour le test")],
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "non merci")

    args = argparse.Namespace(force_unsafe=True)
    exit_code = cli_main.cmd_sync_push(args)

    assert exit_code == 1, "une réponse différente de 'OUI' exact ne doit jamais confirmer le push"
    refs = _run_git(bare_origin, "for-each-ref").stdout.strip()
    assert refs == ""
