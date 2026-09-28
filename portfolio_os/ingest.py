"""Import ledgers and discover the workspace without entering excluded directories."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from portfolio_os.engine import add_event, ensure_work, recompute_health, utcnow
from portfolio_os.exclusion import OWNER_PRIVATE_LABEL, iter_top_level, is_excluded_name

FIRST_BATCH = (
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
)

LEDGER_NAMES = (
    "PORTFOLIO_CANONICAL_PROJECTS.md",
    "WEBSITE_QUALITY_RECOVERY.md",
    "STARTUP_WORK_LOG.md",
    "PORTFOLIO_MASTER_LEDGER.md",
    "PORTFOLIO_EXECUTION_QUEUE.md",
    "PORTFOLIO_EVIDENCE.md",
    "PORTFOLIO_LAUNCH_REPORT.md",
)


def _rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    parsed = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if not cells or cells[0] in {"Directory", "Startup", "---"} or set(cells[0]) <= {"-"}:
            continue
        if all(set(cell) <= {"-", ":"} or cell == "" for cell in cells):
            continue
        parsed.append(cells)
    return parsed


def _upsert_private(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO startups (slug, name, is_public, owner_private, health)
        VALUES ('OWNER-PRIVATE', ?, 0, 1, 'OWNER_PRIVATE')
        ON CONFLICT(slug) DO UPDATE SET
          name = excluded.name,
          primary_path = NULL,
          website_path = NULL,
          github_url = NULL,
          website_url = NULL,
          vercel_project = NULL,
          is_public = 0,
          owner_private = 1,
          health = 'OWNER_PRIVATE'
        """,
        (OWNER_PRIVATE_LABEL,),
    )


def import_canonical(conn: sqlite3.Connection, portfolio_root: Path) -> int:
    path = portfolio_root / "PORTFOLIO_CANONICAL_PROJECTS.md"
    count = 0
    for cells in _rows(path):
        if len(cells) < 4:
            continue
        directory, _name, role, public = cells[0], cells[1], cells[2], cells[3]
        if is_excluded_name(directory) or role == "OWNER-PRIVATE":
            _upsert_private(conn)
            continue
        if role != "PRIMARY" or public != "yes":
            continue
        website = cells[4] if len(cells) > 4 and cells[4] not in {"NONE", "—", "-"} else None
        conn.execute(
            """
            INSERT INTO startups (slug, name, primary_path, website_path, is_public, health, category)
            VALUES (?, ?, ?, ?, 1, 'REVIEW_REQUIRED', 'startup')
            ON CONFLICT(slug) DO UPDATE SET
              name = excluded.name,
              website_path = COALESCE(excluded.website_path, startups.website_path),
              is_public = 1
            """,
            (directory, cells[1] or directory, str(portfolio_root / directory), website),
        )
        count += 1
    _upsert_private(conn)
    return count


def _quality_work(conn: sqlite3.Connection, slug: str, quality: str) -> None:
    row = conn.execute("SELECT * FROM startups WHERE slug = ?", (slug,)).fetchone()
    if row is None or row["owner_private"]:
        return
    if "RENDER PENDING" in quality or quality.startswith("IMPROVED"):
        ensure_work(
            conn,
            row["id"],
            type_="visual_qa_desktop",
            title="Desktop visual review",
            role="VISUAL_REVIEWER",
            description="HTTP status is not a visual review. Render the desktop page and judge it.",
            priority=70,
        )
        ensure_work(
            conn,
            row["id"],
            type_="visual_qa_mobile",
            title="Mobile visual review",
            role="VISUAL_REVIEWER",
            description="Render a phone-sized viewport. A passing status code does not count.",
            priority=70,
        )
        conn.execute(
            "UPDATE startups SET health = 'VISUAL_QA_PENDING' WHERE id = ?",
            (row["id"],),
        )
    elif "NEEDS DESIGN" in quality:
        ensure_work(
            conn,
            row["id"],
            type_="design_review",
            title="Recover and improve the historical design",
            role="DESIGNER",
            priority=65,
        )
        conn.execute("UPDATE startups SET health = 'NEEDS_DESIGN' WHERE id = ?", (row["id"],))
    elif "NEEDS REVIEW" in quality or "UNCOMMITTED" in quality or "LOCAL ONLY" in quality:
        ensure_work(
            conn,
            row["id"],
            type_="historical_review",
            title="Compare the current site with the best historical version",
            role="DESIGNER",
            priority=60,
        )
        conn.execute("UPDATE startups SET health = 'REVIEW_REQUIRED' WHERE id = ?", (row["id"],))


