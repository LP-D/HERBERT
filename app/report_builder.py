"""Génère un rapport HTML statique local pour une tâche, à partir des
données déjà en SQLite — AUCUNE nouvelle source de données. Pas de
dépendance externe : HTML + CSS inline minimal, stdlib Python uniquement
(f-strings), pas de Jinja2. Pas de serveur, aucun port ouvert : c'est un
fichier statique écrit sur disque (reports/<task_id>.html).
"""
import html
import sqlite3
from datetime import date
from pathlib import Path

from app.database.repository import get_latest_change_proof, get_latest_test_result_for_task, get_project
from app.models import Task

_STYLE = """
body { font-family: -apple-system, Segoe UI, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; background: #fafafa; }
h1, h2 { border-bottom: 2px solid #ddd; padding-bottom: 0.3rem; }
table { border-collapse: collapse; width: 100%; margin: 1rem 0; }
th, td { border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; font-size: 0.9rem; }
th { background: #eee; }
.badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 3px; font-size: 0.85rem; font-weight: bold; }
.badge-pass { background: #d4edda; color: #155724; }
.badge-fail { background: #f8d7da; color: #721c24; }
.badge-neutral { background: #e2e3e5; color: #383d41; }
.warning-box { background: #fff3cd; border: 1px solid #ffe08a; padding: 0.8rem 1rem; border-radius: 4px; margin: 1rem 0; }
.limitation-box { background: #f8d7da; border: 1px solid #f1aeb5; padding: 0.8rem 1rem; border-radius: 4px; margin: 1rem 0; }
code { background: #eee; padding: 0.1rem 0.3rem; border-radius: 3px; }
.regressions { color: #721c24; font-weight: bold; }
"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _badge(text: str, kind: str) -> str:
    return f'<span class="badge badge-{kind}">{_esc(text)}</span>'


def _state_transitions_rows(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT from_state, to_state, allowed, reason, created_at
           FROM state_transitions WHERE task_id = ? ORDER BY created_at""",
        (task_id,),
    ).fetchall()


def _relevant_jsonl_files(logs_dir: Path, start: date, end: date) -> list[Path]:
    if not logs_dir.exists():
        return []
    matches = []
    for path in sorted(logs_dir.glob("herbert-*.jsonl")):
        stem = path.stem.removeprefix("herbert-")
        try:
            file_date = date.fromisoformat(stem)
        except ValueError:
            continue
        if start <= file_date <= end:
            matches.append(path)
    return matches


def build_report_html(conn: sqlite3.Connection, task: Task, logs_dir: Path) -> str:
    project = get_project(conn, task.project_id)
    proof = get_latest_change_proof(conn, task.id)
    test_result = get_latest_test_result_for_task(conn, task.id)
    transitions = _state_transitions_rows(conn, task.id)

    project_name = project.name if project else "(projet introuvable)"

    transitions_rows = "".join(
        f"<tr><td>{_esc(t['from_state'])}</td><td>{_esc(t['to_state'])}</td>"
        f"<td>{_badge('acceptée', 'pass') if t['allowed'] else _badge('refusée', 'fail')}</td>"
        f"<td>{_esc(t['reason'] or '')}</td><td>{_esc(t['created_at'])}</td></tr>"
        for t in transitions
    ) or "<tr><td colspan='5'><em>Aucune transition enregistrée</em></td></tr>"

    if proof is None:
        change_proof_html = "<p><em>Aucun ChangeProof enregistré pour cette tâche (lancez <code>engine task test</code>).</em></p>"
        end_date = task.created_at.date()
    else:
        files_changed_html = "".join(f"<li><code>{_esc(f)}</code></li>" for f in proof.files_changed) or "<li><em>aucun</em></li>"
        regressions_html = (
            f'<p class="regressions">Régressions détectées : {_esc(", ".join(proof.regressions))}</p>'
            if proof.regressions
            else "<p>Aucune régression détectée par rapport au dernier run VERIFIED_PASS connu.</p>"
        )
        change_proof_html = f"""
        <p>Statut : {_badge(proof.status.value, 'pass' if proof.status.value == 'DONE' else 'fail')}
           — tests passés : {proof.tests_passed}, tests échoués : {proof.tests_failed}
           ({_esc(proof.created_at.isoformat())})</p>
        {regressions_html}
        <h3>Fichiers modifiés</h3>
        <ul>{files_changed_html}</ul>
        <div class="limitation-box">
            <strong>Limite connue, non corrigée en V0.2 :</strong>
            <code>commands_executed</code> reste <strong>vide</strong> dans ce ChangeProof
            ({len(proof.commands_executed)} commande(s) enregistrée(s)). Claude Code n'envoie
            aucun <code>task_id</code> au hook PreToolUse : aucune commande Bash réelle n'est
            actuellement reliée à cette tâche. Voir le docstring de
            <code>app/change_proof_builder.py</code> pour le détail.
        </div>
        """
        end_date = proof.created_at.date()

    if test_result is not None:
        test_kind = "pass" if test_result.status.value == "VERIFIED_PASS" else "fail"
        test_result_html = (
            f"<p>Dernier TestResult : {_badge(test_result.status.value, test_kind)} — "
            f"total={test_result.total}, passed={test_result.passed}, "
            f"failed={test_result.failed}, errors={test_result.errors}, "
            f"durée={test_result.duration_seconds:.2f}s</p>"
        )
    else:
        test_result_html = "<p><em>Aucun TestResult enregistré pour cette tâche.</em></p>"

    start_date = task.created_at.date()
    jsonl_files = _relevant_jsonl_files(logs_dir, start_date, end_date)
    jsonl_html = "".join(f"<li><code>{_esc(p)}</code></li>" for p in jsonl_files) or "<li><em>aucun fichier trouvé pour cette période</em></li>"

    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Rapport HERBERT — tâche {_esc(task.id)}</title>
<style>{_STYLE}</style>
</head>
<body>
<h1>Rapport de tâche</h1>
<p><strong>Projet :</strong> {_esc(project_name)}<br>
<strong>Description :</strong> {_esc(task.description)}<br>
<strong>État actuel :</strong> {_badge(task.status.value, 'neutral')}<br>
<strong>Créée le :</strong> {_esc(task.created_at.isoformat())}</p>

<h2>Dernier ChangeProof</h2>
{change_proof_html}

<h2>Dernier résultat de test</h2>
{test_result_html}

<h2>Historique des transitions d'état</h2>
<table>
<thead><tr><th>De</th><th>Vers</th><th>Décision</th><th>Raison</th><th>Horodatage</th></tr></thead>
<tbody>{transitions_rows}</tbody>
</table>

<h2>Logs JSONL bruts (période concernée)</h2>
<p>Chemins locaux uniquement — contenu non copié ici :</p>
<ul>{jsonl_html}</ul>

</body>
</html>
"""


def write_report(conn: sqlite3.Connection, task: Task, logs_dir: Path, reports_dir: Path) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_path = reports_dir / f"{task.id}.html"
    output_path.write_text(build_report_html(conn, task, logs_dir), encoding="utf-8")
    return output_path
