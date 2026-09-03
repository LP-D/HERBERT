"""Jeton de confirmation manuelle pour un push classé MANUAL_REQUIRED
(app/push_classifier.py) — voir cmd_sync_push dans app/cli/main.py.

`--confirm-manual` n'est accepté que si un jeton a été écrit par un appel
PRÉCÉDENT de `engine sync push` (classé MANUAL_REQUIRED), pour EXACTEMENT
le même diff (empreinte = hash de la ref amont + du commit HEAD au moment
du blocage — un nouveau commit après le blocage invalide le jeton), ET
dans une FENÊTRE de validité étroite : entre MIN_CONFIRM_DELAY_SECONDS et
MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS après l'écriture
du jeton.

CE QUE ÇA GARANTIT RÉELLEMENT (vérifié empiriquement le 2026-09-03, pas
supposé) : empêche le chaînage INSTANTANÉ vérification+confirmation (le
cas réel qui a motivé ce mécanisme), et élimine la possibilité d'attendre
PASSIVEMENT un prochain point de contact quelconque pour confirmer (sans
la fenêtre haute, un jeton resterait valide indéfiniment tant que le diff
ne change pas). CE QUE ÇA NE GARANTIT PAS : un agent qui insère un
`sleep` explicite d'une durée comprise dans la fenêtre, DANS la même
commande/le même script, peut toujours satisfaire les deux bornes — testé
empiriquement (sleep 31s dans un seul script Python = confirmation
acceptée). La fenêtre étroite ne rend pas ça structurellement impossible,
elle force ce sleep à être explicite et anormalement long dans la
commande elle-même — un signal visible pour qui relit le tool call, pas
une garantie absolue du code.
"""
import hashlib
import json
import time
from pathlib import Path

MIN_CONFIRM_DELAY_SECONDS = 30
# Fenêtre de validité APRÈS le délai minimum — volontairement courte pour
# forcer un sleep explicite proche de MIN_CONFIRM_DELAY_SECONDS plutôt que
# de permettre une confirmation "à tout moment ultérieur, tant que le diff
# n'a pas changé".
TOKEN_EXPIRY_WINDOW_SECONDS = 10


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

    max_valid = MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS
    if elapsed > max_valid:
        return False, (
            f"jeton expiré ({elapsed:.1f}s écoulées, fenêtre de validité "
            f"[{MIN_CONFIRM_DELAY_SECONDS}s, {max_valid}s]) — relancez `engine sync push` "
            "pour un nouveau blocage, --confirm-manual ne se garde pas indéfiniment"
        )

    return True, "confirmation valide"
