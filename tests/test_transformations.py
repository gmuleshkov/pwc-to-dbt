"""Each transformation type on tiny synthetic mappings, executed in DuckDB."""

import pytest
from helpers import ExportBuilder, P, rows, run_project

from pwc_to_dbt import cli

KEY_ROWS = {
    "raw.m": "select * from (values ('1', 'm1'), ('2', 'm2')) t(k, mval)",
    "raw.d": "select * from (values ('1', 'd1'), ('3', 'd3')) t(k, dval)",
}


def join_export(join_type):
    e = ExportBuilder()
    e.source("M", [("K", "varchar2", 10), ("MVAL", "varchar2", 10)])
    e.source("D", [("K", "varchar2", 10), ("DVAL", "varchar2", 10)])
    e.target("OUT", [(c, "varchar2", 10) for c in ("MK", "DK", "MVAL", "DVAL")])
    m = e.mapping()
    sq_m, sq_d = m.use_source("M"), m.use_source("D")
    m.transformation("JNR", "Joiner", [
        P("K1", "INPUT/OUTPUT/MASTER"), P("MVAL", "INPUT/OUTPUT/MASTER"),
        P("K", "INPUT/OUTPUT"), P("DVAL", "INPUT/OUTPUT"),
    ], attrs={"Join Type": join_type, "Join Condition": "K1 = K"})
    m.link(sq_m, "JNR", ("K", "K1"), "MVAL").link(sq_d, "JNR", "K", "DVAL")
    m.link("JNR", m.use_target("OUT"), ("K1", "MK"), ("K", "DK"), "MVAL", "DVAL")
    return e


@pytest.mark.parametrize("join_type, expected", [
    ("Normal Join", [("1", "1")]),
    # Master Outer keeps every DETAIL row: the unmatched detail key 3 survives, master key 2 is dropped
    ("Master Outer Join", [(None, "3"), ("1", "1")]),
    ("Detail Outer Join", [("1", "1"), ("2", None)]),
    ("Full Outer Join", [(None, "3"), ("1", "1"), ("2", None)]),
])
def test_joiner_types(tmp_path, join_type, expected):
    project = join_export(join_type).convert(tmp_path)
    con = run_project(project, KEY_ROWS)
    assert rows(con, "select mk, dk from out order by mk nulls first, dk") == expected


def test_router_sends_rows_to_every_matching_group_and_default_gets_the_rest(tmp_path):
    e = ExportBuilder()
    e.source("S", [("ID", "varchar2", 10), ("X", "number", 10)])
    e.target("OUT", [("ID", "varchar2", 10), ("BAND", "varchar2", 10)])
    m = e.mapping()
    sq = m.use_source("S")
    ports = [P("ID", "INPUT"), P("X", "INPUT", "decimal")]
    for suffix, group in (("1", "POS"), ("2", "BIG"), ("3", "DEFAULT1")):
        ports += [P(f"ID{suffix}", "OUTPUT", GROUP=group, REF_FIELD="ID"),
                  P(f"X{suffix}", "OUTPUT", "decimal", GROUP=group, REF_FIELD="X")]
    m.transformation("RTR", "Router", ports, groups=[
        ("INPUT", "INPUT"), ("POS", "OUTPUT", "X > 0"), ("BIG", "OUTPUT", "X > 3"), ("DEFAULT1", "OUTPUT/DEFAULT")])
    m.link(sq, "RTR", "ID", "X")
    for suffix, band in (("1", "'pos'"), ("2", "'big'"), ("3", "'other'")):
        m.transformation(f"EXP{suffix}", "Expression", [P("ID", "INPUT/OUTPUT"), P("BAND", "OUTPUT", expression=band)])
        m.link("RTR", f"EXP{suffix}", (f"ID{suffix}", "ID"))
    union_ports = [P("ID", "OUTPUT", GROUP="OUTPUT"), P("BAND", "OUTPUT", GROUP="OUTPUT")]
    deps = []
    for g in ("A", "B", "C"):
        union_ports += [P(f"ID_{g}", "INPUT", GROUP=g), P(f"BAND_{g}", "INPUT", GROUP=g)]
        deps += [(f"ID_{g}", "ID"), (f"BAND_{g}", "BAND")]
    m.transformation("UN", "Custom Transformation", union_ports, deps=deps,
                     attrs={"Module Identifier": "pmuniontrans", "Function Identifier": "pmunionfunc"},
                     groups=[("OUTPUT", "OUTPUT"), ("A", "INPUT"), ("B", "INPUT"), ("C", "INPUT")])
    for suffix, g in (("1", "A"), ("2", "B"), ("3", "C")):
        m.link(f"EXP{suffix}", "UN", ("ID", f"ID_{g}"), ("BAND", f"BAND_{g}"))
    m.link("UN", m.use_target("OUT"), "ID", "BAND")

    project = e.convert(tmp_path)
    con = run_project(project, {"raw.s": "select * from (values ('a', 1), ('b', 5), ('c', null), ('d', -2)) t(id, x)"})
    assert rows(con, "select id, band from out order by id, band") == [
        ("a", "pos"), ("b", "big"), ("b", "pos"), ("c", "other"), ("d", "other")]


