"""Venture-studio decisions: what to improve, and how to say it in public."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from portfolio_os.engine import ensure_work, public_text_allowed, utcnow
from portfolio_os.exclusion import ExclusionError, assert_allowed

FLAGSHIPS = frozenset(
    {
        "gh0st",
        "agentos",
        "grokinstall",
        "grokmax",
        "grokbot-office",
        "grokbot-society",
        "q-concierge",
        "seai-mind",
        "paios",
    }
)

PUBLIC_PHASES = {
    "designing": "Rebuilding the public experience.",
    "building": "Building a bounded product improvement.",
    "testing": "Checking the experience before release.",
    "shipped": "A reviewed improvement is ready to release.",
    "researching": "Refining the public research presentation.",
    "experimenting": "An early experiment is in progress.",
}


def compiled_homepage(root: Path) -> Path | None:
    """The page a visitor gets, not an excluded leftover."""
    skip_src_app = False
    tsconfig = root / "tsconfig.json"
    if tsconfig.is_file():
        try:
            data = json.loads(tsconfig.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
        if "src/app" in (data.get("exclude") or []):
            skip_src_app = True
    candidates = (
        "src/app/page.tsx",
        "app/page.tsx",
        "src/app/(marketing)/page.tsx",
        "public-site/index.html",
        "index.html",
    )
    for relative in candidates:
        if skip_src_app and relative.startswith("src/app"):
            continue
        path = root / relative
        if path.is_file():
            return path
    return None


def venture_question(root: Path) -> dict:
    """Highest-value bounded improvement. At most three NOW tasks."""
    page = compiled_homepage(root)
    if page is None:
        return {
            "question": "What is the highest-value bounded improvement right now?",
            "now": ["Add a homepage that states the product."],
            "later": [],
            "reason": "No compiled homepage.",
        }
    text = page.read_text(encoding="utf-8", errors="replace")
    now: list[str] = []
    later: list[str] = []
    if "demo flow" in text.lower() or "stage 1 — demo" in text.lower():
        now.append("Replace placeholder demo-flow copy with the real product sequence.")
    if page.stat().st_size < 2500 and "<svg" not in text and "<table" not in text:
        now.append("Add a product-specific visual of the primary workflow.")
    if not now:
        later.append("Revisit after the next product change. The compiled page already explains a workflow.")
        now.append("Record the senior review. Do not invent a cosmetic edit.")
    return {
        "question": "What is the highest-value bounded improvement right now?",
        "now": now[:3],
        "later": later[:3],
        "reason": "Compiled homepage inspection.",
        "homepage": str(page.name),
    }


def store_venture_review(conn: sqlite3.Connection, startup_id: int, review: dict) -> int:
    cur = conn.execute(
        """
        INSERT INTO venture_reviews (startup_id, question, now_tasks, later, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            startup_id,
            review["question"],
            json.dumps(review["now"]),
            json.dumps(review.get("later") or []),
            utcnow(),
        ),
    )
    for task in review["now"][:3]:
        if task.startswith("Record the senior review"):
            continue
        ensure_work(
            conn,
            startup_id,
            type_="venture_now",
            title=task[:180],
            role="DESIGNER",
            priority=86,
            description=review.get("reason") or "",
        )
    conn.execute(
        "UPDATE work_items SET priority_reason = ? WHERE startup_id = ? AND priority_reason IS NULL AND status != 'completed'",
        (review.get("reason") or "Venture review", startup_id),
    )
    return int(cur.lastrowid)


def record_material(
    conn: sqlite3.Connection,
    startup_id: int,
    *,
    category: str,
    summary: str,
    commit_sha: str = "",
    review_result: str = "",
    work_item_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO material_improvements (
          startup_id, category, work_item_id, summary, commit_sha, review_result, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (startup_id, category, work_item_id, summary, commit_sha, review_result, utcnow()),
    )
    return int(cur.lastrowid)


def material_coverage(conn: sqlite3.Connection) -> dict:
    public = conn.execute(
        "SELECT id, slug FROM startups WHERE owner_private = 0 AND is_public = 1"
    ).fetchall()
    improved = {
        row["startup_id"]
        for row in conn.execute("SELECT DISTINCT startup_id FROM material_improvements")
    }
    reviewed = {
        row["startup_id"]
        for row in conn.execute("SELECT DISTINCT startup_id FROM venture_reviews")
    }
    slugs_improved = [row["slug"] for row in public if row["id"] in improved]
    awaiting = [row["slug"] for row in public if row["id"] not in improved]
    unchanged = [row["slug"] for row in public if row["id"] in reviewed and row["id"] not in improved]
    return {
        "public": len(public),
        "materially_improved": len(slugs_improved),
        "awaiting_improvement": len(awaiting),
        "reviewed_unchanged": len(unchanged),
        "improved_slugs": slugs_improved[:20],
    }


def public_studio_line(name: str, phase: str) -> str | None:
    sentence = PUBLIC_PHASES.get(phase)
    if not sentence or not public_text_allowed(name) or not public_text_allowed(sentence):
        return None
    line = f"{name} — {sentence}"
    if not public_text_allowed(line):
        return None
    return line


def assert_studio_root(path: Path, portfolio_root: Path) -> None:
    try:
        assert_allowed(path, portfolio_root)
    except ExclusionError:
        raise
