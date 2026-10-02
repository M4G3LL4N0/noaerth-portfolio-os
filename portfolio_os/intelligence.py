"""Daily portfolio intelligence.

Everything here is computed from evidence already in the control plane and on
disk. No model is called to produce a number. A heuristic score always ships
with a confidence so nobody mistakes it for a measurement.

`BASELINE_ATTAINMENT` answers one question: how well does this startup satisfy
the expectations of the maturity stage it is actually in. A concept is never
penalised for lacking mature SaaS infrastructure.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCORE_MODEL_VERSION = 1

STAGES = (
    "CONCEPT",
    "STATIC_DEMO",
    "INTERACTIVE_DEMO",
    "PARTIAL_WORKFLOW",
    "WORKING_PRIMARY_WORKFLOW",
    "EARLY_PRODUCT",
    "MATURE_PRODUCT",
)
STAGE_ORDER = {name: index for index, name in enumerate(STAGES)}

STARTUP_TYPES = ("CLI_TOOL", "B2B_SAAS", "CONSUMER_BRAND", "INFRASTRUCTURE", "INTERNAL_TOOL", "MEDIA")

DIMENSIONS = (
    "product",
    "primary_workflow",
    "engineering",
    "ux",
    "design",
    "website",
    "docs",
    "public_alignment",
    "operability",
)

WEIGHTS: dict[str, dict[str, int]] = {
    "CLI_TOOL": {
        "product": 14, "primary_workflow": 18, "engineering": 22, "ux": 6, "design": 4,
        "website": 8, "docs": 16, "public_alignment": 4, "operability": 8,
    },
    "B2B_SAAS": {
        "product": 18, "primary_workflow": 20, "engineering": 14, "ux": 12, "design": 10,
        "website": 6, "docs": 6, "public_alignment": 6, "operability": 8,
    },
    "CONSUMER_BRAND": {
        "product": 20, "primary_workflow": 10, "engineering": 8, "ux": 18, "design": 18,
        "website": 14, "docs": 4, "public_alignment": 4, "operability": 4,
    },
    "INFRASTRUCTURE": {
        "product": 12, "primary_workflow": 16, "engineering": 26, "ux": 4, "design": 2,
        "website": 4, "docs": 16, "public_alignment": 4, "operability": 16,
    },
    "INTERNAL_TOOL": {
        "product": 14, "primary_workflow": 20, "engineering": 22, "ux": 8, "design": 4,
        "website": 4, "docs": 12, "public_alignment": 2, "operability": 14,
    },
    "MEDIA": {
        "product": 16, "primary_workflow": 8, "engineering": 8, "ux": 16, "design": 20,
        "website": 16, "docs": 6, "public_alignment": 6, "operability": 4,
    },
}

# What a stage is expected to have. Used to keep stage inference honest and to
# explain the gap in one sentence.
STAGE_EXPECTATION: dict[str, str] = {
    "CONCEPT": "a legible idea, a written problem, and a named audience",
    "STATIC_DEMO": "a public page that explains the offer without interaction",
    "INTERACTIVE_DEMO": "a surface a visitor can actually click through",
    "PARTIAL_WORKFLOW": "one real workflow start to finish, with rough edges",
    "WORKING_PRIMARY_WORKFLOW": "the primary workflow completes reliably end to end",
    "EARLY_PRODUCT": "repeatable use, real data, and a release that others depend on",
    "MATURE_PRODUCT": "measured retention, support load, and durable operations",
}

CODE_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".rs", ".go", ".svelte", ".vue", ".astro"})
SKIP_DIRS = frozenset({
    ".git", "node_modules", "dist", "build", ".next", "coverage", "vendor",
    ".venv", "venv", "__pycache__", ".trillionx-agent-fabric", "worktrees",
    ".turbo", ".cache", "target", "out",
})

FLAGSHIP_SLUGS = frozenset({"noaerth", "noaerth-labs", "noaerth-team", "portfolio-control", "gh0st"})


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


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


def confidence_from_evidence(present: list[bool]) -> str:
    """Confidence reflects how much evidence exists, never how big the score is.

    A high score backed by three signals is not more trustworthy than a low
    score backed by nine.
    """
    count = sum(1 for flag in present if flag)
    if count >= 7:
        return "HIGH"
    if count >= 4:
        return "MEDIUM"
    return "LOW"


# Evidence categories used to grade confidence. Listed once so the grade means
# the same thing for commercial potential and viability.
EVIDENCE_KEYS = (
    "has_code", "has_depth", "has_tests", "has_ci", "has_deploy_evidence",
    "has_reviews", "has_public_domain", "has_docs", "has_tests_config",
)


def evidence_presence(facts: dict, vercel_project: dict | None, deployment: dict | None,
                      reviews: dict) -> list[bool]:
    return [
        bool(facts.get("code_files", 0)),
        bool(facts.get("code_lines", 0) >= 5000),
        bool(facts.get("test_files", 0)),
        bool(facts.get("has_ci", 0)),
        deployment is not None,
        bool(reviews.get("total", 0)),
        bool(vercel_project and vercel_project.get("domain")),
        bool(facts.get("readme_bytes", 0)),
        bool(facts.get("has_tests_config")),
    ]


# --------------------------------------------------------------- repo facts


ENTRYPOINT_NAMES = frozenset({
    "main.py", "__main__.py", "cli.py", "app.py", "server.py", "manage.py",
    "main.ts", "main.js", "index.ts", "index.js", "server.ts", "app.ts",
    "main.rs", "main.go", "index.mjs",
})


def collect_repo_facts(repo: Path) -> dict:
    """Bounded, deterministic surface scan. No file contents beyond a README head."""
    code_files = 0
    code_lines = 0
    markdown = 0
    test_files = 0
    routes = 0
    components = 0
    languages: dict[str, int] = {}
    code_dirs: set[str] = set()
    entrypoints: list[str] = []
    cli_framework = False
    for dirpath, dirnames, filenames in os.walk(repo):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        try:
            depth = len(Path(dirpath).relative_to(repo).parts)
        except ValueError:
            depth = 0
        if depth > 8:
            dirnames[:] = []
            continue
        for name in filenames:
            suffix = Path(name).suffix.lower()
            path = Path(dirpath) / name
            if suffix in CODE_SUFFIXES:
                code_files += 1
                languages[suffix] = languages.get(suffix, 0) + 1
                try:
                    relative = path.relative_to(repo)
                    code_dirs.add(str(relative.parent))
                except ValueError:
                    pass
                if name in ENTRYPOINT_NAMES:
                    entrypoints.append(name)
                try:
                    with path.open("rb") as handle:
                        head = handle.read(4096).lower()
                        code_lines += sum(1 for _ in path.open("rb"))
                    if b"argparse" in head or b"commander" in head or b"click.command" in head:
                        cli_framework = True
                    if suffix == ".py" and (b"fastapi" in head or b"flask" in head or b"@app.route" in head):
                        routes += 1
                except OSError:
                    pass
                lowered = str(path).lower()
                if "test" in lowered or name.startswith("test_") or name.endswith("_test.go"):
                    test_files += 1
                if suffix in {".tsx", ".jsx", ".vue", ".svelte"}:
                    components += 1
                if suffix in {".ts", ".js", ".tsx", ".jsx", ".py"} and (
                    "route" in lowered or "api" in lowered or "pages" in lowered
                ):
                    routes += 1
            elif suffix == ".md":
                markdown += 1
    package_meta = _package_meta(repo / "package.json")
    readme = repo / "README.md"
    readme_bytes = 0
    readme_head = ""
    if readme.is_file():
        try:
            readme_bytes = readme.stat().st_size
            readme_head = readme.read_text(encoding="utf-8", errors="ignore")[:2500].lower()
        except OSError:
            readme_bytes = 0
    workflows = 0
    workflow_dir = repo / ".github" / "workflows"
    if workflow_dir.is_dir():
        workflows = len([p for p in workflow_dir.glob("*.y*ml")])
    # Test discovery by convention counts. Requiring a config file would mark
    # every pytest or go test project as untested.
    has_test_dir = any(
        (repo / name).is_dir() and any((repo / name).iterdir())
        for name in ("tests", "test", "__tests__", "spec")
    )
    return {
        "code_files": code_files,
        "code_lines": code_lines,
        "markdown": markdown,
        "test_files": test_files,
        "routes": routes,
        "components": components,
        "languages": languages,
        "modules": len(code_dirs),
        "entrypoints": sorted(set(entrypoints)),
        "has_cli_surface": bool(entrypoints) or cli_framework,
        "has_package": (repo / "package.json").is_file(),
        "has_lockfile": any(
            (repo / name).is_file() for name in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb")
        ),
        "has_typescript": (repo / "tsconfig.json").is_file(),
        "has_tests_config": has_test_dir or any(
            (repo / name).is_file()
            for name in ("jest.config.js", "vitest.config.ts", "pytest.ini", "playwright.config.ts")
        ),
        "has_ci": workflows,
        "has_env_example": (repo / ".env.example").is_file(),
        "has_vercel_config": (repo / "vercel.json").is_file(),
        "has_license": any(
            (repo / name).is_file() for name in ("LICENSE", "LICENSE.md", "LICENSE.txt")
        ),
        "has_docs_dir": (repo / "docs").is_dir(),
        "has_container": (repo / "Dockerfile").is_file(),
        "has_site_dir": any((repo / name).is_dir() for name in ("site", "website", "web", "landing")),
        "readme_bytes": readme_bytes,
        "readme_installable": ("install" in readme_head or "npm i" in readme_head or "pip install" in readme_head),
        "readme_usage": ("usage" in readme_head or "quickstart" in readme_head or "getting started" in readme_head),
        "package": package_meta,
    }


def _package_meta(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    scripts = raw.get("scripts") or {}
    deps = raw.get("dependencies") or {}
    return {
        "name": raw.get("name", ""),
        "version": raw.get("version", ""),
        "has_bin": bool(raw.get("bin")),
        "has_build": bool(scripts.get("build")),
        "has_test": bool(scripts.get("test")),
        "has_start": bool(scripts.get("start") or scripts.get("dev")),
        "dependencies": len(deps),
        "framework": _framework_of(deps),
    }


def _framework_of(deps: dict) -> str:
    for name in ("next", "nuxt", "astro", "svelte", "vue", "vite"):
        if name in deps:
            return name
    return ""


def collect_git_windows(repo: Path, now: datetime) -> dict:
    """Real commit counts. Momentum must come from code that actually moved."""
    windows = {
        "commits_24h": now - timedelta(hours=24),
        "commits_7d": now - timedelta(days=7),
        "commits_30d": now - timedelta(days=30),
    }
    out = {key: 0 for key in windows}
    for key, since in windows.items():
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-list", "--count", f"--since={since.isoformat()}", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        try:
            out[key] = int(proc.stdout.strip() or 0)
        except ValueError:
            out[key] = 0
    author_out = subprocess.run(
        ["git", "-C", str(repo), "log", "--since=30.days", "--format=%an"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout.splitlines()
    proc = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    out["authors_30d"] = len({name for name in author_out if name.strip()})
    out["dirty_files"] = len([line for line in proc.stdout.splitlines() if line.strip()])
    return out


# ------------------------------------------------------------------- typing


def infer_type(facts: dict, local: dict, vercel_project: dict | None, is_public: bool, is_website: bool) -> str:
    if is_website:
        return "CONSUMER_BRAND"
    package = facts.get("package") or {}
    deps = package.get("framework")
    if package.get("has_bin") and not (facts.get("routes") or 0):
        return "CLI_TOOL"
    if facts.get("has_container") and (facts.get("code_lines", 0) > 4000):
        return "INFRASTRUCTURE"
    if package.get("name", "").startswith("@noaerth") or "portfolio" in package.get("name", ""):
        return "INTERNAL_TOOL"
    if is_public and vercel_project is not None and deps in {"next", "astro", "nuxt"}:
        return "B2B_SAAS"
    if is_public and (facts.get("components", 0) or 0) >= 8:
        return "CONSUMER_BRAND"
    if (facts.get("routes", 0) or 0) >= 6 or deps:
        return "B2B_SAAS"
    return "INTERNAL_TOOL"


# ------------------------------------------------------------------- stage


def deployment_state(deployment: dict | None) -> str:
    """READY, ERROR or UNKNOWN. Missing evidence is never treated as failure."""
    if not deployment:
        return "UNKNOWN"
    state = (deployment.get("state") or "").upper()
    if state == "READY":
        return "READY"
    if state in {"ERROR", "CANCELED"}:
        return "ERROR"
    return "UNKNOWN"


def infer_stage(facts: dict, local: dict, vercel_project: dict | None, deployment: dict | None,
                health: str, startup_type: str = "B2B_SAAS") -> str:
    """Derive the maturity stage from evidence, never from optimism."""
    code_files = facts.get("code_files", 0)
    code_lines = facts.get("code_lines", 0)
    live = bool(vercel_project and vercel_project.get("domain"))
    deploy = deployment_state(deployment)
    workflows = facts.get("has_ci", 0)
    tests = facts.get("test_files", 0)
    shape = structure_signal(facts, startup_type)
    interfaces = shape["interfaces"]

    if health == "BUILD_FAILING":
        return "CONCEPT"
    if deploy == "READY" and code_files >= 400 and tests >= 5 and workflows >= 1:
        return "EARLY_PRODUCT"
    if deploy == "READY" and interfaces >= 10 and code_lines >= 8000:
        return "WORKING_PRIMARY_WORKFLOW"
    if deploy == "READY" and code_files >= 80:
        return "WORKING_PRIMARY_WORKFLOW"
    if deploy == "READY" and live and interfaces >= 4:
        return "INTERACTIVE_DEMO"
    if deploy == "READY" and live:
        return "STATIC_DEMO"
    if live and interfaces >= 4 and code_files >= 40:
        return "INTERACTIVE_DEMO"
    if live and code_files >= 8:
        return "STATIC_DEMO"
    if code_files >= 40 and interfaces >= 3:
        return "PARTIAL_WORKFLOW"
    if code_files >= 40 and (facts.get("modules", 0) or 0) >= 3:
        return "PARTIAL_WORKFLOW"
    return "CONCEPT"


# --------------------------------------------------------------- dimensions


def structure_signal(facts: dict, startup_type: str) -> dict:
    """Structure evidence appropriate to what the startup actually is.

    A Python control plane with no HTTP routes still has modules, entrypoints and
    a command surface. Scoring it against a Next.js checklist would be wrong.
    """
    web_like = startup_type in {"B2B_SAAS", "CONSUMER_BRAND", "MEDIA"}
    interfaces = (facts.get("routes", 0) or 0)
    if startup_type in {"CLI_TOOL", "INTERNAL_TOOL", "INFRASTRUCTURE"}:
        interfaces = max(
            interfaces,
            len(facts.get("entrypoints", []) or []) * 4 + (6 if facts.get("has_cli_surface") else 0),
            (facts.get("modules", 0) or 0),
        )
    return {
        "web_like": web_like,
        "interfaces": min(60, interfaces),
        "components": facts.get("components", 0) or 0,
        "modules": facts.get("modules", 0) or 0,
    }


def dimension_scores(facts: dict, local: dict, vercel_project: dict | None, deployment: dict | None,
                     health: str, reviews: dict, startup_type: str = "B2B_SAAS") -> dict[str, float]:
    code_files = facts.get("code_files", 0)
    code_lines = facts.get("code_lines", 0)
    live = bool(vercel_project and vercel_project.get("domain"))
    deploy = deployment_state(deployment)
    deployment_ok = deploy == "READY"
    deployment_failed = deploy == "ERROR"
    shape = structure_signal(facts, startup_type)
    interfaces = shape["interfaces"]

    product = 0.0
    if code_files:
        product += 25
    if code_files >= 40:
        product += 25
    if code_lines >= 5000:
        product += 20
    if code_lines >= 25000:
        product += 15
    if interfaces >= 3:
        product += 15

    workflow = 0.0
    if interfaces >= 2:
        workflow += 30
    if deployment_ok:
        workflow += 45
    if live:
        workflow += 10
    if reviews.get("pass_rate", 0) >= 0.5 and reviews.get("total", 0) >= 3:
        workflow += 25

    engineering = 0.0
    engineering += 20 if facts.get("has_package") else (12 if code_files >= 10 else 0)
    engineering += 10 if facts.get("has_lockfile") else (6 if (facts.get("languages") or {}).get(".py") else 0)
    engineering += 12 if facts.get("has_typescript") else 0
    engineering += min(24, (facts.get("test_files", 0) or 0) * 2.5)
    engineering += min(20, (facts.get("has_ci", 0) or 0) * 10)
    engineering += 10 if (facts.get("modules", 0) or 0) >= 5 else 0
    engineering += 8 if facts.get("has_cli_surface") else 0
    engineering -= 25 if health == "BUILD_FAILING" else 0

    ux = 0.0
    if (facts.get("components", 0) or 0) >= 3:
        ux += 20
    if (facts.get("components", 0) or 0) >= 12:
        ux += 15
    ux += min(50, reviews.get("mobile_pass_rate", 0) * 50)
    ux += min(15, reviews.get("desktop_pass_rate", 0) * 15)
    if live:
        ux += 10
    # A UI that exists but has never been reviewed cannot read as a good UI.
    if not reviews.get("mobile") and not reviews.get("desktop"):
        ux = min(ux, 45.0)
    if not shape["web_like"] and ux == 0 and live:
        ux = 25

    design = 0.0
    design += min(55, reviews.get("design_pass_rate", 0) * 55)
    if (facts.get("components", 0) or 0) >= 8:
        design += 20
    if live:
        design += 10
    if not reviews.get("design"):
        design = min(design, 30.0)
    if not shape["web_like"]:
        design = max(design, 30.0)  # not a visual product; do not punish it

    website = 0.0
    if live:
        website += 45
    if deployment_ok:
        website += 25
    if facts.get("has_site_dir"):
        website += 15
    if vercel_project and vercel_project.get("framework"):
        website += 15

    docs = 0.0
    if facts.get("readme_bytes", 0) >= 400:
        docs += 25
    if facts.get("readme_installable"):
        docs += 20
    if facts.get("readme_usage"):
        docs += 15
    if facts.get("has_docs_dir"):
        docs += 15
    if facts.get("has_license"):
        docs += 10
    if facts.get("has_env_example"):
        docs += 15
    if facts.get("has_cli_surface") and facts.get("entrypoints"):
        docs += 10  # a command surface is a documented surface

    if not shape["web_like"]:
        website = max(website, 20.0 if live else 0.0)

    public_alignment = 0.0
    if live:
        public_alignment += 25
    if deployment_ok:
        public_alignment += 35
    if vercel_project and vercel_project.get("link_type"):
        public_alignment += 20
    if facts.get("has_package") and (facts.get("package") or {}).get("version"):
        public_alignment += 10
    if facts.get("readme_bytes", 0) >= 1200:
        public_alignment += 10

    operability = 0.0
    if deployment_ok:
        operability += 35
    if deployment_failed:
        operability -= 20
    if facts.get("has_ci"):
        operability += 20
    if facts.get("has_container"):
        operability += 15
    if facts.get("has_tests_config"):
        operability += 15
    if (local.get("dirty_files", 0) or 0) == 0:
        operability += 15

    return {
        "product": _clamp(product),
        "primary_workflow": _clamp(workflow),
        "engineering": _clamp(engineering),
        "ux": _clamp(ux),
        "design": _clamp(design),
        "website": _clamp(website),
        "docs": _clamp(docs),
        "public_alignment": _clamp(public_alignment),
        "operability": _clamp(operability),
    }


def baseline_attainment(dimensions: dict[str, float], startup_type: str) -> float:
    weights = WEIGHTS.get(startup_type, WEIGHTS["B2B_SAAS"])
    total = sum(dimensions.get(name, 0.0) * weight for name, weight in weights.items())
    return round(_clamp(total / sum(weights.values())), 1)


# ----------------------------------------------------------------- momentum


def momentum(git: dict, signals: dict) -> dict:
    """Material progress only. Scans, heartbeats and dossier writes score zero."""
    commits_24h = git.get("commits_24h", 0)
    commits_7d = git.get("commits_7d", 0)
    commits_30d = git.get("commits_30d", 0)
    weighted_commits = commits_24h * 3 + commits_7d * 1.0 + commits_30d * 0.35

    components = {
        "commits": min(55.0, weighted_commits * 4.0),
        "material": min(20.0, signals.get("material_improvements", 0) * 7.0),
        "releases": min(12.0, signals.get("releases", 0) * 6.0),
        "recovered": min(8.0, signals.get("recovered_workflows", 0) * 8.0),
        "reviews_cleared": min(8.0, signals.get("reviews_passed_7d", 0) * 2.0),
    }
    total = sum(components.values())
    window_24h = _clamp(commits_24h * 34.0 + components["material"] * 0.4)
    window_7d = _clamp(commits_7d * 4.5 + total * 0.35)
    # The 30 day window contains the 7 day window, so it can never score lower.
    # Days 8 to 30 add a smaller, decaying contribution.
    older = max(0, commits_30d - commits_7d)
    window_30d = _clamp(max(window_7d, older * 1.4 + total * 0.30))
    return {
        "components": {key: round(value, 1) for key, value in components.items()},
        "momentum_24h": round(window_24h, 1),
        "momentum_7d": round(window_7d, 1),
        "momentum_30d": round(window_30d, 1),
        "momentum_total": round(_clamp(total), 1),
    }


# ---------------------------------------------------------------- importance


def importance(slug: str, startup_type: str, canonical: dict, is_public: bool, override: dict | None) -> float:
    parts = 15.0
    if slug in FLAGSHIP_SLUGS:
        parts += 40
    if canonical.get("production_domain", "").endswith("noaerth.com"):
        parts += 12
    if startup_type in {"INFRASTRUCTURE", "INTERNAL_TOOL"}:
        parts += 10
    if is_public:
        parts += 8
    if canonical.get("local_core_repo"):
        parts += 6
    if startup_type in {"B2B_SAAS", "CLI_TOOL"}:
        parts += 6
    if override and override.get("importance") is not None:
        parts = float(override["importance"])
    return round(_clamp(parts), 1)


# ------------------------------------------------------- commercial, viability


def commercial_potential(startup_type: str, facts: dict, canonical: dict, deployment: dict | None,
                         stage: str, reviews: dict, override: dict | None) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    priors = {
        "B2B_SAAS": 68.0, "CLI_TOOL": 62.0, "INFRASTRUCTURE": 58.0,
        "CONSUMER_BRAND": 46.0, "INTERNAL_TOOL": 28.0, "MEDIA": 40.0,
    }
    score = priors.get(startup_type, 40.0)
    reasons.append(f"{startup_type.lower().replace('_', ' ')} revenue model prior {int(priors.get(startup_type, 40))}")

    if stage in {"EARLY_PRODUCT", "MATURE_PRODUCT", "WORKING_PRIMARY_WORKFLOW"}:
        score += 10
        reasons.append("a buyer could evaluate something that works")
    if canonical.get("production_domain"):
        score += 6
        reasons.append("a public domain exists")
    if deployment_state(deployment) == "READY":
        score += 4
    if facts.get("readme_installable") and facts.get("readme_usage"):
        score += 5
        reasons.append("install and usage are documented")
    if startup_type == "INTERNAL_TOOL":
        score -= 14
        reasons.append("internal tooling rarely has an external buyer")
    if facts.get("code_files", 0) == 0:
        score -= 20
        reasons.append("no product code exists yet")

    # No market, competitor or willingness-to-pay evidence exists in this
    # control plane yet, so confidence is capped until the external landscape
    # pass lands. A prior plus code evidence is a hypothesis, not a measurement.
    confidence = confidence_from_evidence(
        evidence_presence(facts, {"domain": canonical.get("production_domain")}, deployment, reviews)
    )
    if confidence == "HIGH":
        confidence = "MEDIUM"
    if override and override.get("commercial") is not None:
        score = float(override["commercial"])
        confidence = "HIGH"
        reasons.append("owner override")
    return round(_clamp(score), 1), confidence, reasons


def viability(startup_type: str, facts: dict, health: str, deployment: dict | None, reviews: dict,
              override: dict | None) -> tuple[float, str, list[str]]:
    reasons: list[str] = []
    score = 30.0
    if facts.get("code_files", 0) >= 40:
        score += 18
        reasons.append("real code exists")
    if facts.get("code_files", 0) >= 300:
        score += 10
    if facts.get("test_files", 0) >= 3:
        score += 12
        reasons.append("tests exist")
    if facts.get("has_ci"):
        score += 8
    if deployment_state(deployment) == "READY":
        score += 14
        reasons.append("it deploys cleanly")
    if deployment_state(deployment) == "ERROR":
        score -= 18
        reasons.append("the newest deployment failed")
    if health == "BUILD_FAILING":
        score -= 24
        reasons.append("the build is failing")
    if reviews.get("total", 0) and reviews.get("pass_rate", 0) >= 0.6:
        score += 10
    if reviews.get("total", 0) and reviews.get("fail_rate", 1.0) >= 0.8:
        score -= 8
        reasons.append("most recorded reviews fail")

    confidence = confidence_from_evidence(evidence_presence(facts, None, deployment, reviews))
    if override and override.get("viability") is not None:
        score = float(override["viability"])
        confidence = "HIGH"
        reasons.append("owner override")
    return round(_clamp(score), 1), confidence, reasons


# -------------------------------------------------------------------- heat


def heat(momentum_score: float, commercial: float, importance_score: float, releases: int,
         is_public: bool) -> float:
    """Momentum led, with commercial pull and importance as bounded amplifiers.

    Deliberately not the same as commercial potential: a well positioned but
    dormant startup is not hot.
    """
    raw = (
        momentum_score * 0.70
        + commercial * 0.15
        + importance_score * 0.15
    )
    if releases:
        raw = min(100.0, raw + min(8.0, releases * 4.0))
    if not is_public:
        raw *= 0.92
    return round(_clamp(raw), 1)


def attention(baseline: float, importance_score: float, momentum_score: float, blockers: int,
              failed_reviews: int, open_work: int, cold: bool, mismatch: bool,
              override: dict | None) -> float:
    gap = max(0.0, 100.0 - baseline)
    raw = (
        gap * 0.34
        + importance_score * 0.22
        + (100.0 - momentum_score) * 0.16
        + min(24.0, blockers * 8.0)
        + min(18.0, failed_reviews * 3.0)
        + min(14.0, open_work * 1.4)
        + (12.0 if cold else 0.0)
        + (8.0 if mismatch else 0.0)
    )
    if override and override.get("attention") is not None:
        raw = float(override["attention"])
    return round(_clamp(raw), 1)


# ------------------------------------------------------------------- caching


def facts_cache_path(package_root: Path) -> Path:
    return package_root / "data" / "intelligence" / "repo-facts.json"


def load_facts(package_root: Path, ttl: int = 6 * 3600) -> dict | None:
    path = facts_cache_path(package_root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - payload.get("captured_at", 0) > ttl:
        return None
    return payload.get("facts", {})


def save_facts(package_root: Path, facts: dict) -> None:
    path = facts_cache_path(package_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"captured_at": time.time(), "facts": facts}, separators=(",", ":")),
        encoding="utf-8",
    )