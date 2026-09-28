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
    "reviewer": "Fail unsupported claims, broken workflows, and self-approval. A pass requires evidence.",
    "release": "Commit scoped files, push, and queue one deploy of the latest approved commit.",
}


def assign_shards(slugs: list[str], size: int = SHARD_SIZE) -> dict[str, str]:
    ordered = sorted(slug for slug in slugs if slug and slug != "OWNER-PRIVATE")
    return {slug: f"SHARD-{(index // size) + 1:02d}" for index, slug in enumerate(ordered)}


def squad_for_state(product_state: str) -> list[str]:
    if product_state in {"CONCEPT", "STATIC_DEMO"}:
        return ["founder", "engineer", "designer"]
    if product_state in {"INTERACTIVE_DEMO", "PARTIAL_WORKFLOW"}:
        return ["engineer", "designer", "reviewer"]
    return ["engineer", "reviewer"]
