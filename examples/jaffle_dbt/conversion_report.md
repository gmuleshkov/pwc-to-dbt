# Conversion report

Folder `FLOWLINE_DEMO_JAFFLESHOP`, mappings converted in run order: `m_FL_JS_STAGING_FULL`, `m_FL_JS_MARTS_CORE`, `m_FL_JS_MARTS_ENRICHED`.

- Models: 19 (16 targets, 3 shared intermediates)
- Unsupported constructs: 0 (those models fail at compile time on purpose)
- Warnings: 21

## Models

| Model | Mapping | From | Transformations |
|---|---|---|---|
| `stg_customers` | m_FL_JS_STAGING_FULL | STG_CUSTOMERS | Source Qualifier x1 |
| `stg_locations` | m_FL_JS_STAGING_FULL | STG_LOCATIONS | Sorter x5, Expression x4, Source Qualifier x1, Router x1, Custom Transformation x1 |
| `stg_orders` | m_FL_JS_STAGING_FULL | STG_ORDERS | Source Qualifier x1, Expression x1, Sorter x1 |
| `stg_order_items` | m_FL_JS_STAGING_FULL | STG_ORDER_ITEMS | Source Qualifier x1, Expression x1 |
| `stg_products` | m_FL_JS_STAGING_FULL | STG_PRODUCTS | Source Qualifier x1, Expression x1 |
| `stg_supplies` | m_FL_JS_STAGING_FULL | STG_SUPPLIES | Source Qualifier x2, Joiner x1, Expression x1 |
| `customers` | m_FL_JS_MARTS_CORE | CUSTOMERS | Source Qualifier x2, Aggregator x1, Joiner x1, Expression x1 |
| `orders` | m_FL_JS_MARTS_CORE | ORDERS | Source Qualifier x4, Joiner x3, Aggregator x2, Expression x2, Sorter x1 |
| `order_items` | m_FL_JS_MARTS_CORE | ORDER_ITEMS | Source Qualifier x4, Joiner x3, Sorter x1, Aggregator x1, Expression x1 |
| `products` | m_FL_JS_MARTS_CORE | PRODUCTS | Source Qualifier x1 |
| `supplies` | m_FL_JS_MARTS_CORE | SUPPLIES | Source Qualifier x1 |
| `int_fl_js_marts_enriched__exp_item_calculations` | m_FL_JS_MARTS_ENRICHED | EXP_ITEM_CALCULATIONS | Source Qualifier x6, Joiner x5, Sorter x1, Aggregator x1, Expression x1 |
| `int_fl_js_marts_enriched__exp_customer_order_num` | m_FL_JS_MARTS_ENRICHED | EXP_CUSTOMER_ORDER_NUM | Sorter x2, Expression x2, Aggregator x1, Source Qualifier x1, Joiner x1 |
| `int_fl_js_marts_enriched__un_order_bands` | m_FL_JS_MARTS_ENRICHED | UN_ORDER_BANDS | Expression x2, Sorter x1, Router x1, Custom Transformation x1 |
| `customer_360` | m_FL_JS_MARTS_ENRICHED | CUSTOMER_360 | Expression x5, Aggregator x1, Source Qualifier x1, Joiner x1 |
| `order_financials` | m_FL_JS_MARTS_ENRICHED | ORDER_FINANCIALS1 |  |
| `order_item_enriched` | m_FL_JS_MARTS_ENRICHED | ORDER_ITEM_ENRICHED | Sorter x1 |
| `product_daily_performance` | m_FL_JS_MARTS_ENRICHED | PRODUCT_DAILY_PERFORMANCE | Sorter x2, Aggregator x1, Expression x1 |
| `store_daily_kpis` | m_FL_JS_MARTS_ENRICHED | STORE_DAILY_KPIS | Sorter x2, Expression x2, Aggregator x1, Source Qualifier x1, Joiner x1, Filter x1 |

## Warnings (review these: output may differ from PowerCenter or from intent)

- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.SUBTOTAL -> STG_ORDERS.SUBTOTAL_CENTS while SRT_ORDERS.SUBTOTAL_CENTS -> STG_ORDERS.SUBTOTAL; converted as linked
- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.TAX_PAID -> STG_ORDERS.TAX_PAID_CENTS while SRT_ORDERS.TAX_PAID_CENTS -> STG_ORDERS.TAX_PAID; converted as linked
- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.ORDER_TOTAL -> STG_ORDERS.ORDER_TOTAL_CENTS while SRT_ORDERS.ORDER_TOTAL_CENTS -> STG_ORDERS.ORDER_TOTAL; converted as linked
- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.SUBTOTAL_CENTS -> STG_ORDERS.SUBTOTAL while SRT_ORDERS.SUBTOTAL -> STG_ORDERS.SUBTOTAL_CENTS; converted as linked
- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.TAX_PAID_CENTS -> STG_ORDERS.TAX_PAID while SRT_ORDERS.TAX_PAID -> STG_ORDERS.TAX_PAID_CENTS; converted as linked
- **m_FL_JS_STAGING_FULL / STG_ORDERS**: probable crossed link: SRT_ORDERS.ORDER_TOTAL_CENTS -> STG_ORDERS.ORDER_TOTAL while SRT_ORDERS.ORDER_TOTAL -> STG_ORDERS.ORDER_TOTAL_CENTS; converted as linked
- **m_FL_JS_STAGING_FULL / STG_PRODUCTS**: probable crossed link: EXP_PRODUCTS.PRODUCT_PRICE -> STG_PRODUCTS.PRODUCT_PRICE_CENTS while EXP_PRODUCTS.PRODUCT_PRICE_CENTS -> STG_PRODUCTS.PRODUCT_PRICE; converted as linked
- **m_FL_JS_STAGING_FULL / STG_PRODUCTS**: probable crossed link: EXP_PRODUCTS.PRODUCT_PRICE_CENTS -> STG_PRODUCTS.PRODUCT_PRICE while EXP_PRODUCTS.PRODUCT_PRICE -> STG_PRODUCTS.PRODUCT_PRICE_CENTS; converted as linked
- **m_FL_JS_STAGING_FULL / RTR_REGION**: Router sends a row to EVERY group whose condition is true (not just the first)
- **m_FL_JS_STAGING_FULL / EXP_ORDERS**: TO_DATE with 2-digit year format 'DD-MM-YY': Informatica assumes the current century, strptime %y maps 00-68 to 20xx and 69-99 to 19xx
- **m_FL_JS_MARTS_CORE / EXP_CUSTOMER_ORDER_NUM**: comparison `customer_id = customer_id` compares a value with itself: always true unless NULL (check variable-port order in the source mapping)
- **m_FL_JS_MARTS_ENRICHED / CUSTOMER_360**: probable crossed link: EXP_RFM_SEGMENT.LIFETIME_SPEND -> CUSTOMER_360.LIFETIME_SPEND_PRETAX while EXP_RFM_SEGMENT.LIFETIME_SPEND_PRETAX -> CUSTOMER_360.LIFETIME_TAX_PAID; converted as linked
- **m_FL_JS_MARTS_ENRICHED / CUSTOMER_360**: probable crossed link: EXP_RFM_SEGMENT.LIFETIME_SPEND_PRETAX -> CUSTOMER_360.LIFETIME_TAX_PAID while EXP_RFM_SEGMENT.LIFETIME_TAX_PAID -> CUSTOMER_360.LIFETIME_SPEND; converted as linked
- **m_FL_JS_MARTS_ENRICHED / CUSTOMER_360**: probable crossed link: EXP_RFM_SEGMENT.LIFETIME_TAX_PAID -> CUSTOMER_360.LIFETIME_SPEND while EXP_RFM_SEGMENT.LIFETIME_SPEND -> CUSTOMER_360.LIFETIME_SPEND_PRETAX; converted as linked
- **m_FL_JS_MARTS_ENRICHED / PRODUCT_DAILY_PERFORMANCE**: probable crossed link: EXP_PRODUCT_METRICS.PERISHABLE_SALES_FLAG -> PRODUCT_DAILY_PERFORMANCE.MARGIN_PCT while EXP_PRODUCT_METRICS.MARGIN_PCT -> PRODUCT_DAILY_PERFORMANCE.PERISHABLE_SALES_FLAG; converted as linked
- **m_FL_JS_MARTS_ENRICHED / PRODUCT_DAILY_PERFORMANCE**: probable crossed link: EXP_PRODUCT_METRICS.MARGIN_PCT -> PRODUCT_DAILY_PERFORMANCE.PERISHABLE_SALES_FLAG while EXP_PRODUCT_METRICS.PERISHABLE_SALES_FLAG -> PRODUCT_DAILY_PERFORMANCE.MARGIN_PCT; converted as linked
- **m_FL_JS_MARTS_ENRICHED / EXP_CUSTOMER_ORDER_NUM**: comparison `customer_id = customer_id` compares a value with itself: always true unless NULL (check variable-port order in the source mapping)
- **m_FL_JS_MARTS_ENRICHED / RTR_ORDER_VALUE_BAND**: Router sends a row to EVERY group whose condition is true (not just the first)
- **m_FL_JS_MARTS_ENRICHED / EXP_MARK_FIRST_CUSTOMER**: comparison `location_id = location_id` compares a value with itself: always true unless NULL (check variable-port order in the source mapping)
- **m_FL_JS_MARTS_ENRICHED / EXP_MARK_FIRST_CUSTOMER**: comparison `ordered_at_day = ordered_at_day` compares a value with itself: always true unless NULL (check variable-port order in the source mapping)
- **m_FL_JS_MARTS_ENRICHED / EXP_MARK_FIRST_CUSTOMER**: comparison `customer_id = customer_id` compares a value with itself: always true unless NULL (check variable-port order in the source mapping)

## Parameters

Mapping variables became dbt vars (override with `--vars`):

- `run_date` default '2025-12-31 00:00:00' ($$RUN_DATE from mapping m_FL_JS_MARTS_ENRICHED)

## Not converted

- Workflow and session settings: dbt's DAG (ref/source) replaces the run order; connections, schedules, recovery and pre/post SQL are not converted.
- Command task `cmd_FL_JS_CREATE_RPT_VIEWS` in workflow `wf_FL_JAFFLESHOP_DEMO` runs an external script (`sqlplus -S RPT/<redacted>@localhost:1521/XEPDB1 @"C:\Informatica\10.2.0\server\infa_shared\SrcFiles\Jaffleshop_RPT views.sql" > "C:\Informatica\10.2.0\server\infa_shared\SrcFiles\rpt_views.log" 2>&1`). Its SQL is not in the export; port it by hand (for example as dbt models or an on-run-end hook).
