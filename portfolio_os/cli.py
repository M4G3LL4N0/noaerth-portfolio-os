"""Command line for the portfolio control plane."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from portfolio_os.canonical import PUBLIC_SUFFIX, WEBSITE_SUFFIX
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
    utcnow,
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
    kind = getattr(args, "kind_positional", None) or args.kind
    # `portfolio report daily` is the portfolio intelligence report. The legacy
    # health dump stays available as `report weekly` or `report daily --summary`.
    if kind == "daily" and not getattr(args, "summary", False):
        return cmd_report_daily(args)
    conn = _conn(args)
    print(write_report(conn, kind))
    conn.commit()
    return 0


def cmd_report_daily(args: argparse.Namespace) -> int:
    from portfolio_os.canonical import build_records, capture
    from portfolio_os.daily import build_report, persist_scores, write_reports

    conn = _conn(args)
    root = Path(args.root)
    scan = capture(PACKAGE_ROOT, root, _canonical_startups(conn), _vercel_team_id(root), force=bool(args.refresh))
    records = build_records(
        scan["startups"], scan["local"], scan["github"], scan["vercel"], scan.get("deploys")
    )
    persist_canonical(conn, records)
    payload = build_report(
        conn, root, PACKAGE_ROOT, scan, refetch_facts=bool(args.refresh_facts)
    )
    persist_scores(conn, payload)
    add_event(
        conn,
        actor="report",
        event_type="daily_report_generated",
        summary=f"Daily portfolio report generated for {payload['counts']['startups']} startups",
        visibility="TEAM",
    )
    conn.commit()
    json_path, md_path, html_path = write_reports(PACKAGE_ROOT, payload)
    publish_dir = PACKAGE_ROOT / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)
    (publish_dir / "daily.json").write_text(json_path.read_text(encoding="utf-8"), encoding="utf-8")
    (publish_dir / "daily.md").write_text(md_path.read_text(encoding="utf-8"), encoding="utf-8")
    (publish_dir / "daily.html").write_text(html_path.read_text(encoding="utf-8"), encoding="utf-8")
    if args.quiet:
        print(json.dumps(payload["counts"], indent=1))
    elif args.json:
        print(json.dumps(payload, indent=1))
    elif args.html:
        print(html_path)
    elif args.markdown:
        print(md_path)
    else:
        print(f"day {payload['day']}  score model v{payload['score_model_version']}")
        print(
            f"attainment {payload['portfolio']['baseline_attainment']}%  "
            f"hot {payload['counts']['hot']}  cold {payload['counts']['cold']}  "
            f"review {payload['counts']['review']}  ready {payload['counts']['ready']}  "
            f"blocked {payload['counts']['blocked']}  advanced {payload['counts']['advanced']}"
        )
        print(json_path)
        print(md_path)
        print(html_path)
    return 0


def cmd_report_history(args: argparse.Namespace) -> int:
    conn = _conn(args)
    from portfolio_os.daily import history

    for row in history(conn, args.startup, args.days):
        print(
            f"{row['day']}  attain {row['baseline_attainment']:5}  momentum {row['momentum_total']:5}  "
            f"attention {row['attention_score']:5}  heat {row['heat']:5}"
        )
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
    report.add_argument("kind_positional", nargs="?", choices=("daily", "weekly"), default=None,
                        help="report kind. daily is the portfolio intelligence report")
    report.add_argument("--kind", choices=("daily", "weekly"), default="daily")
    report.add_argument("--intelligence", action="store_true",
                        help="explicit form of the default daily intelligence report")
    report.add_argument("--summary", action="store_true",
                        help="legacy health-and-queue dump instead of the intelligence report")
    report.add_argument("--json", action="store_true", help="print the report payload to stdout")
    report.add_argument("--html", action="store_true", help="print the path of the generated HTML")
    report.add_argument("--markdown", action="store_true", help="print the path of the generated Markdown")
    report.add_argument("--refresh", action="store_true", help="rescan GitHub and Vercel first")
    report.add_argument("--refresh-facts", action="store_true", help="rescan repository surfaces first")
    report.add_argument("--quiet", action="store_true", help="print counts only")
    report.set_defaults(func=cmd_report)

    report_history = sub.add_parser("report-history")
    report_history.add_argument("startup")
    report_history.add_argument("--days", type=int, default=30)
    report_history.set_defaults(func=cmd_report_history)

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

    ecosystem = sub.add_parser("ecosystem")
    ecosystem.set_defaults(func=cmd_ecosystem)

    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8787)
    serve.add_argument("--open", action="store_true")
    serve.set_defaults(func=cmd_serve)

    up = sub.add_parser("up")
    up.add_argument("--host", default="127.0.0.1")
    up.add_argument("--port", type=int, default=8787)
    up.add_argument("--open", action="store_true")
    up.set_defaults(func=cmd_up)

    team = sub.add_parser("team")
    team.set_defaults(func=cmd_team)

    down = sub.add_parser("down")
    down.add_argument("--all-previews", action="store_true")
    down.set_defaults(func=cmd_down)

    ui = sub.add_parser("ui")
    ui.add_argument("--port", type=int, default=8787)
    ui.add_argument("--open", action="store_true")
    ui.set_defaults(func=cmd_ui)

    dossier = sub.add_parser("dossier")
    dossier.add_argument("slug")
    dossier.set_defaults(func=cmd_dossier)

    provider = sub.add_parser("provider")
    provider.add_argument("name", choices=("vercel",))
    provider.set_defaults(func=cmd_provider)

    preview = sub.add_parser("preview")
    preview.add_argument("slug")
    preview.add_argument("--stop", action="store_true")
    preview.set_defaults(func=cmd_preview)

    review_queue = sub.add_parser("review-queue")
    review_queue.set_defaults(func=cmd_review_queue)

    release_queue = sub.add_parser("release-queue")
    release_queue.set_defaults(func=cmd_release_queue)

    approve = sub.add_parser("approve-release")
    approve.add_argument("slug")
    approve.add_argument("commit")
    approve.set_defaults(func=cmd_approve_release)

    block = sub.add_parser("block-release")
    block.add_argument("--startup", required=True)
    block.add_argument("--reason", required=True)
    block.set_defaults(func=cmd_block)

    canonical = sub.add_parser("canonical-resources")
    canonical.add_argument("--refresh", action="store_true", help="rescan instead of using the cache")
    canonical.add_argument("--write", action="store_true", help="write PORTFOLIO_RESOURCE_CANONICALIZATION.md")
    canonical.add_argument("--json", action="store_true")
    canonical.add_argument("--limit", type=int, default=0)
    canonical.set_defaults(func=cmd_canonical_resources)

    canonicalize = sub.add_parser("canonicalize")
    canonicalize.add_argument("startup")
    canonicalize.add_argument("--apply", action="store_true", help="perform the safe rename. no deletion")
    canonicalize.set_defaults(func=cmd_canonicalize)

    canonical_queue = sub.add_parser("canonical-queue")
    canonical_queue.add_argument("--limit", type=int, default=40)
    canonical_queue.set_defaults(func=cmd_canonical_queue)

    landscape = sub.add_parser("landscape", help="external landscape for a startup")
    landscape.add_argument("startup", nargs="?", default="")
    landscape.add_argument("--refresh", action="store_true", help="ignore the cache and re-search")
    landscape.add_argument("--all", action="store_true", help="research every uncovered startup")
    landscape.add_argument("--batch", type=int, default=10, help="batch size for --all")
    landscape.add_argument("--stale", action="store_true", help="list stale coverage only")
    landscape.add_argument("--coverage", action="store_true", help="print research coverage")
    landscape.add_argument("--opportunities", action="store_true", help="portfolio reuse view")
    landscape.add_argument("--write", action="store_true", help="write EXTERNAL_LANDSCAPE.md")
    landscape.add_argument("--json", action="store_true")
    landscape.add_argument("--dry-run", action="store_true", help="score the plan without searching")
    landscape.add_argument("--no-scout", action="store_true", help="deterministic only")
    landscape.set_defaults(func=cmd_landscape)

    return parser


def cmd_ecosystem(args: argparse.Namespace) -> int:
    import subprocess

    conn = _conn(args)
    roles = {
        "noaerth": ("STUDIO_PUBLIC", 1),
        "noaerth-labs": ("STUDIO_PUBLIC_LABS", 1),
        "noaerth-team": ("STUDIO_INTERNAL", 0),
        "portfolio-control": ("STUDIO_CONTROL_PLANE", 0),
    }
    root = Path(args.root)
    for slug, (category, is_public) in roles.items():
        conn.execute(
            "UPDATE startups SET category = ?, is_public = ? WHERE slug = ?",
            (category, is_public, slug),
        )
        repo = root / ("noaerth-portfolio-os" if slug == "portfolio-control" else slug)
        sha = ""
        if (repo / ".git").exists() or slug == "portfolio-control":
            probe = repo if slug != "portfolio-control" else Path(__file__).resolve().parents[1]
            sha = subprocess.run(
                ["git", "-C", str(probe), "rev-parse", "--short", "HEAD"],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
        print(f"{slug}\t{category}\t{sha or 'missing'}")
    conn.commit()
    print("domains: labs.noaerth.com and team.noaerth.com are DOMAIN_CONFIGURATION_REQUIRED")
    print("existing vercel projects: noaerth, noaerth-labs, noaerth-team")
    return 0


def _local_token(host: str) -> str | None:
    from portfolio_os.localauth import resolve_token

    token = resolve_token(PACKAGE_ROOT, host)
    if token is None:
        print("A token is required off loopback, and it is not printed.")
    return token


def _port_open(port: int) -> bool:
    import socket

    probe = socket.socket()
    probe.settimeout(0.3)
    try:
        probe.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _print_running(port: int) -> None:
    from portfolio_os.daemon import daemon_is_fresh

    beat_path = PACKAGE_ROOT / "data" / "daemon.heartbeat"
    lanes = {"mutation": "?", "reviewer": "?"}
    if beat_path.is_file():
        try:
            body = json.loads(beat_path.read_text(encoding="utf-8"))
            lanes = body.get("lanes") or lanes
        except json.JSONDecodeError:
            pass
    daemon = "healthy" if daemon_is_fresh(beat_path) else "starting"
    print("Portfolio OS", flush=True)
    print("Running", flush=True)
    print(flush=True)
    print(f"UI: http://127.0.0.1:{port}", flush=True)
    print(f"Daemon: {daemon}", flush=True)
    print("Model: grok-4.7", flush=True)
    print(f"Workers: {lanes.get('mutation', '?')} mutation / {lanes.get('reviewer', '?')} reviewer", flush=True)


def _open_bootstrap(port: int) -> None:
    import webbrowser

    from portfolio_os.localauth import issue_bootstrap

    nonce = issue_bootstrap(PACKAGE_ROOT)
    webbrowser.open(f"http://127.0.0.1:{port}/bootstrap/{nonce}")


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in {"127.0.0.1", "localhost"}:
        print("serve stays on localhost")
        return 1
    token = _local_token(args.host)
    if token is None:
        return 1
    from portfolio_os.httpapi import serve

    if getattr(args, "open", False):
        _open_bootstrap(args.port)
    _print_running(args.port)
    serve(_db_path(args), args.host, args.port, token)
    return 0


def _db_path(args: argparse.Namespace) -> Path:
    return Path(args.db) if args.db else default_db_path(PACKAGE_ROOT)


def cmd_up(args: argparse.Namespace) -> int:
    import subprocess

    from portfolio_os.daemon import daemon_is_fresh

    if args.host not in {"127.0.0.1", "localhost"}:
        print("serve stays on localhost")
        return 1
    token = _local_token(args.host)
    if token is None:
        return 1
    beat = PACKAGE_ROOT / "data" / "daemon.heartbeat"
    if daemon_is_fresh(beat):
        pass
    else:
        stop = PACKAGE_ROOT / "data" / "daemon.stop"
        if stop.exists():
            stop.unlink()
        subprocess.Popen(
            [sys.executable, "-m", "portfolio_os", "daemon", "--interval", "120"],
            cwd=PACKAGE_ROOT,
            env={**os.environ, "PYTHONPATH": "."},
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if _port_open(args.port):
        _print_running(args.port)
        if args.open:
            _open_bootstrap(args.port)
        return 0
    _print_running(args.port)
    if args.open:
        _open_bootstrap(args.port)
    from portfolio_os.httpapi import serve

    serve(_db_path(args), args.host, args.port, token)
    return 0


def cmd_team(_args: argparse.Namespace) -> int:
    root = DEFAULT_PORTFOLIO / "noaerth-team"
    print(f"Team checkout  {root}")
    print("Start with     pnpm dev")
    print("URL            http://127.0.0.1:4320")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    import signal

    stop = PACKAGE_ROOT / "data" / "daemon.stop"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text("stop\n", encoding="utf-8")
    pid_path = PACKAGE_ROOT / "data" / "serve.pid"
    if pid_path.is_file():
        try:
            os.kill(int(pid_path.read_text(encoding="utf-8").strip()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
    if args.all_previews:
        from portfolio_os.preview import PreviewManager

        conn = _conn(args)
        rows = conn.execute(
            """
            SELECT startups.slug FROM preview_runs
            JOIN startups ON startups.id = preview_runs.startup_id
            WHERE preview_runs.status = 'RUNNING' AND startups.owner_private = 0
            """
        ).fetchall()
        manager = PreviewManager(conn, Path(args.root))
        for row in rows:
            manager.stop(row["slug"])
        conn.commit()
    print("stop requested for the daemon and the UI server")
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    url = f"http://127.0.0.1:{args.port}"
    print(url)
    if args.open:
        import webbrowser

        webbrowser.open(url)
    return 0


def cmd_dossier(args: argparse.Namespace) -> int:
    from portfolio_os.dossier import write_dossier
    from portfolio_os.workspace import startup_roots

    if args.slug in {"openlegal-data"}:
        print("refused")
        return 1
    conn = _conn(args)
    startup = startup_by_slug(conn, args.slug)
    if startup is None or startup["owner_private"]:
        print("refused")
        return 1
    roots = startup_roots(Path(args.root), args.slug)
    dest = write_dossier(roots[0], Path(args.root), args.slug, PACKAGE_ROOT / "dossiers", conn, startup["id"])
    conn.commit()
    print(dest)
    return 0


def cmd_provider(args: argparse.Namespace) -> int:
    conn = _conn(args)
    used = conn.execute(
        """
        SELECT COUNT(*) AS n FROM deployments
        WHERE timestamp >= datetime('now', '-1 day') AND status != 'blocked'
        """
    ).fetchone()["n"]
    queued = conn.execute(
        "SELECT COUNT(*) AS n FROM startups WHERE health = 'RELEASE_READY'"
    ).fetchone()["n"]
    print("plan: vercel hobby")
    print("project cap: 200, do not create new projects")
    print("daily platform allowance: about 100")
    print("autonomous budget: 70")
    print("reserve: 30")
    print(f"non-blocked deployment rows in the last day: {used}")
    print(f"release-ready startups: {queued}")
    print("policy: local QA before one production deploy of the newest reviewed commit")
    print("status: daily cap still blocks new production deploys when a blocker row exists")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from portfolio_os.exclusion import ExclusionError
    from portfolio_os.preview import PreviewManager, chrome_capture

    conn = _conn(args)
    manager = PreviewManager(conn, Path(args.root), capturer=chrome_capture)
    try:
        result = manager.stop(args.slug) if args.stop else manager.start(args.slug)
    except ExclusionError:
        print("not found")
        return 1
    conn.commit()
    for key in ("status", "port", "commit", "branch", "pid"):
        if result.get(key) is not None:
            print(f"{key}: {result[key]}")
    if result.get("local_url"):
        print(f"local: {result['local_url']}")
    if result.get("reason"):
        print(result["reason"])
    return 0 if result.get("status") in {"RUNNING", "STOPPED", "READY"} or result.get("ok") else 1


def cmd_review_queue(args: argparse.Namespace) -> int:
    conn = _conn(args)
    rows = conn.execute(
        """
        SELECT startups.slug, preview_shots.commit_sha, preview_shots.verdict, preview_shots.captured_at
        FROM preview_shots
        JOIN startups ON startups.id = preview_shots.startup_id
        WHERE preview_shots.viewport = 'desktop' AND startups.owner_private = 0
        ORDER BY preview_shots.id DESC
        """
    ).fetchall()
    seen = set()
    for row in rows:
        if row["slug"] in seen:
            continue
        seen.add(row["slug"])
        print(f"{row['slug']}\t{row['commit_sha']}\t{row['verdict'] or 'WAITING'}\t{row['captured_at']}")
    return 0


def cmd_release_queue(args: argparse.Namespace) -> int:
    conn = _conn(args)
    rows = conn.execute(
        """
        SELECT startups.slug, release_gates.commit_sha, release_gates.release_approval, release_gates.deployment
        FROM release_gates JOIN startups ON startups.id = release_gates.startup_id
        WHERE startups.owner_private = 0
        ORDER BY release_gates.id DESC
        """
    ).fetchall()
    for row in rows:
        print(f"{row['slug']}\t{row['commit_sha']}\t{row['release_approval']}\t{row['deployment']}")
    return 0


def cmd_approve_release(args: argparse.Namespace) -> int:
    from portfolio_os.engine import startup_by_slug
    from portfolio_os.preview import approve_release, approve_visual

    conn = _conn(args)
    startup = startup_by_slug(conn, args.slug)
    if startup is None or startup["owner_private"]:
        print("not found")
        return 1
    visual = approve_visual(conn, startup["id"], args.commit)
    release = approve_release(conn, startup["id"], args.commit)
    conn.commit()
    print(visual.get("visual"), release.get("state"))
    return 0 if release.get("ok") else 1


def cmd_block(args: argparse.Namespace) -> int:
    conn = _conn(args)
    health = block_release(conn, args.startup, args.reason)
    conn.commit()
    print(health)
    return 0


# ------------------------------------------------------- canonical resources


def _vercel_team_id(root: Path) -> str | None:
    """The Vercel team already linked by local checkouts. No guesswork."""
    for child in sorted(root.iterdir()):
        if is_excluded_name(child.name) or not child.is_dir():
            continue
        link = child / ".vercel" / "project.json"
        if not link.is_file():
            continue
        try:
            payload = json.loads(link.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("orgId"):
            return payload["orgId"]
    return None


def _canonical_startups(conn: sqlite3.Connection) -> list[str]:
    return [
        row["slug"]
        for row in conn.execute(
            "SELECT slug FROM startups WHERE owner_private = 0 ORDER BY slug"
        ).fetchall()
    ]


def _capture(args: argparse.Namespace, conn: sqlite3.Connection) -> dict:
    from portfolio_os.canonical import capture

    root = Path(args.root)
    return capture(
        PACKAGE_ROOT,
        root,
        _canonical_startups(conn),
        _vercel_team_id(root),
        force=bool(getattr(args, "refresh", False)),
    )


def cmd_canonical_resources(args: argparse.Namespace) -> int:
    from portfolio_os.canonical import build_records, render_markdown, render_table

    conn = _conn(args)
    scan = _capture(args, conn)
    records = build_records(
        scan["startups"], scan["local"], scan["github"], scan["vercel"], scan.get("deploys")
    )
    records = persist_canonical(conn, records)
    summary = canonical_summary(scan, records)
    if args.json:
        print(json.dumps({"summary": summary, "records": records}, indent=2))
    else:
        print(f"canonical startups audited: {summary['startups']}")
        print(
            "GitHub {total} repos / {public} *-public   local {local} repos / {lp} *-public   "
            "Vercel {vercel} projects / {vp} *-public".format(
                total=summary["github_total"],
                public=summary["github_public"],
                local=summary["local_total"],
                lp=summary["local_public"],
                vercel=summary["vercel_total"],
                vp=summary["vercel_public"],
            )
        )
        print(
            f"safe renames: {summary['safe_renames']}   owner review: {summary['owner_review']}   "
            f"destructive held: {summary['destructive_held']}"
        )
        print("")
        print("\n".join(render_table(records, args.limit)))
        if summary["safe_renames"]:
            print("")
            print("safe rename candidates:")
            for record in records:
                if record["recommended_action"] == "RENAME_VERCEL_PROJECT":
                    print(f"  portfolio canonicalize {record['startup']} --apply")
    if args.write:
        root = Path(args.root)
        path = root / "PORTFOLIO_RESOURCE_CANONICALIZATION.md"
        path.write_text(render_markdown(records, summary), encoding="utf-8")
        print("")
        print(f"wrote {path}")
    conn.commit()
    return 0


def cmd_canonicalize(args: argparse.Namespace) -> int:
    from portfolio_os.canonical import (
        CanonicalError,
        relink_local_vercel,
        vercel_project_detail,
        vercel_rename,
    )

    conn = _conn(args)
    root = Path(args.root)
    if is_excluded_name(args.startup):
        print(OWNER_PRIVATE_LABEL)
        return 0
    row = startup_by_slug(conn, args.startup)
    if row is None or row["owner_private"]:
        print("not found")
        return 1

    scan = _capture(args, conn)
    record = conn.execute(
        "SELECT * FROM canonical_resources WHERE slug = ?", (args.startup,)
    ).fetchone()
    if record is None:
        from portfolio_os.canonical import build_records

        records = build_records(
            scan["startups"], scan["local"], scan["github"], scan["vercel"], scan.get("deploys")
        )
        persist_canonical(conn, records)
        conn.commit()
        record = conn.execute(
            "SELECT * FROM canonical_resources WHERE slug = ?", (args.startup,)
        ).fetchone()

    print(f"{args.startup}  {record['duplicate_status']}  {record['recommended_action']}")
    if record["note"]:
        print(record["note"])

    legacy = record["vercel_public"]
    core = record["vercel_core"] or args.startup

    if record["recommended_action"] != "RENAME_VERCEL_PROJECT" or not legacy:
        if record["recommended_action"] in {"MERGE_REVIEW", "ARCHIVE_CANDIDATE"}:
            enqueue(
                conn,
                args.startup,
                "vercel",
                "RECONCILE_DUPLICATE",
                record["note"] or record["recommended_action"],
                risk="REVIEW",
                evidence=json.dumps({"legacy": legacy, "core": core}),
            )
            print("queued for owner review. no automatic action taken")
        else:
            print("no safe action available. inspect manually")
        conn.commit()
        return 0

    if not args.apply:
        before = vercel_project_detail(legacy, _vercel_team_id(root))
        print("dry run. nothing changed")
        print(f"  would rename vercel {legacy} -> {core}")
        print(f"  project id stays {before['id']}")
        print(f"  env vars preserved: {before['env_count']}")
        print(f"  git link: {before['link_type']} {before['link_repo'] or 'none'}")
        print(f"  apply with: portfolio canonicalize {args.startup} --apply")
        return 0

    team = _vercel_team_id(root)
    before = vercel_project_detail(legacy, team)
    result = vercel_rename(legacy, core, team)
    after = vercel_project_detail(core, team)
    relinked = relink_local_vercel(root, result["project_id"], core)
    conn.execute(
        """
        UPDATE canonical_resources
        SET vercel_core = ?, vercel_core_id = ?, vercel_public = '', vercel_public_id = '',
            duplicate_status = 'NONE', recommended_action = 'NONE',
            production_domain = ?, note = ?, scanned_at = datetime('now')
        WHERE slug = ?
        """,
        (
            core,
            result["project_id"],
            (after["targets"].get("production") or {}).get("alias", [""])[0]
            if (after["targets"].get("production") or {}).get("alias")
            else "",
            f"renamed from {legacy} at {utcnow()}",
            args.startup,
        ),
    )
    conn.execute(
        "UPDATE startups SET vercel_project = ? WHERE slug = ?", (core, args.startup)
    )
    enqueue(
        conn,
        args.startup,
        "vercel",
        "RENAME_VERCEL_PROJECT",
        f"{legacy} -> {core}",
        risk="SAFE",
        evidence=json.dumps(
            {
                "project_id_before": before["id"],
                "project_id_after": after["id"],
                "env_preserved": before["env_count"] == after["env_count"],
                "relinked_local": relinked,
            }
        ),
    )
    add_event(
        conn,
        startup_id=row["id"],
        actor="canonicalize",
        event_type="identity_canonicalized",
        summary=f"Vercel project {legacy} renamed to {core}",
        visibility="TEAM",
    )
    conn.commit()
    print(f"renamed {legacy} -> {core}")
    print(f"project id unchanged: {result['project_id']}")
    print(f"env vars preserved: {before['env_count']} -> {after['env_count']}")
    print(f"local checkouts relinked: {len(relinked)}")
    return 0


def cmd_canonical_queue(args: argparse.Namespace) -> int:
    conn = _conn(args)
    rows = conn.execute(
        """
        SELECT id, slug, surface, operation, risk, state, reason, created_at
        FROM canonicalization_queue ORDER BY id DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for row in rows:
        print(f"{row['id']:4}  {row['state']:8} {row['risk']:6} {row['slug']:22} {row['operation']:24} {row['reason'][:60]}")
    return 0


