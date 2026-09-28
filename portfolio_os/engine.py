"""Roles, gates, locks, reviews, priority, and visibility."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from portfolio_os.exclusion import OWNER_PRIVATE_LABEL

ROLES = (
    "PORTFOLIO_DIRECTOR",
    "FOUNDER",
    "SOFTWARE_ENGINEER",
    "DESIGNER",
    "FRONTEND_ENGINEER",
    "QA_ENGINEER",
    "VISUAL_REVIEWER",
    "PRODUCT_REVIEWER",
    "GITHUB_DEVREL",
    "RELEASE_ENGINEER",
    "SECURITY_REVIEWER",
    "NOAERTH_EDITOR",
)

VISIBILITIES = ("PUBLIC", "TEAM", "PRIVATE_SYSTEM")

# Allowlist. Anything else is stored as TEAM.
PUBLIC_EVENT_TYPES = frozenset(
    {
        "public_milestone",
        "public_release",
        "public_launch",
        "public_design_update",
        "public_research_note",
    }
)

_FORBIDDEN_PUBLIC = re.compile(
    r"(/Users/|openlegal|PRIVATE_SYSTEM|api[_-]?key|\btoken\b|\bpassword\b|\bsecret\b|BEGIN [A-Z ]*PRIVATE)",
    re.I,
)


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def public_text_allowed(text: str) -> bool:
    if not text or not text.strip():
        return False
    if _FORBIDDEN_PUBLIC.search(text):
        return False
    if "OWNER-PRIVATE" in text and text.strip() != OWNER_PRIVATE_LABEL:
        return False
    return True


def add_event(
    conn: sqlite3.Connection,
    *,
    actor: str,
    event_type: str,
    summary: str,
    visibility: str,
    startup_id: int | None = None,
    metadata: dict | None = None,
) -> int:
    if visibility not in VISIBILITIES:
        visibility = "PRIVATE_SYSTEM"
    if visibility == "PUBLIC":
        if event_type not in PUBLIC_EVENT_TYPES or not public_text_allowed(summary):
            visibility = "TEAM"
            add_event(
                conn,
                actor="sanitizer",
                event_type="public_rejected",
                summary="A proposed public event was withheld.",
                visibility="PRIVATE_SYSTEM",
                startup_id=startup_id,
            )
    meta = json.dumps(metadata or {}, sort_keys=True)
    # Never persist absolute paths or the excluded directory name in metadata.
    if _FORBIDDEN_PUBLIC.search(meta):
        meta = "{}"
        if visibility == "PUBLIC":
            visibility = "TEAM"
    cur = conn.execute(
        """
        INSERT INTO events (timestamp, startup_id, actor, event_type, summary, visibility, metadata)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (utcnow(), startup_id, actor, event_type, summary, visibility, meta),
    )
    return int(cur.lastrowid)


def startup_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM startups WHERE slug = ?", (slug,)).fetchone()


def claim_lock(
    conn: sqlite3.Connection,
    startup_id: int,
    holder: str,
    minutes: int = 60,
    work_item_id: int | None = None,
    run_id: str | None = None,
) -> None:
    now = datetime.now(timezone.utc)
    row = conn.execute("SELECT * FROM locks WHERE startup_id = ?", (startup_id,)).fetchone()
    if row is not None:
        expires = datetime.fromisoformat(row["expires_at"])
        if expires > now and row["holder"] != holder:
            raise RuntimeError(f"startup locked by {row['holder']} until {row['expires_at']}")
    expires_at = (now + timedelta(minutes=minutes)).replace(microsecond=0).isoformat()
    conn.execute(
        """
        INSERT INTO locks (startup_id, holder, acquired_at, expires_at, work_item_id, run_id)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(startup_id) DO UPDATE SET
          holder = excluded.holder,
          acquired_at = excluded.acquired_at,
          expires_at = excluded.expires_at,
          work_item_id = excluded.work_item_id,
          run_id = excluded.run_id
        """,
        (startup_id, holder, now.replace(microsecond=0).isoformat(), expires_at, work_item_id, run_id),
    )


def release_lock(conn: sqlite3.Connection, startup_id: int, holder: str) -> None:
    conn.execute(
        "DELETE FROM locks WHERE startup_id = ? AND holder = ?",
        (startup_id, holder),
    )


