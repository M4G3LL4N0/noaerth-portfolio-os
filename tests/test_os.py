import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.engine import (
    add_event,
    block_release,
    claim_lock,
    public_events,
    record_review,
    refresh_priorities,
)
from portfolio_os.exclusion import ExclusionError, assert_allowed, iter_top_level
from portfolio_os.ingest import discover, import_canonical, import_recovery
from portfolio_os.publish import build_public_snapshot, build_team_snapshot


def _fixture_root() -> Path:
    root = Path(tempfile.mkdtemp())
    (root / "openlegal-data").mkdir()
    (root / "openlegal-data" / "secret.txt").write_text("super-secret-matter", encoding="utf-8")
    (root / "acme").mkdir()
    (root / "PORTFOLIO_CANONICAL_PROJECTS.md").write_text(
        "\n".join(
            [
                "| Directory | Canonical Startup | Role | Public Product? | Website Location | Notes |",
                "|---|---|---|---|---|---|",
                "| openlegal-data | — | OWNER-PRIVATE | no | — | Not inspected. |",
                "| acme | Acme | PRIMARY | yes | acme/app/page.tsx | Canonical startup. |",
                "| ghost-dup | acme | WEBSITE | paired | ghost-dup/ | Not a second startup. |",
            ]
        ),
        encoding="utf-8",
    )
    (root / "WEBSITE_QUALITY_RECOVERY.md").write_text(
        "\n".join(
            [
                "| Startup | Best historical version found | Current better? | Recover needed | Design work | Product work | Final quality |",
                "|---|---|---|---|---|---|---|",
                "| acme | Prior page kept. | No | Yes | Edited. Local HTTP 200 only. | Review | IMPROVED — RENDER PENDING |",
            ]
        ),
        encoding="utf-8",
    )
    return root


class PortfolioOsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _fixture_root()
        self.db = Path(tempfile.mkdtemp()) / "portfolio.db"
        self.conn = connect(self.db)

    def test_exclusion_refuses_private_directory(self) -> None:
        private = self.root / "openlegal-data"
        with self.assertRaises(ExclusionError):
            assert_allowed(private, self.root)
        with self.assertRaises(ExclusionError):
            assert_allowed(private / "secret.txt", self.root)
        names = [path.name for path in iter_top_level(self.root)]
        self.assertNotIn("openlegal-data", names)
        self.assertIn("acme", names)

    def test_discovery_never_indexes_private_file(self) -> None:
        import_canonical(self.conn, self.root)
        import_recovery(self.conn, self.root)
        discover(self.conn, self.root)
        self.conn.commit()
        blob = "\n".join(
            line
            for line in self.conn.iterdump()
        )
        self.assertNotIn("super-secret-matter", blob)
        self.assertNotIn("openlegal-data", blob)
        self.assertIn("OWNER-PRIVATE", blob)

    def test_private_system_event_never_in_public_output(self) -> None:
        import_canonical(self.conn, self.root)
        add_event(
            self.conn,
            actor="system",
            event_type="internal_note",
            summary="PRIVATE_SYSTEM token=/Users/matador/secret openlegal-data",
            visibility="PRIVATE_SYSTEM",
        )
        add_event(
            self.conn,
            actor="NOAERTH_EDITOR",
            event_type="public_milestone",
            summary="Tried to publish /Users/matador/startups/acme and a token",
            visibility="PUBLIC",
        )
        public = build_public_snapshot(self.conn)
        encoded = json.dumps(public)
        self.assertNotIn("PRIVATE_SYSTEM", encoded)
        self.assertNotIn("/Users/", encoded)
        self.assertNotIn("openlegal", encoded.lower())
        self.assertNotIn("token", encoded.lower())
        self.assertEqual(public_events(self.conn), [])

    def test_render_pending_is_not_healthy(self) -> None:
        import_canonical(self.conn, self.root)
        import_recovery(self.conn, self.root)
        row = self.conn.execute("SELECT health FROM startups WHERE slug = 'acme'").fetchone()
        self.assertEqual(row["health"], "VISUAL_QA_PENDING")
        open_visual = self.conn.execute(
            """
            SELECT COUNT(*) AS n FROM work_items
            JOIN startups ON startups.id = work_items.startup_id
            WHERE startups.slug = 'acme' AND work_items.type LIKE 'visual_qa_%'
              AND work_items.status != 'completed'
            """
        ).fetchone()["n"]
        self.assertGreaterEqual(open_visual, 2)

    def test_failed_visual_review_creates_follow_up_and_cap_blocks_release_only(self) -> None:
        import_canonical(self.conn, self.root)
        import_recovery(self.conn, self.root)
        mobile = self.conn.execute(
            """
            SELECT work_items.id FROM work_items
            JOIN startups ON startups.id = work_items.startup_id
            WHERE startups.slug = 'acme' AND work_items.type = 'visual_qa_mobile'
            """
        ).fetchone()
        failed = record_review(
            self.conn,
            slug="acme",
            role="VISUAL_REVIEWER",
            dimension="mobile",
            finding="Mobile navigation clips the label.",
            severity="high",
            resolution="fail",
            work_item_id=mobile["id"],
        )
        self.assertEqual(failed["health"], "VISUAL_QA_PENDING")
        follow = self.conn.execute(
            "SELECT * FROM work_items WHERE id = ?",
            (failed["follow_up_id"],),
        ).fetchone()
        self.assertEqual(follow["status"], "queued")
        self.assertNotEqual(follow["assigned_role"], "VISUAL_REVIEWER")
        # The reviewer does not get to close the implementation by reviewing their own build.
        record_review(
            self.conn,
            slug="acme",
            role="FRONTEND_ENGINEER",
            dimension="mobile",
            finding="Claimed fixed.",
            severity="low",
            resolution="pass",
            work_item_id=follow["id"],
        )
        still = self.conn.execute("SELECT status FROM work_items WHERE id = ?", (follow["id"],)).fetchone()
        self.assertNotEqual(still["status"], "completed")
        desktop = self.conn.execute(
            """
            SELECT work_items.id FROM work_items
            JOIN startups ON startups.id = work_items.startup_id
            WHERE startups.slug = 'acme' AND work_items.type = 'visual_qa_desktop'
            """
        ).fetchone()
        record_review(
            self.conn,
            slug="acme",
            role="VISUAL_REVIEWER",
            dimension="desktop",
            finding="Desktop layout is readable.",
            severity="low",
            resolution="pass",
            work_item_id=desktop["id"],
        )
        record_review(
            self.conn,
            slug="acme",
            role="VISUAL_REVIEWER",
            dimension="mobile",
            finding="Mobile layout is readable after the fix.",
            severity="low",
            resolution="pass",
            work_item_id=mobile["id"],
        )
        record_review(
            self.conn,
            slug="acme",
            role="QA_ENGINEER",
            dimension="mobile",
            finding="Navigation no longer clips.",
            severity="low",
            resolution="pass",
            work_item_id=follow["id"],
        )
        health = block_release(self.conn, "acme", "Vercel hobby daily deployment cap")
        self.assertEqual(health, "RELEASE_READY")
        visual = self.conn.execute(
            """
            SELECT status FROM work_items
            JOIN startups ON startups.id = work_items.startup_id
            WHERE startups.slug = 'acme' AND work_items.type = 'visual_qa_desktop'
            """
        ).fetchone()
        self.assertEqual(visual["status"], "completed")
        deploy = self.conn.execute(
            """
            SELECT status, blocked_reason FROM work_items
            JOIN startups ON startups.id = work_items.startup_id
            WHERE startups.slug = 'acme' AND work_items.type = 'deploy_retry'
            """
        ).fetchone()
        self.assertEqual(deploy["status"], "blocked")
        public = json.dumps(build_public_snapshot(self.conn))
        team = json.dumps(build_team_snapshot(self.conn))
        self.assertNotIn("PRIVATE_SYSTEM", public)
        self.assertIn("RELEASE_READY", team)
        self.assertNotIn("openlegal", public.lower())

    def test_lock_blocks_second_holder(self) -> None:
        import_canonical(self.conn, self.root)
        startup = self.conn.execute("SELECT id FROM startups WHERE slug = 'acme'").fetchone()
        claim_lock(self.conn, startup["id"], "designer")
        with self.assertRaises(RuntimeError):
            claim_lock(self.conn, startup["id"], "engineer")

    def test_priority_factors_are_stored(self) -> None:
        import_canonical(self.conn, self.root)
        import_recovery(self.conn, self.root)
        refresh_priorities(self.conn)
        row = self.conn.execute(
            "SELECT priority_factors FROM work_items WHERE priority_factors IS NOT NULL LIMIT 1"
        ).fetchone()
        factors = json.loads(row["priority_factors"])
        self.assertIn("user_facing", factors)
        self.assertIn("visual_regression_risk", factors)


if __name__ == "__main__":
    unittest.main()