def persist_canonical(conn: sqlite3.Connection, records: list[dict]) -> list[dict]:
    for record in records:
        conn.execute(
            """
            INSERT INTO canonical_resources
              (slug, local_core_repo, local_website_repo, github_core, github_core_id,
               github_public, vercel_core, vercel_core_id, vercel_public, vercel_public_id,
               production_domain, production_branch, production_commit, github_remote,
               duplicate_status, recommended_action, note, scan_model_version, scanned_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1, datetime('now'))
            ON CONFLICT(slug) DO UPDATE SET
              local_core_repo=excluded.local_core_repo,
              local_website_repo=excluded.local_website_repo,
              github_core=excluded.github_core,
              github_core_id=excluded.github_core_id,
              github_public=excluded.github_public,
              vercel_core=excluded.vercel_core,
              vercel_core_id=excluded.vercel_core_id,
              vercel_public=excluded.vercel_public,
              vercel_public_id=excluded.vercel_public_id,
              production_domain=excluded.production_domain,
              production_branch=excluded.production_branch,
              production_commit=excluded.production_commit,
              github_remote=excluded.github_remote,
              duplicate_status=excluded.duplicate_status,
              recommended_action=excluded.recommended_action,
              note=excluded.note,
              scanned_at=excluded.scanned_at
            """,
            (
                record["startup"],
                record.get("local_core_repo", ""),
                record.get("local_website_repo", ""),
                record.get("github_core", ""),
                record.get("github_core_id", ""),
                record.get("github_public", ""),
                record.get("vercel_core", ""),
                record.get("vercel_core_id", ""),
                record.get("vercel_public", ""),
                record.get("vercel_public_id", ""),
                record.get("production_domain", ""),
                record.get("production_branch", ""),
                record.get("production_commit", ""),
                record.get("github_remote", ""),
                record.get("duplicate_status", "UNKNOWN"),
                record.get("recommended_action", "NONE"),
                record.get("note", ""),
            ),
        )
    conn.commit()
    return records


