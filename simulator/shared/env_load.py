"""Load repo-root `.env` then `connection.env` for simulator CLIs."""

from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv

_SIMULATOR_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _SIMULATOR_ROOT.parent
_CONNECTION_ENV = _REPO_ROOT / "connection.env"


def load_simulator_env() -> None:
    """
    Load repository-root `.env`, then `connection.env` if present.

    Uses ``override=False`` for ``connection.env`` so explicit values in `.env`
    win over the deploy snapshot (e.g. local overrides).
    """
    load_dotenv(_REPO_ROOT / ".env")
    if _CONNECTION_ENV.is_file():
        load_dotenv(_CONNECTION_ENV, override=False)
