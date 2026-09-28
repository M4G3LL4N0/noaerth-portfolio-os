"""Classify workspace paths. Generated junk is not product work."""

from __future__ import annotations

from pathlib import Path

ARTIFACT_PARTS = {"node_modules", ".next", "dist", "build", ".git", "coverage"}
GENERATED_NAMES = {
    "CLAIM_REGISTER.md",
    "DECISION_RECORD.md",
    "FAILURE_REGISTER.md",
    "LAUNCH_READINESS.md",
    "LOCAL_REVIEW.md",
    "NOAERTH_UPGRADE_REPORT.md",
    "PREMIUM_UI_UX_REPORT.md",
    "PROOF_LOOP.md",
    "AUTOBUILDER_FOUNDATION.json",
}
STUB_MARK = "This page is live so navigation and portfolio links do not 404."
PRODUCT_STATES = (
    "CONCEPT",
    "STATIC_DEMO",
    "INTERACTIVE_DEMO",
    "PARTIAL_WORKFLOW",
    "WORKING_PRIMARY_WORKFLOW",
    "EARLY_PRODUCT",
    "MATURE_PRODUCT",
)


def classify_workspace_path(relative: str, text: str = "") -> str:
    path = Path(relative)
    if set(path.parts) & ARTIFACT_PARTS or path.suffix == ".node":
        return "DEPENDENCY_ARTIFACT"
    if ".autobuilder" in path.parts or path.name in GENERATED_NAMES:
        return "GENERATED"
    if STUB_MARK in text:
        return "GENERATED"
    return "UNKNOWN"


def allow_product_state(current: str, proposed: str, evidence: bool) -> str:
    if current not in PRODUCT_STATES or proposed not in PRODUCT_STATES:
        return current if current in PRODUCT_STATES else "CONCEPT"
    if proposed == "WORKING_PRIMARY_WORKFLOW" and not evidence:
        return current
    return proposed
