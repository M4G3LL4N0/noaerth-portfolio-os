import json
import tempfile
import unittest
from pathlib import Path

from portfolio_os.db import connect
from portfolio_os.engine import add_event
from portfolio_os.exclusion import ExclusionError
from portfolio_os.publish import build_public_snapshot
from portfolio_os.daemon import heartbeat_body
from portfolio_os.dossier import classify_frontend, render_dossier, write_dossier
from portfolio_os.reconcile import allow_product_state, classify_workspace_path
from portfolio_os.squads import REASONING_MODEL, assess_public_story, assign_shards
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
        for action in ("resume", "create_task", "reject_review", "accept_review", "reopen", "trigger_review"):
            status, _body = dispatch(
                conn,
                "POST",
                "/api/v1/actions",
                {"authorization": "Bearer " + ("x" * 16)},
                {"action": action, "slug": "acme", "title": "bounded"},
                "x" * 16,
            )
            self.assertEqual(status, 200, action)
        status, view = dispatch(conn, "GET", "/public/v1/activity", {}, None, "")
        self.assertEqual(status, 200)
        self.assertNotIn("team_action", json.dumps(view))
        conn.execute(
            "INSERT INTO startups (slug, name, is_public, owner_private) VALUES ('hidden', 'Hidden', 0, 1)"
        )
        status, _body = dispatch(
            conn,
            "POST",
            "/api/v1/actions",
            {"authorization": "Bearer " + ("x" * 16)},
            {"action": "pause", "slug": "hidden"},
            "x" * 16,
        )
        self.assertEqual(status, 404)

    def test_dossier_uses_real_heading_and_refuses_private_paths(self) -> None:
        root = Path(tempfile.mkdtemp())
        startup = root / "acme"
        (startup / "app").mkdir(parents=True)
        (startup / "app" / "page.tsx").write_text("<h1>Signed access for a door</h1>", encoding="utf-8")
        (startup / "package.json").write_text('{"scripts":{"build":"next build"}}', encoding="utf-8")
        private = root / "openlegal-data"
        private.mkdir()
        (private / "secret.txt").write_text("hidden", encoding="utf-8")
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute("INSERT INTO startups (slug, name, is_public) VALUES ('acme', 'Acme', 1)")
        startup_id = conn.execute("SELECT id FROM startups WHERE slug='acme'").fetchone()["id"]
        dest = write_dossier(startup, root, "acme", Path(tempfile.mkdtemp()), conn, startup_id)
        text = (dest / "UNDERSTANDING.md").read_text(encoding="utf-8")
        self.assertIn("Signed access for a door", text)
        self.assertIn("UNKNOWN", text)
        blob = "".join(path.read_text(encoding="utf-8") for path in dest.iterdir())
        self.assertNotIn("/Users/", blob)
        self.assertNotIn("hidden", blob)
        docs = render_dossier("access-layer", {"homepage": "app/page.tsx", "heading": "Signed access", "routes": [], "tooling": {}}, None)
        self.assertIn("UNKNOWN", docs["UNDERSTANDING.md"])

    def test_root_app_is_canonical_when_src_app_is_unreferenced(self) -> None:
        root = Path(tempfile.mkdtemp())
        (root / "app").mkdir()
        (root / "app" / "page.tsx").write_text("<h1>Served page</h1>", encoding="utf-8")
        (root / "src" / "app").mkdir(parents=True)
        (root / "src" / "app" / "page.tsx").write_text("<h1>Old platform</h1>", encoding="utf-8")
        found = classify_frontend(root)
        self.assertEqual(found["canonical_entrypoint"], "app/page.tsx")
        self.assertIn("src/app/page.tsx", found["dead_paths"])
        docs = render_dossier("acme", {"homepage": "app/page.tsx", "heading": "Served page", "routes": [], "tooling": {}, "frontend": found}, None)
        self.assertIn("BUILD_PLAN.md", docs)
        self.assertIn("NO_SAFE_HIGH_VALUE_CHANGE", docs["BUILD_PLAN.md"])
        self.assertIn("src/app/page.tsx", docs["BUILD_MAP.md"])
        beat = heartbeat_body(root)
        self.assertIn("schema", beat)
        self.assertIn(REASONING_MODEL, beat)
        self.assertNotIn("/Users/", beat)
        shards = assign_shards(["b", "a", "c"] + [f"s{i}" for i in range(12)])
        self.assertEqual(len(shards), 15)
        self.assertEqual(shards["a"], "SHARD-01")
        self.assertEqual(len(set(shards.values())), 2)
        self.assertEqual(
            assess_public_story("There is no store. There is no checkout. Not a live network."),
            "TOO_DEFENSIVE",
        )
        self.assertEqual(assess_public_story("Discover Bourgaeux. A new digital home for taste."), "PASS")

    def test_workspace_classification_ignores_artifacts_and_does_not_invent_a_working_flow(self) -> None:
        self.assertEqual(classify_workspace_path("node_modules/next/package.json"), "DEPENDENCY_ARTIFACT")
        self.assertEqual(classify_workspace_path(".autobuilder/project-state.json"), "GENERATED")
        self.assertEqual(
            classify_workspace_path("app/customers/page.tsx", "This page is live so navigation and portfolio links do not 404."),
            "GENERATED",
        )
        self.assertEqual(allow_product_state("STATIC_DEMO", "WORKING_PRIMARY_WORKFLOW", evidence=False), "STATIC_DEMO")
        self.assertEqual(allow_product_state("STATIC_DEMO", "INTERACTIVE_DEMO", evidence=True), "INTERACTIVE_DEMO")

    def test_private_directory_is_not_a_studio_root(self) -> None:
        root = Path(tempfile.mkdtemp())
        private = root / "openlegal-data"
        private.mkdir()
        from portfolio_os.studio import assert_studio_root

        with self.assertRaises(ExclusionError):
            assert_studio_root(private, root)
