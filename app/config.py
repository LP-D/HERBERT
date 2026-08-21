"""Chargeur de configuration minimal pour config/system.yaml.

Volontairement pas de dépendance PyYAML (contrainte ZERO_EXTRA_COST : stdlib
uniquement en dehors de pydantic/pytest). Supporte un sous-ensemble de YAML
suffisant pour nos besoins : sections à un niveau, paires clé/valeur.
"""
from pathlib import Path

DEFAULT_CONFIG = {
    "database": {"path": "data/herbert.db"},
    "migrations": {"dir": "migrations"},
    "logs": {"dir": "logs"},
}


def _parse_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_config(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        return {k: dict(v) for k, v in DEFAULT_CONFIG.items()}

    config: dict = {}
    current_section: str | None = None

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue

        indent = len(raw_line) - len(raw_line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()

        if indent == 0:
            if value == "":
                config[key] = {}
                current_section = key
            else:
                config[key] = _parse_scalar(value)
                current_section = None
        else:
            if current_section is None:
                continue
            config[current_section][key] = _parse_scalar(value)

    return config


def write_default_config(path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        "# Configuration HERBERT V0.1 — clé: valeur, un seul niveau d'imbrication.\n"
        "database:\n"
        "  path: data/herbert.db\n\n"
        "migrations:\n"
        "  dir: migrations\n\n"
        "logs:\n"
        "  dir: logs\n"
    )
    path.write_text(content, encoding="utf-8")
    return path
