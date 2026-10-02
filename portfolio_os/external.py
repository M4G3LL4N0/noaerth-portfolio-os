"""External intelligence: what already exists elsewhere, and what we can reuse.

This is the third intelligence lane alongside internal product intelligence and
portfolio intelligence. Its job is narrow and blunt:

    before we build it, does strong existing work already solve this?

Deterministic retrieval runs first (GitHub search, cached, TTL-bound). Grok
reasoning runs second and only over an already-shortlisted set. The Scout never
crawls the world; it reasons about candidates retrieval has already found.

Everything here is internal. Nothing in this module may read the owner-private
directory: `portfolio_os.exclusion` enforces that at the path layer, and the
OWNER-PRIVATE slug is never a research subject.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .exclusion import OWNER_PRIVATE_LABEL

# --------------------------------------------------------------------------
# Policy constants
# --------------------------------------------------------------------------

#: §25 default TTL. Research is not repeated on a daily cycle.
DEFAULT_TTL_DAYS = 30

#: §7 first pass is deliberately small. Volume here costs rate limit and attention.
QUERIES_PER_STARTUP = 5
MAX_QUERIES_PER_STARTUP = 6
MIN_QUERIES_PER_STARTUP = 3

#: §8 deterministic filter thresholds. Tuned to keep interesting abandoned work
#: while dropping stale tiny forks and generated mirrors.
MIN_STARS_INTERESTING = 40
MIN_STARS_TO_EXAMINE = 5
STALE_DAYS = 730
TINY_FORK_STARS = 3
MIRROR_MARKERS = ("mirror", "fork of", "mirrored from", "-mirror", "gh-sync")

#: §12 license classification.
PERMISSIVE = "PERMISSIVE"
COPYLEFT_REVIEW = "COPYLEFT_REVIEW"
PROPRIETARY = "PROPRIETARY"
UNKNOWN = "UNKNOWN"

PERMISSIVE_LICENSES = {
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense",
    "0bsd", "cc0-1.0", "wtfpl", "zlib", "mpl-2.0",
}
COPYLEFT_LICENSES = {
    "gpl-3.0", "gpl-2.0", "agpl-3.0", "lgpl-3.0", "lgpl-2.1", "cc-by-sa-4.0",
    "eupl-1.2",
}
NONOS_LICENSES = {"other", "unlicensed", "noassertion"}

#: §10 classification vocabulary.
DIRECT_COMPETITOR = "DIRECT_COMPETITOR"
HIGH_FIT_OPEN_SOURCE = "HIGH_FIT_OPEN_SOURCE"
PARTIAL_COMPONENT = "PARTIAL_COMPONENT"
FRAMEWORK = "FRAMEWORK"
REFERENCE = "REFERENCE"
ACADEMIC_REFERENCE = "ACADEMIC_REFERENCE"
NAME_COLLISION = "NAME_COLLISION"
ABANDONED_BUT_USEFUL = "ABANDONED_BUT_USEFUL"
LICENSE_RISK = "LICENSE_RISK"
IRRELEVANT = "IRRELEVANT"

#: §14 reuse decisions.
ADOPT = "ADOPT"
FORK = "FORK"
INTEGRATE = "INTEGRATE"
WRAP = "WRAP"
REFERENCE_DECISION = "REFERENCE"
REJECT = "REJECT"
OWNER_REVIEW = "OWNER_REVIEW"

#: §8 first-party exclusion. Our own repositories are neither competitors,
#: nor name collisions, nor reuse candidates. Detected from the git remotes of
#: the portfolio root so it stays correct as accounts change.
FIRST_PARTY_OWNERS: frozenset[str] = frozenset({"m4g3ll4n0"})

#: §24 coverage states.
RESEARCHED = "RESEARCHED"
STALE = "STALE"
NOT_RESEARCHED = "NOT_RESEARCHED"

LANDSCOPE_ARTIFACT = "EXTERNAL_LANDSCAPE.md"

#: Reserved query-log row carrying the survey confidence for a startup.
SURVEY_ROW = "__survey__"

#: Category-specific topical signals. A match here is real domain alignment;
#: a shared generic word in the name is not.
# Field-wide vocabulary. These appear in almost every project in the category's
# industry, so they must not, on their own, make a candidate look like a match.
GENERIC_TOPIC_SIGNALS: frozenset[str] = frozenset({
    "agent", "agents", "ai", "llm", "llms", "model", "models", "framework",
    "tool", "tools", "runtime", "workflow", "human", "app", "apps", "library",
    "sdk", "api", "platform", "system", "automation", "generator", "monitoring",
    "observability", "analytics", "productivity", "developer", "development",
})

_TOPIC_SIGNALS: dict[str, set[str]] = {    "evaluation": {"llm", "evaluation", "eval", "grader", "judge", "benchmark",
                   "rubric", "human", "approval", "agent", "model", "scoring"},
    "agent_infrastructure": {"agent", "orchestration", "llm", "runtime", "mcp",
                             "memory", "workflow", "tool", "squad", "framework"},
    "knowledge": {"knowledge", "note", "memory", "document", "graph", "rag",
                  "retrieval", "second-brain", "wiki"},
    "procurement": {"procurement", "supplier", "vendor", "purchase-order", "rfp",
                    "contract", "sourcing", "invoice"},
    "deployment": {"deployment", "preview", "static-site", "hosting", "vercel",
                   "netlify", "docker", "release"},
    "privacy": {"privacy", "redaction", "vault", "encryption", "local-first",
                "anonymisation", "secret"},
    "devtool": {"developer", "cli", "tooling", "code", "git", "lsp", "debug"},
    "business_automation": {"workflow", "automation", "crm", "pipeline", "lead",
                            "outreach", "sales", "integration"},
    "monitoring": {"monitoring", "observability", "metrics", "alerting", "uptime",
                   "dashboard", "health"},
    "data": {"pipeline", "etl", "analytics", "warehouse", "data", "observability"},
    "marketplace": {"marketplace", "listing", "directory", "vendor"},
    "website": {"website", "landing", "cms", "site", "frontend", "web"},
}

#: Topics that signal a paper, benchmark or research artefact rather than a tool.
ACADEMIC_TOPICS = ("paper", "paperswithcode", "benchmark", "benchmarks", "survey",
                   "awesome", "curated-list", "thesis")


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None = None) -> str:
    return (dt or _now()).astimezone(UTC).isoformat(timespec="seconds")


def _days_ago(days: int) -> str:
    return _iso(_now() - timedelta(days=days))


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------
# Startup vocabulary -> research queries (§4)
# --------------------------------------------------------------------------

#: Category -> the search phrases that actually find existing work. A startup
#: name is a weak query; the shape of the problem is a strong one.
CATEGORY_QUERIES: dict[str, list[str]] = {
    "evaluation": [
        "llm evaluation framework",
        "llm judge grader",
        "model evaluation harness",
        "human approval eval workflow",
    ],
    "knowledge": [
        "knowledge base system",
        "second brain notes ai",
        "document knowledge graph",
        "personal knowledge management ai",
    ],
    "procurement": [
        "procurement automation software",
        "supplier management platform",
        "purchase order workflow open source",
        "rfp response automation",
    ],
    "business_automation": [
        "business process automation",
        "workflow automation open source",
        "lead generation automation",
        "crm automation pipeline",
    ],
    "devtool": [
        "developer productivity tool",
        "cli developer workflow",
        "code search tool",
        "local first developer tool",
    ],
    "agent_infrastructure": [
        "agent orchestration framework",
        "multi agent framework",
        "llm agent runtime",
        "agent memory state management",
        "tool calling agent framework",
    ],
    "deployment": [
        "static site deployment platform",
        "website preview deployment",
        "self hosted deployment tool",
    ],
    "privacy": [
        "privacy first analytics",
        "local first software",
        "data privacy tool",
    ],
    "website": [
        "website builder open source",
        "landing page generator",
        "headless cms website",
    ],
    "data": [
        "data pipeline framework",
        "data observability tool",
        "analytics pipeline open source",
    ],
    "marketplace": [
        "marketplace platform open source",
        "multi vendor platform",
    ],
    "monitoring": [
        "observability platform",
        "monitoring dashboard open source",
        "uptime monitoring tool",
    ],
}

DEFAULT_QUERIES = [
    "{category} software",
    "{category} open source",
    "{category} platform",
]

#: Category keywords used to route a startup to a query family.
#: Identity tokens (slug/name) are strong evidence; the same word appearing once
#: in prose is weak. A single prose mention must not outrank a slug match.
CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("procurement", ("procure", "supplier", "vendor", "sourcing", "rfp", "purchase")),
    ("evaluation", ("evalforge", "grader", "judge", "scoring", "rubric")),
    ("agent_infrastructure", ("agent", "orchestrat", "squad", "fleet", "runtime", "mcp")),
    ("knowledge", ("knowledge", "brain", "note", "memory", "atlas", "corpus", "wiki")),
    ("deployment", ("deploy", "sitecloser", "preview", "vercel", "ship", "hosting")),
    ("privacy", ("privacy", "redact", "vault", "secret", "local-first")),
    ("devtool", ("devtool", "devops", "cli", "tooling", "forge", "gh0st", "git")),
    ("business_automation", ("crm", "lead", "outreach", "sales", "pipeline", "growth")),
    ("monitoring", ("monitor", "observ", "health", "beat", "status")),
    ("data", ("data", "pipeline", "ledger", "etl", "analytics")),
    ("marketplace", ("marketplace", "listing", "directory")),
    ("website", ("site", "website", "web", "landing", "brand", "studio")),
]


@dataclass(slots=True)
class QueryPlan:
    slug: str
    queries: list[str]
    category: str
    name: str
    one_liner: str
    primary_workflow: str
    target_user: str
    core_technology: str
    problem: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "queries": self.queries,
            "category": self.category,
            "name": self.name,
            "one_liner": self.one_liner,
            "primary_workflow": self.primary_workflow,
            "target_user": self.target_user,
            "core_technology": self.core_technology,
            "problem": self.problem,
        }


#: Lines that are never a product description.
_BAD_LINE = re.compile(
    r"^(!\[|\[!\[|<img|<p align|=+\s*$|\s*[-*]\s*\*\*|\s*```|\s*\$\s|npm |pip |yarn |"
    r"\s*\d+\.\s|\s*\||\s*>\s*\[|badge|shields\.io)",
    re.IGNORECASE,
)
#: Framework and tooling names never make a good research query.
_TECH_NOISE = {
    "typescript", "javascript", "python", "nextjs", "next", "react", "vite", "vitest",
    "jest", "pytest", "node", "docker", "github", "gitlab", "linux", "macos", "windows",
    "tailwind", "css", "html", "json", "yaml", "npm", "pnpm", "yarn", "uv", "poetry",
    "fastapi", "express", "rust", "golang", "make", "bash", "curl", "http", "https",
    "api", "sdk", "cli", "config", "build", "dev", "prod", "test", "tests", "core",
    "router", "server", "client", "app", "src", "lib", "dist", "public",
}
#: Prose signals a real one-liner.
_PROSE = re.compile(r"[.!?:]\s|\b(is|are|lets?|helps?|turns?|gives?|enables?|"
                    r"provides?|generates?|automates?|tracks?|manages?|builds?|"
                    r"for teams|for operators|for founders)\b", re.IGNORECASE)
#: Scaffold text that ships with the framework and describes nothing.
_FRAMEWORK_BOILERPLATE = re.compile(
    r"(you can start editing|this is a next\.js|get started with|edit the page|"
    r"deploy your|vercel font|next\.js|create-react-app|vite|webpack|"
    r"npm run|pnpm |yarn |npx |cd \w+ && |repository structure|folder structure|"
    r"deploy(ment)? instructions|contributing|how to run|getting started)",
    re.IGNORECASE,
)


def _is_bad_line(line: str) -> bool:
    return bool(_BAD_LINE.match(line)) or "](http" in line or "shields.io" in line


def _strip_wrapping_emphasis(line: str) -> str:
    """Unwrap a line that is entirely bold or italic.

    README one-liners are routinely written as `**Tagline.**`. That is the most
    valuable sentence in the file, so the surrounding markers are removed before
    the line is judged, instead of the line being rejected for having markers.
    """
    cleaned = line.strip().lstrip(">").strip()
    for marker in ("***", "**", "*", "_", "__"):
        if (
            len(cleaned) > len(marker) * 2
            and cleaned.startswith(marker)
            and cleaned.endswith(marker)
        ):
            inner = cleaned[len(marker): -len(marker)].strip()
            # Only unwrap when the markers enclose the whole line, never when
            # they sit inside a sentence.
            if inner and not marker.strip("*" + "_") in inner:
                return inner
    return cleaned


def _pick_one_liner(lines: list[str]) -> str:
    """Prefer the first prose line after the H1: that is where one-liners live."""
    for index, line in enumerate(lines[:12]):
        if line.strip().startswith("# ") and not line.strip().startswith("## "):
            for follow in lines[index + 1: index + 6]:
                cleaned = _strip_wrapping_emphasis(follow)
                if _is_bad_line(cleaned) or not cleaned or cleaned.startswith("#"):
                    continue
                if _looks_like_description(cleaned):
                    return cleaned
            break
    best = ""
    for line in lines:
        cleaned = _strip_wrapping_emphasis(line)
        cleaned = cleaned.lstrip("#").strip() if cleaned.startswith("#") else cleaned
        if _is_bad_line(cleaned) or len(cleaned) < 28 or len(cleaned) > 240:
            continue
        if not _looks_like_description(cleaned):
            continue
        if cleaned.lower().startswith(("purpose", "description", "about", "install",
                                       "usage", "quick", "getting started", "overview")):
            continue
        if not _reads_as_prose(cleaned):
            continue
        if _FRAMEWORK_BOILERPLATE.search(cleaned):
            continue
        if cleaned.count("-") > 6 or cleaned.count("|") > 2:
            continue
        # Emphasis fragments and mid-sentence continuations read as broken copy.
        if _is_marker_heavy(cleaned) or cleaned.endswith(("and.", "or.", "the.", "of.")):
            continue
        if not cleaned[:1].isupper():
            continue
        # A description states a subject before its verb.
        verb = re.search(r"\b(is|are|lets?|helps?|turns?|gives?|enables?|provides?|"
                         r"generates?|automates?|tracks?|manages?|builds?|"
                         r"keeps?|makes?|brings?|delivers?)\b", cleaned, re.IGNORECASE)
        if verb and verb.start() < 12:
            continue
        best = cleaned
        break
    return best


def _reads_as_prose(cleaned: str) -> bool:
    """A sentence can end at the end of the line, with nothing after the period."""
    if _PROSE.search(cleaned):
        return True
    return bool(re.search(r"[.!?:]$", cleaned.strip()))


def _is_marker_heavy(text: str) -> bool:
    """True for badge rows and emphasis fragments, not for prose with bold terms.

    Counting markers outright rejects perfectly good one-liners that happen to
    bold two or three words. Density is the real signal: a badge row is mostly
    markup, whereas prose has a few markers among many words.
    """
    markers = text.count("*") + text.count("`") + text.count("[") + text.count("_")
    if markers == 0:
        return False
    return markers / max(1, len(text)) > 0.12


def _looks_like_description(cleaned: str) -> bool:
    """Reject badges, headings, fragments, emphasis and dependency lines."""
    if _is_bad_line(cleaned) or cleaned.startswith("#"):
        return False
    if _is_marker_heavy(cleaned):
        return False
    if not cleaned[:1].isupper() or not _reads_as_prose(cleaned):
        return False
    if _FRAMEWORK_BOILERPLATE.search(cleaned):
        return False
    # A task line describes a command someone runs, not the product.
    if re.match(r"^[A-Z][A-Za-z ]*\([^)]*\)\s*\.?$", cleaned):
        return False
    if cleaned.lower().startswith(("purpose", "description", "about", "install",
                                   "usage", "quick", "getting started", "overview",
                                   "for developers", "public site", "this version",
                                   "note", "warning", "example", "see ")):
        return False
    if cleaned.endswith(("and.", "or.", "the.", "of.", ":", ",", "-")):
        return False
    return True


def _package_description(root: Path | None) -> str:
    """The machine-authored description is the most reliable one-liner available."""
    if root is None or not root.is_dir():
        return ""
    for name in ("package.json", "pyproject.toml", "Cargo.toml"):
        path = root / name
        if not path.is_file() or path.stat().st_size > 200_000:
            continue
        try:
            if name == "package.json":
                raw = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
                description = str(raw.get("description") or "").strip()
            elif name == "pyproject.toml":
                match = re.search(
                    r'^description\s*=\s*["\'](.+?)["\']', path.read_text(encoding="utf-8"),
                    re.MULTILINE,
                )
                description = match.group(1).strip() if match else ""
            else:
                match = re.search(
                    r'^description\s*=\s*["\'](.+?)["\']', path.read_text(encoding="utf-8"),
                    re.MULTILINE,
                )
                description = match.group(1).strip() if match else ""
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        description = description.strip()
        if 12 <= len(description) <= 200 and not description.lower().startswith(
            ("todo", "a bootstrap", "lorem")
        ):
            return description
    return ""


def _blurb(slug: str, root: Path | None) -> tuple[str, str, str, str, str]:
    """Pull a one-liner, primary workflow, target user, core tech and problem.

    Reads only committed project text. Never the excluded directory.
    """
    one_liner = workflow = user = tech = problem = ""
    if root is None or not root.is_dir():
        return one_liner, workflow, user, tech, problem
    # A manifest description is written to describe the product, so it wins over
    # README prose. README scanning is a fallback and is deliberately limited to
    # the opening: a security contact address three hundred lines down is not a
    # one-liner, and scanning the whole file is how that mistake happens.
    one_liner = _package_description(root)
    # Only the README describes the product. AGENTS.md/CLAUDE.md are internal
    # operating instructions and their prose must never become a description.
    for name, target in (("README.md", "one_liner"), ("docs/README.md", "one_liner")):
        if one_liner:
            break
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if target == "one_liner":
            one_liner = _pick_one_liner(text.splitlines()[:40])
    heading = re.compile(
        r"^#{1,3}\s*(purpose|problem|primary workflow|workflow|target user|"
        r"audience|stack|technology|core technology)\s*:?\s*$",
        re.IGNORECASE,
    )
    if root.is_dir():
        for candidate in ("README.md", "AGENTS.md", "CLAUDE.md", "docs/README.md"):
            path = root / candidate
            if not path.is_file():
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            except OSError:
                continue
            active = ""
            for line in lines:
                match = heading.match(line.strip())
                if match:
                    active = match.group(1).lower()
                    continue
                if active and line.startswith("#"):
                    active = ""
                    continue
                if active:
                    value = line.strip().lstrip("-*").strip()
                    if len(value) < 3:
                        continue
                    if active == "purpose" and not problem:
                        problem = value
                    elif active in ("primary workflow", "workflow") and not workflow:
                        workflow = value
                    elif active in ("target user", "audience") and not user:
                        user = value
                    elif active in ("stack", "technology", "core technology") and not tech:
                        tech = value
                    active = ""
    if not user:
        user = "operator"
    _ = slug
    return one_liner, workflow, user, tech, problem


#: Generic categories must not win on an incidental prose mention. A category
#: needs either an identity hit, or corroboration, to be selected.
CATEGORY_SPECIFICITY: dict[str, float] = {
    "procurement": 1.0,
    "evaluation": 1.0,
    "agent_infrastructure": 1.0,
    "knowledge": 0.9,
    "deployment": 0.9,
    "privacy": 0.9,
    "devtool": 0.8,
    "business_automation": 0.6,
    "monitoring": 0.5,
    "marketplace": 0.8,
    "website": 0.4,
    "data": 0.4,
}


def _word_hit(token: str, haystack: str) -> bool:
    """Prose match requires a real word boundary."""
    return re.search(rf"\b{re.escape(token)}", haystack) is not None


def _identity_hit(token: str, haystack: str) -> bool:
    """Identity match allows compounds: "fastprocure" and "evalforge" are names.

    A strict word boundary would miss every prefixed brand name, which is exactly
    where the strongest signal lives.
    """
    return token in haystack


def infer_category(slug: str, name: str, blurb: str) -> str:
    """Identity first, prose second.

    "fastprocure-ai" is a procurement product even though its README mentions
    "evaluating AI startups" once.
    """
    identity = f"{slug} {name}".lower().replace("-", " ").replace("_", " ")
    prose = (blurb or "").lower()

    scored: list[tuple[float, int, int, str]] = []
    for category, hints in CATEGORY_HINTS:
        identity_hits = sum(1 for hint in hints if _identity_hit(hint, identity))
        prose_hits = sum(1 for hint in hints if _word_hit(hint, prose))
        if not identity_hits and prose_hits < 2:
            # One incidental prose mention is not a category.
            continue
        specificity = CATEGORY_SPECIFICITY.get(category, 0.5)
        score = identity_hits * 5.0 + prose_hits * 1.0 * specificity
        scored.append((score, identity_hits, prose_hits, category))

    if not scored:
        return "business_automation"
    scored.sort(reverse=True)
    return scored[0][3]


def build_query_plan(
    slug: str,
    name: str,
    root: Path | None = None,
    category: str = "",
) -> QueryPlan:
    """§4. Derive 3-6 focused queries from what the startup actually is.

    A startup name alone is a bad query: it finds forks of itself and unrelated
    same-name products. The problem shape is what finds reusable work.
    """
    one_liner, workflow, user, tech, problem = _blurb(slug, root)
    resolved = category or infer_category(slug, name, f"{one_liner} {problem}")

    queries: list[str] = []
    for phrase in CATEGORY_QUERIES.get(resolved, DEFAULT_QUERIES):
        queries.append(phrase.replace("{category}", resolved.replace("_", " ")))

    # Fold in distinctive, non-generic nouns from the product's own language.
    for text in (workflow, tech):
        for noun in _distinctive_terms(text, limit=2):
            candidate = f"{noun} {resolved.replace('_', ' ')}"
            if candidate not in queries:
                queries.append(candidate)

    # An exact-name query is still run once: it detects name collisions (§19).
    if slug and slug.lower() not in {q.lower() for q in queries}:
        queries.append(slug.lower())

    trimmed = _merge_queries(queries)
    return QueryPlan(
        slug=slug,
        queries=trimmed[:MAX_QUERIES_PER_STARTUP],
        category=resolved,
        name=name,
        one_liner=one_liner,
        primary_workflow=workflow,
        target_user=user,
        core_technology=tech,
        problem=problem,
    )


_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "using", "used",
    "when", "then", "than", "will", "have", "your", "their", "our", "are", "was",
    "not", "but", "you", "all", "can", "who", "what", "which", "how", "build",
    "system", "tool", "service", "platform", "app", "product",
}


def _distinctive_terms(text: str, limit: int = 2) -> list[str]:
    """Pick product-specific words worth searching on.

    Stack vocabulary ("next", "vitest", "router") is excluded: it produces
    queries like "router evaluation", which find nothing.
    """
    words = re.findall(r"[a-z][a-z0-9-]{3,}", (text or "").lower())
    seen: list[str] = []
    for word in words:
        if word in _STOPWORDS or word in _TECH_NOISE or word in seen:
            continue
        if len(word) < 5:
            continue
        seen.append(word)
        if len(seen) >= limit:
            break
    return seen


def _merge_queries(queries: Iterable[str]) -> list[str]:
    """Dedup while preserving order, and normalise whitespace."""
    out: list[str] = []
    seen: set[str] = set()
    for query in queries:
        cleaned = " ".join(str(query).split()).strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


# --------------------------------------------------------------------------
# §6 deterministic GitHub retrieval
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Candidate:
    """One retrieved project, before or after reasoning."""

    source: str = "github"
    repo: str = ""
    owner: str = ""
    name: str = ""
    description: str = ""
    url: str = ""
    homepage: str = ""
    stars: int = 0
    forks: int = 0
    language: str = ""
    license_spdx: str = ""
    activity: str = ""
    archived: bool = False
    topics: list[str] = field(default_factory=list)
    matched_queries: list[str] = field(default_factory=list)
    classification: str = IRRELEVANT
    license_class: str = UNKNOWN
    reuse_fit: int = 0
    reuse_fit_factors: dict[str, int] = field(default_factory=dict)
    reuse_decision: str = OWNER_REVIEW
    could_replace: str = ""
    risks: list[str] = field(default_factory=list)
    rationale: str = ""
    attribution: str = ""
    scout_note: str = ""
    filtered: bool = False
    filter_reason: str = ""

    def as_row(self, slug: str, startup_id: int) -> dict[str, Any]:
        return {
            "startup_id": startup_id,
            "slug": slug,
            "source": self.source,
            "repo": self.repo,
            "owner": self.owner,
            "name": self.name or self.repo,
            "kind": self.source,
            "description": (self.description or "")[:500],
            "url": self.url,
            "homepage": self.homepage,
            "stars": self.stars,
            "forks": self.forks,
            "language": self.language,
            "license_spdx": self.license_spdx,
            "license_class": self.license_class,
            "activity": self.activity,
            "archived": int(self.archived),
            "topics": json.dumps(self.topics),
            "classification": self.classification,
            "reuse_fit": self.reuse_fit,
            "reuse_fit_factors": json.dumps(self.reuse_fit_factors),
            "reuse_decision": self.reuse_decision,
            "could_replace": self.could_replace,
            "risks": json.dumps(self.risks),
            "rationale": self.rationale,
            "attribution": self.attribution,
            "scout_note": self.scout_note,
            "reviewed_at": _iso(),
        }


class Cache:
    """§30. Cache everything so unchanged research is never repeated."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def get(self, key: str) -> list[dict[str, Any]] | None:
        row = self.conn.execute(
            "SELECT payload, expires_at FROM landscape_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] and row["expires_at"] < _iso():
            return None
        try:
            return json.loads(row["payload"])
        except (TypeError, ValueError):
            return None

    def put(self, key: str, source: str, payload: list[dict[str, Any]],
            ttl_days: int = DEFAULT_TTL_DAYS) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO landscape_cache"
            " (cache_key, source, payload, fetched_at, expires_at) VALUES (?,?,?,?,?)",
            (key, source, json.dumps(payload), _iso(), _days_ago(-ttl_days)),
        )

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT COUNT(*) entries, COUNT(DISTINCT source) sources FROM landscape_cache"
        ).fetchone()
        live = self.conn.execute(
            "SELECT COUNT(*) FROM landscape_cache WHERE expires_at IS NULL OR expires_at > ?",
            (_iso(),),
        ).fetchone()
        return {
            "entries": row["entries"] if row else 0,
            "sources": row["sources"] if row else 0,
            "live": live["entries"] if live else 0,
        }