def ensure_work(
    conn: sqlite3.Connection,
    startup_id: int,
    *,
    type_: str,
    title: str,
    role: str,
    priority: int = 50,
    description: str = "",
    status: str = "queued",
    blocked_reason: str | None = None,
    parent: int | None = None,
) -> int:
    existing = conn.execute(
        """
        SELECT id FROM work_items
        WHERE startup_id = ? AND type = ? AND title = ? AND status NOT IN ('completed')
        """,
        (startup_id, type_, title),
    ).fetchone()
    if existing:
        return int(existing["id"])
    cur = conn.execute(
        """
        INSERT INTO work_items (
          startup_id, type, title, description, priority, status, assigned_role,
          parent_work_item, created_at, blocked_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            startup_id,
            type_,
            title,
            description,
            priority,
            status,
            role,
            parent,
            utcnow(),
            blocked_reason,
        ),
    )
    return int(cur.lastrowid)


def score_work(item: sqlite3.Row, startup: sqlite3.Row) -> tuple[int, dict]:
    factors: dict[str, int] = {}
    factors["base"] = 10
    if item["type"] in {"visual_qa_desktop", "visual_qa_mobile", "design_review"}:
        factors["user_facing"] = 30
    elif item["type"] in {"product_review", "historical_review"}:
        factors["user_facing"] = 20
    elif item["type"] == "deploy_retry":
        factors["user_facing"] = 5
    else:
        factors["user_facing"] = 15
    factors["public_visibility"] = 15 if startup["is_public"] else 0
    if startup["health"] == "VISUAL_QA_PENDING":
        factors["visual_regression_risk"] = 25
    if item["status"] == "blocked":
        factors["blocker_hold"] = -40
    if startup["slug"] in {
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
        "gh0st",
    }:
        factors["named_recovery_batch"] = 12
    created = datetime.fromisoformat(item["created_at"])
    age_hours = int((datetime.now(timezone.utc) - created).total_seconds() // 3600)
    factors["age_hours_capped"] = min(age_hours, 48)
    total = sum(factors.values())
    return total, factors


def refresh_priorities(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT work_items.*, startups.slug, startups.is_public, startups.health, startups.owner_private
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE work_items.status NOT IN ('completed') AND startups.owner_private = 0
        """
    ).fetchall()
    for row in rows:
        total, factors = score_work(row, row)
        conn.execute(
            "UPDATE work_items SET priority = ?, priority_factors = ? WHERE id = ?",
            (total, json.dumps(factors, sort_keys=True), row["id"]),
        )
        conn.execute(
            "UPDATE startups SET priority = MAX(priority, ?) WHERE id = ?",
            (total, row["startup_id"]),
        )


def visual_gate_passed(conn: sqlite3.Connection, startup_id: int) -> bool:
    rows = conn.execute(
        """
        SELECT dimension FROM reviews
        WHERE startup_id = ? AND reviewer_role = 'VISUAL_REVIEWER' AND resolution = 'pass'
        """,
        (startup_id,),
    ).fetchall()
    found = {row["dimension"] for row in rows}
    return {"desktop", "mobile"} <= found


def recompute_health(conn: sqlite3.Connection, startup_id: int) -> str:
    startup = conn.execute("SELECT * FROM startups WHERE id = ?", (startup_id,)).fetchone()
    if startup is None:
        raise KeyError(startup_id)
    if startup["owner_private"]:
        conn.execute(
            "UPDATE startups SET health = ? WHERE id = ?",
            ("OWNER_PRIVATE", startup_id),
        )
        return "OWNER_PRIVATE"
    open_items = conn.execute(
        """
        SELECT type, status FROM work_items
        WHERE startup_id = ? AND status NOT IN ('completed')
        """,
        (startup_id,),
    ).fetchall()
    types = {row["type"] for row in open_items}
    gate = visual_gate_passed(conn, startup_id)
    visual_open = bool(types & {"visual_qa_desktop", "visual_qa_mobile"})
    if visual_open:
        health = "VISUAL_QA_PENDING"
    elif "design_review" in types:
        health = "NEEDS_DESIGN"
    elif "historical_review" in types or not gate:
        health = "REVIEW_REQUIRED" if "historical_review" in types or not startup["is_public"] else "VISUAL_QA_PENDING"
    else:
        blocked = conn.execute(
            """
            SELECT 1 FROM deployments
            WHERE startup_id = ? AND status = 'blocked'
            ORDER BY id DESC LIMIT 1
            """,
            (startup_id,),
        ).fetchone()
        open_release = bool(types & {"deploy_retry", "release"})
        if blocked and open_release:
            health = "RELEASE_READY"
        else:
            product_fail = conn.execute(
                """
                SELECT 1 FROM reviews
                WHERE startup_id = ? AND reviewer_role = 'PRODUCT_REVIEWER' AND resolution = 'fail'
                  AND id = (
                    SELECT MAX(id) FROM reviews
                    WHERE startup_id = ? AND reviewer_role = 'PRODUCT_REVIEWER'
                  )
                """,
                (startup_id, startup_id),
            ).fetchone()
            health = "DEGRADED" if product_fail else "HEALTHY"
    if health == "HEALTHY" and not gate:
        health = "VISUAL_QA_PENDING"
    conn.execute(
        "UPDATE startups SET health = ?, last_reviewed_at = ? WHERE id = ?",
        (health, utcnow(), startup_id),
    )
    return health


