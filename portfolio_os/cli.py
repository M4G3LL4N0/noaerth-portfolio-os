"""Command line for the portfolio control plane."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from portfolio_os.db import connect, default_db_path
from portfolio_os.engine import (
    add_event,
    block_release,
    claim_lock,
    public_events,
    record_review,
    refresh_priorities,
    release_lock,
    startup_by_slug,
)
from portfolio_os.exclusion import OWNER_PRIVATE_LABEL, is_excluded_name
from portfolio_os.ingest import discover, import_canonical, import_recovery, note_ledgers_read
from portfolio_os.publish import build_public_snapshot, build_team_snapshot, write_report, write_snapshots

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORTFOLIO = PACKAGE_ROOT.parent


def _conn(args: argparse.Namespace) -> sqlite3.Connection:
    return connect(Path(args.db) if args.db else default_db_path(PACKAGE_ROOT))


def _print_startup(row: sqlite3.Row, work: list[sqlite3.Row]) -> None:
    if row["owner_private"]:
        print(OWNER_PRIVATE_LABEL)
        return
    print(f"{row['slug']}  {row['name']}")
    print(f"health {row['health']}  priority {row['priority']}  public {bool(row['is_public'])}")
    if row["website_url"]:
        print(f"url {row['website_url']}")
    for item in work:
        reason = f"  blocked: {item['blocked_reason']}" if item["blocked_reason"] else ""
        print(f"  [{item['status']}] {item['assigned_role']}  {item['title']}{reason}")


def cmd_discover(args: argparse.Namespace) -> int:
    conn = _conn(args)
    canonical = import_canonical(conn, Path(args.root))
    recovery = import_recovery(conn, Path(args.root))
    found = note_ledgers_read(conn, Path(args.root))
    scan = discover(conn, Path(args.root))
    refresh_priorities(conn)
    conn.commit()
    print(
        json.dumps(
            {
                "canonical_public": canonical,
                "recovery_rows": recovery,
                "ledgers": found,
                "directories": scan["directories"],
                "added": scan["added"],
                "removed": scan["removed"],
            },
            indent=2,
        )
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    conn = _conn(args)
    rows = conn.execute(
        """
        SELECT health, COUNT(*) AS n FROM startups
        WHERE owner_private = 0 AND slug != 'portfolio-control'
        GROUP BY health ORDER BY n DESC
        """
    ).fetchall()
    visual = conn.execute(
        """
        SELECT COUNT(*) AS n FROM startups
        WHERE health = 'VISUAL_QA_PENDING' AND owner_private = 0
        """
    ).fetchone()["n"]
    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE status = 'queued'"
    ).fetchone()["n"]
    blocked = conn.execute(
        "SELECT COUNT(*) AS n FROM work_items WHERE status = 'blocked'"
    ).fetchone()["n"]
    print(f"visual QA pending {visual}")
    print(f"queued {queued}  blocked {blocked}")
    for row in rows:
        print(f"{row['health']} {row['n']}")
    return 0


def cmd_startups(args: argparse.Namespace) -> int:
    conn = _conn(args)
    for row in conn.execute(
        "SELECT slug, name, health, owner_private FROM startups ORDER BY owner_private, slug"
    ):
        if row["owner_private"]:
            print(OWNER_PRIVATE_LABEL)
        else:
            print(f"{row['slug']}\t{row['health']}\t{row['name']}")
    return 0


def cmd_startup(args: argparse.Namespace) -> int:
    if is_excluded_name(args.slug):
        print(OWNER_PRIVATE_LABEL)
        return 0
    conn = _conn(args)
    row = startup_by_slug(conn, args.slug)
    if row is None:
        print("not found", file=sys.stderr)
        return 1
    work = conn.execute(
        """
        SELECT title, status, assigned_role, blocked_reason FROM work_items
        WHERE startup_id = ? ORDER BY priority DESC
        """,
        (row["id"],),
    ).fetchall()
    _print_startup(row, work)
    return 0


def cmd_queue(args: argparse.Namespace) -> int:
    conn = _conn(args)
    refresh_priorities(conn)
    conn.commit()
    limit = args.limit or 25
    rows = conn.execute(
        """
        SELECT startups.slug, work_items.title, work_items.assigned_role,
               work_items.priority, work_items.status, work_items.blocked_reason
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE work_items.status NOT IN ('completed') AND startups.owner_private = 0
        ORDER BY work_items.priority DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    for row in rows:
        print(
            f"{row['priority']:3}  {row['status']:10}  {row['slug']:22}  {row['assigned_role']:20}  {row['title']}"
        )
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    conn = _conn(args)
    result = record_review(
        conn,
        slug=args.startup,
        role=args.role,
        dimension=args.dimension,
        finding=args.finding,
        severity=args.severity,
        resolution=args.resolution,
        work_item_id=args.work_item,
    )
    conn.commit()
    print(json.dumps(result))
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    conn = _conn(args)
    refresh_priorities(conn)
    write_report(conn, "daily")
    public_path, team_path = write_snapshots(conn, PACKAGE_ROOT / "publish")
    noaerth_public = DEFAULT_PORTFOLIO / "noaerth" / "data" / "portfolio-public.json"
    if noaerth_public.parent.is_dir():
        noaerth_public.write_text(public_path.read_text(encoding="utf-8"), encoding="utf-8")
    conn.commit()
    print(public_path)
    print(team_path)
    if noaerth_public.parent.is_dir():
        print(noaerth_public)
    return 0