class GitHubRetriever:
    """Deterministic GitHub retrieval through the authenticated `gh` CLI.

    The Scout reasons about what this finds. This class never reasons.
    """

    name = "github"
    per_page = 12
    #: Popularity-pass floor. Anything smaller is not a foundation to build on.
    min_stars = 20

    def __init__(self, timeout: int = 25, dry_run: bool = False) -> None:
        self.timeout = timeout
        self.dry_run = dry_run
        self.available = shutil.which("gh") is not None and not dry_run
        self.calls = 0

    def _once(self, query: str, limit: int, sort: str) -> list[dict[str, Any]]:
        # A star floor on the popularity pass keeps it on projects substantial
        # enough to be worth examining. The relevance pass is left unfiltered
        # because a small repository that matches exactly is still worth a look.
        floor = f" stars:>={self.min_stars}" if sort == "stars" else ""
        try:
            proc = subprocess.run(
                ["gh", "api", "--method", "GET", "search/repositories",
                 "-f", f"q={query} archived:false{floor}", "-f", f"per_page={min(30, limit)}",
                 "-f", f"sort={sort}", "-f", "order=desc"],
                capture_output=True, text=True, timeout=self.timeout, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return []
        if proc.returncode != 0:
            return []
        try:
            return list((json.loads(proc.stdout or "{}") or {}).get("items") or [])
        except ValueError:
            return []

    def search(self, query: str, limit: int = 0) -> list[dict[str, Any]]:
        """Two complementary retrievals, merged.

        Relevance alone returns exact-title homework repos. Stars alone returns
        popular-but-unrelated projects. Reuse candidates sit in the overlap, so
        both are fetched and interleaved.
        """
        if not self.available:
            return []
        limit = limit or self.per_page
        by_name: dict[str, dict[str, Any]] = {}
        for sort, take in (("best", limit), ("stars", limit)):
            self.calls += 1
            for item in self._once(query, take, sort):
                key = item.get("full_name")
                if key and key not in by_name:
                    by_name[key] = item

        # Interleave: alternate the two ranked lists so the shortlist is not
        # dominated by whichever sort happened to run first.
        ordered: list[dict[str, Any]] = []
        ranked_best = self._rank(by_name, query, "best")
        ranked_stars = self._rank(by_name, query, "stars")
        left, right = list(ranked_best), list(ranked_stars)
        while left or right:
            if left:
                ordered.append(left.pop(0))
            if right:
                item = right.pop(0)
                if item not in ordered:
                    ordered.append(item)
        return ordered[:limit * 2]

    @staticmethod
    def _rank(by_name: dict[str, dict[str, Any]], query: str, sort: str
              ) -> list[dict[str, Any]]:
        """Approximate the two GitHub orderings well enough to interleave them."""
        tokens = [t for t in query.lower().split() if len(t) > 3]
        def score(item: dict[str, Any]) -> float:
            text = f"{item.get('full_name','')} {item.get('description','')}".lower()
            hits = sum(1 for t in tokens if t in text)
            stars = (item.get("stargazers_count") or 0) ** 0.5
            return hits * 40 + (stars if sort == "stars" else stars * 0.25)
        return sorted(by_name.values(), key=score, reverse=True)

    @staticmethod
    def to_candidate(item: dict[str, Any], query: str) -> Candidate:
        full_name = str(item.get("full_name") or "")
        owner, _, name = full_name.partition("/")
        license_info = item.get("license") or {}
        return Candidate(
            repo=full_name,
            owner=owner,
            name=name,
            description=str(item.get("description") or ""),
            url=str(item.get("html_url") or ""),
            homepage=str(item.get("homepage") or ""),
            stars=int(item.get("stargazers_count") or 0),
            forks=int(item.get("forks_count") or 0),
            language=str(item.get("language") or ""),
            license_spdx=str(license_info.get("spdx_id") or ""),
            activity=str(item.get("pushed_at") or ""),
            archived=bool(item.get("archived")),
            topics=[str(t) for t in (item.get("topics") or [])],
            matched_queries=[query],
        )


# --------------------------------------------------------------------------
# §8 deterministic filter
# --------------------------------------------------------------------------


def _is_mirror(candidate: Candidate) -> bool:
    haystack = f"{candidate.repo} {candidate.description}".lower()
    return any(marker in haystack for marker in MIRROR_MARKERS)


def _is_tiny_fork(candidate: Candidate) -> bool:
    return candidate.stars <= TINY_FORK_STARS


def _is_academic(candidate: Candidate) -> bool:
    topics = {t.lower() for t in candidate.topics}
    return bool(topics & set(ACADEMIC_TOPICS))


def _stale_days(candidate: Candidate) -> int:
    if not candidate.activity:
        return 0
    try:
        pushed = datetime.fromisoformat(candidate.activity.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, (_now() - pushed).days)


def is_first_party(candidate: Candidate) -> bool:
    owner = (candidate.owner or "").lower()
    return bool(owner) and owner in FIRST_PARTY_OWNERS


def deterministic_filter(
    candidates: list[Candidate], plan: QueryPlan
) -> tuple[list[Candidate], list[Candidate]]:
    """Drop obvious noise before any reasoning. Keep interesting abandoned work.

    Returns (kept, dropped). Dropped candidates carry a reason so the artifact
    can show what was considered and discarded.
    """
    kept: list[Candidate] = []
    dropped: list[Candidate] = []
    slug_tokens = {t for t in re.split(r"[^a-z0-9]+", plan.slug.lower()) if len(t) > 2}

    for candidate in candidates:
        text = f"{candidate.name} {candidate.description} {' '.join(candidate.topics)}".lower()

        if is_first_party(candidate):
            candidate.filtered, candidate.filter_reason = True, "first_party"
        elif not candidate.description.strip():
            candidate.filtered, candidate.filter_reason = True, "no_description"
        elif _is_mirror(candidate):
            candidate.filtered, candidate.filter_reason = True, "generated_mirror"
        elif _is_tiny_fork(candidate) and candidate.stars < MIN_STARS_TO_EXAMINE:
            candidate.filtered, candidate.filter_reason = True, "tiny_fork"
        elif _stale_days(candidate) > STALE_DAYS and candidate.stars < MIN_STARS_INTERESTING:
            # Stale *and* small is noise. Stale but interesting stays.
            candidate.filtered, candidate.filter_reason = True, "stale_and_small"
        else:
            # A repo whose entire name is our slug is a fork of us, not a
            # reusable project. Keep it only if it is genuinely large.
            overlap = slug_tokens & set(
                re.split(r"[^a-z0-9]+", (candidate.name or "").lower())
            )
            if overlap and candidate.stars < 25:
                candidate.filtered, candidate.filter_reason = True, "own_fork"
            elif not any(
                token in text
                for token in _query_tokens(plan.category)
            ) and candidate.stars < 50:
                candidate.filtered, candidate.filter_reason = True, "off_topic"

        (dropped if candidate.filtered else kept).append(candidate)

    # Rank by signal, then cap the shortlist handed to the model (§9).
    kept.sort(key=lambda c: (-_retrieval_score(c), c.repo))
    return kept, dropped


def _query_tokens(category: str) -> set[str]:
    return {t for t in re.split(r"\W+", category.replace("_", " ")) if len(t) > 3}


def _retrieval_score(candidate: Candidate) -> float:
    stars = min(40.0, (candidate.stars or 0) ** 0.5)
    topical = 0.0
    if candidate.license_spdx and candidate.license_spdx.lower() in PERMISSIVE_LICENSES:
        topical += 6.0
    if candidate.topics:
        topical += 2.0
    if not _stale_days(candidate) or _stale_days(candidate) < 180:
        topical += 4.0
    if candidate.description:
        topical += 1.0
    return stars + topical


# --------------------------------------------------------------------------
# §12 license + §11 reuse fit + §10 classification
# --------------------------------------------------------------------------


def classify_license(spdx: str) -> tuple[str, str]:
    """Returns (class, obligations-note). Never guesses past what is declared."""
    key = (spdx or "").strip().lower()
    if not key:
        return UNKNOWN, "No license declared: do not copy code until resolved."
    if key in PERMISSIVE_LICENSES:
        note = {
            "mit": "Usable with attribution; include the license text.",
            "apache-2.0": "Usable with attribution and patent grant; preserve NOTICE.",
            "mpl-2.0": "Usable; modified files stay under MPL.",
            "cc0-1.0": "Public domain dedication; no obligation.",
        }.get(key, "Permissive; preserve attribution and the license text.")
        return PERMISSIVE, note
    if key in COPYLEFT_LICENSES:
        return COPYLEFT_REVIEW, (
            "Copyleft: review distribution obligations before integration."
        )
    if key in NONOS_LICENSES or key in {"proprietary", "commercial"}:
        return PROPRIETARY, "No source rights: do not copy code."
    return UNKNOWN, f"Unrecognised license '{key}': do not copy until resolved."


def reuse_fit(candidate: Candidate, plan: QueryPlan) -> tuple[int, dict[str, int]]:
    """§11. 0-100 across nine named factors. Returns the score and the breakdown.

    The breakdown is stored so any number on screen can be explained.
    """
    text = f"{candidate.name} {candidate.description} {' '.join(candidate.topics)}".lower()
    category_words = _query_tokens(plan.category)
    matched = {token for token in category_words if token in text}
    overlap = len(matched)

    # One shared generic word ("evaluation") is not relevance. Relevance needs
    # several distinct topical signals, or one plus a matching topic tag.
    topical = _TOPIC_SIGNALS.get(plan.category, set())
    hits = {t for t in topical if t in text or t in candidate.topics}
    # Words that describe the whole AI field rather than this category. Every
    # agent framework carries "agent", "llm" and "model", so they cannot
    # distinguish a competitor from an unrelated project in the same field.
    topical_hits = len(hits - GENERIC_TOPIC_SIGNALS)
    distinctive = _distinctive_terms(plan.primary_workflow or plan.problem, limit=3)
    distinctive_hits = sum(1 for token in distinctive if token in text)

    if topical_hits >= 2 and distinctive_hits >= 1:
        functional = _clamp(52 + topical_hits * 9 + distinctive_hits * 8, 0, 100)
    elif topical_hits >= 2:
        # Two category words but nothing from our own workflow language. Plausibly
        # the same field, not the same job.
        functional = _clamp(38 + topical_hits * 7, 0, 100)
    elif topical_hits == 1 and distinctive_hits >= 1:
        functional = _clamp(40 + distinctive_hits * 9, 0, 100)
    elif topical_hits == 1 and len(hits) >= 4:
        functional = _clamp(30 + topical_hits * 8, 0, 100)
    else:
        functional = _clamp(8 + topical_hits * 6 + distinctive_hits * 4, 0, 100)
    quality = _clamp(
        30 + min(35, candidate.stars ** 0.5 * 3.5)
        + (15 if candidate.description else 0)
        + (10 if candidate.topics else 0),
        0, 100,
    )
    stale = _stale_days(candidate)
    activity = _clamp(90 - stale / 12 - (20 if candidate.archived else 0), 0, 100)
    license_score = {
        PERMISSIVE: 100, COPYLEFT_REVIEW: 55, PROPRIETARY: 0, UNKNOWN: 25,
    }[classify_license(candidate.license_spdx)[0]]
    compatibility = _clamp(
        70 if (candidate.language or "").lower() in {"typescript", "javascript", "python"}
        else 55,
        0, 100,
    )
    # Bigger projects cost more to absorb, and that is a real cost.
    integration = _clamp(85 - min(45, candidate.stars / 400.0), 0, 100)
    documentation = _clamp(45 + (35 if candidate.description else 0)
                           + (10 if candidate.homepage else 0), 0, 100)
    community = _clamp(min(100, candidate.stars ** 0.5 * 4), 0, 100)
    security = _clamp(70 + (20 if candidate.license_spdx else -20)
                      - (25 if candidate.archived else 0), 0, 100)
    maintenance = _clamp(85 - stale / 10, 0, 100)

    factors = {
        "functional_overlap": round(functional),
        "code_quality": round(quality),
        "activity": round(activity),
        "license": round(license_score),
        "architecture_compatibility": round(compatibility),
        "integration_effort": round(integration),
        "documentation": round(documentation),
        "community": round(community),
        "security": round(security),
        "maintenance": round(maintenance),
    }
    weights = {
        "functional_overlap": 0.24,
        "code_quality": 0.13,
        "activity": 0.10,
        "license": 0.13,
        "architecture_compatibility": 0.09,
        "integration_effort": 0.08,
        "documentation": 0.06,
        "community": 0.07,
        "security": 0.05,
        "maintenance": 0.05,
    }
    total = sum(factors[key] * weights[key] for key in factors)
    # A popular, well-licensed, active project that does a different job is not
    # reusable. Stars, license and activity are necessary but not sufficient, so
    # functional overlap gates the score instead of merely contributing to it.
    if functional < 30:
        ceiling = 34.0
    elif functional < 45:
        ceiling = 52.0
    elif functional < 60:
        ceiling = 74.0
    else:
        ceiling = 100.0
    return round(_clamp(min(total, ceiling))), factors


def topical_evidence(candidate: Candidate, plan: QueryPlan) -> int:
    """Count genuine domain-alignment signals.

    A high weighted score can be produced by stars alone; high-fit requires that
    the project actually speaks the domain, not that it is popular.
    """
    text = f"{candidate.name} {candidate.description} {' '.join(candidate.topics)}".lower()
    signals = _TOPIC_SIGNALS.get(plan.category, set())
    return len({t for t in signals if t in text or t in candidate.topics})


def classify_candidate(
    candidate: Candidate, plan: QueryPlan, fit: int
) -> tuple[str, str]:
    """Returns (classification, could-replace). Deterministic (§10)."""
    license_class = classify_license(candidate.license_spdx)[0]
    slug_tokens = {t for t in re.split(r"[^a-z0-9]+", plan.slug.lower()) if len(t) > 3}
    name_tokens = {t for t in re.split(r"[^a-z0-9]+", (candidate.name or "").lower())
                   if len(t) > 3}
    text = f"{candidate.description} {' '.join(candidate.topics)}".lower()

    # §19 name collision is a distinct finding, checked before relevance.
    if slug_tokens and slug_tokens <= set(re.split(r"[^a-z0-9]+",
                                                  f"{candidate.owner}/{candidate.name}".lower())):
        if candidate.stars >= 15:
            return NAME_COLLISION, "Signals a name collision to review, not code to reuse."
    if license_class == PROPRIETARY and fit >= 45:
        return LICENSE_RISK, "Competitor reference only; no source reuse permitted."
    if _is_academic(candidate) and fit < 70:
        return ACADEMIC_REFERENCE, "Background reading; not a dependency."
    if candidate.archived:
        if fit >= 60:
            return ABANDONED_BUT_USEFUL, (
                "Unmaintained but substantial; viable as a fork with ownership."
            )
        return REFERENCE, "Abandoned; read for design, do not depend."
    topical = topical_evidence(candidate, plan)
    if fit >= 70 and topical >= 2:
        if name_tokens and (slug_tokens & name_tokens):
            return DIRECT_COMPETITOR, "Same name and same job: a competitor to differentiate from."
        return HIGH_FIT_OPEN_SOURCE, (
            "Could replace a large share of the intended implementation."
        )
    if fit >= 70:
        # Popular, but it does not demonstrably speak this domain.
        return FRAMEWORK if candidate.stars >= 800 else REFERENCE, (
            "Strong project, but no demonstrated overlap with this startup's domain."
        )
    if fit >= 55 and topical >= 1:
        return PARTIAL_COMPONENT, "Reusable component rather than a whole system."
    if fit >= 40:
        if candidate.stars >= 500:
            return FRAMEWORK, "Established framework; useful infrastructure if the fit lands."
        return REFERENCE, "Worth reading before building the same thing."
    return IRRELEVANT, ""


def decide_reuse(
    candidate: Candidate, plan: QueryPlan, fit: int
) -> tuple[str, list[str], str]:
    """§14. Returns (decision, risks, attribution). Never auto-forks on stars."""
    license_class = classify_license(candidate.license_spdx)[0]
    risks: list[str] = []
    attribution = ""

    if license_class == PROPRIETARY:
        return REJECT, ["Proprietary: no source rights."], ""
    if license_class == UNKNOWN:
        return OWNER_REVIEW, ["License unresolved; do not copy until resolved."], ""
    if candidate.classification in (DIRECT_COMPETITOR, NAME_COLLISION):
        return REFERENCE_DECISION, ["Competitor or name collision: read, do not copy."], ""
    if license_class == COPYLEFT_REVIEW:
        # §12: copyleft needs its distribution implications reviewed before any
        # integration. A permissive score elsewhere must not self-authorise this.
        risks.append(
            "Copyleft obligations may constrain how we distribute; review before integration."
        )
        return OWNER_REVIEW, risks, attribution

    if license_class == PERMISSIVE and candidate.license_spdx.lower() == "apache-2.0":
        attribution = "Apache-2.0; preserve NOTICE and license text."
    elif license_class == PERMISSIVE:
        attribution = f"{candidate.license_spdx}; preserve license text and copyright."

    if candidate.archived:
        risks.append("Archived upstream: no security patches unless we fork and own it.")
    if _stale_days(candidate) > 365:
        risks.append("No recent activity: dependencies may be unpatched.")
    if fit >= 78:
        # Forking or integrating high-fit external work is a build-strategy
        # decision, not an automated one. Request review; never self-authorise.
        return OWNER_REVIEW, risks or [
            "High fit: change the build plan before writing new code."
        ], attribution
    if fit >= 62:
        return INTEGRATE, risks, attribution
    if fit >= 48:
        return WRAP, risks, attribution
    if fit >= 30:
        return REFERENCE_DECISION, risks or ["Useful background only."], attribution
    return REJECT, ["Below the reuse floor for this startup."], ""


def security_signals(candidate: Candidate) -> dict[str, Any]:
    """§13. Read-only signals. No code is imported blind."""
    stale = _stale_days(candidate)
    return {
        "archived": candidate.archived,
        "days_since_push": stale,
        "declared_license": candidate.license_spdx or "none",
        "declared_dependencies": bool(candidate.topics),
        "supply_chain_risk": (
            "high" if candidate.archived and stale > 540
            else "medium" if stale > 365 or not candidate.license_spdx
            else "low"
        ),
        "notes": [
            "No dependency graph resolved; risk is inferred from maintenance signals only.",
            "Run a dependency audit before any fork is adopted.",
        ],
    }


# --------------------------------------------------------------------------
# §11 REUSE_LEVERAGE, §23 §24 coverage
# --------------------------------------------------------------------------


def reuse_leverage(candidates: list[Candidate]) -> float:
    """§23. 0 = nothing reusable found; 100 = most of the product is available.

    Weighted by how much of the intended implementation each candidate covers,
    not by how many candidates exist.
    """
    scored = [c for c in candidates if not c.filtered]
    if not scored:
        return 0.0
    reusable = [
        c for c in scored
        if c.reuse_decision in (FORK, INTEGRATE, WRAP, ADOPT)
        and c.license_class in (PERMISSIVE, COPYLEFT_REVIEW)
    ]
    if not reusable:
        return 0.0
    best = max(reusable, key=lambda c: c.reuse_fit)
    top = sorted(reusable, key=lambda c: -c.reuse_fit)[:3]
    top_mean = sum(c.reuse_fit for c in top) / len(top)
    # Breadth is a bonus, never a substitute: five mediocre candidates must not
    # outrank one excellent foundation.
    breadth = min(8.0, len(reusable) * 1.5)
    coverage = best.reuse_fit * 0.6 + top_mean * 0.4
    return round(_clamp(coverage + breadth, 0.0, 92.0), 1)


def coverage_state(landscape: dict[str, Any] | None, ttl_days: int = DEFAULT_TTL_DAYS) -> str:
    if not landscape or not landscape.get("searched_at"):
        return NOT_RESEARCHED
    searched = str(landscape["searched_at"])
    if not searched:
        return NOT_RESEARCHED
    try:
        age = (_now() - datetime.fromisoformat(searched)).days
    except ValueError:
        return NOT_RESEARCHED
    return STALE if age > ttl_days else RESEARCHED


def coverage_report(conn: sqlite3.Connection, ttl_days: int = DEFAULT_TTL_DAYS) -> dict[str, Any]:
    """§24 external research coverage across the whole portfolio."""
    rows = conn.execute(
        "SELECT s.slug, e.searched_at, e.reuse_leverage FROM startups s"
        " LEFT JOIN external_landscape e ON e.startup_id = s.id"
        " WHERE s.owner_private = 0 AND s.slug != ?",
        (OWNER_PRIVATE_LABEL,),
    ).fetchall()
    buckets: dict[str, list[str]] = {RESEARCHED: [], STALE: [], NOT_RESEARCHED: []}
    leverages: list[tuple[str, float]] = []
    for row in rows:
        state = coverage_state(
            {"searched_at": row["searched_at"]} if row["searched_at"] else None, ttl_days
        )
        buckets[state].append(row["slug"])
        if row["reuse_leverage"] is not None:
            leverages.append((row["slug"], float(row["reuse_leverage"])))
    total = len(rows) or 1
    return {
        "total": len(rows),
        "researched": len(buckets[RESEARCHED]),
        "stale": len(buckets[STALE]),
        "not_researched": len(buckets[NOT_RESEARCHED]),
        "external_research_current_pct": round(
            100.0 * len(buckets[RESEARCHED]) / total, 1
        ),
        "reuse_leverage_mean": round(
            sum(v for _, v in leverages) / len(leverages), 1
        ) if leverages else 0.0,
        "not_researched_slugs": sorted(buckets[NOT_RESEARCHED]),
        "stale_slugs": sorted(buckets[STALE]),
    }


# --------------------------------------------------------------------------
# §22 portfolio-wide opportunity buckets
# --------------------------------------------------------------------------

OPP_BUCKETS = {
    "high_reuse_leverage": ("♻ HIGH REUSE LEVERAGE", 70.0),
    "high_competition": ("⚔ HIGH COMPETITION", 3),
    "name_collisions": ("🏷 NAME COLLISIONS", 1),
    "useful_foundations": ("🧱 USEFUL FOUNDATIONS", 1),
    "possible_integrations": ("🧬 POSSIBLE INTEGRATIONS", 2),
}


def portfolio_opportunities(conn: sqlite3.Connection) -> dict[str, Any]:
    """§22/§23. One strategic view across the whole portfolio."""
    rows = conn.execute(
        "SELECT s.slug, e.reuse_leverage, e.high_fit_oss, e.direct_competitors,"
        " e.name_collision, e.reuse_opportunities, e.frameworks"
        " FROM external_landscape e JOIN startups s ON s.id = e.startup_id"
        " WHERE s.owner_private = 0"
    ).fetchall()
    buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in OPP_BUCKETS}

    for row in rows:
        item = {
            "slug": row["slug"],
            "reuse_leverage": row["reuse_leverage"],
            "high_fit_oss": row["high_fit_oss"],
            "direct_competitors": row["direct_competitors"],
            "name_collisions": row["name_collision"],
            "frameworks": row["frameworks"],
        }
        if (row["reuse_leverage"] or 0) >= OPP_BUCKETS["high_reuse_leverage"][1]:
            buckets["high_reuse_leverage"].append(item)
        if (row["direct_competitors"] or 0) >= OPP_BUCKETS["high_competition"][1]:
            buckets["high_competition"].append(item)
        if (row["name_collision"] or 0) >= OPP_BUCKETS["name_collisions"][1]:
            buckets["name_collisions"].append(item)
        if (row["frameworks"] or 0) >= OPP_BUCKETS["useful_foundations"][1]:
            buckets["useful_foundations"].append(item)
        if (row["reuse_opportunities"] or 0) >= OPP_BUCKETS["possible_integrations"][1]:
            buckets["possible_integrations"].append(item)

    for items in buckets.values():
        items.sort(key=lambda i: -(i["reuse_leverage"] or 0))

    return {
        label: {
            "title": title,
            "count": len(buckets[key]),
            "items": buckets[key][:25],
        }
        for key, (title, _) in OPP_BUCKETS.items()
    }