def record_review(
    conn: sqlite3.Connection,
    *,
    slug: str,
    role: str,
    dimension: str,
    finding: str,
    severity: str,
    resolution: str,
    work_item_id: int | None = None,
) -> dict:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    startup = startup_by_slug(conn, slug)
    if startup is None or startup["owner_private"]:
        raise ExclusionErrorPublic()
    conn.execute(
        """
        INSERT INTO reviews (
          startup_id, work_item_id, reviewer_role, dimension, finding, severity, resolution, timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (startup["id"], work_item_id, role, dimension, finding, severity, resolution, utcnow()),
    )
    add_event(
        conn,
        actor=role,
        event_type="review_recorded",
        summary=f"{dimension} review recorded.",
        visibility="TEAM",
        startup_id=startup["id"],
    )
    follow_up = None
    if resolution == "fail":
        follow_role = "FRONTEND_ENGINEER" if dimension in {"desktop", "mobile"} else "DESIGNER"
        follow_up = ensure_work(
            conn,
            startup["id"],
            type_="follow_up",
            title=f"Fix {dimension}: {finding[:80]}",
            role=follow_role,
            priority=80,
            description=finding,
            parent=work_item_id,
        )
        if work_item_id is not None:
            conn.execute(
                "UPDATE work_items SET status = 'reopened', completed_at = NULL WHERE id = ?",
                (work_item_id,),
            )
        add_event(
            conn,
            actor="PORTFOLIO_DIRECTOR",
            event_type="follow_up_created",
            summary="A failed review opened follow-up work.",
            visibility="TEAM",
            startup_id=startup["id"],
        )
    elif resolution == "pass" and work_item_id is not None:
        item = conn.execute("SELECT * FROM work_items WHERE id = ?", (work_item_id,)).fetchone()
        review_types = {
            "visual_qa_desktop",
            "visual_qa_mobile",
            "design_review",
            "product_review",
            "historical_review",
        }
        if item is not None and item["type"] in review_types and item["assigned_role"] == role:
            conn.execute(
                "UPDATE work_items SET status = 'completed', completed_at = ? WHERE id = ?",
                (utcnow(), work_item_id),
            )
        elif item is not None and item["assigned_role"] != role:
            conn.execute(
                "UPDATE work_items SET status = 'completed', completed_at = ? WHERE id = ?",
                (utcnow(), work_item_id),
            )
    health = recompute_health(conn, startup["id"])
    return {"health": health, "follow_up_id": follow_up}


class ExclusionErrorPublic(RuntimeError):
    pass


def block_release(conn: sqlite3.Connection, slug: str, reason: str, commit_sha: str = "", url: str = "") -> str:
    startup = startup_by_slug(conn, slug)
    if startup is None or startup["owner_private"]:
        raise ExclusionErrorPublic()
    conn.execute(
        """
        UPDATE work_items
        SET status = 'blocked', blocked_reason = ?
        WHERE startup_id = ? AND type IN ('release', 'deploy_retry') AND status NOT IN ('completed')
        """,
        (reason, startup["id"]),
    )
    ensure_work(
        conn,
        startup["id"],
        type_="deploy_retry",
        title="Deploy after capacity is available",
        role="RELEASE_ENGINEER",
        status="blocked",
        blocked_reason=reason,
        description="Release stays queued. Design, product, and QA work are not blocked by this limit.",
    )
    conn.execute(
        """
        INSERT INTO deployments (startup_id, provider, project, commit_sha, url, status, blocker, timestamp)
        VALUES (?, 'vercel', ?, ?, ?, 'blocked', ?, ?)
        """,
        (startup["id"], slug, commit_sha, url, reason, utcnow()),
    )
    add_event(
        conn,
        actor="RELEASE_ENGINEER",
        event_type="deployment_blocked",
        summary="Release is waiting on deployment capacity.",
        visibility="TEAM",
        startup_id=startup["id"],
    )
    return recompute_health(conn, startup["id"])


def public_events(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM events WHERE visibility = 'PUBLIC' ORDER BY id DESC"
    ).fetchall()
    kept = []
    for row in rows:
        if row["visibility"] != "PUBLIC":
            continue
        if row["event_type"] not in PUBLIC_EVENT_TYPES:
            continue
        if not public_text_allowed(row["summary"]):
            continue
        kept.append(row)
    return kept
