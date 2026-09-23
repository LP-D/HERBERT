import subprocess

import pytest

from app import claude_headless
from app.claude_headless import (
    ClaudeExecutableConfigError,
    InvocationInfrastructureError,
    build_context_prompt,
    invoke_claude_headless,
    validate_claude_executable,
)
from app.models import Project, TestCaseOutcome, TestCaseResult, TestResult
from app.models.enums import HeadlessInvocationStatus


def _project() -> Project:
    return Project(name="proj", path="C:/proj")


@pytest.fixture
def exe_and_settings(tmp_path):
    """Chemins absolus existants : invoke_claude_headless refuse de lancer
    l'agent si l'exécutable n'est pas absolu ou si le fichier settings
    HERBERT est absent."""
    exe = tmp_path / "bin" / "claude.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    settings = tmp_path / "herbert" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}", encoding="utf-8")
    return str(exe), str(settings)


def _invoke(exe_and_settings, **overrides):
    exe, settings = exe_and_settings
    kwargs = dict(cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10, executable=exe, settings_path=settings)
    kwargs.update(overrides)
    prompt = kwargs.pop("prompt", "p")
    return invoke_claude_headless(prompt, **kwargs)


def test_build_context_prompt_minimal_has_no_failure_section():
    prompt = build_context_prompt("corriger le bug X", _project(), "candidate/abc123")
    assert "corriger le bug X" in prompt
    assert "C:/proj" in prompt
    assert "candidate/abc123" in prompt
    assert "échec" not in prompt.lower()


def test_build_context_prompt_includes_previous_failure():
    failure = TestResult(
        task_id="t1",
        status="VERIFIED_FAIL",
        total=2,
        passed=1,
        failed=1,
        raw_output="AssertionError: 2 != 1",
        test_cases=[
            TestCaseResult(name="test_value", outcome=TestCaseOutcome.FAILED),
            TestCaseResult(name="test_other", outcome=TestCaseOutcome.PASSED),
        ],
    )
    prompt = build_context_prompt("corriger le bug X", _project(), "candidate/abc123", previous_failure=failure)
    assert "test_value" in prompt
    assert "test_other" not in prompt  # seuls les tests en échec sont listés
    assert "AssertionError: 2 != 1" in prompt


# --- invocation : argv exact ---------------------------------------------

def test_invoke_claude_headless_builds_exact_argv_list(monkeypatch, exe_and_settings):
    exe, settings = exe_and_settings
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout='{"type":"result","is_error":false,"result":"ok"}', stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    prompt = "ligne 1\nligne 2 : %USERNAME% & | > \"x\""
    invoke_claude_headless(
        prompt, cwd="/tmp/proj", model="claude-sonnet-5", timeout_seconds=123, executable=exe, settings_path=settings
    )

    assert captured["argv"] == [
        exe, "-p", prompt,
        "--settings", settings,
        "--setting-sources", "",
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--model", "claude-sonnet-5",
    ]
    kwargs = captured["kwargs"]
    assert kwargs["shell"] is False
    assert kwargs["cwd"] == "/tmp/proj"
    assert kwargs["timeout"] == 123
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert kwargs["encoding"] == "utf-8"


def test_invoke_never_uses_bare_claude_or_cmd_wrapper(monkeypatch, exe_and_settings):
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv0"] = argv[0]
        return subprocess.CompletedProcess(argv, 0, stdout='{"type":"result","is_error":false}', stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    _invoke(exe_and_settings)
    assert captured["argv0"] == exe_and_settings[0]
    assert captured["argv0"] != "claude" and not captured["argv0"].lower().endswith(".cmd")


# --- invocation : panne d'infrastructure vs résultat métier ---------------

@pytest.mark.parametrize("exc", [FileNotFoundError("introuvable"), PermissionError("refusé"), OSError("autre")])
def test_launch_failure_raises_infrastructure_error_not_a_result(monkeypatch, exe_and_settings, exc):
    def fake_run(argv, **kwargs):
        raise exc

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    with pytest.raises(InvocationInfrastructureError) as info:
        _invoke(exe_and_settings)
    assert type(exc).__name__ in str(info.value)


def test_relative_executable_is_infrastructure_error_without_launching(monkeypatch, exe_and_settings):
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    with pytest.raises(InvocationInfrastructureError):
        _invoke(exe_and_settings, executable="claude")


def test_missing_settings_file_is_infrastructure_error_without_launching(monkeypatch, exe_and_settings, tmp_path):
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    with pytest.raises(InvocationInfrastructureError):
        _invoke(exe_and_settings, settings_path=str(tmp_path / "absent.json"))
    with pytest.raises(InvocationInfrastructureError):
        _invoke(exe_and_settings, settings_path="relatif/settings.json")


def test_invoke_claude_headless_success(monkeypatch, exe_and_settings):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"type":"result","is_error":false,"result":"ok","session_id":"s1","num_turns":4}',
            stderr="",
        )

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = _invoke(exe_and_settings)

    assert result.invocation_status == HeadlessInvocationStatus.VERIFIED
    assert result.is_error is False
    assert result.session_id == "s1"
    assert result.num_turns == 4
    assert result.result_text == "ok"


def _json_result(is_error, result):
    import json

    return json.dumps({"type": "result", "is_error": is_error, "result": result})


# Sortie RÉELLE observée (2026-09-05 et 2026-09-23), octet pour octet.
REAL_AUTH_FAILURE_STDOUT = _json_result(True, "Failed to authenticate: OAuth session expired and could not be refreshed")


