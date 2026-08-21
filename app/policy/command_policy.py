"""CommandPolicy : source UNIQUE de vérité pour la classification à 3
niveaux des commandes/accès fichiers.

- AUTO_ALLOWED       : absent de deny/ask, rien à faire par défaut.
- CONDITIONAL        : va dans permissions.ask de .claude/settings.json —
                       Claude Code doit demander confirmation.
- BLOCKED_OR_HUMAN_REQUIRED : bloqué. Deux représentations possibles
                       pour une même règle :
                       * settings_pattern : exprimable comme préfixe
                         Bash/Read/Write/Edit simple → va dans
                         permissions.deny de settings.json.
                       * hook_regex : nécessite une regex (le hook
                         Python la vérifie lui-même) parce que
                         settings.json ne sait faire que du préfixe.

.claude/settings.json ET .claude/hooks/pre_tool_use.py sont TOUS LES
DEUX dérivés de POLICY ci-dessous via generate_settings_permissions() et
generate_hook_patterns(). Aucun des deux fichiers ne doit contenir de
pattern écrit à la main en dehors de ce module — sinon rien n'empêche
les deux surfaces de diverger avec le temps. tests/unit/test_command_policy_sync.py
vérifie qu'il n'y a effectivement aucune divergence.

Cas à part, documenté ici mais implémenté dans pre_tool_use.py :
la protection de settings.json contre l'ÉCRITURE (pas la lecture) a
besoin d'une logique par segment de commande (voir
`_targets_settings_json_for_write` dans pre_tool_use.py) qui ne se
réduit pas à une simple regex de POLICY — trop de faux positifs réels
constatés autrement (voir historique du projet). Sa raison textuelle
est tout de même déclarée ici pour que le test de cohérence puisse
vérifier que le message affiché correspond bien à celui utilisé par le
hook.
"""
import re
from dataclasses import dataclass
from enum import Enum


class PolicyLevel(str, Enum):
    AUTO_ALLOWED = "AUTO_ALLOWED"
    CONDITIONAL = "CONDITIONAL"
    BLOCKED_OR_HUMAN_REQUIRED = "BLOCKED_OR_HUMAN_REQUIRED"


@dataclass(frozen=True)
class PolicyRule:
    id: str
    level: PolicyLevel
    reason: str
    settings_pattern: str | None = None
    hook_regex: str | None = None


