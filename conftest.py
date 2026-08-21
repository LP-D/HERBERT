import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))

from app.database.connection import get_connection  # noqa: E402
from app.database.migrate import apply_migrations  # noqa: E402

MIGRATIONS_DIR = REPO_ROOT / "migrations"


def load_module_from_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_repo_root(tmp_path):
    """Copie les migrations réelles dans un répertoire isolé, pour tester le
    CLI et les hooks sans jamais toucher aux données du vrai dépôt HERBERT."""
    shutil.copytree(MIGRATIONS_DIR, tmp_path / "migrations")
    return tmp_path


@pytest.fixture
def pre_tool_use_module():
    return load_module_from_path(REPO_ROOT / ".claude" / "hooks" / "pre_tool_use.py", "herbert_pre_tool_use_test")


@pytest.fixture
def post_tool_use_module():
    return load_module_from_path(REPO_ROOT / ".claude" / "hooks" / "post_tool_use.py", "herbert_post_tool_use_test")


@pytest.fixture
def db_conn(tmp_path):
    conn = get_connection(tmp_path / "test.db")
    apply_migrations(conn, MIGRATIONS_DIR)
    yield conn
    conn.close()


@pytest.fixture
def git_repo(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    def run(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=True,
        )

    run("init")
    run("config", "user.email", "test@herbert.local")
    run("config", "user.name", "HERBERT Test")

    (repo_path / "README.md").write_text("initial\n", encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "initial commit")

    return repo_path