# --------------------------------------------------------------------------
# §11 external research run
# --------------------------------------------------------------------------


@dataclass(slots=True)
class LandscapeResult:
    slug: str
    startup_id: int
    plan: QueryPlan
    candidates: list[Candidate]
    dropped: list[Candidate]
    leverage: float
    queries_run: list[str]
    cache_hits: int = 0
    api_calls: int = 0
    scout_used: bool = False
    scout_model: str = ""
    scout_note: str = ""
    artifact_path: str = ""
    error: str = ""

    def counts(self) -> dict[str, int]:
        return {
            "high_fit_oss": sum(
                1 for c in self.candidates if c.classification == HIGH_FIT_OPEN_SOURCE),
            "direct_competitors": sum(
                1 for c in self.candidates if c.classification == DIRECT_COMPETITOR),
            "reuse_opportunities": sum(
                1 for c in self.candidates
                if c.reuse_decision in (FORK, INTEGRATE, WRAP, ADOPT)),
            "frameworks": sum(1 for c in self.candidates if c.classification == FRAMEWORK),
            "name_collision": sum(
                1 for c in self.candidates if c.classification == NAME_COLLISION),
            "references": sum(1 for c in self.candidates if c.classification == REFERENCE),
            "irrelevant": sum(1 for c in self.candidates if c.classification == IRRELEVANT),
        }

    def survey_confidence(self) -> str:
        """Distinguish "we looked and found little" from "we could not look".

        A near-empty result is only meaningful if the retrieval actually worked.
        """
        if self.api_calls == 0 and self.cache_hits == 0:
            return "NOT_RETRIEVED"
        retrieved = len(self.candidates) + len(self.dropped)
        if retrieved < 5:
            return "THIN"
        if len(self.candidates) == 0 and retrieved > 30:
            # Many results, all discarded: the ecosystem is not open source.
            return "ECOSYSTEM_CLOSED"
        if len(self.candidates) < 3:
            return "THIN"
        return "GOOD"

    def recommendation(self) -> str:
        counts = self.counts()
        confidence = self.survey_confidence()
        if confidence in ("NOT_RETRIEVED", "THIN"):
            return (
                "INSUFFICIENT EVIDENCE: the survey retrieved too little to conclude. "
                "Widen sources before treating this as a low-reuse result."
            )
        if confidence == "ECOSYSTEM_CLOSED":
            return (
                "CONTINUE CUSTOM (verified): open source is genuinely thin for this "
                "category, so the research supports building rather than forking. "
                "The gap itself may be the opportunity: a proprietary tool in this "
                "category is an unbuilt product."
            )
        if counts["name_collision"]:
            return (
                "BRAND_COLLISION_REVIEW: another significant project uses this name. "
                "Do not rename automatically. Assess industry overlap, trademark and "
                "public confusion risk, domain availability, and search discoverability."
            )
        if self.leverage >= 70:
            return (
                "CHANGE THE BUILD PLAN: strong existing work covers most of the "
                "intended implementation. Fork or integrate; build the "
                "differentiated layer instead of a weaker clone."
            )
        if self.leverage >= 45:
            return (
                "INTEGRATE COMPONENTS: reuse the reusable pieces, keep the "
                "differentiated workflow."
            )
        if counts["direct_competitors"] >= 3:
            return (
                "DIFFERENTIATE: strong competition. Narrow the segment, combine "
                "workflows, or use the OSS underneath. Competition alone is not a "
                "reason to stop."
            )
        if counts["direct_competitors"]:
            return "DIFFERENTIATE: a competitor exists; find the wedge."
        return "CONTINUE CUSTOM: little reusable work found."


