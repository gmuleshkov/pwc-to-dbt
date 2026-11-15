"""The sample export from infinitelambda/flowline-pwc-demo: parse, convert, golden output."""

import os
import re
import shutil
from pathlib import Path

import pytest

from pwc_to_dbt import cli, expr, model
from pwc_to_dbt.convert import convert

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "FLOWLINE_DEMO_JAFFLESHOP.xml"
GOLDEN = ROOT / "examples" / "jaffle_dbt"
REPAIRED = ROOT / "examples" / "FLOWLINE_DEMO_JAFFLESHOP_REPAIRED.xml"
REPAIRED_GOLDEN = ROOT / "examples" / "jaffle_dbt_repaired"


@pytest.fixture(scope="module")
def folder():
    return model.load(SAMPLE)[0]


def test_parses_what_the_file_contains(folder):
    assert list(folder.mappings) == ["m_FL_JS_STAGING_FULL", "m_FL_JS_MARTS_ENRICHED", "m_FL_JS_MARTS_CORE"]
    assert len(folder.sources) == 12 and len(folder.targets) == 16
    assert sum(len(m.transformations) for m in folder.mappings.values()) == 98
    assert sum(len(m.connectors) for m in folder.mappings.values()) == 1066


def test_every_expression_in_the_sample_parses(folder):
    texts = set()
    for m in folder.mappings.values():
        for t in m.transformations.values():
            texts |= {p.expression for p in t.ports if p.expression}
            texts |= {g.expression for g in t.groups if g.expression}
            if t.attr("Filter Condition"):
                texts.add(t.attr("Filter Condition"))
    texts.discard("ERROR('transformation error')")  # port default values, not logic
    assert len(texts) > 200
    for text in texts:
        expr.parse(text)


def test_converts_all_targets_in_workflow_order(folder):
    project = convert(folder)
    assert project.mapping_names == ["m_FL_JS_STAGING_FULL", "m_FL_JS_MARTS_CORE", "m_FL_JS_MARTS_ENRICHED"]
    targets = [m.name for m in project.models if m.kind == "target"]
    assert len(targets) == 16 and targets[:6] == [
        "stg_customers", "stg_locations", "stg_orders", "stg_order_items", "stg_products", "stg_supplies"]
    assert not [i for i in project.issues if i.level == "unsupported"]
    assert set(project.sources) == {"RAW_CUSTOMERS", "RAW_ORDERS", "RAW_ITEMS", "RAW_PRODUCTS", "RAW_STORES",
                                    "RAW_SUPPLIES"}  # STG_* sources are the staging models


def test_flags_the_mapping_defects(folder):
    messages = [(i.instance, i.message) for i in convert(folder).issues]
    crossed = {inst for inst, msg in messages if "crossed link" in msg}
    assert crossed == {"STG_ORDERS", "STG_PRODUCTS", "CUSTOMER_360", "PRODUCT_DAILY_PERFORMANCE"}
    self_compare = {inst for inst, msg in messages if "compares a value with itself" in msg}
    assert self_compare == {"EXP_CUSTOMER_ORDER_NUM", "EXP_MARK_FIRST_CUSTOMER"}


def test_report_redacts_the_password_in_the_workflow_command(tmp_path):
    cli.main(["convert", str(SAMPLE), "-o", str(tmp_path)])
    report = (tmp_path / "conversion_report.md").read_text()
    assert "RPT/<redacted>@" in report and "admin123" not in report


def test_output_matches_golden_files(tmp_path):
    """examples/jaffle_dbt is the committed output; regenerate with UPDATE_GOLDEN=1 (make golden)."""
    out = tmp_path / "jaffle_dbt"
    assert cli.main(["convert", str(SAMPLE), "-o", str(out)]) == 0
    if os.environ.get("UPDATE_GOLDEN"):
        shutil.rmtree(GOLDEN, ignore_errors=True)
        shutil.copytree(out, GOLDEN)
    produced = {p.relative_to(out): p.read_text() for p in out.rglob("*") if p.is_file()}
    golden = {p.relative_to(GOLDEN): p.read_text() for p in GOLDEN.rglob("*") if p.is_file()}
    assert sorted(produced) == sorted(golden)
    for path, text in produced.items():
        assert text == golden[path], f"{path} differs from the golden file (run `make golden` if intended)"


def test_generated_sql_has_no_jinja_we_do_not_expect():
    for path in GOLDEN.rglob("*.sql"):
        for tag in re.findall(r"\{\{(.*?)\}\}", path.read_text()):
            assert re.match(r"\s*(source|ref|var)\(", tag), f"{path}: {tag}"


# -- the repaired export (defects P1-P5 fixed, docs/DEFECTS.md)


def test_committed_repaired_export_is_what_repair_mapping_produces(tmp_path):
    import sys

    sys.path.insert(0, str(ROOT / "verify"))
    import repair_mapping

    out = tmp_path / "repaired.xml"
    changes = repair_mapping.repair(str(SAMPLE), str(out))
    assert out.read_bytes() == REPAIRED.read_bytes(), "run `make repair`"
    assert (ROOT / "examples" / "REPAIR_CHANGES.txt").read_text() == "\n".join(changes) + "\n"


def test_repaired_export_has_no_defect_warnings():
    messages = [i.message for i in convert(model.load(REPAIRED)[0]).issues]
    assert not [m for m in messages if "crossed link" in m or "compares a value with itself" in m]


def test_repaired_output_matches_golden_files(tmp_path):
    out = tmp_path / "jaffle_dbt_repaired"
    assert cli.main(["convert", str(REPAIRED), "-o", str(out)]) == 0
    produced = {p.relative_to(out): p.read_text() for p in out.rglob("*") if p.is_file()}
    golden = {p.relative_to(REPAIRED_GOLDEN): p.read_text() for p in REPAIRED_GOLDEN.rglob("*") if p.is_file()}
    assert sorted(produced) == sorted(golden)
    for path, text in produced.items():
        assert text == golden[path], f"{path} differs from the golden file (run `make repair` if intended)"
