"""Checks for the enriched marts, which have no jaffle-shop counterpart.

Each check is an independent, hand-written SQL query over the REFERENCE tables that must
agree with the converted model. A check returns the number of violating rows (0 = pass).
They are written against the repaired export: with the original export several fail
because of defects P1-P4, which the report shows for information.

    uv run python verify/invariants.py --schema repaired
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

HERE = Path(__file__).parent

# name -> SQL returning violations; {s} is the converted schema
CHECKS = {
    "order_financials has one row per order":
        "select abs((select count(*) from {s}.order_financials) - (select count(*) from reference.orders))"
        " + (select count(*) - count(distinct order_id) from {s}.order_financials)",
    "order_financials money and line counts match orders / order_items":
        "select count(*) from {s}.order_financials f join reference.orders o using (order_id) "
        "where abs(f.order_total - o.order_total) > 0.005 or abs(f.order_subtotal - o.subtotal) > 0.005 "
        "or f.item_lines <> coalesce(o.count_order_items, 0) or abs(f.order_cogs - coalesce(o.order_cost, 0)) > 0.005",
    "customer_order_number equals row_number() per customer (stateful variable ports)":
        "select count(*) from {s}.order_financials f join reference.orders o using (order_id) "
        "where f.customer_order_number <> o.customer_order_number",
    "order_value_band: Router + Union assign HIGH iff order_total >= 50":
        "select count(*) from {s}.order_financials "
        "where order_value_band <> case when order_total >= 50 then 'HIGH' else 'STANDARD' end",
    "order_item_enriched has one row per item with the right revenue and cost":
        "select abs((select count(*) from {s}.order_item_enriched) - (select count(*) from reference.order_items))"
        " + (select count(*) from {s}.order_item_enriched e join reference.order_items i using (order_item_id) "
        "    where abs(e.item_revenue - i.product_price) > 0.005 or abs(e.item_cogs - i.supply_cost) > 0.005)",
    "store_daily_kpis counts orders, distinct customers and revenue per store and day":
        "with expected as (select location_id, ordered_at, count(*) n, count(distinct customer_id) c, "
        "  sum(order_total) revenue from reference.orders group by 1, 2) "
        "select count(*) from expected e full join {s}.store_daily_kpis k "
        "  on k.location_id = e.location_id and k.ordered_at_day = e.ordered_at "
        "where k.orders_count is distinct from e.n or k.unique_customers is distinct from e.c "
        "   or abs(k.revenue_total - e.revenue) > 0.005 or k.revenue_total is null or e.revenue is null",
    "product_daily_performance units and revenue per product and day":
        "with expected as (select product_id, ordered_at, count(*) n, sum(product_price) revenue "
        "  from reference.order_items group by 1, 2) "
        "select count(*) from expected e full join {s}.product_daily_performance p "
        "  on p.product_id = e.product_id and p.ordered_at_day = e.ordered_at "
        "where p.units_sold is distinct from e.n or abs(p.revenue - e.revenue) > 0.005 "
        "   or p.revenue is null or e.revenue is null",
    "customer_360 lifetime metrics match customers":
        "select count(*) from {s}.customer_360 x full join reference.customers c using (customer_id) "
        "where x.count_lifetime_orders is distinct from c.count_lifetime_orders "
        "   or abs(x.lifetime_spend - c.lifetime_spend) > 0.005 "
        "   or abs(x.lifetime_spend_pretax - c.lifetime_spend_pretax) > 0.005 "
        "   or abs(x.lifetime_tax_paid - c.lifetime_tax_paid) > 0.005 or x.customer_type <> c.customer_type",
    "customer_360 recency_days = days from last order to $$RUN_DATE (2025-12-31)":
        "select count(*) from {s}.customer_360 "
        "where recency_days <> date_diff('day', last_ordered_at, timestamp '2025-12-31')",
    "customer_360 RFM segment follows the score rules":
        "select count(*) from {s}.customer_360 where rfm_score <> r_score * 100 + f_score * 10 + m_score "
        "or rfm_segment <> case when count_lifetime_orders = 0 then 'PROSPECT' when rfm_score = 333 then 'CHAMPION' "
        "  when r_score = 3 and f_score >= 2 and m_score >= 2 then 'LOYAL' when r_score = 1 and f_score >= 2 "
        "  then 'AT_RISK' else 'STANDARD' end",
}


def run(db: Path, schema: str) -> dict[str, int]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return {name: con.execute(sql.format(s=schema)).fetchall()[0][0] for name, sql in CHECKS.items()}
    finally:
        con.close()


def report(results: dict[str, int], title: str) -> str:
    lines = [f"## {title}", "", "| Check | Violations |", "|---|---|"]
    lines += [f"| {name} | {'pass' if n == 0 else n} |" for name, n in results.items()]
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", type=Path, default=HERE / "work" / "jaffle.duckdb")
    p.add_argument("--schema", default="repaired")
    p.add_argument("--title", default="Invariants for the enriched marts")
    p.add_argument("--report", type=Path)
    p.add_argument("--informational", action="store_true", help="never fail (used for the original export)")
    args = p.parse_args()
    results = run(args.db, args.schema)
    text = report(results, args.title)
    if args.report:
        args.report.write_text(text)
    print(text)
    failed = [k for k, v in results.items() if v]
    if failed and not args.informational:
        print("FAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
