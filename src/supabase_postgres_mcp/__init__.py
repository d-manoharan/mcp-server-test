"""Supabase Postgres MCP server."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("supabase-postgres-mcp")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "1.0.0"

__all__ = ["__version__"]
