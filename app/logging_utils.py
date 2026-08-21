import json
from datetime import datetime, timezone
from pathlib import Path


def append_jsonl_event(
    logs_dir: str | Path,
    component: str,
    event: str,
    level: str,
    status: str,
    task_id: str | None = None,
    details: dict | None = None,
) -> Path:
    """Ajoute un événement structuré en fin de fichier JSONL (append-only).

    Un fichier par jour UTC (herbert-YYYY-MM-DD.jsonl) sous logs_dir.
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    log_file = logs_dir / f"herbert-{now:%Y-%m-%d}.jsonl"

    record = {
        "timestamp": now.isoformat(),
        "task_id": task_id,
        "component": component,
        "level": level,
        "event": event,
        "status": status,
        "details": details or {},
    }

    with open(log_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return log_file
