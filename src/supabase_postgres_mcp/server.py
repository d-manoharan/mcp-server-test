"""FastMCP tools for a Supabase Postgres database."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import __version__
from .config import load_dotenv_files, load_settings
from .db import describe_error, run_query, run_read_queries
from .sql import validate_single_statement

load_dotenv_files()

INSTRUCTIONS = """
This server exposes a Supabase Postgres database.

Workflow:
1. Use list_schemas to find schemas (Supabase projects normally use `public`).
2. Use list_tables and describe_table to learn table and column names.
3. Use query for read-only SQL. Pass values with $1, $2, ... placeholders
   through the `params` array instead of interpolating them into the SQL.

`query` accepts exactly one statement per call. Add LIMIT to queries against
large tables; results are also capped by MAX_ROWS. Writes are only available
through the `execute` tool when the server was started with ALLOW_WRITES=true.
""".strip()

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)

mcp = FastMCP(
    name="supabase-postgres-mcp",
    version=__version__,
    instructions=INSTRUCTIONS,
    mask_error_details=False,
)

_READ_ONLY_HINT = (
    "The `query` tool only runs read-only statements. Start the server with "
    "ALLOW_WRITES=true to register the write-capable `execute` tool."
)


def _tool_error(error: BaseException, extra_hint: str | None = None) -> ToolError:
    info = describe_error(error)
    lines = [f"Error: {info['message']}"]
    for key, label in (
        ("code", "Code"),
        ("detail", "Detail"),
        ("hint", "Hint"),
        ("position", "Position"),
    ):
        value = info.get(key)
        if value:
            lines.append(f"{label}: {value}")
    if extra_hint:
        lines.extend(["", extra_hint])
    return ToolError("\n".join(lines))


def _run_read(sql: str, params: list[Any] | None, max_rows: int | None) -> dict[str, Any]:
    statement = validate_single_statement(sql)
    result = run_query(statement, params or [], read_only=True, max_rows=max_rows)
    return result.as_dict()


@mcp.tool(annotations=READ_ONLY)
def list_schemas() -> dict[str, Any]:
    """List the non-system schemas in the connected Postgres database.

    For a Supabase project this is normally `public` plus any custom schemas.
    """

    try:
        result = run_read_queries([("""
            select nspname as schema
              from pg_catalog.pg_namespace
             where nspname not in ('pg_catalog', 'information_schema')
               and nspname not like 'pg_toast%'
               and nspname not like 'pg_temp_%'
               and nspname not like 'pg_toast_temp_%'
             order by nspname
        """, [])])[0]
        return {"schemas": [row["schema"] for row in result.rows]}
    except Exception as error:  # noqa: BLE001 - reported to the MCP client
        raise _tool_error(error) from error


@mcp.tool(annotations=READ_ONLY)
def list_tables(
    schema: Annotated[
        str,
        Field(min_length=1, description="Schema to inspect. Defaults to `public`."),
    ] = "public",
    include_views: Annotated[
        bool,
        Field(description="Also include views, materialized views, and foreign tables."),
    ] = False,
) -> dict[str, Any]:
    """List tables (and optionally views) in a schema, including comments."""

    kinds = ("r", "p", "v", "m", "f") if include_views else ("r", "p")
    placeholders = ", ".join(["%s"] * len(kinds))
    sql = f"""
        select c.relname as name,
               case c.relkind
                 when 'r' then 'table'
                 when 'p' then 'partitioned table'
                 when 'v' then 'view'
                 when 'm' then 'materialized view'
                 when 'f' then 'foreign table'
               end as type,
               pg_catalog.obj_description(c.oid, 'pg_class') as comment
          from pg_catalog.pg_class c
          join pg_catalog.pg_namespace n on n.oid = c.relnamespace
         where n.nspname = %s
           and c.relkind in ({placeholders})
         order by c.relkind, c.relname
    """

    try:
        result = run_read_queries([(sql, [schema, *kinds])], max_rows=1_000)[0]
        return {"schema": schema, "tables": result.rows}
    except Exception as error:  # noqa: BLE001
        raise _tool_error(error) from error


@mcp.tool(annotations=READ_ONLY)
def describe_table(
    table: Annotated[str, Field(min_length=1, description="Table or view name.")],
    schema: Annotated[
        str,
        Field(min_length=1, description="Schema containing the table. Defaults to `public`."),
    ] = "public",
) -> dict[str, Any]:
    """Describe a table or view: columns, primary key, foreign keys, indexes, comment."""

    queries = [
        (
            """
            select column_name as name,
                   data_type as type,
                   udt_name as underlying_type,
                   is_nullable = 'YES' as nullable,
                   column_default as default_value,
                   character_maximum_length as max_length,
                   numeric_precision as numeric_precision,
                   numeric_scale as numeric_scale
              from information_schema.columns
             where table_schema = %s and table_name = %s
             order by ordinal_position
            """,
            [schema, table],
        ),
        (
            """
            select kcu.column_name as column
              from information_schema.table_constraints tc
              join information_schema.key_column_usage kcu
                on kcu.constraint_name = tc.constraint_name
               and kcu.constraint_schema = tc.constraint_schema
             where tc.constraint_type = 'PRIMARY KEY'
               and tc.table_schema = %s
               and tc.table_name = %s
             order by kcu.ordinal_position
            """,
            [schema, table],
        ),
        (
            """
            select tc.constraint_name as name,
                   kcu.column_name as column,
                   ccu.table_schema as references_schema,
                   ccu.table_name as references_table,
                   ccu.column_name as references_column
              from information_schema.table_constraints tc
              join information_schema.key_column_usage kcu
                on kcu.constraint_name = tc.constraint_name
               and kcu.constraint_schema = tc.constraint_schema
              join information_schema.constraint_column_usage ccu
                on ccu.constraint_name = tc.constraint_name
               and ccu.constraint_schema = tc.constraint_schema
             where tc.constraint_type = 'FOREIGN KEY'
               and tc.table_schema = %s
               and tc.table_name = %s
             order by tc.constraint_name, kcu.ordinal_position
            """,
            [schema, table],
        ),
        (
            """
            select indexname as name, indexdef as definition
              from pg_catalog.pg_indexes
             where schemaname = %s and tablename = %s
             order by indexname
            """,
            [schema, table],
        ),
        (
            """
            select pg_catalog.obj_description(c.oid, 'pg_class') as comment
              from pg_catalog.pg_class c
              join pg_catalog.pg_namespace n on n.oid = c.relnamespace
             where n.nspname = %s and c.relname = %s
            """,
            [schema, table],
        ),
    ]

    try:
        columns, primary_key, foreign_keys, indexes, comment = run_read_queries(
            queries, max_rows=1_000
        )
    except Exception as error:  # noqa: BLE001
        raise _tool_error(error) from error

    if not columns.rows:
        raise ToolError(f'Table or view "{schema}.{table}" was not found in the connected database.')

    return {
        "schema": schema,
        "table": table,
        "comment": comment.rows[0]["comment"] if comment.rows else None,
        "columns": columns.rows,
        "primary_key": [row["column"] for row in primary_key.rows],
        "foreign_keys": foreign_keys.rows,
        "indexes": indexes.rows,
    }


@mcp.tool(annotations=READ_ONLY)
def query(
    sql: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "A single SQL statement. Multiple statements are rejected. "
                "Example: select * from public.todos where done = $1"
            ),
        ),
    ],
    params: Annotated[
        list[str | int | float | bool | None] | None,
        Field(description="Values for the $1, $2, ... placeholders, in order."),
    ] = None,
    max_rows: Annotated[
        int | None,
        Field(
            ge=1,
            le=1_000,
            description="Maximum rows to return. Defaults to MAX_ROWS (100).",
        ),
    ] = None,
) -> dict[str, Any]:
    """Run a single read-only SQL statement inside a read-only transaction.

    Use $1, $2, ... placeholders and the `params` array instead of
    interpolating values. Add LIMIT for large tables; `truncated` in the
    result indicates that more rows were available than were returned.
    """

    try:
        return _run_read(sql, params, max_rows)
    except Exception as error:  # noqa: BLE001
        hint = _READ_ONLY_HINT if describe_error(error).get("code") == "25006" else None
        raise _tool_error(error, hint) from error


def _register_execute() -> None:
    @mcp.tool(name="execute", annotations=WRITE)
    def execute(
        sql: Annotated[
            str,
            Field(
                min_length=1,
                description=(
                    "A single write statement. Multiple statements are rejected. "
                    "Example: insert into public.todos (title) values ($1) returning *"
                ),
            ),
        ],
        params: Annotated[
            list[str | int | float | bool | None] | None,
            Field(description="Values for the $1, $2, ... placeholders, in order."),
        ] = None,
    ) -> dict[str, Any]:
        """Run a single INSERT, UPDATE, DELETE, or DDL statement and COMMIT it.

        Only registered when ALLOW_WRITES=true. Statements run with the
        credentials in DATABASE_URL; prefer a dedicated role with the minimum
        privileges required.
        """

        try:
            statement = validate_single_statement(sql)
            result = run_query(statement, params or [], read_only=False)
            return result.as_dict()
        except Exception as error:  # noqa: BLE001
            raise _tool_error(error) from error


if load_settings().allow_writes:
    _register_execute()


def main() -> None:
    """Run the server over stdio (the default MCP transport)."""

    mcp.run(show_banner=False)
