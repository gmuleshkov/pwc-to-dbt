"""Mapping graph -> dbt models.

Every transformation instance becomes one CTE (Routers become one CTE per output group).
Each CTE exposes the transformation's output ports as columns, so a CONNECTOR
(instance.port -> instance.port) is just "read column X of CTE Y".

A dbt model is created for every Target, plus (by default) an intermediate model for
every node whose output is shared by several targets, so shared logic is written once.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import expr
from .expr import Typed, Unsupported
from .model import Folder, Instance, Mapping, Port, TableDef, Transformation

# --------------------------------------------------------------------------- naming & types

RESERVED = set(
    """all analyse analyze and any array as asc asymmetric both case cast check collate column constraint
    create default deferrable desc describe distinct do else end except false fetch for foreign from grant
    group having in initially intersect into lateral leading limit not null offset on only or order pivot
    placing primary qualify references returning select show some summarize symmetric table then to
    trailing true union unique unpivot using variadic when where window with""".split()
)


def ident(name: str) -> str:
    """Informatica names are case-insensitive; emit them lower-case, quoted only when needed."""
    low = name.lower()
    if re.fullmatch(r"[a-z_][a-z0-9_]*", low) and low not in RESERVED:
        return low
    return '"' + low.replace('"', '""') + '"'


# transformation datatype -> (expression type, SQL type)
def port_types(port: Port) -> tuple[str, str]:
    d = port.datatype.lower()
    if d in ("string", "nstring", "text", "ntext"):
        return "string", "varchar"
    if d in ("integer", "small integer"):
        return "integer", "integer"
    if d == "bigint":
        return "bigint", "bigint"
    if d == "decimal":
        if 0 < port.precision <= 38:
            return "decimal", f"decimal({port.precision}, {port.scale})"
        return "double", "double"
    if d in ("double", "real"):
        return "double", "double"
    if d == "date/time":
        return "date", "timestamp"
    return "unknown", ""


# native (database) column type -> SQL type, or "" when unknown
def column_sql_type(datatype: str, precision: int, scale: int) -> str:
    d = datatype.lower()
    if d in ("varchar2", "nvarchar2", "varchar", "char", "nchar", "clob", "nclob", "long", "string", "text"):
        return "varchar"
    if d.startswith("number") or d in ("numeric", "decimal"):
        if 0 < precision <= 38:
            return f"decimal({precision}, {scale})"
        return "double"
    if d in ("integer", "int", "smallint"):
        return "integer"
    if d == "bigint":
        return "bigint"
    if d in ("float", "binary_float", "binary_double", "double", "real"):
        return "double"
    if d in ("date", "datetime") or d.startswith("timestamp"):
        return "timestamp"
    return ""


SQL_TO_EXPR_TYPE = {"varchar": "string", "integer": "integer", "bigint": "bigint", "double": "double",
                    "timestamp": "date"}


def expr_type_of_sql(sql_type: str) -> str:
    return "decimal" if sql_type.startswith("decimal") else SQL_TO_EXPR_TYPE.get(sql_type, "unknown")


def convert_default_date(value: str) -> str:
    """'12/31/2025 00:00:00' (Informatica MM/DD/YYYY HH24:MI:SS) -> '2025-12-31 00:00:00'."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:\s+(\d{1,2}):(\d{2}):(\d{2})(?:\.\d+)?)?", value.strip())
    if not m:
        return value
    mo, d, y, hh, mi, ss = m.groups()
    return f"{y}-{int(mo):02d}-{int(d):02d} {int(hh or 0):02d}:{mi or '00'}:{ss or '00'}"


def redact(text: str) -> str:
    """Hide credentials such as `user/password@host` found in workflow command lines."""
    return re.sub(r"(\b\w+)/[^@\s/]+@", r"\1/<redacted>@", text)


# --------------------------------------------------------------------------- results


@dataclass
class Issue:
    level: str  # "warning" or "unsupported"
    mapping: str
    instance: str
    message: str


@dataclass
class Rel:
    """Something a downstream node can read from: a CTE or a ref() to another model."""

    relation: str
    columns: dict[str, str]  # PORT NAME (upper) -> column name in the relation
    types: dict[str, str]  # PORT NAME (upper) -> expression type
    ordering: list[tuple[str, str]] = field(default_factory=list)  # (PORT NAME, "asc"/"desc")


@dataclass
class ModelColumn:
    name: str
    data_type: str
    description: str = ""
    tests: list[str] = field(default_factory=list)


@dataclass
class GeneratedModel:
    name: str
    folder: str  # path below models/
    kind: str  # "target" or "intermediate"
    mapping: str
    instance: str
    sql: str
    columns: list[ModelColumn]
    description: str
    issues: list[Issue]
    transformations: list[tuple[str, str]]  # (instance, type) in CTE order


@dataclass
class SourceTable:
    source: str  # dbt source name
    schema: str
    table: TableDef


@dataclass
class Project:
    models: list[GeneratedModel]
    sources: dict[str, SourceTable]  # by table name
    vars: dict[str, tuple[str, str]]  # var name -> (default, comment)
    issues: list[Issue]
    folder: Folder
    mapping_names: list[str]


class NodeError(Exception):
    """A transformation cannot be converted; the whole model becomes a compile-error stub."""


# --------------------------------------------------------------------------- per-mapping conversion

CHEAP_TYPES = {"Source Qualifier", "Sorter", "Router"}  # never worth their own intermediate model


def is_union(t: Transformation) -> bool:
    return t.type == "Custom Transformation" and (
        t.attr("Module Identifier") == "pmuniontrans" or t.attr("Function Identifier") == "pmunionfunc"
    )