def cmd_events(args: argparse.Namespace) -> int:
    conn = _conn(args)
    visibility = args.visibility or "TEAM"
    if visibility == "PUBLIC":
        rows = public_events(conn)
    else:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE visibility = ?
            ORDER BY id DESC LIMIT 40
            """,
            (visibility,),
        ).fetchall()
    for row in rows:
        print(f"{row['timestamp']}  {row['visibility']}  {row['summary']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    conn = _conn(args)
    print(write_report(conn, args.kind))
    conn.commit()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Claim the next startups and run the role pipeline."""
    from portfolio_os.execute import execute_batch, execute_startup

    conn = _conn(args)
    evidence = PACKAGE_ROOT / "evidence"
    if is_excluded_name(args.startup or ""):
        print(OWNER_PRIVATE_LABEL)
        return 0
    if args.startup:
        row = startup_by_slug(conn, args.startup)
        if row is None or row["owner_private"]:
            print(OWNER_PRIVATE_LABEL if row and row["owner_private"] else "not found")
            return 1
        result = execute_startup(conn, Path(args.root), args.startup, evidence)
        print(json.dumps(result))
    else:
        limit = 1 if args.until_idle else args.batch
        runs = 0
        while True:
            batch = execute_batch(conn, Path(args.root), limit, evidence)
            print(json.dumps(batch))
            runs += 1
            if not args.until_idle or not batch or runs >= 20:
                break
    refresh_priorities(conn)
    write_report(conn, "daily")
    write_snapshots(conn, PACKAGE_ROOT / "publish")
    noaerth_public = DEFAULT_PORTFOLIO / "noaerth" / "data" / "portfolio-public.json"
    public_path = PACKAGE_ROOT / "publish" / "public.json"
    if noaerth_public.parent.is_dir() and public_path.is_file():
        noaerth_public.write_text(public_path.read_text(encoding="utf-8"), encoding="utf-8")
    conn.commit()
    return 0


