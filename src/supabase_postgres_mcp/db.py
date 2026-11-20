"""Postgres access: pooled connections, safe SQL execution, and error details."""

from __future__ import annotations

import base64
import math
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from .config import Settings, load_settings
from .sql import bind_parameters

# URL parameters used by other Postgres clients/tools that libpq does not
# understand. Dropping them keeps "copy the connection string" seamless.
_IGNORED_URL_PARAMS = {"pgbouncer", "connection_limit", "pool_timeout", "schema", "supa"}


class ConfigurationError(RuntimeError):
    """Raised when no usable connection string is configured."""


@dataclass(slots=True)
class QueryResult:
    status: str
    row_count: int
    rows: list[dict[str, Any]]
    fields: list[dict[str, Any]]
    truncated: bool
    max_rows: int
    duration_ms: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "row_count": self.row_count,
            "returned_rows": len(self.rows),
            "truncated": self.truncated,
            "max_rows": self.max_rows,
            "duration_ms": self.duration_ms,
            "fields": self.fields,
            "rows": self.rows,
        }


_pool_lock = threading.Lock()
_pool: ConnectionPool | None = None
_pool_conninfo: str | None = None


def _clean_connection_string(connection_string: str) -> str:
    parts = urlsplit(connection_string)
    if parts.scheme not in {"postgres", "postgresql"}:
        return connection_string

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _IGNORED_URL_PARAMS
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _is_supabase_host(host: str) -> bool:
    return (
        host.endswith(".supabase.co")
        or host.endswith(".supabase.com")
        or ".pooler.supabase." in host
    )


def build_conninfo(connection_string: str, settings: Settings) -> str:
    """Normalize the connection string and add safe defaults.

    libpq (through psycopg) handles ``sslmode`` natively, so the value copied
    from the Supabase dashboard works as-is. If the host is a Supabase host but
    no ``sslmode`` was supplied, TLS is enabled.
    """

    cleaned = _clean_connection_string(connection_string)
    try:
        params = conninfo_to_dict(cleaned)
    except psycopg.ProgrammingError:
        # Not a format psycopg can parse; let the connection attempt report it.
        return cleaned

    params.setdefault("application_name", settings.app_name)
    params.setdefault("connect_timeout", str(settings.connect_timeout_s))

    host = params.get("host", "")
    if isinstance(host, str) and _is_supabase_host(host):
        params.setdefault("sslmode", "require")

    return make_conninfo(**params)


def get_pool() -> ConnectionPool:
    """Return the lazily created connection pool."""

    global _pool, _pool_conninfo

    settings = load_settings()
    if not settings.database_url:
        raise ConfigurationError(
            "DATABASE_URL is not set. Add your Supabase Postgres connection "
            "string to the environment or to a .env file next to pyproject.toml. "
            "Example: postgresql://postgres.<PROJECT_REF>:<PASSWORD>@"
            "aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require"
        )

    conninfo = build_conninfo(settings.database_url, settings)

    with _pool_lock:
        if _pool is not None and _pool_conninfo == conninfo:
            return _pool

        if _pool is not None:
            _pool.close()
            _pool = None

        pool = ConnectionPool(
            conninfo=conninfo,
            min_size=0,
            max_size=settings.pool_max,
            timeout=float(settings.connect_timeout_s + 5),
            open=False,
            # PgBouncer/Supavisor in transaction mode does not support named
            # prepared statements. psycopg prepares after 5 executions of the
            # same query by default; disable that for pooler compatibility.
            kwargs={"prepare_threshold": None},
            name="supabase-postgres-mcp",
        )
        pool.open()
        _pool = pool
        _pool_conninfo = conninfo
        return pool


def close_pool() -> None:
    global _pool, _pool_conninfo
    with _pool_lock:
        if _pool is not None:
            _pool.close()
            _pool = None
            _pool_conninfo = None


