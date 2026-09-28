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
    serve(_conn(args), args.host, args.port, token)
    return 0


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

    serve(_conn(args), args.host, args.port, token)
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