def cmd_daemon(args: argparse.Namespace) -> int:
    from portfolio_os.daemon import run_daemon

    conn = _conn(args)
    stop = Path(args.stop_file) if args.stop_file else PACKAGE_ROOT / "data" / "daemon.stop"
    result = run_daemon(
        conn,
        Path(args.root),
        PACKAGE_ROOT / "evidence",
        interval=args.interval,
        max_cycles=args.max_cycles,
        stop_file=stop,
        publish_dir=PACKAGE_ROOT / "publish",
    )
    print(result)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    root = Path(args.root)
    excluded = root / "openlegal-data"
    problems = []
    if excluded.exists() and excluded.is_dir():
        # Existence may be known. Contents must not be read.
        pass
    try:
        from portfolio_os.exclusion import assert_allowed

        if excluded.exists():
            try:
                assert_allowed(excluded, root)
                problems.append("exclusion allowed the private directory")
            except Exception:
                pass
    except Exception as exc:  # pragma: no cover
        problems.append(str(exc))
    conn = _conn(args)
    private = conn.execute("SELECT * FROM startups WHERE owner_private = 1").fetchall()
    if len(private) != 1 or private[0]["name"] != OWNER_PRIVATE_LABEL:
        problems.append("owner-private record missing or too detailed")
    blob = json.dumps(build_public_snapshot(conn))
    if "PRIVATE_SYSTEM" in blob or "openlegal" in blob.lower():
        problems.append("public snapshot leaked")
    team = json.dumps(build_team_snapshot(conn))
    if "openlegal" in team.lower() or "/Users/" in team:
        problems.append("team snapshot leaked")
    secret = os.environ.get("NOAERTH_TEAM_SECRET", "")
    local_env = Path(args.root) / "noaerth" / ".env.local"
    if len(secret) >= 16:
        print("TEAM AUTH: configured in this environment. Value not shown.")
    elif local_env.is_file():
        print("TEAM AUTH: local file present. Value not shown. Production still needs the variable.")
    else:
        print("TEAM AUTH: TEAM_AUTH_CONFIGURATION_REQUIRED")
        print("Set NOAERTH_TEAM_SECRET in the server environment. Do not commit it. Length at least 16.")
    worktrees = Path(__file__).resolve().parents[1] / "worktrees"
    if worktrees.is_dir():
        stale = [p.name for p in worktrees.iterdir() if p.is_dir()]
        print(f"worktrees: {len(stale)}")
    if problems:
        print("\n".join(problems))
        return 1
    print("doctor ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portfolio")
    parser.add_argument("--db", default=None)
    parser.add_argument("--root", default=str(DEFAULT_PORTFOLIO))
    sub = parser.add_subparsers(dest="command", required=True)

    discover_cmd = sub.add_parser("discover")
    discover_cmd.set_defaults(func=cmd_discover)

    status = sub.add_parser("status")
    status.set_defaults(func=cmd_status)

    startups = sub.add_parser("startups")
    startups.set_defaults(func=cmd_startups)

    one = sub.add_parser("startup")
    one.add_argument("slug")
    one.set_defaults(func=cmd_startup)

    queue = sub.add_parser("queue")
    queue.add_argument("--limit", type=int, default=25)
    queue.set_defaults(func=cmd_queue)

    review = sub.add_parser("review")
    review.add_argument("--startup", required=True)
    review.add_argument("--role", required=True)
    review.add_argument("--dimension", required=True)
    review.add_argument("--finding", required=True)
    review.add_argument("--severity", default="high")
    review.add_argument("--resolution", choices=("pass", "fail"), required=True)
    review.add_argument("--work-item", type=int, default=None)
    review.set_defaults(func=cmd_review)

    publish = sub.add_parser("publish")
    publish.set_defaults(func=cmd_publish)

    events = sub.add_parser("events")
    events.add_argument("--visibility", choices=("PUBLIC", "TEAM", "PRIVATE_SYSTEM"))
    events.set_defaults(func=cmd_events)

    report = sub.add_parser("report")
    report.add_argument("--kind", choices=("daily", "weekly"), default="daily")
    report.set_defaults(func=cmd_report)

    run = sub.add_parser("run")
    run.add_argument("--startup", default=None)
    run.add_argument("--batch", type=int, default=1)
    run.add_argument("--until-idle", action="store_true")
    run.set_defaults(func=cmd_run)

    daemon = sub.add_parser("daemon")
    daemon.add_argument("--interval", type=int, default=120)
    daemon.add_argument("--max-cycles", type=int, default=None)
    daemon.add_argument("--stop-file", default=None)
    daemon.set_defaults(func=cmd_daemon)

    doctor = sub.add_parser("doctor")
    doctor.set_defaults(func=cmd_doctor)

    block = sub.add_parser("block-release")
    block.add_argument("--startup", required=True)
    block.add_argument("--reason", required=True)
    block.set_defaults(func=cmd_block)

    return parser


def cmd_block(args: argparse.Namespace) -> int:
    conn = _conn(args)
    health = block_release(conn, args.startup, args.reason)
    conn.commit()
    print(health)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
