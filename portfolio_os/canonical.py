"""Canonical resource identity for every startup.

One startup has one identity across local, GitHub, Vercel, and production.
`<startup>-public` is a legacy naming shape, not a second product.

This module is a scanner and a planner. It performs no destructive action and
never enters an excluded directory.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

from portfolio_os.exclusion import OWNER_PRIVATE_LABEL, iter_top_level

PUBLIC_SUFFIX = "-public"
WEBSITE_SUFFIX = "-website"

DUPLICATE_STATUSES = (
    "NONE",
    "DUPLICATE",
    "WEBSITE_ONLY",
    "CANONICAL_SOURCE_MISNAMED",
    "UNIQUE_HISTORY",
    "SUPERSEDED",
    "UNKNOWN",
)

SAFE_ACTIONS = {
    "RENAME_VERCEL_PROJECT",
    "RENAME_VERCEL_WEBSITE",
    "RELINK_LOCAL_VERCEL",
    "UPDATE_METADATA",
}
DESTRUCTIVE_ACTIONS = {
    "DELETE_VERCEL_PROJECT",
    "DELETE_GITHUB_REPO",
    "ARCHIVE_GITHUB_REPO",
    "MERGE_HISTORY",
}

CACHE_TTL_SECONDS = 3600

# The control plane's own checkout does not share its identity slug. Every other
# startup uses its slug as the local directory name.
LOCAL_ALIASES = {"portfolio-control": "noaerth-portfolio-os"}


class CanonicalError(RuntimeError):
    pass


# ---------------------------------------------------------------- local scan


def _git(repo: Path, *args: str, timeout: int = 25) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip()


def scan_local(root: Path) -> dict[str, dict]:
    """One record per local repository. Excluded directories are never entered."""
    found: dict[str, dict] = {}
    for child in iter_top_level(root):
        if not (child / ".git").exists():
            continue
        remote = _git(child, "config", "--get", "remote.origin.url")
        repo_name = ""
        if remote:
            tail = remote.rstrip("/").rsplit("/", 1)[-1]
            repo_name = tail[:-4] if tail.endswith(".git") else tail
        vercel = child / ".vercel" / "project.json"
        vercel_link = None
        if vercel.is_file():
            try:
                vercel_link = json.loads(vercel.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                vercel_link = None
        found[child.name] = {
            "dir": child.name,
            "path": str(child),
            "remote": remote,
            "remote_repo": repo_name,
            "branch": _git(child, "rev-parse", "--abbrev-ref", "HEAD"),
            "commit": _git(child, "rev-parse", "--short", "HEAD"),
            "commits": _git(child, "rev-list", "--count", "HEAD"),
            "last_commit_at": _git(child, "log", "-1", "--format=%cI"),
            "dirty_files": len(
                [line for line in _git(child, "status", "--porcelain", timeout=60).splitlines() if line.strip()]
            ),
            "vercel_project_id": (vercel_link or {}).get("projectId"),
            "vercel_project_name": (vercel_link or {}).get("projectName"),
            "vercel_org_id": (vercel_link or {}).get("orgId"),
            "is_website_repo": child.name.endswith(WEBSITE_SUFFIX),
        }
    return found


# --------------------------------------------------------------- github scan


def scan_github(owner: str) -> dict[str, dict]:
    """Every repository the authenticated owner can see. Requires `gh auth`."""
    if shutil.which("gh") is None:
        return {}
    raw = ""
    try:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"user/repos?per_page=100&affiliation=owner&visibility=all&sort=full_name",
                "--paginate",
            ],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        raw = proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CanonicalError("github scan failed") from exc
    if not raw.strip():
        return {}
    decoder = json.JSONDecoder()
    items: list[dict] = []
    index = 0
    while index < len(raw):
        while index < len(raw) and raw[index].isspace():
            index += 1
        if index >= len(raw):
            break
        try:
            chunk, index = decoder.raw_decode(raw, index)
        except json.JSONDecodeError:
            break
        if isinstance(chunk, list):
            items.extend(chunk)
    found: dict[str, dict] = {}
    for item in items:
        name = item.get("name")
        if not name:
            continue
        found[name.lower()] = {
            "name": name,
            "id": item.get("id"),
            "url": item.get("html_url"),
            "private": bool(item.get("private")),
            "archived": bool(item.get("archived")),
            "fork": bool(item.get("fork")),
            "default_branch": (item.get("default_branch") or "main"),
            "pushed_at": item.get("pushed_at"),
            "homepage": item.get("homepage"),
            "has_pages": bool(item.get("has_pages")),
            "description": item.get("description"),
        }
    return found


# --------------------------------------------------------------- vercel scan


def _vercel_projects(team_id: str | None) -> dict[str, dict]:
    """Every project. `--paginate` returns a bare list, a single page an object."""
    if shutil.which("vercel") is None:
        return {}
    cmd = ["vercel", "api", "/v9/projects?limit=100", "--paginate"]
    if team_id:
        cmd[2] += f"&teamId={team_id}"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CanonicalError("vercel project list failed") from exc
    raw = proc.stdout
    start = min([i for i in (raw.find("["), raw.find("{")) if i >= 0] or [-1])
    if start < 0:
        return {}
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError as exc:
        raise CanonicalError("vercel project list was not json") from exc
    items = payload if isinstance(payload, list) else payload.get("projects", [])
    found: dict[str, dict] = {}
    for item in items:
        link = item.get("link") or {}
        if not isinstance(link, dict):
            link = {"type": str(link)}
        targets = item.get("targets") or {}
        production = targets.get("production") or {}
        aliases = production.get("alias") or production.get("automaticAliases") or []
        found[item["name"]] = {
            "name": item["name"],
            "id": item.get("id"),
            "framework": item.get("framework"),
            "node_version": item.get("nodeVersion"),
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "root_directory": item.get("rootDirectory") or ".",
            "link_type": link.get("type"),
            "link_repo": (link.get("repo") or ""),
            "link_org": link.get("org"),
            "production_branch": link.get("productionBranch") or item.get("productionBranch"),
            "domain": _pick_domain(item["name"], aliases),
            "aliases": aliases,
        }
    return found


def _is_homepage_surface(project: dict, deployment: dict | None) -> bool:
    """A thin public homepage, not a second copy of the product.

    Deterministic signals only: no Git linkage, no framework preset, and a
    near-empty deployment history.
    """
    if project.get("link_type"):
        return False
    if project.get("framework"):
        return False
    count = (deployment or {}).get("total", 0)
    return count <= 2


def _pick_domain(name: str, aliases: list[str]) -> str:
    """Prefer the assigned domain that is not the bare <name>.vercel.app stub."""
    if not aliases:
        return ""
    bare = f"{name}.vercel.app"
    for alias in aliases:
        if alias != bare:
            return alias
    return aliases[0]


def vercel_latest_deployment(project_id: str, team_id: str | None) -> dict | None:
    """Newest production deployment for one project. The list endpoint omits these."""
    cmd = ["vercel", "api", f"/v6/deployments?projectId={project_id}&limit=5"]
    if team_id:
        cmd[2] += f"&teamId={team_id}"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    raw = proc.stdout
    start = min([i for i in (raw.find("["), raw.find("{")) if i >= 0] or [-1])
    if start < 0:
        return None
    try:
        payload = json.loads(raw[start:])
    except json.JSONDecodeError:
        return None
    items = payload.get("deployments", []) if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not items:
        return None
    newest = max(items, key=lambda d: d.get("created") or 0)
    meta = newest.get("meta") or {}
    return {
        "id": newest.get("uid"),
        "state": newest.get("readyState"),
        "target": newest.get("target"),
        "created_at": newest.get("created"),
        "url": newest.get("url"),
        "commit": meta.get("githubCommitSha") or meta.get("gitlabCommitSha"),
        "message": (meta.get("githubCommitMessage") or "")[:160],
        "total": len(items),
    }


def vercel_project_detail(name: str, team_id: str | None) -> dict:
    """Full single-project record. Used to plan renames safely."""
    cmd = ["vercel", "api", f"/v9/projects/{name}"]
    if team_id:
        cmd[-1] += f"?teamId={team_id}"
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90, check=False)
    raw = proc.stdout
    start = raw.find("{")
    if start < 0:
        raise CanonicalError(f"vercel project {name} was not readable")
    item = json.loads(raw[start:])
    env_names = sorted((item.get("env") or {}).keys())
    link = item.get("link") or {}
    return {
        "name": item.get("name"),
        "id": item.get("id"),
        "root_directory": item.get("rootDirectory") or ".",
        "production_branch": item.get("productionBranch"),
        "framework": item.get("framework"),
        "link_type": link.get("type"),
        "link_repo": (link.get("repo") or {}).get("name"),
        "link_org": (link.get("org") or {}).get("login"),
        "env_names": env_names,
        "env_count": len(env_names),
        "targets": item.get("targets") or {},
        "updated_at": item.get("updatedAt"),
    }


# ------------------------------------------------------------ classification


def _bare(name: str) -> str:
    for suffix in (WEBSITE_SUFFIX, PUBLIC_SUFFIX):
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _ms(value) -> str:
    if not value:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(int(value) / 1000))
    except (TypeError, ValueError, OSError):
        return ""


def classify(startup: str, local: dict, vercel: dict, deploys: dict | None = None) -> dict:
    """Decide the duplicate status and the next safe action for one startup.

    Never returns a destructive action. Destructive work is queued instead.
    """
    core = startup
    website = f"{startup}{WEBSITE_SUFFIX}"
    legacy = f"{startup}{PUBLIC_SUFFIX}"
    local_name = LOCAL_ALIASES.get(startup, startup)

    vercel_core = vercel.get(core)
    vercel_legacy = vercel.get(legacy)
    deploys = deploys or {}
    core_dep = deploys.get(core)
    legacy_dep = deploys.get(legacy)

    status = "NONE"
    action = "NONE"
    note = ""

    if vercel_legacy and not vercel_core:
        status = "CANONICAL_SOURCE_MISNAMED"
        action = "RENAME_VERCEL_PROJECT"
        note = f"canonical Vercel project does not exist; {legacy} holds the real history"
    elif vercel_legacy and vercel_core:
        newer_is_legacy = (vercel_legacy.get("updated_at") or 0) > (vercel_core.get("updated_at") or 0)
        if _is_homepage_surface(vercel_legacy, legacy_dep):
            # The canonical name belongs to the product project, so a homepage
            # surface cannot take it. `-public` is not a description of what the
            # project is, only of how it was created, and it leaks into every
            # human-facing surface. Rename to the website suffix, which is what
            # twelve projects in this portfolio already use. Architecture and
            # resource naming are separate concerns.
            status = "WEBSITE_ONLY"
            action = "RENAME_VERCEL_WEBSITE"
            note = (
                f"{legacy} is the public homepage surface: no Git link, no framework "
                f"preset, {legacy_dep.get('total', 0) if legacy_dep else 0} deployment(s), "
                f"and {core} is the product. Rename to {core}{WEBSITE_SUFFIX}."
            )
        elif legacy_dep is not None and core_dep is None:
            status = "DUPLICATE"
            action = "MERGE_REVIEW"
            note = f"{core} has no production deployment while {legacy} is live"
        elif legacy_dep is None and core_dep is not None:
            status = "SUPERSEDED"
            action = "ARCHIVE_CANDIDATE"
            note = f"{legacy} has no production deployment while {core} is live"
        elif (legacy_dep or {}).get("commit") and (core_dep or {}).get("commit"):
            if legacy_dep["commit"] != core_dep["commit"]:
                status = "DUPLICATE"
                action = "MERGE_REVIEW"
                note = f"diverged commits {legacy_dep['commit'][:8]} vs {core_dep['commit'][:8]}"
            else:
                status = "SUPERSEDED"
                action = "ARCHIVE_CANDIDATE"
                note = "both projects serve the same commit"
        elif newer_is_legacy:
            status = "DUPLICATE"
            action = "MERGE_REVIEW"
            note = f"{legacy} was updated more recently than {core}"
        else:
            status = "DUPLICATE"
            action = "MERGE_REVIEW"
            note = "both projects exist; no commit metadata to separate them"

    if local.get(website) and not local.get(local_name) and status == "NONE":
        status = "WEBSITE_ONLY"
        action = "ADOPT_CORE_LOCAL"
        note = "only the website checkout exists locally"

    record = {
        "startup": startup,
        "local_core_repo": local_name if local_name in local else "",
        "local_website_repo": website if website in local else "",
        "github_core": local.get(local_name, {}).get("remote_repo", "") if local_name in local else "",
        "github_public": "",
        "vercel_core": core if vercel_core else "",
        "vercel_core_id": (vercel_core or {}).get("id", ""),
        "vercel_public": legacy if vercel_legacy else "",
        "vercel_public_id": (vercel_legacy or {}).get("id", ""),
        "production_domain": "",
        "production_branch": "",
        "production_commit": "",
        "github_remote": local.get(local_name, {}).get("remote", "") if local_name in local else "",
        "duplicate_status": status,
        "recommended_action": action,
        "note": note,
    }

    live = vercel_core or vercel_legacy
    if live:
        live_dep = deploys.get(live["name"])
        record["production_domain"] = live.get("domain", "")
        record["production_branch"] = live.get("production_branch") or "main"
        record["production_commit"] = (live_dep or {}).get("commit") or ""
        record["vercel_git_link"] = live.get("link_type") or "none"
    else:
        record["vercel_git_link"] = ""
    return record


def build_records(
    startups: list[str],
    local: dict,
    github: dict,
    vercel: dict,
    deploys: dict | None = None,
) -> list[dict]:
    records = []
    for slug in startups:
        record = classify(slug, local, vercel, deploys)
        if slug.lower() in github:
            repo = github[slug.lower()]
            record["github_core_id"] = repo["id"]
            record["github_core"] = repo["name"]
            if not record["github_remote"]:
                record["github_remote"] = repo["url"] or ""
        else:
            record["github_core_id"] = ""
        records.append(record)
    return records


# ------------------------------------------------------------------- caching


def cache_path(package_root: Path) -> Path:
    return package_root / "data" / "canonical" / "scan.json"


def load_cache(package_root: Path, ttl: int = CACHE_TTL_SECONDS) -> dict | None:
    path = cache_path(package_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - payload.get("captured_at", 0) > ttl:
        return None
    return payload


def save_cache(package_root: Path, payload: dict) -> None:
    path = cache_path(package_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1), encoding="utf-8")


def capture(
    package_root: Path,
    root: Path,
    startups: list[str],
    team_id: str | None,
    force: bool = False,
) -> dict:
    cached = None if force else load_cache(package_root)
    if cached is not None:
        return cached
    vercel = _vercel_projects(team_id)
    payload = {
        "captured_at": time.time(),
        "captured_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "local": scan_local(root),
        "github": scan_github(""),
        "vercel": vercel,
        "startups": startups,
        "deploys": {},
    }
    payload["deploys"] = _collect_deployments(startups, vercel, team_id)
    save_cache(package_root, payload)
    return payload


def _collect_deployments(startups: list[str], vercel: dict, team_id: str | None) -> dict:
    """Deployment state for every project a canonical startup actually uses.

    The list endpoint omits deployment history, so the report would otherwise
    have to guess whether a live domain has a working build behind it.
    """
    wanted: dict[str, str] = {}
    for slug in startups:
        for name in (slug, f"{slug}{PUBLIC_SUFFIX}", f"{slug}{WEBSITE_SUFFIX}"):
            project = vercel.get(name)
            if project and project.get("id"):
                wanted[project["id"]] = name
    out: dict[str, dict] = {}
    for project_id, name in sorted(wanted.items(), key=lambda kv: kv[1]):
        summary = vercel_latest_deployment(project_id, team_id)
        if summary is not None:
            out[name] = summary
    return out


# ------------------------------------------------------------ safe operations


def vercel_rename(project: str, target: str, team_id: str | None) -> dict:
    """Rename one Vercel project in place. Project id, domains, env, history stay."""
    endpoint = f"/v9/projects/{project}"
    if team_id:
        endpoint += f"?teamId={team_id}"
    args = ["vercel", "api", endpoint, "-X", "PATCH", "-f", f"name={target}"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
    if proc.returncode != 0:
        raise CanonicalError(f"vercel rename failed for {project}: {proc.stderr.strip()[:200]}")
    raw = proc.stdout
    start = raw.find("{")
    payload = json.loads(raw[start:]) if start >= 0 else {}
    return {
        "renamed_from": project,
        "renamed_to": payload.get("name", target),
        "project_id": payload.get("id"),
        "domains": ((payload.get("targets") or {}).get("production") or {}).get("alias") or [],
    }


def relink_local_vercel(root: Path, project_id: str, new_name: str) -> list[str]:
    """Point every local checkout already bound to project_id at new_name.

    Only the human-facing projectName changes. The id is the stable link.
    """
    changed = []
    for child in iter_top_level(root):
        link = child / ".vercel" / "project.json"
        if not link.is_file():
            continue
        try:
            payload = json.loads(link.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("projectId") != project_id:
            continue
        if payload.get("projectName") == new_name:
            continue
        payload["projectName"] = new_name
        link.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        changed.append(child.name)
    return changed


def github_rename(owner: str, repo: str, target: str) -> dict:
    args = ["gh", "api", f"repos/{owner}/{repo}", "--method", "PATCH", "--field", f"name={target}"]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        raise CanonicalError(f"github rename failed for {repo}: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout) if proc.stdout.strip().startswith("{") else {"name": target}


# ------------------------------------------------------------------ markdown


FIELD_ORDER = [
    ("local_core_repo", "LOCAL CORE REPO"),
    ("local_website_repo", "LOCAL WEBSITE REPO"),
    ("github_core", "GITHUB CORE"),
    ("github_public", "GITHUB `-public`"),
    ("vercel_core", "VERCEL CORE"),
    ("vercel_public", "VERCEL `-public`"),
    ("production_domain", "PRODUCTION DOMAIN"),
    ("github_remote", "GITHUB REMOTE"),
    ("vercel_core_id", "VERCEL PROJECT ID"),
    ("production_branch", "PRODUCTION BRANCH"),
    ("production_commit", "PRODUCTION COMMIT"),
    ("duplicate_status", "DUPLICATE STATUS"),
    ("recommended_action", "RECOMMENDED ACTION"),
]

SLUG_RE = re.compile(r"[^a-z0-9]+")


def render_markdown(records: list[dict], summary: dict) -> str:
    lines = [
        "# PORTFOLIO_RESOURCE_CANONICALIZATION",
        "",
        "One startup, one identity. Generated by `portfolio canonical-resources --write`.",
        "Owner-private material is represented only as `OWNER-PRIVATE`.",
        "",
        "## Audit summary",
        "",
        f"- canonical startups audited: {summary['startups']}",
        f"- GitHub repositories scanned: {summary['github_total']}",
        f"- GitHub `*-public` repositories: {summary['github_public']}",
        f"- local repositories scanned: {summary['local_total']}",
        f"- local `*-public` directories: {summary['local_public']}",
        f"- Vercel projects scanned: {summary['vercel_total']}",
        f"- Vercel `*-public` projects: {summary['vercel_public']}",
        f"- Vercel `*-public` with no canonical twin: {summary['vercel_public_orphan']}",
        f"- Vercel `*-public` duplicated against a live canonical project: {summary['vercel_public_dup']}",
        f"- safe renames queued: {summary['safe_renames']}",
        f"- ambiguous merges queued for owner review: {summary['owner_review']}",
        f"- destructive cleanups held: {summary['destructive_held']}",
        "",
        "## Classification legend",
        "",
        "| status | meaning |",
        "| --- | --- |",
        "| NONE | one identity already |",
        "| DUPLICATE | two live identities hold conflicting history |",
        "| WEBSITE_ONLY | only the website checkout exists locally |",
        "| CANONICAL_SOURCE_MISNAMED | `-public` is the only real project |",
        "| UNIQUE_HISTORY | `-public` holds history the canonical repo lacks |",
        "| SUPERSEDED | `-public` has no live deployment |",
        "| UNKNOWN | not enough evidence. never deleted |",
        "",
    ]
    for record in records:
        if record.get("owner_private"):
            lines += [f"## {OWNER_PRIVATE_LABEL}", "", f"- {OWNER_PRIVATE_LABEL}", ""]
            continue
        lines.append(f"## {record['startup']}")
        lines.append("")
        for key, label in FIELD_ORDER:
            value = record.get(key) or "-"
            lines.append(f"- {label}: {value}")
        if record.get("note"):
            lines.append(f"- NOTE: {record['note']}")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_table(records: list[dict], limit: int = 0) -> list[str]:
    rows = records[:limit] if limit else records
    lines = [f"{'STARTUP':24} {'GITHUB':22} {'VERCEL':22} {'DOMAIN':34} DUPES           ACTION"]
    for record in rows:
        if record.get("owner_private"):
            lines.append(f"{OWNER_PRIVATE_LABEL:24} {'-':22} {'-':22} {'-':34} {'-':16} -")
            continue
        lines.append(
            "{:24} {:22} {:22} {:34} {:16} {}".format(
                record["startup"][:24],
                (record.get("github_core") or "-")[:22],
                (record.get("vercel_core") or record.get("vercel_public") or "-")[:22],
                (record.get("production_domain") or "-")[:34],
                (record.get("duplicate_status") or "-")[:16],
                record.get("recommended_action") or "-",
            )
        )
    return lines