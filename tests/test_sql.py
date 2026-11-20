import pytest

from supabase_postgres_mcp.sql import bind_parameters, split_statements, validate_single_statement


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("", []),
        ("   ", []),
        ("select 1", ["select 1"]),
        ("select 1;", ["select 1"]),
        ("select ';' as semi", ["select ';' as semi"]),
        ('select 1 as "a;b"', ['select 1 as "a;b"']),
        ("select $$a;b$$", ["select $$a;b$$"]),
        ("select $tag$a;b$tag$", ["select $tag$a;b$tag$"]),
        ("select E'it\\'s; fine'", ["select E'it\\'s; fine'"]),
        ("select 1 /* ; */", ["select 1 /* ; */"]),
        ("select 1; -- comment; here\nselect 2", ["select 1", "-- comment; here\nselect 2"]),
        ("select 1; select 2;", ["select 1", "select 2"]),
    ],
)
def test_split_statements(sql, expected):
    assert split_statements(sql) == expected


def test_validate_accepts_one_statement():
    assert validate_single_statement("select 1") == "select 1"
    assert validate_single_statement("select 'commit; rollback'") == "select 'commit; rollback'"


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "  ;  ",
        "select 1; select 2",
        "commit",
        "  COMMIT ",
        "/* managed by server */ rollback",
        "begin",
        "copy public.widgets to stdout",
    ],
)
def test_validate_rejects(sql):
    with pytest.raises(ValueError):
        validate_single_statement(sql)


@pytest.mark.parametrize(
    ("sql", "params", "expected_sql", "expected_params"),
    [
        ("select $1", [42], "select %s", [42]),
        ("select $2, $1", [10, 20], "select %s, %s", [20, 10]),
        ("select $1, $1", ["x"], "select %s, %s", ["x", "x"]),
        ("select '$1', $1", ["x"], "select '$1', %s", ["x"]),
        ("select $$cost $1$$, $1", ["x"], "select $$cost $1$$, %s", ["x"]),
        ("select E'it\\'s $1', $1", ["x"], "select E'it\\'s $1', %s", ["x"]),
        ("select 1 -- $9\n, $1", ["x"], "select 1 -- $9\n, %s", ["x"]),
        ("select '100%', $1", ["x"], "select '100%%', %s", ["x"]),
        ("select 1", [1], "select 1", [1]),
        ("select %s", [1], "select %s", [1]),
    ],
)
def test_bind_parameters(sql, params, expected_sql, expected_params):
    assert bind_parameters(sql, params) == (expected_sql, expected_params)


@pytest.mark.parametrize("sql", ["select $2", "select $0", "select $99"])
def test_bind_parameters_rejects_missing_values(sql):
    with pytest.raises(ValueError):
        bind_parameters(sql, [1])
