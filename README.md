# pwc-to-dbt

Converts Informatica PowerCenter mapping exports (XML) into a dbt project. Built for the
Infinite Lambda take-home on the [flowline-pwc-demo](https://github.com/infinitelambda/flowline-pwc-demo)
export, and checked end to end against [dbt-labs/jaffle-shop](https://github.com/dbt-labs/jaffle-shop) on DuckDB.

```
PowerCenter XML ──parse──> IR (instances, ports, connectors) ──walk graph──> one CTE per transformation
                                                                             one dbt model per target
                                                                             + shared logic as intermediate models
```

## How to run

Needs [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 and dbt-duckdb into `.venv`) and `make`.

```bash
make install                    # uv sync
make inspect                    # what each mapping does: the flow of transformations per target
make convert                    # sample -> examples/jaffle_dbt (dbt project + conversion_report.md)
make test                       # 83 unit/semantic/golden/demo tests, offline, ~1 s
make e2e                        # full check against jaffle-shop (~30 s; first run clones it), writes verify/REPORT.md
make repair                     # sample with defects P1-P5 fixed -> examples/*_REPAIRED.xml + examples/jaffle_dbt_repaired
make demo                       # interactive walk-through on http://127.0.0.1:8765 (see demo/README.md)
make ui                         # browser tests of the demo page (Playwright + Chrome, ~1 min)
```

Converting your own export:

```bash
uv run pwc-to-dbt convert path/to/export.xml -o my_dbt_project [-m MAPPING ...] [--no-intermediate] [--strict]
```

`--strict` exits 1 if anything could not be converted. The generated project needs a profile called
`pwc_converted` (see `verify/profiles.yml` for a DuckDB one).

**What to read first:** [`examples/jaffle_dbt/`](examples/jaffle_dbt) (the generated project, e.g.
[`orders.sql`](examples/jaffle_dbt/models/fl_js_marts_core/orders.sql)),
[`conversion_report.md`](examples/jaffle_dbt/conversion_report.md) and
[`verify/REPORT.md`](verify/REPORT.md) (the row-by-row comparison with jaffle-shop).

## What the sample mapping does

One folder, three mappings that a workflow runs in sequence, 16 targets, 98 transformations, 1,066 port links:

| Mapping | Reads | Writes | Notable logic |
|---|---|---|---|
| `m_FL_JS_STAGING_FULL` | `RAW_*` (6 tables) | `STG_*` (6) | renames, cents to dollars, `IIF` flags; a Router splits stores by region and a Union glues them back |
| `m_FL_JS_MARTS_CORE` | `STG_*` | `CUSTOMERS`, `ORDERS`, `ORDER_ITEMS`, `PRODUCTS`, `SUPPLIES` | joins + aggregations; per-customer order number computed with row-by-row variable ports |
| `m_FL_JS_MARTS_ENRICHED` | `STG_*` | `CUSTOMER_360` (RFM segments), `ORDER_FINANCIALS`, `ORDER_ITEM_ENRICHED`, `PRODUCT_DAILY_PERFORMANCE`, `STORE_DAILY_KPIS` | margins, value bands (Router + Union), first-customer-of-the-day flag, `$$RUN_DATE` recency |

Staging and core mirror jaffle-shop's models; the enriched marts exist only in the XML. The workflow also runs a
command task (`sqlplus @"Jaffleshop_RPT views.sql"`) whose SQL is not in the export, and that command line
contains a plaintext database password (redacted in our report).

**Note on the reference:** the task links `jaffle_shop_duckdb`, which is the *old* jaffle shop
(customers / orders / payments). The XML mirrors the *new* `dbt-labs/jaffle-shop` (stores, items, products,
supplies), so that is what we verify against (pinned commit `5beb145`).

## How the conversion works

Every transformation instance becomes one CTE whose columns are exactly its output ports, so a connector
`A.X -> B.Y` is simply "B reads column X of CTE A, calling it Y". Handlers per type
(`src/pwc_to_dbt/convert.py`):

| PowerCenter | dbt SQL |
|---|---|
| Source Qualifier | `select cast(col as <port type>) ... from {{ source() }}` or `{{ ref() }}` when another mapping in the export loads that table |
| Expression | `select <expr> as port`; variable ports inlined; previous-row reads become `lag()`, self-counters become a running sum (below) |
| Filter | `where` (NULL = false, as in PowerCenter) |
| Sorter | no `ORDER BY` (meaningless in a CTE); the sort keys are carried as metadata for order-dependent logic downstream |
| Joiner | `Normal` = inner; **`Master Outer` = detail LEFT JOIN master** (keeps all *detail* rows); `Detail Outer` = master LEFT JOIN detail; `Full Outer` |
| Aggregator | `group by` the GROUPBY ports; Informatica's `SUM(x, cond)` becomes `sum(x) filter (where cond)` |
| Router | one CTE per connected group; a row goes to *every* group it matches; DEFAULT gets rows no group matched |
| Union (Custom Transformation `pmuniontrans`) | `union all`, columns paired via `FIELDDEPENDENCY` |
| Target | cast to the target column types (that is where PowerCenter rounds, too) |
| anything else | the model becomes `{{ exceptions.raise_compiler_error(...) }}`: it fails loudly in dbt, never silently wrong |

- **Expressions** (`src/pwc_to_dbt/expr.py`): a tokenizer and recursive-descent parser with Informatica's
  operator precedence produce an AST, which is emitted as SQL with simple type inference. The types are needed
  because `TRUNC`, `TO_CHAR` and `IIF`'s default branch mean different things for dates and numbers. Unknown
  functions raise `Unsupported` instead of being guessed.
- **Variable ports:** PowerCenter evaluates ports top to bottom for each row, and a variable keeps its value
  between rows. So a variable read *below* its definition means "this row", and read *above* (or by itself)
  means "previous row". Previous-row reads become `lag(...) over (order by <upstream Sorter keys>)`. A counter
  like `v = IIF(cond, v + 1, 1)` becomes a run-group id (a running sum of resets) plus a running sum within the
  group.
- **Model layout:** each target becomes a model named after it, materialized `table` (the sessions truncate and
  reload). A transformation feeding several targets becomes an intermediate model (`int_<mapping>__<instance>`,
  a view), so shared logic is written once. `$$RUN_DATE` becomes `var('run_date')`.
- **Faithful, plus loud:** the converter reproduces what PowerCenter would do, defects included. It reports
  anything suspicious in the model header and in `conversion_report.md`, for example crossed links, a value
  compared with itself, Routers with overlapping groups, and 2-digit years.

## How it was verified

`make e2e` (`verify/e2e.py`) loads jaffle-shop's seeds into DuckDB, builds the reference models, then:

1. **Original export:** convert, `dbt build`, and compare every shared column of the 11 staging/core models
   with jaffle-shop, row by row (`verify/compare.py`). Any difference must be listed in
   `verify/reference_map.toml` with its cause, or the run fails. A listed difference that disappears also fails
   (a stale expectation).
2. **Repaired export:** `verify/repair_mapping.py` patches only the five defects diagnosed below, in a copy of
   the XML. Converting that copy must match jaffle-shop on every shared column. This step proves the diagnosis
   and checks the converter on the columns that run 1 could only "explain". The one remaining difference is by
   design: for orders without items the mapping writes 0, where jaffle-shop leaves NULL.
3. **Enriched marts (no reference):** ten independent SQL checks over the reference tables (`verify/invariants.py`):
   - one row per order;
   - money matches;
   - the customer order number equals `row_number()`;
   - the value band follows the Router rule;
   - the store/day KPIs count distinct customers correctly (this exercises the `lag()` translation);
   - RFM rules, recency against `$$RUN_DATE`, and so on.

   All pass on the repaired export.

Unit tests (`tests/`) build small synthetic mappings in code and *execute* the generated SQL in DuckDB:
- every join type;
- Router with overlapping groups plus a default group, into a Union;
- previous-row variables in both port orders;
- aggregates with a filter;
- unconnected ports;
- unsupported types;
- cross-mapping `ref()`.

A golden-file test pins the sample's full output. The agent mutation-checked the two subtlest rules (join direction and
previous-row reads): flipping either makes the tests fail. The demo's "break the Joiner rule" button repeats the
join-direction check on a temporary copy.

## Defects in the sample mapping

Found by the converter's warnings and confirmed by the repaired run. They are converted faithfully, because a
migration tool that silently "fixes" logic would hide changes from the people signing off the migration.

The full write-up (plain-words explanation, cause, fix and how each was confirmed) is in
[`docs/DEFECTS.md`](docs/DEFECTS.md). A copy of the export with all five fixed, and its converted dbt project, are in
`examples/FLOWLINE_DEMO_JAFFLESHOP_REPAIRED.xml` and `examples/jaffle_dbt_repaired/` (`make repair`).

| | Defect | Effect when run as-is |
|---|---|---|
| P1 | Crossed links into targets: `SUBTOTAL -> SUBTOTAL_CENTS` while `SUBTOTAL_CENTS -> SUBTOTAL` (also `TAX_PAID`, `ORDER_TOTAL`, `PRODUCT_PRICE`; `CUSTOMER_360`'s three lifetime-money columns are rotated; `PRODUCT_DAILY_PERFORMANCE` swaps `MARGIN_PCT` and `PERISHABLE_SALES_FLAG`) | staging money columns are off by 100x, and that flows into every mart |
| P2 | `STG_ORDERS.SUBTOTAL`/`TAX_PAID` are `number(15,0)`, so dollars (after the P1 swap) lose their cents | `orders.tax_paid` = 0.01 instead of 1.08 |
| P3 | `v_PREV_CUSTOMER_ID = CUSTOMER_ID` sits *above* the variable that compares with it, so the comparison is always true | `customer_order_number` never resets (it is a global running count), and `unique_customers` is always 0 |
| P4 | `JNR_ORDERS_ITEMS_AGG` makes orders the *master* of a Master Outer join, and takes `ORDER_ID` from the item side | 483 orders without items are dropped from `ORDERS` / `ORDER_FINANCIALS` |
| P5 | `IIF(PERISHABLE = 'Y' OR PERISHABLE = '1', 1, 0)`, but the data holds True/False | `is_perishable` is always 0 |

## Assumptions and what is not handled

Assumed:
- A single-folder export. Each target becomes one model named after the target; a table loaded twice gets one
  model per load, with a warning.
- Every session is truncate-and-load, so models are full-refresh `table`s.
- Source schemas come from `OWNERNAME` (`RAW` gives `source('raw', ...)`).
- Workflow links only order the mappings; dbt's DAG does the rest.
- DuckDB SQL dialect.

Not handled. Each one is either reported or makes the model fail at compile time:
- **Transformations:** Lookup (connected or `:LKP`), Update Strategy (would become incremental models or
  snapshots), Sequence Generator, Normalizer, Rank, Stored Procedure, Java/XML, Transaction Control.
- **Structure:** mapplets and shortcuts; Source Qualifier joins over several sources.
- **SQL in the source dialect:** SQL overrides and source filters are copied verbatim, with a warning.
- **Variable-port patterns:** Aggregator variable ports, and any other self-referencing variable pattern.
- **Run-time settings:** parameter files, session overrides, pre/post SQL, target update overrides,
  partitioning, error rows and `ERROR()` defaults, the workflow's command task.
- **Other dialects:** Snowflake or BigQuery would need a different function map (`strftime`, `strptime`,
  `epoch`, `date_diff`).

## Where the output is most likely wrong

1. **Order-dependent logic.** Only two variable-port shapes are recognised: previous value, and a counter with
   reset. The row order must come from an upstream Sorter through pass-through ports. If sort keys tie, both
   PowerCenter and SQL pick an arbitrary order, so results can differ run to run.
2. **Numeric semantics.** With "Enable high precision" off (as in this export), PowerCenter processes decimals
   as doubles. We use DuckDB decimal and double arithmetic and round at the target casts. Edge cases (division,
   double-to-integer rounding vs truncation, overflow) can differ.
3. **Dates.** We translate the common format tokens only. `DATE_DIFF` in months or years is fractional in
   Informatica but counts boundaries in SQL (we warn). 2-digit years can land in a different century (we warn).
   Time zones are ignored.
4. **NULL and Oracle behaviour.** Common cases are tested (IIF conditions, `||`, default IIF branches,
   aggregates). We do not emulate Oracle's "empty string is NULL".
5. **Source data contract.** Source Qualifiers cast to the declared port types, as PowerCenter does. If the
   warehouse holds different values than the original database did (True/False vs `'Y'`/`'1'`, see P5), the
   logic gives different answers, and the converter cannot know.
6. **Aggregator ports that are neither grouped nor aggregated.** PowerCenter returns the group's last row; we
   use `any_value()` and warn.

## Working with the coding agent

Built with Claude Code: Sonnet 5.5 for the investigation and plan, then Opus 5.5 after a model switch.
The agent wrote the code. I directed the work: I made the four decisions below, asked for the plan to be reviewed after
the model switch, required end-to-end testing against jaffle-shop, and asked for the defects to be fixed and written up.
After the build I had the agent walk me through the code and the results step by step; that session is part 2 of the
conversation file.

The prompts, verbatim from the first one, are in [`docs/PROMPTS.md`](docs/PROMPTS.md). The full conversation is in
[`docs/agent-conversation.md`](docs/agent-conversation.md): part 1 (investigation to first push) rebuilt from the session
transcript, part 2 (walkthrough, defect fixes, demo UI) from `/export`. Private context that Claude Code loads into
every session (global instructions, memory notes) was removed, as the file's header explains.

**Process:**
1. **Investigate.** The agent cloned the demo and both jaffle shops, profiled the XML with throwaway scripts,
   and spotted the reference mismatch and the three mappings.
2. **Decide.** The agent asked me four decisions (reference, scope, stack, repo), each with the options
   explained, then wrote a plan.
3. **Fact-check before coding.** After the switch to Opus it re-read the plan and checked the XML in detail:
   port order, master/detail flags, types. It looked up PowerCenter's port-evaluation rule in a primary source,
   because a web-search summary had stated it ambiguously.
4. **Build.** Converter in layers: IR, then expressions, then handlers, then rendering. The first `dbt build`
   passed; the verification harness came next.
5. **Turn every diff into either a converter fix or a documented mapping defect,** then prove the defect list
   with the repaired run.
6. **Write tests,** including execution-based tests on synthetic mappings, mutation checks and golden files.
   Then this README.

**Where the agent got it wrong, and how it was caught:**

- **P4 diagnosis was incomplete.** The agent first thought swapping Master Outer for Detail Outer would explain
  the 483 missing orders. In the repaired run those orders came back with a NULL `order_id`: the joiner's key
  port comes from the item side. Caught by the repaired comparison (keys "only in ours" and "only in reference");
  fixed the patch to take the key from the orders side.
- **Harness bug.** One hard-coded `converted.` schema survived a refactor of `compare.py`, so the repaired run
  was compared partly against the original schema. Caught because the counts were contradictory (equal row
  totals, 483 keys missing on both sides).
- **Wrong mapping order.** The first workflow parser read only sessions defined inside the WORKFLOW element.
  The staging session is a reusable folder-level session, so `inspect` listed core before staging. Caught by
  comparing with the demo's README (RAW, then staging, then core, then enriched). Fixed by resolving
  TASKINSTANCEs.
- **A comment could leak into live SQL.** The agent put expressions into `--` comments. One Router condition
  ends in a newline, and in the first output it left a blank line after the comment. A multi-line expression
  would have turned into executable SQL. Caught by reading the generated `stg_locations.sql`; all comments are
  now forced onto one line.
- **Misleading tool summary.** The web-search summary claimed the "previous value" variable must sit *above*
  the variable that reads it, without saying what it reads. The agent fetched the original blog example (a
  debugger trace) to confirm the actual rule before relying on it for P3.
- **False test failures.** During mutation testing, restoring the file left stale `.pyc` bytecode (same size,
  same mtime second), so the "restored" code still failed. Caught by checking the file content against the
  behaviour; the cache was cleared.
- **Messy first draft.** The first draft of the Expression translator used exceptions for control flow and had
  dead code. The agent rewrote it during its own review, before any tests existed.

## Repository layout

```
src/pwc_to_dbt/   model.py (XML -> IR), expr.py (expressions), convert.py (graph -> models), render.py, cli.py
tests/            synthetic mappings executed in DuckDB, expression tests, sample + golden tests, e2e
verify/           e2e.py, compare.py + reference_map.toml, repair_mapping.py, invariants.py, REPORT.md
examples/         the sample export, its converted dbt project (golden output), and the repaired export + project
demo/             interactive presentation of this project (make demo)
docs/             DEFECTS.md, prompts and the agent conversation
```
