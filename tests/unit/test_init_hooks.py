"""app/hooks_deploy.py + `engine init-hooks` : fichier settings HERBERT par
projet cible, sous <herbert>/data/target_settings/<project_id>/, jamais dans
le dépôt du projet cible."""
import argparse
import hashlib
import json
import sys

import pytest

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import insert_project
from app.hooks_deploy import (
    HOOK_MATCHER,
    HooksDeployError,
    build_target_settings,
    deploy_target_settings,
    sha256_file,
    target_settings_path,
)
from app.models import Project
from app.policy.command_policy import generate_settings_permissions
from conftest import REPO_ROOT


@pytest.fixture
def project_dir(tmp_path):
    d = tmp_path / "cible"
    d.mkdir()
    (d / "README.md").write_text("projet cible\n", encoding="utf-8")
    return d


@pytest.fixture
def herbert_root(tmp_path):
    d = tmp_path / "herbert"
    d.mkdir()
    return d


@pytest.fixture
def logs(tmp_path):
    return tmp_path / "logs"


def _project(project_dir):
    return Project(name="cible", path=str(project_dir))


def _records(logs_dir):
    out = []
    for f in sorted(logs_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            if r["component"] == "hooks_deploy":
                out.append(r)
    return out


def _tree(d):
    return sorted(str(p.relative_to(d)) for p in d.rglob("*"))


# --- contenu généré --------------------------------------------------------

def test_settings_contain_full_permissions_from_single_source_of_truth(herbert_root):
    settings = build_target_settings(herbert_root)
    assert settings["permissions"] == generate_settings_permissions()
    assert set(settings) == {"permissions", "hooks"}  # rien d'autre, aucune métadonnée


def test_hook_commands_are_absolute_forward_slash_and_use_sys_executable(herbert_root):
    settings = build_target_settings(herbert_root)
    py = sys.executable.replace("\\", "/")
    root = str(herbert_root.resolve()).replace("\\", "/")
    for event, script in (("PreToolUse", "pre_tool_use.py"), ("PostToolUse", "post_tool_use.py")):
        [entry] = settings["hooks"][event]
        [hook] = entry["hooks"]
        assert entry["matcher"] == HOOK_MATCHER
        assert hook["type"] == "command"
        assert hook["command"] == f'"{py}" "{root}/.claude/hooks/{script}"'
        assert "\\" not in hook["command"]
        assert "$CLAUDE_PROJECT_DIR" not in hook["command"]
        assert not hook["command"].startswith("python ")  # jamais `python` nu


def test_hook_matcher_in_sync_with_herbert_own_settings():
    herbert_settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    for event in ("PreToolUse", "PostToolUse"):
        assert [e["matcher"] for e in herbert_settings["hooks"][event]] == [HOOK_MATCHER]


def test_hook_scripts_referenced_for_real_herbert_root_exist():
    from pathlib import Path

    settings = build_target_settings(REPO_ROOT)
    for event in ("PreToolUse", "PostToolUse"):
        command = settings["hooks"][event][0]["hooks"][0]["command"]
        _, interpreter, _, script, _ = command.split('"')
        assert Path(interpreter).is_file()
        assert Path(script).is_file()
        assert Path(script).parent == (REPO_ROOT / ".claude" / "hooks").resolve()


# --- déploiement : emplacement, idempotence, SHA-256, JSONL ---------------

def test_deploy_absent_creates_file_under_herbert_data_never_in_target(herbert_root, project_dir, logs):
    project = _project(project_dir)
    before = _tree(project_dir)

    result = deploy_target_settings(project, herbert_root, logs)

    expected = herbert_root / "data" / "target_settings" / project.id / "settings.json"
    assert result.status == "created"
    assert result.path == expected == target_settings_path(herbert_root, project.id)
    assert expected.is_file()
    assert json.loads(expected.read_text(encoding="utf-8")) == build_target_settings(herbert_root)
    assert _tree(project_dir) == before  # AUCUN fichier créé dans le dépôt cible
    assert result.previous_sha256 is None


def test_deploy_sha256_matches_file_bytes_and_is_logged(herbert_root, project_dir, logs):
    project = _project(project_dir)
    result = deploy_target_settings(project, herbert_root, logs)

    assert result.sha256 == hashlib.sha256(result.path.read_bytes()).hexdigest() == sha256_file(result.path)
    [record] = _records(logs)
    assert record["task_id"] is None
    assert record["details"]["sha256"] == result.sha256
    assert record["details"]["status"] == "created"
    assert record["details"]["project_id"] == project.id
    assert record["details"]["path"] == str(result.path)


def test_deploy_identical_is_noop(herbert_root, project_dir, logs):
    project = _project(project_dir)
    first = deploy_target_settings(project, herbert_root, logs)
    mtime = first.path.stat().st_mtime_ns

    second = deploy_target_settings(project, herbert_root, logs)

    assert second.status == "unchanged"
    assert second.sha256 == second.previous_sha256 == first.sha256
    assert first.path.stat().st_mtime_ns == mtime  # pas réécrit
    assert [r["details"]["status"] for r in _records(logs)] == ["created", "unchanged"]


def test_deploy_different_is_rewritten_without_confirmation(herbert_root, project_dir, logs):
    project = _project(project_dir)
    first = deploy_target_settings(project, herbert_root, logs)
    first.path.write_text('{"disableAllHooks": true}', encoding="utf-8")
    tampered_sha = sha256_file(first.path)

    result = deploy_target_settings(project, herbert_root, logs)

    assert result.status == "rewritten"
    assert result.previous_sha256 == tampered_sha
    assert result.sha256 == first.sha256
    assert json.loads(result.path.read_text(encoding="utf-8")) == build_target_settings(herbert_root)
    assert not list(result.path.parent.glob("*.tmp"))  # écriture atomique, pas de reste


def test_deploy_refuses_when_settings_would_land_inside_project(project_dir, logs):
    """Ex. projet enregistré sur le dépôt HERBERT lui-même."""
    with pytest.raises(HooksDeployError):
        deploy_target_settings(_project(project_dir), project_dir, logs)
    assert not (project_dir / "data").exists()


def test_deploy_refuses_missing_project_dir(herbert_root, tmp_path, logs):
    with pytest.raises(HooksDeployError):
        deploy_target_settings(Project(name="x", path=str(tmp_path / "absent")), herbert_root, logs)


# --- CLI `engine init-hooks` ------------------------------------------------

@pytest.fixture
def cli_env(isolated_repo_root, monkeypatch, tmp_path):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    apply_migrations(conn, isolated_repo_root / "migrations")
    target = tmp_path / "projet_cible"
    target.mkdir()
    project = Project(name="motus-test", path=str(target))
    insert_project(conn, project)
    conn.close()
    return isolated_repo_root, target, project


def test_cli_registered_project_deploys_and_reports(cli_env, capsys):
    root, target, project = cli_env
    assert cli_main.cmd_init_hooks(argparse.Namespace(path=str(target) + "/")) == 0
    out = capsys.readouterr().out
    assert "[VERIFIED] settings HERBERT créé" in out
    assert (root / "data" / "target_settings" / project.id / "settings.json").is_file()
    assert not (target / ".claude").exists()

    assert cli_main.cmd_init_hooks(argparse.Namespace(path=str(target))) == 0
    assert "inchangé" in capsys.readouterr().out


def test_cli_unregistered_path_fails(cli_env, tmp_path, capsys):
    other = tmp_path / "non_enregistre"
    other.mkdir()
    assert cli_main.cmd_init_hooks(argparse.Namespace(path=str(other))) == 1
    assert "aucun projet enregistré" in capsys.readouterr().out


def test_cli_parser_exposes_command():
    args = cli_main.build_parser().parse_args(["init-hooks", "C:/x"])
    assert args.func is cli_main.cmd_init_hooks and args.path == "C:/x"
