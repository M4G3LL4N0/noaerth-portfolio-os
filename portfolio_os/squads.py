"""Startup squads, coverage shards, and the reasoning model id.

Grok 4.7 is the reasoning model. Git, builds, tests, and the database stay deterministic local tools.
"""

from __future__ import annotations

REASONING_MODEL = "grok-4.7"
SHARD_SIZE = 10

ROLE_PROMPTS = {
    "founder": "Decide the user, the problem, and the next bounded product transition. Reject feature creep.",
    "engineer": "Inspect committed code and local product work. Implement the primary workflow. Do not shrink the product to a thin commit when recoverable code exists.",
    "designer": "Make the primary workflow understandable. Keep the public site specific and accurate.",
    "reviewer": "Fail both overclaiming and underselling. A pass requires a credible public story and evidence.",
    "product_marketing": "Write the public story: who it is for, why it matters, and what to do next. Do not publish repository status.",
    "creative_director": "Give the startup a specific visual point of view. Do not reuse a generic startup template.",
    "release": "Commit scoped files, push, and queue one deploy of the latest approved commit.",
}

PUBLIC_STORY_RESULTS = ("PASS", "TOO_DEFENSIVE", "TOO_VAGUE", "TOO_GENERIC", "OVERCLAIMING")
CAVEAT_MARKERS = (
    "does not",
    "there is no",
    "not a live",
    "not saved",
    "not implemented",
    "no store",
    "no customers",
    "sample only",
    "not production",
    "not committed",
)


def assess_public_story(text: str) -> str:
    """Flag public copy that reads like an audit. Safety lines can remain; a page full of them cannot."""
    lowered = " ".join(text.lower().split())
    hits = sum(lowered.count(marker) for marker in CAVEAT_MARKERS)
    words = max(1, len(lowered.split()))
    if hits >= 3 or (hits >= 2 and hits / words > 0.03):
        return "TOO_DEFENSIVE"
    return "PASS"


def squad_for_public_site() -> list[str]:
    return ["founder", "product_marketing", "creative_director", "designer", "reviewer"]


def assign_shards(slugs: list[str], size: int = SHARD_SIZE) -> dict[str, str]:
    ordered = sorted(slug for slug in slugs if slug and slug != "OWNER-PRIVATE")
    return {slug: f"SHARD-{(index // size) + 1:02d}" for index, slug in enumerate(ordered)}


def squad_for_state(product_state: str) -> list[str]:
    if product_state in {"CONCEPT", "STATIC_DEMO"}:
        return ["founder", "engineer", "designer"]
    if product_state in {"INTERACTIVE_DEMO", "PARTIAL_WORKFLOW"}:
        return ["engineer", "designer", "reviewer"]
    return ["engineer", "reviewer"]
