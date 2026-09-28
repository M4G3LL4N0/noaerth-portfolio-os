"""Ephemeral role execution. Workers edit one startup, then exit."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

from portfolio_os.engine import (
    add_event,
    block_release,
    claim_lock,
    ensure_work,
    recompute_health,
    record_review,
    release_lock,
    startup_by_slug,
    utcnow,
)
from portfolio_os.exclusion import ExclusionError, is_excluded_name
from portfolio_os.workspace import (
    commit_owned,
    dirty_paths,
    git_status,
    mutation_block,
    startup_roots,
)

MAX_REVIEW_ATTEMPTS = 3
SHOT = Path("/Users/matador/startups/.redteam-evidence/shot.mjs")


def _run_row(conn, startup_id, role, work_item_id, summary, evidence, status, files, commits):
    conn.execute(
        """
        INSERT INTO agent_runs (
          work_item_id, role, runner, input_summary, result_summary, status,
          started_at, completed_at, files_changed, commits, evidence
        ) VALUES (?, ?, 'portfolio-os', '', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            work_item_id,
            role,
            summary,
            status,
            utcnow(),
            utcnow(),
            json.dumps(files),
            commits or "",
            json.dumps(evidence),
        ),
    )


def _readme_excerpt(root: Path) -> str:
    for name in ("README.md", "readme.md"):
        path = root / name
        if path.is_file():
            return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[:40])
    return ""


def _homepage(root: Path) -> Path | None:
    for relative in ("src/app/page.tsx", "app/page.tsx", "src/app/(marketing)/page.tsx", "public-site/index.html", "index.html"):
        path = root / relative
        if path.is_file():
            return path
    return None


def founder_brief(root: Path) -> dict:
    page = _homepage(root)
    text = page.read_text(encoding="utf-8", errors="replace") if page else ""
    heading = ""
    match = re.search(r"<h1[^>]*>(.*?)</h1>", text, re.S)
    if match:
        heading = re.sub(r"<[^>]+>", " ", match.group(1))
        heading = re.sub(r"\s+", " ", heading).strip()
    readme = _readme_excerpt(root)
    return {
        "outcome": "brief",
        "user": "Not invented. Taken only from files that exist.",
        "problem": heading or "No homepage heading found.",
        "product": readme.splitlines()[0][:180] if readme else root.name,
        "differentiator": heading,
        "maturity": "Prototype" if re.search(r"prototype|demo|concept", text + readme, re.I) else "Unlabeled",
        "cta": "See the homepage heading.",
        "internal": "Do not publish local paths, tokens, or owner-private material.",
        "files_inspected": [str(page.relative_to(root)) if page else "", "README.md"],
    }