def canonical_summary(scan: dict, records: list[dict]) -> dict:
    local = scan["local"]
    github = scan["github"]
    vercel = scan["vercel"]
    statuses: dict[str, int] = {}
    actions: dict[str, int] = {}
    for record in records:
        statuses[record["duplicate_status"]] = statuses.get(record["duplicate_status"], 0) + 1
        actions[record["recommended_action"]] = actions.get(record["recommended_action"], 0) + 1
    return {
        "startups": len(records),
        "github_total": len(github),
        "github_public": sum(1 for name in github if name.endswith(PUBLIC_SUFFIX)),
        "local_total": len(local),
        "local_public": sum(1 for name in local if name.endswith(PUBLIC_SUFFIX)),
        "local_website": sum(1 for name in local if name.endswith(WEBSITE_SUFFIX)),
        "vercel_total": len(vercel),
        "vercel_public": sum(1 for name in vercel if name.endswith(PUBLIC_SUFFIX)),
        "vercel_public_orphan": actions.get("RENAME_VERCEL_PROJECT", 0),
        "vercel_public_dup": statuses.get("DUPLICATE", 0),
        "vercel_public_website_only": statuses.get("WEBSITE_ONLY", 0),
        "vercel_public_superseded": statuses.get("SUPERSEDED", 0),
        "safe_renames": actions.get("RENAME_VERCEL_PROJECT", 0),
        "owner_review": actions.get("MERGE_REVIEW", 0) + statuses.get("UNKNOWN", 0),
        "destructive_held": actions.get("ARCHIVE_CANDIDATE", 0),
        "statuses": statuses,
        "actions": actions,
    }