def _set_statement_timeout(cursor: psycopg.Cursor[Any], milliseconds: int) -> None:
    # int() is redundant here (settings are already bounded ints) but makes the
    # f-string safe if this helper is ever called with something else.
    cursor.execute(f"SET LOCAL statement_timeout = {int(milliseconds)}")


def _dedupe_names(names: Sequence[str]) -> list[str]:
    seen: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        result.append(name if count == 1 else f"{name}_{count}")
    return result


def _jsonable(value: Any) -> Any:
    """Convert psycopg result values into JSON-friendly Python values."""

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, datetime_time)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _execute_cursor(
    cursor: psycopg.Cursor[Any],
    sql: str,
    params: Sequence[Any],
    max_rows: int,
) -> QueryResult:
    started = time.perf_counter()
    cursor.execute(sql, tuple(params) if params else None)

    columns = list(cursor.description or [])
    names = _dedupe_names([column.name for column in columns])
    fetched = cursor.fetchmany(max_rows + 1) if columns else []
    truncated = len(fetched) > max_rows
    visible = fetched[:max_rows]

    rows = [
        {name: _jsonable(value) for name, value in zip(names, row, strict=False)}
        for row in visible
    ]
    fields = [
        {"name": name, "type_code": column.type_code}
        for name, column in zip(names, columns, strict=False)
    ]
    row_count = cursor.rowcount if cursor.rowcount is not None and cursor.rowcount >= 0 else len(fetched)

    return QueryResult(
        status=cursor.statusmessage or "",
        row_count=row_count,
        rows=rows,
        fields=fields,
        truncated=truncated,
        max_rows=max_rows,
        duration_ms=round((time.perf_counter() - started) * 1000),
    )


def _clamp_max_rows(max_rows: int | None, settings: Settings) -> int:
    requested = settings.max_rows if max_rows is None else max_rows
    return max(1, min(int(requested), 1_000))


def run_query(
    sql: str,
    params: Sequence[Any],
    *,
    read_only: bool,
    max_rows: int | None = None,
) -> QueryResult:
    """Run one already-validated statement inside a transaction."""

    settings = load_settings()
    limit = _clamp_max_rows(max_rows, settings)
    pool = get_pool()
    statement, bound_params = bind_parameters(sql, params)

    with pool.connection() as connection:
        if read_only:
            with connection.transaction(force_rollback=True):
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    _set_statement_timeout(cursor, settings.statement_timeout_ms)
                    return _execute_cursor(cursor, statement, bound_params, limit)
        with connection.transaction():
            with connection.cursor() as cursor:
                _set_statement_timeout(cursor, settings.statement_timeout_ms)
                return _execute_cursor(cursor, statement, bound_params, limit)


def run_read_queries(
    queries: Sequence[tuple[str, Sequence[Any]]],
    *,
    max_rows: int | None = None,
) -> list[QueryResult]:
    """Run several statements in one read-only transaction (consistent snapshot)."""

    settings = load_settings()
    limit = _clamp_max_rows(max_rows, settings)
    pool = get_pool()

    with pool.connection() as connection:
        with connection.transaction(force_rollback=True):
            with connection.cursor() as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                _set_statement_timeout(cursor, settings.statement_timeout_ms)
                return [
                    _execute_cursor(cursor, sql, params, limit)
                    for sql, params in queries
                ]


def describe_error(error: BaseException) -> dict[str, Any]:
    """Extract useful Postgres error fields without leaking the connection string."""

    if isinstance(error, ConfigurationError):
        return {"message": str(error)}

    if isinstance(error, psycopg.Error):
        diag = error.diag
        return {
            "message": (diag.message_primary or str(error)).strip(),
            "code": error.sqlstate or None,
            "detail": diag.message_detail or None,
            "hint": diag.message_hint or None,
            "position": diag.statement_position or None,
        }

    message = str(error).strip()
    return {"message": message or type(error).__name__}
