"""Small internal HTTP API. Labs reads public snapshots. Team actions are allowlisted."""

from __future__ import annotations

import json
import sqlite3

from portfolio_os.engine import ensure_work, startup_by_slug
from portfolio_os.publish import build_public_snapshot

ALLOWED_ACTIONS = frozenset({"create_task", "pause", "resume", "priority", "reject_review"})


def dispatch(conn: sqlite3.Connection, method: str, path: str, headers: dict, body: dict | None, token: str) -> tuple[int, dict]:
    if path == "/public/v1/snapshot" and method == "GET":
        return 200, build_public_snapshot(conn)
    presented = headers.get("authorization", "")
    if not token or presented != f"Bearer {token}":
        return 401, {"error": "unauthorized"}
    if path == "/api/v1/health" and method == "GET":
        return 200, {"ok": True}
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
