"""GitHubOS — deterministic GitHub surface inventory and profile generation.

This is not an AI layer. Every number it emits is read from a GitHub API
snapshot, and every section it renders is derived by a rule you can read in
this file. An AI may *advise* whether a repository deserves promotion; it does
not get to invent a number.

Two commands:

    githubos scan     repos.json  -> inventory.json
    githubos render   inventory.json -> bounded markdown block

`scan` consumes the output of ``gh api user/repos`` (or any JSON array of
repository objects with the same fields) so it works unchanged in GitHub
Actions, offline, and against a snapshot taken months ago.

Classification is evidence-based and reproducible:

    PUBLIC_FLAGSHIP    public, scored >= 90 on the readiness rubric
    PUBLIC_SUPPORTING  public, scored >= 70
    PUBLIC_ARCHIVE     public, explicitly archived or marked legacy
    PUBLIC_CANDIDATE   public, scored < 70  -> polish or demote
    SITE_ONLY          deployment/marketing site for a canonical project
    CONTRIBUTION_FORK  fork of someone else's repository, made to send a PR
    PRIVATE_BLOCKED    security or IP blocker on record
    PRIVATE_INTERNAL   private, no blocker, not proposed for publication
    PRIVATE_EXPERIMENT private, explicitly flagged experimental

Security/IP blockers always win. A 100-score repository with an unresolved
secret finding is PRIVATE_BLOCKED, not PUBLIC_FLAGSHIP.

SITE_ONLY and CONTRIBUTION_FORK are exclusions, not scores. They are removed
from every engineering surface — public-system counts, flagship pins, generated
tables, campaign queues — because a marketing site and a fork are not evidence of
engineering. A private GitHub repository does not make the deployed site
private: private source and public deployment are independent facts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

START_MARKER = "<!-- githubos:start -->"
END_MARKER = "<!-- githubos:end -->"

# Files whose absence means a public repository is under-served. Weight is a
# rough contribution to the readiness rubric, not a quality judgement.
COMMUNITY_FILES: dict[str, int] = {
    "README.md": 14,
    "LICENSE": 14,
    "CONTRIBUTING.md": 6,
    "SECURITY.md": 6,
    "CHANGELOG.md": 4,
}

# Repositories that carry identity rather than engineering signal. They are
# scored, but they are never eligible for a flagship pin.
IDENTITY_REPOS = {"why-are-you-here", "M4G3LL4N0"}

# Suffix patterns that mark a repository as deployment infrastructure rather
# than a product. These are a strong signal, not proof — `is_site_only` also
# requires that a canonical sibling exists, so a genuinely web-based product
# named "-website" is not misfiled.
SITE_SUFFIXES = ("-website", "-site", "-web", "-landing", "-marketing")
SITE_PREFIXES = ("website-", "site-", "web-")

# Classifications that must never appear in an engineering surface: not in
# public-system counts, not in flagship pins, not in generated portfolio tables,
# not as contribution-campaign targets, not in launch kits.
NON_ENGINEERING = frozenset({
    "SITE_ONLY",
    "CONTRIBUTION_FORK",
    "PUBLIC_ARCHIVE",
    "PRIVATE_BLOCKED",
})


def site_name_candidates(name: str) -> tuple[str, str]:
    """Return the canonical sibling names a site-only repo might belong to.

    ``agentos-website`` -> ``('agentos', 'agentos-site')``
    ``website-acme``    -> ``('acme', 'acme-website')``
    """
    lowered = name.lower()
    for prefix in SITE_PREFIXES:
        if lowered.startswith(prefix):
            stem = lowered[len(prefix):]
            return stem, f"{stem}-website"
    for suffix in SITE_SUFFIXES:
        if lowered.endswith(suffix):
            stem = lowered[: -len(suffix)]
            return stem, f"{stem}-site"
    return name, name


def is_site_only(repo: dict[str, Any], known_names: set[str] | None = None) -> bool:
    """Semantic test for deployment infrastructure.

    Requires BOTH a naming signal AND a canonical sibling project, so that a
    product whose primary artifact really is a website is not misfiled. An
    explicit ``site_only`` flag always wins.

    Returns False when the repository is flagged as a product, which is how a
    human overrides an ambiguous name.
    """
    if repo.get("is_product") or repo.get("canonical_project"):
        return False
    if repo.get("site_only"):
        return True
    if repo.get("fork"):
        return False

    name = str(repo.get("name") or "")
    lowered = name.lower()
    named_as_site = lowered.endswith(SITE_SUFFIXES) or lowered.startswith(SITE_PREFIXES)
    if not named_as_site:
        return False

    if known_names is None:
        # Without the sibling set we cannot prove the relationship. Requiring a
        # sibling is the conservative choice: misfiling a real product as
        # infrastructure would hide genuine engineering work.
        return False

    stem, alt = site_name_candidates(name)
    return stem in known_names or alt in known_names


@dataclass
class Readiness:
    """A 0-100 publication readiness score, plus the reason for it.

    Held internally. Never render this number on a public profile: a rubric
    score on a stranger's repository is noise dressed up as rigour.
    """

    score: int = 0
    parts: dict[str, int] = field(default_factory=dict)
    blockers: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def blocked(self) -> bool:
        return bool(self.blockers)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _age_days(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - parsed).days)


def score_repository(repo: dict[str, Any], files: Iterable[str] = ()) -> Readiness:
    """Score one repository from API fields and the names of its root files.

    Deterministic on purpose. Same input, same number, every run.
    """
    names = set(files)
    parts: dict[str, int] = {}
    blockers: list[str] = []
    reasons: list[str] = []

    # --- hard gates ---------------------------------------------------------
    if repo.get("archived"):
        reasons.append("archived on GitHub")
    if repo.get("security_blockers"):
        for item in repo["security_blockers"]:
            blockers.append(str(item))
    if repo.get("ip_blocker"):
        blockers.append(str(repo["ip_blocker"]))

    # --- community health (40) ---------------------------------------------
    community = 0
    for name, weight in COMMUNITY_FILES.items():
        if name in names:
            community += weight
        else:
            reasons.append(f"missing {name}")
    parts["community"] = community

    # --- engineering signal (30) -------------------------------------------
    engineering = 0
    if repo.get("has_ci"):
        engineering += 10
    else:
        reasons.append("no CI workflow recorded")
    if repo.get("topics"):
        engineering += 5
    if repo.get("description"):
        engineering += 5
    tests = int(repo.get("test_count") or 0)
    if tests >= 300:
        engineering += 10
    elif tests >= 100:
        engineering += 8
    elif tests >= 20:
        engineering += 5
    elif tests > 0:
        engineering += 2
    else:
        reasons.append("no recorded tests")
    parts["engineering"] = engineering

    # --- discoverability (15) ----------------------------------------------
    discover = 0
    if repo.get("homepage"):
        discover += 6
    if repo.get("has_discussions"):
        discover += 4
    if repo.get("social_preview"):
        discover += 5
    parts["discoverability"] = discover

    # --- maintenance (15) ---------------------------------------------------
    age = _age_days(repo.get("pushed_at"))
    if age is None:
        maintenance = 0
        reasons.append("no push timestamp")
    elif age <= 14:
        maintenance = 15
    elif age <= 45:
        maintenance = 11
    elif age <= 120:
        maintenance = 6
        reasons.append(f"last push {age} days ago")
    else:
        maintenance = 2
        reasons.append(f"stale: last push {age} days ago")
    if repo.get("release_count"):
        maintenance = min(15, maintenance + 0)
    parts["maintenance"] = maintenance

    readiness = Readiness(
        score=sum(parts.values()),
        parts=parts,
        blockers=blockers,
        reasons=reasons,
    )
    if blockers:
        readiness.score = 0
    # COMMUNITY_FILES weights sum slightly above the nominal 40 for community
    # health so a repository carrying every file is not penalised by rounding.
    # Clamp so the reported number always means "out of 100".
    readiness.score = min(100, readiness.score)
    return readiness


def classify(
    repo: dict[str, Any],
    readiness: Readiness,
    known_names: set[str] | None = None,
) -> str:
    """Assign one classification. Blockers override every other signal.

    Order matters: a security or IP blocker wins over everything, then the
    non-engineering exclusions, then visibility, then score.
    """
    if readiness.blocked():
        return "PRIVATE_BLOCKED"
    if repo.get("archived"):
        return "PUBLIC_ARCHIVE"
    if repo.get("identity_repo"):
        return "PUBLIC_ARCHIVE"
    if repo.get("fork") and repo.get("contribution_fork"):
        return "CONTRIBUTION_FORK"
    if is_site_only(repo, known_names):
        return "SITE_ONLY"
    if repo.get("visibility") != "public":
        return "PRIVATE_EXPERIMENT" if repo.get("experimental") else "PRIVATE_INTERNAL"

    if repo["name"] in IDENTITY_REPOS:
        return "PUBLIC_ARCHIVE"
    if readiness.score >= 90:
        return "PUBLIC_FLAGSHIP"
    if readiness.score >= 70:
        return "PUBLIC_SUPPORTING"
    return "PUBLIC_CANDIDATE"


def build_inventory(
    repos: list[dict[str, Any]],
    files_by_repo: dict[str, list[str]] | None = None,
    notes: dict[str, Any] | None = None,
    known_names: set[str] | None = None,
) -> dict[str, Any]:
    """Turn raw API objects into the canonical inventory.

    ``known_names`` is every repository name in the account, public and private.
    It is required to prove the canonical-sibling relationship that SITE_ONLY
    depends on, so it defaults to the names present in ``repos``.
    """
    files_by_repo = files_by_repo or {}
    notes = notes or {}
    rows: list[dict[str, Any]] = []
    if known_names is None:
        known_names = {
            str(r.get("name", "")).lower()
            for r in repos
            if isinstance(r, dict) and r.get("name")
        }

    for repo in sorted(repos, key=lambda r: r.get("name", "")):
        name = repo.get("name", "")
        readiness = score_repository(repo, files_by_repo.get(name, ()))
        classification = classify(repo, readiness, known_names)
        age = _age_days(repo.get("pushed_at"))
        rows.append(
            {
                "name": name,
                "full_name": repo.get("full_name", ""),
                "visibility": repo.get("visibility", "private"),
                "archived": bool(repo.get("archived")),
                "classification": classification,
                "is_engineering": classification not in NON_ENGINEERING,
                "readiness": readiness.score,
                "readiness_parts": readiness.parts,
                "blockers": readiness.blockers,
                "gaps": readiness.reasons,
                "description": repo.get("description") or "",
                "homepage": repo.get("homepage") or "",
                "topics": sorted(repo.get("topics") or []),
                "language": repo.get("language") or "",
                "stars": int(repo.get("stargazers_count") or 0),
                "forks": int(repo.get("forks_count") or 0),
                "open_issues": int(repo.get("open_issues_count") or 0),
                "has_discussions": bool(repo.get("has_discussions")),
                "has_wiki": bool(repo.get("has_wiki")),
                "social_preview": bool(repo.get("social_preview")),
                "release_count": int(repo.get("release_count") or 0),
                "latest_release": repo.get("latest_release") or "",
                "test_count": int(repo.get("test_count") or 0),
                "pushed_at": repo.get("pushed_at") or "",
                "days_since_push": age,
                "notes": notes.get(name, ""),
            }
        )

    counts: dict[str, int] = {}
    for row in rows:
        counts[row["classification"]] = counts.get(row["classification"], 0) + 1

    public = [r for r in rows if r["visibility"] == "public"]
    # "Public systems" means public AND engineering. Including private
    # engineering work here would have reported 134 public systems for an
    # account with 11 public repositories.
    engineering = [
        r for r in rows
        if r["visibility"] == "public" and r["is_engineering"]
    ]
    return {
        "generated_at": _now(),
        "schema": "portfolio-os/githubos/1",
        "totals": {
            "repositories": len(rows),
            "public": len(public),
            "private": len(rows) - len(public),
            "stars": sum(r["stars"] for r in public),
            "forks": sum(r["forks"] for r in public),
            "tests": sum(r["test_count"] for r in public),
            # Engineering totals deliberately exclude SITE_ONLY,
            # CONTRIBUTION_FORK, PUBLIC_ARCHIVE and PRIVATE_BLOCKED. A
            # marketing site is not a system, and a fork we made to send one
            # pull request is not our engineering.
            "public_systems": len(engineering),
            "tests_engineering": sum(r["test_count"] for r in engineering),
            "engineering_total": sum(1 for r in rows if r["is_engineering"]),
            "site_only": sum(1 for r in rows if r["classification"] == "SITE_ONLY"),
            "contribution_forks": sum(
                1 for r in rows if r["classification"] == "CONTRIBUTION_FORK"
            ),
        },
        "classification_counts": counts,
        "flagships": [r["name"] for r in rows if r["classification"] == "PUBLIC_FLAGSHIP"],
        "blocked": [
            {"name": r["name"], "blockers": r["blockers"]}
            for r in rows
            if r["classification"] == "PRIVATE_BLOCKED"
        ],
        "stale_public": [
            r["name"]
            for r in public
            if r["days_since_push"] is not None and r["days_since_push"] > 120
        ],
        "repositories": rows,
    }


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

_TABLE = ("| Project | What it is | Tests | Latest | Stars |\n"
          "| --- | --- | --- | --- | --- |\n")


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _pipe(text: str) -> str:
    return (text or "").replace("|", "\\|")


def _version_key(tag: str) -> tuple:
    """Sort release tags by meaning, not alphabetically.

    Sorting strings would put ``v1.0.0-rc.1`` above ``v0.5.0``, which is
    exactly the kind of small lie this project refuses to tell.
    """
    import re

    match = re.search(r"(\d+)\.(\d+)\.(\d+)(.*)", tag or "")
    if not match:
        return (0, 0, 0, 0, tag or "")
    major, minor, patch, rest = match.groups()
    # A prerelease sorts below its own release.
    return (int(major), int(minor), int(patch), 0 if rest else 1, rest)


def render_block(inventory: dict[str, Any], pins: list[str]) -> str:
    """Render the bounded profile block.

    Only facts present in the inventory are printed. A repository with no
    recorded test count shows `—`, never a guess.
    """
    by_name = {r["name"]: r for r in inventory["repositories"]}
    totals = inventory["totals"]
    lines: list[str] = []

    lines.append("<!-- Generated by Portfolio OS GitHubOS. Do not edit by hand. -->")
    lines.append("")
    lines.append(
        f"| | |\n| --- | --- |\n"
        f"| Public systems | **{totals['public_systems']}** |\n"
        f"| Tests across public systems | **{totals['tests_engineering']:,}** |\n"
        f"| Stars | {totals['stars']} |\n"
        f"| Forks | {totals['forks']} |\n"
    )
    lines.append("")

    released = [
        r for r in inventory["repositories"]
        if r["is_engineering"] and r["visibility"] == "public" and r["latest_release"]
    ]
    released.sort(key=lambda r: _version_key(r["latest_release"]), reverse=True)
    if released:
        lines.append("**Latest releases**\n")
        for row in released[:5]:
            version = row["latest_release"].lstrip("v")
            lines.append(
                f"- [{row['full_name']}](https://github.com/{row['full_name']}) "
                f"**{version}**"
            )
        lines.append("")

    if pins:
        lines.append("**Pinned systems**\n")
        for name in pins:
            row = by_name.get(name)
            if not row:
                continue
            tests = f"{row['test_count']} tests" if row["test_count"] else "no test count"
            lines.append(
                f"- [{row['name']}](https://github.com/{row['full_name']}) — "
                f"{_clip(row['description'], 78) or 'no description'} · {tests}"
            )
        lines.append("")

    supporting = [
        r for r in inventory["repositories"]
        if r["classification"] in ("PUBLIC_SUPPORTING", "PUBLIC_FLAGSHIP")
        and r["is_engineering"]
    ]
    if len(supporting) > len(pins):
        lines.append(_TABLE.rstrip("\n"))
        for row in sorted(supporting, key=lambda r: r["name"]):
            tests = str(row["test_count"]) if row["test_count"] else "—"
            release = row["latest_release"] or "—"
            lines.append(
                f"| [{_pipe(row['name'])}](https://github.com/{row['full_name']}) "
                f"| {_pipe(_clip(row['description'], 70)) or '—'} "
                f"| {tests} | {release} | {row['stars']} |"
            )
        lines.append("")

    lines.append(f"<sub>Snapshot generated {inventory['generated_at']} from the GitHub API.</sub>")
    return "\n".join(lines)


def replace_block(readme: str, block: str) -> str:
    """Replace content between the markers. Outside content is never touched."""
    if START_MARKER not in readme or END_MARKER not in readme:
        raise ValueError(
            f"profile README must contain {START_MARKER} and {END_MARKER}"
        )
    head, _, rest = readme.partition(START_MARKER)
    _, _, tail = rest.partition(END_MARKER)
    return f"{head}{START_MARKER}\n{block}\n{END_MARKER}{tail}"


def write_inventory(path: str | Path, inventory: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(inventory, indent=2) + "\n", encoding="utf-8")
    return target


__all__ = [
    "COMMUNITY_FILES",
    "END_MARKER",
    "IDENTITY_REPOS",
    "Readiness",
    "START_MARKER",
    "build_inventory",
    "classify",
    "render_block",
    "replace_block",
    "score_repository",
    "write_inventory",
]
