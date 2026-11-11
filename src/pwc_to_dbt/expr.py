"""Informatica transformation-language expressions -> SQL (DuckDB dialect).

Three steps:
  tokenize()  text -> tokens
  parse()     tokens -> AST, using Informatica operator precedence (not SQL's)
  emit()      AST -> SQL text plus an inferred type

Identifiers (ports, variables, $$parameters) are resolved by a caller-supplied
resolver, so the transformation handlers decide what a name means (an upstream
column, an inlined variable, a lag() over the previous row, a dbt var, ...).

Anything we do not understand raises Unsupported. We never guess silently.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


class Unsupported(Exception):
    """Raised for syntax or functions the converter does not translate."""


# --------------------------------------------------------------------------- tokens

TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+|--[^\n]*|//[^\n]*)
  | (?P<str>'(?:[^']|'')*')
  | (?P<num>(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
  | (?P<op>\|\||<>|!=|\^=|<=|>=|[-+*/%=<>(),])
  | (?P<name>\$\$?[A-Za-z_][\w#@$]*|[A-Za-z_][\w#@$]*)
  | (?P<bad>:LKP\.|:SP\.|:MCR\.|:UDF\.|.)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str  # str, num, op, name, end
    text: str


def tokenize(text: str) -> list[Token]:
    tokens = []
    for m in TOKEN_RE.finditer(text):
        kind = m.lastgroup
        if kind == "ws":
            continue
        if kind == "bad":
            what = m.group()
            if what.startswith(":"):
                raise Unsupported(f"{what} call (unconnected lookup / stored procedure / macro) in: {text!r}")
            raise Unsupported(f"unexpected character {what!r} in: {text!r}")
        tokens.append(Token(kind, m.group()))
    tokens.append(Token("end", ""))
    return tokens


# --------------------------------------------------------------------------- AST


@dataclass(frozen=True)
class Num:
    text: str


@dataclass(frozen=True)
class Str:
    value: str


@dataclass(frozen=True)
class Const:
    name: str  # TRUE, FALSE, NULL


@dataclass(frozen=True)
class Name:
    name: str


@dataclass(frozen=True)
class Call:
    name: str  # upper-cased
    args: tuple
    star: bool = False  # COUNT(*)


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


# --------------------------------------------------------------------------- parser

# Informatica precedence, lowest first (Transformation Language Reference, "Operator precedence").
# Note unary NOT binds tighter than comparisons, unlike SQL; we always emit explicit parentheses.
BINARY_LEVELS = [
    {"OR"},
    {"AND"},
    {"=", "<>", "!=", "^="},
    {"<", "<=", ">", ">="},
    {"||"},
    {"+", "-"},
    {"*", "/", "%"},
]


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    def peek(self) -> Token:
        return self.tokens[self.i]

    def take(self) -> Token:
        tok = self.tokens[self.i]
        self.i += 1
        return tok

    def expect(self, text: str) -> None:
        tok = self.take()
        if tok.text != text:
            raise Unsupported(f"expected {text!r} but found {tok.text or 'end of expression'!r} in: {self.text!r}")

    def op_at(self, level: int) -> str | None:
        tok = self.peek()
        word = tok.text.upper() if tok.kind == "name" else tok.text
        if tok.kind in ("op", "name") and word in BINARY_LEVELS[level]:
            return word
        return None

    def parse(self):
        node = self.binary(0)
        if self.peek().kind != "end":
            raise Unsupported(f"unexpected {self.peek().text!r} in: {self.text!r}")
        return node

    def binary(self, level: int):
        if level == len(BINARY_LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while (op := self.op_at(level)) is not None:
            self.take()
            node = Binary(op, node, self.binary(level + 1))
        return node

    def unary(self):
        tok = self.peek()
        if tok.text in ("-", "+") or (tok.kind == "name" and tok.text.upper() == "NOT"):
            self.take()
            return Unary(tok.text.upper(), self.unary())
        return self.primary()

    def primary(self):
        tok = self.take()
        if tok.kind == "num":
            return Num(tok.text)
        if tok.kind == "str":
            return Str(tok.text[1:-1].replace("''", "'"))
        if tok.text == "(":
            node = self.binary(0)
            self.expect(")")
            return node
        if tok.kind == "name":
            upper = tok.text.upper()
            if upper in ("TRUE", "FALSE", "NULL"):
                return Const(upper)
            if self.peek().text == "(":
                self.take()
                if self.peek().text == "*":
                    self.take()
                    self.expect(")")
                    return Call(upper, (), star=True)
                args = []
                if self.peek().text != ")":
                    args.append(self.binary(0))
                    while self.peek().text == ",":
                        self.take()
                        args.append(self.binary(0))
                self.expect(")")
                return Call(upper, tuple(args))
            return Name(tok.text)
        raise Unsupported(f"unexpected {tok.text or 'end of expression'!r} in: {self.text!r}")


def parse(text: str):
    return _Parser(text).parse()


def names_in(node) -> list[str]:
    """All identifiers referenced by an expression, in order of appearance."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, Name):
            out.append(n.name)
        elif isinstance(n, Call):
            for a in n.args:
                walk(a)
        elif isinstance(n, Unary):
            walk(n.operand)
        elif isinstance(n, Binary):
            walk(n.left)
            walk(n.right)

    walk(node)
    return out


