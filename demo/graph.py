"""Draw graphs as boxes and arrows (inline SVG), from the same data the converter uses.

Two graphs share one layered layout and one drawing routine:
  * a mapping graph per target: the target, every transformation that feeds it, and the source tables;
  * a table lineage: raw tables, staging tables and marts, with an arrow where one reads from another.

Boxes are laid out in columns by distance from the sources. Each box carries `data-node` so the page can react
to clicks (highlight the matching CTE, or show a table's columns).
"""

from __future__ import annotations

import re
from html import escape

from pwc_to_dbt.convert import MappingConverter, ProjectBuilder, mapping_order

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 176, 46, 60, 18, 16

KIND = {
    "SOURCE": "source",
    "TARGET": "target",
    "Source Qualifier": "sq",
    "Expression": "expr",
    "Filter": "filter",
    "Sorter": "sorter",
    "Joiner": "joiner",
    "Aggregator": "aggr",
    "Router": "router",
    "Custom Transformation": "union",
}
LABEL = {"Custom Transformation": "Union", "Source Qualifier": "Source Qualifier"}


# -- shared layout and drawing -------------------------------------------------------------------------------------

def layered_layout(order: list[str], ups: dict[str, list[str]]) -> dict[str, tuple[int, int]]:
    """node -> (layer, row). Layer = longest path from a source; rows ordered to keep edges short.

    `order` lists the nodes with every node after the nodes it reads from; `ups[n]` are the nodes n reads from.
    """
    nodes = set(order)
    depth: dict[str, int] = {}

    def d(n: str) -> int:
        if n not in depth:
            depth[n] = 0  # cycle guard
            parents = [u for u in ups.get(n, []) if u in nodes]
            depth[n] = 1 + max(d(u) for u in parents) if parents else 0
        return depth[n]

    for n in order:
        d(n)
    layers: dict[int, list[str]] = {}
    for n in order:
        layers.setdefault(depth[n], []).append(n)
    index = {n: i for ns in layers.values() for i, n in enumerate(ns)}
    for _ in range(3):  # barycentre sweeps
        for layer in sorted(layers)[1:]:
            def key(n, layer=layer):
                parents = [index[u] for u in ups.get(n, []) if u in nodes and depth[u] < layer]
                return sum(parents) / len(parents) if parents else index[n]

            layers[layer].sort(key=key)
            for i, n in enumerate(layers[layer]):
                index[n] = i
    return {n: (layer, i) for layer, ns in layers.items() for i, n in enumerate(ns)}


def draw(pos: dict[str, tuple[int, int]], edges: list[tuple[str, str]], info: dict[str, tuple[str, str]],
         title: str, node_w: int = NODE_W, name_chars: int = 24) -> str:
    """SVG for the laid-out nodes. info[n] = (kind, second line of the box)."""
    count: dict[int, int] = {}
    for layer, _ in pos.values():
        count[layer] = count.get(layer, 0) + 1
    tallest = max(count.values())
    xy = {}
    for n, (layer, row) in pos.items():
        offset = (tallest - count[layer]) * (NODE_H + GAP_Y) / 2
        xy[n] = (PAD + layer * (node_w + GAP_X), PAD + offset + row * (NODE_H + GAP_Y))
    width = PAD * 2 + (max(count) + 1) * (node_w + GAP_X) - GAP_X
    height = PAD * 2 + tallest * (NODE_H + GAP_Y) - GAP_Y
    order = sorted(pos, key=lambda x: pos[x])

    paths = []
    for u, n in sorted(edges, key=lambda e: (pos[e[1]], pos[e[0]])):
        x1, y1 = xy[u][0] + node_w, xy[u][1] + NODE_H / 2
        x2, y2 = xy[n][0], xy[n][1] + NODE_H / 2
        dx = max(24, (x2 - x1) / 2)
        paths.append(f'<path class="edge" d="M{x1:.0f} {y1:.0f} C{x1 + dx:.0f} {y1:.0f} {x2 - dx:.0f} {y2:.0f} '
                     f'{x2:.0f} {y2:.0f}" marker-end="url(#arrow)"/>')
    boxes = []
    for n in order:
        kind, second = info[n]
        x, y = xy[n]
        shown = n if len(n) <= name_chars else n[: name_chars - 1] + "…"
        boxes.append(
            f'<g class="node k-{kind}" data-node="{escape(n)}" transform="translate({x:.0f} {y:.0f})">'
            f"<title>{escape(n)} ({escape(second)})</title>"
            f'<rect width="{node_w}" height="{NODE_H}" rx="7"/>'
            f'<text class="name" x="10" y="19">{escape(shown)}</text>'
            f'<text class="type" x="10" y="35">{escape(second)}</text></g>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="graph" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="{escape(title)}">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="arrowhead"/></marker></defs>'
        + "".join(paths) + "".join(boxes) + "</svg>"
    )


# -- mapping graph (one target) --------------------------------------------------------------------------------------