def _result_from_store(
    conn: sqlite3.Connection,
    slug: str,
    startup_id: int,
    plan: QueryPlan,
    queries: list[str],
) -> LandscapeResult:
    """Rebuild a LandscapeResult from the persisted rows for a fresh startup."""
    header = conn.execute(
        "SELECT reuse_leverage, scout_model, artifact_path FROM external_landscape"
        " WHERE startup_id = ?",
        (startup_id,),
    ).fetchone()
    candidates = [
        _candidate_from_row(row)
        for row in conn.execute(
            "SELECT * FROM landscape_candidates WHERE slug = ? ORDER BY reuse_fit DESC, stars DESC",
            (slug,),
        ).fetchall()
    ]
    return LandscapeResult(
        slug=slug,
        startup_id=startup_id,
        plan=plan,
        candidates=candidates,
        dropped=[],
        leverage=float((header["reuse_leverage"] if header else 0) or 0),
        queries_run=list(queries),
        cache_hits=len(queries),
        api_calls=0,
        scout_used=bool((header["scout_model"] if header else "") or ""),
        scout_model=str((header["scout_model"] if header else "") or ""),
        artifact_path=str((header["artifact_path"] if header else "") or ""),
    )


def _candidate_from_row(row: sqlite3.Row) -> Candidate:
    def load(value: str | None, fallback: Any) -> Any:
        try:
            return json.loads(value) if value else fallback
        except json.JSONDecodeError:
            return fallback

    # Columns differ between schema revisions, so read defensively rather than
    # assuming a migration already ran.
    columns = set(row.keys())
    def get(key: str, fallback: Any = "") -> Any:
        return row[key] if key in columns else fallback

    return Candidate(
        source=get("source", "github"),
        repo=get("repo"),
        owner=get("owner"),
        name=get("name"),
        description=get("description"),
        url=get("url"),
        homepage=get("homepage"),
        stars=int(get("stars", 0) or 0),
        forks=int(get("forks", 0) or 0),
        language=get("language"),
        license_spdx=get("license_spdx"),
        activity=get("activity"),
        archived=bool(get("archived", 0)),
        topics=load(get("topics"), []),
        matched_queries=load(get("matched_queries"), []),
        classification=get("classification", IRRELEVANT),
        license_class=get("license_class", UNKNOWN),
        reuse_fit=int(get("reuse_fit", 0) or 0),
        reuse_fit_factors=load(get("reuse_fit_factors"), {}),
        reuse_decision=get("reuse_decision", OWNER_REVIEW),
        could_replace=get("could_replace"),
        risks=load(get("risks"), []),
        rationale=get("rationale"),
        attribution=get("attribution"),
        scout_note=get("scout_note"),
    )


