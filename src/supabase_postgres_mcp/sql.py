"""Small SQL lexer used to enforce one statement per tool call.

Postgres itself blocks writes in a read-only transaction, but a multi-statement
string such as ``COMMIT; DELETE FROM ...`` would end that transaction and run
the next statement outside it. Splitting the input here (while respecting
strings, identifiers, dollar-quoted bodies, and comments) closes that gap and
also gives the model a clear error message.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_PARAMETER = re.compile(r"\$(\d+)")
_TRANSACTION_CONTROL = re.compile(
    r"^(begin|start\s+transaction|commit|rollback|end|savepoint|release|"
    r"prepare\s+transaction)\b",
    re.IGNORECASE,
)
_COPY = re.compile(r"^copy\b", re.IGNORECASE)


def split_statements(sql: str) -> list[str]:
    """Split a SQL string on top-level semicolons.

    Semicolons inside quoted strings, quoted identifiers, dollar-quoted
    strings, and comments are ignored. Empty statements are dropped.
    """

    statements: list[str] = []
    start = 0
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char in ("'", '"'):
            quote = char
            escape_backslash = (
                quote == "'"
                and index > 0
                and sql[index - 1] in "eE"
                and (index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_"))
            )
            index += 1
            while index < length:
                if escape_backslash and sql[index] == "\\":
                    index += 2
                    continue
                if sql[index] == quote:
                    if index + 1 < length and sql[index + 1] == quote:
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue

        if char == "-" and sql.startswith("--", index):
            newline = sql.find("\n", index)
            index = length if newline == -1 else newline + 1
            continue

        if char == "/" and sql.startswith("/*", index):
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            continue

        if char == "$":
            match = _DOLLAR_TAG.match(sql, index)
            if match:
                tag = match.group(0)
                end = sql.find(tag, match.end())
                index = length if end == -1 else end + len(tag)
                continue

        if char == ";":
            statement = sql[start:index].strip()
            if statement:
                statements.append(statement)
            start = index + 1

        index += 1

    tail = sql[start:].strip()
    if tail:
        statements.append(tail)
    return statements


def _strip_leading_comments(statement: str) -> str:
    text = statement.lstrip()
    while True:
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline + 1 :].lstrip()
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :].lstrip()
        else:
            return text


def validate_single_statement(sql: str) -> str:
    """Return the single statement, or raise ``ValueError`` with a clear message."""

    statements = split_statements(sql)
    if not statements:
        raise ValueError("The SQL statement is empty.")
    if len(statements) > 1:
        raise ValueError(
            "Only one SQL statement is allowed per tool call. "
            "Split the work into separate calls."
        )

    statement = statements[0]
    significant = _strip_leading_comments(statement)
    if _TRANSACTION_CONTROL.match(significant):
        raise ValueError(
            "Transaction control statements (BEGIN/COMMIT/ROLLBACK/...) are "
            "managed by the server and are not allowed."
        )
    if _COPY.match(significant):
        raise ValueError(
            "COPY is not supported by this server. Use SELECT/INSERT/UPDATE/DELETE instead."
        )
    return statement


def bind_parameters(sql: str, params: Sequence[Any]) -> tuple[str, list[Any]]:
    """Translate Supabase-style ``$1`` placeholders to psycopg's format.

    psycopg's client-side binding uses ``%s``; Supabase documentation, SQL
    examples, and LLMs commonly use ``$1``. Literal ``%`` characters are
    doubled so patterns such as ``LIKE '%foo%'`` survive psycopg's formatting.

    ``%s``-style parameters are passed through unchanged so either style works.
    """

    if not params:
        return sql, []

    if "\x00" in sql:
        raise ValueError("SQL contains a NUL byte, which Postgres does not allow.")

    pieces: list[str] = []
    order: list[int] = []
    index = 0
    length = len(sql)

    while index < length:
        char = sql[index]

        if char in ("'", '"'):
            quote = char
            escape_backslash = (
                quote == "'"
                and index > 0
                and sql[index - 1] in "eE"
                and (index < 2 or not (sql[index - 2].isalnum() or sql[index - 2] == "_"))
            )
            end = index + 1
            while end < length:
                if escape_backslash and sql[end] == "\\":
                    end += 2
                    continue
                if sql[end] == quote:
                    if end + 1 < length and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                end += 1
            pieces.append(sql[index:end])
            index = end
            continue

        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            end = length if end == -1 else end + 1
            pieces.append(sql[index:end])
            index = end
            continue

        if char == "/" and sql.startswith("/*", index):
            depth = 1
            end = index + 2
            while end < length and depth:
                if sql.startswith("/*", end):
                    depth += 1
                    end += 2
                elif sql.startswith("*/", end):
                    depth -= 1
                    end += 2
                else:
                    end += 1
            pieces.append(sql[index:end])
            index = end
            continue

        if char == "$":
            tag = _DOLLAR_TAG.match(sql, index)
            if tag:
                marker = tag.group(0)
                end = sql.find(marker, tag.end())
                end = length if end == -1 else end + len(marker)
                pieces.append(sql[index:end])
                index = end
                continue

            parameter = _PARAMETER.match(sql, index)
            if parameter:
                position = int(parameter.group(1))
                if position < 1 or position > len(params):
                    raise ValueError(
                        f"SQL references ${position} but only {len(params)} "
                        f"parameter(s) were provided."
                    )
                order.append(position - 1)
                pieces.append("\x00")
                index = parameter.end()
                continue

        pieces.append(char)
        index += 1

    if not order:
        # No $n placeholders; let psycopg handle %s-style SQL as usual.
        return sql, list(params)

    translated = "".join(pieces)
    translated = translated.replace("%", "%%").replace("\x00", "%s")
    return translated, [params[position] for position in order]
