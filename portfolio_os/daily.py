"""Daily portfolio report assembly and rendering.

JSON is the source of truth. Markdown is what the bot delivers. HTML is what
Team renders. All three are generated from the same payload so they cannot
drift, and none of them calls a model.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from portfolio_os.intelligence import (
    SCORE_MODEL_VERSION,
    STAGE_EXPECTATION,
    STAGE_ORDER,
    WEIGHTS,
    attention,
    baseline_attainment,
    collect_git_windows,
    collect_repo_facts,
    commercial_potential,
    dimension_scores,
    structure_signal,
    heat,
    importance,
    infer_stage,
    infer_type,
    load_facts,
    momentum,
    save_facts,
    viability,
)

BANDS = (
    ("🟥", "CRITICAL", "far below baseline for its stage"),
    ("🟧", "NEEDS_WORK", "below baseline for its stage"),
    ("🟨", "BASELINE", "at its stage baseline"),
    ("🟩", "HEALTHY", "healthy for its stage"),
    ("🟦", "AHEAD", "ahead of its stage baseline"),
)

BAND_ICON = {name: icon for icon, name, _ in BANDS}


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def _band(value: float) -> str:
    if value >= 78:
        return "AHEAD"
    if value >= 62:
        return "HEALTHY"
    if value >= 45:
        return "BASELINE"
    if value >= 28:
        return "NEEDS_WORK"
    return "CRITICAL"


def band_icon(name: str) -> str:
    return BAND_ICON.get(name, "🟨")


BATCH_REVIEW_THRESHOLD = 200


def _batch_review_days(conn: sqlite3.Connection) -> set[str]:
    """Days where an automated sweep wrote reviews.

    A day with hundreds of portfolio-wide verdicts is a pipeline run, not human
    or per-startup review evidence. Counting it as quality signal permanently
    marks a startup as failing, so these days are excluded from scoring.
    """
    rows = conn.execute(
        """
        SELECT substr(timestamp, 1, 10) AS day, COUNT(*) AS n
        FROM reviews GROUP BY day HAVING n > ?
        """,
        (BATCH_REVIEW_THRESHOLD,),
    ).fetchall()
    return {row["day"] for row in rows}


def reviews_for(conn: sqlite3.Connection, startup_id: int, days: int = 30,
                excluded_days: set[str] | None = None) -> dict:
    excluded_days = excluded_days or set()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """
        SELECT resolution, dimension, substr(timestamp, 1, 10) AS day FROM reviews
        WHERE startup_id = ? AND timestamp >= ?
        """,
        (startup_id, since),
    ).fetchall()
    usable = [row for row in rows if row["day"] not in excluded_days]
    total = len(usable)
    passed = sum(1 for row in usable if row["resolution"] == "pass")
    failed = total - passed
    by_dimension: dict[str, list[int]] = {}
    for row in usable:
        by_dimension.setdefault(row["dimension"], []).append(1 if row["resolution"] == "pass" else 0)

    def rate(dimension: str) -> float:
        values = by_dimension.get(dimension, [])
        return sum(values) / len(values) if values else 0.0

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "batch_excluded": len(rows) - total,
        "pass_rate": passed / total if total else 0.0,
        "fail_rate": failed / total if total else 1.0,
        "design_pass_rate": rate("design_quality"),
        "mobile_pass_rate": rate("mobile"),
        "desktop_pass_rate": rate("desktop"),
        "clarity_pass_rate": rate("clarity"),
        "mobile": rate("mobile") > 0,
        "desktop": rate("desktop") > 0,
        "design": rate("design_quality") > 0,
    }


def signals_for(conn: sqlite3.Connection, startup_id: int, days: int = 30,
                excluded_days: set[str] | None = None) -> dict:
    excluded_days = excluded_days or set()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    material = conn.execute(
        "SELECT COUNT(*) AS n FROM material_improvements WHERE startup_id = ? AND created_at >= ?",
        (startup_id, since),
    ).fetchone()["n"]
    releases = conn.execute(
        """
        SELECT COUNT(*) AS n FROM deployments
        WHERE startup_id = ? AND timestamp >= ? AND status NOT IN ('blocked', 'held')
        """,
        (startup_id, since),
    ).fetchone()["n"]
    recovered = conn.execute(
        """
        SELECT COUNT(*) AS n FROM work_items
        WHERE startup_id = ? AND completed_at >= ? AND type = 'recover_and_improve'
        """,
        (startup_id, since),
    ).fetchone()["n"]
    passed_rows = conn.execute(
        "SELECT resolution, substr(timestamp, 1, 10) AS day FROM reviews "
        "WHERE startup_id = ? AND resolution = 'pass' AND timestamp >= ?",
        (startup_id, since),
    ).fetchall()
    reviews_passed_7d = sum(
        1 for row in passed_rows if row["day"] not in excluded_days
    )
    return {
        "material_improvements": material,
        "releases": releases,
        "recovered_workflows": recovered,
        "reviews_passed_7d": reviews_passed_7d,
    }


def overrides(package_root: Path) -> dict[str, dict]:
    path = package_root / "data" / "intelligence" / "overrides.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ------------------------------------------------------------------ assembly


def build_report(
    conn: sqlite3.Connection,
    root: Path,
    package_root: Path,
    scan: dict,
    refetch_facts: bool = False,
) -> dict:
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    override_map = overrides(package_root)
    excluded_review_days = _batch_review_days(conn)

    cached = None if refetch_facts else load_facts(package_root)
    facts: dict[str, dict] = {}
    if cached:
        facts = cached

    canonical = {
        row["slug"]: dict(row)
        for row in conn.execute("SELECT * FROM canonical_resources").fetchall()
    }

    startups = conn.execute(
        """
        SELECT * FROM startups
        WHERE owner_private = 0
        ORDER BY slug
        """
    ).fetchall()

    local_index = scan.get("local", {})
    vercel_index = scan.get("vercel", {})
    deploy_index = scan.get("deploys", {})
    github_index = scan.get("github", {})

    missing: list[Path] = []
    for slug in [row["slug"] for row in startups]:
        core = canonical.get(slug) or {}
        repo_name = core.get("local_core_repo") or (slug if slug in local_index else "")
        if repo_name and repo_name in local_index and repo_name not in facts:
            missing.append(Path(local_index[repo_name]["path"]))
    for repo in missing:
        try:
            facts[repo.name] = {
                "surface": collect_repo_facts(repo),
                "git": collect_git_windows(repo, now),
            }
        except OSError:
            continue
    if missing:
        save_facts(package_root, facts)

    reuse_map = _reuse_map(conn)
    entries: list[dict] = []
    for row in startups:
        slug = row["slug"]
        core = canonical.get(slug) or {}
        repo_name = core.get("local_core_repo") or (slug if slug in local_index else "")
        local = local_index.get(repo_name, {})
        bundle = facts.get(repo_name) or {}
        surface = bundle.get("surface") or {}
        git = bundle.get("git") or {}

        vercel_project = vercel_index.get(slug)
        if vercel_project is None:
            vercel_project = vercel_index.get(f"{slug}-public")
        deployment = deploy_index.get((vercel_project or {}).get("name", "")) or (
            deploy_index.get(slug) or deploy_index.get(f"{slug}-public")
        )
        if vercel_project is None and (core.get("vercel_public_id") or core.get("vercel_core_id")):
            vercel_project = {
                "name": slug,
                "id": core.get("vercel_core_id") or core.get("vercel_public_id"),
                "domain": core.get("production_domain", ""),
                "link_type": "",
                "framework": "",
            }

        reviews = reviews_for(conn, row["id"], excluded_days=excluded_review_days)
        signals = signals_for(conn, row["id"], excluded_days=excluded_review_days)
        override = override_map.get(slug, {})

        is_website_repo = bool(core.get("local_website_repo"))
        startup_type = infer_type(surface, local, vercel_project, bool(row["is_public"]), is_website_repo)
        stage = infer_stage(surface, local, vercel_project, deployment, row["health"], startup_type)

        dimensions = dimension_scores(
            surface, local, vercel_project, deployment, row["health"], reviews, startup_type
        )
        baseline = baseline_attainment(dimensions, startup_type)
        gap = round(100.0 - baseline, 1)

        mom = momentum(git, signals)
        importance_score = importance(slug, startup_type, core, bool(row["is_public"]), override)
        commercial, commercial_conf, commercial_why = commercial_potential(
            startup_type, surface, core, deployment, stage, reviews, override
        )
        viable, viable_conf, viable_why = viability(
            startup_type, surface, row["health"], deployment, reviews, override
        )
        heat_score = heat(mom["momentum_total"], commercial, importance_score, signals["releases"], bool(row["is_public"]))

        band = _band(baseline)
        cold = gap >= 40 and mom["momentum_7d"] < 15
        ahead = baseline >= 74 and mom["momentum_7d"] >= 25

        open_work = conn.execute(
            "SELECT COUNT(*) AS n FROM work_items WHERE startup_id = ? AND status IN ('queued','blocked','reopened')",
            (row["id"],),
        ).fetchone()["n"]
        blockers = conn.execute(
            "SELECT COUNT(*) AS n FROM work_items WHERE startup_id = ? AND status = 'blocked'",
            (row["id"],),
        ).fetchone()["n"]
        mismatch = bool(
            core.get("vercel_public")
            and vercel_project
            and (vercel_project.get("link_type") is None)
            and bool(row["is_public"])
        )

        attention_score = attention(
            baseline, importance_score, mom["momentum_total"], blockers,
            reviews["failed"], open_work, cold, mismatch, override,
        )

        entries.append(
            {
                "slug": slug,
                "name": row["name"],
                "type": startup_type,
                "stage": stage,
                "stage_expectation": STAGE_EXPECTATION[stage],
                "health": row["health"],
                "baseline_attainment": baseline,
                "baseline_gap": gap,
                "band": band,
                "dimensions": dimensions,
                "weights": WEIGHTS.get(startup_type, {}),
                "momentum": mom,
                "commits": {
                    "24h": git.get("commits_24h", 0),
                    "7d": git.get("commits_7d", 0),
                    "30d": git.get("commits_30d", 0),
                    "authors_30d": git.get("authors_30d", 0),
                },
                "dirty_files": git.get("dirty_files", 0),
                "attention_score": attention_score,
                "importance": importance_score,
                "commercial_potential": commercial,
                "commercial_confidence": commercial_conf,
                "commercial_reason": commercial_why[0] if commercial_why else "",
                "viability": viable,
                "viability_confidence": viable_conf,
                "viability_reason": viable_why[0] if viable_why else "",
                "heat": heat_score,
                "cold": cold,
                "ahead": ahead,
                "reuse_leverage": reuse_map.get(slug),
                "blocked_count": blockers,
                "open_work": open_work,
                "reviews": {
                    "total": reviews["total"],
                    "pass_rate": round(reviews["pass_rate"], 2),
                    "fail_rate": round(reviews["fail_rate"], 2),
                    "batch_excluded": reviews["batch_excluded"],
                },
                "resources": {
                    "local_core_repo": core.get("local_core_repo", ""),
                    "local_website_repo": core.get("local_website_repo", ""),
                    "github": core.get("github_core", ""),
                    "vercel": (vercel_project or {}).get("name", ""),
                    "vercel_project_id": (vercel_project or {}).get("id", ""),
                    "vercel_git_link": (vercel_project or {}).get("link_type") or "none",
                    "production_domain": core.get("production_domain", ""),
                    "duplicate_status": core.get("duplicate_status", "UNKNOWN"),
                },
                "evidence": {
                    "code_files": surface.get("code_files", 0),
                    "code_lines": surface.get("code_lines", 0),
                    "markdown_files": surface.get("markdown", 0),
                    "test_files": surface.get("test_files", 0),
                    "routes": surface.get("routes", 0),
                    "components": surface.get("components", 0),
                    "ci_workflows": surface.get("has_ci", 0),
                    "readme_bytes": surface.get("readme_bytes", 0),
                    "modules": surface.get("modules", 0),
                    "interfaces": structure_signal(surface, startup_type)["interfaces"],
                    "entrypoints": surface.get("entrypoints", [])[:6],
                    "languages": dict(sorted((surface.get("languages") or {}).items(), key=lambda kv: -kv[1])[:3]),
                },
                "one_liner": one_liner(
                    slug, stage, baseline, gap, mom, cold, ahead, blockers, reviews, dimensions
                ),
            }
        )

    payload = {
        "generated_at": now.replace(microsecond=0).isoformat(),
        "day": day,
        "score_model_version": SCORE_MODEL_VERSION,
        "owner_private_label": "OWNER-PRIVATE",
        "counts": _counts(entries),
        "portfolio": _portfolio_rollup(entries),
        "top": _top_lists(entries, conn),
        # Also at the root: the Grokbot and Team read these as report-level
        # facts rather than digging into the hotspot cards.
        "external_research": _external_research_section(conn),
        "name_collisions": _name_collision_section(conn),
        "matrix": _matrix(entries),
        "excluded_review_days": sorted(excluded_review_days),
        "delta": _delta(conn, entries, day),
        "startups": sorted(entries, key=lambda item: -item["attention_score"]),
        "sort_default": "attention_priority",
        "sortable": [
            "alphabetical", "baseline", "momentum", "commercial_upside",
            "viability", "importance", "heat", "reuse_leverage",
        ],
    }
    return payload


def one_liner(slug: str, stage: str, baseline: float, gap: float, mom: dict, cold: bool,
              ahead: bool, blockers: int, reviews: dict, dimensions: dict | None = None) -> str:
    """One sentence. At most three short lines is the hard limit, so one it is."""
    stage_label = stage.replace("_", " ").lower()
    dimensions = dimensions or {}
    if cold:
        return f"{stage_label}; {gap:.0f} points short of full marks for its stage and little recent work."
    if blockers >= 3:
        return f"{stage_label} with {blockers} blocked work items holding progress back."
    if ahead:
        return f"{stage_label}, ahead of stage baseline and still moving."
    if reviews["total"] >= 3 and reviews["pass_rate"] < 0.35:
        return f"{stage_label}; {reviews['fail_rate'] * 100:.0f}% of recorded reviews fail."
    if baseline >= 62:
        return f"{stage_label}, healthy for its stage, {mom['momentum_total']:.0f} momentum."
    if dimensions:
        # Only claim the primary workflow is the gap when it actually is the
        # weakest scored dimension, and only once there is something to improve.
        weakest = min(dimensions, key=lambda key: dimensions[key])
        if weakest == "primary_workflow" and dimensions["primary_workflow"] > 0 and gap >= 30:
            return f"{stage_label}; the primary workflow is the weakest dimension."
        if weakest == "docs" and dimensions["docs"] < 40:
            return f"{stage_label}; the public and docs surface is the weakest dimension."
        if weakest == "engineering" and dimensions["engineering"] < 40:
            return f"{stage_label}; engineering discipline is the weakest dimension."
    if gap >= 30:
        return f"{stage_label}; broad gaps across product, engineering and surface."
    return f"{stage_label}, {baseline:.0f} of its stage baseline with steady work."


def _counts(entries: list[dict]) -> dict:
    return {
        "startups": len(entries),
        "hot": sum(1 for item in entries if item["heat"] >= 55 and item["momentum"]["momentum_total"] >= 30),
        "cold": sum(1 for item in entries if item["cold"]),
        "review": sum(1 for item in entries if item["health"] in {"REVIEW_REQUIRED", "VISUAL_QA_PENDING"}),
        "ready": sum(1 for item in entries if item["health"] == "RELEASE_READY"),
        "blocked": sum(1 for item in entries if item["blocked_count"] > 0),
        "advanced": sum(1 for item in entries if item["ahead"]),
        "with_production_domain": sum(1 for item in entries if item["resources"]["production_domain"]),
        "with_vercel": sum(1 for item in entries if item["resources"]["vercel"]),
        "with_github": sum(1 for item in entries if item["resources"]["github"]),
    }


def _portfolio_rollup(entries: list[dict]) -> dict:
    if not entries:
        return {"baseline_attainment": 0.0}
    total = len(entries)
    return {
        "baseline_attainment": round(sum(item["baseline_attainment"] for item in entries) / total, 1),
        "momentum": round(sum(item["momentum"]["momentum_total"] for item in entries) / total, 1),
        "stage_mix": {
            stage: sum(1 for item in entries if item["stage"] == stage)
            for stage in STAGE_ORDER
        },
        "type_mix": {
            kind: sum(1 for item in entries if item["type"] == kind)
            for kind in sorted({item["type"] for item in entries})
        },
    }


def _top(entries: list[dict], key, count: int = 5) -> list[dict]:
    ordered = sorted(entries, key=lambda item: -(item[key] if item[key] is not None else -1))
    return [
        {
            "slug": item["slug"],
            "value": round(item[key], 1) if item[key] is not None else None,
            "delta": item["momentum"]["momentum_7d"],
            "band": item["band"],
            "one_liner": item["one_liner"],
        }
        for item in ordered[:count]
    ]


def _top_lists(entries: list[dict], conn: sqlite3.Connection | None = None) -> dict:
    """Top lists for the report. External reuse reads the database when given it."""
    conn = conn if conn is not None else _null_conn()
    return {
        "hot": sorted(
            [
                {"slug": item["slug"], "heat": item["heat"], "momentum_7d": item["momentum"]["momentum_7d"],
                 "band": item["band"]}
                for item in entries
            ],
            key=lambda item: -item["heat"],
        )[:5],
        "attention": _top(entries, "attention_score"),
        "commercial": [
            {"slug": item["slug"], "value": item["commercial_potential"],
             "confidence": item["commercial_confidence"], "band": item["band"]}
            for item in sorted(entries, key=lambda i: -i["commercial_potential"])[:5]
        ],
        "viability": [
            {"slug": item["slug"], "value": item["viability"],
             "confidence": item["viability_confidence"], "band": item["band"]}
            for item in sorted(entries, key=lambda i: -i["viability"])[:5]
        ],
        "important": _top(entries, "importance"),
        "reuse": _reuse_section(conn),
        "reuse_note": _reuse_note(conn),
        "competition": _competition_section(conn),
        "name_collisions": _name_collision_section(conn),
        "external_research": _external_research_section(conn),
    }


def _external_research_section(conn: sqlite3.Connection) -> dict:
    """§24. How much of the portfolio has current external research."""
    from portfolio_os import external as ex

    try:
        report = ex.coverage_report(conn)
    except Exception:  # noqa: BLE001 - the daily report must not fail on this
        return {"researched": 0, "stale": 0, "not_researched": 0, "pct_current": 0}
    return {
        "researched": report.get("researched", 0),
        "stale": report.get("stale", 0),
        "not_researched": report.get("not_researched", 0),
        "pct_current": report.get("external_research_current_pct", 0),
        "mean_leverage": report.get("reuse_leverage_mean", 0),
    }


def _matrix(entries: list[dict]) -> dict:
    return {
        "x": "baseline_attainment",
        "y": "momentum_7d",
        "size": "importance",
        "color": "band",
        "points": [
            {
                "slug": item["slug"],
                "x": item["baseline_attainment"],
                "y": item["momentum"]["momentum_7d"],
                "r": round(3 + item["importance"] / 14, 1),
                "band": item["band"],
                "commercial": item["commercial_potential"],
                "attention": item["attention_score"],
            }
            for item in entries
        ],
    }


def _delta(conn: sqlite3.Connection, entries: list[dict], day: str) -> dict:
    """Only material change since the previous snapshot."""
    previous_day = conn.execute(
        "SELECT MAX(day) AS d FROM daily_scores WHERE day < ?", (day,)
    ).fetchone()["d"]
    delta = {
        "compared_to": previous_day,
        "advanced": [],
        "state_transitions": [],
        "recovered": [],
        "reviews_resolved": [],
        "releases": [],
        "blockers": [],
        "portfolio_baseline_change": 0.0,
    }
    if not previous_day:
        return delta

    prior = {
        row["slug"]: row
        for row in conn.execute(
            "SELECT * FROM daily_scores WHERE day = ?", (previous_day,)
        ).fetchall()
    }
    for item in entries:
        before = prior.get(item["slug"])
        if before is None:
            continue
        old_baseline = before["baseline_attainment"] or 0.0
        if item["baseline_attainment"] - old_baseline >= 5:
            delta["advanced"].append(
                {"slug": item["slug"], "from": old_baseline, "to": item["baseline_attainment"]}
            )
        if before["stage"] and before["stage"] != item["stage"]:
            delta["state_transitions"].append(
                {"slug": item["slug"], "from": before["stage"], "to": item["stage"]}
            )
        if (before["blocked"] or 0) > 0 and item["blocked_count"] == 0:
            delta["recovered"].append({"slug": item["slug"], "unblocked": before["blocked"]})
        if (before["blocked"] or 0) == 0 and item["blocked_count"] > 0:
            delta["blockers"].append({"slug": item["slug"], "blocked": item["blocked_count"]})
    now = datetime.now(timezone.utc)
    releases = conn.execute(
        """
        SELECT startups.slug, deployments.commit_sha, deployments.timestamp
        FROM deployments JOIN startups ON startups.id = deployments.startup_id
        WHERE deployments.timestamp > ? AND deployments.status NOT IN ('blocked','held')
          AND startups.owner_private = 0
        ORDER BY deployments.id DESC LIMIT 12
        """,
        ((now - timedelta(hours=24)).isoformat(),),
    ).fetchall()
    delta["releases"] = [dict(row) for row in releases]
    resolved = conn.execute(
        """
        SELECT startups.slug, COUNT(*) AS n
        FROM reviews JOIN startups ON startups.id = reviews.startup_id
        WHERE reviews.resolution = 'pass' AND reviews.timestamp > ? AND startups.owner_private = 0
        GROUP BY startups.slug ORDER BY n DESC LIMIT 12
        """,
        ((now - timedelta(hours=24)).isoformat(),),
    ).fetchall()
    delta["reviews_resolved"] = [dict(row) for row in resolved]
    return delta


def persist_scores(conn: sqlite3.Connection, payload: dict) -> None:
    day = payload["day"]
    conn.execute("DELETE FROM daily_scores WHERE day = ?", (day,))


def history(conn: sqlite3.Connection, slug: str, days: int = 30) -> list[dict]:
    rows = conn.execute(
        """
        SELECT day, baseline_attainment, momentum_total, attention_score, heat, importance
        FROM daily_scores WHERE slug = ? ORDER BY day DESC LIMIT ?
        """,
        (slug, days),
    ).fetchall()
    return list(reversed([dict(row) for row in rows]))


# ------------------------------------------------------------------ markdown


def render_markdown(payload: dict) -> str:
    counts = payload["counts"]
    portfolio = payload["portfolio"]
    top = payload["top"]
    lines = [
        f"NOAERTH · DAILY PORTFOLIO · {payload['day']}",
        "",
        f"PORTFOLIO  {'█' * int(portfolio['baseline_attainment'] / 10)}{'░' * (10 - int(portfolio['baseline_attainment'] / 10))} "
        f"{portfolio['baseline_attainment']}% ▲{portfolio['momentum']}",
        f"🔥 HOT {counts['hot']}   🟥 COLD {counts['cold']}   👁 REVIEW {counts['review']}   "
        f"🚀 READY {counts['ready']}   ⛔ BLOCKED {counts['blocked']}   ▲ ADVANCED {counts['advanced']}",
        f"◎ COVERAGE {counts['with_github']}/{counts['startups']}",
        "",
        "Score model v%d. BASELINE_ATTAINMENT measures a startup against its own maturity stage." % payload["score_model_version"],
        "",
    ]

    lines += ["## 🔥 HOT"]
    for item in top["hot"]:
        lines.append(f"{item['slug']}  heat {item['heat']:.0f}  momentum7d {item['momentum_7d']:.0f}")
    if not top["hot"]:
        lines.append("none")

    lines += ["", "## 🟥 ATTENTION"]
    for item in top["attention"]:
        lines.append(f"{item['slug']}  attention {item['value']:.0f}  {band_icon(item['band'])} {item['band'].replace('_', ' ').lower()}")
    if not top["attention"]:
        lines.append("none")

    lines += ["", "## 💰 COMMERCIAL UPSIDE"]
    for item in top["commercial"]:
        lines.append(f"{item['slug']}  {item['value']:.0f} {item['confidence']}")
    if not top["commercial"]:
        lines.append("none")

    lines += ["", "## ⭐ IMPORTANT"]
    for item in top["important"]:
        lines.append(f"{item['slug']}  {item['value']:.0f}")

    lines += ["", "## ✓ VIABILITY"]
    for item in top["viability"]:
        lines.append(f"{item['slug']}  {item['value']:.0f} {item['confidence']}")

    lines += ["", "## ♻ REUSE OPPORTUNITY"]
    if top["reuse"]:
        for item in top["reuse"]:
            lines.append(
                f"{item['slug']}  {item['reuse_leverage']:.0f} — {item['note']}"
            )
    else:
        lines.append(top["reuse_note"])

    crowded = top.get("competition") or []
    if crowded:
        lines += ["", "## ⚔ CROWDED"]
        for item in crowded:
            bits = []
            if item["direct_competitors"]:
                bits.append(f"{item['direct_competitors']} direct competitors")
            if item["name_collisions"]:
                bits.append(f"{item['name_collisions']} name collision(s)")
            lines.append(f"{item['slug']} — {', '.join(bits)}")

    delta = payload["delta"]
    if delta.get("compared_to"):
        lines += ["", f"## DAILY DELTA since {delta['compared_to']}"]
        if not any(delta[key] for key in ("advanced", "state_transitions", "recovered", "releases", "reviews_resolved", "blockers")):
            lines.append("no material change")
        for item in delta["advanced"][:8]:
            lines.append(f"advanced {item['slug']} {item['from']:.0f} -> {item['to']:.0f}")
        for item in delta["state_transitions"][:8]:
            lines.append(f"stage {item['slug']} {item['from']} -> {item['to']}")
        for item in delta["recovered"][:8]:
            lines.append(f"unblocked {item['slug']}")
        for item in delta["blockers"][:8]:
            lines.append(f"blocked {item['slug']} x{item['blocked']}")
        for item in delta["releases"][:8]:
            lines.append(f"release {item['slug']} {item['commit_sha'][:8]}")
    else:
        lines += ["", "## DAILY DELTA", "first snapshot for this score model version"]

    lines += ["", f"## ALL {payload['counts']['startups']} STARTUPS", ""]
    lines.append("sorted by attention priority")
    lines.append("")
    for item in payload["startups"]:
        icon = band_icon(item["band"])
        flags = []
        if item["cold"]:
            flags.append("🧊")
        if item["ahead"]:
            flags.append("🔥")
        if item["blocked_count"]:
            flags.append("⛔")
        reuse = item.get("reuse_leverage")
        if reuse:
            flags.append(f"♻{reuse:.0f}")
        lines.append(
            f"{icon} {item['slug']}  {item['baseline_attainment']:.0f}% ▲{item['momentum']['momentum_7d']:.0f}  "
            f"🔥{item['heat']:.0f} 💰{item['commercial_potential']:.0f}{item['commercial_confidence'][0]} "
            f"✓{item['viability']:.0f}{item['viability_confidence'][0]} "
            f"{''.join(flags)}  [{item['stage'].lower()}]"
        )
        lines.append(f"    {item['one_liner']}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------- html


def render_html(payload: dict) -> str:
    counts = payload["counts"]
    portfolio = payload["portfolio"]
    top = payload["top"]
    baseline = portfolio["baseline_attainment"]
    filled = int(baseline / 10)

    def chips(items: list[dict], value_key: str, suffix: str = "") -> str:
        if not items:
            return '<p class="empty">none</p>'
        rows = []
        for item in items:
            band = item.get("band", "")
            icon = band_icon(band) if band else ""
            confidence = item.get("confidence")
            conf_html = f'<span class="conf">{confidence[0]}</span>' if confidence else ""
            delta = item.get("momentum_7d")
            delta_html = f' <span class="delta">▲{delta:.0f}</span>' if isinstance(delta, (int, float)) else ""
            label = item.get("slug", "")
            value = item.get(value_key)
            value_html = f"{value:.0f}{suffix}" if isinstance(value, (int, float)) else str(value)
            rows.append(
                f'<li><span class="dot {band.lower()}"></span><span class="slug">{label}</span>'
                f'<span class="val">{value_html}</span>{delta_html}{conf_html}</li>'
            )
        return "<ul class=\"chips\">" + "".join(rows) + "</ul>"

    stage_rows = "".join(
        f'<div class="stagebar"><span class="sname">{stage.replace("_", " ").lower()}</span>'
        f'<span class="strack"><span class="sfill" style="width:{count / max(1, counts["startups"]) * 100:.1f}%"></span></span>'
        f'<span class="scount">{count}</span></div>'
        for stage, count in sorted(portfolio.get("stage_mix", {}).items(), key=lambda kv: -kv[1])
    )

    matrix_points = "".join(
        '<span class="pt {band}" style="left:{x:.2f}%;bottom:{y:.2f}%;width:{d:.1f}px;height:{d:.1f}px" '
        'title="{slug} attainment {x:.0f} momentum7d {y:.0f}"></span>'.format(
            band=point["band"].lower(),
            x=min(98.5, max(1.5, point["x"])),
            y=min(98.5, max(1.5, point["y"])),
            d=point["r"] * 2,
            slug=point["slug"],
        )
        for point in payload["matrix"]["points"]
    )

    row_html = []
    for item in payload["startups"]:
        flags = []
        if item["cold"]:
            flags.append('<span class="flag cold" title="cold">🧊</span>')
        if item["ahead"]:
            flags.append('<span class="flag ahead" title="ahead of stage">▲</span>')
        if item["blocked_count"]:
            flags.append(f'<span class="flag block" title="{item["blocked_count"]} blocked">⛔</span>')
        row_html.append(
            f'<tr class="{item["band"].lower()}">'
            f'<td class="band">{band_icon(item["band"])}<span class="bandtext">{item["band"].replace("_", " ").lower()}</span></td>'
            f'<td class="slug"><a href="/{item["slug"]}">{item["slug"]}</a>'
            f'<span class="stage">{item["stage"].replace("_", " ").lower()}</span></td>'
            f'<td class="num strong">{item["baseline_attainment"]:.0f}</td>'
            f'<td class="num">▲{item["momentum"]["momentum_7d"]:.0f}</td>'
            f'<td class="num">🔥{item["heat"]:.0f}</td>'
            f'<td class="num">💰{item["commercial_potential"]:.0f}'
            f'<span class="conf">{item["commercial_confidence"][0]}</span></td>'
            f'<td class="num">✓{item["viability"]:.0f}<span class="conf">{item["viability_confidence"][0]}</span></td>'
            f'<td class="num">⭐{item["importance"]:.0f}</td>'
            f'<td class="num att">{item["attention_score"]:.0f}</td>'
            f'<td class="why">{"".join(flags)} {item["one_liner"]}</td>'
            "</tr>"
        )

    delta = payload["delta"]
    delta_items = []
    for item in delta.get("advanced", [])[:6]:
        delta_items.append(f'<li>advanced <b>{item["slug"]}</b> {item["from"]:.0f} → {item["to"]:.0f}</li>')
    for item in delta.get("state_transitions", [])[:6]:
        delta_items.append(f'<li>stage <b>{item["slug"]}</b> {item["from"].replace("_", " ").lower()} → {item["to"].replace("_", " ").lower()}</li>')
    for item in delta.get("recovered", [])[:6]:
        delta_items.append(f'<li>unblocked <b>{item["slug"]}</b></li>')
    for item in delta.get("blockers", [])[:6]:
        delta_items.append(f'<li>blocked <b>{item["slug"]}</b> ×{item["blocked"]}</li>')
    for item in delta.get("releases", [])[:6]:
        delta_items.append(f'<li>release <b>{item["slug"]}</b> {(item.get("commit_sha") or "")[:8]}</li>')
    delta_html = (
        f'<ul class="delta-list">{"".join(delta_items)}</ul>'
        if delta_items
        else '<p class="empty">no material change since the previous snapshot</p>'
    )

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NOAERTH · DAILY PORTFOLIO · {payload['day']}</title>
<style>
:root{{--bg:#0b0d10;--panel:#141821;--line:#232a36;--fg:#e8edf5;--dim:#8b97ab;
--crit:#ff5470;--needs:#ff9f43;--base:#ffd93d;--health:#3ddc97;--ahead:#4da3ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);
font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace}}
.wrap{{max-width:1500px;margin:0 auto;padding:18px 20px 60px}}
header{{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;border-bottom:1px solid var(--line);padding-bottom:10px}}
h1{{font-size:15px;letter-spacing:.14em;margin:0}}
.gen{{color:var(--dim);font-size:11px}}
.strip{{display:grid;grid-template-columns:2fr repeat(6,1fr);gap:8px;margin:12px 0}}
.tile{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:9px 11px}}
.tile .k{{color:var(--dim);font-size:10px;letter-spacing:.1em;text-transform:uppercase}}
.tile .v{{font-size:20px;margin-top:3px}}
.bar{{height:7px;background:#1e2430;border-radius:4px;overflow:hidden;margin-top:7px}}
.bar i{{display:block;height:100%;background:linear-gradient(90deg,var(--crit),var(--base),var(--health))}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:12px}}
.card{{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:10px 12px}}
.card h2{{font-size:11px;letter-spacing:.1em;margin:0 0 7px;color:var(--dim);text-transform:uppercase}}
.chips{{list-style:none;margin:0;padding:0}}
.chips li{{display:flex;align-items:center;gap:7px;padding:2px 0;border-bottom:1px solid #1b2029}}
.chips li:last-child{{border:0}}
.dot{{width:8px;height:8px;border-radius:50%;flex:0 0 auto;background:var(--dim)}}
.dot.ahead{{background:var(--ahead)}}.dot.healthy{{background:var(--health)}}
.dot.baseline{{background:var(--base)}}.dot.needs_work{{background:var(--needs)}}
.dot.critical{{background:var(--crit)}}
.chips .slug{{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.chips .val{{color:var(--dim)}}
.delta{{color:var(--health)}}.conf{{color:var(--dim);font-size:10px;margin-left:2px}}
.empty{{color:var(--dim);margin:0;font-size:11px}}
.matrix{{position:relative;height:290px;margin-top:6px;padding:4px 0 16px 14px}}
.matrix .plot{{position:relative;width:100%;height:100%;
border-left:1px solid var(--line);border-bottom:1px solid var(--line)}}
.matrix .axis-x,.matrix .axis-y{{position:absolute;color:var(--dim);font-size:9px;letter-spacing:.08em}}
.matrix .axis-x{{bottom:0;left:50%;transform:translateX(-50%)}}
.matrix .axis-y{{top:50%;left:0;transform:rotate(-90deg) translateX(50%);transform-origin:left top}}
.pt{{position:absolute;border-radius:50%;transform:translate(-50%,50%);opacity:.55}}
.pt.ahead{{background:var(--ahead);box-shadow:0 0 0 1px var(--ahead)}}
.pt.healthy{{background:var(--health);box-shadow:0 0 0 1px var(--health)}}
.pt.baseline{{background:var(--base);box-shadow:0 0 0 1px var(--base)}}
.pt.needs_work{{background:var(--needs);box-shadow:0 0 0 1px var(--needs)}}
.pt.critical{{background:var(--crit);box-shadow:0 0 0 1px var(--crit)}}
.stages{{margin-top:4px}}
.stagebar{{display:flex;align-items:center;gap:6px;padding:1px 0}}
.stagebar .sname{{width:110px;color:var(--dim);font-size:10px}}
.stagebar .strack{{flex:1;height:5px;background:#1e2430;border-radius:3px;overflow:hidden}}
.stagebar .sfill{{display:block;height:100%;background:var(--health)}}
.stagebar .scount{{width:22px;text-align:right;font-size:10px}}
.legend{{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0;font-size:11px;color:var(--dim)}}
.legend b{{font-weight:400}}
table{{width:100%;border-collapse:collapse}}
th{{text-align:left;font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--dim);
border-bottom:1px solid var(--line);padding:6px 5px;position:sticky;top:0;background:var(--bg)}}
td{{padding:4px 5px;border-bottom:1px solid #171c25;vertical-align:top}}
td.num{{text-align:right;white-space:nowrap}}
td.strong{{color:var(--fg);font-weight:600}}
td.att{{color:var(--needs)}}
td.slug a{{color:var(--fg);text-decoration:none}}
td.slug a:hover{{text-decoration:underline}}
.stage{{display:block;color:var(--dim);font-size:10px}}
.bandtext{{display:block;color:var(--dim);font-size:9px;margin-left:2px}}
.band{{width:64px;white-space:nowrap}}
.why{{color:var(--dim);font-size:11px;max-width:420px}}
.flag{{margin-right:3px}}
.delta-list{{list-style:none;margin:0;padding:0;font-size:11px;color:var(--dim)}}
.delta-list li{{padding:1px 0}}
.delta-list b{{color:var(--fg);font-weight:500}}
footer{{margin-top:20px;color:var(--dim);font-size:10px;border-top:1px solid var(--line);padding-top:8px}}
</style></head><body><div class="wrap">
<header>
<h1>NOAERTH · DAILY PORTFOLIO</h1>
<span class="gen">{payload['day']} · score model v{payload['score_model_version']} · generated {payload['generated_at']}</span>
</header>

<section class="strip">
<div class="tile"><div class="k">Portfolio attainment</div>
<div class="v">{baseline:.1f}% <span style="font-size:12px;color:var(--dim)">▲{portfolio['momentum']}</span></div>
<div class="bar"><i style="width:{baseline}%"></i></div></div>
<div class="tile"><div class="k">🔥 Hot</div><div class="v">{counts['hot']}</div></div>
<div class="tile"><div class="k">🟥 Cold</div><div class="v">{counts['cold']}</div></div>
<div class="tile"><div class="k">👁 Review</div><div class="v">{counts['review']}</div></div>
<div class="tile"><div class="k">🚀 Ready</div><div class="v">{counts['ready']}</div></div>
<div class="tile"><div class="k">⛔ Blocked</div><div class="v">{counts['blocked']}</div></div>
<div class="tile"><div class="k">◎ Coverage</div><div class="v">{counts['with_github']}/{counts['startups']}</div></div>
</section>

<div class="grid">
<div class="card"><h2>🔥 Hot · momentum and opportunity</h2>{chips(top['hot'], 'heat')}</div>
<div class="card"><h2>🟥 Attention · needs work most</h2>{chips(top['attention'], 'value')}</div>
<div class="card"><h2>💰 Commercial upside</h2>{chips(top['commercial'], 'value')}</div>
<div class="card"><h2>✓ Viability</h2>{chips(top['viability'], 'value')}</div>
<div class="card"><h2>⭐ Importance</h2>{chips(top['important'], 'value')}</div>
<div class="card"><h2>♻ Reuse leverage</h2><p class="empty">{top['reuse_note']}</p></div>
<div class="card" style="grid-column:span 2">
<h2>Portfolio matrix · attainment × momentum · bubble = importance</h2>
<div class="matrix">
<span class="axis-x">baseline attainment &rarr;</span>
<span class="axis-y">momentum 7d &rarr;</span>
<div class="plot">
{matrix_points}
</div>
</div>
<div class="legend">
<span>🟥 critical</span><span>🟧 needs work</span><span>🟨 at baseline</span>
<span>🟩 healthy</span><span>🟦 ahead of stage</span><span>🧊 cold</span><span>🔥 hot</span>
</div>
</div>
<div class="card"><h2>Stage mix · attainment is relative to stage</h2><div class="stages">{stage_rows}</div></div>
<div class="card"><h2>Daily delta</h2>{delta_html}</div>
</div>

<h2 style="font-size:12px;letter-spacing:.1em;color:var(--dim);margin:18px 0 6px">
ALL {counts['startups']} STARTUPS · default sort attention priority</h2>
<table><thead><tr>
<th>Band</th><th>Startup</th><th>Attain</th><th>7d</th><th>Heat</th>
<th>Upside</th><th>Viable</th><th>Imp</th><th>Attn</th><th>Why</th>
</tr></thead><tbody>
{''.join(row_html)}
</tbody></table>

<footer>
BASELINE_ATTAINMENT is how well a startup satisfies expectations for its current maturity stage.
It is not percentage complete. Heuristic scores carry confidence: L low, M medium, H high.
Model version {payload['score_model_version']} · source of truth is the JSON report · owner-private material is {payload['owner_private_label']}.
</footer>
</div></body></html>
"""


