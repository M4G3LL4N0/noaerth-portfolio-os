"""Bounded daemon. One startup at a time. Stops when asked."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from portfolio_os.execute import execute_batch
from portfolio_os.publish import write_report, write_snapshots


def run_daemon(
    conn: sqlite3.Connection,
    portfolio_root: Path,
    evidence_root: Path,
    *,
    interval: int = 120,
    max_cycles: int | None = None,
    stop_file: Path | None = None,
    publish_dir: Path | None = None,
) -> str:
    cycles = 0
    while True:
        if stop_file is not None and stop_file.exists():
            return "stopped"
        beat = Path(__file__).resolve().parents[1] / "data" / "daemon.heartbeat"
        beat.parent.mkdir(parents=True, exist_ok=True)
        beat.write_text("running\n", encoding="utf-8")
        if max_cycles is not None and cycles >= max_cycles:
            return "idle"
        execute_batch(conn, portfolio_root, 1, evidence_root)
        if publish_dir is not None:
            write_report(conn, "daily")
            write_snapshots(conn, publish_dir)
        conn.commit()
        cycles += 1
        if max_cycles is None:
            time.sleep(interval)
