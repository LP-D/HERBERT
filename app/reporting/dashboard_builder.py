"""Génère le dashboard HTML statique local (V0.4) — LECTURE SEULE sur le
SQLite existant, via app/reporting/dashboard_data.py. Aucun serveur, aucun
port ouvert : uniquement des fichiers écrits sous dashboard/. Stdlib Python
uniquement (f-strings), même approche que app/report_builder.py — pas de
Jinja2, pas de librairie de charting pour le diagramme SVG.

Motion : CSS pur (transitions/animations natives), pas de GSAP — décision
documentée dans docs/DASHBOARD.md (licence GSAP vérifiée permissive, mais
design-taste-frontend lui-même déconseille GSAP hors animations de
pin/scroll réelles, et ce dashboard n'en a pas besoin).
"""
import html
import json
import sqlite3
import webbrowser
from pathlib import Path

from app.report_builder import build_report_html
from app.reporting.dashboard_data import (
    list_audit_log,
    list_human_decisions,
    list_projects_with_task_counts,
    list_rollbacks,
    list_tasks_for_project,
)
from app.state_machine.states import TaskState
from app.state_machine.transitions import LEGAL_TRANSITIONS

# --- palette par état (contraste vérifié >= 7.9:1 en mode clair, voir le
# calcul dans le rapport de la Phase 0/2 — pas d'accent violet/"AI purple",
# 9 teintes distinctes) ---------------------------------------------------
_STATE_COLORS: dict[str, tuple[str, str]] = {  # état -> (bg, fg)
    "RECEIVED": ("#e2e8f0", "#334155"),
    "EXECUTING": ("#dbeafe", "#1e3a8a"),
    "TESTING": ("#cffafe", "#164e63"),
    "DONE": ("#dcfce7", "#14532d"),
    "FAILED": ("#fee2e2", "#7f1d1d"),
    "BLOCKED": ("#ffedd5", "#7c2d12"),
    "HUMAN_REQUIRED": ("#fef3c7", "#78350f"),
    "PROMOTED": ("#ccfbf1", "#134e4a"),
    "ROLLED_BACK": ("#ffe4e6", "#881337"),
}

