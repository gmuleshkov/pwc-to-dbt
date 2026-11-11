"""Test helpers: build small PowerCenter exports in code, and run converted SQL in DuckDB."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import quoteattr

import duckdb

from pwc_to_dbt import model
from pwc_to_dbt.convert import Project, convert


def P(name, porttype="INPUT/OUTPUT", datatype="string", expression=None, precision=None, scale=0, **extra):  # noqa: N802
    """A transformation port. Defaults: string pass-through port."""
    precision = precision if precision is not None else {"string": 50, "decimal": 12, "integer": 10,
                                                         "double": 15, "date/time": 29}.get(datatype, 10)
    if expression is None and "OUTPUT" in porttype and "INPUT" in porttype:
        expression = name
    return dict(NAME=name, PORTTYPE=porttype, DATATYPE=datatype, PRECISION=str(precision), SCALE=str(scale),
                EXPRESSION=expression, **extra)


@dataclass
class ExportBuilder:
    """Build a one-folder export. Instances are created for every definition automatically."""

    sources: list = field(default_factory=list)
    targets: list = field(default_factory=list)
    mappings: dict = field(default_factory=dict)

    def source(self, name, columns, owner="RAW"):
        self.sources.append((name, owner, columns))
        return self

    def target(self, name, columns):
        self.targets.append((name, columns))
        return self

    def mapping(self, name="m_test"):
        m = MappingBuilder(self, name)
        self.mappings[name] = m
        return m

    def xml(self) -> str:
        out = ['<?xml version="1.0" encoding="UTF-8"?>', "<POWERMART>",
               '<REPOSITORY NAME="R" DATABASETYPE="Oracle">', '<FOLDER NAME="F">']
        for name, owner, cols in self.sources:
            out.append(f'<SOURCE NAME="{name}" OWNERNAME="{owner}" DATABASETYPE="Oracle">')
            out += [_col("SOURCEFIELD", c) for c in cols]
            out.append("</SOURCE>")
        for name, cols in self.targets:
            out.append(f'<TARGET NAME="{name}" DATABASETYPE="Oracle">')
            out += [_col("TARGETFIELD", c) for c in cols]
            out.append("</TARGET>")
        for m in self.mappings.values():
            out.append(m.xml())
        out += ["</FOLDER>", "</REPOSITORY>", "</POWERMART>"]
        return "\n".join(out)

    def write(self, path: Path) -> Path:
        path.write_text(self.xml())
        return path

    def convert(self, tmp_path: Path, **kw) -> Project:
        folder = model.load(self.write(tmp_path / "export.xml"))[0]
        return convert(folder, **kw)


def _col(tag, c):
    name, dtype, prec, scale = (list(c) + [0, 0])[:4]
    key = 'KEYTYPE="PRIMARY KEY" NULLABLE="NOTNULL"' if len(c) > 4 and c[4] == "pk" else 'NULLABLE="NULL"'
    return f'<{tag} NAME="{name}" DATATYPE="{dtype}" PRECISION="{prec}" SCALE="{scale}" {key}/>'


@dataclass
class MappingBuilder:
    export: ExportBuilder
    name: str
    instances: list = field(default_factory=list)  # (name, type, definition, transformation type, sources)
    transformations: list = field(default_factory=list)
    connectors: list = field(default_factory=list)
    variables: list = field(default_factory=list)

    def use_source(self, source, sq_name=None, ports=None):
        """Add a Source instance plus its Source Qualifier, linked by port name."""
        cols = next(c for n, _, c in self.export.sources if n == source)
        sq = sq_name or f"SQ_{source}"
        types = {"varchar2": "string", "number": "decimal", "number(p,s)": "decimal", "date": "date/time",
                 "timestamp": "date/time", "integer": "integer"}
        ports = ports or [P(c[0], datatype=types.get(c[1], "string"), precision=c[2] or None,
                            scale=c[3] if len(c) > 3 else 0) for c in cols]
        self.instances.append((source, "SOURCE", source, "Source Definition", []))
        self.transformation(sq, "Source Qualifier", ports, sources=[source])
        for p in ports:
            self.link(source, sq, p["NAME"])
        return sq

    def use_target(self, target, instance=None):
        self.instances.append((instance or target, "TARGET", target, "Target Definition", []))
        return instance or target

    def transformation(self, name, ttype, ports, attrs=None, groups=None, deps=None, sources=None):
        self.transformations.append((name, ttype, ports, attrs or {}, groups or [], deps or []))
        self.instances.append((name, "TRANSFORMATION", name, ttype, sources or []))
        return name

    def link(self, src, dst, *fields):
        """link(A, B, "X") or link(A, B, ("X", "Y")) for X -> Y."""
        for f in fields:
            a, b = (f, f) if isinstance(f, str) else f
            self.connectors.append((src, a, dst, b))
        return self

    def variable(self, name, datatype, default):
        self.variables.append((name, datatype, default))
        return self

    def xml(self) -> str:
        out = [f'<MAPPING NAME="{self.name}" DESCRIPTION="">']
        for name, ttype, ports, attrs, groups, deps in self.transformations:
            out.append(f'<TRANSFORMATION NAME="{name}" TYPE="{ttype}">')
            for g in groups:
                expr = f' EXPRESSION={quoteattr(g[2])}' if len(g) > 2 and g[2] else ""
                out.append(f'<GROUP NAME="{g[0]}" TYPE="{g[1]}"{expr}/>')
            for p in ports:
                attrs_xml = " ".join(f"{k}={quoteattr(str(v))}" for k, v in p.items() if v is not None)
                out.append(f"<TRANSFORMFIELD {attrs_xml}/>")
            for k, v in attrs.items():
                out.append(f"<TABLEATTRIBUTE NAME={quoteattr(k)} VALUE={quoteattr(v)}/>")
            for i, o in deps:
                out.append(f'<FIELDDEPENDENCY INPUTFIELD="{i}" OUTPUTFIELD="{o}"/>')
            out.append("</TRANSFORMATION>")
        for name, itype, definition, ttype, sources in self.instances:
            out.append(f'<INSTANCE NAME="{name}" TYPE="{itype}" TRANSFORMATION_NAME="{definition}" '
                       f'TRANSFORMATION_TYPE="{ttype}">')
            out += [f'<ASSOCIATED_SOURCE_INSTANCE NAME="{s}"/>' for s in sources]
            out.append("</INSTANCE>")
        for a, af, b, bf in self.connectors:
            out.append(f'<CONNECTOR FROMINSTANCE="{a}" FROMFIELD="{af}" TOINSTANCE="{b}" TOFIELD="{bf}"/>')
        for name, dtype, default in self.variables:
            out.append(f'<MAPPINGVARIABLE NAME="{name}" DATATYPE="{dtype}" DEFAULTVALUE="{default}" ISPARAM="YES"/>')
        out.append("</MAPPING>")
        return "\n".join(out)


def run_project(project: Project, tables: dict[str, str]) -> duckdb.DuckDBPyConnection:
    """Create the raw tables, then every generated model as a view, resolving the dbt Jinja we emit.

    `tables` maps "raw.name" -> a SELECT that produces its rows.
    """
    con = duckdb.connect()
    for name, select in tables.items():
        schema = name.split(".")[0]
        con.execute(f"create schema if not exists {schema}")
        con.execute(f"create table {name} as {select}")
    for m in project.models:  # models come out in dependency order
        sql = jinja(m.sql, project)
        con.execute(f"create view {m.name} as {sql}")
    return con


def jinja(sql: str, project: Project) -> str:
    sql = re.sub(r"\{\{ source\('(\w+)', '(\w+)'\) \}\}", r"\1.\2", sql)
    sql = re.sub(r"\{\{ ref\('(\w+)'\) \}\}", r"\1", sql)
    sql = re.sub(r"\{\{ var\('(\w+)'\) \}\}", lambda m: project.vars[m.group(1)][0], sql)
    if "{{" in sql:
        raise AssertionError(f"unhandled jinja in:\n{sql}")
    return sql


def rows(con, sql: str) -> list[tuple]:
    return con.execute(sql).fetchall()
