"""Compare converted models with the dbt-labs/jaffle-shop reference models, row by row.

Both live in one DuckDB file (schemas `converted` and `reference`). For each pair in
reference_map.toml we join on the key and compare every mapped column:
numbers within a tolerance, booleans as 0/1, dates/timestamps as timestamps.

A difference is "explained" when reference_map.toml lists it under [models.X.expected]
with the reason (usually a defect in the PowerCenter mapping that the converter reproduces
on purpose). Anything else is "unexplained" and makes the run fail.

    uv run python verify/compare.py --db verify/work/jaffle.duckdb --report verify/REPORT.md
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import duckdb

HERE = Path(__file__).parent
TOLERANCE = 0.005  # half a cent: reference rounds money to 2 decimals


@dataclass
class ColumnResult:
    ours: str
    ref: str
    mismatches: int
    example: str = ""
    expected_reason: str = ""

    @property
    def status(self) -> str:
        if self.mismatches == 0:
            return "stale-expectation" if self.expected_reason else "match"
        return "explained" if self.expected_reason else "UNEXPLAINED"


@dataclass
class ModelResult:
    model: str
    reference: str
    rows_ours: int
    rows_ref: int
    only_ours: int
    only_ref: int
    rows_reason: str
    columns: list[ColumnResult] = field(default_factory=list)
    unmapped_ours: list[str] = field(default_factory=list)
    unmapped_ref: list[str] = field(default_factory=list)

    @property
    def rows_status(self) -> str:
        same = self.only_ours == 0 and self.only_ref == 0 and self.rows_ours == self.rows_ref
        if same:
            return "stale-expectation" if self.rows_reason else "match"
        return "explained" if self.rows_reason else "UNEXPLAINED"


def columns_of(con, schema: str, table: str) -> dict[str, str]:
    rows = con.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = ? and table_name = ? order by ordinal_position", [schema, table]).fetchall()
    return dict(rows)


def normalized(expr: str, data_type: str) -> str:
    t = data_type.upper()
    if t == "BOOLEAN":
        return f"cast({expr} as integer)"
    if t.startswith(("DECIMAL", "DOUBLE", "FLOAT", "INTEGER", "BIGINT", "HUGEINT", "SMALLINT", "TINYINT", "REAL")):
        return f"cast({expr} as double)"
    if t.startswith(("TIMESTAMP", "DATE")):
        return f"cast({expr} as timestamp)"
    return f"cast({expr} as varchar)"


def differs(a: str, b: str, ta: str, tb: str) -> str:
    na, nb = normalized(a, ta), normalized(b, tb)
    numeric = all(normalized("x", t).endswith("as double)") or t.upper() == "BOOLEAN" for t in (ta, tb))
    if numeric:
        return f"(({na}) is distinct from ({nb}) and coalesce(abs(({na}) - ({nb})) > {TOLERANCE}, true))"
    return f"(({na}) is distinct from ({nb}))"


def compare_model(con, name: str, spec: dict, schema: str = "converted",
                  expected_key: str = "expected") -> ModelResult:
    ref = spec.get("reference", name)
    keys = spec["key"]  # list of [ours, ref] or names
    keys = [(k, k) if isinstance(k, str) else tuple(k) for k in keys]
    renames = spec.get("columns", {})  # ours -> ref
    expected = spec.get(expected_key, {})  # ours column (or "_rows") -> reason
    ours_cols, ref_cols = columns_of(con, schema, name), columns_of(con, "reference", ref)
    if not ours_cols or not ref_cols:
        raise SystemExit(f"missing table {schema}.{name} or reference.{ref}; run dbt first")

    on = " and ".join(f"o.{a} = r.{b}" for a, b in keys)
    count = lambda sql: con.execute(sql).fetchall()[0][0]  # noqa: E731
    result = ModelResult(
        model=name, reference=ref,
        rows_ours=count(f"select count(*) from {schema}.{name}"),
        rows_ref=count(f"select count(*) from reference.{ref}"),
        only_ours=count(f"select count(*) from {schema}.{name} o anti join reference.{ref} r on {on}"),
        only_ref=count(f"select count(*) from reference.{ref} r anti join {schema}.{name} o on {on}"),
        rows_reason=expected.get("_rows", ""),
    )
    pairs = []
    for col in ours_cols:
        target = renames.get(col, col)
        if target in ref_cols and (col, target) not in keys:
            pairs.append((col, target))
    mapped_ref = {b for _, b in pairs} | {b for _, b in keys}
    result.unmapped_ours = [c for c in ours_cols if c not in {a for a, _ in pairs} | {a for a, _ in keys}]
    result.unmapped_ref = [c for c in ref_cols if c not in mapped_ref]

    for a, b in pairs:
        cond = differs(f"o.{a}", f"r.{b}", ours_cols[a], ref_cols[b])
        n = count(f"select count(*) from {schema}.{name} o join reference.{ref} r on {on} where {cond}")
        example = ""
        if n:
            key_sql = ", ".join(f"o.{k}" for k, _ in keys)
            row = con.execute(f"select {key_sql}, o.{a}, r.{b} from {schema}.{name} o join reference.{ref} r "
                              f"on {on} where {cond} order by {key_sql} limit 1").fetchall()[0]
            example = f"key={row[:-2]}: ours={row[-2]!r} ref={row[-1]!r}"
        result.columns.append(ColumnResult(a, b, n, example, expected.get(a, "")))
    return result


def report(results: list[ModelResult], title: str = "Converted vs reference", level: int = 1) -> str:
    h = "#" * level
    lines = [f"{h} {title}", "",
             "Generated by `verify/compare.py`. Rows are joined on the key; numbers compared with a "
             f"tolerance of {TOLERANCE}. `explained` = a known, documented difference "
             "(see `verify/reference_map.toml` for the reason); `UNEXPLAINED` = a failure.", "",
             "| Model | Rows (ours / ref) | Row check | Columns matching | Explained diffs | Unexplained |",
             "|---|---|---|---|---|---|"]
    for r in results:
        ok = sum(c.status == "match" for c in r.columns)
        exp = sum(c.status == "explained" for c in r.columns)
        bad = sum(c.status in ("UNEXPLAINED", "stale-expectation") for c in r.columns)
        lines.append(f"| `{r.model}` | {r.rows_ours} / {r.rows_ref} | {r.rows_status} | {ok}/{len(r.columns)} | "
                     f"{exp} | {bad} |")
    for r in results:
        lines += ["", f"{h}# `{r.model}` vs `reference.{r.reference}`", ""]
        reason = f": {r.rows_reason}" if r.rows_reason else ""
        lines.append(f"- Rows: ours {r.rows_ours}, reference {r.rows_ref}; keys only in ours {r.only_ours}, "
                     f"only in reference {r.only_ref} -> **{r.rows_status}**{reason}")
        for c in r.columns:
            name = c.ours if c.ours == c.ref else f"{c.ours} ~ {c.ref}"
            detail = f" ({c.mismatches} rows; {c.example})" if c.mismatches else ""
            reason = f" -- {c.expected_reason}" if c.expected_reason else ""
            lines.append(f"- `{name}`: **{c.status}**{detail}{reason}")
        if r.unmapped_ours:
            lines.append(f"- Only in converted (no reference column): {', '.join(r.unmapped_ours)}")
        if r.unmapped_ref:
            lines.append(f"- Only in reference (the mapping does not produce it): {', '.join(r.unmapped_ref)}")
    return "\n".join(lines) + "\n"


def failures(results: list[ModelResult]) -> list[str]:
    out = []
    for r in results:
        if r.rows_status in ("UNEXPLAINED", "stale-expectation"):
            out.append(f"{r.model}: rows {r.rows_status}")
        out += [f"{r.model}.{c.ours}: {c.status}" for c in r.columns
                if c.status in ("UNEXPLAINED", "stale-expectation")]
    return out


def run(db: Path, config: Path = HERE / "reference_map.toml", schema: str = "converted",
        expected_key: str = "expected") -> list[ModelResult]:
    spec = tomllib.loads(config.read_text())
    con = duckdb.connect(str(db), read_only=True)
    try:
        return [compare_model(con, name, s, schema, expected_key) for name, s in spec["models"].items()]
    finally:
        con.close()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=HERE / "work" / "jaffle.duckdb")
    p.add_argument("--config", type=Path, default=HERE / "reference_map.toml")
    p.add_argument("--schema", default="converted", help="schema holding the converted models")
    p.add_argument("--expected", default="expected", help="expectation table: expected or expected_repaired")
    p.add_argument("--title", default="Converted vs reference (dbt-labs/jaffle-shop)")
    p.add_argument("--report", type=Path)
    p.add_argument("--json", type=Path)
    args = p.parse_args()
    results = run(args.db, args.config, args.schema, args.expected)
    text = report(results, args.title)
    if args.report:
        args.report.write_text(text)
    if args.json:
        args.json.write_text(json.dumps([asdict(r) for r in results], indent=2, default=str))
    bad = failures(results)
    print(text if not args.report else f"wrote {args.report}")
    if bad:
        print("FAILED:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print(f"OK: {len(results)} models compared, all differences explained")
    return 0


if __name__ == "__main__":
    sys.exit(main())