_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
  margin: 0; padding: 0 1.25rem 3rem; max-width: 1100px; margin-inline: auto;
  color: #1a1a1a; background: #fafafa; line-height: 1.5;
}
code, .mono { font-family: ui-monospace, "Cascadia Code", "Segoe UI Mono", Consolas, monospace; font-size: 0.85em; }
h1, h2, h3 { line-height: 1.25; }
h1 { font-size: 1.6rem; margin: 1.5rem 0 0.75rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 0.5rem; border-bottom: 1px solid #ddd; padding-bottom: 0.3rem; }
a { color: #1e3a8a; text-decoration: none; }
a:hover { text-decoration: underline; }

nav.hb-nav {
  display: flex; align-items: center; gap: 0.5rem; padding: 0.75rem 0;
  font-size: 0.9rem; color: #555; border-bottom: 1px solid #e5e5e5; margin-bottom: 1rem;
}
nav.hb-nav a { color: #1e3a8a; }
nav.hb-nav .sep { color: #999; }

.hb-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; }
.hb-card {
  display: block; background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
  padding: 1rem 1.1rem; transition: box-shadow 150ms ease, border-color 150ms ease, transform 100ms ease;
}
a.hb-card:hover { box-shadow: 0 2px 10px rgba(0,0,0,0.08); border-color: #c7d2e8; text-decoration: none; }
a.hb-card:active { transform: scale(0.98); }
.hb-card h3 { margin: 0 0 0.35rem; font-size: 1.05rem; color: #1a1a1a; }
.hb-card .path { color: #666; font-size: 0.8rem; margin-bottom: 0.6rem; word-break: break-all; }

.badge {
  display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
  font-size: 0.78rem; font-weight: 600; white-space: nowrap;
}
.badge-count { opacity: 0.55; }
.badge-row { display: flex; flex-wrap: wrap; gap: 0.35rem; }

table.hb-table { border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; }
table.hb-table th, table.hb-table td { border-bottom: 1px solid #e5e5e5; padding: 0.5rem 0.6rem; text-align: left; font-size: 0.88rem; }
table.hb-table th { background: #f0f0f0; cursor: pointer; user-select: none; position: sticky; top: 0; }
table.hb-table th:hover { background: #e6e6e6; }
table.hb-table th.sorted::after { content: " \\25BE"; }
table.hb-table tbody tr { transition: background-color 120ms ease; }
table.hb-table tbody tr:hover { background: #f5f7fb; }

input.hb-filter {
  width: 100%; max-width: 420px; padding: 0.45rem 0.7rem; margin: 0.5rem 0 0.75rem;
  border: 1px solid #ccc; border-radius: 6px; font-size: 0.9rem;
  transition: border-color 150ms ease, box-shadow 150ms ease;
}
input.hb-filter:focus { outline: none; border-color: #1e3a8a; box-shadow: 0 0 0 3px rgba(30,58,138,0.12); }

button.hb-btn {
  padding: 0.45rem 0.9rem; border: 1px solid #1e3a8a; background: #fff; color: #1e3a8a;
  border-radius: 6px; font-size: 0.85rem; cursor: pointer; transition: background-color 120ms ease, transform 100ms ease;
}
button.hb-btn:hover { background: #eef2fb; }
button.hb-btn:active { transform: translateY(1px); }
button.hb-btn:disabled { opacity: 0.4; cursor: default; }

details.hb-details { border: 1px solid #e0e0e0; border-radius: 6px; margin: 0.5rem 0; background: #fff; }
details.hb-details summary { padding: 0.5rem 0.75rem; cursor: pointer; font-size: 0.85rem; }
details.hb-details[open] summary { border-bottom: 1px solid #eee; }
details.hb-details .hb-details-body { padding: 0.6rem 0.9rem; font-size: 0.8rem; color: #444; }

.hb-svg-wrap { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 0.75rem; overflow-x: auto; }
.hb-empty { color: #777; font-style: italic; padding: 0.5rem 0; }
"""

_APP_JS = """
function hbInitFilter(inputId, rowSelector, matchAttr) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener("input", () => {
    const q = input.value.trim().toLowerCase();
    document.querySelectorAll(rowSelector).forEach((row) => {
      const haystack = (matchAttr ? row.getAttribute(matchAttr) : row.textContent) || "";
      row.style.display = haystack.toLowerCase().includes(q) ? "" : "none";
    });
  });
}

function hbInitSortableTable(tableId) {
  const table = document.getElementById(tableId);
  if (!table) return;
  const tbody = table.tBodies[0];
  table.querySelectorAll("th[data-sort-key]").forEach((th, colIndex) => {
    let asc = true;
    th.addEventListener("click", () => {
      const key = th.getAttribute("data-sort-key");
      const rows = Array.from(tbody.querySelectorAll("tr"));
      rows.sort((a, b) => {
        const av = a.children[colIndex].getAttribute("data-sort-value") || a.children[colIndex].textContent;
        const bv = b.children[colIndex].getAttribute("data-sort-value") || b.children[colIndex].textContent;
        if (av < bv) return asc ? -1 : 1;
        if (av > bv) return asc ? 1 : -1;
        return 0;
      });
      asc = !asc;
      table.querySelectorAll("th").forEach((h) => h.classList.remove("sorted"));
      th.classList.add("sorted");
      rows.forEach((r) => tbody.appendChild(r));
    });
  });
}

function hbInitLoadMore(containerId, buttonId, pageSize) {
  const container = document.getElementById(containerId);
  const button = document.getElementById(buttonId);
  if (!container || !button) return;
  const rows = Array.from(container.children);
  let shown = 0;
  function reveal() {
    const next = rows.slice(shown, shown + pageSize);
    next.forEach((r) => (r.style.display = ""));
    shown += next.length;
    if (shown >= rows.length) { button.disabled = true; button.textContent = "Tout est affiché (" + rows.length + ")"; }
    else { button.textContent = "Charger plus (" + shown + "/" + rows.length + ")"; }
  }
  rows.forEach((r) => (r.style.display = "none"));
  button.addEventListener("click", reveal);
  reveal();
}
"""


def _esc(value) -> str:
    return html.escape(str(value)) if value is not None else ""


def _badge(text: str, state: str) -> str:
    bg, fg = _STATE_COLORS.get(state, ("#e2e3e5", "#383d41"))
    return f'<span class="badge" style="background:{bg};color:{fg}">{_esc(text)}</span>'


def _page(title: str, nav_html: str, body_html: str, asset_prefix: str) -> str:
    # app.js est chargé dans <head>, SANS defer/async : il doit s'exécuter en
    # synchrone avant que le parser n'atteigne les <script> inline du body
    # (hbInitFilter/hbInitSortableTable/hbInitLoadMore) — sinon ces appels
    # échouent silencieusement (ReferenceError avalé), les fonctions n'étant
    # pas encore définies. Bug réel constaté en Phase 6 sur données réelles
    # (le filtre de project/<id>.html ne filtrait rien) : les tests Python
    # ne peuvent pas détecter un problème d'ordre d'exécution JS, seul un
    # navigateur réel le révèle — voir docs/DASHBOARD.md.
    return f"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)}</title>
<link rel="stylesheet" href="{asset_prefix}assets/style.css">
<script src="{asset_prefix}assets/app.js"></script>
</head>
<body>
{nav_html}
{body_html}
</body>
</html>
"""


def _nav(*crumbs: tuple[str, str | None]) -> str:
    """`crumbs` = [(label, href_or_None), ...] — le dernier élément est la
    page courante (pas de lien)."""
    parts = []
    for i, (label, href) in enumerate(crumbs):
        if i > 0:
            parts.append('<span class="sep">/</span>')
        parts.append(f'<a href="{_esc(href)}">{_esc(label)}</a>' if href else f"<strong>{_esc(label)}</strong>")
    return f'<nav class="hb-nav">{"".join(parts)}</nav>'


# --- diagramme SVG de la state machine -----------------------------------

_NODE_POS: dict[str, tuple[int, int]] = {
    "RECEIVED": (300, 40),
    "EXECUTING": (300, 150),
    "BLOCKED": (500, 150),
    "TESTING": (300, 260),
    "FAILED": (100, 260),
    "HUMAN_REQUIRED": (500, 260),
    "DONE": (300, 370),
    "PROMOTED": (300, 480),
    "ROLLED_BACK": (500, 480),
}
_NODE_W, _NODE_H = 130, 40


def _svg_arrow_defs() -> str:
    return """
    <defs>
      <marker id="arrow-gray" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#bbb" />
      </marker>
      <marker id="arrow-blue" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#1e3a8a" />
      </marker>
      <marker id="arrow-red" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill="#c81e3a" />
      </marker>
    </defs>"""


def _edge_path(a: str, b: str) -> tuple[int, int, int, int]:
    ax, ay = _NODE_POS[a]
    bx, by = _NODE_POS[b]
    return ax, ay + _NODE_H // 2, bx, by - _NODE_H // 2 if by > ay else by + _NODE_H // 2


def render_state_diagram_svg(transitions: list[dict], current_state: str) -> str:
    """SVG généré en Python pur (pas de librairie de charting) : structure
    complète de la state machine en gris clair, chemin RÉELLEMENT suivi par
    cette tâche (transitions allowed=True, dans l'ordre) en bleu épais,
    tentatives refusées (allowed=False) en rouge pointillé, état courant
    entouré."""
    parts = [f'<svg viewBox="0 0 640 540" width="640" height="540" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="Diagramme de la state machine HERBERT">']
    parts.append(_svg_arrow_defs())

    # structure complète (fond, gris clair)
    for from_state, targets in LEGAL_TRANSITIONS.items():
        for to_state in targets:
            if from_state.value not in _NODE_POS or to_state.value not in _NODE_POS:
                continue
            x1, y1, x2, y2 = _edge_path(from_state.value, to_state.value)
            parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#ccc" stroke-width="1.5" marker-end="url(#arrow-gray)" />')

    # chemin réellement suivi (allowed=True, ordre chronologique)
    followed = [t for t in transitions if t["allowed"]]
    for t in followed:
        if t["from_state"] not in _NODE_POS or t["to_state"] not in _NODE_POS:
            continue
        x1, y1, x2, y2 = _edge_path(t["from_state"], t["to_state"])
        parts.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#1e3a8a" stroke-width="3.5" marker-end="url(#arrow-blue)" />')

    # tentatives refusées
    rejected = [t for t in transitions if not t["allowed"]]
    for t in rejected:
        if t["from_state"] not in _NODE_POS or t["to_state"] not in _NODE_POS:
            continue
        x1, y1, x2, y2 = _edge_path(t["from_state"], t["to_state"])
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="#c81e3a" stroke-width="2" '
            f'stroke-dasharray="5,4" marker-end="url(#arrow-red)" />'
        )

    # nœuds
    for state, (x, y) in _NODE_POS.items():
        bg, fg = _STATE_COLORS.get(state, ("#eee", "#333"))
        is_current = state == current_state
        stroke = "#111" if is_current else "#999"
        stroke_width = 3 if is_current else 1
        rx = x - _NODE_W // 2
        ry = y - _NODE_H // 2
        parts.append(
            f'<rect x="{rx}" y="{ry}" width="{_NODE_W}" height="{_NODE_H}" rx="8" '
            f'fill="{bg}" stroke="{stroke}" stroke-width="{stroke_width}" />'
        )
        parts.append(
            f'<text x="{x}" y="{y+5}" text-anchor="middle" font-size="12" font-family="monospace" fill="{fg}">{_esc(state)}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


# --- pages -----------------------------------------------------------------

def _build_index_html(projects: list[dict]) -> str:
    cards = []
    for p in projects:
        badges = "".join(
            f'{_badge(state, state)}<span class="badge-count">×{count}</span> '
            for state, count in p["task_counts"].items() if count > 0
        ) or '<span class="hb-empty">aucune tâche</span>'
        cards.append(f"""
        <a class="hb-card" href="project/{_esc(p['id'])}.html">
          <h3>{_esc(p['name'])}</h3>
          <div class="path">{_esc(p['path'])}</div>
          <div class="badge-row">{badges}</div>
        </a>""")

    body = f"""
    <h1>Projets HERBERT</h1>
    <p>{len(projects)} projet(s) enregistré(s).</p>
    <div class="hb-grid">{"".join(cards) or '<p class="hb-empty">Aucun projet enregistré (engine project add).</p>'}</div>
    """
    return _page("HERBERT — Dashboard", _nav(("Dashboard", None)), body, asset_prefix="")


def _build_project_html(project: dict, tasks: list[dict]) -> str:
    rows = []
    for t in tasks:
        rows.append(f"""
        <tr data-filter-text="{_esc(t['id'] + ' ' + t['description'] + ' ' + t['status'])}">
          <td class="mono" data-sort-value="{_esc(t['id'])}"><a href="../task/{_esc(t['id'])}.html">{_esc(t['id'][:8])}</a></td>
          <td data-sort-value="{_esc(t['description'])}">{_esc(t['description'])}</td>
          <td data-sort-value="{_esc(t['status'])}">{_badge(t['status'], t['status'])}</td>
          <td class="mono" data-sort-value="{_esc(t['created_at'])}">{_esc(t['created_at'])}</td>
          <td class="mono" data-sort-value="{_esc(t['updated_at'])}">{_esc(t['updated_at'])}</td>
        </tr>""")

    archived_notice = (
        f'<p class="hb-empty">Ce projet est archivé depuis le {_esc(project["archived_at"])} — '
        f"invisible sur le tableau de bord par défaut, mais toujours consultable directement.</p>"
        if project.get("archived_at") else ""
    )
    body = f"""
    <h1>{_esc(project['name'])}</h1>
    {archived_notice}
    <p class="path mono">{_esc(project['path'])}</p>
    <input class="hb-filter" id="task-filter" placeholder="Filtrer les tâches (id, description, état)...">
    <table class="hb-table" id="task-table">
      <thead><tr>
        <th data-sort-key="id">ID</th>
        <th data-sort-key="description">Description</th>
        <th data-sort-key="status">État</th>
        <th data-sort-key="created_at">Créée</th>
        <th data-sort-key="updated_at">Mise à jour</th>
      </tr></thead>
      <tbody>{"".join(rows) or '<tr><td colspan="5" class="hb-empty">Aucune tâche pour ce projet.</td></tr>'}</tbody>
    </table>
    <script>
      hbInitSortableTable("task-table");
      hbInitFilter("task-filter", "#task-table tbody tr", "data-filter-text");
    </script>
    """
    nav = _nav(("Dashboard", "../index.html"), (project["name"], None))
    return _page(f"HERBERT — {project['name']}", nav, body, asset_prefix="../")


def _build_task_html(conn: sqlite3.Connection, task_row: dict, project: dict, logs_dir: Path) -> str:
    from app.models import Task
    from app.database.repository import get_task

    task = get_task(conn, task_row["id"])
    base_report_html = build_report_html(conn, task, logs_dir)

    transitions = conn.execute(
        "SELECT from_state, to_state, allowed, created_at FROM state_transitions WHERE task_id = ? ORDER BY created_at",
        (task.id,),
    ).fetchall()
    transitions = [dict(r) for r in transitions]

    svg = render_state_diagram_svg(transitions, task.status.value)
    diagram_section = f"""
    <h2>Chemin d'état réellement suivi</h2>
    <p>Gris = transitions légales possibles. Bleu épais = chemin réellement suivi par cette tâche.
    Rouge pointillé = tentative(s) de transition refusée(s).</p>
    <div class="hb-svg-wrap">{svg}</div>
    """

    nav = _nav(("Dashboard", "../index.html"), (project["name"], f"../project/{project['id']}.html"), (task.id[:8], None))
    archived_notice = (
        f'<p class="hb-empty">Cette tâche est archivée depuis le {_esc(task_row["archived_at"])} — '
        f"invisible sur le tableau de bord par défaut, mais toujours consultable directement.</p>"
        if task_row.get("archived_at") else ""
    )

    # Réutilisation de app.report_builder.build_report_html telle quelle
    # (pas de duplication de sa logique) : on injecte la nav dashboard + le
    # lien vers le CSS partagé + le diagramme SVG par substitution de
    # marqueurs simples, sans parser HTML (même approche stdlib-only que le
    # reste du projet).
    out = base_report_html.replace(
        "</head>", '<link rel="stylesheet" href="../assets/style.css"></head>'
    )
    out = out.replace("<body>", f"<body>\n{nav}\n{archived_notice}")
    out = out.replace("</body>", f"{diagram_section}\n</body>")
    return out


def _build_decisions_html(decisions: list[dict]) -> str:
    rows = []
    for d in decisions:
        decision_badge = _badge("acceptée", "DONE") if d["allowed"] else _badge("refusée", "FAILED")
        rows.append(f"""
        <tr>
          <td class="mono">{_esc(d['created_at'])}</td>
          <td><a href="task/{_esc(d['task_id'])}.html">{_esc(d['task_description'])}</a></td>
          <td>{_esc(d['project_name'])}</td>
          <td>{_esc(d['from_state'])} &rarr; {_esc(d['to_state'])}</td>
          <td>{decision_badge}</td>
          <td>{_esc(d['reason'])}</td>
        </tr>""")

    body = f"""
    <h1>Décisions humaines</h1>
    <div class="hb-details" style="margin-bottom:1rem;">
      <details class="hb-details">
        <summary>Limite connue de cette page (cliquer pour lire)</summary>
        <div class="hb-details-body">
          Le schéma SQLite actuel ne distingue pas une transition déclenchée par un
          humain (<code>engine task status --to X --reason "..."</code>) d'une transition
          automatique. Cette page affiche les transitions dont la <code>reason</code>
          n'est ni vide ni un libellé système connu — une décision humaine prise
          <strong>sans</strong> <code>--reason</code> n'apparaît donc pas ici.
          Voir docs/DASHBOARD.md.
        </div>
      </details>
    </div>
    <table class="hb-table">
      <thead><tr><th>Horodatage</th><th>Tâche</th><th>Projet</th><th>Transition</th><th>Décision</th><th>Raison</th></tr></thead>
      <tbody>{"".join(rows) or '<tr><td colspan="6" class="hb-empty">Aucune décision humaine détectée.</td></tr>'}</tbody>
    </table>
    """
    return _page("HERBERT — Décisions humaines", _nav(("Dashboard", "index.html"), ("Décisions", None)), body, asset_prefix="")


_DETAILS_TRUNCATE_AT = 500


def _format_details(details: dict) -> str:
    """Certaines valeurs de `details` (sortie brute d'un outil capturée par
    le hook PostToolUse — lecture de fichier, sortie de commande) peuvent
    atteindre des dizaines de Ko. Constaté sur données réelles : 706 Ko pour
    seulement 51 entrées avant troncature, un `audit.html` illisible et
    inutilement lourd. Tronqué ici pour rester scannable, jamais caché
    silencieusement (le nombre de caractères retirés est toujours affiché)."""
    raw = json.dumps(details, ensure_ascii=False, default=str)
    if len(raw) <= _DETAILS_TRUNCATE_AT:
        return _esc(raw)
    return f"{_esc(raw[:_DETAILS_TRUNCATE_AT])}… <em>(tronqué, {len(raw) - _DETAILS_TRUNCATE_AT} caractère(s) de plus — voir logs/*.jsonl ou la table audit_log pour le détail complet)</em>"


def _build_audit_html(entries: list[dict]) -> str:
    rows = []
    for e in entries:
        target = f'<a href="task/{_esc(e["task_id"])}.html">{_esc(e["task_description"] or e["task_id"][:8])}</a>' if e["task_id"] else "<em>(aucune tâche)</em>"
        rows.append(f"""
        <div class="hb-audit-row" data-filter-text="{_esc(e['component'] + ' ' + e['event'] + ' ' + (e['project_name'] or '') + ' ' + e['status'])}">
          <details class="hb-details">
            <summary><span class="mono">{_esc(e['created_at'])}</span> — <strong>{_esc(e['component'])}</strong> — {_esc(e['event'])} — {_badge(e['status'], 'DONE' if e['status'] == 'VERIFIED' else 'FAILED')}</summary>
            <div class="hb-details-body">
              Tâche : {target}<br>
              Projet : {_esc(e['project_name'] or '(aucun)')}<br>
              Niveau : {_esc(e['level'])}<br>
              Détails : <code>{_format_details(e['details'])}</code>
            </div>
          </details>
        </div>""")

    rows_html = "".join(rows) or '<p class="hb-empty">Aucune entrée d’audit.</p>'
    body = f"""
    <h1>Journal d'audit</h1>
    <input class="hb-filter" id="audit-filter" placeholder="Filtrer (composant, événement, projet, statut)...">
    <div id="audit-list">{rows_html}</div>
    <button class="hb-btn" id="audit-load-more" type="button">Charger plus</button>
    <script>
      hbInitFilter("audit-filter", "#audit-list .hb-audit-row", "data-filter-text");
      hbInitLoadMore("audit-list", "audit-load-more", 200);
    </script>
    """
    return _page("HERBERT — Journal d'audit", _nav(("Dashboard", "index.html"), ("Audit", None)), body, asset_prefix="")


# --- orchestration -----------------------------------------------------

def build_dashboard(conn: sqlite3.Connection, dashboard_dir: Path, logs_dir: Path) -> dict:
    """Régénère TOUT dashboard_dir depuis SQLite — écrase le contenu
    existant, ne l'accumule pas. Retourne un manifeste des fichiers écrits
    (utilisé par les tests et par `engine dashboard build`)."""
    dashboard_dir = Path(dashboard_dir)
    if dashboard_dir.exists():
        for child in dashboard_dir.iterdir():
            if child.is_dir():
                import shutil
                shutil.rmtree(child)
            else:
                child.unlink()
    dashboard_dir.mkdir(parents=True, exist_ok=True)
    (dashboard_dir / "assets").mkdir(exist_ok=True)
    (dashboard_dir / "assets" / "vendor").mkdir(exist_ok=True)
    (dashboard_dir / "project").mkdir(exist_ok=True)
    (dashboard_dir / "task").mkdir(exist_ok=True)

    written = {"pages": [], "assets": []}

    (dashboard_dir / "assets" / "style.css").write_text(_STYLE, encoding="utf-8", errors="replace")
    (dashboard_dir / "assets" / "app.js").write_text(_APP_JS, encoding="utf-8", errors="replace")
    written["assets"] += ["assets/style.css", "assets/app.js"]

    # Soft delete (V0.5, point 2) : TOUTES les entités (archivées ou non)
    # reçoivent une page de détail — un lien depuis decisions.html/
    # audit.html vers une tâche archivée (ou son projet archivé) ne doit
    # jamais devenir un lien mort (voir test_dashboard_offline.py et
    # test_build_dashboard_no_dead_internal_links). Seules les LISTES de
    # navigation (cartes de index.html, tableau de tâches d'une page
    # projet) appliquent le filtre par défaut — la présence directe des
    # pages reste "toujours interrogeable", conforme à la contrainte
    # append-only du point 2.
    all_projects = list_projects_with_task_counts(conn, include_archived=True)
    visible_projects = [p for p in all_projects if not p["archived_at"]]
    (dashboard_dir / "index.html").write_text(_build_index_html(visible_projects), encoding="utf-8", errors="replace")
    written["pages"].append("index.html")

    for project in all_projects:
        all_tasks = list_tasks_for_project(conn, project["id"], include_archived=True)
        visible_tasks = [t for t in all_tasks if not t["archived_at"]]
        page = _build_project_html(project, visible_tasks)
        (dashboard_dir / "project" / f"{project['id']}.html").write_text(page, encoding="utf-8", errors="replace")
        written["pages"].append(f"project/{project['id']}.html")

        for task_row in all_tasks:
            task_page = _build_task_html(conn, task_row, project, logs_dir)
            (dashboard_dir / "task" / f"{task_row['id']}.html").write_text(task_page, encoding="utf-8", errors="replace")
            written["pages"].append(f"task/{task_row['id']}.html")

    decisions = list_human_decisions(conn)
    (dashboard_dir / "decisions.html").write_text(_build_decisions_html(decisions), encoding="utf-8", errors="replace")
    written["pages"].append("decisions.html")

    audit_entries = list_audit_log(conn)
    (dashboard_dir / "audit.html").write_text(_build_audit_html(audit_entries), encoding="utf-8", errors="replace")
    written["pages"].append("audit.html")

    # historique des rollbacks : embarqué dans audit.html via les entrées
    # audit_log/promotions correspondantes plutôt qu'une page dédiée
    # supplémentaire non demandée par la spec — disponible via
    # list_rollbacks() pour un usage futur (ex. filtre dédié).
    _ = list_rollbacks(conn)

    return written


def open_dashboard(dashboard_dir: Path) -> Path:
    """Ouvre dashboard/index.html dans le navigateur par défaut via le
    module stdlib `webbrowser` — aucun serveur, aucun port."""
    index_path = Path(dashboard_dir) / "index.html"
    webbrowser.open(index_path.resolve().as_uri())
    return index_path
