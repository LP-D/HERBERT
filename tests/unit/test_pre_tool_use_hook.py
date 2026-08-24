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


def test_pre_tool_use_blocks_path_traversal_on_write(pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    """V0.2 : PathPolicy câblée dans le hook pour Write/Edit, via `cwd`."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    project_root = tmp_path / "some_project"
    project_root.mkdir()

    event = {
        "tool_name": "Write",
        "cwd": str(project_root),
        "tool_input": {"file_path": "../../etc/passwd", "content": "x"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2
    assert "PathPolicy" in message


def test_pre_tool_use_allows_valid_path_write(pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    project_root = tmp_path / "some_project"
    project_root.mkdir()

    event = {
        "tool_name": "Write",
        "cwd": str(project_root),
        "tool_input": {"file_path": "src/main.py", "content": "x"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, message


def test_pre_tool_use_blocks_reserved_windows_name_write(pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    project_root = tmp_path / "some_project"
    project_root.mkdir()

    event = {
        "tool_name": "Edit",
        "cwd": str(project_root),
        "tool_input": {"file_path": "logs/COM1.txt", "content": "x"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2
    assert "PathPolicy" in message


def test_pre_tool_use_blocks_edit_tool_writing_settings_json_directly(
    pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch
):
    """Faux sentiment de sécurité potentiel : étendre le matcher à Write|Edit
    pour PathPolicy ne doit pas laisser un Edit direct sur settings.json
    passer à travers (seul le texte des commandes Bash était vérifié avant)."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    project_root = tmp_path / "some_project"
    project_root.mkdir()
    (project_root / ".claude").mkdir()

    event = {
        "tool_name": "Edit",
        "cwd": str(project_root),
        "tool_input": {"file_path": str(project_root / ".claude" / "settings.json"), "content": "{}"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2
    assert "permission" in message.lower()


# --- CORRECTIF : détecteur anti-auto-modification trop large (faux positifs
# réels constatés — voir .claude/hooks/pre_tool_use.py, docstring de
# _targets_settings_json_for_write pour le détail des 3 cas) ---


def test_pre_tool_use_allows_fd_duplication_near_settings_json_read(
    pre_tool_use_module, isolated_repo_root, monkeypatch
):
    """Faux positif réel #3 : `2>&1` duplique le descripteur stderr vers
    stdout, ça n'écrit dans AUCUN fichier — ne doit jamais être confondu
    avec une redirection `>` réelle simplement parce qu'il contient le
    caractère '>', même quand settings.json est lu par la même commande."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "python -m json.tool .claude/settings.json 2>&1"},
    }

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, f"2>&1 ne doit jamais être traité comme une écriture: {message}"


def test_pre_tool_use_allows_writing_other_project_settings_json_via_heredoc(
    pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch
):
    """Faux positif réel #2 : écrire (via heredoc) le .claude/settings.json
    d'un AUTRE projet, dont le contenu JSON contient littéralement
    "Bash(rm -rf:*)" (un pattern deny normal généré par
    generate_settings_permissions()), ne doit pas être bloqué — ce n'est ni
    une écriture du fichier protégé de CE projet, ni un pattern dangereux
    en soi."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    other_project = tmp_path / "other_project"
    (other_project / ".claude").mkdir(parents=True)
    other_settings = other_project / ".claude" / "settings.json"

    command = (
        f"cat > {other_settings} <<'EOF'\n"
        "{\n"
        '  "permissions": {\n'
        '    "deny": ["Bash(rm -rf:*)"]\n'
        "  }\n"
        "}\n"
        "EOF"
    )
    event = {"tool_name": "Bash", "tool_input": {"command": command}}

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, f"écrire le settings.json d'un AUTRE projet ne doit pas être bloqué: {message}"


def test_pre_tool_use_allows_settings_json_mentioned_in_content_written_elsewhere(
    pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch
):
    """La chaîne "settings.json" apparaissant dans le CONTENU écrit vers un
    fichier qui n'est PAS settings.json ne doit jamais déclencher le
    blocage — seule la cible réelle de l'écriture compte."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    target = tmp_path / "notes.txt"

    command = f'echo "voir .claude/settings.json pour les permissions" > {target}'
    event = {"tool_name": "Bash", "tool_input": {"command": command}}

    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, message


def test_pre_tool_use_blocks_powershell_style_write_to_settings_json(
    pre_tool_use_module, isolated_repo_root, monkeypatch
):
    """Redesign : les commandes à chemin de fichier explicite (Set-Content,
    Remove-Item, ...) doivent aussi être analysées par cible réelle (valeur
    de -Path), pas seulement par présence du nom de la commande dans le
    texte de la commande."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "Set-Content -Path .claude/settings.json -Value '{}'"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 2, message


def test_pre_tool_use_allows_powershell_style_write_to_other_file(
    pre_tool_use_module, isolated_repo_root, monkeypatch
):
    """Même commande que ci-dessus mais ciblant un fichier différent : ne
    doit pas être bloquée par la protection dédiée à settings.json."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "Set-Content -Path other_settings.json -Value '{}'"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, message


def test_pre_tool_use_allows_write_tool_to_other_settings_json_nested_elsewhere(
    pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch
):
    """Même rigueur côté Write/Edit natif que côté Bash : un file_path
    résolu vers un fichier settings.json DIFFÉRENT de
    project_root/.claude/settings.json (ex. celui d'un sous-projet vendorisé)
    ne doit pas être bloqué par cette protection dédiée, même s'il se
    termine textuellement par ".claude/settings.json" (ancien bug : simple
    correspondance de suffixe, pas de résolution canonique)."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    project_root = tmp_path / "some_project"
    nested = project_root / "vendor" / "other-project" / ".claude"
    nested.mkdir(parents=True)
    other_settings = nested / "settings.json"

    event = {
        "tool_name": "Write",
        "cwd": str(project_root),
        "tool_input": {"file_path": str(other_settings), "content": "{}"},
    }
    exit_code, message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0, f"écrire un settings.json différent ne doit pas être bloqué: {message}"


def test_pre_tool_use_attributes_task_id_from_active_task(pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    """V0.5 point 1 : Claude Code n'envoie jamais task_id — le hook doit le
    résoudre depuis la "tâche active" du projet correspondant à `cwd`."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)

    from app.active_task import activate_task
    from app.database.repository import insert_project, insert_task
    from app.models import Project, Task

    project_dir = tmp_path / "projet-hook-actif"
    project_dir.mkdir()

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    from app.database.migrate import apply_migrations
    apply_migrations(conn, isolated_repo_root / "migrations")
    project = Project(name="projet-hook-actif", path=str(project_dir))
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche active pour le hook")
    insert_task(conn, task)
    activate_task(conn, task.id, isolated_repo_root / "logs")
    conn.close()

    event = {
        "tool_name": "Bash",
        "cwd": str(project_dir),
        "tool_input": {"command": "git status"},
        # PAS de task_id ici — exactement ce que Claude Code envoie réellement.
    }
    exit_code, _message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT task_id FROM commands WHERE command = 'git status' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["task_id"] == task.id


def test_pre_tool_use_no_active_task_keeps_task_id_none(pre_tool_use_module, isolated_repo_root, tmp_path, monkeypatch):
    """Comportement préservé quand aucune tâche n'est active (ou le projet
    n'est pas enregistré) : jamais bloquant, task_id reste NULL comme avant
    cette fonctionnalité."""
    monkeypatch.setattr(pre_tool_use_module, "REPO_ROOT", isolated_repo_root)
    unrelated_dir = tmp_path / "jamais-enregistre"
    unrelated_dir.mkdir()

    event = {
        "tool_name": "Bash",
        "cwd": str(unrelated_dir),
        "tool_input": {"command": "git status"},
    }
    exit_code, _message = pre_tool_use_module.handle_event(event)
    assert exit_code == 0

    conn = get_connection(isolated_repo_root / "data" / "herbert.db")
    row = conn.execute(
        "SELECT task_id FROM commands WHERE command = 'git status' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["task_id"] is None