class MappingConverter:
    def __init__(self, project: ProjectBuilder, mapping: Mapping, intermediate_models: bool):
        self.p = project
        self.m = mapping
        self.intermediate_models = intermediate_models
        self.incoming: dict[str, list] = defaultdict(list)
        self.outgoing: dict[str, list] = defaultdict(list)
        for c in mapping.connectors:
            self.incoming[c.to_instance].append(c)
            self.outgoing[c.from_instance].append(c)
        self.issues: list[Issue] = []
        self.boundary_rels: dict[str, Rel] = {}  # instance -> Rel pointing at its model
        self.folder_name = re.sub(r"^m_", "", mapping.name, flags=re.I).lower()

    # -- graph helpers
    def inst(self, name: str) -> Instance:
        return self.m.instances[name]

    def ttype(self, name: str) -> str:
        i = self.inst(name)
        return self.m.transformation_of(name).type if i.type == "TRANSFORMATION" else i.type

    def upstream(self, name: str) -> list[str]:
        return list(dict.fromkeys(c.from_instance for c in self.incoming[name]))

    def downstream(self, name: str) -> list[str]:
        return list(dict.fromkeys(c.to_instance for c in self.outgoing[name]))

    def targets_reached(self) -> dict[str, frozenset]:
        memo: dict[str, frozenset] = {}

        def walk(n: str) -> frozenset:
            if n not in memo:
                memo[n] = frozenset()  # cycle guard
                if self.inst(n).type == "TARGET":
                    memo[n] = frozenset([n])
                else:
                    memo[n] = frozenset().union(*[walk(d) for d in self.downstream(n)])
            return memo[n]

        for n in self.m.instances:
            walk(n)
        return memo

    def topo_order(self) -> list[str]:
        order, seen = [], set()

        def visit(n: str):
            if n in seen:
                return
            seen.add(n)
            for u in self.upstream(n):
                visit(u)
            order.append(n)

        for n in self.m.instances:
            visit(n)
        return order

    def boundaries(self) -> list[str]:
        """Instances that become their own dbt model, in dependency order."""
        reached = self.targets_reached()
        chosen = {n for n, i in self.m.instances.items() if i.type == "TARGET"}
        for n, i in self.m.instances.items():
            if i.type == "TARGET" and len(self.upstream(n)) == 0:
                self.warn(n, "target has no incoming connectors; it is skipped")
                chosen.discard(n)
        if self.intermediate_models:
            for n, i in self.m.instances.items():
                if i.type != "TRANSFORMATION" or len(reached[n]) < 2:
                    continue
                consumers = self.downstream(n)
                if len(consumers) > 1 or any(reached[c] != reached[n] for c in consumers):
                    # Last node of a segment shared by several targets. Cheap nodes push the
                    # boundary up to their (single) input; a plain Source Qualifier is just duplicated.
                    b = n
                    while (self.ttype(b) in CHEAP_TYPES - {"Source Qualifier"}) and len(self.upstream(b)) == 1:
                        b = self.upstream(b)[0]
                    if self.ttype(b) not in CHEAP_TYPES and self.inst(b).type == "TRANSFORMATION":
                        chosen.add(b)
        return [n for n in self.topo_order() if n in chosen]

    def warn(self, instance: str, message: str) -> None:
        self.issues.append(Issue("warning", self.m.name, instance, message))

    def lint_crossed_links(self) -> None:
        """Flag X.A -> Y.B links where X also has a port B that goes to a different port of Y.

        Renames (ID -> CUSTOMER_ID) are normal; swapped pairs such as SUBTOTAL -> SUBTOTAL_CENTS
        while SUBTOTAL_CENTS -> SUBTOTAL are almost always a linking mistake. We still convert the
        links as they are (faithful), but say so loudly.
        """
        by_pair = defaultdict(dict)
        for c in self.m.connectors:
            by_pair[(c.from_instance, c.to_instance)][c.from_field.upper()] = c.to_field.upper()
        for (src, dst), links in by_pair.items():
            for f, g in links.items():
                if f != g and g in links and links[g] != g:
                    self.warn(dst, f"probable crossed link: {src}.{f} -> {dst}.{g} while {src}.{g} -> "
                                   f"{dst}.{links[g]}; converted as linked")

    # -- driver
    def convert(self) -> list[GeneratedModel]:
        self.lint_crossed_links()
        models = []
        for b in self.boundaries():
            models.append(self.build_model(b))
        return models

    def model_name_for(self, boundary: str) -> tuple[str, str, str]:
        i = self.inst(boundary)
        if i.type == "TARGET":
            return self.p.target_model_name(self.m.name, boundary), self.folder_name, "target"
        return f"int_{self.folder_name}__{boundary.lower()}", f"{self.folder_name}/intermediate", "intermediate"

    def build_model(self, boundary: str) -> GeneratedModel:
        name, folder, kind = self.model_name_for(boundary)
        builder = ModelBuilder(self, boundary)
        issues_before = len(self.issues)
        try:
            sql, columns, final_rel = builder.build()
        except (NodeError, Unsupported) as e:
            node = builder.current or boundary
            self.issues.append(Issue("unsupported", self.m.name, node, str(e)))
            sql = stub_sql(self.m.name, boundary, node, str(e))
            columns, final_rel = [], None
        in_model = set(builder.order)
        model_issues = []
        for i in self.issues[issues_before:] + [i for i in self.issues[:issues_before] if i.instance in in_model]:
            if i not in model_issues:
                model_issues.append(i)
        header = model_header(self.m.name, boundary, self.inst(boundary), builder.order, model_issues)
        if kind == "target":
            columns = self.target_columns(boundary)
        if final_rel is not None and kind == "intermediate":
            self.boundary_rels[boundary] = Rel(
                relation="{{ ref('" + name + "') }}",
                columns={k: ident(k) for k in final_rel.columns},
                types=dict(final_rel.types),
                ordering=list(final_rel.ordering),
            )
        flow = " -> ".join(builder.order)
        description = (f"Converted from PowerCenter mapping {self.m.name}, "
                       + (f"target {self.inst(boundary).definition}" if kind == "target"
                          else f"output of shared transformation {boundary}")
                       + f". Flow: {flow}.")
        return GeneratedModel(
            name=name, folder=folder, kind=kind, mapping=self.m.name, instance=boundary,
            sql=header + sql, columns=columns, description=description, issues=model_issues,
            transformations=[(n, self.ttype(n)) for n in builder.order],
        )

    def target_columns(self, target_instance: str) -> list[ModelColumn]:
        tdef = self.p.folder.targets[self.inst(target_instance).definition]
        cols = []
        for c in tdef.columns:
            tests = []
            if not c.nullable:
                tests.append("not_null")
            if c.keytype in ("PRIMARY KEY", "PRIMARY FOREIGN KEY"):
                if sum(x.keytype in ("PRIMARY KEY", "PRIMARY FOREIGN KEY") for x in tdef.columns) == 1:
                    tests += ["unique"] + ([] if "not_null" in tests else ["not_null"])
            sql_type = column_sql_type(c.datatype, c.precision, c.scale)
            cols.append(ModelColumn(c.name.lower(), sql_type, c.description, tests))
        return cols


