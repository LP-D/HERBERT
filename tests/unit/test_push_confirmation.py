import pytest

from app.push_confirmation import (
    MIN_CONFIRM_DELAY_SECONDS,
    TOKEN_EXPIRY_WINDOW_SECONDS,
    clear_pending_confirmation,
    diff_fingerprint,
    validate_confirmation,
    write_pending_confirmation,
)


def test_validate_without_any_pending_token_fails(tmp_path):
    valid, reason, elapsed = validate_confirmation(tmp_path, "somefingerprint")

    assert valid is False
    assert "aucun push bloqué" in reason
    assert elapsed is None, "aucun jeton -> aucune mesure de temps possible"


def test_validate_too_soon_after_write_fails(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, ["taille: 50 lignes"])

    # "now" = exactement l'instant de l'écriture -> 0s écoulée, bien en
    # dessous du délai minimum requis.
    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=__import__("time").time())

    assert valid is False
    assert "délai minimum" in reason
    assert elapsed is not None and elapsed == pytest.approx(0, abs=1.0)


def test_validate_after_sufficient_delay_succeeds(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    data = json.loads(token_path.read_text(encoding="utf-8"))
    created_at = data["created_at"]

    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS + 1)

    assert valid is True
    assert elapsed == MIN_CONFIRM_DELAY_SECONDS + 1


def test_validate_exactly_at_delay_boundary_is_sufficient(tmp_path):
    """Cas limite : "au moins N secondes" est un minimum INCLUSIF (>=) —
    contrairement au seuil strict de classify_push (< pour AUTO). Exactement
    au seuil doit donc réussir, pas échouer."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS)

    assert valid is True
    assert elapsed == MIN_CONFIRM_DELAY_SECONDS


def test_validate_just_under_delay_boundary_is_insufficient(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + MIN_CONFIRM_DELAY_SECONDS - 0.5)

    assert valid is False


def test_validate_just_under_expiry_window_still_valid(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    max_valid = MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS
    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + max_valid - 0.5)

    assert valid is True


def test_validate_exactly_at_expiry_boundary_still_valid(tmp_path):
    """Borne haute inclusive, symétrique à la borne basse inclusive du délai
    minimum — cohérent avec "fenêtre [MIN, MIN+WINDOW]"."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    max_valid = MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS
    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + max_valid)

    assert valid is True


def test_validate_past_expiry_window_fails_with_clear_message(tmp_path):
    """Le cas central du point 3 : un jeton qui reste "en attente" bien
    au-delà de la fenêtre (ex. quelqu'un revient des heures plus tard sans
    action explicite entre-temps) doit être refusé, pas silencieusement
    accepté parce que le diff n'a pas changé."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    max_valid = MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS
    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + max_valid + 0.5)

    assert valid is False
    assert "expiré" in reason


def test_validate_hours_after_block_fails_expired_not_reason_used_for_min_delay(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    import json

    token_path = tmp_path / "data" / "pending_manual_push.json"
    created_at = json.loads(token_path.read_text(encoding="utf-8"))["created_at"]

    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=created_at + 3600)

    assert valid is False
    assert "expiré" in reason
    assert "délai minimum" not in reason  # la vraie raison est l'expiration, pas le délai minimum
    assert elapsed == 3600


def test_validate_wrong_fingerprint_fails_even_after_delay(tmp_path):
    """Un nouveau commit après le blocage doit invalider le jeton — même
    après le délai, une empreinte différente doit être refusée."""
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    other_fp = diff_fingerprint("def456", "origin/main")
    valid, reason, elapsed = validate_confirmation(tmp_path, other_fp, now=1e12)

    assert valid is False
    assert "diff a changé" in reason
    assert elapsed is not None, "le diff a changé mais le temps écoulé reste mesurable et doit être journalisé"


def test_clear_pending_confirmation_removes_token(tmp_path):
    fp = diff_fingerprint("abc123", "origin/main")
    write_pending_confirmation(tmp_path, fp, [])

    clear_pending_confirmation(tmp_path)

    valid, reason, elapsed = validate_confirmation(tmp_path, fp, now=1e12)
    assert valid is False
    assert "aucun push bloqué" in reason
    assert elapsed is None


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