# --------------------------------------------------------------------------- emitter

NUMERIC = {"integer", "bigint", "decimal", "double"}


@dataclass(frozen=True)
class Typed:
    sql: str
    type: str  # string, integer, bigint, decimal, double, date, boolean, null, unknown


Resolver = Callable[[str], Typed]

# Informatica date format -> strftime/strptime. Longest tokens first.
DATE_FORMAT_TOKENS = [
    ("YYYY", "%Y"), ("MONTH", "%B"), ("HH24", "%H"), ("HH12", "%I"), ("YYY", "%Y"), ("MON", "%b"),
    ("DAY", "%A"), ("DDD", "%j"), ("RR", "%y"), ("YY", "%y"), ("MM", "%m"), ("DD", "%d"), ("DY", "%a"),
    ("HH", "%I"), ("MI", "%M"), ("SS", "%S"), ("MS", "%g"), ("US", "%f"), ("AM", "%p"), ("PM", "%p"),
]
DEFAULT_DATE_FORMAT = "MM/DD/YYYY HH24:MI:SS"  # Informatica session default

# TRUNC / DATE_DIFF / GET_DATE_PART format strings -> SQL date part
DATE_PARTS = {
    "Y": "year", "YY": "year", "YYY": "year", "YYYY": "year",
    "MM": "month", "MON": "month", "MONTH": "month",
    "D": "day", "DD": "day", "DDD": "day", "DY": "day", "DAY": "day",
    "HH": "hour", "HH12": "hour", "HH24": "hour",
    "MI": "minute", "SS": "second", "MS": "millisecond", "US": "microsecond",
}
SECONDS_PER = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}


def convert_date_format(fmt: str) -> str:
    out, i = [], 0
    while i < len(fmt):
        if fmt[i] == '"':  # quoted literal text
            j = fmt.find('"', i + 1)
            j = len(fmt) if j < 0 else j
            out.append(fmt[i + 1 : j].replace("%", "%%"))
            i = j + 1
            continue
        for token, repl in DATE_FORMAT_TOKENS:
            if fmt[i : i + len(token)].upper() == token:
                out.append(repl)
                i += len(token)
                break
        else:
            out.append("%%" if fmt[i] == "%" else fmt[i])
            i += 1
    return "".join(out)


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


AGGREGATES = {"SUM", "AVG", "MIN", "MAX", "COUNT", "MEDIAN", "STDDEV", "VARIANCE", "FIRST", "LAST"}


