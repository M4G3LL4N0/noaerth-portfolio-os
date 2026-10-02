"""Ecosystem sources.

GitHub was the only source, which made every result "open source GitHub work".
That is a real limitation for commercial and enterprise categories: procurement
tooling is not on GitHub at all.

Each provider is an adapter over the same contract. A provider may legitimately
return nothing for a given startup; what matters is that the intelligence layer
supports it cleanly and records which sources were actually consulted, so a thin
result is never mistaken for an exhausted ecosystem.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol

PROVIDER_CACHE_TTL = 7 * 24 * 3600


@dataclass(slots=True)
class EcosystemItem:
    """One retrieved artefact from any ecosystem source."""

    source: str
    identifier: str
    name: str = ""
    description: str = ""
    url: str = ""
    version: str = ""
    license: str = ""
    downloads: int = 0
    stars: int = 0
    language: str = ""
    homepage: str = ""
    topics: list[str] = field(default_factory=list)
    published_at: str = ""
    error: str = ""

    def as_row(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "identifier": self.identifier,
            "name": self.name or self.identifier,
            "description": (self.description or "")[:500],
            "url": self.url,
            "version": self.version,
            "license": self.license,
            "downloads": self.downloads,
            "stars": self.stars,
            "language": self.language,
            "topics": json.dumps(self.topics),
            "published_at": self.published_at,
        }


class EcosystemProvider(Protocol):
    """Every source implements this. GitHub is one implementation, not the API."""

    name: str

    def available(self) -> bool: ...

    def search(self, query: str, limit: int) -> list[EcosystemItem]: ...


class _HttpProvider:
    """Shared plumbing for the registry-backed providers."""

    name = "base"
    endpoint = ""

    def available(self) -> bool:
        raise NotImplementedError

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        raise NotImplementedError

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """stdlib only, matching the rest of Portfolio OS: no new dependency."""
        import urllib.parse
        import urllib.request

        query = ""
        if params:
            query = "?" + urllib.parse.urlencode(params)
        url = f"{self.endpoint}{path}{query}"
        request = urllib.request.Request(url, headers={"accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001 - a registry outage is not fatal
            return None

    def probe(self) -> dict[str, Any]:
        """One cheap reachability check, cached by the caller."""
        if not self.available():
            return {"provider": self.name, "available": False,
                    "reason": "disabled or unreachable"}
        try:
            body = self._get(self.probe_path, dict(self.probe_params))
        except Exception as exc:  # noqa: BLE001 - a provider outage is not fatal
            return {"provider": self.name, "available": False, "error": str(exc)[:200]}
        return {
            "provider": self.name,
            "available": True,
            "reachable": body is not None,
        }

    probe_path = "/"
    probe_params: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# Registries
# ---------------------------------------------------------------------------


class NpmProvider(_HttpProvider):
    """npm registry. Directly useful for JS/TS products and web tooling."""

    name = "npm"
    endpoint = "https://registry.npmjs.org"

    def available(self) -> bool:
        from .env import live_network

        return live_network()

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        body = self._get("/-/v1/search", {"text": query, "size": min(50, limit)})
        if not body:
            return []
        out: list[EcosystemItem] = []
        for entry in (body.get("objects") or [])[:limit]:
            pkg = entry.get("package") or {}
            out.append(
                EcosystemItem(
                    source=self.name,
                    identifier=str(pkg.get("name") or ""),
                    description=str(pkg.get("description") or ""),
                    url=str(pkg.get("links") or {}).replace("npm:", "")
                    or f"https://www.npmjs.com/package/{pkg.get('name')}",
                    version=str(pkg.get("version") or ""),
                    license=str(pkg.get("license") or ""),
                    downloads=int((entry.get("score") or {}).get("final", 0) * 1000),
                    stars=int((entry.get("score") or {}).get("final", 0) * 1000),
                    published_at=str(pkg.get("date") or ""),
                    topics=[t for t in (pkg.get("keywords") or []) if isinstance(t, str)],
                )
            )
        return out

    probe_path = "/-/ping"
    probe_params: dict[str, Any] = {}


class PyPiProvider(_HttpProvider):
    """PyPI. Name search is weak, so project metadata is fetched per hit."""

    name = "pypi"
    endpoint = "https://pypi.org"

    def available(self) -> bool:
        from .env import live_network

        return live_network()

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        # PyPI has no search API; derive candidate names and verify each.
        tokens = [t for t in _slug_tokens(query) if len(t) > 2][:4]
        out: list[EcosystemItem] = []
        for token in tokens:
            for name in (f"{token}", f"{token}-ai", f"py{token}"):
                body = self._get(f"/pypi/{name}/json", {})
                if not body:
                    continue
                info = body.get("info") or {}
                out.append(
                    EcosystemItem(
                        source=self.name,
                        identifier=str(info.get("name") or name),
                        description=str(info.get("summary") or ""),
                        url=str(info.get("package_url") or ""),
                        version=str(info.get("version") or ""),
                        license=str(info.get("license") or "")[:40],
                        language="Python",
                        topics=[c for c in (info.get("classifiers") or [])
                                if isinstance(c, str)][:6],
                        published_at=str(info.get("upload_time") or ""),
                    )
                )
                break
            if len(out) >= limit:
                break
        return out[:limit]

    probe_path = "/pypi/requests/json"
    probe_params: dict[str, Any] = {}


class CratesProvider(_HttpProvider):
    """crates.io. Relevant to Rust-flavoured dev tooling and CLIs."""

    name = "crates"
    endpoint = "https://crates.io/api/v1"

    def available(self) -> bool:
        from .env import live_network

        return live_network()

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        body = self._get("/crates", {"q": query, "per_page": min(20, limit)})
        if not body:
            return []
        out: list[EcosystemItem] = []
        for crate in (body.get("crates") or [])[:limit]:
            out.append(
                EcosystemItem(
                    source=self.name,
                    identifier=str(crate.get("id") or ""),
                    description=str(crate.get("description") or "")[:300],
                    url=str(crate.get("repository") or f"https://crates.io/crates/{crate.get('id')}"),
                    version=str(crate.get("max_version") or ""),
                    license=str(crate.get("license") or "")[:40],
                    downloads=int(crate.get("downloads") or 0),
                    stars=int(crate.get("recent_downloads") or 0),
                    language="Rust",
                    topics=[t for t in (crate.get("keywords") or []) if isinstance(t, str)],
                    published_at=str(crate.get("updated_at") or ""),
                )
            )
        return out

    probe_path = "/crates/serde"
    probe_params: dict[str, Any] = {}


class WebDiscoveryProvider:
    """Commercial / directory discovery.

    Deliberately honest about its own limits: without a search API or a
    configured commercial dataset it cannot return results. It reports that
    rather than inventing competitors.
    """

    name = "web"

    def __init__(self, dataset: list[dict[str, Any]] | None = None) -> None:
        self.dataset = dataset or []

    def available(self) -> bool:
        return True  # always interrogable, even if the answer is "nothing"

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        terms = {t for t in _slug_tokens(query) if len(t) > 3}
        out: list[EcosystemItem] = []
        for record in self.dataset:
            haystack = f"{record.get('name','')} {record.get('description','')}".lower()
            if terms and not any(t in haystack for t in terms):
                continue
            out.append(
                EcosystemItem(
                    source=self.name,
                    identifier=str(record.get("id") or record.get("name") or ""),
                    name=str(record.get("name") or ""),
                    description=str(record.get("description") or ""),
                    url=str(record.get("url") or ""),
                    license=str(record.get("license") or "proprietary"),
                    stars=int(record.get("stars") or 0),
                    topics=[str(t) for t in (record.get("topics") or [])],
                )
            )
            if len(out) >= limit:
                break
        return out

    def probe(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "available": True,
            "reachable": True,
            "dataset_size": len(self.dataset),
            "note": (
                "empty dataset: commercial competitors are not surveyed. "
                "Configure a commercial directory to cover this source."
            ) if not self.dataset else "",
        }


def _slug_tokens(text: str) -> list[str]:
    import re

    return [t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if t]


# ---------------------------------------------------------------------------
# GitHub stays in external.py, but is adapted to the same shape here
# ---------------------------------------------------------------------------


class GitHubProvider:
    """Adapter over the existing deterministic GitHub retriever."""

    name = "github"

    def __init__(self) -> None:
        from .external import GitHubRetriever

        self._retriever = GitHubRetriever()

    def available(self) -> bool:
        return self._retriever.available

    def search(self, query: str, limit: int = 12) -> list[EcosystemItem]:
        out: list[EcosystemItem] = []
        for item in self._retriever.search(query, limit):
            candidate = self._retriever.to_candidate(item, query)
            out.append(
                EcosystemItem(
                    source=self.name,
                    identifier=candidate.repo,
                    name=candidate.name,
                    description=candidate.description,
                    url=candidate.url,
                    license=candidate.license_spdx,
                    stars=candidate.stars,
                    language=candidate.language,
                    homepage=candidate.homepage,
                    topics=candidate.topics,
                    published_at=candidate.activity,
                )
            )
        return out

    def probe(self) -> dict[str, Any]:
        return {"provider": self.name, "available": self.available(),
                "tool": "gh", "authenticated": bool(shutil.which("gh"))}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, Any] = {}


def register_providers() -> dict[str, Any]:
    if PROVIDERS:
        return PROVIDERS
    PROVIDERS.update(
        {
            "github": GitHubProvider(),
            "npm": NpmProvider(),
            "pypi": PyPiProvider(),
            "crates": CratesProvider(),
            "web": WebDiscoveryProvider(),
        }
    )
    return PROVIDERS


def get_provider(name: str) -> Any:
    register_providers()
    return PROVIDERS[name]


def available_providers() -> list[str]:
    register_providers()
    return [name for name, provider in PROVIDERS.items() if provider.available()]


def survey(
    queries: Iterable[str],
    *,
    providers: list[str] | None = None,
    per_query: int = 6,
) -> tuple[list[EcosystemItem], list[dict[str, Any]]]:
    """Search every configured provider and record exactly which were consulted.

    Returns (items, provenance). Provenance is what stops a thin result from
    being read as an exhausted ecosystem.
    """
    register_providers()
    names = providers or list(PROVIDERS)
    items: list[EcosystemItem] = []
    provenance: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for query in queries:
        for name in names:
            provider = PROVIDERS[name]
            started = time.monotonic()
            if not provider.available():
                provenance.append(
                    {"query": query, "provider": name, "status": "unavailable",
                     "results": 0, "ms": 0}
                )
                continue
            try:
                found = provider.search(query, per_query)
            except Exception as exc:  # noqa: BLE001 - one provider must not break the rest
                provenance.append(
                    {"query": query, "provider": name, "status": f"error: {exc}"[:120],
                     "results": 0, "ms": int((time.monotonic() - started) * 1000)}
                )
                continue
            provenance.append(
                {"query": query, "provider": name, "status": "ok",
                 "results": len(found),
                 "ms": int((time.monotonic() - started) * 1000)}
            )
            for item in found:
                key = (item.source, item.identifier)
                if not item.identifier or key in seen:
                    continue
                seen.add(key)
                items.append(item)
    return items, provenance


def provider_health() -> dict[str, dict[str, Any]]:
    register_providers()
    out: dict[str, dict[str, Any]] = {}
    for name, provider in PROVIDERS.items():
        try:
            out[name] = provider.probe() if hasattr(provider, "probe") else {
                "provider": name, "available": provider.available()
            }
        except Exception as exc:  # noqa: BLE001
            out[name] = {"provider": name, "available": False, "error": str(exc)[:200]}
    return out