def research_startup(
    conn: sqlite3.Connection,
    startup: sqlite3.Row,
    portfolio_root: Path,
    *,
    force: bool = False,
    ttl_days: int = DEFAULT_TTL_DAYS,
    scout: Any = None,
    dry_run: bool = False,
) -> LandscapeResult:
    """Run the deterministic pass for one startup, then optionally reason.

    Order matters: retrieve -> filter -> score -> reason. The model never sees
    raw search results.
    """
    slug = str(startup["slug"])
    startup_id = int(startup["id"])
    name = str(startup["name"] or slug)
    if slug == OWNER_PRIVATE_LABEL or startup["owner_private"]:
        raise ValueError("owner-private startup is never a research subject")

    root = portfolio_root / slug if portfolio_root else None
    plan = build_query_plan(slug, name, root)

    existing = conn.execute(
        "SELECT searched_at, queries FROM external_landscape WHERE startup_id = ?",
        (startup_id,),
    ).fetchone()
    if existing and not force and coverage_state(
        {"searched_at": existing["searched_at"]}, ttl_days
    ) == RESEARCHED:
        # The stored research is still current. Return it rather than an empty
        # result, so re-running the command shows the real landscape instead of
        # claiming nothing has been researched.
        return _result_from_store(
            conn, slug, startup_id, plan, json.loads(existing["queries"] or "[]")
        )

    cache = Cache(conn)
    retriever = GitHubRetriever(dry_run=dry_run)
    merged: dict[str, Candidate] = {}
    queries_run: list[str] = []
    cache_hits = 0

    for query in plan.queries:
        cache_key = f"gh:search:{query}"
        payload = None if force else cache.get(cache_key)
        if payload is not None:
            cache_hits += 1
            queries_run.append(query)
        else:
            payload = retriever.search(query)
            if payload:
                cache.put(cache_key, "github", payload, ttl_days=ttl_days)
            queries_run.append(query)
        for item in payload or []:
            candidate = GitHubRetriever.to_candidate(item, query)
            if not candidate.repo:
                continue
            existing_candidate = merged.get(candidate.repo)
            if existing_candidate is None:
                merged[candidate.repo] = candidate
            elif query not in existing_candidate.matched_queries:
                existing_candidate.matched_queries.append(query)
        conn.execute(
            "INSERT OR REPLACE INTO landscape_queries"
            " (slug, query, source, run_at, result_count) VALUES (?,?,?,?,?)",
            (slug, query, "github", _iso(), len(payload or [])),
        )

    kept, dropped = deterministic_filter(list(merged.values()), plan)

    for candidate in kept:
        fit, factors = reuse_fit(candidate, plan)
        candidate.reuse_fit = fit
        candidate.reuse_fit_factors = factors
        candidate.license_class = classify_license(candidate.license_spdx)[0]
        classification, could_replace = classify_candidate(candidate, plan, fit)
        candidate.classification = classification
        candidate.could_replace = could_replace
        decision, risks, attribution = decide_reuse(candidate, plan, fit)
        candidate.reuse_decision = decision
        candidate.risks = risks
        candidate.attribution = attribution
        if not candidate.rationale:
            candidate.rationale = _deterministic_rationale(candidate, plan)

    # §9 optional reasoning over an already-shortlisted set.
    if scout is not None and kept:
        applied = scout.review(plan, kept[:12])
        if applied:
            for candidate in kept:
                note = applied.get(candidate.repo)
                if note:
                    candidate.scout_note = note
            result_scout = True
        else:
            result_scout = False
    else:
        result_scout = False

    result = LandscapeResult(
        slug=slug,
        startup_id=startup_id,
        plan=plan,
        candidates=kept,
        dropped=dropped,
        leverage=reuse_leverage(kept),
        queries_run=queries_run,
        cache_hits=cache_hits,
        api_calls=retriever.calls,
        scout_used=result_scout,
        scout_model=getattr(scout, "model", "") if result_scout else "",
    )
    persist(conn, result)
    return result


