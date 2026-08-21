from app.policy.path_policy import PathViolation, validate_path


def test_valid_path_accepted(tmp_path):
    result = validate_path("src/main.py", str(tmp_path))
    assert result.allowed is True
    assert result.violation is None


def test_path_traversal_rejected(tmp_path):
    result = validate_path("../../etc/passwd", str(tmp_path))
    assert result.allowed is False
    assert result.violation == PathViolation.TRAVERSAL


def test_traversal_rejected_even_if_absolute_path_resolves_inside_root(tmp_path):
    (tmp_path / "sub").mkdir()
    candidate = str(tmp_path / "sub" / ".." / "sub" / "file.py")
    result = validate_path(candidate, str(tmp_path))
    # le segment '..' est rejeté sur l'intention, pas seulement le résultat résolu
    assert result.allowed is False
    assert result.violation == PathViolation.TRAVERSAL


def test_absolute_path_outside_root_rejected(tmp_path):
    result = validate_path("C:/Windows/System32/config", str(tmp_path))
    assert result.allowed is False
    assert result.violation == PathViolation.OUTSIDE_ROOT


def test_reserved_windows_name_rejected_without_extension(tmp_path):
    result = validate_path("CON", str(tmp_path))
    assert result.allowed is False
    assert result.violation == PathViolation.RESERVED_NAME


def test_reserved_windows_name_rejected_with_extension(tmp_path):
    result = validate_path("sub/lpt1.log", str(tmp_path))
    assert result.allowed is False
    assert result.violation == PathViolation.RESERVED_NAME


def test_reserved_windows_name_case_insensitive(tmp_path):
    result = validate_path("Nul.txt", str(tmp_path))
    assert result.allowed is False
    assert result.violation == PathViolation.RESERVED_NAME


def test_non_reserved_name_containing_reserved_substring_is_allowed(tmp_path):
    # "console.py" contient "CON" mais n'est PAS le nom réservé lui-même
    result = validate_path("console.py", str(tmp_path))
    assert result.allowed is True


def test_symlink_flagged_but_not_blocking_when_inside_root(tmp_path):
    target = tmp_path / "real_file.txt"
    target.write_text("contenu", encoding="utf-8")
    link = tmp_path / "link_to_file.txt"
    try:
        link.symlink_to(target)
    except OSError as exc:
        import pytest

        # Cause réelle constatée (pas une supposition) : WinError 1314,
        # "le client ne dispose pas d'un privilège nécessaire" —
        # SeCreateSymbolicLinkPrivilege absent. Sur Windows, un compte
        # standard ne l'a QUE si le mode développeur est activé, ou en
        # session élevée (admin). Ni l'un ni l'autre ici : skip légitime
        # et nommé, pas un échec du test lui-même.
        pytest.skip(
            f"privilège SeCreateSymbolicLinkPrivilege absent (mode développeur Windows "
            f"non activé / session non élevée) — {exc}"
        )

    result = validate_path("link_to_file.txt", str(tmp_path))
    assert result.allowed is True
    assert result.is_symlink_or_junction is True