def enqueue(
    conn: sqlite3.Connection,
    slug: str,
    surface: str,
    operation: str,
    reason: str,
    risk: str = "REVIEW",
    evidence: str = "",
) -> None:
    conn.execute(
        """
        INSERT INTO canonicalization_queue (slug, surface, operation, reason, risk, evidence, created_at)
        VALUES (?,?,?,?,?,?, datetime('now'))
        """,
        (slug, surface, operation, reason, risk, evidence),
    )


def main(argv: list[str] | None = None) -> int:
    from portfolio_os.canonical import CanonicalError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CanonicalError as exc:
        print(f"canonicalization stopped: {exc}", file=sys.stderr)
        return 1


def _landscape_root(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "root", None) or os.environ.get("PORTFOLIO_ROOT") or Path.cwd())


def cmd_landscape(args: argparse.Namespace) -> int:
    """External intelligence: what already exists, what we can reuse (§1-§30)."""
    from portfolio_os import external as ex

    conn = _conn(args)
    root = _landscape_root(args)

    if args.coverage:
        report = ex.coverage_report(conn)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print("EXTERNAL RESEARCH COVERAGE")
            print(f"  total                {report['total']}")
            print(f"  RESEARCHED          {report['researched']}")
            print(f"  STALE               {report['stale']}")
            print(f"  NOT RESEARCHED      {report['not_researched']}")
            print(f"  EXTERNAL_RESEARCH_CURRENT  {report['external_research_current_pct']}%")
            print(f"  mean reuse leverage {report['reuse_leverage_mean']}")
        conn.commit()
        return 0

    if args.opportunities:
        buckets = ex.portfolio_opportunities(conn)
        if args.json:
            print(json.dumps(buckets, indent=2))
        else:
            print("PORTFOLIO EXTERNAL OPPORTUNITIES")
            for bucket in buckets.values():
                print(f"\n{bucket['title']}  ({bucket['count']})")
                for item in bucket["items"][:12]:
                    print(f"  {item['slug']:<34} leverage {item['reuse_leverage'] or 0}")
                if not bucket["items"]:
                    print("  none yet")
        conn.commit()
        return 0

    if args.stale:
        report = ex.coverage_report(conn)
        slugs = report["stale_slugs"] + report["not_researched_slugs"]
        if args.json:
            print(json.dumps({"stale": report["stale_slugs"],
                              "not_researched": report["not_researched_slugs"]}, indent=2))
        else:
            print(f"STALE ({len(report['stale_slugs'])})")
            for slug in report["stale_slugs"]:
                print(f"  {slug}")
            print(f"\nNOT RESEARCHED ({len(report['not_researched_slugs'])})")
            for slug in report["not_researched_slugs"][:80]:
                print(f"  {slug}")
            if len(report["not_researched_slugs"]) > 80:
                print(f"  ... and {len(report['not_researched_slugs']) - 80} more")
        conn.commit()
        return 0

    scout = None if args.no_scout else _maybe_scout(conn, args)

    if args.all:
        return _landscape_all(conn, args, root, scout)

    if not args.startup:
        report = ex.coverage_report(conn)
        print("usage: portfolio landscape <startup> | --all | --stale | --coverage | --opportunities")
        print(f"coverage: {report['researched']}/{report['total']} researched, "
              f"{report['external_research_current_pct']}% current")
        conn.commit()
        return 0

    startup = conn.execute(
        "SELECT * FROM startups WHERE slug = ? OR name = ?", (args.startup, args.startup)
    ).fetchone()
    if startup is None:
        print(f"startup not found: {args.startup}", file=sys.stderr)
        conn.commit()
        return 1

    result = ex.research_startup(
        conn, startup, root, force=bool(args.refresh),
        scout=scout, dry_run=bool(args.dry_run),
    )
    if result.error:
        print(result.error, file=sys.stderr)
        conn.commit()
        return 1
    if args.write:
        result.artifact_path = ex.write_artifact(conn, result.slug, root)
        conn.commit()
    _print_landscape(conn, result, root, args)
    conn.commit()
    return 0


