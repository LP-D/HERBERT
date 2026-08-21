import json

from app.database.connection import get_connection


def _read_jsonl_events(logs_dir):
    events = []
    for path in sorted(logs_dir.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
    return events


def test_pre_tool_use_blocks_dangerous_command(pre_tool_use_module, isolated_repo_root, monkeypatch):
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "task_id": None,
        "tool_input": {"command": "diskpart /s wipe.txt"},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)

    assert exit_code == 2
    assert "bloquée" in message
    assert "diskpart" in message

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT * FROM commands WHERE command LIKE '%diskpart%' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["decision"] == "DENY"

    # le hook doit journaliser la même décision dans les logs/*.jsonl, pas
    # seulement en SQLite
    jsonl_events = _read_jsonl_events(isolated_repo_root / "logs")
    matching = [e for e in jsonl_events if "diskpart" in e.get("details", {}).get("command", "")]
    assert matching, "aucune trace JSONL trouvée pour la commande bloquée"
    assert matching[-1]["status"] == "BLOCKED"
    assert matching[-1]["details"]["decision"] == "DENY"


def test_pre_tool_use_blocks_self_permission_edit(pre_tool_use_module, isolated_repo_root, monkeypatch):
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "echo '{}' > .claude/settings.json"},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2
    assert "permission" in message.lower() or "settings.json" in message.lower() or "règles" in message.lower()


def test_pre_tool_use_allows_reading_settings_json(pre_tool_use_module, isolated_repo_root, monkeypatch):
    """Faux positif réel constaté en session live : lire settings.json
    (python -m json.tool, cat, ...) ne doit PAS être bloqué, seul l'écrire
    doit l'être."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m json.tool .claude/settings.json"},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, f"la lecture de settings.json ne doit pas être bloquée: {message}"


def test_pre_tool_use_allows_unrelated_arrow_text_near_settings_json_read(
    pre_tool_use_module, isolated_repo_root, monkeypatch
):
    """Deuxième faux positif réel constaté en session live : un '>' littéral
    dans du texte de diagnostic ("->") déclenchait le blocage dès que
    settings.json apparaissait ailleurs (même en lecture) dans la même
    commande multi-segments."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {
            "command": 'echo "hash identique -> aucune modification" && git diff .claude/settings.json'
        },
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, f"ne doit pas être bloqué: {message}"


def test_pre_tool_use_still_blocks_real_write_in_multi_segment_command(
    pre_tool_use_module, isolated_repo_root, monkeypatch
):
    """Le découpage par segment ne doit pas affaiblir la protection réelle :
    une écriture effective sur settings.json dans un des segments doit
    toujours être bloquée."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": 'echo "diagnostic" && echo "{}" >> .claude/settings.json'},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2, "une écriture réelle sur settings.json doit rester bloquée"


def test_pre_tool_use_allows_safe_command(pre_tool_use_module, isolated_repo_root, monkeypatch):
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "git status"},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0
    assert message == ""

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT * FROM commands WHERE command = 'git status' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["decision"] == "ALLOW"


def test_pre_tool_use_logs_before_deciding_even_on_deny(pre_tool_use_module, isolated_repo_root, monkeypatch):
    """Vérifie que la commande DENY est bien présente en base après l'appel
    (journalisation avant décision, jamais un blocage silencieux)."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {"tool_name": "Bash", "tool_input": {"command": "format c:"}}
    exit_code, _ = pre_tool_use_module.handle_event(event)
    assert exit_code == 2

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    count = conn.execute("SELECT COUNT(*) AS c FROM commands").fetchone()["c"]
    conn.close()
    assert count == 1