def write_reports(package_root: Path, payload: dict) -> tuple[Path, Path, Path]:
    base = package_root / "reports" / "daily"
    base.mkdir(parents=True, exist_ok=True)
    day = payload["day"]
    json_path = base / f"{day}.json"
    md_path = base / f"{day}.md"
    html_path = base / f"{day}.html"
    json_path.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    html_path.write_text(render_html(payload), encoding="utf-8")
    latest = base / "latest.json"
    latest.write_text(json.dumps(payload, indent=1) + "\n", encoding="utf-8")
    (base / "latest.md").write_text(render_markdown(payload), encoding="utf-8")
    (base / "latest.html").write_text(render_html(payload), encoding="utf-8")
    return json_path, md_path, html_path

def _null_conn() -> sqlite3.Connection:
    """An in-memory connection, so a report can be rendered from a bare payload."""
    return sqlite3.connect(":memory:")


def _reuse_map(conn: sqlite3.Connection) -> dict[str, float]:
    """§23 reuse leverage per slug, straight from the external landscape."""
    rows = conn.execute(
        "SELECT slug, reuse_leverage FROM external_landscape WHERE reuse_leverage IS NOT NULL"
    ).fetchall()
    return {row["slug"]: float(row["reuse_leverage"]) for row in rows}


def _reuse_section(conn: sqlite3.Connection | None, count: int = 5) -> list[dict]:
    """§31. Top reuse opportunities. Maximum a handful: the report stays short."""
    rows = conn.execute(
        "SELECT e.slug, e.reuse_leverage, e.high_fit_oss, e.recommendation"
        " FROM external_landscape e JOIN startups s ON s.id = e.startup_id"
        " WHERE e.reuse_leverage IS NOT NULL AND s.owner_private = 0"
        " ORDER BY e.reuse_leverage DESC LIMIT ?",
        (count,),
    ).fetchall()
    return [
        {
            "slug": row["slug"],
            "reuse_leverage": round(float(row["reuse_leverage"]), 1),
            "high_fit_oss": int(row["high_fit_oss"] or 0),
            "note": _reuse_note_for(row["reuse_leverage"], int(row["high_fit_oss"] or 0)),
            "recommendation": row["recommendation"] or "",
        }
        for row in rows
    ]


