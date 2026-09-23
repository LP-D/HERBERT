"""Règle CommandPolicy `engine_init_hooks` : un agent contraint ne redéploie
jamais ses propres hooks. Le hook n'a aucune notion de "session headless" :
la règle s'applique à toute session où le hook HERBERT tourne — en headless,
c'est garanti puisque le fichier --settings HERBERT est la SEULE source
chargée (--setting-sources "")."""
import pytest

from app.models.enums import CommandDecision
from app.policy.command_policy import get_rule

INIT_HOOKS = "init" + "-hooks"  # jamais écrit d'un bloc : la règle bloquerait ce texte dans une commande


@pytest.mark.parametrize(
    "command",
    [
        f"python engine.py {INIT_HOOKS} C:/Users/Dufour/motus",
        f'python "C:\\Users\\Dufour\\herbert\\engine.py" {INIT_HOOKS} "C:\\Users\\Dufour\\motus"',
        f"python -m app.cli.main {INIT_HOOKS} .",
        f"cd C:/Users/Dufour/herbert && python engine.py {INIT_HOOKS} C:/x",
    ],
)
def test_hook_denies_init_hooks_commands(pre_tool_use_module, tmp_path, command):
    decision, reason = pre_tool_use_module.evaluate_command(command, str(tmp_path))
    assert decision == CommandDecision.DENY
    assert reason == get_rule("engine_init_hooks").reason


@pytest.mark.parametrize(
    "command",
    [
        "python -m pytest tests/unit/test_init_hooks.py -q",
        "python engine.py init",
        "python engine.py project list",
    ],
)
def test_hook_still_allows_neighbour_commands(pre_tool_use_module, tmp_path, command):
    decision, _ = pre_tool_use_module.evaluate_command(command, str(tmp_path))
    assert decision == CommandDecision.ALLOW


def test_rule_is_hook_only_not_in_settings_json():
    rule = get_rule("engine_init_hooks")
    assert rule.settings_pattern is None and rule.hook_regex