def _maybe_scout(conn: sqlite3.Connection, args: argparse.Namespace):
    from portfolio_os.scout import EcosystemScout

    scout = EcosystemScout(conn, enabled=not bool(getattr(args, "no_scout", False)))
    return scout if scout.available() else None


def _landscape_all(
    conn: sqlite3.Connection, args: argparse.Namespace, root: Path, scout
) -> int:
    """§28. Batch by coverage shard so development keeps running alongside."""
    from portfolio_os import external as ex

    rows = conn.execute(
        "SELECT s.*, COALESCE(c.shard, '') shard FROM startups s"
        " LEFT JOIN startup_coverage c ON c.startup_id = s.id"
        " WHERE s.owner_private = 0 AND s.slug != ? ORDER BY c.shard, s.slug",
        (OWNER_PRIVATE_LABEL,),
    ).fetchall()
    report = ex.coverage_report(conn)
    pending = [
        r for r in rows
        if ex.coverage_state(
            {"searched_at": _landscape_searched(conn, r["id"])}
        ) in (ex.STALE, ex.NOT_RESEARCHED)
    ]
    batch = pending[: max(1, args.batch)]
    print(f"coverage {report['researched']}/{report['total']} researched; "
          f"researching {len(batch)} of {len(pending)} outstanding")
    for startup in batch:
        try:
            result = ex.research_startup(conn, startup, root, scout=scout,
                                         dry_run=bool(args.dry_run))
        except Exception as exc:  # noqa: BLE001 - one bad startup must not stop the batch
            print(f"  {startup['slug']}: error {exc}")
            continue
        conn.commit()
        counts = result.counts()
        print(f"  {startup['slug']:<32} leverage {result.leverage:>5} "
              f"oss {counts['high_fit_oss']} comp {counts['direct_competitors']} "
              f"({result.api_calls} api, {result.cache_hits} cached)")
    remaining = ex.coverage_report(conn)
    print(f"\ncoverage now {remaining['researched']}/{remaining['total']} "
          f"({remaining['external_research_current_pct']}% current)")
    return 0