def test_real_auth_failure_output_raises_infrastructure_error(monkeypatch, exe_and_settings):
    monkeypatch.setattr(
        claude_headless.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout=REAL_AUTH_FAILURE_STDOUT, stderr=""),
    )
    with pytest.raises(InvocationInfrastructureError) as info:
        _invoke(exe_and_settings)
    assert "échec d'authentification" in str(info.value)
    assert "claude auth login" in str(info.value)
    assert "OAuth session expired and could not be refreshed" in str(info.value)


@pytest.mark.parametrize(
    "message",
    [
        "Tests en échec : SyntaxError: invalid syntax (calc.py, line 2)",
        "Reached max turns (4)",
        "API Error: 500 Internal Server Error",
        # Chaînes d'auth présentes dans le binaire mais jamais observées en
        # sortie -p : volontairement hors du motif (strict, non deviné).
        "Failed to authenticate through the broker: timeout",
        "Failed to authenticate",
        "Failed to authenticate: OAuth session expired",  # littéral incomplet
    ],
)
def test_other_is_error_stays_a_normal_result(monkeypatch, exe_and_settings, message):
    """Le process a TOURNÉ, is_error générique : comportement inchangé, pas
    d'exception — seul le littéral exact AUTH_FAILURE_LITERAL bascule."""
    monkeypatch.setattr(
        claude_headless.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 1, stdout=_json_result(True, message), stderr=""),
    )
    result = _invoke(exe_and_settings)

    assert result.invocation_status == HeadlessInvocationStatus.INVOCATION_FAILED
    assert result.is_error is True
    assert result.error_detail == message


def test_auth_literal_without_is_error_is_not_an_infrastructure_error(monkeypatch, exe_and_settings):
    """Le modèle peut CITER le message dans une réponse réussie : sans
    is_error=true, ce n'est pas une panne."""
    literal = claude_headless.AUTH_FAILURE_LITERAL
    monkeypatch.setattr(
        claude_headless.subprocess, "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, stdout=_json_result(False, f"J'ai vu ce message : {literal}"), stderr=""
        ),
    )
    assert _invoke(exe_and_settings).invocation_status == HeadlessInvocationStatus.VERIFIED


def test_invoke_claude_headless_timeout_no_exception_propagates(monkeypatch, exe_and_settings):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs["timeout"], output="partiel", stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = _invoke(exe_and_settings, timeout_seconds=99)

    assert result.invocation_status == HeadlessInvocationStatus.TIMED_OUT
    assert "99s" in result.error_detail


def test_invoke_claude_headless_malformed_json_is_unavailable(monkeypatch, exe_and_settings):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="ceci n'est pas du JSON", stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = _invoke(exe_and_settings)

    assert result.invocation_status == HeadlessInvocationStatus.UNAVAILABLE
    assert result.raw_stdout == "ceci n'est pas du JSON"


def test_invoke_claude_headless_json_missing_required_keys_is_unavailable(monkeypatch, exe_and_settings):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout='{"foo": "bar"}', stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    assert _invoke(exe_and_settings).invocation_status == HeadlessInvocationStatus.UNAVAILABLE


def test_not_executed_is_never_produced_anymore(monkeypatch, exe_and_settings):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("x")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    with pytest.raises(InvocationInfrastructureError):
        _invoke(exe_and_settings)


# --- validation de l'exécutable au démarrage (mockée ; le réel est couvert
# par tests/integration/test_real_claude_headless.py) ----------------------

@pytest.fixture
def fake_exe(tmp_path):
    exe = tmp_path / "claude.exe"
    exe.write_bytes(b"")
    return exe


def _version_ok(monkeypatch, calls=None):
    def fake_run(argv, **kwargs):
        if calls is not None:
            calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, stdout="2.1.235 (Claude Code)\n", stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)


def test_validate_runs_version_and_returns_it(monkeypatch, fake_exe):
    calls = []
    _version_ok(monkeypatch, calls)

    result = validate_claude_executable(str(fake_exe))

    assert result.version == "2.1.235 (Claude Code)"
    assert result.path == str(fake_exe)
    assert calls[0][0] == [str(fake_exe), "--version"]
    assert calls[0][1]["shell"] is False


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_validate_missing_key_names_config_key(monkeypatch, configured):
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    with pytest.raises(ClaudeExecutableConfigError) as info:
        validate_claude_executable(configured)
    assert "claude_headless.executable" in str(info.value)


def test_validate_rejects_bare_claude_and_relative_paths(monkeypatch):
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    for configured in ("claude", "bin/claude.exe"):
        with pytest.raises(ClaudeExecutableConfigError) as info:
            validate_claude_executable(configured)
        assert "claude_headless.executable" in str(info.value)


def test_validate_rejects_cmd_wrapper(monkeypatch, tmp_path):
    cmd = tmp_path / "claude.cmd"
    cmd.write_text("@echo off", encoding="utf-8")
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    with pytest.raises(ClaudeExecutableConfigError) as info:
        validate_claude_executable(str(cmd))
    assert ".exe" in str(info.value)


def test_validate_rejects_nonexistent_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_headless.subprocess, "run", lambda *a, **k: pytest.fail("ne doit jamais lancer"))
    with pytest.raises(ClaudeExecutableConfigError):
        validate_claude_executable(str(tmp_path / "absent" / "claude.exe"))


@pytest.mark.parametrize(
    "outcome",
    [
        subprocess.CompletedProcess(["x"], 1, stdout="", stderr="boom"),
        subprocess.CompletedProcess(["x"], 0, stdout="   ", stderr=""),
        OSError("lancement impossible"),
        subprocess.TimeoutExpired(cmd="x", timeout=60),
    ],
)
def test_validate_fails_when_version_does_not_really_answer(monkeypatch, fake_exe, outcome):
    def fake_run(argv, **kwargs):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    with pytest.raises(ClaudeExecutableConfigError) as info:
        validate_claude_executable(str(fake_exe))
    assert "claude_headless.executable" in str(info.value)