def _reuse_note_for(leverage: float | None, high_fit: int) -> str:
    if leverage is None:
        return ""
    if leverage >= 70:
        return f"{high_fit} strong open-source foundations may accelerate this"
    if leverage >= 45:
        return f"{high_fit} reusable components available"
    return "little reusable work found"


def _reuse_note(conn: sqlite3.Connection | None) -> str:
    """§24 coverage, stated plainly when there is nothing yet to show."""
    from portfolio_os.external import coverage_report

    report = coverage_report(conn)
    if not report["researched"]:
        return (
            f"No external research yet for {report['total']} startups. "
            "Run `portfolio landscape --all`."
        )
    return (
        f"{report['researched']}/{report['total']} researched "
        f"({report['external_research_current_pct']}% current); "
        f"{report['stale']} stale, {report['not_researched']} outstanding"
    )


def _name_collision_section(conn: sqlite3.Connection, count: int = 5) -> list[dict]:
    """§19. Startups whose name is already used by a significant external project."""
    rows = conn.execute(
        "SELECT e.slug, e.reuse_leverage, e.recommendation FROM external_landscape e"
        " JOIN startups s ON s.id = e.startup_id"
        " WHERE s.owner_private = 0 AND e.recommendation LIKE 'BRAND_COLLISION%'"
        " ORDER BY e.reuse_leverage DESC LIMIT ?",
        (count,),
    ).fetchall()
    return [
        {"slug": row["slug"], "reuse_leverage": round(float(row["reuse_leverage"] or 0), 1)}
        for row in rows
    ]


def _competition_section(conn: sqlite3.Connection | None, count: int = 5) -> list[dict]:
    """§32. Only surfaced when it is actually useful."""
    rows = conn.execute(
        "SELECT e.slug, e.direct_competitors, e.name_collision"
        " FROM external_landscape e JOIN startups s ON s.id = e.startup_id"
        " WHERE s.owner_private = 0 AND (e.direct_competitors >= 3 OR e.name_collision >= 1)"
        " ORDER BY e.direct_competitors DESC LIMIT ?",
        (count,),
    ).fetchall()
    return [
        {
            "slug": row["slug"],
            "direct_competitors": int(row["direct_competitors"] or 0),
            "name_collisions": int(row["name_collision"] or 0),
        }
        for row in rows
    ]
