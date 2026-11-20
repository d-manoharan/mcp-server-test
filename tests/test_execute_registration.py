"""Verify ALLOW_WRITES controls whether the execute tool is registered."""

from __future__ import annotations

import json
import os
import subprocess
import sys

CONNECTION_VARS = (
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "POSTGRES_URL",
    "PG_CONNECTION_STRING",
)


def test_execute_registered_when_allowed():
    env = {
        **os.environ,
        "ALLOW_WRITES": "true",
        "MCP_NO_DOTENV": "1",
    }
    for name in CONNECTION_VARS:
        env.pop(name, None)

    code = """
import asyncio
import json
from fastmcp import Client
from supabase_postgres_mcp.server import mcp

async def main():
    async with Client(mcp) as client:
        names = sorted(tool.name for tool in await client.list_tools())
    print(json.dumps(names))

asyncio.run(main())
"""

    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    names = json.loads(result.stdout.strip().splitlines()[-1])
    assert "execute" in names
