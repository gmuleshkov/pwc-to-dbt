"""Write a copy of the sample export with the mapping defects we diagnosed patched.

Why: converting the ORIGINAL export must reproduce what PowerCenter would do, so its output
differs from jaffle-shop wherever the mapping has a defect. Those differences are only
"explained" if our diagnosis is right. Converting this REPAIRED copy and getting a clean
diff against jaffle-shop proves the diagnosis and checks the converter on every column.

Each patch is generic over the XML (no hard-coded SQL) and prints what it changed.

    uv run python verify/repair_mapping.py examples/FLOWLINE_DEMO_JAFFLESHOP.xml verify/work/repaired.xml
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PATCHES: list[tuple[str, str]] = []  # (id, description) for the README / report


def patch(pid: str, description: str):
    def register(fn):
        PATCHES.append((pid, description))
        fn.pid = pid
        return fn

    return register


@patch("P1", "Crossed target links: X.A -> T.B while X.B -> T.A (STG_ORDERS cents/dollars, STG_PRODUCTS price, "
             "CUSTOMER_360 lifetime money, PRODUCT_DAILY_PERFORMANCE margin/flag). Relink each port to the "
             "target column of the same name.")
def uncross_links(folder):
    targets = {t.get("NAME"): {f.get("NAME") for f in t.findall("TARGETFIELD")} for t in folder.findall("TARGET")}
    for m in folder.findall("MAPPING"):
        target_inst = {i.get("NAME"): i.get("TRANSFORMATION_NAME")
                       for i in m.findall("INSTANCE") if i.get("TYPE") == "TARGET"}
        links = {}
        for c in m.findall("CONNECTOR"):
            if c.get("TOINSTANCE") in target_inst:
                links.setdefault((c.get("FROMINSTANCE"), c.get("TOINSTANCE")), []).append(c)
        for (src, dst), conns in links.items():
            fields = {c.get("FROMFIELD") for c in conns}
            columns = targets[target_inst[dst]]
            for c in conns:
                f, g = c.get("FROMFIELD"), c.get("TOFIELD")
                if f != g and f in columns and g in fields:  # crossed: a same-named column exists
                    c.set("TOFIELD", f)
                    yield f"{m.get('NAME')}: {src}.{f} -> {dst}.{g}  relinked to {dst}.{f}"


@patch("P2", "STG_ORDERS.SUBTOTAL / TAX_PAID are number(15,0) but hold dollars: widen to number(12,2) in the "
             "target and source definitions and in the Source Qualifier ports that read them.")
def money_types(folder):
    for kind, tag in (("TARGET", "TARGETFIELD"), ("SOURCE", "SOURCEFIELD")):
        for t in folder.findall(kind):
            if t.get("NAME") == "STG_ORDERS":
                for f in t.findall(tag):
                    if f.get("NAME") in ("SUBTOTAL", "TAX_PAID"):
                        f.set("DATATYPE", "number(p,s)"), f.set("PRECISION", "12"), f.set("SCALE", "2")
                        yield f"{kind} STG_ORDERS.{f.get('NAME')} -> number(12,2)"
    for m in folder.findall("MAPPING"):
        for i in m.findall("INSTANCE"):
            if i.get("TYPE") == "SOURCE" and i.get("TRANSFORMATION_NAME") == "STG_ORDERS":
                sq = {c.get("TOINSTANCE") for c in m.findall("CONNECTOR") if c.get("FROMINSTANCE") == i.get("NAME")}
                for t in m.findall("TRANSFORMATION"):
                    if t.get("NAME") in sq:
                        for f in t.findall("TRANSFORMFIELD"):
                            if f.get("NAME") in ("SUBTOTAL", "TAX_PAID"):
                                f.set("DATATYPE", "decimal"), f.set("PRECISION", "12"), f.set("SCALE", "2")
                                yield f"{m.get('NAME')}: {t.get('NAME')}.{f.get('NAME')} -> decimal(12,2)"


@patch("P3", "Variable-port order: v_PREV_* copies the current row's key ABOVE the variable that compares "
             "against it, so the comparison is always true. Move each such variable below its last reader.")
def port_order(folder):
    for m in folder.findall("MAPPING"):
        for t in m.findall("TRANSFORMATION"):
            if t.get("TYPE") != "Expression":
                continue
            fields = t.findall("TRANSFORMFIELD")
            variables = [f for f in fields if f.get("PORTTYPE") == "LOCAL VARIABLE"]
            inputs = {f.get("NAME") for f in fields if "INPUT" in f.get("PORTTYPE")}
            for v in variables:
                if v.get("EXPRESSION") not in inputs:
                    continue  # only plain copies of an input are "previous value" holders
                order = list(t.findall("TRANSFORMFIELD"))
                pos = order.index(v)
                readers = [f for f in order[pos + 1:] if f.get("PORTTYPE") == "LOCAL VARIABLE"
                           and v.get("NAME") in (f.get("EXPRESSION") or "")]
                if readers:
                    t.remove(v)
                    t.insert(list(t).index(readers[-1]) + 1, v)
                    yield f"{m.get('NAME')}: {t.get('NAME')}.{v.get('NAME')} moved below {readers[-1].get('NAME')}"


@patch("P4", "JNR_ORDERS_ITEMS_AGG makes orders the MASTER of a Master Outer join, which keeps all DETAIL "
             "(item summary) rows and drops orders without items; its ORDER_ID output also comes from the item "
             "side. Use Detail Outer (keep every order) and output the key from the orders side.")
def join_type(folder):
    for m in folder.findall("MAPPING"):
        for t in m.findall("TRANSFORMATION"):
            if t.get("NAME") != "JNR_ORDERS_ITEMS_AGG":
                continue
            attrs = {a.get("NAME"): a for a in t.findall("TABLEATTRIBUTE")}
            if attrs["Join Type"].get("VALUE") != "Master Outer Join":
                continue
            attrs["Join Type"].set("VALUE", "Detail Outer Join")
            ports = {f.get("NAME"): f for f in t.findall("TRANSFORMFIELD")}
            a, b = [x.strip() for x in attrs["Join Condition"].get("VALUE").split("=")]
            master, detail = (a, b) if "MASTER" in ports[a].get("PORTTYPE") else (b, a)
            ports[master].set("PORTTYPE", "INPUT/OUTPUT/MASTER")
            ports[detail].set("PORTTYPE", "INPUT")
            for c in m.findall("CONNECTOR"):
                if c.get("FROMINSTANCE") == t.get("NAME") and c.get("FROMFIELD") == detail:
                    c.set("FROMFIELD", master)
            yield f"{m.get('NAME')}: JNR_ORDERS_ITEMS_AGG Master Outer -> Detail Outer, key output {detail} -> {master}"


@patch("P5", "EXP_SUPPLIES tests PERISHABLE = 'Y' OR '1', but the data holds True/False: also accept 'TRUE'.")
def perishable(folder):
    for t in folder.iter("TRANSFORMATION"):
        if t.get("NAME") == "EXP_SUPPLIES":
            for f in t.findall("TRANSFORMFIELD"):
                if f.get("NAME") == "IS_PERISHABLE":
                    f.set("EXPRESSION", "IIF(UPPER(PERISHABLE) = 'TRUE' OR PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)")
                    yield "EXP_SUPPLIES.IS_PERISHABLE accepts 'TRUE'"


def repair(src: str, dst: str) -> list[str]:
    raw = Path(src).read_bytes()
    header = raw[: raw.index(b"<POWERMART")]  # XML declaration + DOCTYPE, kept so the file still looks like an export
    tree = ET.parse(src)
    folder = tree.getroot().find(".//FOLDER")
    changes = []
    for fn in (uncross_links, money_types, port_order, join_type, perishable):
        done = list(fn(folder))
        if not done:
            raise SystemExit(f"patch {fn.pid} changed nothing; is this the sample export?")
        changes += [f"[{fn.pid}] {c}" for c in done]
    body = ET.tostring(tree.getroot(), encoding="unicode").encode("cp1252", errors="xmlcharrefreplace")
    Path(dst).write_bytes(header + body + b"\n")
    return changes


if __name__ == "__main__":
    for line in repair(sys.argv[1], sys.argv[2]):
        print(line)
