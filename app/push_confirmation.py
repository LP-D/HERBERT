"""Jeton de confirmation manuelle pour un push classé MANUAL_REQUIRED
(app/push_classifier.py) — voir cmd_sync_push dans app/cli/main.py.

`--confirm-manual` n'est accepté que si un jeton a été écrit par un appel
PRÉCÉDENT de `engine sync push` (classé MANUAL_REQUIRED), pour EXACTEMENT
le même diff (empreinte = hash de la ref amont + du commit HEAD au moment
du blocage — un nouveau commit après le blocage invalide le jeton), ET
dans une FENÊTRE de validité : entre MIN_CONFIRM_DELAY_SECONDS et
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
acceptée). La fenêtre ne rend pas ça structurellement impossible, elle
force ce sleep à être explicite et anormalement long dans la commande
elle-même — un signal visible pour qui relit le tool call, pas une
garantie absolue du code.

RECALIBRAGE DU 2026-09-04 : la fenêtre initiale [30s, 40s] a échoué deux
fois de suite en usage réel (122,1s et 159,4s écoulées pour un blocage
suivi d'un `--confirm-manual` explicitement voulu et non retardé
délibérément), à cause de la latence propre à l'ordonnanceur d'outils/
tâches en arrière-plan de l'environnement d'exécution — pas d'un sleep
insuffisant ni d'un aléa isolé (les deux mesures sont du même ordre de
grandeur malgré des sleeps différents, ce qui pointe vers un surcoût
structurel plutôt qu'une variance ponctuelle ; 2 mesures seulement,
donc hypothèse raisonnable mais pas prouvée statistiquement). Fenêtre
élargie à [30s, 300s] pour absorber cette latence avec marge, tout en
restant très en dessous d'un délai qui permettrait une attente passive
jusqu'au prochain point de contact humain naturel. `validate_confirmation`
retourne désormais aussi le temps écoulé mesuré (secondes), pour que
chaque tentative — réussie ou non — laisse une donnée réelle exploitable
si un nouveau recalibrage s'avère nécessaire, plutôt que de deviner.
"""
import hashlib
import json
import time
from pathlib import Path

MIN_CONFIRM_DELAY_SECONDS = 30
# Fenêtre de validité APRÈS le délai minimum. Élargie le 2026-09-04 (voir
# note "RECALIBRAGE" ci-dessus) pour absorber la latence réelle observée
# de l'ordonnanceur d'outils, pas pour permettre une attente passive.
TOKEN_EXPIRY_WINDOW_SECONDS = 270


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
) -> tuple[bool, str, float | None]:
    """Retourne (valide, raison, temps_écoulé_secondes). `now` injectable
    pour les tests — sans injection, utilise le vrai temps (time.time()).

    Le 3e élément (temps réellement écoulé depuis l'écriture du jeton) est
    `None` uniquement quand aucune mesure n'est possible (aucun jeton, ou
    jeton illisible) ; dans tous les autres cas — y compris un jeton
    invalidé par un diff différent — il reflète le temps réel écoulé, pour
    que l'appelant puisse le journaliser même sur un échec."""
    now = time.time() if now is None else now
    path = _token_path(repo_root)
    if not path.exists():
        return False, "aucun push bloqué en attente — relancez d'abord `engine sync push` sans --confirm-manual", None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"jeton de confirmation illisible: {exc}", None

    elapsed = now - data.get("created_at", 0)

    if data.get("fingerprint") != current_fingerprint:
        return False, "le diff a changé depuis le blocage (nouveaux commits ?) — relancez `engine sync push` pour re-classer", elapsed

    if elapsed < MIN_CONFIRM_DELAY_SECONDS:
        return False, (
            f"délai minimum non atteint ({elapsed:.1f}s / {MIN_CONFIRM_DELAY_SECONDS}s) — "
            "--confirm-manual ne peut pas suivre le blocage dans la même action"
        ), elapsed

    max_valid = MIN_CONFIRM_DELAY_SECONDS + TOKEN_EXPIRY_WINDOW_SECONDS
    if elapsed > max_valid:
        return False, (
            f"jeton expiré ({elapsed:.1f}s écoulées, fenêtre de validité "
            f"[{MIN_CONFIRM_DELAY_SECONDS}s, {max_valid}s]) — relancez `engine sync push` "
            "pour un nouveau blocage, --confirm-manual ne se garde pas indéfiniment"
        ), elapsed

    return True, "confirmation valide", elapsed