class Emitter:
    """Turns an AST into SQL. One instance per expression being translated."""

    def __init__(self, resolve: Resolver, warn: Callable[[str], None], allow_aggregates: bool = False):
        self.resolve = resolve
        self.warn = warn
        self.allow_aggregates = allow_aggregates
        self._in_aggregate = False

    # -- coercions: Informatica has no boolean type (TRUE is 1); SQL does.
    def as_bool(self, t: Typed) -> str:
        if t.type in NUMERIC:
            return f"({t.sql} <> 0)"
        return t.sql

    def as_value(self, t: Typed) -> Typed:
        if t.type == "boolean":
            return Typed(f"cast({t.sql} as integer)", "integer")
        return t

    def emit(self, node) -> Typed:
        if isinstance(node, Num):
            return Typed(node.text, "decimal" if "." in node.text or "e" in node.text.lower() else "integer")
        if isinstance(node, Str):
            return Typed(sql_string(node.value), "string")
        if isinstance(node, Const):
            return Typed(node.name.lower(), "null" if node.name == "NULL" else "boolean")
        if isinstance(node, Name):
            return self.resolve(node.name)
        if isinstance(node, Unary):
            inner = self.emit(node.operand)
            if node.op == "NOT":
                return Typed(f"(not {self.as_bool(inner)})", "boolean")
            inner = self.as_value(inner)
            return Typed(f"({node.op}{inner.sql})", inner.type)
        if isinstance(node, Binary):
            return self.binary(node)
        if isinstance(node, Call):
            return self.call(node)
        raise Unsupported(f"unknown expression node {node!r}")

    def binary(self, node: Binary) -> Typed:
        op = node.op
        left, right = self.emit(node.left), self.emit(node.right)
        if op in ("AND", "OR"):
            return Typed(f"({self.as_bool(left)} {op.lower()} {self.as_bool(right)})", "boolean")
        if op in ("=", "<>", "!=", "^=", "<", "<=", ">", ">="):
            if op == "=" and left.sql == right.sql:
                self.warn(f"comparison `{left.sql} = {right.sql}` compares a value with itself: always true "
                          "unless NULL (check variable-port order in the source mapping)")
            if left.type == "boolean" and right.type in NUMERIC:
                left = self.as_value(left)
            if right.type == "boolean" and left.type in NUMERIC:
                right = self.as_value(right)
            sql_op = "<>" if op in ("!=", "^=") else op
            return Typed(f"({left.sql} {sql_op} {right.sql})", "boolean")
        if op == "||":
            # Informatica treats NULL as empty in ||; SQL || would return NULL. concat() matches.
            return Typed(f"concat({left.sql}, {right.sql})", "string")
        left, right = self.as_value(left), self.as_value(right)
        if op == "/":
            # DuckDB '/' is float division, like Informatica. (Postgres would truncate integers.)
            return Typed(f"({left.sql} / {right.sql})", "double")
        return Typed(f"({left.sql} {op} {right.sql})", _numeric_result(left.type, right.type))

    # -- functions
    def args(self, node: Call, low: int, high: int | None = None) -> list[Typed]:
        high = low if high is None else high
        if not low <= len(node.args) <= high:
            raise Unsupported(f"{node.name} expects {low}..{high} arguments, got {len(node.args)}")
        return [self.emit(a) for a in node.args]

    def literal_arg(self, node: Call, index: int, default: str) -> str:
        if len(node.args) <= index:
            return default
        arg = node.args[index]
        if not isinstance(arg, Str):
            raise Unsupported(f"{node.name}: only a literal format string is supported as argument {index + 1}")
        return arg.value

    def call(self, node: Call) -> Typed:
        name = node.name
        if name in AGGREGATES:
            return self.aggregate(node)
        handler = getattr(self, f"fn_{name.lower()}", None)
        if handler is not None:
            return handler(node)
        simple = SIMPLE_FUNCTIONS.get(name)
        if simple is not None:
            sql_name, arity, result = simple
            args = self.args(node, *arity)
            values = [self.as_value(a) for a in args]
            rtype = values[0].type if result == "same" and values else result
            return Typed(f"{sql_name}({', '.join(a.sql for a in values)})", rtype)
        raise Unsupported(f"function {name}() is not supported")

    def aggregate(self, node: Call) -> Typed:
        if not self.allow_aggregates:
            raise Unsupported(f"aggregate function {node.name}() outside an Aggregator")
        if self._in_aggregate:
            raise Unsupported(f"nested aggregate {node.name}()")
        if node.star:
            if node.name != "COUNT":
                raise Unsupported(f"{node.name}(*)")
            return Typed("count(*)", "bigint")
        self._in_aggregate = True
        try:
            args = self.args(node, 1, 2)
        finally:
            self._in_aggregate = False
        value = self.as_value(args[0])
        sql_name = {"STDDEV": "stddev_samp", "VARIANCE": "var_samp"}.get(node.name, node.name.lower())
        if node.name in ("FIRST", "LAST"):
            self.warn(f"{node.name}() depends on row order; SQL {sql_name}() without ORDER BY is arbitrary")
        rtype = {"COUNT": "bigint", "AVG": "double", "MEDIAN": "double", "STDDEV": "double",
                 "VARIANCE": "double"}.get(node.name, value.type)
        sql = f"{sql_name}({value.sql})"
        if len(args) == 2:  # Informatica aggregate filter condition, e.g. SUM(x, x > 0)
            sql += f" filter (where {self.as_bool(args[1])})"
        return Typed(sql, rtype)

    def fn_iif(self, node: Call) -> Typed:
        args = self.args(node, 2, 3)
        cond, then = args[0], self.as_value(args[1])
        if len(args) == 3:
            other = self.as_value(args[2])
        else:  # Informatica default for the missing branch depends on the type of the first
            other = Typed({"string": "''", "date": "null"}.get(then.type, "0"), then.type)
        rtype = then.type if then.type not in ("null", "unknown") else other.type
        if then.type in NUMERIC and other.type in NUMERIC:
            rtype = _numeric_result(then.type, other.type)
        return Typed(f"case when {self.as_bool(cond)} then {then.sql} else {other.sql} end", rtype)

    def fn_decode(self, node: Call) -> Typed:
        if len(node.args) < 3:
            raise Unsupported("DECODE needs at least 3 arguments")
        args = [self.emit(a) for a in node.args]
        subject, rest = args[0], args[1:]
        default = rest.pop() if len(rest) % 2 == 1 else Typed("null", "null")
        whens = []
        for search, result in zip(rest[0::2], rest[1::2], strict=True):
            if subject.sql == "true":  # DECODE(TRUE, cond1, r1, ...) idiom
                whens.append(f"when {self.as_bool(search)} then {self.as_value(result).sql}")
            else:
                whens.append(f"when {subject.sql} = {search.sql} then {self.as_value(result).sql}")
        rtype = next((self.as_value(r).type for r in rest[1::2] if r.type != "null"), default.type)
        return Typed(f"case {' '.join(whens)} else {self.as_value(default).sql} end", rtype)

    def fn_isnull(self, node: Call) -> Typed:
        (x,) = self.args(node, 1)
        return Typed(f"({x.sql} is null)", "boolean")

    def fn_is_spaces(self, node: Call) -> Typed:
        (x,) = self.args(node, 1)
        return Typed(f"(length({x.sql}) > 0 and trim({x.sql}) = '')", "boolean")

    def fn_is_number(self, node: Call) -> Typed:
        (x,) = self.args(node, 1)
        return Typed(f"(try_cast(trim({x.sql}) as double) is not null)", "boolean")

    def fn_in(self, node: Call) -> Typed:
        args = self.args(node, 2, 99)
        return Typed(f"({args[0].sql} in ({', '.join(a.sql for a in args[1:])}))", "boolean")

    def _trim(self, node: Call, fn: str) -> Typed:
        args = self.args(node, 1, 2)
        return Typed(f"{fn}({', '.join(a.sql for a in args)})", "string")

    def fn_ltrim(self, node: Call) -> Typed:
        return self._trim(node, "ltrim")

    def fn_rtrim(self, node: Call) -> Typed:
        return self._trim(node, "rtrim")

    def fn_substr(self, node: Call) -> Typed:
        args = self.args(node, 2, 3)
        start = args[1]
        if start.sql == "0":  # Informatica treats start 0 as 1
            start = Typed("1", "integer")
        return Typed(f"substr({', '.join(a.sql for a in [args[0], start, *args[2:]])})", "string")

    def fn_concat(self, node: Call) -> Typed:
        a, b = self.args(node, 2)
        return Typed(f"concat({a.sql}, {b.sql})", "string")

    def fn_to_char(self, node: Call) -> Typed:
        x = self.emit(node.args[0]) if node.args else None
        if x is None or len(node.args) > 2:
            raise Unsupported("TO_CHAR expects 1 or 2 arguments")
        if x.type == "date":
            fmt = convert_date_format(self.literal_arg(node, 1, DEFAULT_DATE_FORMAT))
            return Typed(f"strftime({x.sql}, {sql_string(fmt)})", "string")
        if len(node.args) == 2:
            raise Unsupported("TO_CHAR(number, format)")
        return Typed(f"cast({x.sql} as varchar)", "string")

    def fn_to_date(self, node: Call) -> Typed:
        x = self.emit(node.args[0]) if node.args else None
        if x is None or len(node.args) > 2:
            raise Unsupported("TO_DATE expects 1 or 2 arguments")
        fmt = self.literal_arg(node, 1, DEFAULT_DATE_FORMAT)
        if "YY" in fmt.upper() and "YYYY" not in fmt.upper():
            self.warn(f"TO_DATE with 2-digit year format {fmt!r}: Informatica assumes the current century, "
                      "strptime %y maps 00-68 to 20xx and 69-99 to 19xx")
        return Typed(f"strptime({x.sql}, {sql_string(convert_date_format(fmt))})", "date")

    def _cast(self, node: Call, sql_type: str, rtype: str) -> Typed:
        x = self.emit(node.args[0]) if node.args else None
        if x is None:
            raise Unsupported(f"{node.name} needs an argument")
        return Typed(f"cast({self.as_value(x).sql} as {sql_type})", rtype)

    def fn_to_integer(self, node: Call) -> Typed:
        x = self.emit(node.args[0])
        return Typed(f"cast(trunc({self.as_value(x).sql}) as integer)", "integer")

    def fn_to_bigint(self, node: Call) -> Typed:
        x = self.emit(node.args[0])
        return Typed(f"cast(trunc({self.as_value(x).sql}) as bigint)", "bigint")

    def fn_to_float(self, node: Call) -> Typed:
        return self._cast(node, "double", "double")

    def fn_to_decimal(self, node: Call) -> Typed:
        scale = node.args[1].text if len(node.args) > 1 and isinstance(node.args[1], Num) else "0"
        return self._cast(node, f"decimal(28, {scale})", "decimal")

    def fn_trunc(self, node: Call) -> Typed:
        x = self.emit(node.args[0]) if node.args else None
        if x is None or len(node.args) > 2:
            raise Unsupported("TRUNC expects 1 or 2 arguments")
        if x.type == "date":
            part = DATE_PARTS.get(self.literal_arg(node, 1, "DD").upper())
            if part is None:
                raise Unsupported(f"TRUNC date format {self.literal_arg(node, 1, 'DD')!r}")
            return Typed(f"date_trunc('{part}', {x.sql})", "date")
        if x.type in NUMERIC:
            if len(node.args) == 2:
                raise Unsupported("TRUNC(number, precision)")
            return Typed(f"trunc({x.sql})", x.type)
        raise Unsupported(f"TRUNC of a value whose type is unknown ({x.sql}): cannot tell date from number")

    def fn_round(self, node: Call) -> Typed:
        args = self.args(node, 1, 2)
        if args[0].type == "date":
            raise Unsupported("ROUND(date)")
        return Typed(f"round({', '.join(self.as_value(a).sql for a in args)})", args[0].type)

    def fn_date_diff(self, node: Call) -> Typed:
        if len(node.args) != 3:
            raise Unsupported("DATE_DIFF expects 3 arguments")
        a, b = self.emit(node.args[0]), self.emit(node.args[1])
        part = DATE_PARTS.get(self.literal_arg(node, 2, "DD").upper())
        if part is None:
            raise Unsupported(f"DATE_DIFF format {node.args[2]!r}")
        # Informatica returns a fractional difference (a - b). Two day-truncated dates differ by whole
        # days, so date_diff() is exact there and easier to read.
        both_truncated = all(s.sql.startswith("date_trunc('day'") for s in (a, b))
        if part == "day" and both_truncated:
            return Typed(f"date_diff('day', {b.sql}, {a.sql})", "bigint")
        if part in SECONDS_PER:
            return Typed(f"((epoch({a.sql}) - epoch({b.sql})) / {SECONDS_PER[part]})", "double")
        self.warn(f"DATE_DIFF in {part}s: Informatica returns a fractional value, date_diff() counts boundaries")
        return Typed(f"date_diff('{part}', {b.sql}, {a.sql})", "bigint")

    def fn_add_to_date(self, node: Call) -> Typed:
        if len(node.args) != 3:
            raise Unsupported("ADD_TO_DATE expects 3 arguments")
        d, amount = self.emit(node.args[0]), self.emit(node.args[2])
        part = DATE_PARTS.get(self.literal_arg(node, 1, "DD").upper())
        if part is None:
            raise Unsupported(f"ADD_TO_DATE format {node.args[1]!r}")
        return Typed(f"({d.sql} + ({amount.sql}) * interval 1 {part})", "date")

    def fn_get_date_part(self, node: Call) -> Typed:
        if len(node.args) != 2:
            raise Unsupported("GET_DATE_PART expects 2 arguments")
        d = self.emit(node.args[0])
        part = DATE_PARTS.get(self.literal_arg(node, 1, "DD").upper())
        if part is None:
            raise Unsupported(f"GET_DATE_PART format {node.args[1]!r}")
        return Typed(f"date_part('{part}', {d.sql})", "integer")

    def fn_reg_match(self, node: Call) -> Typed:
        s, p = self.args(node, 2)
        return Typed(f"regexp_full_match({s.sql}, {p.sql})", "boolean")

    def fn_reg_replace(self, node: Call) -> Typed:
        s, p, r = self.args(node, 3)
        return Typed(f"regexp_replace({s.sql}, {p.sql}, {r.sql}, 'g')", "string")

    def fn_mod(self, node: Call) -> Typed:
        a, b = (self.as_value(x) for x in self.args(node, 2))
        return Typed(f"({a.sql} % {b.sql})", _numeric_result(a.type, b.type))

    def fn_error(self, node: Call) -> Typed:
        (msg,) = self.args(node, 1)
        return Typed(f"error({msg.sql})", "null")

    fn_abort = fn_error