def import_recovery(conn: sqlite3.Connection, portfolio_root: Path) -> int:
    path = portfolio_root / "WEBSITE_QUALITY_RECOVERY.md"
    seen = 0
    for cells in _rows(path):
        if len(cells) < 7:
            continue
        slug, quality = cells[0], cells[-1]
        if is_excluded_name(slug) or slug == "OWNER-PRIVATE":
            continue
        _quality_work(conn, slug, quality)
        seen += 1
    gh0st = conn.execute("SELECT id FROM startups WHERE slug = 'gh0st'").fetchone()
    if gh0st:
        gid = gh0st["id"]
        ensure_work(conn, gid, type_="design_review", title="Design review of the restored privacy diagram", role="DESIGNER", priority=85)
        ensure_work(conn, gid, type_="visual_qa_desktop", title="Desktop visual review", role="VISUAL_REVIEWER", priority=84)
        ensure_work(conn, gid, type_="visual_qa_mobile", title="Mobile visual review", role="VISUAL_REVIEWER", priority=84)
        ensure_work(conn, gid, type_="product_review", title="Product clarity review", role="PRODUCT_REVIEWER", priority=80)
        ensure_work(
            conn,
            gid,
            type_="deploy_retry",
            title="Deploy after capacity is available",
            role="RELEASE_ENGINEER",
            status="blocked",
            blocked_reason="Vercel hobby daily deployment cap",
            priority=20,
            description="Website commit 3107ff4 is not in production. Do not retry the deploy until the cap resets.",
        )
        conn.execute(
            """
            INSERT INTO deployments (startup_id, provider, project, commit_sha, url, status, blocker, timestamp)
            VALUES (?, 'vercel', 'gh0st', '3107ff4', 'https://gh0st-six.vercel.app', 'blocked', ?, ?)
            """,
            (gid, "Vercel hobby daily deployment cap", utcnow()),
        )
        conn.execute("UPDATE startups SET health = 'VISUAL_QA_PENDING', website_url = ? WHERE id = ?", ("https://gh0st-six.vercel.app", gid))
        add_event(
            conn,
            actor="NOAERTH_EDITOR",
            event_type="public_design_update",
            summary="gh0st — Reworked the privacy-flow experience and restored the CLI product showcase.",
            visibility="PUBLIC",
            startup_id=gid,
        )
    bio = conn.execute("SELECT id FROM startups WHERE slug = 'bioyield-labs'").fetchone()
    if bio:
        add_event(
            conn,
            actor="NOAERTH_EDITOR",
            event_type="public_research_note",
            summary="Bioyield Labs — Refined its public research presentation.",
            visibility="PUBLIC",
            startup_id=bio["id"],
        )
    # Cross-portfolio engineering item. Not a startup website.
    system = conn.execute("SELECT id FROM startups WHERE slug = 'portfolio-control'").fetchone()
    if system is None:
        conn.execute(
            """
            INSERT INTO startups (slug, name, is_public, health, category, maturity)
            VALUES ('portfolio-control', 'Portfolio control plane', 0, 'ACTIVE', 'internal', 'internal')
            """
        )
        system = conn.execute("SELECT id FROM startups WHERE slug = 'portfolio-control'").fetchone()
    ensure_work(
        conn,
        system["id"],
        type_="consolidation_analysis",
        title="Investigate whether early lab sites can share deployment infrastructure",
        role="RELEASE_ENGINEER",
        priority=30,
        description=(
            "Evidence only. Do not migrate or delete Vercel projects. "
            "Compare independent domains, static sites, and the 200-project cap."
        ),
    )
    for slug in FIRST_BATCH:
        row = conn.execute("SELECT id FROM startups WHERE slug = ?", (slug,)).fetchone()
        if row:
            recompute_health(conn, row["id"])
    if gh0st:
        recompute_health(conn, gh0st["id"])
    return seen


def discover(conn: sqlite3.Connection, portfolio_root: Path) -> dict:
    names = []
    for child in iter_top_level(portfolio_root):
        names.append(child.name)
        if is_excluded_name(child.name):
            raise RuntimeError("exclusion failed")
    previous = conn.execute("SELECT directory_names FROM discovery_state WHERE id = 1").fetchone()
    previous_names = set(json.loads(previous["directory_names"])) if previous and previous["directory_names"] else set()
    current = set(names)
    added = sorted(current - previous_names) if previous_names else []
    removed = sorted(previous_names - current) if previous_names else []
    conn.execute(
        """
        INSERT INTO discovery_state (id, scanned_at, directory_names)
        VALUES (1, ?, ?)
        ON CONFLICT(id) DO UPDATE SET scanned_at = excluded.scanned_at, directory_names = excluded.directory_names
        """,
        (utcnow(), json.dumps(names)),
    )
    for name in added:
        if is_excluded_name(name):
            continue
        add_event(
            conn,
            actor="discovery",
            event_type="directory_added",
            summary=f"New top-level directory {name}.",
            visibility="TEAM",
        )
    return {"directories": len(names), "added": added, "removed": removed}


def note_ledgers_read(conn: sqlite3.Connection, portfolio_root: Path) -> list[str]:
    found = [name for name in LEDGER_NAMES if (portfolio_root / name).is_file()]
    add_event(
        conn,
        actor="ingest",
        event_type="ledgers_imported",
        summary=f"Imported {len(found)} portfolio ledgers into SQLite.",
        visibility="PRIVATE_SYSTEM",
        metadata={"count": len(found)},
    )
    return found
