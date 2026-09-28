"""Startup dossiers. Facts come from the repo. Narratives are explicit or UNKNOWN."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import subprocess

from portfolio_os.engine import utcnow
from portfolio_os.exclusion import ExclusionError, assert_allowed
from portfolio_os.studio import compiled_homepage

SCHEMA_VERSION = 2

NARRATIVES = {
    "access-layer": {
        "what": "AccessXWorld is a coordination layer for physical access: policy, a signed pass, an edge check, and an audit trail.",
        "who": "Operators of venues, parking, property, campuses, and infrastructure who need access without shared gate codes.",
        "problem": "Physical access still depends on copied codes and improvised staff decisions.",
        "exists": "The published homepage is app/page.tsx. It shows four steps and an illustration of what a pass would carry. The page does not sign or check a credential. src/app is excluded by tsconfig and is not the served tree.",
        "execution": "FIX_NOW",
        "distinctive": "The product story is the signed pass, not a generic access-control brochure.",
        "maturity": "Early Access",
        "workflow": "Define a policy, issue a signed pass, check it at the edge, keep the proof.",
        "capabilities": "Public homepage, how-it-works, use cases, and the access-flow section.",
        "incomplete": "Many secondary routes exist, including customers and metrics pages, that can overclaim a finished platform.",
        "internal": "Do not publish operator credentials, gate hardware details, or private deployment config.",
        "opportunity": "Audit secondary pages so they match the pilot-brief honesty of the homepage.",
        "now": ["Do not publish the untracked stub routes that only exist to avoid a 404."],
        "later": ["Do not restore the old 147k homepage. It claimed a production platform the pilot does not have."],
        "public_safe": "Signed access for doors, gates, and lots. Pilot brief. Four-step decision flow.",
        "forbidden": "Live gate counts. Shared lock codes. A claim that every gate already runs on AXW.",
    },
    "autoerp": {
        "what": "AutoERP is a browser prototype that maps where finance, HR, procurement, and inventory stall.",
        "who": "Operators sketching an ERP rollout before buying a large system.",
        "problem": "ERP sprawl multiplies a bottleneck that was never mapped.",
        "exists": "The published repository is the homepage. It shows six planner inputs and a labeled sample map. Planner, dashboard, and pricing routes exist only in an uncommitted index, so the page no longer links to them.",
        "execution": "FIX_NOW",
        "why_no_change": "portfolio/autoerp/228 adds components/SiteNav.tsx, which the published homepage does not import. Merging that file alone does not change the page. The rest of the app is an uncommitted index and was not committed.",
        "distinctive": "It shows the planner inputs instead of a generic ERP feature grid.",
        "maturity": "Prototype",
        "workflow": "Describe the company shape and read an operating map. It is not a live ERP replacement.",
        "capabilities": "Homepage, planner, dashboard, and pricing routes in the app tree.",
        "incomplete": "The nav fix is not on main because the main index has unrelated dirty files.",
        "internal": "Do not commit the dirty autobuilder index. Do not publish internal financial figures.",
        "opportunity": "Merge portfolio/autoerp/228 when it will not overwrite unrelated work.",
        "now": ["Leave portfolio/autoerp/228 unmerged until the published homepage imports SiteNav."],
        "later": ["Show a worked planner result only after the merge is safe."],
        "public_safe": "Prototype command-center planner. Not a live ERP. Not audited finance results.",
        "forbidden": "Savings figures presented as audited results. A claim of production ERP replacement.",
    },
    "behindcurtain": {
        "what": "BehindCurtain is a public directory for profiles, sources, and claims, with an honest limit on what the site can verify.",
        "who": "Readers who want a source-aware profile directory rather than an unsourced feed.",
        "problem": "Public claims are hard to separate from allegations, and search on the homepage is not live.",
        "exists": "Published homepage is src/app/page.tsx. app/page.tsx on disk re-exports it and is not in git. Search on the homepage is off. The explorer filters a seeded sample directory.",
        "distinctive": "The site says the checks are a method, and the homepage no longer calls the sample directory verified.",
        "execution": "FIX_NOW",
        "maturity": "Prototype",
        "workflow": "Browse featured profiles and sources. Search is not live. Verification is explained, not executed as an engine.",
        "capabilities": "Homepage directory, explorer route, and a Supabase client dependency.",
        "incomplete": "Both app/page.tsx and src/app/page.tsx exist. The edited public page is src/app/page.tsx.",
        "internal": "Do not publish private source material, admin tools, or unverified personal data.",
        "opportunity": "Make one homepage canonical so Next is not serving a second, older page.",
        "now": ["Do not commit the untracked app/ tree unless every published src route is re-exported."],
        "later": ["A live search only after the directory can support it."],
        "public_safe": "A directory and a described verification method. Search is not live.",
        "forbidden": "A claim that every fact has three sources or that every statement is source-linked.",
    },
    "bioyield-labs": {
        "what": "Bioyield Labs is a public research page about crop-protection questions and the boundary of what is not published.",
        "who": "Readers who need the research direction without a procedure.",
        "problem": "A biology concept site can slide into operational instructions. This one is required not to.",
        "exists": "A research page with a reading order: public boundary, questions, posture, research desk. No procedure is published.",
        "execution": "DESIGN_NOW",
        "distinctive": "The page says what is absent as clearly as what is present.",
        "maturity": "Research",
        "workflow": "Read the questions and the boundary. There is no application step.",
        "capabilities": "Public research homepage. Mailto uses an example address and is not a real inbox.",
        "incomplete": "The example address must not be treated as a working contact.",
        "internal": "Methods, formulations, strain choices, synthesis, and application steps stay off the public site.",
        "opportunity": "Keep the research presentation high-level. Do not add procedures.",
        "now": ["Keep the reading order. Do not add a method, formulation, or application step."],
        "later": ["Design identity work that does not add operational biology."],
        "public_safe": "Research direction, questions, and limits.",
        "forbidden": "Protocols, formulations, strain selection, synthesis, dosing, or application steps.",
    },
    "blitzproof": {
        "what": "Blitzproof is a proof engine for a founder running many bets and scaling only the ones that show evidence.",
        "who": "A single founder comparing ideas by clicks, leads, and recorded payments.",
        "problem": "Most startups are built before they are proven.",
        "exists": "The published repository is the homepage. Five proof gates and labeled sample ideas are on that page. Dashboard and funnel routes are not in git, so the page does not link to them.",
        "execution": "FIX_NOW",
        "distinctive": "The scoreboard is the product, and the page says the numbers are samples.",
        "maturity": "Prototype",
        "workflow": "Score a local demo idea by attention, interest, conversion, delivery, and revenue proof.",
        "capabilities": "Homepage, dashboard, and idea routes under /i.",
        "incomplete": "The published tree does not contain the dashboard or funnel routes that the old homepage linked.",
        "internal": "Do not publish private customer metrics or treat sample numbers as results.",
        "opportunity": "Keep sample labels on any new scoreboard figure.",
        "now": ["Do not link the published page at the uncommitted dashboard."],
        "later": ["A second proof illustration only if it shows the scoring workflow."],
        "public_safe": "Proof gates and a sample scoreboard. Not audited revenue.",
        "forbidden": "Sample clicks, leads, or revenue presented as real customer results.",
    },
    "noaerth": {
        "what": "Noaerth is the public studio site: what the studio builds, which work is featured, and how to reach the full portfolio and Labs.",
        "who": "Someone deciding where to start in the portfolio, not an operator running the queue.",
        "problem": "A wall of every startup hides what is mature, experimental, or worth opening first.",
        "exists": "A homepage with an intent router, a Labs teaser, featured work, and a filterable venture wall. A studio section now separates featured work, the full directory, and Labs.",
        "distinctive": "It is the public home of the studio. It is not the control plane and it does not list internal reviews.",
        "maturity": "Product",
        "workflow": "Read the studio framing, open a featured product, or go to the portfolio directory. Labs is the public progress feed.",
        "capabilities": "Homepage, portfolio search and stage filters, Labs preview, fail-closed team route.",
        "incomplete": "Production is behind the git main that contains the Labs teaser and the studio section.",
        "internal": "Do not publish Team controls, agent notes, or dossier text.",
        "opportunity": "Keep homepage prominence editorial. Do not rank startups by folder order.",
        "execution": "DESIGN_NOW",
        "now": ["Keep the studio section. Do not dump every startup into the hero."],
        "later": ["Project pages that summarize a startup without copying its whole website."],
        "public_safe": "Studio identity, featured products, portfolio discovery, and a Labs preview.",
        "forbidden": "Internal health scores, agent logs, and owner-private work.",
    },
    "noaerth-labs": {
        "what": "Noaerth Labs is the public build-in-public feed. It reads a sanitized snapshot. It does not control Portfolio OS.",
        "who": "A reader who wants selected public progress, not the operating queue.",
        "problem": "Shipping notes and internal review noise get mixed together if the feed is not sanitized.",
        "exists": "A project map, built-this-week, activity, and a public method: build, review, test, ship. One correction note uses BehindCurtain’s removed source-count claim.",
        "distinctive": "Labs explains the public process and shows a before/change/now note. It does not expose agent mechanics.",
        "maturity": "Product",
        "workflow": "Open the map or the week. A project page shows public notes only.",
        "capabilities": "Home, activity, week, and project routes fed by the public snapshot.",
        "incomplete": "labs.noaerth.com is not the live host yet. The main site still has a preview.",
        "internal": "Do not publish review failures, locks, or local paths.",
        "opportunity": "Add another before/change/now note only when a public correction is real.",
        "execution": "DESIGN_NOW",
        "now": ["Keep the method section and the BehindCurtain correction factual."],
        "later": ["A weekly digest that a person would reread."],
        "public_safe": "Selected public milestones and the four-step public method.",
        "forbidden": "Internal agent mechanics, private metrics, and owner-private material.",
    },
    "noaerth-team": {
        "what": "Noaerth Team is the private operating room. It reads the team snapshot and sends allowlisted actions to Portfolio OS.",
        "who": "The person operating the portfolio.",
        "problem": "The studio cannot be run from 130 terminals, and production cannot pretend a localhost action succeeded.",
        "exists": "Command center, startup pages, queue, and a system page that shows the control-plane commit against the daemon commit. A startup page can show the stored dossier.",
        "distinctive": "It is internal. Writes go through the Portfolio OS action API. There is no arbitrary shell.",
        "maturity": "Early Access",
        "workflow": "Sign in, read a startup dossier, and send an allowlisted action when the write URL is configured.",
        "capabilities": "Auth-gated pages. Actions return an error when the write URL is unset.",
        "incomplete": "Production Team cannot call a laptop localhost. Until a remote control plane exists, production stays read-only or unavailable.",
        "internal": "Session secrets, evidence paths, and dossier text stay off the public web.",
        "opportunity": "Show UPDATE REQUIRED when the daemon commit drifts from the control plane.",
        "execution": "BUILD_NOW",
        "now": ["Show dossier fields and daemon commit drift on the operating pages."],
        "later": ["A remote Team-to-Portfolio-OS connection that does not expose the local machine."],
        "public_safe": "Nothing. Team is not a public product.",
        "forbidden": "Public links, secrets, and any claim that a disabled action succeeded.",
    },
    "portfolio-control": {
        "what": "Portfolio OS is the private control plane: discovery, dossiers, queue, reviews, snapshots, and the release budget.",
        "who": "The daemon and the Team app. Not the public.",
        "problem": "Website review was being treated as the whole startup, and the running daemon could lag the control-plane commit.",
        "exists": "SQLite state, a dossier generator with a canonical entrypoint and a build plan, a public sanitizer, and a daemon heartbeat that records its commit.",
        "distinctive": "It decides and records work. Noaerth, Labs, and Team are products that read what it publishes.",
        "maturity": "Product",
        "workflow": "Classify the entrypoint, write a build plan, make the bounded change, then publish a sanitized snapshot.",
        "capabilities": "CLI, daemon, HTTP actions, public and team snapshots.",
        "incomplete": "The daemon must be restarted onto the commit that contains this behavior or Team will show UPDATE REQUIRED.",
        "internal": "Database, evidence, locks, and dossiers stay private.",
        "opportunity": "Restart the daemon onto the current commit after the current lock is free.",
        "execution": "BUILD_NOW",
        "now": ["Heartbeat includes the control-plane commit. Team compares it."],
        "later": ["A remote API for Team that is not the developer laptop."],
        "public_safe": "Only the sanitized public snapshot fields.",
        "forbidden": "Dossier text, local paths, secrets, and owner-private rows in the public feed.",
    },
}


def _git_tracks(root: Path, relative: str) -> bool | None:
    try:
        subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        err = ((exc.stderr or "") + (exc.stdout or "")).lower()
        if exc.returncode == 128 or "not a git repository" in err:
            return None
        return False
    except (OSError, subprocess.SubprocessError):
        return None


def _ts_excludes(root: Path) -> set[str]:
    path = root / "tsconfig.json"
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return set()
    return {str(item).rstrip("/") for item in data.get("exclude") or []}


def classify_frontend(root: Path) -> dict:
    """Next serves ./app before ./src/app. Excluded or unreexported trees are not the product."""
    app_page = root / "app" / "page.tsx"
    src_page = root / "src" / "app" / "page.tsx"
    excluded = _ts_excludes(root)
    app_tracked = _git_tracks(root, "app/page.tsx") if app_page.is_file() else False
    src_tracked = _git_tracks(root, "src/app/page.tsx") if src_page.is_file() else False
    reexports = ""
    if app_page.is_file():
        text = app_page.read_text(encoding="utf-8", errors="replace")
        if "src/app/page" in text:
            reexports = "app/page.tsx re-exports src/app/page.tsx"
    imported = ""
    if (root / "app").is_dir():
        chunks = []
        for page in (root / "app").rglob("page.tsx"):
            if "node_modules" in page.parts:
                continue
            chunks.append(page.read_text(encoding="utf-8", errors="replace"))
        imported = "\n".join(chunks)
    dead: list[str] = []
    src_root = root / "src" / "app"
    if src_root.is_dir() and (root / "app").is_dir():
        for page in sorted(src_root.rglob("page.tsx")):
            if "node_modules" in page.parts:
                continue
            relative = page.relative_to(root).as_posix()
            marker = relative[: -len(".tsx")] if relative.endswith(".tsx") else relative
            if "src/app" in excluded or marker not in imported:
                dead.append(relative)
    canonical = ""
    note = ""
    if app_page.is_file() and app_tracked is not False:
        canonical = "app/page.tsx"
        note = "Next prefers ./app over ./src/app."
    elif src_page.is_file() and (app_tracked is False or not app_page.is_file()):
        canonical = "src/app/page.tsx"
        if app_page.is_file() and app_tracked is False:
            note = "app/page.tsx exists on disk but is not in git. Next on this disk prefers it. The published entrypoint is src/app/page.tsx. Do not commit app/ unless every published route is re-exported."
        else:
            note = "No root app/page.tsx. Next uses src/app."
    elif app_page.is_file():
        canonical = "app/page.tsx"
        note = "Next prefers ./app over ./src/app."
    elif (root / "portfolio_os" / "cli.py").is_file():
        canonical = "portfolio_os/cli.py"
        note = "Python control plane. There is no Next homepage."
    return {
        "canonical_entrypoint": canonical,
        "reexport": reexports,
        "dead_paths": dead[:12],
        "note": note,
        "src_tracked": src_tracked,
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
    frontend = classify_frontend(root)
    return {
        "homepage": str(page.relative_to(root)) if page else "",
        "heading": heading,
        "routes": _routes(root),
        "tooling": _scripts(root),
        "frontend": frontend,
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
    front = facts.get("frontend") or {}
    dead = front.get("dead_paths") or []
    dead_lines = "\n".join(f"- `{item}`" for item in dead) or "- none detected"
    canonical = front.get("canonical_entrypoint") or facts.get("homepage") or "unknown"
    build_map = "\n".join(
        [
            f"# Build map: {slug}",
            "",
            "Derived from the repository. Not a guessed architecture.",
            "",
            "## Canonical entrypoint",
            f"`{canonical}`",
            "",
            front.get("note") or "",
            "",
            front.get("reexport") or "",
            "",
            "## Dead or superseded paths",
            dead_lines,
            "",
            "These are not deleted. They are marked so the next pass does not treat them as the product.",
            "",
            "## Homepage evidence",
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
    now = story.get("now") or ["Record a venture review before adding work."]
    execution = story.get("execution") or ("NO_SAFE_HIGH_VALUE_CHANGE" if not story else "BUILD_NOW")
    why = story.get("why_no_change") or (
        "Understanding is UNKNOWN, so a product edit would be invented." if not story else "A bounded change is identified in Now."
    )
    build_plan = "\n".join(
        [
            f"# Build plan: {slug}",
            "",
            "## Execution",
            execution,
            "",
            "## Why",
            why,
            "",
            "## Now",
            *[f"- {item}" for item in now],
            "",
            "## Acceptance",
            "The change matches code that is actually published, and public copy does not claim a route or metric the repository does not contain.",
            "",
            "## Next",
            *[f"- {item}" for item in story.get("later") or ["None recorded."]],
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
        "BUILD_PLAN.md": build_plan,
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
        (
            startup_id,
            slug,
            json.dumps(
                {
                    "heading": facts["heading"],
                    "canonical_entrypoint": (facts.get("frontend") or {}).get("canonical_entrypoint"),
                    "dead_paths": (facts.get("frontend") or {}).get("dead_paths") or [],
                    "execution": (NARRATIVES.get(slug) or {}).get("execution") or "NO_SAFE_HIGH_VALUE_CHANGE",
                    "what": (NARRATIVES.get(slug) or {}).get("what"),
                    "public_safe": (NARRATIVES.get(slug) or {}).get("public_safe"),
                    "internal": (NARRATIVES.get(slug) or {}).get("internal"),
                    "now": (NARRATIVES.get(slug) or {}).get("now") or [],
                }
            ),
            utcnow(),
        ),
    )
    return out
