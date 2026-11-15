# Defects in the sample mapping

The sample export (`examples/FLOWLINE_DEMO_JAFFLESHOP.xml`) re-implements dbt's jaffle shop in PowerCenter. When
converted as-is and run next to the real jaffle-shop, the numbers differ. All the differences come from **five
defects in the mapping itself**. None is a converter bug.

**Policy:** the converter reproduces what PowerCenter would do, defects included, and warns where it can. A migration
tool that quietly "fixes" logic would hide changes from the people who sign off the migration. The fixed version is
a separate, opt-in file.

## At a glance

| | In plain words | Effect when run as-is | Converter warns? | Fix |
|---|---|---|---|---|
| **P1** | Output ports are wired to the wrong target columns (dollars into `_cents` and the reverse) | Money columns are off by 100x, and so is every mart that sums them (13 links, 4 targets) | Yes: `probable crossed link` | Relink each port to the target column with the same name |
| **P2** | `SUBTOTAL` and `TAX_PAID` hold dollars but are declared `number(15,0)`: no decimals | The cents are rounded away (tax `0.20` becomes `0`; 61,296 orders affected) | No | Declare `number(12,2)` (the owner may prefer to store cents instead) |
| **P3** | A "previous row" variable sits above the variable that reads it, so it is already overwritten | `customer_order_number` never restarts for a new customer (customer 2's first order is numbered 191); 61,275 wrong values | Yes: `compares a value with itself` | Move the variable below the ones that read it |
| **P4** | A Joiner is "Master Outer" with orders as the master, which keeps every *detail* row | 483 orders without items disappear from `orders` and `order_financials` | No | Switch to "Detail Outer" and take the key from the orders side |
| **P5** | `IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', ...)`, but the data holds `True`/`False` | `is_perishable` is always 0 (the reference has 30) | No | Also accept `'TRUE'` |

## Each one, briefly

**P1: crossed links.** In `EXP_ORDERS`, `SUBTOTAL` is dollars (`SUBTOTAL_IN / 100`) and `SUBTOTAL_CENTS` is cents. The
links into `STG_ORDERS` go `SUBTOTAL -> SUBTOTAL_CENTS` and `SUBTOTAL_CENTS -> SUBTOTAL`. It is an easy mistake
(similar names, drag and drop) and PowerCenter does not flag it. The same happens for tax, total, product price,
three lifetime-money columns in `CUSTOMER_360`, and `MARGIN_PCT` / `PERISHABLE_SALES_FLAG`.

**P2: whole-dollar money.** The column was probably designed for cents (whole numbers are fine) and later fed with
dollars without changing its type. Fixing the type is a design decision, so the pipeline owner must choose between
"store dollars" and "store cents".

**P3: variable-port order.** PowerCenter evaluates variable ports top to bottom for each row, and a variable keeps its
value into the next row. "Is this a new customer?" needs `v_PREV_CUSTOMER_ID` to still hold the *previous* row's
customer when compared, so it must be assigned *after* the comparison. The order is invisible in the diagram, which
is what makes it easy to get wrong. It occurs in three Expressions across two mappings. Our converter translates
these ports with `lag()` and running counters, in the same evaluation order as PowerCenter, so it reproduces the
bug and the fix equally.

**P4: Joiner direction.** "Master Outer" keeps all rows of the *detail* input (and matching master rows). With orders
as master and item summaries as detail, orders without items are dropped. "Detail Outer" keeps all masters. The
names are the opposite of what most people expect. The same Joiner exists in two mappings.

**P5: flag values.** The mapping was written for one encoding of the data (`'Y'`/`'1'`) and receives another
(`True`/`False`). Only a comparison with real data finds this, which is why the end-to-end check exists.

## How we know these are the causes

1. `make e2e` converts the original export, runs it in DuckDB and compares every shared column with the real
   jaffle-shop. Each difference must be explained in `verify/reference_map.toml`, or the run fails.
2. `verify/repair_mapping.py` applies the five fixes to a copy of the XML. Converting that copy and comparing again
   gives **no unexplained differences**, so each diagnosis is confirmed, not guessed. A fix that changes nothing
   makes the script fail.

The repaired version is committed so it can be inspected and demoed:

| File | What it is |
|---|---|
| `examples/FLOWLINE_DEMO_JAFFLESHOP_REPAIRED.xml` | the sample with P1-P5 fixed |
| `examples/REPAIR_CHANGES.txt` | every individual change, one per line (the XML is re-serialised, so a text diff is noisy; use this list) |
| `examples/jaffle_dbt_repaired/` | the dbt project converted from it |

Regenerate with `make repair`. Tests check that the committed files are exactly what the script produces, that
the repaired export raises no crossed-link or self-comparison warning, and that its dbt output matches the golden files.

One difference remains by design, and it is not a defect: for orders without items the mapping writes `0`
(`IIF(ISNULL(x), 0, x)`) where jaffle-shop leaves `NULL`. It is listed in `reference_map.toml` under
`expected_repaired`.

## Limits worth stating

- "Defect" is judged against jaffle-shop. In a real migration the business decides what is correct.
- Only P1 and P3 can be detected from the XML alone. P2, P4 and P5 look intentional until compared with data. A
  lint for them would guess and be noisy, so we did not add one.
- The repaired XML is for our converter and for reference. It was not re-imported into PowerCenter, which we do not
  have.