# name -> (sql function, (min args, max args), result type or "same" as first argument)
SIMPLE_FUNCTIONS = {
    "UPPER": ("upper", (1, 1), "string"),
    "LOWER": ("lower", (1, 1), "string"),
    "LENGTH": ("length", (1, 1), "integer"),
    "LPAD": ("lpad", (3, 3), "string"),
    "RPAD": ("rpad", (3, 3), "string"),
    "INSTR": ("instr", (2, 2), "integer"),
    "REVERSE": ("reverse", (1, 1), "string"),
    "CHR": ("chr", (1, 1), "string"),
    "ASCII": ("ascii", (1, 1), "integer"),
    "ABS": ("abs", (1, 1), "same"),
    "CEIL": ("ceil", (1, 1), "same"),
    "FLOOR": ("floor", (1, 1), "same"),
    "SIGN": ("sign", (1, 1), "integer"),
    "POWER": ("power", (2, 2), "double"),
    "SQRT": ("sqrt", (1, 1), "double"),
    "EXP": ("exp", (1, 1), "double"),
    "LN": ("ln", (1, 1), "double"),
    "LOG": ("log", (2, 2), "double"),
    "LAST_DAY": ("last_day", (1, 1), "date"),
}


def _numeric_result(a: str, b: str) -> str:
    for t in ("double", "decimal", "bigint", "integer"):
        if t in (a, b):
            return t
    return "unknown"


def to_sql(text: str, resolve: Resolver, warn: Callable[[str], None], allow_aggregates: bool = False) -> Typed:
    """Translate one Informatica expression to SQL."""
    return Emitter(resolve, warn, allow_aggregates).emit(parse(text))