def sequence_export(prev_below_reader: bool):
    """v_SEQ = IIF(v_PREV = K, v_SEQ + 1, 1); v_PREV = K.  Placement of v_PREV decides the meaning."""
    e = ExportBuilder()
    e.source("S", [("K", "varchar2", 10), ("T", "number", 10)])
    e.target("OUT", [("K", "varchar2", 10), ("T", "number", 10), ("SEQ", "number", 10), ("FIRST", "number", 1)])
    m = e.mapping()
    sq = m.use_source("S")
    m.transformation("SRT", "Sorter", [P("K", ISSORTKEY="YES"), P("T", datatype="decimal", ISSORTKEY="YES")])
    prev = [P("v_PREV", "LOCAL VARIABLE", expression="K")]
    readers = [P("v_SEQ", "LOCAL VARIABLE", "decimal", expression="IIF(v_PREV = K, v_SEQ + 1, 1)"),
               P("v_FIRST", "LOCAL VARIABLE", "integer", expression="IIF(v_PREV = K, 0, 1)")]
    variables = readers + prev if prev_below_reader else prev + readers
    m.transformation("EXP", "Expression", [
        P("K"), P("T", datatype="decimal"), *variables,
        P("SEQ", "OUTPUT", "decimal", expression="v_SEQ"), P("FIRST", "OUTPUT", "integer", expression="v_FIRST"),
    ])
    m.link(sq, "SRT", "K", "T").link("SRT", "EXP", "K", "T")
    m.link("EXP", m.use_target("OUT"), "K", "T", "SEQ", "FIRST")
    return e


SEQ_ROWS = {"raw.s": "select * from (values ('b', 1), ('a', 2), ('a', 1), ('b', 2), ('b', 3)) t(k, t)"}


def test_previous_row_variables_number_rows_per_key(tmp_path):
    project = sequence_export(prev_below_reader=True).convert(tmp_path)
    con = run_project(project, SEQ_ROWS)
    assert rows(con, "select k, t, seq, first from out order by k, t") == [
        ("a", 1, 1, 1), ("a", 2, 2, 0), ("b", 1, 1, 1), ("b", 2, 2, 0), ("b", 3, 3, 0)]
    assert "lag(" in project.models[0].sql


def test_previous_value_assigned_above_its_reader_is_reproduced_and_flagged(tmp_path):
    # PowerCenter evaluates variables top to bottom: v_PREV already holds the CURRENT key when the
    # comparison runs, so the counter never resets and FIRST is always 0. We reproduce that.
    project = sequence_export(prev_below_reader=False).convert(tmp_path)
    con = run_project(project, SEQ_ROWS)
    assert rows(con, "select k, t, seq, first from out order by k, t") == [
        ("a", 1, 1, 0), ("a", 2, 2, 0), ("b", 1, 3, 0), ("b", 2, 4, 0), ("b", 3, 5, 0)]
    assert any("compares a value with itself" in i.message for i in project.issues)


