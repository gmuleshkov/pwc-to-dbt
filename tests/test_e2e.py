"""Full pipeline against dbt-labs/jaffle-shop (dbt + DuckDB). Run with: make e2e  (pytest -m e2e)."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.e2e
def test_end_to_end_against_jaffle_shop():
    sys.path.insert(0, str(ROOT / "verify"))
    import e2e

    assert e2e.main() == 0, "see verify/REPORT.md"
