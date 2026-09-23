from app.push_classifier import HERBERT_BLOCKED_PATTERNS, MAX_AUTO_DIFF_LINES, classify_push


def test_all_criteria_met_classifies_auto():
    result = classify_push(files_changed=["README.md"], total_diff_lines=5, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "AUTO"
    assert result.failed_criteria == []


def test_hooks_file_touched_requires_manual():
    result = classify_push(files_changed=[".claude/hooks/pre_tool_use.py"], total_diff_lines=3, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any(".claude/hooks/" in c for c in result.failed_criteria)


def test_multiple_files_requires_manual():
    result = classify_push(files_changed=["README.md", "docs/DEPLOYMENT.md"], total_diff_lines=5, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("fichiers:" in c for c in result.failed_criteria)


def test_failing_tests_requires_manual_even_if_rest_passes():
    """Le point le plus important : même un diff parfait (1 fichier, petit,
    hors liste noire) doit rester MANUAL_REQUIRED si les tests échouent."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=2, tests_passed=False, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("tests:" in c for c in result.failed_criteria)


def test_diff_exactly_at_threshold_requires_manual():
    """Cas limite construit exprès : exactement au seuil (pas juste en
    dessous) doit basculer MANUAL_REQUIRED, pas AUTO."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=MAX_AUTO_DIFF_LINES, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("taille:" in c for c in result.failed_criteria)


def test_diff_just_under_threshold_is_auto():
    """Non-régression du cas limite : juste EN DESSOUS du seuil doit rester
    AUTO — sinon le seuil serait mal placé dans l'autre sens."""
    result = classify_push(files_changed=["README.md"], total_diff_lines=MAX_AUTO_DIFF_LINES - 1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "AUTO"


def test_settings_json_touched_requires_manual():
    result = classify_push(files_changed=[".claude/settings.json"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any(".claude/settings.json" in c for c in result.failed_criteria)


def test_policy_file_touched_requires_manual():
    result = classify_push(files_changed=["app/policy/command_policy.py"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("app/policy/" in c for c in result.failed_criteria)


def test_migration_file_touched_requires_manual():
    result = classify_push(files_changed=["migrations/0006_new.sql"], total_diff_lines=3, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("migrations/" in c for c in result.failed_criteria)


def test_config_system_yaml_touched_requires_manual():
    result = classify_push(files_changed=["config/system.yaml"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"


def test_verify_security_script_touched_requires_manual():
    result = classify_push(files_changed=["scripts/verify_security.py"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"


def test_classifier_module_itself_touched_requires_manual():
    """Auto-référentiel : un changement à la logique de classification
    elle-même (ou au jeton de confirmation) ne doit jamais s'auto-approuver."""
    result = classify_push(files_changed=["app/push_classifier.py"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)
    assert result.decision == "MANUAL_REQUIRED"

    result2 = classify_push(files_changed=["app/push_confirmation.py"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)
    assert result2.decision == "MANUAL_REQUIRED"


def test_windows_backslash_paths_normalized_for_blacklist_match():
    """Les chemins peuvent arriver avec des antislashs (Windows) — la
    détection de liste noire ne doit pas dépendre du séparateur."""
    result = classify_push(files_changed=[".claude\\hooks\\pre_tool_use.py"], total_diff_lines=1, tests_passed=True, blocked_patterns=HERBERT_BLOCKED_PATTERNS)

    assert result.decision == "MANUAL_REQUIRED"


def test_multiple_failed_criteria_all_reported_not_just_first():
    """Le doute ne bénéficie jamais à l'auto-push, et l'utilisateur doit
    voir TOUTES les raisons, pas seulement la première rencontrée."""
    result = classify_push(
        files_changed=[".claude/hooks/pre_tool_use.py", "README.md"], total_diff_lines=50, tests_passed=False,
        blocked_patterns=HERBERT_BLOCKED_PATTERNS,
    )

    assert result.decision == "MANUAL_REQUIRED"
    assert len(result.failed_criteria) >= 3  # tests + fichiers + chemin sensible + taille


# --- Chemins protégés par projet (migration 0009) -----------------------

import pytest  # noqa: E402

from app.database.repository import (  # noqa: E402
    add_project_blocked_patterns,
    get_project,
    insert_project,
)
from app.models import Project  # noqa: E402
from app.push_classifier import (  # noqa: E402
    DEFAULT_BLOCKED_PATTERNS,
    UNRESOLVED_PATTERNS_CRITERION,
    resolve_blocked_patterns,
)


def _classify_target(path, patterns=DEFAULT_BLOCKED_PATTERNS):
    return classify_push(files_changed=[path], total_diff_lines=1, tests_passed=True, blocked_patterns=patterns)


def _insert(db_conn, name):
    project = Project(name=name, path="C:/cible")
    insert_project(db_conn, project)
    return project


def _store_raw(db_conn, project, raw):
    db_conn.execute("UPDATE projects SET extra_blocked_patterns = ? WHERE id = ?", (raw, project.id))
    db_conn.commit()


def test_none_blocked_patterns_forces_manual_even_if_everything_else_passes():
    result = classify_push(files_changed=["README.md"], total_diff_lines=1, tests_passed=True, blocked_patterns=None)

    assert result.decision == "MANUAL_REQUIRED"
    assert result.failed_criteria == [UNRESOLVED_PATTERNS_CRITERION]


@pytest.mark.parametrize("invalid", [[], (), "README.md", [".env", 3], [""], ["   "], {".env"}])
def test_invalid_blocked_patterns_force_manual(invalid):
    """Liste vide, chaîne seule (itérable caractère par caractère), élément
    non-chaîne ou vide, ensemble : jamais interprétés comme "rien à protéger"."""
    result = _classify_target("README.md", invalid)

    assert result.decision == "MANUAL_REQUIRED"
    assert UNRESOLVED_PATTERNS_CRITERION in result.failed_criteria


def test_blocked_patterns_is_required_keyword_without_default():
    with pytest.raises(TypeError):
        classify_push(files_changed=["README.md"], total_diff_lines=1, tests_passed=True)


def test_invalid_json_in_db_resolves_to_none_and_forces_manual(db_conn):
    project = _insert(db_conn, "cible-json-casse")
    _store_raw(db_conn, project, "{pas du json")

    stored = get_project(db_conn, project.id)  # ne lève pas : le reste de HERBERT continue de fonctionner
    assert stored.extra_blocked_patterns is None
    patterns = resolve_blocked_patterns(stored)
    assert patterns is None

    result = _classify_target("README.md", patterns)
    assert result.decision == "MANUAL_REQUIRED"
    assert UNRESOLVED_PATTERNS_CRITERION in result.failed_criteria


@pytest.mark.parametrize("stored_value", ['{"a": 1}', '"README.md"', "[1, 2]", '[""]'])
def test_non_list_of_strings_json_in_db_resolves_to_none(db_conn, stored_value):
    project = _insert(db_conn, "cible-json-type")
    _store_raw(db_conn, project, stored_value)

    assert resolve_blocked_patterns(get_project(db_conn, project.id)) is None


def test_resolve_none_project_is_unresolved():
    assert resolve_blocked_patterns(None) is None


def test_null_column_resolves_to_defaults_only_not_a_failure(db_conn):
    project = _insert(db_conn, "cible-null")

    assert resolve_blocked_patterns(get_project(db_conn, project.id)) == DEFAULT_BLOCKED_PATTERNS


@pytest.mark.parametrize("path", [".env", "config/.env", "deep/nested/dir/.env", "src/.env.production", "APP/.ENV"])
def test_env_file_at_any_depth_requires_manual(path):
    result = _classify_target(path)

    assert result.decision == "MANUAL_REQUIRED"
    assert any("chemin sensible" in c for c in result.failed_criteria)


@pytest.mark.parametrize("path", ["conftest.py", "tests/conftest.py", "tests\\unit\\conftest.py"])
def test_conftest_at_any_depth_requires_manual(path):
    assert _classify_target(path).decision == "MANUAL_REQUIRED"


@pytest.mark.parametrize("path", ["server.pem", "certs/prod/server.PEM"])
def test_pem_file_requires_manual(path):
    assert _classify_target(path).decision == "MANUAL_REQUIRED"


def test_github_workflow_requires_manual():
    result = _classify_target(".github/workflows/x.yml")

    assert result.decision == "MANUAL_REQUIRED"
    assert any(".github/" in c for c in result.failed_criteria)


@pytest.mark.parametrize(
    "path",
    [".vscode/tasks.json", ".vscode/launch.json", ".claude/settings.local.json", "pyproject.toml",
     "requirements-dev.txt", "CLAUDE.md", ".GitHub/workflows/x.yml"],
)
def test_other_default_prefixes_require_manual(path):
    assert _classify_target(path).decision == "MANUAL_REQUIRED"


@pytest.mark.parametrize("path", [".env.example", "config/.env.sample", ".env.template"])
def test_env_template_files_are_explicit_exceptions_and_classify_auto(path):
    result = _classify_target(path)

    assert result.decision == "AUTO", result.failed_criteria


def test_vscode_settings_json_not_blocked_by_default():
    """Seuls tasks.json/launch.json (exécution de code) sont protégés."""
    assert _classify_target(".vscode/settings.json").decision == "AUTO"


def test_readme_on_target_project_with_defaults_only_is_auto(db_conn):
    """Non-régression : les défauts ne doivent pas bloquer un changement
    ordinaire d'un projet cible."""
    project = _insert(db_conn, "cible-readme")
    patterns = resolve_blocked_patterns(get_project(db_conn, project.id))

    for path in ["README.md", "src/solver.py", "tests/test_solver.py"]:
        result = _classify_target(path, patterns)
        assert result.decision == "AUTO", (path, result.failed_criteria)


def test_herbert_specific_prefixes_not_applied_to_target_projects():
    """app/policy/ n'a de sens que pour HERBERT : un projet cible qui a son
    propre app/policy/ n'hérite pas de cette règle par défaut."""
    assert _classify_target("app/policy/rules.py").decision == "AUTO"
    assert _classify_target("app/policy/rules.py", HERBERT_BLOCKED_PATTERNS).decision == "MANUAL_REQUIRED"


def test_project_added_pattern_is_respected(db_conn):
    project = _insert(db_conn, "cible-ajout")
    before = resolve_blocked_patterns(get_project(db_conn, project.id))
    assert _classify_target("data/grille.db", before).decision == "AUTO"

    add_project_blocked_patterns(db_conn, project.id, ["data/", "**/*.sqlite"])
    patterns = resolve_blocked_patterns(get_project(db_conn, project.id))

    result = _classify_target("data/grille.db", patterns)
    assert result.decision == "MANUAL_REQUIRED"
    assert any("data/" in c for c in result.failed_criteria)
    assert _classify_target("cache/x/y.sqlite", patterns).decision == "MANUAL_REQUIRED"


def test_project_additions_never_remove_a_default(db_conn):
    project = _insert(db_conn, "cible-defauts")
    add_project_blocked_patterns(db_conn, project.id, ["data/"])
    add_project_blocked_patterns(db_conn, project.id, ["docs/private/"])

    patterns = resolve_blocked_patterns(get_project(db_conn, project.id))

    assert patterns[: len(DEFAULT_BLOCKED_PATTERNS)] == DEFAULT_BLOCKED_PATTERNS
    assert {"data/", "docs/private/"} <= set(patterns)
    assert _classify_target(".env", patterns).decision == "MANUAL_REQUIRED"


def test_add_is_merge_never_replacement(db_conn):
    project = _insert(db_conn, "cible-fusion")

    assert add_project_blocked_patterns(db_conn, project.id, ["a/", "b/"]) == ["a/", "b/"]
    assert add_project_blocked_patterns(db_conn, project.id, ["b/", "c/"]) == ["c/"]
    assert get_project(db_conn, project.id).extra_blocked_patterns == ["a/", "b/", "c/"]


def test_add_refuses_to_overwrite_unreadable_stored_value(db_conn):
    project = _insert(db_conn, "cible-illisible")
    _store_raw(db_conn, project, "not json")

    with pytest.raises(ValueError):
        add_project_blocked_patterns(db_conn, project.id, ["x/"])
    raw = db_conn.execute("SELECT extra_blocked_patterns FROM projects WHERE id = ?", (project.id,)).fetchone()[0]
    assert raw == "not json"