def _landscape_searched(conn: sqlite3.Connection, startup_id: int) -> str | None:
    row = conn.execute(
        "SELECT searched_at FROM external_landscape WHERE startup_id = ?", (startup_id,)
    ).fetchone()
    return row["searched_at"] if row else None


def _print_landscape(
    conn: sqlite3.Connection, result: object, root: Path, args: argparse.Namespace
) -> None:
    from portfolio_os import external as ex

    slug = getattr(result, "slug", "")
    if getattr(result, "candidates", None):
        landscape = ex.load_landscape(conn, slug)
    else:
        plan = getattr(result, "plan", None)
        landscape = {
            "slug": slug,
            "summary": getattr(plan, "one_liner", "") if plan else "",
            "primary_workflow": getattr(plan, "primary_workflow", "") if plan else "",
            "reuse_leverage": getattr(result, "leverage", 0.0),
            "searched_at": "",
            "coverage": "NOT_RESEARCHED",
            "queries": getattr(plan, "queries", []) if plan else [],
            "scout_used": getattr(result, "scout_used", False),
            "scout_model": getattr(result, "scout_model", ""),
            "candidates": [],
            "recommendation": "nothing researched yet",
        }
    if args.json:
        print(json.dumps(landscape, indent=2, default=str))
        return
    print(f"EXTERNAL LANDSCAPE  {slug}")
    print(f"  leverage        {landscape.get('reuse_leverage') or 0}/100")
    print(f"  queries         {len(landscape.get('queries') or [])}")
    print(f"  candidates      {len(landscape.get('candidates') or [])}")
    print(f"  coverage        {landscape.get('coverage')}")
    if getattr(result, "artifact_path", ""):
        print(f"  artifact        {result.artifact_path}")
    print()
    print(f"RECOMMENDATION: {landscape.get('recommendation')}")
    strong = sorted(landscape.get("candidates") or [], key=lambda c: -c.get("reuse_fit", 0))[:10]
    if strong:
        print()
        print("  project                          type                  fit  license       decision")
        for item in strong:
            name = (item.get("name") or item.get("repo") or "")[:30]
            print(f"  {name:<32} {item['classification'][:18]:<20} "
                  f"{item.get('reuse_fit', 0):>3}  "
                  f"{(item.get('license_spdx') or 'none')[:11]:<11}  {item.get('reuse_decision')}")
    else:
        print()
        print("  no candidates (dry run, or cache is warm)")


if __name__ == "__main__":
    raise SystemExit(main())