def test_aggregator_group_by_count_and_conditional_sum(tmp_path):
    e = ExportBuilder()
    e.source("S", [("K", "varchar2", 10), ("X", "number", 10)])
    e.target("OUT", [("K", "varchar2", 10), ("N", "number", 10), ("BIG", "number", 10), ("POS", "number", 10)])
    m = e.mapping()
    sq = m.use_source("S")
    m.transformation("AGG", "Aggregator", [
        P("K", EXPRESSIONTYPE="GROUPBY"), P("X", "INPUT", "decimal"),
        P("N", "OUTPUT", "decimal", expression="COUNT(*)"),
        P("BIG", "OUTPUT", "decimal", expression="SUM(IIF(X > 2, 1, 0))"),
        P("POS", "OUTPUT", "decimal", expression="SUM(X, X > 0)"),
    ])
    m.link(sq, "AGG", "K", "X").link("AGG", m.use_target("OUT"), "K", "N", "BIG", "POS")
    con = run_project(e.convert(tmp_path), {"raw.s": "select * from (values ('a', 1), ('a', 3), ('b', -1)) t(k, x)"})
    assert rows(con, "select k, n, big, pos from out order by k") == [("a", 2, 1, 4), ("b", 1, 0, None)]


def test_filter_and_mapping_variable(tmp_path):
    e = ExportBuilder()
    e.source("S", [("K", "varchar2", 10), ("D", "date", 19)])
    e.target("OUT", [("K", "varchar2", 10), ("AGE", "number", 10)])
    m = e.mapping().variable("$$AS_OF", "date/time", "01/31/2025 00:00:00")
    sq = m.use_source("S")
    m.transformation("FIL", "Filter", [P("K"), P("D", datatype="date/time")],
                     attrs={"Filter Condition": "NOT ISNULL(D)"})
    m.transformation("EXP", "Expression", [P("K"), P("D", "INPUT", "date/time"),
                     P("AGE", "OUTPUT", "integer", expression="DATE_DIFF(TRUNC($$AS_OF), TRUNC(D), 'DD')")])
    m.link(sq, "FIL", "K", "D").link("FIL", "EXP", "K", "D").link("EXP", m.use_target("OUT"), "K", "AGE")
    project = e.convert(tmp_path)
    assert project.vars["as_of"][0] == "2025-01-31 00:00:00"
    data = "select * from (values ('a', timestamp '2025-01-01 10:00'), ('b', null)) t(k, d)"
    con = run_project(project, {"raw.s": data})
    assert rows(con, "select k, age from out") == [("a", 30)]


def test_crossed_links_are_converted_as_linked_but_flagged(tmp_path):
    e = ExportBuilder()
    e.source("S", [("A", "number", 10), ("B", "number", 10)])
    e.target("OUT", [("A", "number", 10), ("B", "number", 10)])
    m = e.mapping()
    sq = m.use_source("S")
    m.link(sq, m.use_target("OUT"), ("A", "B"), ("B", "A"))
    project = e.convert(tmp_path)
    assert rows(run_project(project, {"raw.s": "select 1 as a, 2 as b"}), "select a, b from out") == [(2, 1)]
    assert any("probable crossed link" in i.message for i in project.issues)


def test_unconnected_target_column_is_null_with_warning(tmp_path):
    e = ExportBuilder()
    e.source("S", [("A", "number", 10)])
    e.target("OUT", [("A", "number", 10), ("LOAD_TS", "date", 19)])
    m = e.mapping()
    m.link(m.use_source("S"), m.use_target("OUT"), "A")
    project = e.convert(tmp_path)
    assert rows(run_project(project, {"raw.s": "select 1 as a"}), "select a, load_ts from out") == [(1, None)]
    assert any("LOAD_TS is not connected" in i.message for i in project.issues)