def historical_compare(root: Path) -> dict:
    page = _homepage(root)
    if page is None or not (root / ".git").exists():
        return {"richer_history": False, "reason": "no homepage or git"}
    rel = str(page.relative_to(root))
    log = subprocess.run(
        ["git", "-C", str(root), "log", "-5", "--pretty=format:%h", "--", rel],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    commits = [line for line in log.stdout.splitlines() if line.strip()]
    current = page.stat().st_size
    richer = False
    if len(commits) > 1:
        older = subprocess.run(
            ["git", "-C", str(root), "show", f"{commits[-1]}:{rel}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        if older.returncode == 0 and len(older.stdout) > current * 1.5:
            richer = True
    skeleton = False
    public = root / "public-site" / "index.html"
    if public.is_file() and public.stat().st_size < 2500 and current > public.stat().st_size * 1.5:
        skeleton = True
    return {
        "richer_history": richer,
        "skeleton_public_site": skeleton,
        "homepage": rel,
        "homepage_bytes": current,
        "commits_seen": commits[:5],
    }


def judge_render(metrics: dict | None, text: str) -> tuple[str, str]:
    if not metrics:
        return "fail", "Render did not return page metrics."
    width = int(metrics.get("w") or 0)
    scroll = int(metrics.get("s") or 0)
    if width and scroll > width + 8:
        return "fail", f"Content is wider than the viewport ({scroll} > {width})."
    sample = (metrics.get("t") or text or "").strip()
    if len(sample) < 80:
        return "fail", "Rendered text is too small to explain the product."
    if "CANONICAL SITE IS THE APP HOMEPAGE" in sample:
        return "fail", "The skeleton page rendered instead of the product homepage."
    return "pass", "Rendered text fits the viewport and states the product."


def render_url(url: str, dest: Path, width: int, height: int) -> dict | None:
    if not SHOT.is_file():
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        result = subprocess.run(
            ["node", str(SHOT), url, str(dest), str(width), str(height)],
            check=False,
            capture_output=True,
            text=True,
            timeout=50,
        )
        line = (result.stdout or "").strip().splitlines()
        if result.returncode == 0 and line:
            try:
                return json.loads(line[-1])
            except json.JSONDecodeError:
                pass
        time.sleep(1)
    return None


def _free_port(seed: int) -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1] or seed)


def preview(root: Path, slug: str) -> tuple[subprocess.Popen | None, str | None]:
    public = root / "public-site" / "index.html"
    page = _homepage(root)
    if page and page.suffix == ".html" and "public-site" not in str(page):
        return None, page.as_uri()
    if (root / "package.json").is_file() and (root / "node_modules").is_dir() and page and page.suffix == ".tsx":
        port = _free_port(4300)
        proc = subprocess.Popen(
            ["pnpm", "exec", "next", "dev", "--port", str(port)],
            cwd=root,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        deadline = time.time() + 60
        import urllib.request

        url = f"http://127.0.0.1:{port}/"
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as response:
                    body = response.read(4000).decode("utf-8", "replace")
                    if response.status < 500 and ("<h1" in body.lower() or len(body) > 800):
                        time.sleep(2)
                        return proc, url
            except Exception:
                time.sleep(0.5)
        _stop(proc)
        return None, None
    if public.is_file():
        return None, public.as_uri()
    return None, None


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    import os
    import signal

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.kill()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def provider_blocked(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        """
        SELECT blocker FROM deployments
        WHERE status = 'blocked' AND blocker LIKE '%daily%'
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    return row["blocker"] if row else None


def execute_startup(
    conn: sqlite3.Connection,
    portfolio_root: Path,
    slug: str,
    evidence_root: Path,
    render: bool = True,
) -> dict:
    if is_excluded_name(slug):
        raise ExclusionError("excluded startup")
    startup = startup_by_slug(conn, slug)
    if startup is None or startup["owner_private"]:
        raise ExclusionError("excluded startup")
    if startup["paused"]:
        return {"outcome": "paused"}
    roots = startup_roots(portfolio_root, slug)
    root = roots[0]
    if not root.is_dir():
        return {"outcome": "missing_repo"}
    run_id = uuid.uuid4().hex[:12]
    work_id = ensure_work(
        conn,
        startup["id"],
        type_="pipeline",
        title="Advance the next quality gate",
        role="PORTFOLIO_DIRECTOR",
    )
    claim_lock(conn, startup["id"], f"worker:{run_id}", minutes=30, work_item_id=work_id, run_id=run_id)
    changed: list[str] = []
    try:
        brief = founder_brief(root)
        _run_row(conn, startup["id"], "FOUNDER", work_id, brief["problem"][:180], brief, "completed", brief["files_inspected"], "")
        history = historical_compare(root)
        block = mutation_block(root, slug)
        engineer = {"outcome": "inspected", "mutation": block or "allowed", "history": history}
        if (root / "package.json").is_file():
            engineer["package"] = True
        _run_row(conn, startup["id"], "SOFTWARE_ENGINEER", work_id, "Inspected the repo without adding features.", engineer, "completed", [], "")
        evidence_dir = evidence_root / slug
        proc, url = preview(root, slug) if render else (None, None)
        desktop = render_url(url, evidence_dir / "desktop.png", 1440, 900) if url else None
        mobile = render_url(url, evidence_dir / "mobile.png", 390, 844) if url else None
        _stop(proc)
        desktop_item = ensure_work(conn, startup["id"], type_="visual_qa_desktop", title="Desktop visual review", role="VISUAL_REVIEWER")
        mobile_item = ensure_work(conn, startup["id"], type_="visual_qa_mobile", title="Mobile visual review", role="VISUAL_REVIEWER")
        attempts = conn.execute(
            """
            SELECT COUNT(*) AS n FROM reviews
            WHERE startup_id = ? AND reviewer_role = 'VISUAL_REVIEWER' AND resolution = 'fail'
            """,
            (startup["id"],),
        ).fetchone()["n"]
        d_resolution, d_finding = judge_render(desktop, "")
        m_resolution, m_finding = judge_render(mobile, "")
        if attempts >= MAX_REVIEW_ATTEMPTS:
            ensure_work(
                conn,
                startup["id"],
                type_="senior_review",
                title="Senior review required after repeated visual failures",
                role="PORTFOLIO_DIRECTOR",
                priority=95,
            )
        record_review(
            conn,
            slug=slug,
            role="VISUAL_REVIEWER",
            dimension="desktop",
            finding=d_finding,
            severity="high" if d_resolution == "fail" else "low",
            resolution=d_resolution,
            work_item_id=desktop_item,
        )
        record_review(
            conn,
            slug=slug,
            role="VISUAL_REVIEWER",
            dimension="mobile",
            finding=m_finding,
            severity="high" if m_resolution == "fail" else "low",
            resolution=m_resolution,
            work_item_id=mobile_item,
        )
        if d_resolution == "pass" and m_resolution == "pass":
            conn.execute(
                """
                UPDATE work_items
                SET status = 'completed', completed_at = ?
                WHERE startup_id = ? AND type = 'follow_up'
                  AND title LIKE '%did not return page metrics%'
                  AND status != 'completed'
                """,
                (utcnow(), startup["id"]),
            )
            conn.execute(
                "UPDATE work_items SET status = 'completed', completed_at = ? WHERE id = ?",
                (utcnow(), work_id),
            )
        _run_row(
            conn,
            startup["id"],
            "VISUAL_REVIEWER",
            work_id,
            f"desktop {d_resolution}; mobile {m_resolution}",
            {"desktop": desktop, "mobile": mobile, "url": url, "run_id": run_id},
            "completed",
            [],
            "",
        )
        product_item = ensure_work(conn, startup["id"], type_="product_review", title="Product clarity review", role="PRODUCT_REVIEWER")
        product_ok = bool(brief["problem"] and brief["problem"] != "No homepage heading found.")
        record_review(
            conn,
            slug=slug,
            role="PRODUCT_REVIEWER",
            dimension="clarity",
            finding="Homepage states what the product is." if product_ok else "Homepage does not state the product.",
            severity="low" if product_ok else "high",
            resolution="pass" if product_ok else "fail",
            work_item_id=product_item,
        )
        build = {"ran": False}
        if (root / "package.json").is_file() and (root / "node_modules").is_dir() and d_resolution == "pass":
            built = subprocess.run(
                ["pnpm", "exec", "tsc", "--noEmit", "--pretty", "false"],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            build = {"ran": True, "exit": built.returncode}
        _run_row(conn, startup["id"], "QA_ENGINEER", work_id, json.dumps(build), build, "completed", [], "")
        sha = None
        if block is None and changed:
            dirty = dirty_paths(git_status(root))
            sha = commit_owned(root, [root / rel for rel in changed], "Apply the scoped portfolio-os fix.", dirty)
        cap = provider_blocked(conn)
        if cap and d_resolution == "pass" and m_resolution == "pass" and (not build["ran"] or build["exit"] == 0):
            block_release(conn, slug, cap)
        elif cap:
            add_event(
                conn,
                actor="RELEASE_ENGINEER",
                event_type="deployment_held",
                summary="Deployment stays held. Design and review work continued.",
                visibility="TEAM",
                startup_id=startup["id"],
            )
        if build.get("ran") and build.get("exit") not in (0, None):
            conn.execute("UPDATE startups SET health = 'BUILD_FAILING' WHERE id = ?", (startup["id"],))
            ensure_work(
                conn,
                startup["id"],
                type_="fix_build",
                title="Fix the failing typecheck",
                role="SOFTWARE_ENGINEER",
                priority=90,
            )
            health = "BUILD_FAILING"
        else:
            health = recompute_health(conn, startup["id"])
        return {"outcome": health, "run_id": run_id, "desktop": d_resolution, "mobile": m_resolution, "commit": sha}
    finally:
        release_lock(conn, startup["id"], f"worker:{run_id}")


def next_slugs(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT startups.slug
        FROM work_items JOIN startups ON startups.id = work_items.startup_id
        WHERE work_items.status = 'queued'
          AND startups.owner_private = 0
          AND startups.paused = 0
          AND startups.slug != 'portfolio-control'
          AND work_items.type IN ('visual_qa_desktop', 'visual_qa_mobile', 'pipeline', 'design_review', 'historical_review')
        GROUP BY startups.slug
        ORDER BY MAX(work_items.priority) DESC, startups.slug
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["slug"] for row in rows]


def execute_batch(conn: sqlite3.Connection, portfolio_root: Path, limit: int, evidence_root: Path) -> list[dict]:
    results = []
    for slug in next_slugs(conn, limit):
        results.append({"slug": slug, **execute_startup(conn, portfolio_root, slug, evidence_root)})
        conn.commit()
    return results
