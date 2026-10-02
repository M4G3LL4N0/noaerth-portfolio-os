"""Ecosystem sources: provider contract, provenance, and honest thin results."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from portfolio_os import ecosystems as eco
from portfolio_os import external as ex


class FakeProvider:
    name = "fake"

    def __init__(self, items=None, available=True, boom=False):
        self.items = items or []
        self._available = available
        self.boom = boom
        self.calls = 0

    def available(self):
        return self._available

    def search(self, query, limit=12):
        self.calls += 1
        if self.boom:
            raise RuntimeError("provider outage")
        return list(self.items[:limit])

    def probe(self):
        return {"provider": self.name, "available": self._available}


class TestSurvey(unittest.TestCase):
    def setUp(self):
        eco.PROVIDERS.clear()
        self.addCleanup(eco.PROVIDERS.clear)

    def test_provenance_records_every_source_consulted(self):
        """A thin result must never look like an exhausted ecosystem."""
        eco.PROVIDERS.update({
            "github": FakeProvider([eco.EcosystemItem("github", "a/b")]),
            "npm": FakeProvider([eco.EcosystemItem("npm", "thing")]),
            "web": FakeProvider([]),
        })
        _, provenance = eco.survey(["query one"])
        self.assertEqual(len(provenance), 3)
        self.assertEqual({p["provider"] for p in provenance},
                         {"github", "npm", "web"})

    def test_unavailable_provider_is_recorded_not_silently_skipped(self):
        eco.PROVIDERS.update({"npm": FakeProvider(available=False)})
        items, provenance = eco.survey(["q"])
        self.assertEqual(items, [])
        self.assertEqual(provenance[0]["status"], "unavailable")

    def test_one_broken_provider_does_not_break_the_sweep(self):
        eco.PROVIDERS.update({
            "npm": FakeProvider(boom=True),
            "pypi": FakeProvider([eco.EcosystemItem("pypi", "pkg")]),
        })
        items, provenance = eco.survey(["q"])
        self.assertEqual(len(items), 1)
        status = {p["provider"]: p["status"] for p in provenance}
        self.assertTrue(status["npm"].startswith("error"))
        self.assertEqual(status["pypi"], "ok")

    def test_identical_identifiers_from_different_sources_do_not_collapse(self):
        eco.PROVIDERS.update({
            "npm": FakeProvider([eco.EcosystemItem("npm", "svelte")]),
            "crates": FakeProvider([eco.EcosystemItem("crates", "svelte")]),
        })
        items, _ = eco.survey(["ui framework"])
        self.assertEqual({i.source for i in items}, {"npm", "crates"})

    def test_items_without_identifier_are_dropped(self):
        eco.PROVIDERS.update({
            "npm": FakeProvider([eco.EcosystemItem("npm", "")]),
        })
        items, provenance = eco.survey(["q"])
        self.assertEqual(items, [])
        self.assertEqual(provenance[0]["results"], 1)  # provider said it, we dropped it


class TestWebProvider(unittest.TestCase):
    def test_empty_dataset_reports_its_own_limitation(self):
        probe = eco.WebDiscoveryProvider().probe()
        self.assertEqual(probe["dataset_size"], 0)
        self.assertIn("not surveyed", probe["note"])

    def test_dataset_matches_on_domain_terms(self):
        provider = eco.WebDiscoveryProvider([
            {"name": "Coupa", "description": "procurement software", "url": "https://x"},
            {"name": "Unrelated", "description": "a text editor"},
        ])
        found = provider.search("procurement platform")
        self.assertEqual([f.name for f in found], ["Coupa"])


class TestRegistryRelevanceGate(unittest.TestCase):
    """Registries match on names, so name matches alone must not count."""

    def _plan(self, one_liner="enterprise procurement software"):
        plan = ex.build_query_plan("alpha", "Alpha", None, category="ops")
        plan.one_liner = one_liner
        plan.primary_workflow = "procure"
        return plan

    def test_domain_speaking_registry_hit_is_admitted(self):
        candidate = ex.Candidate(repo="npm:procurex", name="procurex",
                                 description="AI procurement workflow for buyers")
        self.assertTrue(ex._registry_hit_is_topical(candidate, self._plan()))

    def test_name_only_registry_noise_is_rejected(self):
        candidate = ex.Candidate(repo="npm:zrender", name="zrender",
                                 description="a graphics library for SVG rendering")
        self.assertFalse(ex._registry_hit_is_topical(candidate, self._plan()))

    def test_confidence_is_not_upgraded_by_breadth_alone(self):
        result = ex.LandscapeResult(
            slug="alpha", startup_id=1, plan=self._plan(),
            candidates=[ex.Candidate(repo=f"a/{i}", name=str(i)) for i in range(6)],
            dropped=[], leverage=0.0, queries_run=[], api_calls=1,
            sources_consulted=["github", "npm", "pypi", "crates"],
        )
        self.assertEqual(result.survey_confidence(), ex.THIN)


class TestNetworkSwitch(unittest.TestCase):
    def test_live_network_is_off_unless_explicitly_enabled(self):
        from portfolio_os import env
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(env.live_network())
        with mock.patch.dict(os.environ, {"PORTFOLIO_OS_LIVE_NETWORK": "1"}):
            self.assertTrue(env.live_network())

    def test_ambient_agent_is_not_consent(self):
        from portfolio_os import env
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(env.scout_endpoint(), "")

    def test_registry_providers_refuse_to_call_out_without_the_switch(self):
        for provider in (eco.NpmProvider(), eco.PyPiProvider(), eco.CratesProvider()):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertFalse(provider.available())


if __name__ == "__main__":
    unittest.main()
