"""Single place that answers whether the outside world may be reached.

Live network access is off by default. Every provider asks here rather than
reaching for its own flag, so there is exactly one switch and one audit point.
"""

from __future__ import annotations

import os


def live_network() -> bool:
    raw = os.getenv("PORTFOLIO_OS_LIVE_NETWORK", "")
    if raw:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return False


def live_llm() -> bool:
    raw = os.getenv("PORTFOLIO_OS_LIVE_LLM", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def scout_endpoint() -> str:
    """An ambient agent binary is not consent; an explicit endpoint is."""
    return os.getenv("TRILLIONX_SCOUT_CMD", "")