def stub_sql(mapping: str, boundary: str, node: str, message: str) -> str:
    msg = f"pwc-to-dbt could not convert {node} in mapping {mapping} (needed by {boundary}): {message}"
    msg = msg.replace("\\", "\\\\").replace('"', '\\"')
    return "{{ exceptions.raise_compiler_error(\"" + msg + "\") }}\n"


def model_header(mapping: str, boundary: str, inst: Instance, order: list[str], issues: list[Issue]) -> str:
    what = (f"target {inst.definition} (instance {boundary})" if inst.type == "TARGET"
            else f"shared transformation {boundary} (used by several targets)")
    lines = [f"-- Generated by pwc-to-dbt from PowerCenter mapping {mapping}: {what}.",
             "-- Do not edit by hand; change the mapping or the converter and regenerate."]
    if order:
        lines.append("-- Flow: " + " -> ".join(order))
    for i in issues:
        lines.append(f"-- {i.level.upper()} [{i.instance}]: {one_line(i.message)}")
    return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- one model


class ModelBuilder:
    def __init__(self, conv: MappingConverter, boundary: str):
        self.c = conv
        self.m = conv.m
        self.boundary = boundary
        self.rels: dict[str, dict[str | None, Rel]] = {}
        self.ctes: list[tuple[str, str]] = []  # (name, sql)
        self.order: list[str] = []
        self.current: str | None = None

    # -- assembling
    def build(self) -> tuple[str, list[ModelColumn], Rel]:
        self.visit(self.boundary)
        final = self.rels[self.boundary][None]
        body = ",\n\n".join(f"{ident(n)} as (\n{indent(sql)}\n)" for n, sql in self.ctes)
        sql = f"with\n\n{body}\n\nselect * from {final.relation}\n"
        cols = [ModelColumn(ident(p).strip('"'), "") for p in final.columns]
        return sql, cols, final

    def visit(self, n: str) -> None:
        if n in self.rels:
            return
        if n != self.boundary and n in self.c.boundary_rels:
            self.rels[n] = {None: self.c.boundary_rels[n]}
            return
        inst = self.c.inst(n)
        if inst.type == "SOURCE":
            return  # read directly by its Source Qualifier
        for u in self.c.upstream(n):
            self.visit(u)
        self.current = n
        self.order.append(n)
        if inst.type == "TARGET":
            self.rels[n] = {None: self.target(n)}
            return
        if inst.type != "TRANSFORMATION" or inst.definition not in self.m.transformations:
            raise NodeError(f"instance {n} of type {inst.type or '?'} ({inst.transformation_type}) is not supported "
                            "(mapplets and shortcuts must be expanded first)")
        t = self.m.transformation_of(n)
        handler = HANDLERS.get(t.type)
        if t.type == "Custom Transformation" and is_union(t):
            handler = ModelBuilder.union
        if handler is None:
            raise NodeError(f"transformation type '{t.type}' is not supported")
        self.rels[n] = handler(self, n, t)

    def add_cte(self, name: str, comment: str, sql: str) -> str:
        self.ctes.append((name, f"-- {one_line(comment)}\n{sql}"))
        return ident(name)

    def warn(self, message: str) -> None:
        self.c.warn(self.current or self.boundary, message)

    # -- inputs
    def rel_of(self, instance: str, port: str) -> Rel:
        rels = self.rels[instance]
        if None in rels:
            return rels[None]
        group = self.m.transformation_of(instance).port(port).group
        if group not in rels:
            raise NodeError(f"{instance}: group {group} is not generated")
        return rels[group]

    def input_columns(self, n: str, ports: list[str] | None = None) -> dict[str, tuple[Rel, str]]:
        """PORT (upper) of node n -> (upstream relation, column) for every connected input port."""
        out = {}
        for c in self.c.incoming[n]:
            if ports is not None and c.to_field not in ports:
                continue
            rel = self.rel_of(c.from_instance, c.from_field)
            out[c.to_field.upper()] = (rel, rel.columns[c.from_field.upper()])
        return out

    def single_input(self, n: str) -> tuple[Rel, dict[str, str]]:
        cols = self.input_columns(n)
        rels = {id(r): r for r, _ in cols.values()}
        if len(rels) != 1:
            raise NodeError(f"{n} reads from {len(rels)} pipelines; only one is supported for this type")
        rel = next(iter(rels.values()))
        return rel, {p: col for p, (_, col) in cols.items()}

    def map_ordering(self, rel: Rel, colmap: dict[str, str], passthrough: set[str]) -> list[tuple[str, str]]:
        """Carry the upstream row order through ports that are passed on unchanged."""
        by_col = {col: port for port, col in colmap.items() if port in passthrough}
        out = []
        for port, direction in rel.ordering:
            mine = by_col.get(rel.columns[port])
            if mine is None:
                break  # order is only known for the prefix we still carry
            out.append((mine, direction))
        return out

    def resolver(self, n: str, columns: dict[str, str], types: dict[str, str]):
        variables = {v.name.upper(): v for v in self.m.variables}
        inst = self.c.inst(n)
        ports = ({p.name.upper(): p for p in self.m.transformation_of(n).ports if p.is_input}
                 if inst.type == "TRANSFORMATION" else {})

        def resolve(name: str) -> Typed:
            key = name.upper()
            if key in columns:
                return Typed(columns[key], types.get(key, "unknown"))
            if key in ports:  # an input port with no incoming link is NULL in PowerCenter
                self.warn(f"input port {name} is not connected; it is NULL")
                etype, sql_type = port_types(ports[key])
                return Typed(f"cast(null as {sql_type or 'varchar'})", etype)
            if key in variables:
                return self.c.p.mapping_var(self.m.name, variables[key])
            if key in ("SYSDATE", "SESSSTARTTIME"):
                self.warn(f"{name} is the session start time; using current_timestamp")
                return Typed("current_timestamp", "date")
            if key.startswith("$"):
                raise NodeError(f"parameter {name} is not a mapping variable of {self.m.name}")
            raise NodeError(f"{n}: unknown port {name}")

        return resolve

    def translate(self, text: str, resolve, allow_aggregates: bool = False) -> Typed:
        return expr.to_sql(text, resolve, self.warn, allow_aggregates)

    @staticmethod
    def coerce(value: Typed, port: Port) -> str:
        target, sql_type = port_types(port)
        if value.type == "boolean" and target in expr.NUMERIC | {"unknown"}:
            return f"cast({value.sql} as integer)"
        if target in ("integer", "bigint") and value.type in ("double", "decimal"):
            return f"cast({value.sql} as {sql_type})"
        return value.sql

    # -- handlers ------------------------------------------------------------

    def source_qualifier(self, n: str, t: Transformation) -> dict:
        sources = self.c.inst(n).associated_sources
        sources = sources or [u for u in self.c.upstream(n) if self.c.inst(u).type == "SOURCE"]
        if len(sources) != 1:
            raise NodeError(f"{n} reads {len(sources)} sources (a Source Qualifier join); "
                            "only single-source qualifiers are supported")
        src_inst = self.c.inst(sources[0])
        tdef = self.c.p.folder.sources[src_inst.definition]
        relation = self.c.p.source_relation(self.m.name, tdef)
        fields = {c.to_field.upper(): c.from_field for c in self.c.incoming[n] if c.from_instance == src_inst.name}
        lines, columns, types = [], {}, {}
        for p in t.ports:
            if not p.is_output:
                continue
            src_col = fields.get(p.name.upper(), p.name)
            etype, sql_type = port_types(p)
            value = f"cast({ident(src_col)} as {sql_type})" if sql_type else ident(src_col)
            lines.append(f"{value} as {ident(p.name)}")
            columns[p.name.upper()], types[p.name.upper()] = ident(p.name), etype
        override = t.attr("Sql Query").strip()
        distinct = t.attr("Select Distinct") == "YES"
        if override:
            self.warn("SQL override copied verbatim: it is in the source database dialect and bypasses ref()/source()")
            names = ", ".join(columns.values())
            sql = f"select{' distinct' if distinct else ''} *\nfrom (\n{indent(override)}\n) as sq_override({names})"
        else:
            sql = f"select{' distinct' if distinct else ''}\n{indent(',\n'.join(lines))}\nfrom {relation}"
            if t.attr("Source Filter").strip():
                self.warn("Source Filter copied verbatim (source database SQL)")
                sql += f"\nwhere {t.attr('Source Filter').strip()}"
        sorted_ports = int(t.attr("Number Of Sorted Ports", "0") or 0)
        ordering = [(p.name.upper(), "asc") for p in [q for q in t.ports if q.is_output][:sorted_ports]]
        cte = self.add_cte(n, f"{n} (Source Qualifier): reads {tdef.name}", sql)
        return {None: Rel(cte, {k: v for k, v in columns.items()}, types, ordering)}

    def expression(self, n: str, t: Transformation) -> dict:
        return ExpressionTranslator(self, n, t).translate()

    def filter(self, n: str, t: Transformation) -> dict:
        rel, colmap = self.single_input(n)
        types = {p: rel.types.get(_port_of(rel, c), "unknown") for p, c in colmap.items()}
        cond = self.translate(t.attr("Filter Condition") or "TRUE", self.resolver(n, colmap, types))
        lines, columns = [], {}
        for p in t.ports:
            if p.is_output and p.name.upper() in colmap:
                lines.append(f"{colmap[p.name.upper()]} as {ident(p.name)}")
                columns[p.name.upper()] = ident(p.name)
        sql = f"select\n{indent(',\n'.join(lines))}\nfrom {rel.relation}\nwhere {emitter_bool(cond)}"
        cte = self.add_cte(n, f"{n} (Filter): keeps rows where {t.attr('Filter Condition')}", sql)
        ordering = self.map_ordering(rel, colmap, set(columns))
        return {None: Rel(cte, columns, types, ordering)}

    def sorter(self, n: str, t: Transformation) -> dict:
        rel, colmap = self.single_input(n)
        lines, columns, types = [], {}, {}
        for p in t.ports:
            if p.is_output and p.name.upper() in colmap:
                lines.append(f"{colmap[p.name.upper()]} as {ident(p.name)}")
                columns[p.name.upper()] = ident(p.name)
                types[p.name.upper()] = port_types(p)[0]
        keys = [(p.name.upper(), "desc" if p.sort_direction.upper().startswith("DESC") else "asc")
                for p in t.ports if p.is_sort_key]
        distinct = t.attr("Distinct") == "YES"
        if distinct:
            keys = keys or [(p, "asc") for p in columns]
        sql = f"select{' distinct' if distinct else ''}\n{indent(',\n'.join(lines))}\nfrom {rel.relation}"
        key_text = ", ".join(f"{k.lower()} {d}" for k, d in keys)
        cte = self.add_cte(n, f"{n} (Sorter on {key_text}): row order only matters to order-dependent "
                              "logic downstream, so no ORDER BY here", sql)
        return {None: Rel(cte, columns, types, keys)}

    def joiner(self, n: str, t: Transformation) -> dict:
        master_ports = [p.name for p in t.ports if p.is_input and p.is_master]
        detail_ports = [p.name for p in t.ports if p.is_input and not p.is_master]
        sides = {}
        for side, ports in (("master", master_ports), ("detail", detail_ports)):
            cols = self.input_columns(n, ports)
            rels = {id(r): r for r, _ in cols.values()}
            if len(rels) != 1:
                raise NodeError(f"{n}: {side} ports come from {len(rels)} pipelines")
            sides[side] = (next(iter(rels.values())), {p: f"{side}.{col}" for p, (_, col) in cols.items()})
        (mrel, mcols), (drel, dcols) = sides["master"], sides["detail"]
        columns = {**mcols, **dcols}
        types = {}
        for p in t.ports:
            if p.is_input:
                rel = mrel if p.is_master else drel
                upstream_col = columns[p.name.upper()].split(".", 1)[1]
                types[p.name.upper()] = rel.types.get(_port_of(rel, upstream_col), port_types(p)[0])
        cond = self.translate(t.attr("Join Condition"), self.resolver(n, columns, types))
        join_type = t.attr("Join Type", "Normal Join")
        # "Master Outer" keeps all DETAIL rows (and matching master rows): detail LEFT JOIN master.
        joins = {
            "Normal Join": ("detail", "inner join", "master"),
            "Master Outer Join": ("detail", "left join", "master"),
            "Detail Outer Join": ("master", "left join", "detail"),
            "Full Outer Join": ("detail", "full outer join", "master"),
        }
        if join_type not in joins:
            raise NodeError(f"join type {join_type!r}")
        left, how, right = joins[join_type]
        rels = {"master": mrel, "detail": drel}
        out_lines, out_cols, out_types = [], {}, {}
        for p in t.ports:
            if p.is_output and p.name.upper() in columns:
                out_lines.append(f"{columns[p.name.upper()]} as {ident(p.name)}")
                out_cols[p.name.upper()] = ident(p.name)
                out_types[p.name.upper()] = types[p.name.upper()]
        sql = (f"select\n{indent(',\n'.join(out_lines))}\nfrom {rels[left].relation} as {left}\n"
               f"{how} {rels[right].relation} as {right}\n    on {emitter_bool(cond)}")
        cte = self.add_cte(n, f"{n} (Joiner, {join_type}): {t.attr('Join Condition')}", sql)
        return {None: Rel(cte, out_cols, out_types, [])}

    def aggregator(self, n: str, t: Transformation) -> dict:
        rel, colmap = self.single_input(n)
        types = {p: rel.types.get(_port_of(rel, c), "unknown") for p, c in colmap.items()}
        if any(p.is_variable for p in t.ports):
            raise NodeError("variable ports inside an Aggregator")
        group_by = [p for p in t.ports if p.expressiontype == "GROUPBY"]
        resolve = self.resolver(n, colmap, types)
        lines, columns, out_types = [], {}, {}
        for p in t.ports:
            if not p.is_output:
                continue
            key = p.name.upper()
            if p in group_by or (p.is_input and (p.expression or p.name).upper() == key):
                value = Typed(colmap[key], types.get(key, "unknown"))
                if p not in group_by:
                    self.warn(f"{p.name} is neither grouped nor aggregated: PowerCenter returns the last row's "
                              "value of each group, any_value() returns an arbitrary one")
                    value = Typed(f"any_value({value.sql})", value.type)
            else:
                value = self.translate(p.expression or p.name, resolve, allow_aggregates=True)
            lines.append(f"{self.coerce(value, p)} as {ident(p.name)}")
            columns[key], out_types[key] = ident(p.name), value.type if value.type != "unknown" else port_types(p)[0]
        sql = f"select\n{indent(',\n'.join(lines))}\nfrom {rel.relation}"
        if group_by:
            sql += "\ngroup by " + ", ".join(colmap[p.name.upper()] for p in group_by)
        keys = ", ".join(p.name for p in group_by) or "(no group by: one row)"
        cte = self.add_cte(n, f"{n} (Aggregator) grouped by {keys}", sql)
        return {None: Rel(cte, columns, out_types, [])}

    def router(self, n: str, t: Transformation) -> dict:
        rel, colmap = self.single_input(n)
        types = {p: rel.types.get(_port_of(rel, c), "unknown") for p, c in colmap.items()}
        resolve = self.resolver(n, colmap, types)
        used_ports = {c.from_field.upper() for c in self.c.outgoing[n]}
        conditions = [(g, self.translate(g.expression or "FALSE", resolve)) for g in t.groups if g.type == "OUTPUT"]
        out: dict[str | None, Rel] = {}
        for g in t.groups:
            if not g.type.startswith("OUTPUT"):
                continue
            ports = [p for p in t.ports if p.group == g.name and p.is_output]
            if not any(p.name.upper() in used_ports for p in ports):
                continue  # group not connected downstream
            if g.type == "OUTPUT":
                where = emitter_bool(dict((x.name, c) for x, c in conditions)[g.name])
                comment = f"{n} (Router) group {g.name}: {g.expression}"
            else:  # DEFAULT group: rows that matched no other group (NULL conditions count as false)
                where = " and ".join(f"not coalesce({emitter_bool(c)}, false)" for _, c in conditions) or "true"
                comment = f"{n} (Router) default group {g.name}: rows no other group matched"
            lines, columns, gtypes = [], {}, {}
            for p in ports:
                src = (p.ref_field or p.name).upper()
                lines.append(f"{colmap[src]} as {ident(p.name)}")
                columns[p.name.upper()], gtypes[p.name.upper()] = ident(p.name), types.get(src, "unknown")
            sql = f"select\n{indent(',\n'.join(lines))}\nfrom {rel.relation}\nwhere {where}"
            cte = self.add_cte(f"{n}__{g.name}", comment, sql)
            by_input = {(p.ref_field or p.name).upper(): p.name.upper() for p in ports}
            ordering = []
            for port, d in rel.ordering:
                src = next((k for k, v in colmap.items() if v == rel.columns[port]), None)
                if src not in by_input:
                    break
                ordering.append((by_input[src], d))
            out[g.name] = Rel(cte, columns, gtypes, ordering)
        if len(conditions) > 1:
            self.warn("Router sends a row to EVERY group whose condition is true (not just the first)")
        return out

    def union(self, n: str, t: Transformation) -> dict:
        outputs = [p for p in t.ports if p.is_output]
        deps = defaultdict(dict)  # group -> output port -> input port
        input_ports = {p.name: p for p in t.ports if p.is_input}
        for i, o in t.field_dependencies:
            if i in input_ports:
                deps[input_ports[i].group][o] = i
        selects, types = [], {}
        for g in [g for g in t.groups if g.type == "INPUT"]:
            ports = [p for p in t.ports if p.is_input and p.group == g.name]
            cols = self.input_columns(n, [p.name for p in ports])
            if not cols:
                continue  # group not connected
            rels = {id(r): r for r, _ in cols.values()}
            if len(rels) != 1:
                raise NodeError(f"{n}: input group {g.name} reads from several pipelines")
            rel = next(iter(rels.values()))
            mapping = deps.get(g.name) or {o.name: ports[k].name for k, o in enumerate(outputs) if k < len(ports)}
            lines = []
            for o in outputs:
                src = mapping.get(o.name)
                if src and src.upper() in cols:
                    col = cols[src.upper()][1]
                    lines.append(f"{col} as {ident(o.name)}")
                    types.setdefault(o.name.upper(), rel.types.get(_port_of(rel, col), port_types(o)[0]))
                else:
                    lines.append(f"cast(null as {port_types(o)[1] or 'varchar'}) as {ident(o.name)}")
            selects.append(f"-- input group {g.name}\nselect\n{indent(',\n'.join(lines))}\nfrom {rel.relation}")
        if not selects:
            raise NodeError(f"{n}: no connected input groups")
        sql = "\n\nunion all\n\n".join(selects)
        cte = self.add_cte(n, f"{n} (Union): UNION ALL of {len(selects)} input groups", sql)
        columns = {o.name.upper(): ident(o.name) for o in outputs}
        return {None: Rel(cte, columns, {k: types.get(k, "unknown") for k in columns}, [])}

    def target(self, n: str) -> Rel:
        tdef = self.c.p.folder.targets[self.c.inst(n).definition]
        rel, colmap = self.single_input(n)
        lines, columns, types = [], {}, {}
        for col in tdef.columns:
            sql_type = column_sql_type(col.datatype, col.precision, col.scale)
            key = col.name.upper()
            if key in colmap:
                value = colmap[key]
                if rel.types.get(_port_of(rel, value)) == "boolean":
                    value = f"cast({value} as integer)"
            else:
                value = "null"
                self.warn(f"target column {col.name} is not connected; it is loaded as NULL")
            if sql_type:
                value = f"cast({value} as {sql_type})"
            else:
                self.warn(f"target column {col.name}: unknown type {col.datatype}; not cast")
            lines.append(f"{value} as {ident(col.name)}")
            columns[key], types[key] = ident(col.name), expr_type_of_sql(sql_type)
        sql = f"select\n{indent(',\n'.join(lines))}\nfrom {rel.relation}"
        self.add_cte("final", f"{n} (Target {tdef.name}): cast to the target column types", sql)
        return Rel("final", columns, types, [])


