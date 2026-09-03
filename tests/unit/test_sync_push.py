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
    # Mini-repo de test sans suite pytest propre : le critère "tests réels"
    # de la classification est mocké ici (comme run_all_security_checks
    # l'est déjà plus bas) — un seul fichier (README.md, 1 ligne) reste
    # classé AUTO sans passer par --confirm-manual.
    monkeypatch.setattr(cli_main, "_herbert_tests_pass", lambda: True)

    args = argparse.Namespace(force_unsafe=False, confirm_manual=False)
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
    monkeypatch.setattr(cli_main, "_herbert_tests_pass", lambda: True)
    monkeypatch.setattr(
        cli_main,
        "run_all_security_checks",
        lambda: [CheckResult("faux check", "FAILED", "échec simulé pour le test")],
    )

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit jamais être appelé si la vérification sécurité échoue")

    monkeypatch.setattr(cli_main, "push_to_origin", _fail_if_called)

    args = argparse.Namespace(force_unsafe=False, confirm_manual=False)
    exit_code = cli_main.cmd_sync_push(args)

    assert exit_code == 1
    # le dépôt bare doit rester complètement vide (aucun ref)
    refs = _run_git(bare_origin, "for-each-ref").stdout.strip()
    assert refs == ""


def test_sync_push_force_unsafe_requires_typed_confirmation(repo_with_local_bare_origin, monkeypatch):
    work_repo, bare_origin = repo_with_local_bare_origin
    monkeypatch.setattr(cli_main, "REPO_ROOT", work_repo)
    monkeypatch.setattr(cli_main, "_herbert_tests_pass", lambda: True)
    monkeypatch.setattr(
        cli_main,
        "run_all_security_checks",
        lambda: [CheckResult("faux check", "FAILED", "échec simulé pour le test")],
    )
    monkeypatch.setattr("builtins.input", lambda prompt="": "non merci")

    args = argparse.Namespace(force_unsafe=True, confirm_manual=False)
    exit_code = cli_main.cmd_sync_push(args)

    assert exit_code == 1, "une réponse différente de 'OUI' exact ne doit jamais confirmer le push"
    refs = _run_git(bare_origin, "for-each-ref").stdout.strip()
    assert refs == ""


# --- classification AUTO / MANUAL_REQUIRED (V0.5+) ------------------------

def _establish_upstream_with_auto_push(work_repo, monkeypatch):
    """Un premier push AUTO (1 fichier, petit) pour établir origin/main comme
    branche amont — les scénarios MANUAL_REQUIRED ci-dessous portent sur les
    commits ajoutés APRÈS ce point, dans des conditions réalistes (upstream
    déjà configuré, pas le cas particulier du tout premier push)."""
    monkeypatch.setattr(cli_main, "REPO_ROOT", work_repo)
    monkeypatch.setattr(cli_main, "_herbert_tests_pass", lambda: True)
    exit_code = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=False))
    assert exit_code == 0, "le push initial doit réussir (précondition du test, pas ce qui est testé ici)"


def test_sync_push_blocks_multi_file_change_and_never_pushes(repo_with_local_bare_origin, monkeypatch):
    work_repo, bare_origin = repo_with_local_bare_origin
    _establish_upstream_with_auto_push(work_repo, monkeypatch)
    head_before = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()

    (work_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (work_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "deux fichiers a la fois")

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit jamais être appelé pour un diff MANUAL_REQUIRED sans confirmation")

    monkeypatch.setattr(cli_main, "push_to_origin", _fail_if_called)

    exit_code = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=False))

    assert exit_code == 1
    head_after = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before, "le dépôt bare ne doit pas avoir bougé"


