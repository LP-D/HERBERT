from app import git_wrapper


def test_rollback_restores_previous_state(git_repo):
    stable_commit = git_wrapper.get_head_commit(git_repo)

    branch_result = git_wrapper.create_candidate_branch(git_repo, "herbert/candidate-test")
    assert branch_result.ok, branch_result.stderr

    new_file = git_repo / "change.txt"
    new_file.write_text("modification candidate\n", encoding="utf-8")

    commit_result = git_wrapper.commit_candidate(git_repo, "changement de test")
    assert commit_result.ok, commit_result.stderr
    assert new_file.exists()

    candidate_commit = git_wrapper.get_head_commit(git_repo)
    assert candidate_commit != stable_commit

    rollback_result = git_wrapper.rollback_to_commit(git_repo, stable_commit)
    assert rollback_result.ok, rollback_result.stderr

    assert not new_file.exists(), "le rollback doit faire disparaître le fichier ajouté par le commit candidat"
    assert git_wrapper.get_head_commit(git_repo) == stable_commit


def test_diff_numstat_rename_reports_source_and_destination_separately(git_repo):
    """Sans --no-renames, git produit `app/{policy => other}/rules.py`, qu'aucun
    motif protégé (préfixe `app/policy/`) ne reconnaît : déplacer un fichier
    protégé passait inaperçu. Les deux chemins réels doivent apparaître, et
    être vérifiés individuellement par classify_push."""
    from app.push_classifier import HERBERT_BLOCKED_PATTERNS, classify_push

    import subprocess

    def run(*args):
        subprocess.run(["git", *args], cwd=git_repo, capture_output=True, text=True, check=True)

    policy_dir = git_repo / "app" / "policy"
    policy_dir.mkdir(parents=True)
    (policy_dir / "rules.py").write_text("".join(f"REGLE_{i} = {i}\n" for i in range(10)), encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "ajout règles")
    base = git_wrapper.get_head_commit(git_repo)

    (git_repo / "app" / "other").mkdir()
    run("mv", "app/policy/rules.py", "app/other/rules.py")
    run("commit", "-m", "déplacement")

    files, total_lines = git_wrapper.diff_numstat_since(git_repo, base)

    assert sorted(files) == ["app/other/rules.py", "app/policy/rules.py"]
    assert not any("=>" in f for f in files)
    assert total_lines == 20  # 10 supprimées + 10 ajoutées

    result = classify_push(files, total_lines, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)
    assert result.decision == "MANUAL_REQUIRED"
    sensitive = [c for c in result.failed_criteria if c.startswith("chemin sensible")]
    assert sensitive == ["chemin sensible: app/policy/rules.py (liste noire: app/policy/)"]


def test_create_candidate_branch_switches_branch(git_repo):
    original_branch = git_wrapper.get_current_branch(git_repo)
    result = git_wrapper.create_candidate_branch(git_repo, "herbert/candidate-2")
    assert result.ok, result.stderr

    new_branch = git_wrapper.get_current_branch(git_repo)
    assert new_branch == "herbert/candidate-2"
    assert new_branch != original_branch


def test_checkout_branch_returns_to_origin(git_repo):
    original_branch = git_wrapper.get_current_branch(git_repo)
    git_wrapper.create_candidate_branch(git_repo, "herbert/candidate-3")

    result = git_wrapper.checkout_branch(git_repo, original_branch)
    assert result.ok, result.stderr
    assert git_wrapper.get_current_branch(git_repo) == original_branch
