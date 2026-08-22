import argparse

from app.cli import main as cli_main
from app.database.connection import get_connection
from app.database.migrate import current_schema_version

from tests.unit.test_migrations import EXPECTED_VERSIONS


def test_engine_init_creates_structure_and_applies_migrations(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    args = argparse.Namespace()
    exit_code = cli_main.cmd_init(args)
    assert exit_code == 0

    for rel in [
        "app/cli",
        "app/models",
        "app/state_machine",
        "app/database",
        "trusted",
        ".claude/hooks",
        "tests/unit",
        "logs",
    ]:
        assert (isolated_repo_root / rel).exists(), f"{rel} devrait avoir été créé par engine init"

    config_path = isolated_repo_root / "config" / "system.yaml"
    assert config_path.exists()

    db_path = isolated_repo_root / "data" / "herbert.db"
    assert db_path.exists(), "engine init doit réellement créer la base SQLite via les migrations"

    conn = get_connection(db_path)
    assert current_schema_version(conn) == max(EXPECTED_VERSIONS)
    tables = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert {
        "projects", "tasks", "state_transitions", "commands", "change_proofs", "audit_log", "test_results",
        "promotions",
    }.issubset(tables)
    conn.close()


def test_engine_init_is_idempotent(isolated_repo_root, monkeypatch):
    monkeypatch.setattr(cli_main, "REPO_ROOT", isolated_repo_root)

    args = argparse.Namespace()
    assert cli_main.cmd_init(args) == 0
    assert cli_main.cmd_init(args) == 0  # ne doit pas échouer si relancé
