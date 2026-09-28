"""Fail-closed exclusion. Downstream code never receives the private directory."""

from __future__ import annotations

from pathlib import Path

EXCLUDED_DIR_NAMES = frozenset({"openlegal-data"})
OWNER_PRIVATE_LABEL = "OWNER-PRIVATE"


class ExclusionError(RuntimeError):
    pass


def is_excluded_name(name: str) -> bool:
    return name in EXCLUDED_DIR_NAMES


def assert_allowed(path: Path, root: Path) -> None:
    """Refuse any path whose first relative segment is excluded.

    This runs before a directory is listed, read, or handed to an agent.
    """
    root_resolved = root.resolve()
    candidate = path.resolve()
    try:
        relative = candidate.relative_to(root_resolved)
    except ValueError as exc:
        raise ExclusionError("path is outside the portfolio root") from exc
    if relative.parts and is_excluded_name(relative.parts[0]):
        raise ExclusionError("excluded directory")


def iter_top_level(root: Path):
    """Yield top-level directories, never entering the excluded one."""
    root = root.resolve()
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if is_excluded_name(child.name):
            continue
        assert_allowed(child, root)
        yield child
