"""Configuration loading for the Supabase Postgres MCP server."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_TRUTHY = {"1", "true", "yes", "on"}
_CONNECTION_STRING_VARS = (
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "POSTGRES_URL",
    "PG_CONNECTION_STRING",
)

_dotenv_loaded = False


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved runtime configuration."""

    database_url: str | None
    allow_writes: bool
    max_rows: int
    statement_timeout_ms: int
    pool_max: int
    connect_timeout_s: int
    app_name: str


def _first_non_empty(values: list[str | None]) -> str | None:
    for value in values:
        if value is not None and value.strip():
            return value.strip()
    return None


def _read_int(value: str | None, default: int, minimum: int, maximum: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return max(minimum, min(parsed, maximum))


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from environment variables.

    Values are read on every call so tests and long-running processes can
    change the environment without re-importing the package.
    """

    source = os.environ if env is None else env
    url = _first_non_empty([source.get(name) for name in _CONNECTION_STRING_VARS])
    app_name = (source.get("APPLICATION_NAME") or "").strip() or "supabase-postgres-mcp"

    return Settings(
        database_url=url,
        allow_writes=(source.get("ALLOW_WRITES") or "").strip().lower() in _TRUTHY,
        max_rows=_read_int(source.get("MAX_ROWS"), 100, 1, 1000),
        statement_timeout_ms=_read_int(
            source.get("STATEMENT_TIMEOUT_MS"), 15_000, 1_000, 120_000
        ),
        pool_max=_read_int(source.get("POOL_MAX"), 5, 1, 20),
        connect_timeout_s=_read_int(source.get("CONNECT_TIMEOUT_S"), 10, 1, 60),
        app_name=app_name,
    )


def load_dotenv_files() -> None:
    """Load .env files without overriding real environment variables.

    An MCP client often launches the server with a working directory that is
    not the project root, so the project-level .env is checked as well.
    """

    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True

    if (os.environ.get("MCP_NO_DOTENV") or "").strip().lower() in _TRUTHY:
        return

    candidates: list[Path] = []
    explicit = os.environ.get("DOTENV_CONFIG_PATH")
    if explicit:
        candidates.append(Path(explicit))

    try:
        project_root = Path(__file__).resolve().parents[2]
        candidates.append(project_root / ".env")
    except IndexError:  # pragma: no cover - only if installed unusually shallow
        pass

    candidates.append(Path.cwd() / ".env")

    for path in dict.fromkeys(candidates):
        if path.is_file():
            load_dotenv(path, override=False)
