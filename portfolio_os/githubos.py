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
    PRIVATE_BLOCKED    security or IP blocker on record
    PRIVATE_INTERNAL   private, no blocker, not proposed for publication
    PRIVATE_EXPERIMENT private, explicitly flagged experimental

Security/IP blockers always win. A 100-score repository with an unresolved
secret finding is PRIVATE_BLOCKED, not PUBLIC_FLAGSHIP.
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


def classify(repo: dict[str, Any], readiness: Readiness) -> str:
    """Assign one classification. Blockers override every other signal."""
    if readiness.blocked():
        return "PRIVATE_BLOCKED"
    if repo.get("archived"):
        return "PUBLIC_ARCHIVE"
    if repo.get("identity_repo"):
        return "PUBLIC_ARCHIVE"
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
) -> dict[str, Any]:
    """Turn raw API objects into the canonical inventory."""
    files_by_repo = files_by_repo or {}
    notes = notes or {}
    rows: list[dict[str, Any]] = []

    for repo in sorted(repos, key=lambda r: r.get("name", "")):
        name = repo.get("name", "")
        readiness = score_repository(repo, files_by_repo.get(name, ()))
        classification = classify(repo, readiness)
        age = _age_days(repo.get("pushed_at"))
        rows.append(
            {
                "name": name,
                "full_name": repo.get("full_name", ""),
                "visibility": repo.get("visibility", "private"),
                "archived": bool(repo.get("archived")),
                "classification": classification,
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
        f"| Public systems | **{totals['public']}** of {totals['repositories']} |\n"
        f"| Tests across public repos | **{totals['tests']:,}** |\n"
        f"| Stars | {totals['stars']} |\n"
        f"| Forks | {totals['forks']} |\n"
    )
    lines.append("")

    released = [
        r for r in inventory["repositories"]
        if r["visibility"] == "public" and r["latest_release"]
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
