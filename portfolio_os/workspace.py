"""Filesystem scope and git safety for one startup."""

from __future__ import annotations

import subprocess
from pathlib import Path

from portfolio_os.exclusion import ExclusionError, assert_allowed, is_excluded_name

INSPECT_ONLY_SLUGS = frozenset({"spouwse"})


class ScopeError(RuntimeError):
    pass


def startup_roots(portfolio_root: Path, slug: str) -> list[Path]:
    if is_excluded_name(slug) or slug == "OWNER-PRIVATE":
        raise ExclusionError("excluded startup")
    primary = (portfolio_root / slug).resolve()
    assert_allowed(primary, portfolio_root)
    roots = [primary]
    paired = (portfolio_root / f"{slug}-website").resolve()
    if paired.is_dir():
        assert_allowed(paired, portfolio_root)
        roots.append(paired)
    return roots


def assert_in_scope(path: Path, roots: list[Path]) -> None:
    candidate = path.resolve()
    if is_excluded_name(candidate.name) or any(is_excluded_name(part) for part in candidate.parts):
        raise ExclusionError("excluded path")
    for root in roots:
        try:
            candidate.relative_to(root)
            return
        except ValueError:
            continue
    raise ScopeError(f"path outside startup scope: {candidate.name}")


def git_status(repo: Path) -> list[str]:
    if not (repo / ".git").exists():
        return []
    result = subprocess.run(
        ["git", "-C", str(repo), "status", "--short"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def dirty_paths(status_lines: list[str]) -> set[str]:
    paths = set()
    for line in status_lines:
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.add(path)
    return paths


def mutation_block(repo: Path, slug: str) -> str | None:
    if slug in INSPECT_ONLY_SLUGS:
        return "spouwse has an unrelated index and is inspect-only"
    status = git_status(repo)
    staged = [line for line in status if line[:1] in {"M", "A", "D", "R"} and line[1:2] != " "]
    # Short status: first column staged, second unstaged. Staged is XY where X != space and X != ?.
    staged = [line for line in status if line and line[0] not in {" ", "?"} and not line.startswith("??")]
    if len(staged) > 3:
        return "unrelated staged changes; refusing to commit or overwrite"
    return None


def commit_owned(repo: Path, paths: list[Path], message: str, dirty_before: set[str]) -> str | None:
    if not paths:
        return None
    relative = []
    for path in paths:
        assert_in_scope(path, [repo])
        rel = str(path.resolve().relative_to(repo.resolve()))
        if rel in dirty_before:
            raise ScopeError(f"refusing to commit a file that was already dirty: {rel}")
        relative.append(rel)
    subprocess.run(["git", "-C", str(repo), "add", "--", *relative], check=True, timeout=30)
    result = subprocess.run(
        ["git", "-C", str(repo), "commit", "--only", "-m", message, "--", *relative],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return sha.stdout.strip()
