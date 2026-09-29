"""Small internal HTTP API plus the local engine UI. Labs reads public snapshots."""

from __future__ import annotations

import hmac
import json
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs

from portfolio_os.engine import add_event, ensure_work, startup_by_slug
from portfolio_os.publish import build_public_snapshot
from portfolio_os.localauth import consume_bootstrap, loopback
from portfolio_os.ui import login_page, page as render_page

ALLOWED_ACTIONS = frozenset(
    {
        "create_task",
        "pause",
        "resume",
        "priority",
        "reject_review",
        "accept_review",
        "reopen",
        "trigger_review",
        "start_preview",
        "stop_preview",
        "capture",
        "pause_task",
        "resume_task",
        "cancel_task",
        "approve_visual",
        "approve_release",
        "queue_deployment",
    }
)
SCHEMA_VERSION = 1


def _public_view(conn: sqlite3.Connection, kind: str) -> dict:
    snapshot = build_public_snapshot(conn)
    snapshot["schema"] = SCHEMA_VERSION
    if kind == "activity":
        return {"schema": SCHEMA_VERSION, "activity": snapshot.get("activity", [])}
    if kind == "week":
        return {"schema": SCHEMA_VERSION, "built_this_week": snapshot.get("built_this_week", [])}
    if kind == "projects":
        return {"schema": SCHEMA_VERSION, "startups": snapshot.get("startups", [])}
    return snapshot


