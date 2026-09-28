import tempfile
import unittest
from pathlib import Path

from portfolio_os.daemon import run_daemon
from portfolio_os.db import connect
from portfolio_os.engine import claim_lock, utcnow
from portfolio_os.exclusion import ExclusionError
from portfolio_os.execute import execute_startup, judge_render
from portfolio_os.workspace import ScopeError, commit_owned, startup_roots


class ExecuteTests(unittest.TestCase):
    def test_scope_rejects_private_and_sibling(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "openlegal-data").mkdir()
        (root / "acme").mkdir()
        (root / "other").mkdir()
        with self.assertRaises(ExclusionError):
            startup_roots(root, "openlegal-data")
        roots = startup_roots(root, "acme")
        self.assertEqual(roots, [(root / "acme").resolve()])

    def test_stale_lock_can_be_replaced(self) -> None:
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute(
            "INSERT INTO startups (slug, name, is_public) VALUES ('acme', 'Acme', 1)"
        )
        startup = conn.execute("SELECT id FROM startups WHERE slug = 'acme'").fetchone()
        claim_lock(conn, startup["id"], "old", minutes=30)
        conn.execute(
            "UPDATE locks SET expires_at = ? WHERE startup_id = ?",
            ("2000-01-01T00:00:00+00:00", startup["id"]),
        )
        claim_lock(conn, startup["id"], "new", minutes=5)
        holder = conn.execute("SELECT holder FROM locks WHERE startup_id = ?", (startup["id"],)).fetchone()
        self.assertEqual(holder["holder"], "new")

    def test_dirty_file_is_not_committed(self) -> None:
        repo = Path(tempfile.mkdtemp())
        (repo / "README.md").write_text("keep\n", encoding="utf-8")
        (repo / "page.txt").write_text("page\n", encoding="utf-8")
        import subprocess

        subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, capture_output=True)
        (repo / "README.md").write_text("user edit\n", encoding="utf-8")
        (repo / "page.txt").write_text("worker edit\n", encoding="utf-8")
        with self.assertRaises(ScopeError):
            commit_owned(repo, [repo / "README.md"], "nope", {"README.md"})

    def test_failed_render_creates_follow_up_without_blocking_design(self) -> None:
        root = Path(tempfile.mkdtemp())
        startup = root / "acme"
        startup.mkdir()
        (startup / "src" / "app").mkdir(parents=True)
        (startup / "src" / "app" / "page.tsx").write_text("<h1>Acme board</h1><p>A local demo.</p>", encoding="utf-8")
        (root / "openlegal-data").mkdir()
        (root / "openlegal-data" / "secret.txt").write_text("hidden", encoding="utf-8")
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute(
            "INSERT INTO startups (slug, name, is_public, health) VALUES ('acme', 'Acme', 1, 'VISUAL_QA_PENDING')"
        )
        conn.execute(
            """
            INSERT INTO deployments (startup_id, provider, project, status, blocker, timestamp)
            VALUES (1, 'vercel', 'acme', 'blocked', 'Vercel hobby daily deployment cap', ?)
            """,
            (utcnow(),),
        )
        result = execute_startup(conn, root, "acme", Path(tempfile.mkdtemp()), render=False)
        self.assertEqual(result["desktop"], "fail")
        follow = conn.execute("SELECT COUNT(*) AS n FROM work_items WHERE type = 'follow_up'").fetchone()["n"]
        self.assertGreaterEqual(follow, 1)
        founder = conn.execute("SELECT COUNT(*) AS n FROM agent_runs WHERE role = 'FOUNDER'").fetchone()["n"]
        self.assertEqual(founder, 1)
        blob = "\n".join(conn.iterdump())
        self.assertNotIn("hidden", blob)
        self.assertNotIn("openlegal-data", blob)
        self.assertEqual(result["outcome"], "VISUAL_QA_PENDING")

    def test_overflow_fails_and_empty_metrics_fail(self) -> None:
        resolution, _finding = judge_render({"w": 390, "s": 900, "t": "x" * 100}, "")
        self.assertEqual(resolution, "fail")
        resolution, _finding = judge_render(None, "")
        self.assertEqual(resolution, "fail")

    def test_daemon_stops_on_file(self) -> None:
        root = Path(tempfile.mkdtemp())
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        stop = root / "stop"
        stop.write_text("stop", encoding="utf-8")
        self.assertEqual(
            run_daemon(conn, root, root / "evidence", max_cycles=5, stop_file=stop, interval=0),
            "stopped",
        )


if __name__ == "__main__":
    unittest.main()
