"""Local browser for the portfolio engine. It does not replace Team."""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

from portfolio_os.daemon import control_plane_commit, daemon_is_fresh, worker_plan
from portfolio_os.squads import REASONING_MODEL

NAV = (
    ("/", "Engine"),
    ("/workers", "Workers"),
    ("/agents", "Agents"),
    ("/queue", "Queue"),
    ("/coverage", "Coverage"),
    ("/startups", "Startups"),
    ("/previews", "Previews"),
    ("/releases", "Releases"),
    ("/providers/vercel", "Vercel"),
    ("/workspaces", "Workspaces"),
    ("/events", "Events"),
    ("/system", "System"),
)


def _e(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, args).fetchall()


def _heartbeat(root: Path) -> dict:
    path = root / "data" / "daemon.heartbeat"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def page(conn: sqlite3.Connection, path: str, root: Path) -> str:
    routes = {
        "/": _home,
        "/workers": _workers,
        "/agents": _agents,
        "/queue": _queue,
        "/coverage": _coverage,
        "/startups": _startups,
        "/previews": _previews,
        "/releases": _releases,
        "/providers/vercel": _vercel,
        "/workspaces": _workspaces,
        "/events": _events,
        "/system": _system,
    }
    if path.startswith("/startups/"):
        body = _startup(conn, path.removeprefix("/startups/"), root)
    else:
        builder = routes.get(path)
        body = builder(conn, root) if builder else "<h1>Not found</h1>"
    links = "".join(f'<a href="{href}"{" class=here" if href == path else ""}>{label}</a>' for href, label in NAV)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Portfolio OS</title>
