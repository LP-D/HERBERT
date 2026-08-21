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
