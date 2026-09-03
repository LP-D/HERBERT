from app.push_confirmation import (
    MIN_CONFIRM_DELAY_SECONDS,
    clear_pending_confirmation,
    diff_fingerprint,
    validate_confirmation,
    write_pending_confirmation,
)


def test_validate_without_any_pending_token_fails(tmp_path):
    valid, reason = validate_confirmation(tmp_path, "somefingerprint")

    assert valid is False
    assert "aucun push bloqué" in reason


def test_validate_too_soon_after_write_fails(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, ["taille: 50 lignes"])

    # "now" = exactement l'instant de l'écriture -> 0s écoulée, bien en
    # dessous du délai minimum requis.
    valid, reason = validate_confirmation(tmp_path, fp, now=__import__("time").time())

    assert valid is False
    assert "délai minimum" in reason


def test_validate_after_sufficient_delay_succeeds(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    data = json.loads(token_path.read_text(encoding="utf-8"))
    created_at = data["created_at"]

    valid, reason = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS + 1)

    assert valid is True


def test_validate_exactly_at_delay_boundary_is_sufficient(tmp_path):
    """Cas limite : "au moins N secondes" est un minimum INCLUSIF (>=) —
    contrairement au seuil strict de classify_push (< pour AUTO). Exactement
    au seuil doit donc réussir, pas échouer."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    valid, reason = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS)

    assert valid is True


def test_validate_just_under_delay_boundary_is_insufficient(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    valid, reason = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS - 0.5)

    assert valid is False


def test_validate_wrong_fingerprint_fails_even_after_delay(tmp_path):
    """Un nouveau commit après le blocage doit invalider le jeton — même
    après le délai, une empreinte différente doit être refusée."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    other_fp = diff_fingerprint("def456", "origin/main")
    valid, reason = validate_confirmation(tmp_path, other_fp, now=1e12)

    assert valid is False
    assert "diff a changé" in reason


def test_clear_pending_confirmation_removes_token(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    clear_pending_confirmation(tmp_path)

    valid, reason = validate_confirmation(tmp_path, fp, now=1e12)
    assert valid is False
    assert "aucun push bloqué" in reason


def test_diff_fingerprint_changes_with_head_commit():
    fp1 = diff_fingerprint("abc123", "origin/main")
    fp2 = diff_fingerprint("def456", "origin/main")
    assert fp1 != fp2


def test_diff_fingerprint_stable_for_same_inputs():
    fp1 = diff_fingerprint("abc123", "origin/main")
    fp2 = diff_fingerprint("abc123", "origin/main")
    assert fp1 == fp2


def test_diff_fingerprint_handles_no_upstream():
    """Tout premier push d'une branche : upstream=None ne doit pas lever."""
    fp = diff_fingerprint("abc123", None)
    assert isinstance(fp, str) and fp
