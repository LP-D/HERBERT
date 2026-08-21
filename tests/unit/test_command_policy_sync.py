"""Vérifie qu'il n'y a AUCUNE divergence possible entre .claude/settings.json,
.claude/hooks/pre_tool_use.py, et app/policy/command_policy.py — les trois
doivent être dérivés de la même source (POLICY)."""
import json

from conftest import REPO_ROOT, load_module_from_path

from app.policy.command_policy import generate_hook_patterns, generate_settings_permissions


def test_settings_json_permissions_match_generated_policy():
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    generated = generate_settings_permissions()

    assert settings["permissions"]["deny"] == generated["deny"]
    assert settings["permissions"]["ask"] == generated["ask"]


def test_hook_patterns_match_generated_policy():
    hook_module = load_module_from_path(
        REPO_ROOT / ".claude" / "hooks" / "pre_tool_use.py", "sync_check_pre_tool_use"
    )
    actual = [(p.pattern, reason) for p, reason in hook_module.ADDITIONAL_DANGEROUS_PATTERNS]
    generated = [(p.pattern, reason) for p, reason in generate_hook_patterns()]

    assert actual == generated


def test_pre_tool_use_matcher_covers_write_and_edit_for_path_policy():
    """PathPolicy ne sert à rien si le hook n'est jamais invoqué pour
    Write/Edit — vérifie que le matcher déclaré le couvre bien."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    pre_matchers = [h["matcher"] for h in settings["hooks"]["PreToolUse"]]
    assert any("Write" in m and "Edit" in m for m in pre_matchers), pre_matchers