def test_sync_push_confirm_manual_without_prior_block_fails(repo_with_local_bare_origin, monkeypatch):
    """--confirm-manual ne peut RIEN confirmer s'il n'y a jamais eu de
    blocage préalable (aucun jeton écrit)."""
    work_repo, bare_origin = repo_with_local_bare_origin
    _establish_upstream_with_auto_push(work_repo, monkeypatch)
    head_before = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()

    (work_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (work_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "deux fichiers, jamais bloqué avant")

    exit_code = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=True))

    assert exit_code == 1
    head_after = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()
    assert head_after == head_before, "le dépôt bare ne doit pas avoir bougé"


def test_sync_push_confirm_manual_too_soon_after_block_fails(repo_with_local_bare_origin, monkeypatch):
    """La chaîne réaliste d'un agent : bloquer puis confirmer IMMÉDIATEMENT
    après (même dans deux appels CLI séparés) doit échouer — le délai
    minimum n'est structurellement pas écoulé."""
    work_repo, bare_origin = repo_with_local_bare_origin
    _establish_upstream_with_auto_push(work_repo, monkeypatch)

    (work_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (work_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "deux fichiers")

    blocked_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=False))
    assert blocked_exit == 1

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit jamais être appelé si le délai minimum n'est pas écoulé")

    monkeypatch.setattr(cli_main, "push_to_origin", _fail_if_called)

    confirm_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=True))

    assert confirm_exit == 1


def test_sync_push_confirm_manual_after_delay_succeeds(repo_with_local_bare_origin, monkeypatch):
    """Le chemin nominal complet : bloqué, puis confirmé dans une commande
    séparée après le délai minimum réellement écoulé (délai raccourci ici
    pour ne pas ralentir la suite — le mécanisme testé est la comparaison
    temporelle elle-même, pas la valeur de production 30s)."""
    import time

    from app import push_confirmation

    monkeypatch.setattr(push_confirmation, "MIN_CONFIRM_DELAY_SECONDS", 0.05)

    work_repo, bare_origin = repo_with_local_bare_origin
    _establish_upstream_with_auto_push(work_repo, monkeypatch)

    (work_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (work_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "deux fichiers")

    blocked_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=False))
    assert blocked_exit == 1

    time.sleep(0.1)

    confirm_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=True))

    assert confirm_exit == 0
    local_head = _run_git(work_repo, "rev-parse", "HEAD").stdout.strip()
    remote_head = _run_git(bare_origin, "rev-parse", "HEAD").stdout.strip()
    assert local_head == remote_head, "le push confirmé doit réellement avoir eu lieu"


def test_sync_push_confirm_manual_after_new_commit_invalidates_stale_token(repo_with_local_bare_origin, monkeypatch):
    """Le jeton est lié à une empreinte précise du diff : un NOUVEAU commit
    après le blocage doit invalider le jeton, même une fois le délai
    écoulé — sinon --confirm-manual pourrait pousser un contenu jamais
    réellement classé."""
    import time

    from app import push_confirmation

    monkeypatch.setattr(push_confirmation, "MIN_CONFIRM_DELAY_SECONDS", 0.05)

    work_repo, bare_origin = repo_with_local_bare_origin
    _establish_upstream_with_auto_push(work_repo, monkeypatch)

    (work_repo / "a.txt").write_text("a\n", encoding="utf-8")
    (work_repo / "b.txt").write_text("b\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "deux fichiers")

    blocked_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=False))
    assert blocked_exit == 1

    # nouveau commit APRÈS le blocage : le jeton devient obsolète
    (work_repo / "c.txt").write_text("c\n", encoding="utf-8")
    _run_git(work_repo, "add", "-A")
    _run_git(work_repo, "commit", "-m", "encore un fichier après le blocage")

    time.sleep(0.1)

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("push_to_origin ne doit jamais être appelé avec un jeton obsolète")

    monkeypatch.setattr(cli_main, "push_to_origin", _fail_if_called)

    confirm_exit = cli_main.cmd_sync_push(argparse.Namespace(force_unsafe=False, confirm_manual=True))

    assert confirm_exit == 1
