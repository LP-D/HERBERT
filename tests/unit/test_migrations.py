from app.database.migrate import apply_migrations, current_schema_version
from app.database.connection import get_connection

from conftest import MIGRATIONS_DIR

EXPECTED_TABLES = {
    "projects",
    "tasks",
    "state_transitions",
    "commands",
    "change_proofs",
    "audit_log",
}


def test_migration_applies_without_error(tmp_path):
    conn = get_connection(tmp_path / "migration_test.db")
    applied = apply_migrations(conn, MIGRATIONS_DIR)
    assert applied == [1]
    conn.close()


def test_schema_has_expected_tables(db_conn):
    rows = db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    table_names = {row["name"] for row in rows}
    assert EXPECTED_TABLES.issubset(table_names)


def test_schema_version_tracked(db_conn):
    assert current_schema_version(db_conn) == 1


def test_migration_is_idempotent(tmp_path):
    conn = get_connection(tmp_path / "idempotent_test.db")
    first = apply_migrations(conn, MIGRATIONS_DIR)
    second = apply_migrations(conn, MIGRATIONS_DIR)
    assert first == [1]
    assert second == []
    conn.close()


def test_foreign_keys_enabled(db_conn):
    row = db_conn.execute("PRAGMA foreign_keys").fetchone()
    assert row[0] == 1
