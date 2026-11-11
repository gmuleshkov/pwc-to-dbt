"""Expression language: parsing, precedence, SQL output and (executed) semantics."""

import duckdb
import pytest

from pwc_to_dbt import expr
from pwc_to_dbt.expr import Binary, Call, Name, Typed, Unary, Unsupported

TYPES = {"S": "string", "N": "integer", "D": "decimal", "F": "double", "DT": "date", "X": "decimal", "Y": "decimal"}


def sql(text, allow_aggregates=False, warnings=None):
    def resolve(name):
        if name.upper() not in TYPES:
            raise Unsupported(f"unknown {name}")
        return Typed(name.lower(), TYPES[name.upper()])

    sink = warnings if warnings is not None else []
    return expr.to_sql(text, resolve, sink.append, allow_aggregates).sql


def value(sql_text):
    return duckdb.sql(f"select {sql_text}").fetchall()[0][0]


# -- parsing


def test_precedence_follows_informatica():
    assert expr.parse("1 + 2 * 3") == Binary("+", expr.Num("1"), Binary("*", expr.Num("2"), expr.Num("3")))
    # || binds looser than + and tighter than comparisons
    assert expr.parse("A || B = C") == Binary("=", Binary("||", Name("A"), Name("B")), Name("C"))
    # AND binds tighter than OR
    assert expr.parse("A OR B AND C") == Binary("OR", Name("A"), Binary("AND", Name("B"), Name("C")))
    # Informatica puts unary NOT above comparisons (unlike SQL); we keep that and emit explicit parentheses
    assert expr.parse("NOT A = B") == Binary("=", Unary("NOT", Name("A")), Name("B"))


def test_parses_calls_strings_comments_and_star():
    node = expr.parse("IIF(ISNULL(X), 'it''s', 'no') -- trailing comment")
    assert node == Call("IIF", (Call("ISNULL", (Name("X"),)), expr.Str("it's"), expr.Str("no")))
    assert expr.parse("COUNT(*)") == Call("COUNT", (), star=True)
    assert expr.parse("$$RUN_DATE") == Name("$$RUN_DATE")


@pytest.mark.parametrize("bad", ["IIF(A, 1", "A +", ":LKP.LKP_CUSTOMER(ID)", "A ? B"])
def test_bad_syntax_is_unsupported_not_guessed(bad):
    with pytest.raises(Unsupported):
        expr.parse(bad)


# -- SQL output


@pytest.mark.parametrize(
    "informatica, expected",
    [
        ("IIF(ISNULL(X), 0, X)", "case when (x is null) then 0 else x end"),
        ("IIF(N >= 5, 3, IIF(N >= 2, 2, 1))",
         "case when (n >= 5) then 3 else case when (n >= 2) then 2 else 1 end end"),
        ("UPPER(LTRIM(RTRIM(S))) = 'JAFFLE'", "(upper(ltrim(rtrim(s))) = 'JAFFLE')"),
        ("TRUNC(DT)", "date_trunc('day', dt)"),
        ("TRUNC(DT, 'MM')", "date_trunc('month', dt)"),
        ("TRUNC(D)", "trunc(d)"),
        ("TO_CHAR(DT, 'DD-MM-YY')", "strftime(dt, '%d-%m-%y')"),
        ("TO_DATE(S, 'MM/DD/YYYY HH24:MI:SS')", "strptime(s, '%m/%d/%Y %H:%M:%S')"),
        ("DATE_DIFF(TRUNC(DT), TRUNC(DT), 'DD')", "date_diff('day', date_trunc('day', dt), date_trunc('day', dt))"),
        ("DATE_DIFF(DT, DT, 'HH')", "((epoch(dt) - epoch(dt)) / 3600)"),
        ("S || 'x'", "concat(s, 'x')"),
        ("N <> 1 AND N != 2", "((n <> 1) and (n <> 2))"),
        ("DECODE(N, 1, 'one', 2, 'two', 'many')", "case when n = 1 then 'one' when n = 2 then 'two' else 'many' end"),
        ("IIF(N, 1, 0)", "case when (n <> 0) then 1 else 0 end"),  # numeric condition: non-zero is true
    ],
)
def test_translation(informatica, expected):
    assert sql(informatica) == expected


def test_aggregates_only_inside_aggregator():
    assert sql("SUM(IIF(S = 'HIGH', 1, 0))", allow_aggregates=True) == "sum(case when (s = 'HIGH') then 1 else 0 end)"
    assert sql("COUNT(*)", allow_aggregates=True) == "count(*)"
    assert sql("SUM(D, D > 0)", allow_aggregates=True) == "sum(d) filter (where (d > 0))"
    with pytest.raises(Unsupported):
        sql("SUM(D)")
    with pytest.raises(Unsupported):
        sql("SUM(MAX(D))", allow_aggregates=True)


def test_unknown_function_is_unsupported():
    with pytest.raises(Unsupported, match="SOUNDEX"):
        sql("SOUNDEX(S)")


def test_self_comparison_warns():
    warnings = []
    sql("IIF(S = S, 1, 0)", warnings=warnings)
    assert any("compares a value with itself" in w for w in warnings)


def test_trunc_of_unknown_type_refuses_to_guess():
    def resolve(name):
        return Typed(name.lower(), "unknown")

    with pytest.raises(Unsupported, match="cannot tell date from number"):
        expr.to_sql("TRUNC(Z)", resolve, lambda _: None)


# -- semantics, executed in DuckDB


def test_division_is_not_integer_division():
    assert value(sql("7 / 2")) == 3.5


def test_concat_ignores_null_like_informatica():
    assert value(sql("'a' || NULL")) == "a"


def test_iif_without_else_defaults_by_type():
    assert value(sql("IIF(1 = 2, 'x')")) == ""
    assert value(sql("IIF(1 = 2, 5)")) == 0


def test_null_condition_takes_the_else_branch():
    assert value(sql("IIF(NULL = 1, 'then', 'else')")) == "else"


def test_two_digit_year_round_trip_stays_in_this_century():
    out = value(sql("TO_DATE(TO_CHAR(TO_DATE('2024-09-01', 'YYYY-MM-DD'), 'DD-MM-YY'), 'DD-MM-YY')"))
    assert str(out).startswith("2024-09-01")


def test_date_format_conversion():
    assert expr.convert_date_format('YYYY-MM-DD"T"HH24:MI') == "%Y-%m-%dT%H:%M"
    assert expr.convert_date_format("MON DD, RR") == "%b %d, %y"