def _deterministic_rationale(candidate: Candidate, plan: QueryPlan) -> str:
    top = sorted(candidate.reuse_fit_factors.items(), key=lambda kv: -kv[1])[:3]
    drivers = ", ".join(f"{key.replace('_', ' ')} {value}" for key, value in top)
    return (
        f"{candidate.classification} for {plan.slug} "
        f"({candidate.stars} stars, {candidate.license_spdx or 'no license'}); "
        f"driven by {drivers}"
    )


def persist(conn: sqlite3.Connection, result: LandscapeResult) -> None:
    """Upsert the landscape header and replace its candidate set."""
    counts = result.counts()
    now = _iso()
    conn.execute("DELETE FROM landscape_candidates WHERE startup_id = ?", (result.startup_id,))
    for candidate in result.candidates:
        row = candidate.as_row(result.slug, result.startup_id)
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(
            f"INSERT OR REPLACE INTO landscape_candidates ({columns}) VALUES ({marks})",
            tuple(row.values()),
        )
    conn.execute(
        "INSERT OR REPLACE INTO external_landscape"
        " (startup_id, slug, queries, summary, primary_workflow, reuse_leverage,"
        "  high_fit_oss, direct_competitors, reuse_opportunities, frameworks,"
        "  name_collision, recommendation, coverage, artifact_path, scout_used,"
        "  scout_model, scout_note, searched_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            result.startup_id,
            result.slug,
            json.dumps(result.plan.queries),
            result.plan.one_liner,
            result.plan.primary_workflow,
            result.leverage,
            counts["high_fit_oss"],
            counts["direct_competitors"],
            counts["reuse_opportunities"],
            counts["frameworks"],
            counts["name_collision"],
            result.recommendation(),
            RESEARCHED,
            result.artifact_path,
            int(result.scout_used),
            result.scout_model,
            result.scout_note,
            now,
            now,
        ),
    )

    # Survey confidence rides in the query log so the artifact, the CLI and the
    # Team view all read one value instead of recomputing it.
    conn.execute(
        "INSERT OR REPLACE INTO landscape_queries"
        " (slug, query, source, run_at, result_count) VALUES (?,?,?,?,?)",
        (result.slug, SURVEY_ROW, result.survey_confidence(), now, len(result.candidates)),
    )


