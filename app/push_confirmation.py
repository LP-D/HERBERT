"""Jeton de confirmation manuelle pour un push classé MANUAL_REQUIRED
(app/push_classifier.py) — voir cmd_sync_push dans app/cli/main.py.

Empêche structurellement qu'un agent chaîne la vérification et le push
forcé dans une seule invocation Bash : `--confirm-manual` n'est accepté
que si un jeton a été écrit par un appel PRÉCÉDENT et SÉPARÉ de
`engine sync push` (classé MANUAL_REQUIRED), au moins
MIN_CONFIRM_DELAY_SECONDS plus tôt, et pour EXACTEMENT le même diff
(empreinte = hash de la ref amont + du commit HEAD au moment du blocage) —
un nouveau commit après le blocage invalide le jeton, il faut se refaire
bloquer avant de confirmer. Le délai minimum rend une invocation chaînée
("vérifier puis forcer dans la même commande") structurellement
insuffisante : les deux étapes ne peuvent pas partager le même instant.
"""
import hashlib
import json
import time
from pathlib import Path

MIN_CONFIRM_DELAY_SECONDS = 30


def diff_fingerprint(head_commit: str, upstream_ref: str | None) -> str:
    return hashlib.sha256(f"{upstream_ref or 'ROOT'}:{head_commit}".encode("utf-8")).hexdigest()


def _token_path(repo_root: str | Path) -> Path:
    return Path(repo_root) / "data" / "pending_manual_push.json"


def write_pending_confirmation(repo_root: str | Path, fingerprint: str, failed_criteria: list[str]) -> None:
    path = _token_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"created_at": time.time(), "fingerprint": fingerprint, "failed_criteria": failed_criteria},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def clear_pending_confirmation(repo_root: str | Path) -> None:
    _token_path(repo_root).unlink(missing_ok=True)


def validate_confirmation(
    repo_root: str | Path, current_fingerprint: str, now: float | None = None
) -> tuple[bool, str]:
    """Retourne (valide, raison). `now` injectable pour les tests — sans
    injection, utilise le vrai temps (time.time())."""
    now = time.time() if now is None else now
    path = _token_path(repo_root)
    if not path.exists():
        return False, "aucun push bloqué en attente — relancez d'abord `engine sync push` sans --confirm-manual"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"jeton de confirmation illisible: {exc}"

    if data.get("fingerprint") != current_fingerprint:
        return False, "le diff a changé depuis le blocage (nouveaux commits ?) — relancez `engine sync push` pour re-classer"

    elapsed = now - data.get("created_at", 0)
    if elapsed < MIN_CONFIRM_DELAY_SECONDS:
        return False, (
            f"délai minimum non atteint ({elapsed:.1f}s / {MIN_CONFIRM_DELAY_SECONDS}s) — "
            "--confirm-manual ne peut pas suivre le blocage dans la même action"
        )

    return True, "confirmation valide"
