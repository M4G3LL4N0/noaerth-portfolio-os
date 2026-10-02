"""Named specialists for external intelligence.

The Scout is a thin dispatcher, not a crawler. Deterministic retrieval happens
first in `external.py`; this module only reasons over an already-shortlisted set,
and only when reasoning is worth its cost.

Both roles follow the `squads.py` contract: a role id, a system prompt, and a
callable that dispatches to the reasoning model. If the model is unavailable,
every entry point degrades to a deterministic no-op rather than failing the run.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from .squads import REASONING_MODEL

#: The Scout's reasoning model. §3.
SCOUT_MODEL = REASONING_MODEL
#: The role that adapts external work once it has been selected. §16.
INTEGRATION_MODEL = REASONING_MODEL

#: Only shortlists at least this interesting are worth a reasoning call (§44).
SCOUT_MIN_INTERESTING = 2

ECOSYSTEM_SCOUT_PROMPT = (
    "You are ECOSYSTEM_SCOUT. You are given a startup summary, its primary "
    "workflow, and a SHORTLIST of already-retrieved open-source projects. "
    "Do not search. Do not invent projects. For each shortlisted project answer: "
    "how relevant is this, what overlaps with the intended implementation, what "
    "could we reuse, what must stay custom, and what risk exists. "
    "Prefer concrete component-level answers over generalities. "
    "Reply as JSON {\"repos\":{\"owner/name\":{\"relevance\":str,"
    "\"overlap\":str,\"reuse\":str,\"keep_custom\":str,\"risk\":str}}}."
)

INTEGRATION_ENGINEER_PROMPT = (
    "You are INTEGRATION_ENGINEER. You are given a selected external project and "
    "the target startup. Determine: which files or subsystems to isolate, how to "
    "adapt them, what must remain different so the startup keeps its "
    "differentiation, what attribution is required, and what test proves the "
    "integration. Do not propose importing the whole repository. "
    "Reply as JSON {\"isolate\":[str],\"adapt\":str,\"differentiate\":str,"
    "\"attribution\":str,\"test\":str}."
)

REVIEW_WORK_ITEM_TYPE = "external_reuse_review"
INTEGRATION_WORK_ITEM_TYPE = "integration_work"


class ScoutUnavailable(RuntimeError):
    pass


def _model_endpoint() -> str | None:
    return os.getenv("TRILLIONX_SCOUT_CMD") or None


class EcosystemScout:
    """§3. Repository, competitor, framework and research reasoning.

    Deterministic retrieval is `external.GitHubRetriever`. This class receives
    candidates that retrieval already found and filtered.
    """

    role = "ecosystem_scout"
    model = SCOUT_MODEL
    system_prompt = ECOSYSTEM_SCOUT_PROMPT

    def __init__(self, conn: Any = None, enabled: bool = True) -> None:
        self.conn = conn
        self.enabled = enabled
        self.calls = 0
        self.skipped = 0

    # --- capability --------------------------------------------------------
    def available(self) -> bool:
        if not self.enabled:
            return False
        return _model_endpoint() is not None or shutil.which("opencode") is not None

    # --- review ------------------------------------------------------------
    def review(self, plan: Any, candidates: list[Any]) -> dict[str, str]:
        """Return `{repo: note}` for shortlisted candidates, or {} to skip.

        Skips silently when the shortlist is thin or the model is unreachable:
        the deterministic classification already stands on its own.
        """
        if not self.enabled:
            return {}
        interesting = [
            c for c in candidates
            if getattr(c, "reuse_fit", 0) >= 45 and not getattr(c, "filtered", False)
        ]
        if len(interesting) < SCOUT_MIN_INTERESTING or not self.available():
            self.skipped += 1
            return {}
        payload = {
            "startup": {
                "slug": getattr(plan, "slug", ""),
                "summary": getattr(plan, "one_liner", ""),
                "workflow": getattr(plan, "primary_workflow", ""),
                "user": getattr(plan, "target_user", ""),
                "technology": getattr(plan, "core_technology", ""),
            },
            "candidates": [
                {
                    "repo": c.repo,
                    "description": (c.description or "")[:300],
                    "stars": c.stars,
                    "license": c.license_spdx or "none",
                    "language": c.language,
                    "topics": c.topics[:6],
                    "deterministic_fit": c.reuse_fit,
                    "classification": c.classification,
                    "readme_excerpt": self._readme_excerpt(c.repo)[:1200],
                }
                for c in interesting
            ],
        }
        raw = self._dispatch(json.dumps(payload))
        if not raw:
            self.skipped += 1
            return {}
        self.calls += 1
        return self._parse(raw)

    def _readme_excerpt(self, repo: str) -> str:
        """One bounded README fetch, cached by the caller. Never a crawl."""
        if not repo or "/" not in repo or not shutil.which("gh"):
            return ""
        try:
            proc = subprocess.run(
                ["gh", "api", f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"],
                capture_output=True, text=True, timeout=15, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def _dispatch(self, prompt: str) -> str:
        endpoint = _model_endpoint()
        if not endpoint:
            return ""
        try:
            proc = subprocess.run(
                endpoint, shell=True, input=prompt, capture_output=True, text=True,
                timeout=120, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    @staticmethod
    def _parse(raw: str) -> dict[str, str]:
        try:
            body = json.loads(raw)
        except ValueError:
            start, end = raw.find("{"), raw.rfind("}")
            if start == -1 or end <= start:
                return {}
            try:
                body = json.loads(raw[start:end + 1])
            except ValueError:
                return {}
        repos = body.get("repos") if isinstance(body, dict) else None
        if not isinstance(repos, dict):
            return {}
        notes: dict[str, str] = {}
        for repo, value in repos.items():
            if not isinstance(value, dict):
                continue
            parts = [
                f"relevance: {value.get('relevance', '')}".strip(),
                f"overlap: {value.get('overlap', '')}".strip(),
                f"reuse: {value.get('reuse', '')}".strip(),
                f"keep custom: {value.get('keep_custom', '')}".strip(),
                f"risk: {value.get('risk', '')}".strip(),
            ]
            notes[str(repo)] = " | ".join(p for p in parts if p.split(": ", 1)[-1])
        return notes

    # --- work items --------------------------------------------------------
    def enqueue_review(
        self, conn: Any, startup_id: int, slug: str, leverage: float, summary: str
    ) -> int | None:
        """§15. High-fit reuse creates EXTERNAL_REUSE_REVIEW work."""
        if leverage < 70:
            return None
        cur = conn.execute(
            "INSERT INTO work_items (startup_id, type, title, description, priority,"
            " status, created_at) VALUES (?,?,?,?,?,'queued',?)",
            (
                startup_id, REVIEW_WORK_ITEM_TYPE,
                f"External reuse review: {slug}",
                f"Reuse leverage {leverage}. {summary}".strip(),
                min(100, int(leverage)), _now_iso(),
            ),
        )
        return cur.lastrowid

    def stats(self) -> dict[str, Any]:
        return {"model": self.model, "calls": self.calls, "skipped": self.skipped,
                "available": self.available()}


class IntegrationEngineer:
    """§16. Used only when external work has already been selected."""

    role = "integration_engineer"
    model = INTEGRATION_MODEL
    system_prompt = INTEGRATION_ENGINEER_PROMPT
    work_item_type = INTEGRATION_WORK_ITEM_TYPE

    #: Owns the differentiation, so these paths are never agent-writable.
    PROTECTED_PATHS = (
        "safety", "payments", "auth", "spend", "budget", "guard", "approval",
        "authz", "secrets", "credential",
    )

    def __init__(self, conn: Any = None, enabled: bool = True) -> None:
        self.conn = conn
        self.enabled = enabled
        self.calls = 0

    def available(self) -> bool:
        return self.enabled and (
            _model_endpoint() is not None or shutil.which("opencode") is not None
        )

    def plan(self, candidate: dict[str, Any], startup: dict[str, Any]) -> dict[str, Any]:
        """Produce an integration plan, or a deterministic fallback."""
        fallback = self._fallback_plan(candidate, startup)
        if not self.available():
            return {**fallback, "source": "deterministic_fallback"}
        payload = json.dumps({"candidate": candidate, "startup": startup})
        raw = self._dispatch(payload)
        if not raw:
            return {**fallback, "source": "deterministic_fallback"}
        self.calls += 1
        try:
            body = json.loads(raw)
        except ValueError:
            return {**fallback, "source": "deterministic_fallback"}
        if not isinstance(body, dict):
            return {**fallback, "source": "deterministic_fallback"}
        return {**fallback, **body, "source": "integration_engineer"}

    def _fallback_plan(self, candidate: dict[str, Any], startup: dict[str, Any]) -> dict[str, Any]:
        license_class = candidate.get("license_class", "UNKNOWN")
        attribution = candidate.get("attribution") or (
            "Unresolved license: do not integrate until confirmed."
            if license_class == "UNKNOWN"
            else f"Preserve {candidate.get('license_spdx') or 'the upstream license'}."
        )
        return {
            "isolate": _isolation_hint(candidate),
            "adapt": (
                f"Wrap {candidate.get('name') or candidate.get('repo')} behind an internal "
                f"interface owned by {startup.get('slug', 'this startup')}; depend on the "
                "interface, not the upstream package."
            ),
            "differentiate": (
                f"Keep the {startup.get('primary_workflow') or 'primary workflow'} and the "
                "public product surface local. Upstream supplies infrastructure only."
            ),
            "attribution": attribution,
            "test": (
                "Contract test against the wrapper interface plus one end-to-end test of the "
                "differentiated workflow."
            ),
        }

    def guards(self, paths: list[str]) -> list[str]:
        """Never let an integration agent write safety, money or auth code."""
        return [
            path for path in paths
            if any(marker in str(path).lower() for marker in self.PROTECTED_PATHS)
        ]

    def enqueue_work(
        self, conn: Any, startup_id: int, slug: str, repo: str, plan: dict[str, Any]
    ) -> int:
        """§15. Only created after a review approved the selection."""
        cur = conn.execute(
            "INSERT INTO work_items (startup_id, type, title, description, priority,"
            " status, created_at) VALUES (?,?,?,?,?, 'queued', ?)",
            (
                startup_id, self.work_item_type,
                f"Integrate {repo} into {slug}",
                json.dumps(plan)[:1800],
                80, _now_iso(),
            ),
        )
        return cur.lastrowid

    def _dispatch(self, prompt: str) -> str:
        endpoint = _model_endpoint()
        if not endpoint:
            return ""
        try:
            proc = subprocess.run(
                endpoint, shell=True, input=prompt, capture_output=True, text=True,
                timeout=180, check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def stats(self) -> dict[str, Any]:
        return {"model": self.model, "calls": self.calls, "available": self.available()}


def _isolation_hint(candidate: dict[str, Any]) -> list[str]:
    language = (candidate.get("language") or "").lower()
    if language in {"typescript", "javascript"}:
        return ["src/adapters/<vendor>/", "src/adapters/index.ts"]
    if language == "python":
        return ["opportunityos/adapters/<vendor>/"]
    return ["vendor-adapter/ (isolate behind an internal interface)"]


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")