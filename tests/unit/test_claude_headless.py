import subprocess

import pytest

from app import claude_headless
from app.claude_headless import build_context_prompt, invoke_claude_headless
from app.models import Project, TestCaseOutcome, TestCaseResult, TestResult
from app.models.enums import HeadlessInvocationStatus


def _project() -> Project:
    return Project(name="proj", path="C:/proj")


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


def test_invoke_claude_headless_builds_expected_argv(monkeypatch):
    captured = {}

    def fake_run(argv, cwd, capture_output, text, timeout):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout='{"type":"result","is_error":false,"result":"ok"}', stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    invoke_claude_headless("prompt X", cwd="/tmp/proj", model="claude-sonnet-5", timeout_seconds=123)

    argv = captured["argv"]
    assert argv[0] == "claude"
    assert "-p" in argv and "prompt X" in argv
    assert "--output-format" in argv and "json" in argv
    assert "--model" in argv and "claude-sonnet-5" in argv
    assert "--permission-mode" in argv and "bypassPermissions" in argv
    assert captured["cwd"] == "/tmp/proj"
    assert captured["timeout"] == 123


def test_invoke_claude_headless_success(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            argv, 0,
            stdout='{"type":"result","is_error":false,"result":"ok","session_id":"s1","num_turns":4}',
            stderr="",
        )

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10)

    assert result.invocation_status == HeadlessInvocationStatus.VERIFIED
    assert result.is_error is False
    assert result.session_id == "s1"
    assert result.num_turns == 4
    assert result.result_text == "ok"


def test_invoke_claude_headless_is_error_true_maps_to_invocation_failed(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(
            argv, 1,
            stdout='{"type":"result","is_error":true,"result":"Failed to authenticate: OAuth session expired"}',
            stderr="",
        )

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10)

    assert result.invocation_status == HeadlessInvocationStatus.INVOCATION_FAILED
    assert result.is_error is True
    assert "authenticate" in result.error_detail


def test_invoke_claude_headless_binary_not_found(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        raise FileNotFoundError("claude introuvable")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10)

    assert result.invocation_status == HeadlessInvocationStatus.NOT_EXECUTED
    assert "claude introuvable" in result.error_detail


def test_invoke_claude_headless_timeout_no_exception_propagates(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout, output="partiel", stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=99)

    assert result.invocation_status == HeadlessInvocationStatus.TIMED_OUT
    assert "99s" in result.error_detail


def test_invoke_claude_headless_malformed_json_is_unavailable(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout="ceci n'est pas du JSON", stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10)

    assert result.invocation_status == HeadlessInvocationStatus.UNAVAILABLE
    assert result.raw_stdout == "ceci n'est pas du JSON"


def test_invoke_claude_headless_json_missing_required_keys_is_unavailable(monkeypatch):
    def fake_run(argv, cwd, capture_output, text, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout='{"foo": "bar"}', stderr="")

    monkeypatch.setattr(claude_headless.subprocess, "run", fake_run)
    result = invoke_claude_headless("p", cwd="/tmp", model="claude-sonnet-5", timeout_seconds=10)

    assert result.invocation_status == HeadlessInvocationStatus.UNAVAILABLE
