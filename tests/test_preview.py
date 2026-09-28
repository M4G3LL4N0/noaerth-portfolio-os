import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.exclusion import ExclusionError
from portfolio_os.preview import (
    PreviewManager,
    approve_release,
    approve_visual,
    detect_profile,
    dev_argv,
    queue_deployment,
    screenshot_matches,
)


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = connect(self.root / "portfolio.db")
        web = self.root / "acme"
        web.mkdir()
        (web / "package.json").write_text(
            '{"packageManager":"pnpm@10.0.0","scripts":{"dev":"next dev"}}',
            encoding="utf-8",
        )
        (web / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")
        (self.root / "openlegal-data").mkdir()
        self.conn.execute(
            "INSERT INTO startups (slug, name, is_public, owner_private) VALUES ('acme', 'Acme', 1, 0)"
        )
        self.conn.commit()
        self.started = []
        self.stopped = []
        self.manager = PreviewManager(
            self.conn,
            self.root,
            launcher=self._launch,
            prober=lambda port: True,
            alive=lambda pid: pid in self.started and pid not in self.stopped,
            stopper=self.stopped.append,
            max_active=2,
        )

    def _launch(self, argv, cwd, port):
        self.assertIn("127.0.0.1", argv)
        self.assertNotIn("0.0.0.0", argv)
        pid = 5000 + len(self.started)
        self.started.append(pid)
        return pid

    def test_profile_uses_pnpm_and_localhost(self):
        profile = detect_profile(self.root / "acme", self.root)
        self.assertEqual(profile["manager"], "pnpm")
        argv = dev_argv(profile, 4300)
        self.assertEqual(argv[:2], ["pnpm", "dev"])
        self.assertNotIn("--", argv)
        self.assertIn("127.0.0.1", argv)

    def test_second_start_reuses_the_running_preview(self):
        first = self.manager.start("acme")
        second = self.manager.start("acme")
        self.assertEqual(first["status"], "RUNNING")
        self.assertEqual(second["port"], first["port"])
        self.assertEqual(len(self.started), 1)

    def test_third_preview_stops_the_oldest(self):
        for name in ("one", "two", "three"):
            folder = self.root / name
            folder.mkdir()
            (folder / "package.json").write_text('{"scripts":{"dev":"next dev"}}', encoding="utf-8")
            self.conn.execute(
                "INSERT INTO startups (slug, name, is_public, owner_private) VALUES (?, ?, 1, 0)",
                (name, name),
            )
        self.conn.commit()
        self.manager.start("one")
        self.manager.start("two")
        self.manager.start("three")
        self.assertEqual(len(self.stopped), 1)

    def test_private_directory_is_refused_before_start(self):
        with self.assertRaises(ExclusionError):
            self.manager.repo_for("openlegal-data")

    def test_release_requires_visual_approval_and_the_same_commit(self):
        startup_id = self.conn.execute("SELECT id FROM startups WHERE slug = 'acme'").fetchone()["id"]
        blocked = queue_deployment(self.conn, startup_id, "abc123", "abc123")
        self.assertEqual(blocked["state"], "VISUAL_REVIEW_REQUIRED")
        approve_visual(self.conn, startup_id, "abc123")
        still = approve_release(self.conn, startup_id, "abc123")
        self.assertTrue(still["ok"])
        queued = queue_deployment(self.conn, startup_id, "abc123", "abc123")
        self.assertEqual(queued["state"], "APPROVED — QUEUED FOR DEPLOYMENT")
        stale = queue_deployment(self.conn, startup_id, "abc123", "def456")
        self.assertEqual(stale["state"], "REAPPROVAL_REQUIRED")
        self.assertFalse(screenshot_matches({"commit": "abc123"}, "def456"))

    def test_missing_dev_script_is_not_guessed(self):
        bare = self.root / "bare"
        bare.mkdir()
        (bare / "package.json").write_text("{}", encoding="utf-8")
        self.assertEqual(detect_profile(bare, self.root)["status"], "RUN_PROFILE_REVIEW_REQUIRED")


if __name__ == "__main__":
    unittest.main()