def load_landscape(
    conn: sqlite3.Connection, slug: str, ttl_days: int = DEFAULT_TTL_DAYS
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM external_landscape WHERE slug = ?", (slug,)
    ).fetchone()
    if row is None:
        return None
    header = dict(row)
    header["queries"] = json.loads(header.get("queries") or "[]")
    header["coverage"] = coverage_state(header, ttl_days)
    row = conn.execute(
        "SELECT source, result_count FROM landscape_queries"
        " WHERE slug = ? AND query = ? ORDER BY run_at DESC LIMIT 1",
        (slug, SURVEY_ROW),
    ).fetchone()
    header["survey_confidence"] = row["source"] if row else "UNKNOWN"
    header["candidates"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM landscape_candidates WHERE slug = ?"
            " ORDER BY reuse_fit DESC, stars DESC", (slug,)
        )
    ]
    for candidate in header["candidates"]:
        candidate["topics"] = json.loads(candidate.get("topics") or "[]")
        candidate["risks"] = json.loads(candidate.get("risks") or "[]")
        candidate["reuse_fit_factors"] = json.loads(candidate.get("reuse_fit_factors") or "{}")
    return header


# --------------------------------------------------------------------------
# §1 EXTERNAL_LANDSCAPE.md artifact
# --------------------------------------------------------------------------