def _authorized(headers: dict, token: str) -> bool:
    presented = headers.get("authorization", "")
    if token and hmac.compare_digest(presented, f"Bearer {token}"):
        return True
    for part in headers.get("cookie", "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == "portfolio_os_session" and token and hmac.compare_digest(value, token):
            return True
    return False


def _form(raw: bytes) -> dict:
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


UI_PATHS = {
    "/",
    "/workers",
    "/agents",
    "/queue",
    "/coverage",
    "/startups",
    "/previews",
    "/releases",
    "/providers/vercel",
    "/workspaces",
    "/events",
    "/system",
}


def dispatch(conn: sqlite3.Connection, method: str, path: str, headers: dict, body: dict | None, token: str) -> tuple[int, dict]:
    public_routes = {
        "/public/v1/snapshot": "snapshot",
        "/public/v1/activity": "activity",
        "/public/v1/week": "week",
        "/public/v1/projects": "projects",
    }
    if path in public_routes and method == "GET":
        return 200, _public_view(conn, public_routes[path])
    if not _authorized(headers, token):
        return 401, {"error": "unauthorized"}
    if path == "/api/v1/health" and method == "GET":
        return 200, {"ok": True, "schema": SCHEMA_VERSION}
    if path != "/api/v1/actions" or method != "POST":
        return 404, {"error": "not_found"}
    payload = body or {}
    action = str(payload.get("action") or "")
    if action not in ALLOWED_ACTIONS:
        return 400, {"error": "unsupported_action"}
    slug = str(payload.get("slug") or "")
    startup = startup_by_slug(conn, slug)
    if startup is None or startup["owner_private"]:
        return 404, {"error": "not_found"}
    if action == "pause":
        conn.execute("UPDATE startups SET paused = 1 WHERE id = ?", (startup["id"],))
    elif action == "resume":
        conn.execute("UPDATE startups SET paused = 0 WHERE id = ?", (startup["id"],))
    elif action == "priority":
        conn.execute("UPDATE startups SET priority = ? WHERE id = ?", (int(payload.get("priority") or 50), startup["id"]))
    elif action == "create_task":
        ensure_work(
            conn,
            startup["id"],
            type_="team_task",
            title=str(payload.get("title") or "Team task")[:180],
            role=str(payload.get("role") or "PORTFOLIO_DIRECTOR"),
            description="Created from Noaerth Team.",
        )
    elif action == "reject_review":
        ensure_work(
            conn,
            startup["id"],
            type_="follow_up",
            title=str(payload.get("title") or "Team rejected the current review")[:180],
            role="DESIGNER",
            priority=80,
            description=str(payload.get("finding") or "")[:500],
        )
    elif action == "accept_review":
        ensure_work(
            conn,
            startup["id"],
            type_="design_quality",
            title="Team accepted the current public design",
            role="VISUAL_REVIEWER",
            priority=40,
            description="Accepted from Noaerth Team. The reviewer still records the pass.",
        )
    elif action == "reopen":
        conn.execute("UPDATE startups SET health = 'REVIEW_REQUIRED' WHERE id = ?", (startup["id"],))
        ensure_work(
            conn,
            startup["id"],
            type_="venture_now",
            title=str(payload.get("title") or "Reopened from Team")[:180],
            role="PORTFOLIO_DIRECTOR",
            priority=84,
        )
    elif action in {"start_preview", "stop_preview", "approve_visual", "approve_release", "queue_deployment"}:
        from pathlib import Path

        from portfolio_os.exclusion import ExclusionError
        from portfolio_os.preview import PreviewManager, approve_release, approve_visual, chrome_capture, queue_deployment

        portfolio_root = Path(__file__).resolve().parents[2]
        manager = PreviewManager(conn, portfolio_root, capturer=chrome_capture)
        try:
            if action == "start_preview":
                result = manager.start(slug)
            elif action == "stop_preview":
                result = manager.stop(slug)
            elif action == "capture":
                current = conn.execute(
                    """
                    SELECT preview_runs.commit_sha, preview_runs.branch, preview_runs.port, preview_runs.status
                    FROM preview_runs JOIN startups ON startups.id = preview_runs.startup_id
                    WHERE startups.slug = ? ORDER BY preview_runs.id DESC LIMIT 1
                    """,
                    (slug,),
                ).fetchone()
                if current is None or current["status"] != "RUNNING" or not current["port"]:
                    result = {"ok": False, "state": "NO_RUNNING_PREVIEW"}
                else:
                    result = manager.capture(
                        slug,
                        f"http://127.0.0.1:{current['port']}",
                        current["commit_sha"] or "",
                        current["branch"] or "",
                    )
            else:
                commit = str(payload.get("commit") or "")
                if not commit:
                    return 400, {"error": "commit_required"}
                if action == "approve_visual":
                    result = approve_visual(conn, startup["id"], commit)
                elif action == "approve_release":
                    result = approve_release(conn, startup["id"], commit)
                else:
                    import subprocess

                    head = subprocess.run(
                        ["git", "-C", str(portfolio_root / slug), "rev-parse", "--short", "HEAD"],
                        check=False,
                        capture_output=True,
                        text=True,
                    ).stdout.strip()
                    result = queue_deployment(conn, startup["id"], commit, head)
        except ExclusionError:
            return 404, {"error": "not_found"}
        add_event(
            conn,
            actor="NOAERTH_TEAM",
            event_type="team_action",
            summary=f"{action} on {slug}",
            visibility="TEAM",
            startup_id=startup["id"],
        )
        return 200, {"ok": True, "action": action, "slug": slug, "result": result}
    elif action in {"pause_task", "resume_task", "cancel_task"}:
        item_id = int(payload.get("work_item") or 0)
        item = conn.execute(
            "SELECT id, status FROM work_items WHERE id = ? AND startup_id = ?",
            (item_id, startup["id"]),
        ).fetchone()
        if item is None:
            return 404, {"error": "not_found"}
        if action == "cancel_task" and item["status"] not in {"queued", "paused", "blocked"}:
            return 400, {"error": "unsafe_cancel"}
        next_status = {"pause_task": "paused", "resume_task": "queued", "cancel_task": "cancelled"}[action]
        if action == "resume_task" and item["status"] not in {"paused", "blocked"}:
            return 400, {"error": "not_paused"}
        conn.execute("UPDATE work_items SET status = ? WHERE id = ?", (next_status, item["id"]))
    elif action == "trigger_review":
        ensure_work(
            conn,
            startup["id"],
            type_="visual_qa_desktop",
            title="Team requested a visual review",
            role="VISUAL_REVIEWER",
            priority=82,
        )
        ensure_work(
            conn,
            startup["id"],
            type_="visual_qa_mobile",
            title="Team requested a phone visual review",
            role="VISUAL_REVIEWER",
            priority=82,
        )
    add_event(
        conn,
        actor="NOAERTH_TEAM",
        event_type="team_action",
        summary=f"{action} on {slug}",
        visibility="TEAM",
        startup_id=startup["id"],
    )
    return 200, {"ok": True, "action": action, "slug": slug}


def handle(
    conn: sqlite3.Connection,
    method: str,
    path: str,
    headers: dict,
    raw: bytes,
    token: str,
    root: Path,
    client_host: str = "127.0.0.1",
) -> tuple[int, str, bytes, dict]:
    """Return status, content type, body, and extra headers."""
    extra: dict[str, str] = {}
    if path.startswith("/bootstrap/") and method == "GET":
        if not loopback(client_host):
            return 404, "text/plain", b"not found", extra
        nonce = path.removeprefix("/bootstrap/")
        if not consume_bootstrap(root, nonce):
            return 404, "text/plain", b"bootstrap expired", extra
        extra["Set-Cookie"] = "portfolio_os_session=" + token + "; HttpOnly; SameSite=Strict; Path=/"
        extra["Location"] = "/"
        return 302, "text/plain", b"", extra
    if path == "/login" and method == "GET":
        return 200, "text/html; charset=utf-8", login_page().encode("utf-8"), extra
    if path == "/login" and method == "POST":
        supplied = _form(raw).get("token", "")
        if token and hmac.compare_digest(supplied, token):
            extra["Set-Cookie"] = "portfolio_os_session=" + token + "; HttpOnly; SameSite=Strict; Path=/"
            extra["Location"] = "/"
            return 302, "text/plain", b"", extra
        return 401, "text/html; charset=utf-8", login_page(failed=True).encode("utf-8"), extra
    ui = path in UI_PATHS or path.startswith("/startups/")
    evidence = path.startswith("/evidence/")
    if ui or evidence or path == "/ui/action":
        if not _authorized(headers, token):
            extra["Location"] = "/login"
            return 302, "text/plain", b"", extra
    if path == "/ui/action" and method == "POST":
        status, payload = dispatch(conn, "POST", "/api/v1/actions", headers, _form(raw), token)
        extra["Location"] = headers.get("referer") or "/"
        return 303 if status < 400 else status, "application/json", json.dumps(payload).encode("utf-8"), extra
    if evidence and method == "GET":
        parts = path.strip("/").split("/")
        if len(parts) != 3 or parts[2] not in {"desktop.png", "mobile.png"}:
            return 404, "application/json", b'{"error":"not_found"}', extra
        slug = parts[1]
        startup = startup_by_slug(conn, slug)
        if startup is None or startup["owner_private"] or not slug.replace("-", "").isalnum():
            return 404, "application/json", b'{"error":"not_found"}', extra
        file_path = (root / "evidence" / slug / parts[2]).resolve()
        if root.resolve() not in file_path.parents or not file_path.is_file():
            return 404, "application/json", b'{"error":"not_found"}', extra
        return 200, "image/png", file_path.read_bytes(), extra
    if ui and method == "GET":
        return 200, "text/html; charset=utf-8", render_page(conn, path, root).encode("utf-8"), extra
    if method == "POST":
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {}
    else:
        body = None
    status, payload = dispatch(conn, method, path, headers, body, token)
    return status, "application/json", json.dumps(payload).encode("utf-8"), extra


def serve(db_path: Path, host: str, port: int, token: str) -> None:
    import os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from portfolio_os.db import connect

    root = Path(__file__).resolve().parents[1]
    pid_path = root / "data" / "serve.pid"
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(os.getpid()), encoding="utf-8")

    class Handler(BaseHTTPRequestHandler):
        def _respond(self, status: int, content_type: str, body: bytes, extra: dict) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in extra.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _go(self, method: str, raw: bytes = b"") -> None:
            conn = connect(db_path)
            try:
                headers = {key.lower(): value for key, value in self.headers.items()}
                client_host = self.client_address[0] if self.client_address else ""
                status, content_type, body, extra = handle(
                    conn, method, self.path.split("?")[0], headers, raw, token, root, client_host
                )
                if method == "POST":
                    conn.commit()
            finally:
                conn.close()
            self._respond(status, content_type, body, extra)

        def do_GET(self) -> None:  # noqa: N802
            self._go("GET")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            self._go("POST", self.rfile.read(length) if length else b"")

        def log_message(self, fmt: str, *args) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()
