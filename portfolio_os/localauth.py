"""Loopback-only credential for the local engine UI. Never used off this machine."""

from __future__ import annotations

import hmac
import secrets
from pathlib import Path

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_CREDENTIAL = "local-credential"
_NONCE = "bootstrap-nonce"


def loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def _path(root: Path, name: str) -> Path:
    return root / "data" / name


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def ensure_local_credential(root: Path) -> str:
    path = _path(root, _CREDENTIAL)
    if path.is_file():
        current = path.read_text(encoding="utf-8").strip()
        if len(current) >= 16:
            return current
    value = secrets.token_urlsafe(32)
    _write_secret(path, value)
    return value


def resolve_token(root: Path, host: str) -> str | None:
    explicit = __import__("os").environ.get("PORTFOLIO_OS_API_TOKEN", "").strip()
    if len(explicit) >= 16:
        return explicit
    if loopback(host):
        return ensure_local_credential(root)
    return None


def issue_bootstrap(root: Path) -> str:
    nonce = secrets.token_urlsafe(24)
    _write_secret(_path(root, _NONCE), nonce)
    return nonce


def consume_bootstrap(root: Path, nonce: str) -> bool:
    path = _path(root, _NONCE)
    if not path.is_file() or not nonce:
        return False
    current = path.read_text(encoding="utf-8").strip()
    path.unlink(missing_ok=True)
    return bool(current) and hmac.compare_digest(current, nonce)