LANDSCAPE_HEADER = """# External Landscape — {slug}

{summary}

- **Primary workflow**: {workflow}
- **Target user**: {user}
- **Core technology**: {tech}
- **Problem**: {problem}
- **Reuse leverage**: **{leverage}/100**
- **Searched**: {searched} ({coverage}, {age} days ago)
- **Queries**: {queries}
- **Scout**: {scout}

> Internal working document. Competitive research is not a public statement.
> The public site describes the product, never "we copied repo X".
>
> **Sources surveyed**: {sources}
> **Survey confidence**: {confidence}
"""


def render_landscape(landscape: dict[str, Any]) -> str:
    """Render the inspectable artifact. Database stays authoritative."""
    candidates: list[dict[str, Any]] = landscape.get("candidates", [])
    by_class: dict[str, list[dict[str, Any]]] = {}
    for candidate in candidates:
        by_class.setdefault(candidate["classification"], []).append(candidate)

    searched = landscape.get("searched_at", "")
    age = "?"
    if searched:
        try:
            age = (_now() - datetime.fromisoformat(searched)).days
        except ValueError:
            age = "?"

    scout = (
        f"{landscape.get('scout_model')} reviewed the shortlist"
        if landscape.get("scout_used") else "not used (deterministic only)"
    )

    out = [LANDSCAPE_HEADER.format(
        slug=landscape["slug"],
        summary=landscape.get("summary") or "_(no one-liner captured)_",
        workflow=landscape.get("primary_workflow") or "_(not captured)_",
        user=_plan_field(landscape, "target_user"),
        tech=_plan_field(landscape, "core_technology"),
        problem=_plan_field(landscape, "problem"),
        leverage=landscape.get("reuse_leverage") or 0,
        searched=searched or "never",
        coverage=landscape.get("coverage"),
        age=age,
        queries=", ".join(f"`{q}`" for q in landscape.get("queries", [])) or "_none_",
        scout=scout,
        sources=landscape.get("sources") or "GitHub repositories (open source only)",
        confidence=landscape.get("survey_confidence") or "UNKNOWN",
    )]

    def section(title: str, klass: str, limit: int = 8) -> None:
        items = by_class.get(klass, [])[:limit]
        out.append(f"\n## {title}")
        if not items:
            out.append("_none found_")
            return
        out.append("")
        out.append("| project | fit | license | stars | activity | decision | could replace |")
        out.append("|---|---|---|---|---|---|---|")
        for item in items:
            out.append(
                f"| [{_name(item)}]({item.get('url') or ''}) | {_fit(item)} "
                f"| {_license(item)} | {item.get('stars', 0)} "
                f"| {item.get('activity') or '—'} | **{item.get('reuse_decision')}** "
                f"| {_esc(item.get('could_replace') or '—')} |"
            )

    section("Exact-name results", NAME_COLLISION)
    section("Direct competitors", DIRECT_COMPETITOR)
    section("Open-source projects (high fit)", HIGH_FIT_OPEN_SOURCE)
    section("Reusable libraries / partial components", PARTIAL_COMPONENT)
    section("Frameworks", FRAMEWORK)
    section("Reference (read, do not depend)", REFERENCE)
    section("Academic reference", ACADEMIC_REFERENCE)
    section("Abandoned but useful", ABANDONED_BUT_USEFUL)

    # §5 package ecosystem
    out.append("\n## Package ecosystem")
    languages: dict[str, int] = {}
    for candidate in candidates:
        language = candidate.get("language") or "unknown"
        languages[language] = languages.get(language, 0) + 1
    if languages:
        out.append("")
        for language, count in sorted(languages.items(), key=lambda kv: -kv[1]):
            out.append(f"- **{language}**: {count}")
    else:
        out.append("\n_none detected_")

    # §20 §21 dense decision table
    out.append("\n## High-fit reuse candidates")
    strong = sorted(candidates, key=lambda c: -c["reuse_fit"])[:10]
    if not strong:
        out.append("\n_none_")
    else:
        out.append("")
        out.append("| project | type | fit | license | activity | what it could replace | recommendation |")
        out.append("|---|---|---|---|---|---|---|")
        for item in strong:
            out.append(
                f"| {_name(item)} | {item['classification']} | {_fit(item)} "
                f"| {_license(item)} | {'archived' if item.get('archived') else 'active'} "
                f"| {_esc(item.get('could_replace') or '—')} | **{item.get('reuse_decision')}** |"
            )

    out.append("\n## License notes")
    seen_licenses: set[str] = set()
    any_license = False
    for item in strong:
        key = f"{item.get('license_spdx') or 'none'}::{item.get('license_class')}"
        if key in seen_licenses:
            continue
        seen_licenses.add(key)
        any_license = True
        spdx = item.get("license_spdx") or "none declared"
        klass = item.get("license_class")
        note = _license_obligation(klass, spdx)
        out.append(f"- **{spdx}** (`{klass}`) — {note}")
        if item.get("attribution"):
            out.append(f"  - attribution: {_esc(item['attribution'])}")
    if not any_license:
        out.append("\n_none_")

    out.append("\n## Security signals")
    out.append("")
    for item in strong[:5]:
        signals = security_signals(_as_candidate(item))
        out.append(
            f"- **{_name(item)}** — supply-chain risk `{signals['supply_chain_risk']}`, "
            f"last push {signals['days_since_push'] or 'unknown'} days ago, "
            f"declared license `{signals['declared_license']}`"
        )
    out.append("\n> Signals are inferred from maintenance metadata only. "
               "No dependency graph was resolved and no code was imported.")

    out.append(f"\n## Recommendation\n\n**{landscape.get('recommendation', '')}**")

    out.append("\n## Fit detail (why each number)")
    out.append("")
    out.append("| project | " + " | ".join(
        ["overlap", "quality", "activity", "license", "compat", "effort",
         "docs", "community", "security", "maint"]) + " |")
    out.append("|" + "---|" * 11)
    for item in strong[:8]:
        factors = item.get("reuse_fit_factors") or {}
        out.append(
            f"| {_name(item)} | "
            + " | ".join(str(factors.get(k, "—")) for k in (
                "functional_overlap", "code_quality", "activity", "license",
                "architecture_compatibility", "integration_effort",
                "documentation", "community", "security", "maintenance")) + " |"
        )

    considered = [c for c in candidates if c.get("classification") == IRRELEVANT]
    out.append(f"\n## Considered and discarded\n\n{len(considered)} candidates were "
               f"classified irrelevant during the deterministic filter.")
    return "\n".join(out) + "\n"


def _plan_field(landscape: dict[str, Any], field_name: str) -> str:
    value = landscape.get(field_name)
    return _esc(value) if value else "_(not captured)_"


def _name(item: dict[str, Any]) -> str:
    return item.get("name") or item.get("repo") or "unknown"


def _fit(item: dict[str, Any]) -> str:
    return f"**{item.get('reuse_fit', 0)}**"


def _license(item: dict[str, Any]) -> str:
    return f"{item.get('license_spdx') or 'none'} / {item.get('license_class')}"


def _esc(text: str) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _license_obligation(klass: str, spdx: str) -> str:
    return {
        PERMISSIVE: "usable with attribution; preserve the license text",
        COPYLEFT_REVIEW: "review distribution obligations before integration",
        PROPRIETARY: "do not copy code",
        UNKNOWN: "unresolved — do not copy until the license is confirmed",
    }.get(klass, f"unrecognised license '{spdx}'")


def _as_candidate(item: dict[str, Any]) -> Candidate:
    return Candidate(
        source=item.get("source", "github"),
        repo=item.get("repo") or "",
        owner=item.get("owner") or "",
        name=item.get("name") or "",
        description=item.get("description") or "",
        url=item.get("url") or "",
        stars=int(item.get("stars") or 0),
        license_spdx=item.get("license_spdx") or "",
        activity=item.get("activity") or "",
        archived=bool(item.get("archived")),
        topics=item.get("topics") or [],
    )


def write_artifact(
    conn: sqlite3.Connection, slug: str, portfolio_root: Path, ttl_days: int = DEFAULT_TTL_DAYS
) -> str:
    """Write EXTERNAL_LANDSCAPE.md inside the startup's own directory."""
    landscape = load_landscape(conn, slug, ttl_days)
    if landscape is None:
        return ""
    root = portfolio_root / slug
    root.mkdir(parents=True, exist_ok=True)
    path = root / LANDSCOPE_ARTIFACT
    path.write_text(render_landscape(landscape), encoding="utf-8")
    conn.execute(
        "UPDATE external_landscape SET artifact_path = ? WHERE slug = ?",
        (str(path), slug),
    )
    return str(path)
