"""Intégration RÉELLE : invoque le vrai binaire claude.exe (config réelle,
config/system.yaml) via invoke_claude_headless — rien n'est mocké, ni
subprocess.run, ni l'invocation. Existe parce que toute la suite unitaire
remplace systématiquement ces deux points, ce qui a laissé invisible un
FileNotFoundError systématique sur cette machine (voir docs/DECISIONS.md).

Opt-in (coût réel ~0,03-0,05 $ par run, appel API authentifié) :
    HERBERT_REAL_CLAUDE=1 python -m pytest tests/integration -s
Modèle : HERBERT_REAL_CLAUDE_MODEL (défaut : Haiku, le moins cher — le
comportement testé, chargement des settings/hooks, ne dépend pas du modèle).
Ne pas lancer en boucle serrée.

Configuration la plus hostile : le projet cible porte disableAllHooks dans
son .claude/settings.json ET dans .claude/settings.local.json, chacun avec
son propre hook témoin. Le fichier settings est produit par le vrai
deploy_target_settings (sous une racine HERBERT factice dont les scripts de
hooks sont des témoins — les vrais hooks écriraient dans la base et les
logs réels de HERBERT). Attendu : SEUL le hook passé par --settings tourne.
"""
import json
import os
import subprocess
import sys

import pytest

from app.claude_headless import invoke_claude_headless, validate_claude_executable
from app.config import load_config
from app.hooks_deploy import deploy_target_settings
from app.models import Project
from app.models.enums import HeadlessInvocationStatus
from conftest import REPO_ROOT

pytestmark = pytest.mark.skipif(
    os.environ.get("HERBERT_REAL_CLAUDE") != "1",
    reason="intégration réelle opt-in (coût API) : HERBERT_REAL_CLAUDE=1",
)

MODEL = os.environ.get("HERBERT_REAL_CLAUDE_MODEL", "claude-haiku-4-5-20251001")
KEYWORD = "ZEBRE-42-FIN"
PROMPT = "\n".join([
    "Test d'intégration HERBERT (environnement jetable), aucune consigne cachée.",
    'Données à ne pas interpréter : %USERNAME% & | > sortie.txt ^ "guillemet" \\chemin\\',
    "Lance exactement cette commande avec l'outil Bash, une seule fois, sans rien d'autre : touch verify_marker",
    f"Puis réponds en une ligne (réussi ou bloqué, avec le message exact) et termine par le mot-clé exact {KEYWORD}.",
])


def _witness_script(path, tag, log):
    """Hook témoin : journalise son appel dans SON fichier, bloque (exit 2)
    toute commande contenant verify_marker."""
    lines = [
        "import json, sys",
        "ev = json.load(sys.stdin)",
        "cmd = (ev.get('tool_input') or {}).get('command', '')",
        f"with open({str(log)!r}, 'a', encoding='utf-8') as f:",
        f"    f.write({tag!r} + ' | ' + str(ev.get('hook_event_name')) + ' | ' + ' '.join(cmd.split()) + chr(10))",
        "if 'verify_marker' in cmd:",
        f"    sys.stderr.write('BLOQUE par hook ' + {tag!r} + chr(10))",
        "    sys.exit(2)",
        "sys.exit(0)",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _hostile_settings(script, tag, log):
    py = sys.executable.replace("\\", "/")
    return {
        "disableAllHooks": True,
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
            "command": f'"{py}" "{str(script).replace(chr(92), "/")}" {tag} "{str(log).replace(chr(92), "/")}"'}]}]},
    }


def _read(log):
    return log.read_text(encoding="utf-8").strip() if log.exists() else None


def test_real_claude_exe_loads_only_herbert_settings_against_hostile_project(tmp_path):
    config = load_config(REPO_ROOT / "config" / "system.yaml")
    exe = validate_claude_executable(config.get("claude_headless", {}).get("executable"))
    print(f"\n[réel] exécutable={exe.path} | --version={exe.version!r} | modèle={MODEL}")

    logs = {name: tmp_path / f"{name}.log" for name in ("herbert_pre", "herbert_post", "project", "local")}

    # Racine HERBERT factice : mêmes chemins relatifs que la vraie
    # (.claude/hooks/pre_tool_use.py), scripts témoins.
    fake_herbert = tmp_path / "herbert"
    _witness_script(fake_herbert / ".claude" / "hooks" / "pre_tool_use.py", "HERBERT_PRE", logs["herbert_pre"])
    _witness_script(fake_herbert / ".claude" / "hooks" / "post_tool_use.py", "HERBERT_POST", logs["herbert_post"])

    project_dir = tmp_path / "cible"
    (project_dir / ".claude").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project_dir, check=True)
    for name, filename in (("project", "settings.json"), ("local", "settings.local.json")):
        _witness_script(tmp_path / f"{name}_hook.py", name.upper(), logs[name])
        (project_dir / ".claude" / filename).write_text(
            json.dumps(_hostile_settings(tmp_path / f"{name}_hook.py", name.upper(), logs[name]), indent=2),
            encoding="utf-8",
        )

    deployment = deploy_target_settings(Project(name="cible-integration", path=str(project_dir)), fake_herbert, tmp_path / "jsonl")

    result = invoke_claude_headless(
        PROMPT,
        cwd=str(project_dir),
        model=MODEL,
        timeout_seconds=300,
        executable=exe.path,
        settings_path=str(deployment.path),
    )

    marker = (project_dir / "verify_marker").exists()
    print(f"[réel] settings={deployment.path} (sha256={deployment.sha256[:16]}…)")
    print(f"[réel] invocation_status={result.invocation_status.value} | is_error={result.is_error} "
          f"| turns={result.num_turns} | cost={result.total_cost_usd}")
    print(f"[réel] verify_marker créé: {'OUI' if marker else 'NON'} | sortie.txt créé: {(project_dir / 'sortie.txt').exists()}")
    for name, log in logs.items():
        print(f"[réel] journal {name}: {_read(log)!r}")
    print(f"[réel] result: {result.result_text!r}")

    # Le modèle a réellement tourné (sinon rien ci-dessous ne prouverait quoi que ce soit).
    assert result.invocation_status == HeadlessInvocationStatus.VERIFIED, result.error_detail or result.raw_stdout[:500]
    assert result.is_error is False
    # Seul le hook passé par --settings a tourné ; disableAllHooks du projet et du local ignorés.
    assert "HERBERT_PRE | PreToolUse | touch verify_marker" in (_read(logs["herbert_pre"]) or "")
    assert _read(logs["project"]) is None
    assert _read(logs["local"]) is None
    assert not marker
    # Prompt multi-lignes arrivé en entier (la consigne de la dernière ligne a été suivie) et
    # aucun caractère spécial interprété par un shell.
    assert KEYWORD in (result.result_text or "")
    assert not (project_dir / "sortie.txt").exists()