HANDLERS = {
    "Source Qualifier": ModelBuilder.source_qualifier,
    "Expression": ModelBuilder.expression,
    "Filter": ModelBuilder.filter,
    "Sorter": ModelBuilder.sorter,
    "Joiner": ModelBuilder.joiner,
    "Aggregator": ModelBuilder.aggregator,
    "Router": ModelBuilder.router,
}


def _port_of(rel: Rel, column: str) -> str | None:
    for port, col in rel.columns.items():
        if col == column:
            return port
    return None


def emitter_bool(t: Typed) -> str:
    return f"({t.sql} <> 0)" if t.type in expr.NUMERIC else t.sql


def one_line(text: str) -> str:
    """Comments must stay on one line: a newline would turn the rest into live SQL."""
    return " ".join(text.split())


def indent(text: str, by: str = "    ") -> str:
    return "\n".join(by + line if line else line for line in text.splitlines())


# --------------------------------------------------------------------------- Expression transformation


class ExpressionTranslator:
    """Expression transformation, including PowerCenter's row-by-row variable ports.

    PowerCenter evaluates, for each row: input ports, then variable ports top to bottom, then
    output ports. A variable keeps its value between rows. So when variable V is read by:
      - an output port, or a variable BELOW V   -> V's value for the current row
      - a variable ABOVE V, or V itself         -> V's value from the PREVIOUS row
    Previous-row reads become window functions over the order set by the nearest upstream
    Sorter: lag() for a plain value, and a running sum for a self-referencing counter such as
    v_SEQ = IIF(<condition>, v_SEQ + 1, 1).
    """

    def __init__(self, b: ModelBuilder, n: str, t: Transformation):
        self.b, self.n, self.t = b, n, t
        self.rel, self.colmap = b.single_input(n)
        self.variables = [p for p in t.ports if p.is_variable]
        self.pos = {v.name.upper(): i for i, v in enumerate(self.variables)}
        self.parsed = {v.name.upper(): expr.parse(v.expression or "NULL") for v in self.variables}
        self.input_types = {p: self.rel.types.get(_port_of(self.rel, c), "unknown") for p, c in self.colmap.items()}
        # Self-referencing variables (counters) need staged CTEs; then inputs are addressed by port name.
        self.accumulator_keys = [k for k, node in self.parsed.items() if k in _upper_names(node)]
        self.staged = bool(self.accumulator_keys)
        self.memo: dict[str, Typed] = {}
        self.in_progress: set[str] = set()
        self.accumulators: dict[str, dict] = {}  # var -> {reset, step, init} SQL
        self.emitting_parts = False

    def input_sql(self, port: str) -> str:
        return ident(port) if self.staged else self.colmap[port]

    def window(self, frame: bool = False) -> str:
        if not self.rel.ordering:
            self.b.warn("order-dependent variable port without an upstream Sorter: row order is undefined, "
                        "so the result is not deterministic")
        cols = ", ".join(f"{self.order_col(p)} {d}" for p, d in self.rel.ordering)
        spec = f"order by {cols}" if cols else ""
        if frame:
            spec += " rows between unbounded preceding and current row"
        return spec.strip()

    def order_col(self, upstream_port: str) -> str:
        col = self.rel.columns[upstream_port]
        if not self.staged:
            return col
        mine = next((p for p, c in self.colmap.items() if c == col), None)
        return ident(mine) if mine else f"__sort_{upstream_port.lower()}"

    def resolve_at(self, at: int | None):
        """Name resolution for an expression evaluated at variable position `at` (None = output port)."""

        def resolve(name: str) -> Typed:
            key = name.upper()
            if key in self.pos:
                if at is not None and self.pos[key] > at:
                    return self.previous(key)
                return self.current(key)
            if key in self.colmap:
                return Typed(self.input_sql(key), self.input_types.get(key, "unknown"))
            return self.b.resolver(self.n, {}, {})(name)

        return resolve

    def emit(self, node, at: int | None) -> Typed:
        return expr.Emitter(self.resolve_at(at), self.b.warn).emit(node)

    def current(self, key: str) -> Typed:
        if key in self.memo:
            return self.memo[key]
        if key in self.in_progress:
            raise NodeError(f"variable {key} depends on itself through other variables")
        if key in self.accumulator_keys and self.emitting_parts:
            raise NodeError(f"counter {key} is used inside another counter's condition")
        self.in_progress.add(key)
        var = self.variables[self.pos[key]]
        if key in self.accumulator_keys:
            value = self.accumulator(key, var, self.parsed[key])
        else:
            value = self.emit(self.parsed[key], self.pos[key])
            if not re.fullmatch(r"[\w.\"]+|\(.*\)", value.sql):
                value = Typed(f"({value.sql})", value.type)
        self.in_progress.discard(key)
        self.memo[key] = value
        return value

    def previous(self, key: str) -> Typed:
        if key in self.accumulator_keys:
            raise NodeError(f"previous-row value of counter {key}")
        value = self.current(key)
        if " over (" in value.sql:
            raise NodeError(f"previous-row value of {key}, which is itself order-dependent")
        var = self.variables[self.pos[key]]
        init = {"string": "''", "date": "timestamp '1753-01-01'"}.get(port_types(var)[0], "0")
        self.b.warn(f"{var.name} is read by a variable above it, so that read sees the previous row's value (lag)")
        return Typed(f"lag({value.sql}, 1, {init}) over ({self.window()})", value.type)

    def accumulator(self, key: str, var: Port, node) -> Typed:
        """v = IIF(cond, v + step, init)  |  IIF(cond, init, v + step)  |  v + step"""

        def self_plus(n):
            if isinstance(n, expr.Binary) and n.op == "+":
                if isinstance(n.left, expr.Name) and n.left.name.upper() == key:
                    return n.right
                if isinstance(n.right, expr.Name) and n.right.name.upper() == key:
                    return n.left
            return None

        reset_when_true = False
        if isinstance(node, expr.Call) and node.name == "IIF" and len(node.args) == 3:
            cond, a, b = node.args
            if (step := self_plus(a)) is not None:
                init = b
            elif (step := self_plus(b)) is not None:
                init, reset_when_true = a, True
            else:
                raise NodeError(f"variable {key} references itself in an unsupported way: {var.expression}")
        elif (step := self_plus(node)) is not None:
            cond, init = expr.Const("TRUE"), expr.Num("0")
        else:
            raise NodeError(f"variable {key} references itself in an unsupported way: {var.expression}")
        if any(key in _upper_names(part) for part in (cond, step, init)):
            raise NodeError(f"variable {key}: self-reference inside the condition or step is not supported")
        at = self.pos[key]
        self.emitting_parts = True
        try:
            keep = f"coalesce({emitter_bool(self.emit(cond, at))}, false)"  # NULL condition = false in IIF
            step_sql = self.emit(step, at)
            init_sql = self.emit(init, at)
        finally:
            self.emitting_parts = False
        as_value = expr.Emitter(lambda _: Typed("", ""), self.b.warn).as_value
        self.accumulators[key] = {
            "reset": keep if reset_when_true else f"not {keep}",
            "step": as_value(step_sql).sql,
            "init": as_value(init_sql).sql,
        }
        return Typed(ident(key), port_types(var)[0])

    def translate(self) -> dict:
        b, t = self.b, self.t
        outputs = [p for p in t.ports if p.is_output]
        for key in self.accumulator_keys:  # analyse counters first so their parts land in step 1
            self.current(key)
        lines, columns, types = [], {}, {}
        for p in outputs:
            key = p.name.upper()
            if p.is_input:
                value = Typed(self.input_sql(key), self.input_types.get(key, "unknown"))
            else:
                value = self.emit(expr.parse(p.expression or "NULL"), None)
            lines.append(f"{b.coerce(value, p)} as {ident(p.name)}")
            columns[key] = ident(p.name)
            types[key] = value.type if value.type not in ("unknown", "null", "boolean") else port_types(p)[0]
        ordering = b.map_ordering(self.rel, self.colmap, {p.name.upper() for p in outputs if p.is_input})

        if not self.accumulators:
            sql = f"select\n{indent(',\n'.join(lines))}\nfrom {self.rel.relation}"
            cte = b.add_cte(self.n, f"{self.n} (Expression)", sql)
            return {None: Rel(cte, columns, types, ordering)}

        # Staged form: (1) inputs by port name + per-row parts, (2) group id that starts a new group at
        # every reset row, (3) running sum inside each group, (4) the output ports.
        stage = [f"{c} as {ident(p)}" for p, c in self.colmap.items()]
        for port, _ in self.rel.ordering:
            if self.rel.columns[port] not in self.colmap.values():
                stage.append(f"{self.rel.columns[port]} as __sort_{port.lower()}")
        for key, acc in self.accumulators.items():
            v = key.lower()
            stage += [f"{acc['reset']} as __{v}_reset", f"{acc['step']} as __{v}_step", f"{acc['init']} as __{v}_init"]
        prev = b.add_cte(f"{self.n}__rows", f"{self.n} (Expression) step 1: inputs and per-row parts of the "
                         "self-referencing variables", f"select\n{indent(',\n'.join(stage))}\nfrom {self.rel.relation}")
        spec = self.window(frame=True)
        groups = [f"sum(case when __{k.lower()}_reset then 1 else 0 end) over ({spec}) as __{k.lower()}_grp"
                  for k in self.accumulators]
        prev = b.add_cte(f"{self.n}__groups", f"{self.n} step 2: every reset row starts a new group",
                         f"select\n    *,\n{indent(',\n'.join(groups))}\nfrom {prev}")
        running = [f"sum(case when __{k.lower()}_reset then __{k.lower()}_init else __{k.lower()}_step end) "
                   f"over (partition by __{k.lower()}_grp {spec}) as {ident(k)}" for k in self.accumulators]
        prev = b.add_cte(f"{self.n}__running", f"{self.n} step 3: running value of each counter within its group",
                         f"select\n    *,\n{indent(',\n'.join(running))}\nfrom {prev}")
        cte = b.add_cte(self.n, f"{self.n} (Expression) outputs",
                        f"select\n{indent(',\n'.join(lines))}\nfrom {prev}")
        return {None: Rel(cte, columns, types, ordering)}


