import json
import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.engine import add_event
from portfolio_os.exclusion import ExclusionError
from portfolio_os.publish import build_public_snapshot
from portfolio_os.httpapi import dispatch
from portfolio_os.studio import (
    compiled_homepage,
    material_coverage,
    public_studio_line,
    record_material,
    store_venture_review,
    venture_question,
)


class StudioTests(unittest.TestCase):
    def test_compiled_homepage_skips_excluded_src_app(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "src" / "app").mkdir(parents=True)
        (root / "app").mkdir()
        (root / "src" / "app" / "page.tsx").write_text("x" * 8000, encoding="utf-8")
        (root / "app" / "page.tsx").write_text("<h1>Served</h1>", encoding="utf-8")
        (root / "tsconfig.json").write_text(json.dumps({"exclude": ["src/app"]}), encoding="utf-8")
        page = compiled_homepage(root)
        self.assertIsNotNone(page)
        assert page is not None
        self.assertEqual(page.name, "page.tsx")
        self.assertIn("Served", page.read_text(encoding="utf-8"))

    def test_venture_review_is_bounded_and_public_line_hides_failures(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "app").mkdir()
        (root / "app" / "page.tsx").write_text("<h1>Board</h1>" + ("section " * 400), encoding="utf-8")
        review = venture_question(root)
        self.assertLessEqual(len(review["now"]), 3)
        self.assertIsNone(public_studio_line("Acme", "mobile header wraps"))
        line = public_studio_line("AccessXWorld", "designing")
        self.assertIsNotNone(line)
        assert line is not None
        self.assertNotIn("fail", line.lower())
        self.assertIsNone(public_studio_line("openlegal-data", "designing"))

    def test_material_ledger_and_public_snapshot_stay_clean(self) -> None:
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute(
            "INSERT INTO startups (slug, name, is_public) VALUES ('acme', 'Acme', 1)"
        )
        startup = conn.execute("SELECT id FROM startups WHERE slug = 'acme'").fetchone()
        store_venture_review(
            conn,
            startup["id"],
            {"question": "What next?", "now": ["Replace the placeholder flow."], "later": [], "reason": "Historical regression"},
        )
        record_material(conn, startup["id"], category="design", summary="Restored the access flow.", commit_sha="abc")
        coverage = material_coverage(conn)
        self.assertEqual(coverage["materially_improved"], 1)
        add_event(
            conn,
            actor="NOAERTH_EDITOR",
            event_type="public_designing",
            summary="AccessXWorld — Rebuilding the public experience.",
            visibility="PUBLIC",
            startup_id=startup["id"],
        )
        add_event(
            conn,
            actor="QA",
            event_type="review_failed",
            summary="TEAM ONLY failure at /Users/nope",
            visibility="TEAM",
            startup_id=startup["id"],
        )
        public = json.dumps(build_public_snapshot(conn))
        self.assertNotIn("TEAM ONLY", public)
        self.assertNotIn("/Users/", public)
        self.assertIn("Rebuilding the public experience", public)

    def test_internal_action_requires_a_token_and_public_feed_stays_public(self) -> None:
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute("INSERT INTO startups (slug, name, is_public) VALUES ('acme', 'Acme', 1)")
        status, body = dispatch(conn, "GET", "/public/v1/snapshot", {}, None, "x" * 16)
        self.assertEqual(status, 200)
        self.assertNotIn("PRIVATE_SYSTEM", json.dumps(body))
        status, body = dispatch(conn, "POST", "/api/v1/actions", {}, {"action": "pause", "slug": "acme"}, "x" * 16)
        self.assertEqual(status, 401)
        status, _body = dispatch(
            conn,
            "POST",
            "/api/v1/actions",
            {"authorization": "Bearer " + ("x" * 16)},
            {"action": "pause", "slug": "acme"},
            "x" * 16,
        )
        self.assertEqual(status, 200)
        paused = conn.execute("SELECT paused FROM startups WHERE slug='acme'").fetchone()["paused"]
        self.assertEqual(paused, 1)
        status, _body = dispatch(
            conn,
            "POST",
            "/api/v1/actions",
            {"authorization": "Bearer " + ("x" * 16)},
            {"action": "rm -rf", "slug": "acme"},
            "x" * 16,
        )
        self.assertEqual(status, 400)

    def test_private_directory_is_not_a_studio_root(self) -> None:
        root = Path(tempfile.mkdtemp())
        private = root / "openlegal-data"
        private.mkdir()
        from portfolio_os.studio import assert_studio_root

        with self.assertRaises(ExclusionError):
            assert_studio_root(private, root)
