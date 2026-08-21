import re
import sqlite3
from pathlib import Path

MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_.*\.sql$")


def _migration_files(migrations_dir: Path) -> list[tuple[int, Path]]:
    files = []
    for path in migrations_dir.glob("*.sql"):
        match = MIGRATION_FILENAME_RE.match(path.name)
        if not match:
            continue
        version = int(match.group(1))
        files.append((version, path))
    return sorted(files, key=lambda item: item[0])


def apply_migrations(conn: sqlite3.Connection, migrations_dir: str | Path) -> list[int]:
    """Applique dans l'ordre les migrations dont le numéro dépasse la version
    courante (PRAGMA user_version). Retourne la liste des versions appliquées.
    """
    migrations_dir = Path(migrations_dir)
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]

    applied: list[int] = []
    for version, path in _migration_files(migrations_dir):
        if version <= current_version:
            continue
        sql = path.read_text(encoding="utf-8")
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
        applied.append(version)

    return applied


def current_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]
