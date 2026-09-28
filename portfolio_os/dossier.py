"""Startup dossiers. Facts come from the repo. Narratives are explicit or UNKNOWN."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from portfolio_os.engine import utcnow
from portfolio_os.exclusion import ExclusionError, assert_allowed
from portfolio_os.studio import compiled_homepage

NARRATIVES = {
    "access-layer": {
        "what": "AccessXWorld is a coordination layer for physical access: policy, a signed pass, an edge check, and an audit trail.",
        "who": "Operators of venues, parking, property, campuses, and infrastructure who need access without shared gate codes.",
        "problem": "Physical access still depends on copied codes and improvised staff decisions.",
        "exists": "A public pilot brief, a policy-decision illustration, and a four-step access flow. Not a claim that live gates already run on it.",
        "distinctive": "The product story is the signed pass, not a generic access-control brochure.",
        "maturity": "Early Access",
        "workflow": "Define a policy, issue a signed pass, check it at the edge, keep the proof.",
        "capabilities": "Public homepage, how-it-works, use cases, and the access-flow section.",
        "incomplete": "Many secondary routes exist, including customers and metrics pages, that can overclaim a finished platform.",
        "internal": "Do not publish operator credentials, gate hardware details, or private deployment config.",
        "opportunity": "Audit secondary pages so they match the pilot-brief honesty of the homepage.",
        "now": ["Review secondary pages that imply a finished production platform."],
        "later": ["Do not restore the old 147k homepage. It claimed a production platform the pilot does not have."],
        "public_safe": "Signed access for doors, gates, and lots. Pilot brief. Four-step decision flow.",
        "forbidden": "Live gate counts. Shared lock codes. A claim that every gate already runs on AXW.",
    },
    "autoerp": {
        "what": "AutoERP is a browser prototype that maps where finance, HR, procurement, and inventory stall.",
        "who": "Operators sketching an ERP rollout before buying a large system.",
        "problem": "ERP sprawl multiplies a bottleneck that was never mapped.",
        "exists": "A homepage with six planner inputs and an operating-map illustration. A responsive nav fix exists on branch portfolio/autoerp/228.",
        "distinctive": "It shows the planner inputs instead of a generic ERP feature grid.",
        "maturity": "Prototype",
        "workflow": "Describe the company shape and read an operating map. It is not a live ERP replacement.",
        "capabilities": "Homepage, planner, dashboard, and pricing routes in the app tree.",
        "incomplete": "The nav fix is not on main because the main index has unrelated dirty files.",
        "internal": "Do not commit the dirty autobuilder index. Do not publish internal financial figures.",
        "opportunity": "Merge portfolio/autoerp/228 when it will not overwrite unrelated work.",
        "now": ["Keep MERGE_REVIEW_REQUIRED until the branch can land without the dirty index."],
        "later": ["Show a worked planner result only after the merge is safe."],
        "public_safe": "Prototype command-center planner. Not a live ERP. Not audited finance results.",
        "forbidden": "Savings figures presented as audited results. A claim of production ERP replacement.",
    },
    "behindcurtain": {
        "what": "BehindCurtain is a public directory for profiles, sources, and claims, with an honest limit on what the site can verify.",
        "who": "Readers who want a source-aware profile directory rather than an unsourced feed.",
        "problem": "Public claims are hard to separate from allegations, and search on the homepage is not live.",
        "exists": "A profile directory, a disabled search field, and a verification method that is descriptive.",
        "distinctive": "The site now says the checks are a method, not a live count of sources.",
        "maturity": "Prototype",
        "workflow": "Browse featured profiles and sources. Search is not live. Verification is explained, not executed as an engine.",
        "capabilities": "Homepage directory, explorer route, and a Supabase client dependency.",
        "incomplete": "Both app/page.tsx and src/app/page.tsx exist. The edited public page is src/app/page.tsx.",
        "internal": "Do not publish private source material, admin tools, or unverified personal data.",
        "opportunity": "Make one homepage canonical so Next is not serving a second, older page.",
        "now": ["Decide which homepage tree Next should serve and retire the other."],
        "later": ["A live search only after the directory can support it."],
        "public_safe": "A directory and a described verification method. Search is not live.",
        "forbidden": "A claim that every fact has three sources or that every statement is source-linked.",
    },
    "bioyield-labs": {
        "what": "Bioyield Labs is a public research page about crop-protection questions and the boundary of what is not published.",
        "who": "Readers who need the research direction without a procedure.",
        "problem": "A biology concept site can slide into operational instructions. This one is required not to.",
        "exists": "A research page, a field illustration, public-versus-closed cards, and an explicit withholding of protocols.",
        "distinctive": "The page says what is absent as clearly as what is present.",
        "maturity": "Research",
        "workflow": "Read the questions and the boundary. There is no application step.",
        "capabilities": "Public research homepage. Mailto uses an example address and is not a real inbox.",
        "incomplete": "The example address must not be treated as a working contact.",
        "internal": "Methods, formulations, strain choices, synthesis, and application steps stay off the public site.",
        "opportunity": "Keep the research presentation high-level. Do not add procedures.",
        "now": ["Leave the safe boundary in place."],
        "later": ["Design identity work that does not add operational biology."],
        "public_safe": "Research direction, questions, and limits.",
        "forbidden": "Protocols, formulations, strain selection, synthesis, dosing, or application steps.",
    },
    "blitzproof": {
        "what": "Blitzproof is a proof engine for a founder running many bets and scaling only the ones that show evidence.",
        "who": "A single founder comparing ideas by clicks, leads, and recorded payments.",
        "problem": "Most startups are built before they are proven.",
        "exists": "A homepage with five proof gates and a local demo scoreboard. Sample figures are labeled as samples.",
        "distinctive": "The scoreboard is the product, and the page says the numbers are samples.",
        "maturity": "Prototype",
        "workflow": "Score a local demo idea by attention, interest, conversion, delivery, and revenue proof.",
        "capabilities": "Homepage, dashboard, and idea routes under /i.",
        "incomplete": "The dashboard is a local demo, not a live portfolio of customers.",
        "internal": "Do not publish private customer metrics or treat sample numbers as results.",
        "opportunity": "Keep sample labels on any new scoreboard figure.",
        "now": ["Confirm the dashboard still labels sample metrics before any public claim of results."],
        "later": ["A second proof illustration only if it shows the scoring workflow."],
        "public_safe": "Proof gates and a sample scoreboard. Not audited revenue.",
        "forbidden": "Sample clicks, leads, or revenue presented as real customer results.",
    },
}


def _routes(root: Path) -> list[str]:
    found = []
    for path in root.glob("app/**/page.tsx"):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        found.append(str(path.relative_to(root)))
    for path in root.glob("src/app/**/page.tsx"):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        found.append(str(path.relative_to(root)))
    return sorted(found)[:40]


def _scripts(root: Path) -> dict:
    package = root / "package.json"
    if not package.is_file():
        return {}
    try:
        data = json.loads(package.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    scripts = data.get("scripts") or {}
    deps = sorted((data.get("dependencies") or {}).keys())
    return {
        "scripts": {key: scripts[key] for key in ("dev", "build", "start", "lint", "typecheck") if key in scripts},
        "dependencies": deps[:12],
    }


def extract_facts(root: Path, portfolio_root: Path) -> dict:
    assert_allowed(root, portfolio_root)
    page = compiled_homepage(root)
    heading = ""
    if page and page.is_file():
        import re

        text = page.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
        if match:
            heading = re.sub(r"<[^>]+>", " ", match.group(1))
            heading = re.sub(r"\s+", " ", heading).strip()
    return {
        "homepage": str(page.relative_to(root)) if page else "",
        "heading": heading,
        "routes": _routes(root),
        "tooling": _scripts(root),
    }


def render_dossier(slug: str, facts: dict, narrative: dict | None) -> dict[str, str]:
    story = narrative or {}
    unknown = "UNKNOWN. Needs a venture review. Do not invent this."
    what = story.get("what") or (f"Homepage heading: {facts['heading']}" if facts.get("heading") else unknown)
    understanding = "\n".join(
        [
            f"# {slug}",
            "",
            "## What is this?",
            what,
            "",
            "## Who is it for?",
            story.get("who", unknown),
            "",
            "## Problem",
            story.get("problem", unknown),
            "",
            "## What exists",
            story.get("exists", unknown),
            "",
            "## Distinctive",
            story.get("distinctive", unknown),
            "",
            "## Maturity",
            story.get("maturity", "Unlabeled"),
            "",
            "## Primary workflow",
            story.get("workflow", unknown),
            "",
            "## Incomplete",
            story.get("incomplete", unknown),
            "",
            "## Opportunity",
            story.get("opportunity", unknown),
            "",
            f"Served homepage file: `{facts.get('homepage') or 'none'}`.",
            f"Heading: {facts.get('heading') or 'none'}.",
        ]
    )
    route_lines = "\n".join(f"- `{route}`" for route in facts.get("routes") or []) or "- none found"
    scripts = facts.get("tooling", {}).get("scripts") or {}
    script_lines = "\n".join(f"- `{key}`: `{value}`" for key, value in scripts.items()) or "- no package scripts"
    deps = ", ".join(facts.get("tooling", {}).get("dependencies") or []) or "none recorded"
    build_map = "\n".join(
        [
            f"# Build map: {slug}",
            "",
            "Derived from the repository. Not a guessed architecture.",
            "",
            "## Homepage",
            f"`{facts.get('homepage') or 'none'}`",
            "",
            "## Routes",
            route_lines,
            "",
            "## Commands",
            script_lines,
            "",
            "## Direct dependencies",
            deps,
            "",
            "```mermaid",
            "flowchart LR",
            "  Visitor --> Homepage",
            "  Homepage --> PublicSurface",
            "```",
        ]
    )
    public_surface = "\n".join(
        [
            f"# Public surface: {slug}",
            "",
            "## Safe to say",
            story.get("public_safe", unknown),
            "",
            "## Forbidden",
            story.get("forbidden", "Secrets, local paths, and owner-private material."),
            "",
            "## Internal",
            story.get("internal", unknown),
        ]
    )
    progress = "\n".join(
        [
            f"# Progress: {slug}",
            "",
            f"Updated {utcnow()}.",
            "",
            story.get("exists", "No narrative recorded yet."),
        ]
    )
    now = story.get("now") or ["Record a venture review before adding work."]
    roadmap = "\n".join(
        [
            f"# Roadmap: {slug}",
            "",
            "## Now",
            *[f"- {item}" for item in now],
            "",
            "## Later",
            *[f"- {item}" for item in story.get("later") or ["None recorded."]],
            "",
            "## Not planned",
            "- Giant speculative features.",
        ]
    )
    health = "\n".join(
        [
            f"# Health: {slug}",
            "",
            f"- PRODUCT: {'ACTIVE' if story else 'REVIEW REQUIRED'}",
            f"- PUBLIC ALIGNMENT: {'NEEDS WORK' if story.get('incomplete') else 'REVIEW REQUIRED'}",
            "- WEBSITE: see the served homepage heading above.",
            "- RELEASE: not deployed from this dossier.",
        ]
    )
    documents = {
        "UNDERSTANDING.md": understanding,
        "BUILD_MAP.md": build_map,
        "PUBLIC_SURFACE.md": public_surface,
        "PROGRESS.md": progress,
        "ROADMAP.md": roadmap,
        "HEALTH.md": health,
    }
    blob = "\n".join(documents.values())
    if "/Users/" in blob or "openlegal" in blob.lower():
        raise ExclusionError("dossier contained a forbidden token")
    return documents


def write_dossier(root: Path, portfolio_root: Path, slug: str, dest: Path, conn: sqlite3.Connection, startup_id: int) -> Path:
    facts = extract_facts(root, portfolio_root)
    documents = render_dossier(slug, facts, NARRATIVES.get(slug))
    out = dest / slug
    out.mkdir(parents=True, exist_ok=True)
    for name, body in documents.items():
        (out / name).write_text(body + "\n", encoding="utf-8")
    conn.execute(
        """
        INSERT INTO dossiers (startup_id, slug, facts, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(startup_id) DO UPDATE SET facts = excluded.facts, updated_at = excluded.updated_at, slug = excluded.slug
        """,
        (startup_id, slug, json.dumps({"homepage": facts["homepage"], "heading": facts["heading"]}), utcnow()),
    )
    return out
