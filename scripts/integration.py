#!/usr/bin/env python3
"""Integration test against a real Postgres database.

    TEST_DATABASE_URL=postgresql://... python scripts/integration.py

Creates a temporary schema, exercises every tool, and drops the schema again.
Point it at a disposable database, never production.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from uuid import uuid4

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
if not TEST_DATABASE_URL:
    sys.exit(
        "TEST_DATABASE_URL is required, e.g. "
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres"
    )

for _name in ("SUPABASE_DB_URL", "POSTGRES_URL", "PG_CONNECTION_STRING"):
    os.environ.pop(_name, None)
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ALLOW_WRITES"] = "true"
os.environ["MCP_NO_DOTENV"] = "1"

import psycopg  # noqa: E402
from fastmcp import Client  # noqa: E402

from supabase_postgres_mcp.server import mcp  # noqa: E402

SCHEMA = f"mcp_it_{uuid4().hex[:12]}"


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def payload(result):
    """Return structured data, falling back to the JSON text block."""

    if result.data is not None:
        return result.data
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            return json.loads(text)
    raise AssertionError("Tool returned no data")


def text(result) -> str:
    return "\n".join(getattr(block, "text", "") for block in result.content)


def setup_database() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"create schema {quote(SCHEMA)}")
        connection.execute(
            f"""
            create table {quote(SCHEMA)}.widgets (
                id serial primary key,
                name text not null,
                qty integer not null default 0
            )
            """
        )
        connection.execute(
            f"insert into {quote(SCHEMA)}.widgets (name, qty) values ('alpha', 1), ('beta', 2)"
        )


def teardown_database() -> None:
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as connection:
        connection.execute(f"drop schema if exists {quote(SCHEMA)} cascade")


async def run_checks() -> None:
    async with Client(mcp) as client:
        tools = {tool.name for tool in await client.list_tools()}
        for required in ("describe_table", "execute", "list_schemas", "list_tables", "query"):
            assert required in tools, f"missing tool {required}: {sorted(tools)}"

        schemas = payload(await client.call_tool("list_schemas"))
        assert SCHEMA in schemas["schemas"], f"{SCHEMA} missing from {schemas}"

        tables = payload(await client.call_tool("list_tables", {"schema": SCHEMA}))
        assert [table["name"] for table in tables["tables"]] == ["widgets"]
        assert tables["tables"][0]["type"] == "table"

        description = payload(
            await client.call_tool("describe_table", {"schema": SCHEMA, "table": "widgets"})
        )
        assert description["primary_key"] == ["id"]
        assert [column["name"] for column in description["columns"]] == ["id", "name", "qty"]
        assert description["columns"][1]["nullable"] is False

        limited = payload(
            await client.call_tool(
                "query",
                {
                    "sql": f"select name, qty from {quote(SCHEMA)}.widgets order by id",
                    "max_rows": 1,
                },
            )
        )
        assert limited["returned_rows"] == 1
        assert limited["truncated"] is True
        assert limited["rows"][0]["name"] == "alpha"

        read_only_rejected = await client.call_tool(
            "query",
            {"sql": f"insert into {quote(SCHEMA)}.widgets (name) values ('nope')"},
            raise_on_error=False,
        )
        assert read_only_rejected.is_error is True
        assert "25006" in text(read_only_rejected) or "read-only" in text(read_only_rejected).lower()

        multi = await client.call_tool(
            "query", {"sql": "select 1; select 2"}, raise_on_error=False
        )
        assert multi.is_error is True, "multi-statement SQL must be rejected"

        transaction_control = await client.call_tool(
            "query", {"sql": "commit"}, raise_on_error=False
        )
        assert transaction_control.is_error is True, "transaction control must be rejected"

        inserted = payload(
            await client.call_tool(
                "execute",
                {
                    "sql": (
                        f"insert into {quote(SCHEMA)}.widgets (name, qty) "
                        "values ($1, $2) returning id, name, qty"
                    ),
                    "params": ["gamma", 3],
                },
            )
        )
        assert inserted["row_count"] == 1
        assert inserted["rows"][0]["name"] == "gamma"

        count = payload(
            await client.call_tool(
                "query",
                {"sql": f"select count(*)::int as count from {quote(SCHEMA)}.widgets"},
            )
        )
        assert count["rows"][0]["count"] == 3

        types_row = payload(
            await client.call_tool(
                "query",
                {
                    "sql": (
                        "select now() as ts, 1.25::numeric as amount, "
                        "'\\xdeadbeef'::bytea as blob, true as flag"
                    )
                },
            )
        )["rows"][0]
        assert isinstance(types_row["ts"], str)
        assert types_row["amount"] == "1.25"
        assert types_row["blob"] == "3q2+7w=="
        assert types_row["flag"] is True

        duplicates = payload(
            await client.call_tool("query", {"sql": "select 1 as x, 2 as x"})
        )
        assert list(duplicates["rows"][0]) == ["x", "x_2"]
        assert duplicates["rows"][0] == {"x": 1, "x_2": 2}


def main() -> None:
    setup_database()
    try:
        asyncio.run(run_checks())
    finally:
        teardown_database()
    print("Integration test passed.")


if __name__ == "__main__":
    main()
