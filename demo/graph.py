"""Draw a mapping as boxes and arrows (inline SVG), from the same IR the converter uses.

One graph per target: the target, every transformation that feeds it, and the source tables, laid out in
columns by distance from the sources. Each box carries `data-node` so the page can highlight the matching
CTE in the generated SQL.
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


def layout(conv: MappingConverter, needed: set[str]) -> dict[str, tuple[int, int]]:
    """node -> (layer, row). Layer = longest path from a source; rows ordered to keep edges short."""
    depth: dict[str, int] = {}

    def d(n: str) -> int:
        if n not in depth:
            depth[n] = 0  # cycle guard
            ups = [u for u in conv.upstream(n) if u in needed]
            depth[n] = 1 + max(d(u) for u in ups) if ups else 0
        return depth[n]

    for n in needed:
        d(n)
    layers: dict[int, list[str]] = {}
    for n in conv.topo_order():
        if n in needed:
            layers.setdefault(depth[n], []).append(n)
    index = {n: i for ns in layers.values() for i, n in enumerate(ns)}
    for _ in range(3):  # barycentre sweeps
        for layer in sorted(layers)[1:]:
            def key(n, layer=layer):
                ups = [index[u] for u in conv.upstream(n) if u in needed and depth[u] < layer]
                return sum(ups) / len(ups) if ups else index[n]

            layers[layer].sort(key=key)
            for i, n in enumerate(layers[layer]):
                index[n] = i
    return {n: (layer, i) for layer, ns in layers.items() for i, n in enumerate(ns)}


def target_graph(conv: MappingConverter, target: str) -> dict:
    needed = needed_nodes(conv, target)
    pos = layout(conv, needed)
    tallest = max(sum(1 for p in pos.values() if p[0] == layer) for layer in {p[0] for p in pos.values()})
    count = {layer: sum(1 for p in pos.values() if p[0] == layer) for layer in {p[0] for p in pos.values()}}
    xy = {}
    for n, (layer, row) in pos.items():
        offset = (tallest - count[layer]) * (NODE_H + GAP_Y) / 2
        xy[n] = (PAD + layer * (NODE_W + GAP_X), PAD + offset + row * (NODE_H + GAP_Y))
    width = PAD * 2 + (max(p[0] for p in pos.values()) + 1) * (NODE_W + GAP_X) - GAP_X
    height = PAD * 2 + tallest * (NODE_H + GAP_Y) - GAP_Y

    edges = []
    for n in sorted(needed, key=lambda x: pos[x]):
        for u in conv.upstream(n):
            if u in needed:
                x1, y1 = xy[u][0] + NODE_W, xy[u][1] + NODE_H / 2
                x2, y2 = xy[n][0], xy[n][1] + NODE_H / 2
                dx = max(24, (x2 - x1) / 2)
                edges.append(f'<path class="edge" d="M{x1:.0f} {y1:.0f} C{x1 + dx:.0f} {y1:.0f} {x2 - dx:.0f} {y2:.0f} '
                             f'{x2:.0f} {y2:.0f}" marker-end="url(#arrow)"/>')
    boxes, nodes = [], []
    for n in sorted(needed, key=lambda x: pos[x]):
        ttype = conv.ttype(n)
        kind = KIND.get(ttype, "other")
        label = LABEL.get(ttype, ttype)
        if conv.inst(n).type == "SOURCE":
            label = "Source table"
        x, y = xy[n]
        boxes.append(
            f'<g class="node k-{kind}" data-node="{escape(n)}" transform="translate({x:.0f} {y:.0f})">'
            f"<title>{escape(n)} ({escape(label)})</title>"
            f'<rect width="{NODE_W}" height="{NODE_H}" rx="7"/>'
            f'<text class="name" x="10" y="19">{escape(n if len(n) <= 24 else n[:23] + "…")}</text>'
            f'<text class="type" x="10" y="35">{escape(label)}</text></g>'
        )
        nodes.append({"name": n, "type": label, "kind": kind})
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" class="graph" viewBox="0 0 {width:.0f} {height:.0f}" '
        f'width="{width:.0f}" height="{height:.0f}" role="img" aria-label="Mapping graph for {escape(target)}">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" '
        'orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10 z" class="arrowhead"/></marker></defs>'
        + "".join(edges) + "".join(boxes) + "</svg>"
    )
    return {"svg": svg, "nodes": nodes}


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
