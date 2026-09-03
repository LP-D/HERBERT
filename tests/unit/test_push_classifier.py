from app.push_classifier import MAX_AUTO_DIFF_LINES, classify_push


def test_all_criteria_met_classifies_auto():
    result = classify_push(files_changed=["README.md"], total_diff_lines=5, tests_passed=True)

    assert result.decision == "AUTO"
    assert result.failed_criteria == []


def test_hooks_file_touched_requires_manual():
    result = classify_push(files_changed=[".claude/hooks/pre_tool_use.py"], total_diff_lines=3, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any(".claude/hooks/" in c for c in result.failed_criteria)


def test_multiple_files_requires_manual():
    result = classify_push(files_changed=["README.md", "docs/DEPLOYMENT.md"], total_diff_lines=5, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("fichiers:" in c for c in result.failed_criteria)


def test_failing_tests_requires_manual_even_if_rest_passes():
    """Le point le plus important : même un diff parfait (1 fichier, petit,
    hors liste noire) doit rester MANUAL_REQUIRED si les tests échouent."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=2, tests_passed=False)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("tests:" in c for c in result.failed_criteria)


def test_diff_exactly_at_threshold_requires_manual():
    """Cas limite construit exprès : exactement au seuil (pas juste en
    dessous) doit basculer MANUAL_REQUIRED, pas AUTO."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=MAX_AUTO_DIFF_LINES, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("taille:" in c for c in result.failed_criteria)


def test_diff_just_under_threshold_is_auto():
    """Non-régression du cas limite : juste EN DESSOUS du seuil doit rester
    AUTO — sinon le seuil serait mal placé dans l'autre sens."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=MAX_AUTO_DIFF_LINES - 1, tests_passed=True)

    assert result.decision == "AUTO"


def test_settings_json_touched_requires_manual():
    result = classify_push(files_changed=[".claude/settings.json"], total_diff_lines=1, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any(".claude/settings.json" in c for c in result.failed_criteria)


def test_policy_file_touched_requires_manual():
    result = classify_push(files_changed=["app/policy/command_policy.py"], total_diff_lines=1, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("app/policy/" in c for c in result.failed_criteria)


def test_migration_file_touched_requires_manual():
    result = classify_push(files_changed=["migrations/0006_new.sql"], total_diff_lines=3, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("migrations/" in c for c in result.failed_criteria)


def test_config_system_yaml_touched_requires_manual():
    result = classify_push(files_changed=["config/system.yaml"], total_diff_lines=1, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"


def test_verify_security_script_touched_requires_manual():
    result = classify_push(files_changed=["scripts/verify_security.py"], total_diff_lines=1, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"


def test_classifier_module_itself_touched_requires_manual():
    """Auto-référentiel : un changement à la logique de classification
    elle-même (ou au jeton de confirmation) ne doit jamais s'auto-approuver."""
    result = classify_push(files_changed=["app/push_classifier.py"], total_diff_lines=1, tests_passed=True)
    assert result.decision == "MANUAL_REQUIRED"

    result2 = classify_push(files_changed=["app/push_confirmation.py"], total_diff_lines=1, tests_passed=True)
    assert result2.decision == "MANUAL_REQUIRED"


def test_windows_backslash_paths_normalized_for_blacklist_match():
    """Les chemins peuvent arriver avec des antislashs (Windows) — la
    détection de liste noire ne doit pas dépendre du séparateur."""
    result = classify_push(files_changed=[".claude\\hooks\\pre_tool_use.py"], total_diff_lines=1, tests_passed=True)

    assert result.decision == "MANUAL_REQUIRED"


def test_multiple_failed_criteria_all_reported_not_just_first():
    """Le doute ne bénéficie jamais à l'auto-push, et l'utilisateur doit
    voir TOUTES les raisons, pas seulement la première rencontrée."""
    result = classify_push(
        files_changed=[".claude/hooks/pre_tool_use.py", "README.md"], total_diff_lines=50, tests_passed=False
    )

    assert result.decision == "MANUAL_REQUIRED"
    assert len(result.failed_criteria) >= 3  # tests + fichiers + chemin sensible + taille
