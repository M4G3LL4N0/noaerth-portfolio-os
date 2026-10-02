"""External intelligence: reuse-first discipline.

These tests pin the rules that make the output trustworthy rather than merely
interesting: never claim more than was retrieved, never classify a weak signal as
high fit, never auto-authorise a fork, never reuse without a license.
"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from portfolio_os import external as ex
from portfolio_os.db import connect
from portfolio_os.scout import EcosystemScout, IntegrationEngineer


def _plan(slug="evalforge", category="evaluation", workflow=""):
    """A realistic plan. A deliberately empty plan relaxes the relevance gate,
    because a thin plan cannot measure relevance at all."""
    plan = ex.build_query_plan(slug, slug, None, category=category)
    if workflow:
        plan.primary_workflow = workflow
    return plan


#: Same plan, but carrying real product vocabulary.
def _rich_plan():
    return _plan(workflow="Compare model responses with a human approval gate")


class TestQueryGeneration(unittest.TestCase):
    def test_query_count_is_bounded(self):
        plan = _plan()
        self.assertGreaterEqual(len(plan.queries), ex.MIN_QUERIES_PER_STARTUP - 1)
        self.assertLessEqual(len(plan.queries), ex.MAX_QUERIES_PER_STARTUP)

    def test_queries_are_not_just_the_name(self):
        plan = _plan()
        substantive = [q for q in plan.queries if plan.slug.lower() not in q]
        self.assertGreaterEqual(len(substantive), 3)

    def test_category_from_compound_identity(self):
        # "procure" only appears inside "fastprocure"; a word-boundary test misses it.
        self.assertEqual(ex.infer_category("fastprocure-ai", "fastprocure-ai", ""), "procurement")
        self.assertEqual(ex.infer_category("evalforge", "evalforge", ""), "evaluation")
        self.assertEqual(ex.infer_category("deploylocal", "deploylocal", ""), "deployment")

    def test_incidental_prose_does_not_win(self):
        # The README says "evaluating AI startups"; identity still decides.
        self.assertEqual(
            ex.infer_category("fastprocure-ai", "FastProcure AI", "evaluating AI startups"),
            "procurement",
        )

    def test_generic_category_needs_corroboration(self):
        self.assertNotEqual(ex.infer_category("widgetco", "widget", "data pipeline"), "evaluation")

    def test_tech_vocabulary_never_becomes_a_query(self):
        terms = ex._distinctive_terms("Next.js 16 TypeScript Tailwind Vitest router")
        for term in terms:
            self.assertNotIn(term, ex._TECH_NOISE)

    def test_queries_are_deduped_and_lowercased(self):
        plan = _plan()
        self.assertEqual(len(plan.queries), len(set(plan.queries)))
        self.assertTrue(all(q == q.lower() for q in plan.queries))


class TestDeterministicFilter(unittest.TestCase):
    def _c(self, **kw):
        base = dict(
            repo="acme/widget", owner="acme", name="widget", description="a useful tool",
            stars=100, language="Python", license_spdx="mit", activity="2026-09-01T00:00:00Z",
        )
        base.update(kw)
        return ex.Candidate(**base)

    def test_first_party_is_never_a_candidate(self):
        candidate = self._c(owner="M4G3LL4N0")
        kept, dropped = ex.deterministic_filter([candidate], _plan())
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].filter_reason, "first_party")

    def test_tiny_fork_dropped(self):
        kept, dropped = ex.deterministic_filter([self._c(stars=1)], _plan())
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].filter_reason, "tiny_fork")

    def test_mirror_dropped(self):
        kept, dropped = ex.deterministic_filter(
            [self._c(description="mirror of some other project")], _plan()
        )
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].filter_reason, "generated_mirror")

    def test_interested_abandoned_work_is_kept(self):
        candidate = self._c(archived=True, activity="2020-01-01T00:00:00Z", stars=4000)
        kept, _ = ex.deterministic_filter([candidate], _plan())
        self.assertEqual(len(kept), 1, "abandoned but substantial work must survive")

    def test_stale_and_small_is_dropped(self):
        candidate = self._c(activity="2015-01-01T00:00:00Z", stars=10)
        kept, dropped = ex.deterministic_filter([candidate], _plan())
        self.assertEqual(len(kept), 0)
        self.assertEqual(dropped[0].filter_reason, "stale_and_small")

    def test_every_dropped_candidate_carries_a_reason(self):
        candidates = [self._c(stars=0), self._c(owner="M4G3LL4N0", repo="x/y")]
        _, dropped = ex.deterministic_filter(candidates, _plan())
        self.assertTrue(all(c.filter_reason for c in dropped))


class TestLicenseClassification(unittest.TestCase):
    def test_permissive(self):
        self.assertEqual(ex.classify_license("MIT")[0], ex.PERMISSIVE)
        self.assertEqual(ex.classify_license("Apache-2.0")[0], ex.PERMISSIVE)

    def test_copyleft_needs_review(self):
        self.assertEqual(ex.classify_license("GPL-3.0")[0], ex.COPYLEFT_REVIEW)
        self.assertEqual(ex.classify_license("AGPL-3.0")[0], ex.COPYLEFT_REVIEW)

    def test_no_license_is_not_reusable(self):
        self.assertEqual(ex.classify_license("")[0], ex.UNKNOWN)

    def test_noassertion_is_unknown_not_proprietary(self):
        # NOASSERTION is GitHub's "we could not determine a license", which is
        # not the same as a proprietary one. It stays UNKNOWN, which still means
        # do not copy until resolved. This corrects an earlier expectation in
        # this file that contradicted test_no_license_is_not_reusable.
        self.assertEqual(ex.classify_license("NOASSERTION")[0], ex.UNKNOWN)

    def test_declared_proprietary_is_rejected(self):
        self.assertEqual(ex.classify_license("proprietary")[0], ex.PROPRIETARY)

    def test_unrecognised_license_is_unknown_not_assumed_permissive(self):
        self.assertEqual(ex.classify_license("Weird-Corp-1.0")[0], ex.UNKNOWN)

    def test_obligation_text_is_actionable(self):
        _, note = ex.classify_license("GPL-3.0")
        self.assertIn("copyleft", note.lower())


class TestReuseFit(unittest.TestCase):
    def test_factors_are_all_reported(self):
        candidate = ex.Candidate(
            repo="a/b", name="b", description="llm evaluation judge for agents",
            stars=900, language="Python", license_spdx="mit",
            activity="2026-09-01T00:00:00Z", topics=["llm", "evaluation", "agent"],
        )
        score, factors = ex.reuse_fit(candidate, _plan())
        self.assertEqual(len(factors), 10)
        self.assertTrue(all(0 <= v <= 100 for v in factors.values()))
        self.assertTrue(0 <= score <= 100)

    def test_unlicensed_code_scores_worse_than_the_same_code_licensed(self):
        base = dict(repo="a/b", name="b", stars=900, language="Python",
                    description="llm evaluation judge for agents",
                    topics=["llm", "evaluation", "agent"],
                    activity="2026-09-01T00:00:00Z")
        licensed, _ = ex.reuse_fit(ex.Candidate(license_spdx="mit", **base), _plan())
        unlicensed, _ = ex.reuse_fit(ex.Candidate(license_spdx="", **base), _plan())
        self.assertGreater(licensed, unlicensed)

    def test_relevance_ceiling_beats_popularity(self):
        # Stars and a permissive license must not lift an off-topic project.
        off_topic = ex.Candidate(
            repo="a/b", name="migrator", description="database migration utility",
            topics=["database"], stars=20000, license_spdx="mit",
            activity="2026-09-01T00:00:00Z",
        )
        fit, _ = ex.reuse_fit(off_topic, _rich_plan())
        self.assertLess(fit, 40)

    def test_archived_code_scores_worse_on_maintenance(self):
        base = dict(repo="a/b", name="b", stars=900, license_spdx="mit",
                    description="llm evaluation judge", topics=["llm", "evaluation"])
        live, factors_live = ex.reuse_fit(ex.Candidate(activity="2026-09-01T00:00:00Z", **base), _plan())
        dead, factors_dead = ex.reuse_fit(ex.Candidate(archived=True, activity="2019-01-01T00:00:00Z", **base), _plan())
        self.assertGreater(factors_live["maintenance"], factors_dead["maintenance"])
        self.assertGreater(live, dead)


class TestClassificationDiscipline(unittest.TestCase):
    def _candidate(self, **kw):
        base = dict(repo="a/b", name="b", description="llm evaluation judge for agents",
                    stars=900, language="Python", license_spdx="mit",
                    activity="2026-09-01T00:00:00Z", topics=["llm", "evaluation", "agent"])
        base.update(kw)
        return ex.Candidate(**base)

    def test_stars_alone_do_not_make_high_fit(self):
        # Popular but off-domain: must not be called a high-fit foundation.
        candidate = self._candidate(description="a database migration utility",
                                    topics=["database"], name="migrator")
        fit, _ = ex.reuse_fit(candidate, _plan())
        classification, _ = ex.classify_candidate(candidate, _plan(), fit)
        self.assertNotEqual(classification, ex.HIGH_FIT_OPEN_SOURCE)

    def test_domained_project_is_high_fit(self):
        candidate = self._candidate()
        plan = _rich_plan()
        fit, factors = ex.reuse_fit(candidate, plan)
        classification, _ = ex.classify_candidate(candidate, plan, fit, factors)
        self.assertEqual(classification, ex.HIGH_FIT_OPEN_SOURCE)

    def test_archived_but_strong_is_kept_as_a_candidate(self):
        candidate = self._candidate(archived=True, activity="2021-01-01T00:00:00Z")
        plan = _rich_plan()
        fit, factors = ex.reuse_fit(candidate, plan)
        classification, _ = ex.classify_candidate(candidate, plan, fit, factors)
        self.assertIn(classification, (ex.ABANDONED_BUT_USEFUL, ex.HIGH_FIT_OPEN_SOURCE))

    def test_proprietary_with_high_fit_is_a_license_risk(self):
        candidate = self._candidate(license_spdx="NOASSERTION")
        plan = _rich_plan()
        fit, factors = ex.reuse_fit(candidate, plan)
        candidate.classification, _ = ex.classify_candidate(candidate, plan, fit, factors)
        self.assertEqual(candidate.classification, ex.LICENSE_RISK)


class TestReuseDecision(unittest.TestCase):
    def _candidate(self, fit_license="mit", archived=False, **kw):
        base = dict(repo="a/b", name="b", stars=900, license_spdx=fit_license,
                    description="llm evaluation judge", topics=["llm", "evaluation"],
                    activity="2026-09-01T00:00:00Z", archived=archived)
        base.update(kw)
        return ex.Candidate(**base)

    def test_high_fit_never_self_authorises_a_fork(self):
        candidate = self._candidate()
        candidate.reuse_fit = 95
        candidate.classification = ex.HIGH_FIT_OPEN_SOURCE
        decision, risks, _ = ex.decide_reuse(candidate, _plan(), 95)
        self.assertEqual(decision, ex.OWNER_REVIEW)
        self.assertTrue(risks)

    def test_proprietary_is_rejected(self):
        decision, risks, _ = ex.decide_reuse(
            self._candidate(fit_license="proprietary"), _plan(), 90
        )
        self.assertEqual(decision, ex.REJECT)
        self.assertTrue(risks)

    def test_no_license_declared_at_all_is_rejected(self):
        # Nothing declared is not a permissive default: there are no rights.
        decision, risks, _ = ex.decide_reuse(
            self._candidate(fit_license=""), _plan(), 90
        )
        self.assertEqual(decision, ex.REJECT)
        self.assertTrue(risks)

    def test_copyleft_never_self_authorises_integration(self):
        # A strong fit must not let a copyleft license integrate itself.
        decision, risks, _ = ex.decide_reuse(
            self._candidate(fit_license="AGPL-3.0"), _plan(), 70
        )
        self.assertEqual(decision, ex.OWNER_REVIEW)
        self.assertTrue(any("opyleft" in risk for risk in risks))

    def test_unresolved_license_needs_owner_review(self):
        decision, risks, attribution = ex.decide_reuse(
            self._candidate(fit_license="Weird-1.0"), _plan(), 70
        )
        self.assertEqual(decision, ex.OWNER_REVIEW)
        self.assertFalse(attribution)

    def test_permissive_records_attribution(self):
        _, _, attribution = ex.decide_reuse(self._candidate(), _plan(), 70)
        self.assertIn("license", attribution.lower())

    def test_competitor_is_reference_only(self):
        candidate = self._candidate()
        candidate.classification = ex.DIRECT_COMPETITOR
        decision, _, _ = ex.decide_reuse(candidate, _plan(), 90)
        self.assertEqual(decision, ex.REFERENCE_DECISION)

    def test_low_fit_is_rejected(self):
        decision, _, _ = ex.decide_reuse(self._candidate(), _plan(), 10)
        self.assertEqual(decision, ex.REJECT)


class TestReuseLeverage(unittest.TestCase):
    def _c(self, fit, decision=ex.INTEGRATE, license_class=ex.PERMISSIVE):
        candidate = ex.Candidate(repo="a/b", name="b")
        candidate.reuse_fit = fit
        candidate.reuse_decision = decision
        candidate.license_class = license_class
        return candidate

    def test_nothing_found_is_zero(self):
        self.assertEqual(ex.reuse_leverage([]), 0.0)

    def test_excluded_candidates_do_not_count(self):
        candidate = self._c(90)
        candidate.filtered = True
        self.assertEqual(ex.reuse_leverage([candidate]), 0.0)

    def test_unlicensed_work_does_not_raise_leverage(self):
        self.assertEqual(ex.reuse_leverage([self._c(90, license_class=ex.UNKNOWN)]), 0.0)

    def test_one_excellent_foundation_scores_high(self):
        self.assertGreaterEqual(ex.reuse_leverage([self._c(95)]), 80)

    def test_leverage_never_saturates_at_100(self):
        # Breadth must never make a mediocre portfolio look perfect.
        many = [self._c(50) for _ in range(40)]
        self.assertLess(ex.reuse_leverage(many), 70)
        self.assertLessEqual(ex.reuse_leverage([self._c(100)]), 92)


class TestCoverageAndTTL(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = connect(self.root / "p.db")
        self.conn.execute(
            "INSERT INTO startups (slug, name, health) VALUES ('alpha', 'alpha', 'ACTIVE')"
        )
        self.conn.execute(
            "INSERT INTO startups (slug, name, health, owner_private)"
            " VALUES ('OWNER-PRIVATE', 'secret', 'ACTIVE', 1)"
        )

    def test_never_researched(self):
        self.assertEqual(ex.coverage_state(None), ex.NOT_RESEARCHED)

    def test_fresh_is_researched(self):
        from datetime import UTC, datetime

        state = ex.coverage_state({"searched_at": datetime.now(UTC).isoformat()})
        self.assertEqual(state, ex.RESEARCHED)

    def test_past_ttl_is_stale(self):
        from datetime import UTC, datetime, timedelta

        old = (datetime.now(UTC) - timedelta(days=ex.DEFAULT_TTL_DAYS + 1)).isoformat()
        self.assertEqual(ex.coverage_state({"searched_at": old}), ex.STALE)

    def test_owner_private_is_never_a_research_subject(self):
        row = self.conn.execute("SELECT * FROM startups WHERE slug = 'OWNER-PRIVATE'").fetchone()
        with self.assertRaises(ValueError):
            ex.research_startup(self.conn, row, self.root, dry_run=True)

    def test_coverage_excludes_owner_private(self):
        report = ex.coverage_report(self.conn)
        self.assertEqual(report["total"], 1)

    def test_coverage_percentage(self):
        report = ex.coverage_report(self.conn)
        self.assertEqual(report["external_research_current_pct"], 0.0)


class TestSurveyConfidence(unittest.TestCase):
    def _result(self, candidates, dropped, api=1, cached=0):
        return ex.LandscapeResult(
            slug="alpha", startup_id=1, plan=_plan(), candidates=candidates,
            dropped=dropped, leverage=0.0, queries_run=[], cache_hits=cached, api_calls=api,
        )

    def test_no_retrieval_is_not_a_low_reuse_result(self):
        result = self._result([], [], api=0, cached=0)
        self.assertEqual(result.survey_confidence(), ex.NOT_RETRIEVED)
        self.assertIn("INSUFFICIENT EVIDENCE", result.recommendation())

    def test_many_discarded_means_the_ecosystem_is_closed(self):
        dropped = [ex.Candidate(repo=f"a/{i}", name=str(i)) for i in range(40)]
        result = self._result([], dropped)
        self.assertEqual(result.survey_confidence(), "ECOSYSTEM_CLOSED")
        self.assertIn("CONTINUE CUSTOM", result.recommendation())

    def test_healthy_survey_is_good(self):
        candidates = [ex.Candidate(repo=f"a/{i}", name=str(i)) for i in range(5)]
        result = self._result(candidates, [])
        self.assertEqual(result.survey_confidence(), "GOOD")


class TestRecommendation(unittest.TestCase):
    def _result(self, leverage, candidates=None, **counts):
        if candidates is None:
            # A survey that actually looked: confidence must not short-circuit.
            candidates = [ex.Candidate(repo=f"a/{i}", name=str(i)) for i in range(5)]
        result = ex.LandscapeResult(
            slug="alpha", startup_id=1, plan=_plan(), candidates=candidates, dropped=[],
            leverage=leverage, queries_run=["q"], api_calls=1,
        )
        for key, value in counts.items():
            setattr(result, key, value)
        return result

    def test_high_leverage_changes_the_build_plan(self):
        self.assertIn("CHANGE THE BUILD PLAN", self._result(85.0).recommendation())

    def test_competition_is_not_a_reason_to_stop(self):
        recommendation = self._result(20.0).recommendation()
        self.assertNotIn("stop", recommendation.lower())

    def test_name_collision_asks_for_review_not_a_rename(self):
        candidate = ex.Candidate(repo="jsdhwfmax/EvalForge", owner="jsdhwfmax",
                                 name="EvalForge", stars=210)
        candidate.classification = ex.NAME_COLLISION
        recommendation = self._result(50.0, candidates=[candidate] + [
            ex.Candidate(repo=f"a/{i}", name=str(i)) for i in range(4)
        ]).recommendation()
        self.assertIn("BRAND_COLLISION_REVIEW", recommendation)
        self.assertIn("Do not rename automatically", recommendation)


class TestArtifact(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.conn = connect(self.root / "p.db")
        self.conn.execute(
            "INSERT INTO startups (slug, name, health) VALUES ('alpha', 'alpha', 'ACTIVE')"
        )
        candidate = ex.Candidate(
            repo="acme/evaltool", owner="acme", name="evaltool",
            description="llm evaluation judge for agents", stars=900, language="Python",
            license_spdx="mit", activity="2026-09-01T00:00:00Z", topics=["llm", "evaluation"],
        )
        fit, factors = ex.reuse_fit(candidate, _plan())
        candidate.reuse_fit, candidate.reuse_fit_factors = fit, factors
        candidate.license_class = ex.PERMISSIVE
        candidate.classification, candidate.could_replace = ex.classify_candidate(
            candidate, _plan(), fit
        )
        candidate.reuse_decision, candidate.risks, candidate.attribution = ex.decide_reuse(
            candidate, _plan(), fit
        )
        result = ex.LandscapeResult(
            slug="alpha", startup_id=1, plan=_plan(), candidates=[candidate], dropped=[],
            leverage=ex.reuse_leverage([candidate]), queries_run=["llm eval"], api_calls=1,
        )
        ex.persist(self.conn, result)
        self.conn.commit()
        self.landscape = ex.load_landscape(self.conn, "alpha")

    def test_artifact_has_every_required_section(self):
        text = ex.render_landscape(self.landscape)
        for heading in (
            "Exact-name results", "Direct competitors", "Open-source projects",
            "Reusable libraries", "Frameworks", "Reference", "Academic reference",
            "Abandoned but useful", "Package ecosystem", "High-fit reuse candidates",
            "License notes", "Security signals", "Recommendation",
        ):
            self.assertIn(heading, text, f"missing section: {heading}")

    def test_artifact_states_survey_confidence(self):
        self.assertIn("Survey confidence", ex.render_landscape(self.landscape))

    def test_artifact_explains_every_fit_number(self):
        self.assertIn("Fit detail", ex.render_landscape(self.landscape))

    def test_fit_factors_survive_the_round_trip(self):
        candidate = self.landscape["candidates"][0]
        self.assertIn("functional_overlap", candidate["reuse_fit_factors"])

    def test_artifact_is_written_to_disk(self):
        path = ex.write_artifact(self.conn, "alpha", self.root)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(path).name, ex.LANDSCOPE_ARTIFACT)

    def test_unknown_landscape_is_not_an_error(self):
        self.assertIsNone(ex.load_landscape(self.conn, "does-not-exist"))


class TestSecuritySignals(unittest.TestCase):
    def test_unmaintained_repo_is_high_supply_chain_risk(self):
        candidate = ex.Candidate(archived=True, activity="2020-01-01T00:00:00Z")
        self.assertEqual(ex.security_signals(candidate)["supply_chain_risk"], "high")

    def test_active_licensed_repo_is_low_risk(self):
        candidate = ex.Candidate(license_spdx="mit", activity="2026-09-01T00:00:00Z")
        self.assertEqual(ex.security_signals(candidate)["supply_chain_risk"], "low")

    def test_signals_admit_they_are_inferred(self):
        notes = " ".join(ex.security_signals(ex.Candidate())["notes"])
        self.assertIn("inferred", notes.lower())


class TestScout(unittest.TestCase):
    def test_scout_is_off_when_no_endpoint(self):
        scout = EcosystemScout(enabled=True)
        self.assertFalse(scout.available())

    def test_scout_skips_a_thin_shortlist(self):
        scout = EcosystemScout(enabled=True)
        self.assertEqual(scout.review(_plan(), []), {})

    def test_scout_never_raises(self):
        scout = EcosystemScout(enabled=False)
        self.assertEqual(scout.review(_plan(), [ex.Candidate(repo="a/b")]), {})

    def test_scout_parses_only_structured_output(self):
        self.assertEqual(EcosystemScout._parse("not json at all"), {})
        parsed = EcosystemScout._parse('{"repos": {"a/b": {"relevance": "high"}}}')
        self.assertIn("a/b", parsed)

    def test_scout_model_is_grok(self):
        self.assertEqual(EcosystemScout.model, "grok-4.7")


class TestIntegrationEngineer(unittest.TestCase):
    def test_protected_paths_are_flagged(self):
        engineer = IntegrationEngineer()
        guarded = engineer.guards(["src/payments/stripe.py", "src/reader.ts"])
        self.assertEqual(guarded, ["src/payments/stripe.py"])

    def test_plan_falls_back_deterministically(self):
        plan = IntegrationEngineer(enabled=False).plan(
            {"name": "evalscope", "repo": "modelscope/evalscope", "license_class": "PERMISSIVE",
             "language": "Python"},
            {"slug": "evalforge"},
        )
        self.assertEqual(plan["source"], "deterministic_fallback")
        self.assertTrue(plan["attribution"])

    def test_unresolved_license_blocks_integration_text(self):
        plan = IntegrationEngineer(enabled=False).plan(
            {"name": "x", "repo": "a/x", "license_class": "UNKNOWN"},
            {"slug": "evalforge"},
        )
        self.assertIn("do not integrate", plan["attribution"].lower())

    def test_license_obligation_is_preserved(self):
        plan = IntegrationEngineer(enabled=False).plan(
            {"name": "x", "repo": "a/x", "license_spdx": "Apache-2.0",
             "license_class": "PERMISSIVE"},
            {"slug": "evalforge"},
        )
        self.assertIn("Apache-2.0", plan["attribution"])


class TestCache(unittest.TestCase):
    def setUp(self):
        self.conn = connect(Path(tempfile.mkdtemp()) / "p.db")

    def test_round_trip(self):
        cache = ex.Cache(self.conn)
        cache.put("k", "github", [{"a": 1}])
        self.assertEqual(cache.get("k"), [{"a": 1}])

    def test_expired_entry_is_a_miss(self):
        cache = ex.Cache(self.conn)
        cache.put("k", "github", [{"a": 1}], ttl_days=-1)
        self.assertIsNone(cache.get("k"))

    def test_missing_key_is_a_miss(self):
        self.assertIsNone(ex.Cache(self.conn).get("nope"))


if __name__ == "__main__":
    unittest.main()