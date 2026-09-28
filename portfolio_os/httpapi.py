"""Small internal HTTP API. Labs reads public snapshots. Team actions are allowlisted."""

from __future__ import annotations

import json
import sqlite3

from portfolio_os.engine import add_event, ensure_work, startup_by_slug
from portfolio_os.publish import build_public_snapshot

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


def dispatch(conn: sqlite3.Connection, method: str, path: str, headers: dict, body: dict | None, token: str) -> tuple[int, dict]:
    public_routes = {
        "/public/v1/snapshot": "snapshot",
        "/public/v1/activity": "activity",
        "/public/v1/week": "week",
        "/public/v1/projects": "projects",
    }
    if path in public_routes and method == "GET":
        return 200, _public_view(conn, public_routes[path])
    presented = headers.get("authorization", "")
    if not token or presented != f"Bearer {token}":
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


def serve(conn: sqlite3.Connection, host: str, port: int, token: str) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802
            status, payload = dispatch(conn, "GET", self.path.split("?")[0], {k.lower(): v for k, v in self.headers.items()}, None, token)
            self._send(status, payload)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                body = {}
            status, payload = dispatch(conn, "POST", self.path.split("?")[0], {k.lower(): v for k, v in self.headers.items()}, body, token)
            conn.commit()
            self._send(status, payload)

        def log_message(self, fmt: str, *args) -> None:
            return

    ThreadingHTTPServer((host, port), Handler).serve_forever()