def test_unsupported_transformation_becomes_a_compile_error_model(tmp_path):
    e = ExportBuilder()
    e.source("S", [("A", "varchar2", 10)])
    e.target("OUT", [("A", "varchar2", 10)])
    m = e.mapping()
    sq = m.use_source("S")
    m.transformation("LKP_X", "Lookup Procedure", [P("A")])
    m.link(sq, "LKP_X", "A").link("LKP_X", m.use_target("OUT"), "A")
    project = e.convert(tmp_path)
    assert "raise_compiler_error" in project.models[0].sql
    assert [i.instance for i in project.issues if i.level == "unsupported"] == ["LKP_X"]
    xml = e.write(tmp_path / "x.xml")
    assert cli.main(["convert", str(xml), "-o", str(tmp_path / "out")]) == 0
    assert cli.main(["convert", str(xml), "-o", str(tmp_path / "out"), "--strict"]) == 1


def shared_export():
    e = ExportBuilder()
    e.source("S", [("K", "varchar2", 10), ("X", "number", 10)])
    e.target("T1", [("K", "varchar2", 10), ("Y", "number", 10)])
    e.target("T2", [("K", "varchar2", 10), ("Y", "number", 10)])
    m = e.mapping()
    sq = m.use_source("S")
    m.transformation("EXP_SHARED", "Expression", [P("K"), P("X", "INPUT", "decimal"),
                                                  P("Y", "OUTPUT", "decimal", expression="X * 2")])
    m.link(sq, "EXP_SHARED", "K", "X")
    m.link("EXP_SHARED", m.use_target("T1"), "K", "Y").link("EXP_SHARED", m.use_target("T2"), "K", "Y")
    return e


def test_logic_shared_by_targets_becomes_an_intermediate_model(tmp_path):
    project = shared_export().convert(tmp_path)
    names = [m.name for m in project.models]
    assert names == ["int_test__exp_shared", "t1", "t2"]
    assert "{{ ref('int_test__exp_shared') }}" in project.models[1].sql
    con = run_project(project, {"raw.s": "select 'a' as k, 4 as x"})
    assert rows(con, "select y from t2") == [(8,)]

    inlined = shared_export().convert(tmp_path, intermediate_models=False)
    assert [m.name for m in inlined.models] == ["t1", "t2"]
    assert all("x * 2" in m.sql for m in inlined.models)


def test_target_of_one_mapping_is_a_ref_in_the_next(tmp_path):
    e = ExportBuilder()
    e.source("RAW_A", [("A", "varchar2", 10)])
    e.source("STG_A", [("A", "varchar2", 10)], owner="STG")
    e.target("STG_A", [("A", "varchar2", 10)])
    e.target("MART_A", [("A", "varchar2", 10)])
    m1 = e.mapping("m_stage")
    m1.link(m1.use_source("RAW_A"), m1.use_target("STG_A"), "A")
    m2 = e.mapping("m_mart")
    m2.link(m2.use_source("STG_A"), m2.use_target("MART_A"), "A")
    project = e.convert(tmp_path)
    mart = next(m for m in project.models if m.name == "mart_a")
    assert "{{ ref('stg_a') }}" in mart.sql
    assert set(project.sources) == {"RAW_A"}  # STG_A is a model, not a source

    only_mart = e.convert(tmp_path, mappings=["m_mart"])  # without the producer it is a source again
    assert "{{ source('stg', 'stg_a') }}" in only_mart.models[0].sql


def test_primary_keys_become_dbt_tests(tmp_path):
    e = ExportBuilder()
    e.source("S", [("ID", "varchar2", 10)])
    e.target("OUT", [("ID", "varchar2", 10, 0, "pk")])
    m = e.mapping()
    m.link(m.use_source("S"), m.use_target("OUT"), "ID")
    project = e.convert(tmp_path)
    assert project.models[0].columns[0].tests == ["not_null", "unique"]
