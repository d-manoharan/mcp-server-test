# supabase-postgres-mcp

A [FastMCP](https://gofastmcp.com) server that lets MCP clients (Claude Desktop,
VS Code, Cursor, OpenCode, ...) inspect and query a **Supabase Postgres**
database.

- **One setting to configure:** a Postgres connection string (`DATABASE_URL`).
- **Read-only by default.** `SELECT`, `WITH`, `SHOW`, and `EXPLAIN` run inside a
  read-only transaction. Writes require an explicit opt-in at startup.
- **Supabase-aware.** `sslmode=require` (what the Supabase dashboard emits) is
  handled by libpq, TLS is enabled automatically for Supabase hosts, and
  prepared statements are disabled for Supavisor/PgBouncer compatibility.
- **`$1` placeholders.** The tool API accepts Supabase-style `$1, $2, ...`
  parameters (translated safely for psycopg); `%s`-style SQL also works.
- **Starts without a connection string** so clients can always connect; tool
  calls then return an actionable error instead of crashing the process.
- **Guarded execution.** One statement per call, transaction-control and `COPY`
  statements rejected, statement timeout, and a row cap.

## Tools

| Tool | Writes | Description |
| --- | --- | --- |
| `list_schemas` | no | List non-system schemas (usually `public`). |
| `list_tables` | no | List tables/views in a schema with their comments. |
| `describe_table` | no | Columns, types, primary key, foreign keys, indexes, comment. |
| `query` | no | Run one read-only SQL statement with `$1`-style parameters. |
| `execute` | **yes** | Run one `INSERT`/`UPDATE`/`DELETE`/DDL statement. Only registered when `ALLOW_WRITES=true`. |

`query` results look like:

```json
{
  "status": "SELECT 42",
  "row_count": 42,
  "returned_rows": 42,
  "truncated": false,
  "max_rows": 100,
  "duration_ms": 12,
  "fields": [{ "name": "id", "type_code": 23 }],
  "rows": [{ "id": 1 }]
}
```

`row_count` is what Postgres reports; `returned_rows` is what was sent back.
`truncated` is `true` when more rows existed than `max_rows` allowed. Add a
`LIMIT` to your SQL when you expect large result sets.

Values are JSON-normalized for MCP: `numeric`/`int8` become strings, dates and
timestamps become ISO-8601 strings, `bytea` becomes base64, and duplicate column
names are suffixed (`id`, `id_2`).

## Quick start

```bash
git clone https://github.com/d-manoharan/mcp-server-test.git
cd mcp-server-test
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env
# edit .env and replace the DATABASE_URL placeholder
pytest -q                          # optional: smoke tests need no database
```

Then point your MCP client at `.venv/bin/supabase-postgres-mcp` (see below), or
run it manually:

```bash
python -m supabase_postgres_mcp
```

> The server speaks MCP over **stdio**, so it will appear to hang when run in a
> terminal. That is expected — normally your MCP client launches it.

### Using uv (optional)

```bash
uv sync
uv run supabase-postgres-mcp
```

## The connection string

Copy it from **Supabase Dashboard → Project Settings → Database → Connection
string**. Replace only the placeholders:

```dotenv
DATABASE_URL=postgresql://postgres.<PROJECT_REF>:<YOUR_DB_PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require
```

Three forms are supported:

| Mode | Port | Host | Notes |
| --- | --- | --- | --- |
| Transaction pooler | `6543` | `aws-0-<REGION>.pooler.supabase.com` | **Recommended.** IPv4-compatible, good for short-lived MCP processes. No session-level features (`LISTEN`, advisory locks across calls, ...). |
| Session pooler | `5432` | `aws-0-<REGION>.pooler.supabase.com` | IPv4-compatible with session-level features. |
| Direct | `5432` | `db.<PROJECT_REF>.supabase.co` | IPv6-only by default; use only if your network supports IPv6. |

Tips:

- The pooler username is `postgres.<PROJECT_REF>`, **not** just `postgres`.
- URL-encode special characters in the password: `p@ss` → `p%40ss`, `#` → `%23`.
- Keep `?sslmode=require` (or `verify-full` if you want certificate
  verification). If you omit it on a Supabase host, TLS is enabled automatically.
- Prisma-style parameters such as `?pgbouncer=true&connection_limit=1` are
  dropped automatically because libpq does not understand them.

The server loads `.env` from the current directory or from next to
`pyproject.toml`. Real environment variables always win, so client config takes
precedence over the file.

## MCP client configuration

Replace `/absolute/path/to/mcp-server-test` with the real path.

### Claude Desktop

`claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "supabase-postgres": {
      "command": "/absolute/path/to/mcp-server-test/.venv/bin/supabase-postgres-mcp",
      "env": {
        "DATABASE_URL": "postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require"
      }
    }
  }
}
```

### VS Code

`.vscode/mcp.json`:

```json
{
  "servers": {
    "supabase-postgres": {
      "type": "stdio",
      "command": "/absolute/path/to/mcp-server-test/.venv/bin/supabase-postgres-mcp",
      "env": {
        "DATABASE_URL": "postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require"
      }
    }
  }
}
```

### OpenCode

`opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "supabase-postgres": {
      "type": "local",
      "command": ["/absolute/path/to/mcp-server-test/.venv/bin/supabase-postgres-mcp"],
      "enabled": true,
      "environment": {
        "DATABASE_URL": "postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require"
      }
    }
  }
}
```

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "supabase-postgres": {
      "command": "/absolute/path/to/mcp-server-test/.venv/bin/supabase-postgres-mcp",
      "env": {
        "DATABASE_URL": "postgresql://postgres.<PROJECT_REF>:<PASSWORD>@aws-0-<REGION>.pooler.supabase.com:6543/postgres?sslmode=require"
      }
    }
  }
}
```

If you prefer not to put the URL in client config, copy `.env.example` to
`.env` next to `pyproject.toml` and omit the `env` block.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DATABASE_URL` | — | Supabase Postgres connection string. Also accepts `SUPABASE_DB_URL`, `POSTGRES_URL`, or `PG_CONNECTION_STRING`. |
| `ALLOW_WRITES` | `false` | Set to `true` to register the `execute` tool. Restart required. |
| `MAX_ROWS` | `100` | Default row cap for `query` (1–1000). |
| `STATEMENT_TIMEOUT_MS` | `15000` | Server-side statement timeout (1000–120000). |
| `POOL_MAX` | `5` | Maximum pooled connections (1–20). |
| `CONNECT_TIMEOUT_S` | `10` | TCP connect timeout (1–60). |
| `APPLICATION_NAME` | `supabase-postgres-mcp` | Shown in `pg_stat_activity`. |
| `MCP_NO_DOTENV` | unset | Set to `1`/`true` to ignore `.env` files. |
| `DOTENV_CONFIG_PATH` | unset | Explicit `.env` file to load. |

## Safety

- Every `query` runs in `BEGIN` + `SET TRANSACTION READ ONLY` and is rolled
  back (`force_rollback=True`). Postgres rejects writes with
  `25006 read_only_sql_transaction`.
- Only one statement per call is allowed. Strings, quoted identifiers,
  dollar-quoted bodies, and comments are understood by the splitter, so a
  payload such as `COMMIT; DELETE ...` is rejected. `BEGIN`/`COMMIT`/
  `ROLLBACK`/`SAVEPOINT` and `COPY` are blocked explicitly.
- `execute` is not registered unless `ALLOW_WRITES=true`, and it **commits
  immediately**. There is no preview or undo.
- The server connects with the role in `DATABASE_URL`. Connecting as
  `postgres` **bypasses Row Level Security**. For read-only usage, prefer a
  dedicated role:

  ```sql
  create role mcp_reader login password '<strong-password>';
  grant usage on schema public to mcp_reader;
  grant select on all tables in schema public to mcp_reader;
  alter default privileges in schema public grant select on tables to mcp_reader;
  ```

  Then use `postgresql://mcp_reader.<PROJECT_REF>:<password>@...` (pooler) or
  `postgresql://mcp_reader:<password>@db.<PROJECT_REF>.supabase.co:5432/postgres`.

- Passwords placed in `.env` or client config are stored in plain text. Keep
  `.env` out of version control (it is git-ignored) and rotate credentials if
  they leak.

## Troubleshooting

| Symptom | Likely fix |
| --- | --- |
| `DATABASE_URL is not set` | Set it in the client config, the shell, or `.env` next to `pyproject.toml`. |
| `getaddrinfo ENOTFOUND` / connection timeout | Use the pooler host (`...pooler.supabase.com`, port 6543) — direct hosts are often IPv6-only. |
| `Tenant or user not found` | Username must be `postgres.<PROJECT_REF>` for the pooler, and the region must match the project. |
| `password authentication failed` | Re-copy the password; URL-encode special characters. |
| `server does not support SSL` | Remove `sslmode` for a local Postgres; Supabase requires TLS. |
| `invalid connection option "..."` | Remove unsupported URL parameters, or open an issue; common Prisma parameters are already stripped. |
| `cannot execute INSERT in a read-only transaction` | Use `execute` (requires `ALLOW_WRITES=true`) or run the write in the Supabase SQL editor. |
| `prepared statement "..." does not exist` | The server already disables prepared statements for the pooler. If you use another client, set `prepare_threshold=None` / `statement_cache_size=0`. |
| `Only one SQL statement is allowed` | Send one statement per tool call. |

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

pytest -q

TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  python scripts/integration.py
```

The integration test creates a `mcp_it_*` schema, exercises every tool, and
drops the schema. Run it against a disposable database, never production.

### Layout

```
src/supabase_postgres_mcp/
  config.py    environment parsing and .env loading
  db.py        pool, TLS defaults, read-only/write execution
  sql.py       single-statement validation and $n parameter binding
  server.py    FastMCP server and tool definitions
tests/         unit and smoke tests (no database required)
scripts/       integration test against a real database
```

## Limitations

- stdio transport only (no HTTP).
- No migration, schema-editing, or Supabase Management API features; this is a
  focused SQL access server.
- libpq buffers each result, so the row cap limits what is returned but not
  what the server sends over the wire. Use `LIMIT` for very large tables; the
  statement timeout is the backstop.
- Transaction pooler connections cannot keep session state between calls.
- `COPY` is not supported.
- `ALLOW_WRITES` is read at startup; restart the server after changing it.

## License

MIT
