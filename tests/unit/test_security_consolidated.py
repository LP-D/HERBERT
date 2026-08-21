from scripts.verify_security import run_all_checks


def test_all_security_checks_pass(tmp_path):
    results = run_all_checks(work_dir=tmp_path)

    failed = [r for r in results if r.status == "FAILED"]
    assert not failed, "\n".join(f"{r.name}: {r.detail}" for r in failed)

    # garde-fou : si quelqu'un supprime des checks par erreur, le test doit
    # le remarquer plutôt que de passer trivialement sur une liste vide
    assert len(results) >= 10


def test_settings_json_write_check_actually_hashes_the_real_file(tmp_path):
    """Vérifie que le check dédié à settings.json compare bien un hash
    SHA-256 réel avant/après, pas une simple absence d'erreur."""
    from scripts.verify_security import (
        REAL_SETTINGS_JSON,
        _sha256,
        check_write_settings_json_blocked_and_file_untouched,
        load_hooks,
    )

    before = _sha256(REAL_SETTINGS_JSON)
    pre_module, _ = load_hooks()
    pre_module.REPO_ROOT = tmp_path

    result = check_write_settings_json_blocked_and_file_untouched(pre_module)

    after = _sha256(REAL_SETTINGS_JSON)
    assert result.status == "VERIFIED", result.detail
    assert before == after, "le fichier settings.json réel a été modifié par le test lui-même"
