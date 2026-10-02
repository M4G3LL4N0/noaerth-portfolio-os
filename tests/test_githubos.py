"""Tests for the GitHubOS inventory, scoring, classification, and rendering.

The important properties here are that the output is deterministic and that
missing evidence lowers the score rather than being assumed away. A tool that
publishes a number it did not measure is the exact failure this project exists
to prevent, so it must not have one either.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from portfolio_os import githubos


def _repo(**over):
    base = {
        "name": "demo",
        "full_name": "M4G3LL4N0/demo",
        "visibility": "public",
        "archived": False,
        "description": "A demo repository.",
        "homepage": "https://example.com",
        "topics": ["python", "open-source"],
        "language": "Python",
        "stargazers_count": 0,
        "forks_count": 0,
        "open_issues_count": 0,
        "has_discussions": True,
        "social_preview": True,
        "release_count": 1,
        "latest_release": "v1.0.0",
        "test_count": 120,
        "has_ci": True,
        "pushed_at": "2026-10-01T00:00:00Z",
    }
    base.update(over)
    return base


ALL_FILES = ["README.md", "LICENSE", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"]


class Scoring(unittest.TestCase):
    def test_full_community_surface_scores_highest(self):
        best = githubos.score_repository(_repo(), ALL_FILES)
        poor = githubos.score_repository(_repo(), ["README.md"])
        self.assertGreater(best.score, poor.score)
        self.assertEqual(best.score, 100)
        self.assertLessEqual(best.score, 100)

    def test_score_is_deterministic(self):
        a = githubos.score_repository(_repo(), ALL_FILES)
        b = githubos.score_repository(_repo(), ALL_FILES)
        self.assertEqual(a.score, b.score)
        self.assertEqual(a.parts, b.parts)

    def test_missing_evidence_lowers_score_and_is_recorded(self):
        r = githubos.score_repository(_repo(), ["README.md"])
        self.assertIn("missing LICENSE", r.reasons)
        self.assertLess(r.score, 100)

    def test_no_tests_lowers_score_and_says_so(self):
        with_tests = githubos.score_repository(_repo(test_count=300), ALL_FILES)
        without = githubos.score_repository(_repo(test_count=0), ALL_FILES)
        self.assertGreater(with_tests.score, without.score)
        self.assertIn("no recorded tests", without.reasons)

    def test_more_tests_score_at_least_as_high(self):
        scores = [
            githubos.score_repository(_repo(test_count=n), ALL_FILES).score
            for n in (0, 10, 50, 150, 400)
        ]
        self.assertEqual(scores, sorted(scores))

    def test_stale_repository_scores_lower_and_is_flagged(self):
        fresh = githubos.score_repository(_repo(), ALL_FILES)
        stale = githubos.score_repository(_repo(pushed_at="2024-01-01T00:00:00Z"), ALL_FILES)
        self.assertGreater(fresh.score, stale.score)
        self.assertTrue(any("stale" in reason for reason in stale.reasons))

    def test_security_blocker_zeroes_the_score(self):
        blocked = githubos.score_repository(
            _repo(security_blockers=["generic-api-key at src/keys.py"]), ALL_FILES
        )
        self.assertEqual(blocked.score, 0)
        self.assertTrue(blocked.blocked())

    def test_ip_blocker_zeroes_the_score(self):
        blocked = githubos.score_repository(_repo(ip_blocker="third-party assets"), ALL_FILES)
        self.assertEqual(blocked.score, 0)


class Classification(unittest.TestCase):
    def _classify(self, **over):
        files = over.pop("files", ALL_FILES)
        repo = _repo(**over)
        return githubos.classify(repo, githubos.score_repository(repo, files))

    def test_public_and_ready_is_flagship(self):
        self.assertEqual(self._classify(), "PUBLIC_FLAGSHIP")

    def test_private_is_internal(self):
        self.assertEqual(self._classify(visibility="private"), "PRIVATE_INTERNAL")

    def test_private_experiment_is_flagged(self):
        self.assertEqual(
            self._classify(visibility="private", experimental=True), "PRIVATE_EXPERIMENT"
        )

    def test_blocker_overrides_public_visibility(self):
        self.assertEqual(
            self._classify(security_blockers=["leaked token"]), "PRIVATE_BLOCKED"
        )

    def test_ip_blocker_overrides_public_visibility(self):
        self.assertEqual(self._classify(ip_blocker="unclear ownership"), "PRIVATE_BLOCKED")

    def test_archived_is_archive(self):
        self.assertEqual(self._classify(archived=True), "PUBLIC_ARCHIVE")

    def test_identity_repo_never_becomes_a_flagship(self):
        repo = _repo(name="why-are-you-here")
        self.assertEqual(
            githubos.classify(repo, githubos.score_repository(repo, ALL_FILES)),
            "PUBLIC_ARCHIVE",
        )

    def test_thin_public_repo_is_a_candidate_not_a_flagship(self):
        self.assertEqual(
            self._classify(files=["README.md"], test_count=0, has_ci=False), "PUBLIC_CANDIDATE"
        )


class Inventory(unittest.TestCase):
    def test_totals_add_up(self):
        inv = githubos.build_inventory([
            _repo(name="a"), _repo(name="b", visibility="private"), _repo(name="c"),
        ], {"a": ALL_FILES, "b": ALL_FILES, "c": ALL_FILES})
        self.assertEqual(inv["totals"]["repositories"], 3)
        self.assertEqual(inv["totals"]["public"], 2)
        self.assertEqual(inv["totals"]["private"], 1)

    def test_repositories_are_sorted_by_name(self):
        inv = githubos.build_inventory([_repo(name="z"), _repo(name="a")])
        names = [r["name"] for r in inv["repositories"]]
        self.assertEqual(names, ["a", "z"])

    def test_blocked_list_names_the_repository_and_the_reason(self):
        inv = githubos.build_inventory([
            _repo(name="leaky", security_blockers=["generic-api-key in src/a.py"]),
        ])
        self.assertEqual(inv["blocked"][0]["name"], "leaky")
        self.assertIn("src/a.py", inv["blocked"][0]["blockers"][0])

    def test_stale_public_repositories_are_listed(self):
        inv = githubos.build_inventory([
            _repo(name="old", pushed_at="2024-01-01T00:00:00Z"),
            _repo(name="new", pushed_at="2026-10-01T00:00:00Z"),
        ])
        self.assertEqual(inv["stale_public"], ["old"])

    def test_inventory_is_json_serialisable(self):
        inv = githubos.build_inventory([_repo()], {"demo": ALL_FILES})
        json.dumps(inv)  # must not raise


class Rendering(unittest.TestCase):
    def setUp(self):
        self.inventory = githubos.build_inventory([
            _repo(name="alpha", test_count=310, latest_release="v2.0.0"),
            _repo(name="beta", test_count=0, latest_release=""),
        ], {"alpha": ALL_FILES, "beta": ALL_FILES})

    def test_block_contains_only_evidence_backed_facts(self):
        block = githubos.render_block(self.inventory, ["alpha"])
        self.assertIn("alpha", block)
        self.assertIn("2.0.0", block)
        # beta has no recorded test count and must render as a dash, not a 0.
        self.assertNotIn("beta — 0 tests", block)

    def test_table_uses_dash_for_missing_test_counts(self):
        block = githubos.render_block(self.inventory, [])
        self.assertIn("| — |", block)

    def test_unknown_pin_names_are_skipped_not_crashed(self):
        block = githubos.render_block(self.inventory, ["does-not-exist"])
        self.assertNotIn("does-not-exist", block)

    def test_pipe_characters_are_escaped(self):
        inv = githubos.build_inventory([_repo(description="a | b")], {"demo": ALL_FILES})
        self.assertIn("a \\| b", githubos.render_block(inv, []))

    def test_long_descriptions_are_clipped(self):
        inv = githubos.build_inventory([_repo(description="x" * 300)], {"demo": ALL_FILES})
        block = githubos.render_block(inv, [])
        self.assertNotIn("x" * 300, block)
        self.assertIn("…", block)


class VersionOrdering(unittest.TestCase):
    def test_prerelease_sorts_below_its_own_release(self):
        self.assertGreater(
            githubos._version_key("v1.0.0"), githubos._version_key("v1.0.0-rc.1")
        )

    def test_newer_major_wins_over_newer_minor(self):
        self.assertGreater(githubos._version_key("v1.0.0"), githubos._version_key("v0.9.9"))

    def test_alphabetical_would_be_wrong(self):
        # String sort ranks v0.9.0 above v0.10.0, because "9" > "1".
        self.assertGreater("v0.9.0", "v0.10.0")
        self.assertGreater(githubos._version_key("v0.10.0"), githubos._version_key("v0.9.0"))

    def test_unparseable_tags_do_not_crash(self):
        self.assertIsInstance(githubos._version_key("nightly"), tuple)
        self.assertIsInstance(githubos._version_key(""), tuple)

    def test_releases_render_newest_first(self):
        inv = githubos.build_inventory([
            _repo(name="a", latest_release="v1.0.0-rc.1"),
            _repo(name="b", latest_release="v0.5.0"),
        ], {"a": ALL_FILES, "b": ALL_FILES})
        block = githubos.render_block(inv, [])
        self.assertLess(block.index("v1.0.0-rc.1"), block.index("v0.5.0"))


class MarkerReplacement(unittest.TestCase):
    README = (
        "# Title\n\nbefore\n\n"
        f"{githubos.START_MARKER}\nold content\n{githubos.END_MARKER}\n\nafter\n"
    )

    def test_only_content_between_markers_is_replaced(self):
        out = githubos.replace_block(self.README, "new content")
        self.assertIn("before", out)
        self.assertIn("after", out)
        self.assertIn("new content", out)
        self.assertNotIn("old content", out)

    def test_replacement_is_idempotent(self):
        once = githubos.replace_block(self.README, "new content")
        twice = githubos.replace_block(once, "new content")
        self.assertEqual(once, twice)

    def test_missing_markers_raise_rather_than_corrupting_the_file(self):
        with self.assertRaises(ValueError):
            githubos.replace_block("# No markers here\n", "content")

    def test_exactly_one_marker_pair_is_replaced(self):
        doubled = self.README + "\n" + githubos.START_MARKER + "\nx\n" + githubos.END_MARKER
        out = githubos.replace_block(doubled, "new content")
        self.assertEqual(out.count("new content"), 1)


class Cli(unittest.TestCase):
    def test_scan_then_render_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            repos = Path(tmp) / "repos.json"
            repos.write_text(json.dumps([_repo(name="alpha")]), encoding="utf-8")
            inventory_path = Path(tmp) / "inventory.json"

            import subprocess
            import sys

            env_root = str(Path(__file__).resolve().parents[1])
            scan = subprocess.run(
                [sys.executable, "-m", "portfolio_os", "githubos", "scan",
                 str(repos), "--out", str(inventory_path)],
                capture_output=True, text=True, cwd=env_root,
            )
            self.assertEqual(scan.returncode, 0, scan.stderr)
            self.assertTrue(inventory_path.exists())

            profile = Path(tmp) / "README.md"
            profile.write_text(
                f"# Profile\n\n{githubos.START_MARKER}\n{githubos.END_MARKER}\n",
                encoding="utf-8",
            )
            render = subprocess.run(
                [sys.executable, "-m", "portfolio_os", "githubos", "render",
                 str(inventory_path), "--profile", str(profile), "--pins", "alpha"],
                capture_output=True, text=True, cwd=env_root,
            )
            self.assertEqual(render.returncode, 0, render.stderr)
            body = profile.read_text(encoding="utf-8")
            self.assertIn("alpha", body)
            self.assertIn("# Profile", body)

            # Second run must be a no-op, so CI can call it unconditionally.
            again = subprocess.run(
                [sys.executable, "-m", "portfolio_os", "githubos", "render",
                 str(inventory_path), "--profile", str(profile), "--pins", "alpha"],
                capture_output=True, text=True, cwd=env_root,
            )
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("No write", again.stdout)

    def test_render_check_fails_when_profile_is_stale(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory_path = Path(tmp) / "inventory.json"
            inventory_path.write_text(
                json.dumps(githubos.build_inventory([_repo(name="alpha")], {"alpha": ALL_FILES})),
                encoding="utf-8",
            )
            profile = Path(tmp) / "README.md"
            profile.write_text("# Profile\n\nstale body\n", encoding="utf-8")

            import subprocess
            import sys

            env_root = str(Path(__file__).resolve().parents[1])
            check = subprocess.run(
                [sys.executable, "-m", "portfolio_os", "githubos", "render",
                 str(inventory_path), "--profile", str(profile), "--check"],
                capture_output=True, text=True, cwd=env_root,
            )
            # No markers in the profile, so it must refuse rather than clobber.
            self.assertEqual(check.returncode, 2)


if __name__ == "__main__":
    unittest.main()