def converter(folder, mapping_name: str) -> MappingConverter:
    builder = ProjectBuilder(folder, mapping_order(folder))
    return MappingConverter(builder, folder.mappings[mapping_name], intermediate_models=True)


def needed_nodes(conv: MappingConverter, target: str) -> set[str]:
    needed, stack = set(), [target]
    while stack:
        n = stack.pop()
        if n not in needed:
            needed.add(n)
            stack += conv.upstream(n)
    return needed


def target_graph(conv: MappingConverter, target: str) -> dict:
    needed = needed_nodes(conv, target)
    order = [n for n in conv.topo_order() if n in needed]
    pos = layered_layout(order, {n: conv.upstream(n) for n in order})
    edges = [(u, n) for n in order for u in conv.upstream(n) if u in needed]
    info, nodes = {}, []
    for n in sorted(needed, key=lambda x: pos[x]):
        ttype = conv.ttype(n)
        kind = KIND.get(ttype, "other")
        label = "Source table" if conv.inst(n).type == "SOURCE" else LABEL.get(ttype, ttype)
        info[n] = (kind, label)
        nodes.append({"name": n, "type": label, "kind": kind})
    return {"svg": draw(pos, edges, info, f"Mapping graph for {target}"), "nodes": nodes}


CTE_START = re.compile(r'^("[^"]+"|\w+) as \($')


def cte_ranges(sql: str) -> dict[str, list[int]]:
    """CTE name -> [first line, last line] (1-based) in a generated model."""
    ranges: dict[str, list[int]] = {}
    current = None
    for i, line in enumerate(sql.splitlines(), 1):
        m = CTE_START.match(line)
        if m:
            current = m.group(1).strip('"')
            ranges[current] = [i, i]
        elif current and line in (")", "),"):
            ranges[current][1] = i
            current = None
    return ranges


def cte_names_for(node: str, ranges: dict[str, list[int]], is_target: bool) -> list[str]:
    if is_target:
        return ["final"] if "final" in ranges else []
    low = node.lower()
    return [c for c in ranges if c == low or c.startswith(low + "__")]


# -- table lineage ---------------------------------------------------------------------------------------------------

SOURCE_CALL = re.compile(r"\{\{\s*source\('(\w+)',\s*'(\w+)'\)\s*\}\}")
REF_CALL = re.compile(r"\{\{\s*ref\('(\w+)'\)\s*\}\}")


def table_lineage(project) -> dict:
    """Raw tables and generated models as a graph; intermediate models are folded into their consumers.

    Returns {"svg", "tables": [{name, layer, kind, columns, source}]} where `layer` is a readable layer name.
    """
    models = {m.name: m for m in project.models}

    def reads(model_name: str) -> list[str]:
        """Direct tables a model reads, looking through intermediate (int_*) models."""
        m = models[model_name]
        out: list[str] = []
        for src in SOURCE_CALL.findall(m.sql):
            out.append(src[1])
        for ref in REF_CALL.findall(m.sql):
            out += reads(ref) if models[ref].kind == "intermediate" else [ref]
        return list(dict.fromkeys(out))

    layer_of = {}
    layers = []
    for name in project.mapping_names:
        label = re.sub(r"^m_FL_JS_", "", name).replace("_FULL", "").replace("_", " ").lower()
        layers.append(label)
        for m in project.models:
            if m.mapping == name and m.kind == "target":
                layer_of[m.name] = label

    tables = {}
    for t in project.sources.values():
        tables[t.table.name.lower()] = {
            "name": t.table.name.lower(), "layer": "raw", "kind": "source",
            "columns": [{"name": c.name.lower(), "type": _sql_type(c)} for c in t.table.columns],
        }
    kinds = dict(zip(layers, ["sq", "expr", "aggr", "union", "router"], strict=False))
    for m in project.models:
        if m.kind == "target":
            tables[m.name] = {
                "name": m.name, "layer": layer_of[m.name], "kind": kinds.get(layer_of[m.name], "target"),
                "columns": [{"name": c.name, "type": c.data_type} for c in m.columns],
            }
    ups = {n: [u for u in (reads(n) if n in models else []) if u in tables] for n in tables}
    order, seen = [], set()

    def visit(n):
        if n not in seen:
            seen.add(n)
            for u in ups[n]:
                visit(u)
            order.append(n)

    for n in tables:
        visit(n)
    pos = layered_layout(order, ups)
    edges = [(u, n) for n in order for u in ups[n]]
    info = {n: (tables[n]["kind"], tables[n]["layer"]) for n in order}
    svg = draw(pos, edges, info, "Table lineage: raw tables to staging to marts", node_w=210, name_chars=27)
    legend = [{"label": "raw", "kind": "source"}] + [{"label": lb, "kind": kinds.get(lb, "target")} for lb in layers]
    return {"svg": svg, "tables": [tables[n] for n in sorted(order, key=lambda x: pos[x])], "legend": legend}


def _sql_type(column) -> str:
    from pwc_to_dbt.convert import column_sql_type

    return column_sql_type(column.datatype, column.precision, column.scale)