def _upper_names(node) -> set[str]:
    return {x.upper() for x in expr.names_in(node)}


# --------------------------------------------------------------------------- project


class ProjectBuilder:
    def __init__(self, folder: Folder, mapping_names: list[str], intermediate_models: bool = True):
        self.folder = folder
        self.mapping_names = mapping_names
        self.intermediate_models = intermediate_models
        self.issues: list[Issue] = []
        self.sources: dict[str, SourceTable] = {}
        self.vars: dict[str, tuple[str, str]] = {}
        # target definition -> model name, for every target loaded by a mapping we convert
        self.target_models: dict[str, str] = {}
        loads = defaultdict(list)
        for mname in mapping_names:
            for inst in folder.mappings[mname].instances.values():
                if inst.type == "TARGET":
                    loads[inst.definition].append((mname, inst.name))
        self.instance_models: dict[tuple[str, str], str] = {}
        for tname, users in loads.items():
            for mname, iname in users:
                name = tname.lower() if len(users) == 1 else f"{tname.lower()}__{iname.lower()}"
                self.instance_models[(mname, iname)] = name
            self.target_models[tname] = self.instance_models[users[0]]
            if len(users) > 1:
                self.issues.append(Issue("warning", users[0][0], tname,
                                         f"target {tname} is loaded {len(users)} times; one model per load was "
                                         f"generated and ref({tname.lower()!r}) points at the first"))

    def target_model_name(self, mapping: str, instance: str) -> str:
        return self.instance_models[(mapping, instance)]

    def source_relation(self, mapping: str, tdef: TableDef) -> str:
        if tdef.name in self.target_models:  # produced by another mapping we convert: use lineage
            return "{{ ref('" + self.target_models[tdef.name] + "') }}"
        source = (tdef.owner or "default").lower()
        self.sources.setdefault(tdef.name, SourceTable(source, source, tdef))
        return "{{ source('" + source + "', '" + tdef.name.lower() + "') }}"

    def mapping_var(self, mapping: str, var) -> Typed:
        name = var.name.lstrip("$").lower()
        etype = port_types(Port(var.name, "", var.datatype))[0]
        default = convert_default_date(var.default) if etype == "date" else var.default
        if name in self.vars and self.vars[name][0] != default:
            self.issues.append(Issue("warning", mapping, var.name, f"{var.name} has different defaults in "
                                     "different mappings; dbt vars are project-wide"))
        self.vars.setdefault(name, (default, f"{var.name} from mapping {mapping}"))
        jinja = "{{ var('" + name + "') }}"
        if etype == "date":
            return Typed(f"cast('{jinja}' as timestamp)", "date")
        if etype == "string":
            return Typed(f"'{jinja}'", "string")
        return Typed(jinja, etype)

    def build(self) -> Project:
        models = []
        for mname in self.mapping_names:
            conv = MappingConverter(self, self.folder.mappings[mname], self.intermediate_models)
            models += conv.convert()
            self.issues += conv.issues
        return Project(models, self.sources, self.vars, self.issues, self.folder, self.mapping_names)


def mapping_order(folder: Folder) -> list[str]:
    """Mappings in workflow order when a workflow says so, else in file order."""
    names = list(folder.mappings)
    for wf in folder.workflows:
        session_mapping = {t.name: t.detail for t in wf.tasks if t.type == "Session"}
        nxt = dict(wf.links)
        start = next((t.name for t in wf.tasks if t.type == "Start"), None)
        ordered, cur, seen = [], start, set()
        while cur in nxt and cur not in seen:
            seen.add(cur)
            cur = nxt[cur]
            if cur in session_mapping and session_mapping[cur] in names:
                ordered.append(session_mapping[cur])
        if ordered:
            return ordered + [n for n in names if n not in ordered]
    return names


def convert(folder: Folder, mappings: list[str] | None = None, intermediate_models: bool = True) -> Project:
    names = mapping_order(folder)
    if mappings:
        unknown = [m for m in mappings if m not in folder.mappings]
        if unknown:
            raise ValueError(f"unknown mapping(s): {', '.join(unknown)}; available: {', '.join(names)}")
        names = [n for n in names if n in mappings]
    return ProjectBuilder(folder, names, intermediate_models).build()
