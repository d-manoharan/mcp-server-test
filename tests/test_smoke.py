"""Smoke tests that do not require a database."""

from __future__ import annotations

import os

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

# Configure the server before importing it: ALLOW_WRITES controls registration
# at import time, and .env loading must not make the test depend on a local DB.
os.environ["MCP_NO_DOTENV"] = "1"
os.environ.pop("ALLOW_WRITES", None)
for _name in (
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "POSTGRES_URL",
    "PG_CONNECTION_STRING",
):
    os.environ.pop(_name, None)

from supabase_postgres_mcp.server import mcp  # noqa: E402


@pytest.mark.asyncio
async def test_registers_read_only_tools():
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert {tool.name for tool in tools} == {
        "list_schemas",
        "list_tables",
        "describe_table",
        "query",
    }


@pytest.mark.asyncio
async def test_missing_connection_string_is_actionable():
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as info:
            await client.call_tool("query", {"sql": "select 1"})

    assert "DATABASE_URL" in str(info.value)


@pytest.mark.asyncio
async def test_multiple_statements_rejected_without_database():
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as info:
            await client.call_tool("query", {"sql": "select 1; select 2"})

    assert "Only one SQL statement" in str(info.value)


@pytest.mark.asyncio
async def test_transaction_control_rejected_without_database():
    async with Client(mcp) as client:
        with pytest.raises(ToolError) as info:
            await client.call_tool("query", {"sql": "commit"})

    assert "Transaction control" in str(info.value)
