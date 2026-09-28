"""Local preview and human release gates.

Dev servers bind to 127.0.0.1. Production deploy stays queued until a human
approves the exact commit. Portfolio OS never spends a Vercel deploy from here.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from portfolio_os.engine import startup_by_slug, utcnow
from portfolio_os.exclusion import ExclusionError, assert_allowed, is_excluded_name

MAX_ACTIVE = 2
PORT_BASE = 4300


def detect_profile(root: Path, portfolio_root: Path) -> dict:
    assert_allowed(root, portfolio_root)
    if is_excluded_name(root.name):
        raise ExclusionError("excluded directory")
    package = root / "package.json"
    if not package.is_file():
        return {"status": "RUN_PROFILE_REVIEW_REQUIRED", "reason": "no package.json"}
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"status": "RUN_PROFILE_REVIEW_REQUIRED", "reason": "package.json is not valid"}
    dev = (data.get("scripts") or {}).get("dev")
    if not isinstance(dev, str) or not dev.strip():
        return {"status": "RUN_PROFILE_REVIEW_REQUIRED", "reason": "no dev script"}
    manager_field = str(data.get("packageManager") or "")
    if (root / "pnpm-lock.yaml").is_file() or manager_field.startswith("pnpm"):
        manager = "pnpm"
    elif (root / "yarn.lock").is_file():
        manager = "yarn"
    elif (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
        manager = "bun"
    else:
        manager = "npm"
    return {
        "status": "READY",
        "manager": manager,
        "dev_command": f"{manager} dev",
        "health_route": "/",
        "bind": "127.0.0.1",
    }


def dev_argv(profile: dict, port: int) -> list[str]:
    manager = profile["manager"]
    flags = ["--hostname", "127.0.0.1", "--port", str(port)]
    if manager == "npm":
        return ["npm", "run", "dev", "--", *flags]
    # pnpm and yarn forward these flags to the dev script. An extra "--" becomes a directory.
    return [manager, "dev", *flags]


def chrome_capture(slug: str, url: str) -> dict:
    shot = Path("/Users/matador/startups/.redteam-evidence/shot.mjs")
    if not shot.is_file() or not url.startswith("http://127.0.0.1:"):
        return {}
    dest = Path(__file__).resolve().parents[1] / "evidence" / slug
    dest.mkdir(parents=True, exist_ok=True)
    saved = {}
    for viewport, name, width, height in (
        ("desktop", "desktop.png", "1440", "1000"),
        ("mobile", "mobile.png", "390", "844"),
    ):
        target = dest / name
        subprocess.run(
            ["node", str(shot), url, str(target), width, height],
            check=False,
            timeout=90,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if target.is_file():
            saved[viewport] = name
    return saved


def screenshot_matches(record: dict, commit: str) -> bool:
    return bool(commit) and record.get("commit") == commit


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


class PreviewManager:
    def __init__(
        self,
        conn: sqlite3.Connection,
        portfolio_root: Path,
        *,
        launcher=None,
        prober=None,
        alive=None,
        stopper=None,
        capturer=None,
        max_active: int = MAX_ACTIVE,
    ) -> None:
        self.conn = conn
        self.portfolio_root = portfolio_root
        self.launcher = launcher or _default_launcher
        self.prober = prober or _http_probe
        self.alive = alive or _alive
        self.stopper = stopper or _signal_stop
        self.capturer = capturer
        self.max_active = max_active

    def repo_for(self, slug: str) -> Path:
        if is_excluded_name(slug) or slug == "OWNER-PRIVATE":
            raise ExclusionError("excluded directory")
        startup = startup_by_slug(self.conn, slug)
        if startup is None or startup["owner_private"]:
            raise ExclusionError("excluded directory")
        root = self.portfolio_root / slug
        assert_allowed(root, self.portfolio_root)
        return root

    def start(self, slug: str) -> dict:
        root = self.repo_for(slug)
        startup = startup_by_slug(self.conn, slug)
        assert startup is not None
        current = self._current(startup["id"])
        if current and current["status"] == "RUNNING" and self.alive(current["pid"]):
            self._touch(current["id"])
            return self._public(current)
        profile = detect_profile(root, self.portfolio_root)
        if profile["status"] != "READY":
            return profile
        self._make_room()
        port = self._port()
        commit = _git(root, "rev-parse", "--short", "HEAD")
        branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
        argv = dev_argv(profile, port)
        now = utcnow()
        cursor = self.conn.execute(
            """
            INSERT INTO preview_runs (
              startup_id, surface, branch, commit_sha, port, pid, command, status, started_at, last_access
            ) VALUES (?, 'website', ?, ?, ?, NULL, ?, 'STARTING', ?, ?)
            """,
            (startup["id"], branch, commit, port, " ".join(argv), now, now),
        )
        run_id = cursor.lastrowid
        try:
            pid = self.launcher(argv, root, port)
        except OSError:
            self.conn.execute("UPDATE preview_runs SET status = 'FAILED' WHERE id = ?", (run_id,))
            return {"status": "FAILED", "port": port, "commit": commit}
        self.conn.execute("UPDATE preview_runs SET pid = ? WHERE id = ?", (pid, run_id))
        healthy = False
        deadline = time.time() + 25
        while time.time() < deadline:
            if self.prober(port):
                healthy = True
                break
            time.sleep(0.4)
        status = "RUNNING" if healthy else "UNHEALTHY"
        self.conn.execute(
            "UPDATE preview_runs SET status = ?, last_health = ? WHERE id = ?",
            (status, utcnow() if healthy else None, run_id),
        )
        row = self.conn.execute("SELECT * FROM preview_runs WHERE id = ?", (run_id,)).fetchone()
        public = self._public(row)
        if healthy and self.capturer is not None and public.get("local_url"):
            public["shots"] = self.capture(slug, public["local_url"], commit or "", branch or "")
        return public

    def capture(self, slug: str, url: str, commit: str, branch: str) -> dict:
        startup = startup_by_slug(self.conn, slug)
        if startup is None or startup["owner_private"] or self.capturer is None:
            return {}
        shots = self.capturer(slug, url)
        saved = {}
        for viewport, file_name in shots.items():
            if not file_name:
                continue
            self.conn.execute(
                """
                INSERT INTO preview_shots (
                  startup_id, viewport, commit_sha, branch, route, file_name, captured_at
                ) VALUES (?, ?, ?, ?, '/', ?, ?)
                """,
                (startup["id"], viewport, commit, branch, file_name, utcnow()),
            )
            saved[viewport] = {"file": file_name, "commit": commit}
        return saved

    def stop(self, slug: str) -> dict:
        startup = startup_by_slug(self.conn, slug)
        if startup is None or startup["owner_private"]:
            raise ExclusionError("excluded directory")
        current = self._current(startup["id"])
        if current is None:
            return {"status": "STOPPED"}
        self._stop_row(current)
        return {"status": "STOPPED", "port": current["port"]}

    def _make_room(self) -> None:
        rows = self.conn.execute(
            "SELECT * FROM preview_runs WHERE status = 'RUNNING' ORDER BY last_access ASC"
        ).fetchall()
        live = [row for row in rows if self.alive(row["pid"])]
        while len(live) >= self.max_active:
            self._stop_row(live.pop(0))

    def _stop_row(self, row) -> None:
        pid = row["pid"]
        if self.alive(pid):
            self.stopper(pid)
        self.conn.execute(
            "UPDATE preview_runs SET status = 'STOPPED', last_access = ? WHERE id = ?",
            (utcnow(), row["id"]),
        )

    def _port(self) -> int:
        used = {
            row["port"]
            for row in self.conn.execute(
                "SELECT port FROM preview_runs WHERE status IN ('STARTING', 'RUNNING') AND port IS NOT NULL"
            ).fetchall()
        }
        port = PORT_BASE
        while port in used:
            port += 1
        return port

    def _current(self, startup_id: int):
        return self.conn.execute(
            "SELECT * FROM preview_runs WHERE startup_id = ? ORDER BY id DESC LIMIT 1",
            (startup_id,),
        ).fetchone()

    def _touch(self, run_id: int) -> None:
        self.conn.execute("UPDATE preview_runs SET last_access = ? WHERE id = ?", (utcnow(), run_id))

    def _public(self, row) -> dict:
        return {
            "status": row["status"],
            "port": row["port"],
            "commit": row["commit_sha"],
            "branch": row["branch"],
            "pid": row["pid"],
            "local_url": f"http://127.0.0.1:{row['port']}" if row["port"] else None,
        }


def latest_shot(conn: sqlite3.Connection, startup_id: int, viewport: str = "desktop"):
    return conn.execute(
        """
        SELECT commit_sha, file_name, verdict, findings, captured_at
        FROM preview_shots WHERE startup_id = ? AND viewport = ?
        ORDER BY id DESC LIMIT 1
        """,
        (startup_id, viewport),
    ).fetchone()


def approve_visual(conn: sqlite3.Connection, startup_id: int, commit: str, approver: str = "owner") -> dict:
    shot = latest_shot(conn, startup_id, "desktop")
    if shot is None or shot["commit_sha"] != commit:
        return {"ok": False, "visual": "STALE_SCREENSHOT", "commit": commit}
    _gate(conn, startup_id, commit, visual="pass", approver=approver)
    return {"ok": True, "visual": "pass", "commit": commit}


def record_visual_review(conn: sqlite3.Connection, startup_id: int, commit: str, verdict: str, findings: str) -> dict:
    if verdict not in {"PASS", "PASS_WITH_FOLLOWUP", "FAIL"}:
        return {"ok": False, "state": "BAD_VERDICT"}
    conn.execute(
        """
        UPDATE preview_shots SET verdict = ?, findings = ?
        WHERE id = (
          SELECT id FROM preview_shots
          WHERE startup_id = ? AND viewport = 'desktop' AND commit_sha = ?
          ORDER BY id DESC LIMIT 1
        )
        """,
        (verdict, findings[:500], startup_id, commit),
    )
    return {"ok": True, "verdict": verdict, "commit": commit}


def approve_release(conn: sqlite3.Connection, startup_id: int, commit: str, approver: str = "owner") -> dict:
    row = _gate_row(conn, startup_id, commit)
    if row is None or row["visual"] != "pass":
        return {"ok": False, "state": "VISUAL_REVIEW_REQUIRED", "commit": commit}
    conn.execute(
        "UPDATE release_gates SET release_approval = 'approved', approver = ? WHERE id = ?",
        (approver, row["id"]),
    )
    return {"ok": True, "state": "approved", "commit": commit}


def queue_deployment(conn: sqlite3.Connection, startup_id: int, commit: str, head: str) -> dict:
    row = _gate_row(conn, startup_id, commit)
    if row is None or row["visual"] != "pass":
        return {"ok": False, "state": "VISUAL_REVIEW_REQUIRED"}
    if row["release_approval"] != "approved":
        return {"ok": False, "state": "REVIEW_REQUIRED"}
    if commit != head:
        conn.execute("UPDATE release_gates SET release_approval = 'stale', deployment = 'REAPPROVAL_REQUIRED' WHERE id = ?", (row["id"],))
        return {"ok": False, "state": "REAPPROVAL_REQUIRED"}
    conn.execute("UPDATE release_gates SET deployment = 'QUEUED' WHERE id = ?", (row["id"],))
    conn.execute(
        """
        INSERT INTO deployments (startup_id, provider, commit_sha, status, blocker, timestamp)
        VALUES (?, 'vercel', ?, 'queued', 'human approved; waiting for provider capacity', ?)
        """,
        (startup_id, commit, utcnow()),
    )
    return {"ok": True, "state": "APPROVED — QUEUED FOR DEPLOYMENT", "commit": commit}


def _gate(conn, startup_id: int, commit: str, visual: str, approver: str) -> None:
    now = utcnow()
    conn.execute(
        """
        INSERT INTO release_gates (startup_id, commit_sha, visual, release_approval, deployment, approver, created_at)
        VALUES (?, ?, ?, 'pending', 'blocked', ?, ?)
        ON CONFLICT(startup_id, commit_sha) DO UPDATE SET visual = excluded.visual, approver = excluded.approver
        """,
        (startup_id, commit, visual, approver, now),
    )


def _gate_row(conn, startup_id: int, commit: str):
    return conn.execute(
        "SELECT * FROM release_gates WHERE startup_id = ? AND commit_sha = ?",
        (startup_id, commit),
    ).fetchone()


def _signal_stop(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def _default_launcher(argv: list[str], cwd: Path, port: int) -> int:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["HOST"] = "127.0.0.1"
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc.pid


def _http_probe(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
            return 200 <= response.status < 500
    except (OSError, urllib.error.URLError):
        return False
