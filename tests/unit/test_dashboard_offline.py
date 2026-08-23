"""Phase 4 : le dashboard doit rester 100% offline. Aucune balise src/href
d'un fichier HTML/CSS/JS généré ne doit pointer vers un domaine réseau
tiers (cdnjs, unpkg, jsdelivr, fonts.google, ou n'importe quel autre
http(s)://). Durci : ce test doit échouer si un tel lien apparaît, pas
seulement passer trivialement sur le contenu actuel — voir
test_offline_check_detects_injected_external_reference ci-dessous, qui
prouve que le détecteur a réellement des dents."""
import re
from pathlib import Path

from app.database.repository import insert_project, insert_task
from app.models import Project, Task
from app.reporting.dashboard_builder import build_dashboard

# Cible tout href/src en HTML, et tout url(...) en CSS.
_EXTERNAL_REF_RE = re.compile(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']|url\(\s*["\']?([^)"\']+)["\']?\s*\)')


def _external_violations(dashboard_dir: Path) -> list[str]:
    """Retourne la liste des références réseau tierces trouvées.

    EXCEPTION EXPLICITE ET DOCUMENTÉE : un chemin relatif vers
    `assets/vendor/` (fichier vendorisé localement dans le dépôt, jamais une
    requête réseau à l'exécution) est autorisé — voir docs/DASHBOARD.md,
    section décision GSAP/licence (Phase 0c). Tout le reste commençant par
    `http://` ou `https://` est une violation, vendorisé ou non : le but est
    justement d'empêcher qu'un CDN (cdnjs, unpkg, jsdelivr, fonts.google)
    s'introduise un jour, même "juste en fallback".
    """
    violations = []
    for path in list(dashboard_dir.rglob("*.html")) + list(dashboard_dir.rglob("*.css")) + list(dashboard_dir.rglob("*.js")):
        content = path.read_text(encoding="utf-8")
        for m in _EXTERNAL_REF_RE.finditer(content):
            ref = m.group(1) or m.group(2)
            if not ref:
                continue
            if ref.startswith(("http://", "https://", "//")):
                violations.append(f"{path.relative_to(dashboard_dir)}: {ref}")
    return violations


def _seed(conn):
    project = Project(name="projet-offline", path="C:/projet-offline")
    insert_project(conn, project)
    task = Task(project_id=project.id, description="tâche offline")
    insert_task(conn, task)
    return project, task


def test_generated_dashboard_has_zero_external_references(db_conn, tmp_path):
    _seed(db_conn)
    dashboard_dir = tmp_path / "dashboard"
    build_dashboard(db_conn, dashboard_dir, tmp_path / "logs")

    violations = _external_violations(dashboard_dir)
    assert violations == [], f"référence(s) réseau externe trouvée(s) : {violations}"


def test_offline_check_detects_injected_external_reference(tmp_path):
    """Preuve que le détecteur a des dents : sans cette assertion, un test
    qui passe toujours (même sur un dashboard truffé de CDN) serait une
    fausse sécurité. On injecte volontairement une référence à un CDN connu
    et on vérifie qu'elle est bien détectée."""
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "index.html").write_text(
        '<html><head><script src="https://cdn.jsdelivr.net/npm/somelib@1.0/lib.min.js"></script></head>'
        '<body><link href="https://fonts.googleapis.com/css?family=Roboto"></body></html>',
        encoding="utf-8",
    )

    violations = _external_violations(dashboard_dir)

    assert len(violations) == 2
    assert any("jsdelivr" in v for v in violations)
    assert any("fonts.googleapis" in v for v in violations)


def test_offline_check_allows_local_vendor_assets(tmp_path):
    """Un chemin relatif vers assets/vendor/ (fichier vendorisé, pas une
    requête réseau) ne doit PAS être signalé — c'est l'exception explicite
    documentée dans _external_violations()."""
    dashboard_dir = tmp_path / "dashboard"
    (dashboard_dir / "task").mkdir(parents=True)
    (dashboard_dir / "task" / "abc.html").write_text(
        '<html><head><script src="../assets/vendor/gsap.min.js"></script>'
        '<link rel="stylesheet" href="../assets/style.css"></head><body></body></html>',
        encoding="utf-8",
    )

    violations = _external_violations(dashboard_dir)
    assert violations == []


def test_offline_check_protocol_relative_url_is_flagged(tmp_path):
    """`//cdn.example.com/x.js` (protocol-relative) est un cas réel de
    contournement d'un filtre naïf sur "http" — doit être détecté aussi."""
    dashboard_dir = tmp_path / "dashboard"
    dashboard_dir.mkdir()
    (dashboard_dir / "index.html").write_text(
        '<html><body><script src="//sneaky-cdn.example.com/lib.js"></script></body></html>', encoding="utf-8"
    )

    violations = _external_violations(dashboard_dir)
    assert len(violations) == 1
    assert "sneaky-cdn.example.com" in violations[0]
