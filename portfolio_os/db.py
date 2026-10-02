"""SQLite source of truth for the portfolio control plane."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS startups (
  id INTEGER PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  primary_path TEXT,
  website_path TEXT,
  category TEXT,
  maturity TEXT,
  is_public INTEGER NOT NULL DEFAULT 0,
  github_url TEXT,
  website_url TEXT,
  vercel_project TEXT,
  noaerth_url TEXT,
  health TEXT NOT NULL DEFAULT 'REVIEW_REQUIRED',
  priority INTEGER NOT NULL DEFAULT 50,
  last_reviewed_at TEXT,
  owner_private INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS work_items (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER NOT NULL REFERENCES startups(id),
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT,
  priority INTEGER NOT NULL DEFAULT 50,
  status TEXT NOT NULL DEFAULT 'queued',
  assigned_role TEXT,
  parent_work_item INTEGER,
  created_at TEXT NOT NULL,
  started_at TEXT,
  completed_at TEXT,
  blocked_reason TEXT,
  priority_factors TEXT
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY,
  work_item_id INTEGER REFERENCES work_items(id),
  role TEXT NOT NULL,
  runner TEXT NOT NULL,
  input_summary TEXT,
  result_summary TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  completed_at TEXT,
  files_changed TEXT,
  commits TEXT,
  evidence TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER NOT NULL REFERENCES startups(id),
  work_item_id INTEGER,
  reviewer_role TEXT NOT NULL,
  dimension TEXT NOT NULL,
  finding TEXT NOT NULL,
  severity TEXT NOT NULL,
  resolution TEXT NOT NULL,
  timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER REFERENCES startups(id),
  source TEXT NOT NULL,
  category TEXT NOT NULL,
  signal TEXT NOT NULL,
  value TEXT,
  text TEXT,
  is_public INTEGER NOT NULL DEFAULT 0,
  timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deployments (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER NOT NULL REFERENCES startups(id),
  provider TEXT NOT NULL,
  project TEXT,
  commit_sha TEXT,
  url TEXT,
  status TEXT NOT NULL,
  blocker TEXT,
  timestamp TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  timestamp TEXT NOT NULL,
  startup_id INTEGER,
  actor TEXT NOT NULL,
  event_type TEXT NOT NULL,
  summary TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('PUBLIC', 'TEAM', 'PRIVATE_SYSTEM')),
  metadata TEXT
);

CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS locks (
  startup_id INTEGER PRIMARY KEY REFERENCES startups(id),
  holder TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  period TEXT NOT NULL,
  created_at TEXT NOT NULL,
  visibility TEXT NOT NULL CHECK (visibility IN ('PUBLIC', 'TEAM', 'PRIVATE_SYSTEM')),
  body TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_state (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  scanned_at TEXT,
  directory_names TEXT
);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in present:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def connect(path: Path) -> sqlite3.Connection:
    """Open one connection for the calling thread. Do not share it across threads."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    # External landscape (external intelligence): cached, TTL-bound, and never
    # re-run for an unchanged startup. See portfolio_os/external.py.
    conn.executescript("""
CREATE TABLE IF NOT EXISTS external_landscape (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER NOT NULL REFERENCES startups(id),
  slug TEXT NOT NULL,
  queries TEXT NOT NULL DEFAULT '[]',
  summary TEXT,
  primary_workflow TEXT,
  reuse_leverage REAL,
  high_fit_oss INTEGER NOT NULL DEFAULT 0,
  direct_competitors INTEGER NOT NULL DEFAULT 0,
  reuse_opportunities INTEGER NOT NULL DEFAULT 0,
  frameworks INTEGER NOT NULL DEFAULT 0,
  name_collision INTEGER NOT NULL DEFAULT 0,
  recommendation TEXT,
  coverage TEXT NOT NULL DEFAULT 'RESEARCHED',
  artifact_path TEXT,
  scout_used INTEGER NOT NULL DEFAULT 0,
  scout_model TEXT,
  scout_note TEXT,
  searched_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(startup_id)
);

CREATE TABLE IF NOT EXISTS landscape_candidates (
  id INTEGER PRIMARY KEY,
  startup_id INTEGER NOT NULL REFERENCES startups(id),
  slug TEXT NOT NULL,
  source TEXT NOT NULL,
  repo TEXT,
  owner TEXT,
  name TEXT,
  kind TEXT NOT NULL DEFAULT 'github',
  description TEXT,
  url TEXT,
  homepage TEXT,
  stars INTEGER NOT NULL DEFAULT 0,
  forks INTEGER NOT NULL DEFAULT 0,
  language TEXT,
  license_spdx TEXT,
  license_class TEXT NOT NULL DEFAULT 'UNKNOWN',
  activity TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  topics TEXT NOT NULL DEFAULT '[]',
  classification TEXT NOT NULL DEFAULT 'IRRELEVANT',
  reuse_fit INTEGER NOT NULL DEFAULT 0,
  reuse_fit_factors TEXT NOT NULL DEFAULT '{}',
  reuse_decision TEXT NOT NULL DEFAULT 'OWNER_REVIEW',
  could_replace TEXT,
  risks TEXT NOT NULL DEFAULT '[]',
  rationale TEXT,
  attribution TEXT,
  scout_note TEXT,
  reviewed_at TEXT NOT NULL,
  UNIQUE(startup_id, source, repo)
);

CREATE TABLE IF NOT EXISTS landscape_queries (
  id INTEGER PRIMARY KEY,
  slug TEXT NOT NULL,
  query TEXT NOT NULL,
  source TEXT NOT NULL,
  run_at TEXT NOT NULL,
  result_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE(slug, query, source)
);

CREATE TABLE IF NOT EXISTS landscape_cache (
  cache_key TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  payload TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  expires_at TEXT
);
""")
    _ensure_column(conn, "startups", "paused", "paused INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "locks", "work_item_id", "work_item_id INTEGER")
    _ensure_column(conn, "locks", "run_id", "run_id TEXT")
    _ensure_column(conn, "work_items", "priority_reason", "priority_reason TEXT")
    _ensure_column(conn, "agent_runs", "model", "model TEXT")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS material_improvements (
          id INTEGER PRIMARY KEY,
          startup_id INTEGER NOT NULL REFERENCES startups(id),
          category TEXT NOT NULL,
          work_item_id INTEGER,
          summary TEXT NOT NULL,
          commit_sha TEXT,
          review_result TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS venture_reviews (
          id INTEGER PRIMARY KEY,
          startup_id INTEGER NOT NULL REFERENCES startups(id),
          question TEXT NOT NULL,
          now_tasks TEXT NOT NULL,
          later TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS dossiers (
          startup_id INTEGER PRIMARY KEY REFERENCES startups(id),
          slug TEXT NOT NULL,
          facts TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS startup_coverage (
          startup_id INTEGER PRIMARY KEY REFERENCES startups(id),
          shard TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS preview_runs (
          id INTEGER PRIMARY KEY,
          startup_id INTEGER NOT NULL REFERENCES startups(id),
          surface TEXT NOT NULL,
          branch TEXT,
          commit_sha TEXT,
          port INTEGER,
          pid INTEGER,
          command TEXT,
          status TEXT NOT NULL,
          started_at TEXT NOT NULL,
          last_health TEXT,
          last_access TEXT
        );
        CREATE TABLE IF NOT EXISTS preview_shots (
          id INTEGER PRIMARY KEY,
          startup_id INTEGER NOT NULL REFERENCES startups(id),
          viewport TEXT NOT NULL,
          commit_sha TEXT NOT NULL,
          branch TEXT,
          route TEXT NOT NULL,
          file_name TEXT NOT NULL,
          verdict TEXT,
          findings TEXT,
          captured_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS release_gates (
          id INTEGER PRIMARY KEY,
          startup_id INTEGER NOT NULL REFERENCES startups(id),
          commit_sha TEXT NOT NULL,
          visual TEXT NOT NULL,
          release_approval TEXT NOT NULL,
          deployment TEXT NOT NULL,
          approver TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(startup_id, commit_sha)
        );
        CREATE TABLE IF NOT EXISTS canonical_resources (
          slug TEXT PRIMARY KEY,
          local_core_repo TEXT,
          local_website_repo TEXT,
          github_core TEXT,
          github_core_id TEXT,
          github_public TEXT,
          vercel_core TEXT,
          vercel_core_id TEXT,
          vercel_public TEXT,
          vercel_public_id TEXT,
          production_domain TEXT,
          production_branch TEXT,
          production_commit TEXT,
          github_remote TEXT,
          duplicate_status TEXT NOT NULL DEFAULT 'UNKNOWN',
          recommended_action TEXT NOT NULL DEFAULT 'NONE',
          note TEXT,
          scan_model_version INTEGER NOT NULL DEFAULT 1,
          scanned_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS canonicalization_queue (
          id INTEGER PRIMARY KEY,
          slug TEXT NOT NULL,
          surface TEXT NOT NULL,
          operation TEXT NOT NULL,
          reason TEXT NOT NULL,
          risk TEXT NOT NULL DEFAULT 'REVIEW',
          state TEXT NOT NULL DEFAULT 'queued',
          evidence TEXT,
          created_at TEXT NOT NULL,
          resolved_at TEXT,
          notes TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_scores (
          id INTEGER PRIMARY KEY,
          day TEXT NOT NULL,
          slug TEXT NOT NULL,
          score_model_version INTEGER NOT NULL,
          baseline_attainment REAL,
          baseline_gap REAL,
          stage TEXT,
          momentum_24h REAL,
          momentum_7d REAL,
          momentum_30d REAL,
          momentum_total REAL,
          attention_score REAL,
          importance REAL,
          commercial_potential REAL,
          commercial_confidence TEXT,
          viability REAL,
          viability_confidence TEXT,
          heat REAL,
          cold INTEGER NOT NULL DEFAULT 0,
          ahead INTEGER NOT NULL DEFAULT 0,
          blocked INTEGER NOT NULL DEFAULT 0,
          release_ready INTEGER NOT NULL DEFAULT 0,
          in_review INTEGER NOT NULL DEFAULT 0,
          reuse_leverage REAL,
          band TEXT,
          payload TEXT,
          created_at TEXT NOT NULL,
          UNIQUE(day, slug)
        );
        """
    )
    return conn


def default_db_path(package_root: Path | None = None) -> Path:
    root = package_root or Path(__file__).resolve().parents[1]
    return root / "data" / "portfolio.db"
