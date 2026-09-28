"""Sanitized public and team snapshots. Public output is allowlisted."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from portfolio_os.engine import public_events, public_text_allowed, utcnow
from portfolio_os.studio import material_coverage

PUBLIC_HEALTH = {
    "VISUAL_QA_PENDING": "In review",
    "NEEDS_DESIGN": "In design",
    "REVIEW_REQUIRED": "In review",
    "RELEASE_READY": "Release queued",
    "DEPLOYMENT_BLOCKED": "Release queued",
    "HEALTHY": "Public",
    "ACTIVE": "In progress",
    "DEGRADED": "In review",
    "BUILD_FAILING": "In review",
}


def _startup_public(row: sqlite3.Row) -> dict | None:
    if row["owner_private"] or not row["is_public"]:
        return None
    status = PUBLIC_HEALTH.get(row["health"], "In review")
    item = {
        "slug": row["slug"],
        "name": row["name"],
        "public_status": status,
        "url": row["website_url"] if row["website_url"] and public_text_allowed(row["website_url"]) else None,
        "github": row["github_url"] if row["github_url"] and public_text_allowed(row["github_url"]) else None,
    }
    blob = json.dumps(item)
    if not public_text_allowed(blob) and "openlegal" in blob.lower():
        return None
    return item


def build_public_snapshot(conn: sqlite3.Connection) -> dict:
    startups = []
    for row in conn.execute("SELECT * FROM startups ORDER BY slug").fetchall():
        item = _startup_public(row)
        if item:
            startups.append(item)
    activity = []
    for event in public_events(conn):
        activity.append(
            {
                "startup": None,
                "summary": event["summary"],
                "at": event["timestamp"],
                "kind": event["event_type"],
            }
        )
    # Attach slug only when the startup is public.
    enriched = []
    for event in public_events(conn):
        slug = None
        if event["startup_id"]:
            row = conn.execute("SELECT slug, is_public, owner_private FROM startups WHERE id = ?", (event["startup_id"],)).fetchone()
            if row and row["is_public"] and not row["owner_private"]:
                slug = row["slug"]
        enriched.append({"startup": slug, "summary": event["summary"], "at": event["timestamp"], "kind": event["event_type"]})
    in_review = sum(1 for item in startups if item["public_status"] == "In review")
    payload = {
        "generated_at": utcnow(),
        "pulse": {
            "public_projects": len(startups),
            "in_review": in_review,
            "public_notes": len(enriched),
        },
        "now_building": [
            {"startup": item["slug"], "name": item["name"], "public_status": item["public_status"]}
            for item in startups
            if item["slug"] in {
                "gh0st",
                "1bc",
                "access-layer",
                "agentapi-hub",
                "autobuilder",
                "autoerp",
                "behindcurtain",
                "bioyield-labs",
                "blitzproof",
                "blitzunicorn",
                "brandcrossover",
            }
        ],
        "activity": enriched,
        "built_this_week": [
            item
            for item in enriched
            if item["kind"]
            in {
                "public_designing",
                "public_building",
                "public_testing",
                "public_shipped",
                "public_design_update",
                "public_researching",
                "public_experimenting",
            }
        ][:8],
        "startups": startups,
    }
    encoded = json.dumps(payload)
    if "PRIVATE_SYSTEM" in encoded or "openlegal" in encoded.lower() or "/Users/" in encoded:
        raise RuntimeError("public snapshot failed the sanitizer")
    return payload


def build_team_snapshot(conn: sqlite3.Connection) -> dict:
    health_counts: dict[str, int] = {}
    startups = []
    for row in conn.execute("SELECT * FROM startups WHERE owner_private = 0 ORDER BY priority DESC, slug").fetchall():
        health_counts[row["health"]] = health_counts.get(row["health"], 0) + 1
        open_work = conn.execute(
            """
            SELECT id, type, title, status, assigned_role, priority, blocked_reason
            FROM work_items
            WHERE startup_id = ? AND status NOT IN ('completed')
            ORDER BY priority DESC
            LIMIT 8
            """,
            (row["id"],),
        ).fetchall()
        dossier = None
        raw_facts = conn.execute("SELECT facts FROM dossiers WHERE startup_id = ?", (row["id"],)).fetchone()
        if raw_facts:
            try:
                dossier = json.loads(raw_facts["facts"])
            except json.JSONDecodeError:
                dossier = None
            if dossier and ("/Users/" in json.dumps(dossier) or "openlegal" in json.dumps(dossier).lower()):
                dossier = None
        startups.append(
            {
                "slug": row["slug"],
                "name": row["name"],
                "health": row["health"],
                "priority": row["priority"],
                "website_url": row["website_url"],
                "open_work": [dict(item) for item in open_work],
                "dossier": dossier,
            }
        )
    queue = [
        dict(row)
        for row in conn.execute(
            """
            SELECT work_items.id, startups.slug, work_items.type, work_items.title,
                   work_items.status, work_items.assigned_role, work_items.priority,
                   work_items.blocked_reason, work_items.priority_factors
            FROM work_items JOIN startups ON startups.id = work_items.startup_id
            WHERE work_items.status NOT IN ('completed') AND startups.owner_private = 0
            ORDER BY work_items.priority DESC, work_items.id
            LIMIT 40
            """
        ).fetchall()
    ]
    events = [
        dict(row)
        for row in conn.execute(
            """
            SELECT timestamp, actor, event_type, summary, visibility
            FROM events
            WHERE visibility IN ('TEAM', 'PUBLIC')
            ORDER BY id DESC LIMIT 40
            """
        ).fetchall()
    ]
    reports = [
        dict(row)
        for row in conn.execute(
            """
            SELECT kind, period, created_at, body FROM reports
            WHERE visibility = 'TEAM' ORDER BY id DESC LIMIT 4
            """
        ).fetchall()
    ]
    from pathlib import Path
    import time

    from portfolio_os.daemon import control_plane_commit
    from portfolio_os.dossier import SCHEMA_VERSION

    beat = Path(__file__).resolve().parents[1] / "data" / "daemon.heartbeat"
    daemon = "offline"
    daemon_commit = None
    daemon_schema = None
    if beat.is_file() and time.time() - beat.stat().st_mtime < 600:
        raw = beat.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            daemon = parsed.get("status") or "running"
            daemon_commit = parsed.get("commit")
            daemon_schema = parsed.get("schema")
        else:
            daemon = "running"
    control_commit = control_plane_commit()
    agents = [
        dict(row)
        for row in conn.execute(
            """
            SELECT agent_runs.role, agent_runs.status, agent_runs.result_summary,
                   agent_runs.started_at, startups.slug
            FROM agent_runs
            LEFT JOIN work_items ON work_items.id = agent_runs.work_item_id
            LEFT JOIN startups ON startups.id = work_items.startup_id
            ORDER BY agent_runs.id DESC
            LIMIT 24
            """
        ).fetchall()
    ]
    payload = {
        "generated_at": utcnow(),
        "schema": SCHEMA_VERSION,
        "daemon": daemon,
        "daemon_commit": daemon_commit,
        "daemon_schema": daemon_schema,
        "control_plane": {"commit": control_commit, "schema": SCHEMA_VERSION},
        "agents": agents,
        "health": health_counts,
        "queue": queue,
        "startups": startups,
        "activity": events,
        "reports": reports,
        "material": material_coverage(conn),
        "deployment_blocked": [
            dict(row)
            for row in conn.execute(
                """
                SELECT startups.slug, deployments.status, deployments.blocker, deployments.commit_sha, deployments.url
                FROM deployments JOIN startups ON startups.id = deployments.startup_id
                WHERE deployments.status = 'blocked' AND startups.owner_private = 0
                ORDER BY deployments.id DESC LIMIT 20
                """
            ).fetchall()
        ],
    }
    encoded = json.dumps(payload)
    if "openlegal" in encoded.lower() or "/Users/" in encoded:
        raise RuntimeError("team snapshot contained excluded or local path data")
    return payload


def write_snapshots(conn: sqlite3.Connection, publish_dir: Path) -> tuple[Path, Path]:
    publish_dir.mkdir(parents=True, exist_ok=True)
    public = build_public_snapshot(conn)
    team = build_team_snapshot(conn)
    public_path = publish_dir / "public.json"
    team_path = publish_dir / "team.json"
    public_path.write_text(json.dumps(public, indent=2) + "\n", encoding="utf-8")
    team_path.write_text(json.dumps(team, indent=2) + "\n", encoding="utf-8")
    conn.execute(
        "INSERT INTO snapshots (kind, created_at, payload) VALUES ('public', ?, ?)",
        (public["generated_at"], json.dumps(public)),
    )
    conn.execute(
        "INSERT INTO snapshots (kind, created_at, payload) VALUES ('team', ?, ?)",
        (team["generated_at"], json.dumps({"startups": len(team["startups"]), "queue": len(team["queue"])})),
    )
    return public_path, team_path


def write_report(conn: sqlite3.Connection, kind: str) -> str:
    from portfolio_os.engine import refresh_priorities

    refresh_priorities(conn)
    health = {}
    for row in conn.execute(
        "SELECT health, COUNT(*) AS n FROM startups WHERE owner_private = 0 GROUP BY health"
    ):
        health[row["health"]] = row["n"]
    completed = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE status = 'completed'"
    ).fetchone()["n"]
    reopened = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE status = 'reopened'"
    ).fetchone()["n"]
    blocked = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE status = 'blocked'"
    ).fetchone()["n"]
    next_work = conn.execute(
        """
        SELECT startups.slug, work_items.title, work_items.assigned_role, work_items.priority
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE work_items.status = 'queued' AND startups.owner_private = 0
        ORDER BY work_items.priority DESC LIMIT 8
        """
    ).fetchall()
    lines = [
        f"# Portfolio {kind} report",
        "",
        "## Health",
    ]
    for key, value in sorted(health.items()):
        lines.append(f"- {key}: {value}")
    lines += [
        "",
        f"Completed work items: {completed}",
        f"Reopened work items: {reopened}",
        f"Blocked work items: {blocked}",
        "",
        "## Next",
    ]
    for row in next_work:
        lines.append(f"- {row['slug']}: {row['title']} ({row['assigned_role']}, {row['priority']})")
    body = "\n".join(lines) + "\n"
    conn.execute(
        """
        INSERT INTO reports (kind, period, created_at, visibility, body)
        VALUES (?, ?, ?, 'TEAM', ?)
        """,
        (kind, utcnow()[:10], utcnow(), body),
    )
    return body