# Ordre préservé intentionnellement : c'est celui qui apparaît dans
# .claude/settings.json (deny puis ask), pour un diff lisible.
POLICY: list[PolicyRule] = [
    # --- BLOCKED_OR_HUMAN_REQUIRED, préfixe simple -> permissions.deny ---
    PolicyRule("rm_rf", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "suppression récursive forcée", settings_pattern="Bash(rm -rf:*)"),
    PolicyRule("remove_item_recurse_force", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "suppression récursive forcée (PowerShell)", settings_pattern="Bash(Remove-Item -Recurse -Force:*)"),
    PolicyRule("ri_recurse_force", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "alias PowerShell de Remove-Item -Recurse -Force", settings_pattern="Bash(ri -Recurse -Force:*)"),
    PolicyRule("git_push_force", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "réécriture d'historique distant", settings_pattern="Bash(git push --force:*)"),
    PolicyRule("git_push_force_with_lease", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "réécriture d'historique distant", settings_pattern="Bash(git push --force-with-lease:*)"),
    PolicyRule("reg", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "modification du registre Windows", settings_pattern="Bash(reg:*)"),
    PolicyRule("reg_exe", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "modification du registre Windows", settings_pattern="Bash(reg.exe:*)"),
    PolicyRule("sudo", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "élévation de privilèges", settings_pattern="Bash(sudo:*)"),
    PolicyRule("runas", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "élévation de privilèges (Windows)", settings_pattern="Bash(runas:*)"),
    PolicyRule("read_env", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Read(./.env)"),
    PolicyRule("read_env_star", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Read(./.env.*)"),
    PolicyRule("read_secrets", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Read(./secrets/**)"),
    PolicyRule("edit_env", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Edit(./.env)"),
    PolicyRule("edit_env_star", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Edit(./.env.*)"),
    PolicyRule("edit_secrets", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Edit(./secrets/**)"),
    PolicyRule("write_env", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Write(./.env)"),
    PolicyRule("write_env_star", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Write(./.env.*)"),
    PolicyRule("write_secrets", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "secret potentiel", settings_pattern="Write(./secrets/**)"),

    # --- BLOCKED_OR_HUMAN_REQUIRED, regex -> hook Python uniquement ---
    PolicyRule("diskpart", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "diskpart peut reformater/repartitionner un disque", hook_regex=r"\bdiskpart\b"),
    PolicyRule("disk_format", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "formatage de disque", hook_regex=r"\bformat\s+[a-z]:"),
    PolicyRule(
        "delete_drive_root",
        PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED,
        "suppression récursive à la racine d'un lecteur",
        hook_regex=r"(del|erase|rd|rmdir)\s+(/s\s+/q|/q\s+/s)\s+[a-z]:\\?\s*$",
    ),
    PolicyRule(
        "remove_item_drive_root",
        PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED,
        "Remove-Item récursif ciblant la racine d'un lecteur",
        hook_regex=r"remove-item[^\n]*-recurse[^\n]*-force[^\n]*[a-z]:\\\s*$",
    ),
    PolicyRule(
        "admin_account",
        PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED,
        "création/élévation de compte administrateur",
        hook_regex=r"\bnet\s+(user|localgroup)\b[^\n]*\b(add|administrators)\b",
    ),
    # Déclarée ici pour documentation/cohérence du message, mais PAS incluse
    # dans generate_hook_patterns() : logique dédiée par segment dans le
    # hook (voir docstring du module).
    PolicyRule("settings_json_write", PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED, "modification des règles de permission HERBERT elles-mêmes"),

    # --- CONDITIONAL -> permissions.ask ---
    PolicyRule("git_push", PolicyLevel.CONDITIONAL, "publication de commits", settings_pattern="Bash(git push:*)"),
    PolicyRule("pip_install", PolicyLevel.CONDITIONAL, "installation de dépendance", settings_pattern="Bash(pip install:*)"),
    PolicyRule("npm_install", PolicyLevel.CONDITIONAL, "installation de dépendance", settings_pattern="Bash(npm install:*)"),
    PolicyRule("curl", PolicyLevel.CONDITIONAL, "accès réseau sortant", settings_pattern="Bash(curl:*)"),
    PolicyRule("invoke_webrequest", PolicyLevel.CONDITIONAL, "accès réseau sortant (PowerShell)", settings_pattern="Bash(Invoke-WebRequest:*)"),
]


def generate_settings_permissions() -> dict:
    """{"deny": [...], "ask": [...]} pour la section `permissions` de
    .claude/settings.json, dans l'ordre déclaré dans POLICY."""
    deny = [r.settings_pattern for r in POLICY if r.level is PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED and r.settings_pattern]
    ask = [r.settings_pattern for r in POLICY if r.level is PolicyLevel.CONDITIONAL and r.settings_pattern]
    return {"deny": deny, "ask": ask}


def generate_hook_patterns() -> list[tuple[re.Pattern, str]]:
    """(regex compilée, raison) pour les règles BLOCKED non exprimables en
    préfixe simple — c'est la liste que pre_tool_use.py doit utiliser."""
    return [
        (re.compile(r.hook_regex, re.IGNORECASE), r.reason)
        for r in POLICY
        if r.level is PolicyLevel.BLOCKED_OR_HUMAN_REQUIRED and r.hook_regex
    ]


def get_rule(rule_id: str) -> PolicyRule:
    for r in POLICY:
        if r.id == rule_id:
            return r
    raise KeyError(f"règle de policy inconnue: {rule_id}")