<style>
:root {{ color-scheme: dark; }}
body {{ margin: 0; background: #10140f; color: #e7efe2; font: 15px/1.45 "Iowan Old Style", Palatino, serif; }}
header, main {{ width: min(1100px, calc(100% - 32px)); margin: 0 auto; }}
header {{ padding: 18px 0 8px; border-bottom: 1px solid #314033; }}
nav {{ display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 8px; }}
a {{ color: #d7e38a; text-decoration: none; }}
a.here {{ color: #fff; }}
h1 {{ font-weight: 500; letter-spacing: 0; }}
section {{ margin: 22px 0; }}
table {{ width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace; font-size: 12px; }}
td, th {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #2a3328; vertical-align: top; }}
button {{ background: #d7e38a; color: #172012; border: 0; padding: 6px 10px; font: inherit; cursor: pointer; }}
.muted {{ color: #9aab96; }}
form {{ display: inline; }}
img {{ max-width: 280px; height: auto; background: #000; }}
</style></head><body>
<header><strong>Portfolio OS</strong> <span class="muted">engine, not the studio</span>
{_version_line(root)}
<p><a href="http://127.0.0.1:4320">Open Team</a></p>
<nav>{links}</nav></header>
<main>{body}</main></body></html>"""


def _version_line(root: Path) -> str:
    beat = _heartbeat(root)
    loaded = beat.get("commit") or "unknown"
    head = control_plane_commit(root)
    warn = ""
    if loaded != head:
        warn = "<strong>CONTROL PLANE UPDATE REQUIRED.</strong> "
    return f"<p>{warn}HEAD {_e(head)} · DAEMON {_e(loaded)} · MODEL {_e(beat.get('model') or 'grok-4.7')} · SCHEMA {_e(beat.get('schema', ''))}</p>"


def login_page(failed: bool = False) -> str:
    note = "<p>That token did not match.</p>" if failed else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Portfolio OS</title></head>
<body style="background:#10140f;color:#e7efe2;font:16px/1.4 Palatino,serif;padding:48px">
<h1>Portfolio OS</h1>
<p>Local control plane. This page is not the venture studio.</p>
{note}
<form method="post" action="/login">
<label>API token <input name="token" type="password" autocomplete="current-password" required></label>
<button type="submit">Open</button>
</form>
</body></html>"""


def _home(conn: sqlite3.Connection, root: Path) -> str:
    beat = _heartbeat(root)
    lanes = beat.get("lanes") or worker_plan()
    fresh = daemon_is_fresh(root / "data" / "daemon.heartbeat")
    locks = _rows(
        conn,
        """
        SELECT startups.slug, locks.holder, locks.acquired_at
        FROM locks JOIN startups ON startups.id = locks.startup_id
        WHERE startups.owner_private = 0
        """,
    )
    queue = _rows(
        conn,
        """
        SELECT startups.slug, work_items.priority, work_items.title, work_items.assigned_role
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE work_items.status = 'queued' AND startups.owner_private = 0
        ORDER BY work_items.priority DESC LIMIT 8
        """,
    )
    previews = _rows(
        conn,
        """
        SELECT startups.slug, preview_runs.status, preview_runs.port, preview_runs.commit_sha
        FROM preview_runs JOIN startups ON startups.id = preview_runs.startup_id
        WHERE preview_runs.status IN ('STARTING', 'RUNNING') AND startups.owner_private = 0
        """,
    )
    coverage = conn.execute("SELECT COUNT(*) AS n FROM startups WHERE owner_private = 0 AND is_public = 1").fetchone()["n"]
    shards = conn.execute("SELECT COUNT(DISTINCT shard) AS n FROM startup_coverage").fetchone()["n"]
    return f"""
<h1>Engine</h1>
<section>
<p>Daemon {"running" if fresh else "not fresh"} · loaded {_e(beat.get("commit") or "unknown")} · head {_e(control_plane_commit(root))}</p>
<p>Schema {_e(beat.get("schema", "unknown"))} · model {_e(beat.get("model") or REASONING_MODEL)} · mutation {_e(lanes.get("mutation"))} · reviewer {_e(lanes.get("reviewer"))}</p>
</section>
<section><h2>Active locks</h2>{_table(["startup", "holder", "since"], [(r["slug"], r["holder"], r["acquired_at"]) for r in locks])}</section>
<section><h2>Queue</h2>{_table(["startup", "priority", "role", "task"], [(r["slug"], r["priority"], r["assigned_role"], r["title"]) for r in queue])}</section>
<section><h2>Previews</h2>{_table(["startup", "status", "port", "commit"], [(r["slug"], r["status"], r["port"], r["commit_sha"]) for r in previews])}</section>
<section><h2>Coverage</h2><p>{coverage} public startups · {shards} shards</p><p><a href="/coverage">Open the cycle</a></p></section>
"""


def _workers(conn: sqlite3.Connection, root: Path) -> str:
    beat = _heartbeat(root)
    lanes = beat.get("lanes") or worker_plan()
    running = _rows(
        conn,
        """
        SELECT startups.slug, agent_runs.role, agent_runs.model, agent_runs.status, agent_runs.started_at, work_items.title
        FROM agent_runs
        LEFT JOIN work_items ON work_items.id = agent_runs.work_item_id
        LEFT JOIN startups ON startups.id = work_items.startup_id
        WHERE agent_runs.status = 'running' AND (startups.owner_private = 0 OR startups.id IS NULL)
        ORDER BY agent_runs.id DESC
        """,
    )
    body = "".join(
        f"<p>{_e(row['role'])} · {_e(row['slug'])} · {_e(row['model'] or REASONING_MODEL)} · {_e(row['title'])} · since {_e(row['started_at'])}</p>"
        for row in running
    ) or "<p>No agent run is currently marked running.</p>"
    return f"""<h1>Workers</h1>
<p>Capacity: {lanes.get("mutation", 0)} mutation, {lanes.get("reviewer", 0)} reviewer. Model {_e(beat.get("model") or REASONING_MODEL)}.</p>
{body}"""


def _agents(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT startups.slug, agent_runs.role, agent_runs.model, agent_runs.status, agent_runs.result_summary,
               agent_runs.files_changed, agent_runs.commits, agent_runs.started_at
        FROM agent_runs
        LEFT JOIN work_items ON work_items.id = agent_runs.work_item_id
        LEFT JOIN startups ON startups.id = work_items.startup_id
        WHERE startups.owner_private = 0 OR startups.id IS NULL
        ORDER BY agent_runs.id DESC LIMIT 40
        """,
    )
    return "<h1>Agent runs</h1>" + _table(
        ["startup", "role", "model", "status", "result", "commit"],
        [(r["slug"], r["role"], r["model"] or REASONING_MODEL, r["status"], (r["result_summary"] or "")[:180], r["commits"]) for r in rows],
    )


def _queue(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT work_items.id, startups.slug, work_items.priority, work_items.title, work_items.assigned_role,
               work_items.status, work_items.blocked_reason, work_items.created_at
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE startups.owner_private = 0 AND work_items.status IN ('queued', 'paused', 'blocked', 'running')
        ORDER BY work_items.priority DESC LIMIT 40
        """,
    )
    lines = []
    for row in rows:
        controls = ""
        if row["status"] in {"queued", "paused", "blocked"}:
            controls = (
                _action(row["slug"], "pause_task", "Pause", item=row["id"])
                + _action(row["slug"], "resume_task", "Resume", item=row["id"])
                + _action(row["slug"], "cancel_task", "Cancel", item=row["id"])
            )
        lines.append((row["priority"], row["slug"], row["assigned_role"], row["status"], row["title"], row["blocked_reason"], controls))
    return "<h1>Queue</h1>" + _table(["priority", "startup", "role", "status", "task", "blocker", ""], lines, raw_last=True)


def _coverage(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT startup_coverage.shard, COUNT(*) AS n
        FROM startup_coverage JOIN startups ON startups.id = startup_coverage.startup_id
        WHERE startups.owner_private = 0
        GROUP BY startup_coverage.shard ORDER BY startup_coverage.shard
        """,
    )
    improved = conn.execute("SELECT COUNT(DISTINCT startup_id) AS n FROM material_improvements").fetchone()["n"]
    return f"<h1>Coverage</h1><p>Material improvements recorded: {improved}</p>" + _table(
        ["shard", "startups"], [(r["shard"], r["n"]) for r in rows]
    )


def _startups(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT slug, health, priority, category FROM startups
        WHERE owner_private = 0 ORDER BY priority DESC, slug LIMIT 160
        """,
    )
    return "<h1>Startups</h1><p>Technical index. Product review stays in Team.</p>" + _table(
        ["startup", "health", "priority", "category"],
        [(f'<a href="/startups/{_e(r["slug"])}">{_e(r["slug"])}</a>', r["health"], r["priority"], r["category"]) for r in rows],
        raw_first=True,
    )


def _startup(conn: sqlite3.Connection, slug: str, root: Path) -> str:
    del root
    row = conn.execute("SELECT * FROM startups WHERE slug = ? AND owner_private = 0", (slug,)).fetchone()
    if row is None:
        return "<h1>Not found</h1>"
    preview = conn.execute(
        "SELECT status, port, commit_sha, branch, pid FROM preview_runs WHERE startup_id = ? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    gate = conn.execute(
        "SELECT commit_sha, visual, release_approval, deployment FROM release_gates WHERE startup_id = ? ORDER BY id DESC LIMIT 1",
        (row["id"],),
    ).fetchone()
    preview_line = "none"
    if preview is not None:
        preview_line = f"{preview['status']} {preview['branch']} {preview['commit_sha']} port {preview['port']} pid {preview['pid']}"
    release_line = "none"
    if gate is not None:
        release_line = f"{gate['commit_sha']} visual {gate['visual']} approval {gate['release_approval']} deployment {gate['deployment']}"
    return f"""<h1>{_e(row["name"])}</h1>
<p><a href="http://127.0.0.1:3000/startups/{_e(slug)}">View in Team</a></p>
<p>Health {_e(row["health"])} · priority {_e(row["priority"])} · {_e(row["category"])}</p>
<p>Repo path is kept off this page. Branch and commit come from the last preview run.</p>
<p>Preview: {_e(preview_line)}</p>
<p>Release: {_e(release_line)}</p>
<p>{_action(slug, "start_preview", "Start")} {_action(slug, "stop_preview", "Stop")} {_action(slug, "capture", "Capture")}</p>
"""


def _previews(conn: sqlite3.Connection, root: Path) -> str:
    rows = _rows(
        conn,
        """
        SELECT startups.slug, preview_runs.surface, preview_runs.branch, preview_runs.commit_sha,
               preview_runs.port, preview_runs.pid, preview_runs.status, preview_runs.started_at, preview_runs.last_access
        FROM preview_runs JOIN startups ON startups.id = preview_runs.startup_id
        WHERE startups.owner_private = 0
        ORDER BY preview_runs.id DESC LIMIT 20
        """,
    )
    evidence = root / "evidence"
    lines = []
    for row in rows:
        desktop = evidence / row["slug"] / "desktop.png"
        image = f'<img src="/evidence/{_e(row["slug"])}/desktop.png" alt="">' if desktop.is_file() else "no shot"
        lines.append(
            (
                row["slug"],
                row["status"],
                row["branch"],
                row["commit_sha"],
                row["port"],
                row["pid"],
                image,
                _action(row["slug"], "start_preview", "Start")
                + _action(row["slug"], "stop_preview", "Stop")
                + _action(row["slug"], "capture", "Capture")
                + (f'<a href="http://127.0.0.1:{row["port"]}">Open</a>' if row["status"] == "RUNNING" and row["port"] else ""),
            )
        )
    return "<h1>Previews</h1>" + _table(
        ["startup", "status", "branch", "commit", "port", "pid", "desktop", ""],
        lines,
        raw_last=True,
        raw_index=6,
    )


def _releases(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT startups.slug, release_gates.commit_sha, release_gates.visual, release_gates.release_approval, release_gates.deployment
        FROM release_gates JOIN startups ON startups.id = release_gates.startup_id
        WHERE startups.owner_private = 0 ORDER BY release_gates.id DESC LIMIT 30
        """,
    )
    queued = _rows(
        conn,
        """
        SELECT startups.slug, deployments.commit_sha, deployments.status, deployments.blocker
        FROM deployments JOIN startups ON startups.id = deployments.startup_id
        WHERE startups.owner_private = 0 ORDER BY deployments.id DESC LIMIT 20
        """,
    )
    return "<h1>Releases</h1><p>What the engine will do. Team answers what you can ship.</p>" + _table(
        ["startup", "commit", "visual", "approval", "deployment"],
        [(r["slug"], r["commit_sha"], r["visual"], r["release_approval"], r["deployment"]) for r in rows],
    ) + "<h2>Provider rows</h2>" + _table(
        ["startup", "commit", "status", "blocker"],
        [(r["slug"], r["commit_sha"], r["status"], r["blocker"]) for r in queued],
    )


def _vercel(conn: sqlite3.Connection, root: Path) -> str:
    del root
    count = conn.execute("SELECT COUNT(*) AS n FROM deployments WHERE provider = 'vercel'").fetchone()["n"]
    blocked = conn.execute(
        "SELECT COUNT(*) AS n FROM deployments WHERE provider = 'vercel' AND status = 'blocked'"
    ).fetchone()["n"]
    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM deployments WHERE provider = 'vercel' AND status = 'queued'"
    ).fetchone()["n"]
    return f"""<h1>Vercel</h1>
<p>Plan: Hobby. Known limits used for scheduling: about 200 projects, 100 deployments / 24h, 1 concurrent build.</p>
<p>Portfolio OS rows: {count}. Queued: {queued}. Blocked: {blocked}.</p>
<p>Soft autonomous budget 70. Reserve 30. Remaining quota is not shown unless the provider returns it.</p>
<p>This process does not deploy.</p>"""


def _workspaces(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT startups.slug, dossiers.facts FROM dossiers
        JOIN startups ON startups.id = dossiers.startup_id
        WHERE startups.owner_private = 0
        ORDER BY startups.slug LIMIT 160
        """,
    )
    lines = []
    for row in rows:
        try:
            facts = json.loads(row["facts"])
        except json.JSONDecodeError:
            continue
        status = facts.get("workspace_status") or facts.get("workspace")
        if status:
            lines.append((row["slug"], status))
    return "<h1>Workspaces</h1><p>Classes already stored on the dossier. This view does not scan repositories.</p>" + _table(
        ["startup", "workspace"], lines
    )


def _events(conn: sqlite3.Connection, root: Path) -> str:
    del root
    rows = _rows(
        conn,
        """
        SELECT events.timestamp, startups.slug, events.actor, events.event_type, events.visibility, events.summary
        FROM events LEFT JOIN startups ON startups.id = events.startup_id
        WHERE events.visibility != 'PRIVATE_SYSTEM' AND (startups.owner_private = 0 OR startups.id IS NULL)
        ORDER BY events.id DESC LIMIT 40
        """,
    )
    return "<h1>Events</h1>" + _table(
        ["when", "startup", "actor", "type", "visibility", "summary"],
        [(r["timestamp"], r["slug"], r["actor"], r["event_type"], r["visibility"], (r["summary"] or "")[:180]) for r in rows],
    )


def _system(conn: sqlite3.Connection, root: Path) -> str:
    beat = _heartbeat(root)
    fresh = daemon_is_fresh(root / "data" / "daemon.heartbeat")
    db = root / "data" / "portfolio.db"
    return f"""<h1>System</h1>
<p>Database {"present" if db.is_file() else "missing"}.</p>
<p>Daemon {"fresh" if fresh else "stale or stopped"} · loaded {_e(beat.get("commit"))} · head {_e(control_plane_commit(root))}.</p>
<p>Schema {_e(beat.get("schema"))} · model {_e(beat.get("model") or REASONING_MODEL)}.</p>
<p>Bind is 127.0.0.1. This UI is not for a public hostname.</p>
<p>Team feed is publish/team.json. Labs feed is the public snapshot.</p>"""


def _action(slug: str, action: str, label: str, item: int | None = None) -> str:
    hidden = f'<input type="hidden" name="work_item" value="{item}">' if item else ""
    return (
        f'<form method="post" action="/ui/action"><input type="hidden" name="slug" value="{_e(slug)}">'
        f'<input type="hidden" name="action" value="{_e(action)}">{hidden}'
        f'<button type="submit">{_e(label)}</button></form> '
    )


def _table(headers: list[str], rows: list[tuple], raw_last: bool = False, raw_first: bool = False, raw_index: int | None = None) -> str:
    if not rows:
        return "<p class=muted>None recorded.</p>"
    head = "".join(f"<th>{_e(item)}</th>" for item in headers)
    body = []
    for row in rows:
        cells = []
        for index, cell in enumerate(row):
            raw = (raw_first and index == 0) or (raw_last and index == len(row) - 1) or index == raw_index
            cells.append(f"<td>{cell if raw else _e(cell)}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"
