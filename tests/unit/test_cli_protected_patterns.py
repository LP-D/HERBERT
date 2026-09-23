"""CLI `engine project protect add|list` (migration 0009) + règle
CommandPolicy `engine_project_protect` qui la réserve à un humain."""
import argparse
import json

import pytest

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import apply_migrations
from app.database.repository import get_project, insert_project
from app.models import Project
from app.models.enums import CommandDecision
from app.push_classifier import DEFAULT_BLOCKED_PATTERNS

PROTECT_TEXT = "project " + "protect"  # jamais écrit d'un bloc : voir la règle engine_project_protect


def _db_path(root):
    return root / "data" / "herbert.db"


@pytest.fixture
def cli_root(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)
    conn = get_connection(_db_path(isolated_repo_root))
    apply_migrations(conn, isolated_repo_root / "migrations")
    insert_project(conn, Project(name="motus", path="C:/motus"))
    conn.close()
    return isolated_repo_root


def _add(pattern, project="motus"):
    return cli_main.cmd_project_protect_add(argparse.Namespace(project=project, pattern=pattern))


def _stored(root, name="motus"):
    conn = get_connection(_db_path(root))
    try:
        row = conn.execute("SELECT id, extra_blocked_patterns FROM projects WHERE name = ?", (name,)).fetchone()
        return row["id"], row["extra_blocked_patterns"]
    finally:
        conn.close()


def _protect_log_records(root):
    records = []
    for f in (root / "logs").glob("*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record["component"] == "cli.project_protect":
                records.append(record)
    return records


def test_add_appends_pattern_and_logs_jsonl_with_null_task_id(cli_root, capsys):
    assert _add("data/") == 0
    assert "[VERIFIED] motif protégé ajouté" in capsys.readouterr().out

    project_id, raw = _stored(cli_root)
    assert json.loads(raw) == ["data/"]

    records = _protect_log_records(cli_root)
    assert len(records) == 1
    assert records[0]["task_id"] is None
    assert records[0]["details"] == {"project_id": project_id, "project_name": "motus", "pattern": "data/"}


def test_add_is_append_only_across_calls(cli_root):
    assert _add("data/") == 0
    assert _add("**/*.sqlite") == 0
    assert _add("data/") == 0  # doublon : no-op, pas d'erreur

    _, raw = _stored(cli_root)
    assert json.loads(raw) == ["data/", "**/*.sqlite"]
    assert len(_protect_log_records(cli_root)) == 2  # seuls les ajouts réels sont journalisés


def test_add_accepts_project_id_as_well_as_name(cli_root):
    project_id, _ = _stored(cli_root)
    assert _add("data/", project=project_id) == 0
    assert json.loads(_stored(cli_root)[1]) == ["data/"]


def test_add_normalizes_windows_separators_and_leading_dot_slash(cli_root):
    assert _add(".\\data\\raw\\") == 0
    assert json.loads(_stored(cli_root)[1]) == ["data/raw/"]


@pytest.mark.parametrize("pattern", ["", "   ", "../outside", "C:/Windows", "**/dir/file.txt", "./", "a/../../b"])
def test_add_rejects_invalid_patterns_without_touching_db(cli_root, pattern):
    assert _add(pattern) == 1
    assert _stored(cli_root)[1] is None
    assert _protect_log_records(cli_root) == []


def test_add_default_pattern_is_noop(cli_root, capsys):
    assert _add(".github/") == 0
    assert "déjà protégé par défaut" in capsys.readouterr().out
    assert _stored(cli_root)[1] is None


def test_add_unknown_project_fails(cli_root):
    assert _add("data/", project="inconnu") == 1


def test_add_refuses_to_overwrite_unreadable_value(cli_root):
    conn = get_connection(_db_path(cli_root))
    conn.execute("UPDATE projects SET extra_blocked_patterns = 'cassé' WHERE name = 'motus'")
    conn.commit()
    conn.close()

    assert _add("data/") == 1
    assert _stored(cli_root)[1] == "cassé"


def test_no_removal_subcommand_exists():
    parser = cli_main.build_parser()
    args = parser.parse_args(["project", "protect", "add", "motus", "data/"])
    assert args.func is cli_main.cmd_project_protect_add
    for forbidden in ("remove", "rm", "delete", "set", "clear"):
        with pytest.raises(SystemExit):
            parser.parse_args(["project", "protect", forbidden, "motus", "data/"])


def test_list_shows_defaults_then_project_additions(cli_root, capsys):
    _add("data/")
    capsys.readouterr()

    assert cli_main.cmd_project_protect_list(argparse.Namespace(project="motus")) == 0
    out = capsys.readouterr().out
    assert f"{len(DEFAULT_BLOCKED_PATTERNS)} par défaut, 1 ajouté(s)" in out
    assert "  [défaut] .github/" in out
    assert "  [projet] data/" in out


def test_list_reports_unreadable_value_as_failed(cli_root, capsys):
    conn = get_connection(_db_path(cli_root))
    conn.execute("UPDATE projects SET extra_blocked_patterns = 'cassé' WHERE name = 'motus'")
    conn.commit()
    conn.close()

    assert cli_main.cmd_project_protect_list(argparse.Namespace(project="motus")) == 1
    assert "MANUAL_REQUIRED" in capsys.readouterr().out


# --- CommandPolicy : réservé à un humain hors session agent --------------

@pytest.mark.parametrize(
    "command",
    [
        f"python engine.py {PROTECT_TEXT} add motus data/",
        f'python "C:\\Users\\Dufour\\herbert\\engine.py" {PROTECT_TEXT} add motus "**/*.db"',
        f"python -m app.cli.main {PROTECT_TEXT} list motus",
        f"cd C:/Users/Dufour/herbert && python engine.py project  protect add motus x/",
    ],
)
def test_hook_denies_project_protect_commands(pre_tool_use_module, tmp_path, command):
    decision, reason = pre_tool_use_module.evaluate_command(command, str(tmp_path))
    assert decision == CommandDecision.DENY
    assert "chemins protégés" in reason


@pytest.mark.parametrize(
    "command",
    ["python engine.py project list", "python -m pytest tests/unit/test_cli_protected_patterns.py -q",
     "python engine.py project add --name x --path C:/x"],
)
def test_hook_still_allows_other_project_commands(pre_tool_use_module, tmp_path, command):
    decision, _ = pre_tool_use_module.evaluate_command(command, str(tmp_path))
    assert decision == CommandDecision.ALLOW
