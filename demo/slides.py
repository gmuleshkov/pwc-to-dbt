"""The demo script: ten slides as data. The page (demo/static/app.js) renders them.

Per slide:
  points   what the slide shows and says
  mention  the moments not to skip: bugs found, decisions and why
  blocks   the visual and interactive parts, in order (see app.js for each type)

Text supports `code` and **bold**. Hints are always visible on the page, so they are written as plain talking points.
"""

XML = "examples/FLOWLINE_DEMO_JAFFLESHOP.xml"
STG_ORDERS_SQL = "examples/jaffle_dbt/models/fl_js_staging_full/stg_orders.sql"

QUERY_P1 = """select r.order_id, r.subtotal as reference_dollars, c.subtotal as converted_subtotal,
       c.subtotal_cents as converted_cents_column
from reference.stg_orders r join converted.stg_orders c using (order_id)
order by order_id limit 3"""

QUERY_P2 = """select order_id, r.tax_paid as reference_tax, c.tax_paid_cents as converted_tax_column,
       p.tax_paid as repaired_tax
from reference.stg_orders r join converted.stg_orders c using (order_id) join repaired.stg_orders p using (order_id)
where r.tax_paid <> round(r.tax_paid) order by order_id limit 3"""

QUERY_P3 = """select c.customer_id, c.ordered_at_day, r.customer_order_number as reference_n,
       c.customer_order_number as converted_n
from converted.orders c join reference.orders r using (order_id)
where c.customer_order_number <> r.customer_order_number
order by c.customer_id, c.ordered_at_day limit 3"""

QUERY_P4 = """select (select count(*) from reference.orders) as reference_orders,
       (select count(*) from converted.orders) as converted_orders,
       (select count(*) from repaired.orders) as repaired_orders"""

QUERY_P5 = """select (select sum(is_perishable_supply::int) from reference.stg_supplies) as reference_perishable,
       (select sum(is_perishable::int) from converted.stg_supplies) as converted_perishable,
       (select sum(is_perishable::int) from repaired.stg_supplies) as repaired_perishable"""

SLIDES = [
    # ------------------------------------------------------------------------------------------------ 1
    {
        "id": "problem",
        "short": "Problem",
        "title": "The problem and the approach",
        "subtitle": "Informatica PowerCenter mappings (XML) become dbt models (SQL)",
        "core": True,
        "minutes": 2,
        "points": [
            "**PowerCenter** is an ETL tool: pipelines are drawn as boxes and arrows (a *mapping*), run row by row on "
            "Informatica's own engine, and saved as an XML export.",
            "**dbt** is the modern way: each step is a SQL `SELECT` in a file. dbt runs the files in dependency order "
            "inside the warehouse (ELT).",
            "The task: a tool that converts the export into a dbt project, so a migration does not mean rewriting "
            "thousands of mappings by hand.",
            "The sample is the public jaffle shop rebuilt in PowerCenter: 3 mappings, 16 targets, 98 transformations, "
            "1,066 port links, 4,701 lines of XML.",
        ],
        "mention": [
            "**Decision:** Python with a real expression parser, not regex and not an LLM at run time. Output is "
            "deterministic, testable and auditable.",
            "**Decision:** all three mappings, not only the staging one. The enriched marts have no reference, so they "
            "are checked with independent invariants.",
            "**Finding:** the task links the *old* `jaffle_shop_duckdb`, but the XML mirrors the *new* "
            "`dbt-labs/jaffle-shop` (stores, items, supplies). I verify against the new one and say so.",
            "Principle used throughout: **faithful, plus loud**. Reproduce what PowerCenter does, warn about what looks wrong.",
        ],
        "blocks": [
            {"type": "compare",
             "left": {"title": "PowerCenter (ETL)", "items": [
                 "Mapping = graph of transformations and connectors",
                 "Formulas in Informatica's own language (`IIF`, `TO_DATE`...)",
                 "Engine processes **row by row**, variables keep values between rows",
                 "Transform happens on the Informatica server, then load",
                 "Saved as an XML export"]},
             "right": {"title": "dbt (ELT)", "items": [
                 "Model = one `SELECT` in one `.sql` file",
                 "`ref()` / `source()` give the dependency order",
                 "The database processes **all rows at once** (set-based)",
                 "Raw data is loaded first, transformed inside the warehouse",
                 "Plain files in git, runnable with `dbt build`"]}},
            {"type": "flow", "steps": [
                {"label": "PowerCenter XML", "sub": "input", "path": XML},
                {"label": "model.py", "sub": "XML to objects", "path": "src/pwc_to_dbt/model.py"},
                {"label": "convert.py + expr.py", "sub": "graph and formulas to SQL",
                 "path": "src/pwc_to_dbt/convert.py"},
                {"label": "render.py", "sub": "write the dbt project", "path": "src/pwc_to_dbt/render.py"},
                {"label": "dbt project + report", "sub": "output", "path": "examples/jaffle_dbt/conversion_report.md"},
            ]},
            {"type": "files", "title": "Read more", "items": [
                {"label": "README: what the sample mapping does", "path": "README.md", "section": "What the sample mapping does"},
                {"label": "Plan decisions (prompts)", "path": "docs/PROMPTS.md"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 2
    {
        "id": "input",
        "short": "Input",
        "title": "The input: a PowerCenter export",
        "subtitle": "A graph of transformations plus formulas written as text",
        "core": True,
        "minutes": 2,
        "points": [
            "The XML has source and target table definitions, three **mappings**, a session per mapping, and one "
            "**workflow** that runs them in order.",
            "A mapping is a graph: transformation **instances** (nodes) joined by **connectors** (`instance.port -> "
            "instance.port`).",
            "Logic is not in the structure. It is text inside `EXPRESSION=\"...\"` attributes, in Informatica's "
            "language, so the converter needs a real parser.",
            "`inspect` prints the chain of boxes behind each target. Staging has straight lines, a join, and a "
            "Router + Union.",
        ],
        "mention": [
            "**Bug P1 is visible right here:** in the connectors into `STG_ORDERS`, `SUBTOTAL` is linked to "
            "`SUBTOTAL_CENTS` and `SUBTOTAL_CENTS` to `SUBTOTAL`. Dollars land in the cents column.",
            "The XML also holds a plaintext database password in a workflow command task. The converter redacts it in "
            "its report, and this viewer redacts it too.",
            "The sources are declared as Oracle. The converter never connects to a database: it only translates logic.",
        ],
        "blocks": [
            {"type": "file", "title": "EXP_ORDERS: ports and formulas", "path": XML, "lines": [106, 124],
             "note": "OUTPUT ports carry an EXPRESSION; INPUT ports arrive from upstream. Note `SUBTOTAL_IN/100` and "
                     "the odd `TO_DATE(TO_CHAR(...))` date trick."},
            {"type": "file", "title": "Connectors into STG_ORDERS (look at the SUBTOTAL lines)", "path": XML,
             "lines": [604, 613], "note": "Each connector says which output port feeds which target column."},
            {"type": "action", "action": "inspect", "label": "Run: inspect the staging mapping",
             "params": {"mapping": "m_FL_JS_STAGING_FULL"}, "command": "pwc-to-dbt inspect FLOWLINE_DEMO_JAFFLESHOP.xml"},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 3
    {
        "id": "how",
        "short": "Formulas",
        "title": "How it works: formulas",
        "subtitle": "Tokenizer, then parser, then SQL emitter",
        "core": True,
        "minutes": 2.5,
        "points": [
            "**Tokenizer:** one regular expression cuts the text into names, numbers, strings and operators.",
            "**Parser:** a hand-written recursive-descent / Pratt parser builds a tree using **Informatica's operator "
            "precedence**, which differs from SQL (for example `NOT` binds tighter than `=`).",
            "**Emitter:** walks the tree and writes SQL, tracking types, because `TRUNC`, `TO_CHAR` and `IIF` defaults "
            "mean different things for dates and numbers.",
            "Names are resolved by the caller: a column, a `$$` variable (a dbt `var`), or a `lag()` for previous-row logic.",
        ],
        "mention": [
            "**Anything not understood raises `Unsupported`.** The converter never guesses.",
            "Semantics are not just syntax: `'a' || NULL` is `'a'` in Informatica, so it becomes `concat()`; `7 / 2` is 3.5; "
            "`IIF` with no else defaults by type. Tests execute the SQL to check these.",
            "**Why not regex or an LLM per formula:** regex breaks on nesting and precedence; an LLM answers "
            "differently on different days and cannot be unit-tested.",
        ],
        "blocks": [
            {"type": "translator", "formula": "IIF(ISNULL(SUBTOTAL_IN), 0, SUBTOTAL_IN / 100)",
             "columns": "SUBTOTAL_IN:double",
             "examples": [
                 "IIF(ISNULL(SUBTOTAL_IN), 0, SUBTOTAL_IN / 100)",
                 "'a' || NULL",
                 "TO_DATE(TO_CHAR(ORDERED_AT, 'DD-MM-YY'), 'DD-MM-YY')",
                 "NOT A = B",
                 "DECODE(N, 1, 'one', 2, 'two', 'many')",
                 "SOUNDEX(NAME)",
             ]},
            {"type": "files", "title": "Where it lives", "items": [
                {"label": "Tokenizer regex", "path": "src/pwc_to_dbt/expr.py", "lines": [28, 40]},
                {"label": "Informatica precedence table", "path": "src/pwc_to_dbt/expr.py", "lines": [105, 120]},
                {"label": "Test: precedence follows Informatica", "path": "tests/test_expr.py", "lines": [29, 36]},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 4
    {
        "id": "output",
        "short": "Output",
        "title": "The output: one model per target",
        "subtitle": "Each PowerCenter box becomes one CTE, in the same order",
        "core": True,
        "minutes": 2.5,
        "points": [
            "Each **target table** becomes one dbt model. Each **transformation** becomes one CTE, so a reviewer can put "
            "the PowerCenter graph and the SQL side by side.",
            "Click a box in the graph: the matching CTE is highlighted in the generated SQL.",
            "A transformation feeding several targets becomes an `int_*` view, so shared logic is written once.",
            "Targets are `table` models, because the sessions truncate and reload on every run.",
        ],
        "mention": [
            "The header of every model lists the flow and every **WARNING**. In `stg_orders` there are six crossed-link "
            "warnings and a 2-digit-year warning.",
            "In the final CTE, `subtotal` is cast into `subtotal_cents` and the reverse: the converter reproduced the "
            "crossed links on purpose.",
            "**Output is deterministic.** The convert button diffs a fresh conversion against the committed golden project.",
            "Unsupported items never produce wrong SQL: the model raises a dbt compile error that names the node.",
        ],
        "blocks": [
            {"type": "graph", "default": {"mapping": "m_FL_JS_STAGING_FULL", "target": "STG_ORDERS"}},
            {"type": "action", "action": "convert", "label": "Run: convert the sample and diff against the golden project",
             "command": "pwc-to-dbt convert FLOWLINE_DEMO_JAFFLESHOP.xml -o <temp>"},
            {"type": "files", "title": "Files", "items": [
                {"label": "Conversion report (warnings, redacted password)",
                 "path": "examples/jaffle_dbt/conversion_report.md"},
                {"label": "dbt_project.yml (materializations, vars)", "path": "examples/jaffle_dbt/dbt_project.yml"},
                {"label": "A model that uses ref(): core customers",
                 "path": "examples/jaffle_dbt/models/fl_js_marts_core/customers.sql"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 5
    {
        "id": "hard",
        "short": "Hard cases",
        "title": "The hard cases",
        "subtitle": "Where row-by-row and set-based thinking differ",
        "core": False,
        "minutes": 2,
        "points": [
            "**Joiner:** the names are counter-intuitive. *Master Outer* keeps all **detail** rows, so it becomes "
            "`detail LEFT JOIN master`. A test pins this down.",
            "**Router:** a row goes to **every** group whose condition is true (not just the first); the default group "
            "gets the rest. The converter warns when groups overlap.",
            "**Variable ports:** evaluated top to bottom for each row, and a variable keeps its value into the next row. "
            "A read below the definition means *this row*; a read above means *the previous row*, so it becomes `lag()`. "
            "A counter with reset becomes a run-group id plus a running sum.",
        ],
        "mention": [
            "Only **two** variable-port shapes are recognised: previous value, and a counter with reset. Anything else "
            "fails loudly instead of being guessed.",
            "The row order for `lag()` comes from the upstream Sorter. If sort keys tie, PowerCenter and SQL both pick "
            "an arbitrary order, so results may differ between runs.",
        ],
        "blocks": [
            {"type": "graph", "default": {"mapping": "m_FL_JS_STAGING_FULL", "target": "STG_LOCATIONS"},
             "title": "Router and Union: STG_LOCATIONS"},
            {"type": "files", "title": "Code and tests", "items": [
                {"label": "Joiner handler: Master Outer = detail LEFT JOIN master", "path": "src/pwc_to_dbt/convert.py",
                 "lines": [585, 595]},
                {"label": "Test: every join type", "path": "tests/test_transformations.py", "lines": [30, 41]},
                {"label": "Test: Router overlapping groups", "path": "tests/test_transformations.py", "lines": [43, 100]},
                {"label": "Previous-row variables (lag, counters)", "path": "src/pwc_to_dbt/convert.py", "lines": [770, 800]},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 6
    {
        "id": "proof",
        "short": "Proof",
        "title": "The proof: compare with the real jaffle shop",
        "subtitle": "One DuckDB file, four schemas, two runs",
        "core": True,
        "minutes": 2.5,
        "points": [
            "PowerCenter cannot be run here (licensed server, Oracle). So the independent judge is the **real "
            "dbt-labs/jaffle-shop**, which the XML re-implements.",
            "`make e2e` loads the sample data into DuckDB (`raw`), builds the reference models (`reference`), converts "
            "the original XML (`converted`) and a repaired copy (`repaired`), then compares every shared column row by row.",
            "DuckDB is the stand-in warehouse: an embedded analytical database, one file, no server.",
            "Every difference must be explained in `reference_map.toml`, or the run fails. A listed difference that "
            "disappears also fails (a stale expectation).",
        ],
        "mention": [
            "**The first run did not match.** The differences turned out to be five defects in the mapping, not in the "
            "converter (next slide).",
            "**Run 2 is the proof of the diagnosis:** patch only those five spots, convert again, and the result matches "
            "the reference on every shared column.",
            "**Honest limit:** the reference is jaffle-shop's output, not PowerCenter's real output. A real migration "
            "would diff against the legacy system's tables, on the same data.",
            "The 5 enriched marts have no reference model: 10 independent SQL invariants check them.",
        ],
        "blocks": [
            {"type": "action", "action": "e2e", "label": "Run: full end-to-end check (about 30 s)",
             "command": "make e2e", "refresh": True,
             "note": "If a `duckdb` shell is open on verify/work/jaffle.duckdb, close it first (it holds a lock)."},
            {"type": "compare-table", "run": "original", "title": "Run 1: original export vs reference"},
            {"type": "compare-table", "run": "repaired", "title": "Run 2: repaired export vs reference"},
            {"type": "files", "title": "Files", "items": [
                {"label": "e2e orchestration", "path": "verify/e2e.py"},
                {"label": "Explained differences, with causes", "path": "verify/reference_map.toml"},
                {"label": "Generated report", "path": "verify/REPORT.md"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 7
    {
        "id": "defects",
        "short": "Defects",
        "title": "Five defects in the sample mapping",
        "subtitle": "Reproduced faithfully, warned about where possible, fixed in a separate copy",
        "core": True,
        "minutes": 3,
        "points": [
            "The converter reproduces what PowerCenter would do, defects included. A tool that silently fixes logic "
            "would hide changes from the people who sign off a migration.",
            "Each defect below has a live query: reference vs converted (original XML) vs repaired.",
            "Only **P1 and P3** can be detected from the XML alone and the converter warns about them. P2, P4 and P5 "
            "look intentional until compared with data, so a lint for them would only guess.",
            "The fixed export and its dbt project are in the repo (`make repair`), with a list of every change.",
        ],
        "mention": [
            "**None of the five is a converter bug.** Each was confirmed, not guessed: patch the XML, re-run, get a match.",
            "**P2 needs an owner's decision:** store dollars (`number(12,2)`) or store cents. We patched the type.",
            "One difference remains by design, not a defect: for orders without items the mapping writes `0`, "
            "jaffle-shop leaves `NULL`.",
            "I cannot tell whether the defects were planted. The demo repo says only \"for demo purposes only\".",
            "**Agent mistake on P4:** the first diagnosis was incomplete. The repaired run showed NULL order ids, which "
            "led to the key-port fix.",
        ],
        "blocks": [
            {"type": "defects", "items": [
                {"id": "P1", "title": "Crossed links",
                 "plain": "Output ports are wired to the wrong target columns: dollars go into `_cents` columns and the reverse.",
                 "effect": "Money is off by 100x, and so is every mart that sums it (13 links, 4 targets).",
                 "warns": True, "fix": "Relink each port to the target column with the same name.",
                 "query": QUERY_P1, "path": XML, "lines": [604, 613]},
                {"id": "P2", "title": "Whole-dollar money",
                 "plain": "`SUBTOTAL` and `TAX_PAID` hold dollars but are declared `number(15,0)`: no decimals.",
                 "effect": "Cents are rounded away: tax 0.20 becomes 0 (61,296 orders affected).",
                 "warns": False, "fix": "Declare `number(12,2)`, or store cents (owner's decision).",
                 "query": QUERY_P2, "path": XML, "lines": [72, 76]},
                {"id": "P3", "title": "Variable-port order",
                 "plain": "A \"previous customer\" variable is placed above the one that reads it, so it is already overwritten.",
                 "effect": "The order counter never restarts for a new customer (customer 2's first order is 191); 61,275 wrong values.",
                 "warns": True, "fix": "Move the variable below the ones that read it.",
                 "query": QUERY_P3, "path": XML, "lines": [1705, 1706]},
                {"id": "P4", "title": "Joiner keeps the wrong side",
                 "plain": "A Master Outer join with orders as master keeps every *detail* row, so orders without items vanish.",
                 "effect": "483 orders are missing from `orders` and `order_financials`.",
                 "warns": False, "fix": "Switch to Detail Outer and take the key from the orders side.",
                 "query": QUERY_P4, "path": XML, "lines": [1573, 1600]},
                {"id": "P5", "title": "Flag never matches",
                 "plain": "`PERISHABLE = 'Y' OR '1'`, but the data holds `True` / `False`.",
                 "effect": "`is_perishable` is always 0 (the reference has 30).",
                 "warns": False, "fix": "Also accept `'TRUE'`.",
                 "query": QUERY_P5, "path": XML, "lines": [504, 504]},
            ]},
            {"type": "sql", "queries": [
                {"label": "what is in the database", "sql": "select table_schema, count(*) as tables\nfrom information_schema.tables\ngroup by 1 order by 1"},
                {"label": "reference: first orders", "sql": "select order_id, subtotal, tax_paid, order_total\nfrom reference.stg_orders order by order_id limit 5"},
                {"label": "converted: same orders (P1)", "sql": "select order_id, subtotal_cents, subtotal, order_total\nfrom converted.stg_orders order by order_id limit 5"},
                {"label": "repaired: same orders", "sql": "select order_id, subtotal, tax_paid, order_total\nfrom repaired.stg_orders order by order_id limit 5"},
                {"label": "rows that differ, reference vs repaired", "sql": "select count(*) as differing_rows from (\n  select order_id, subtotal, tax_paid, order_total from reference.stg_orders\n  except\n  select order_id, subtotal, tax_paid, order_total from repaired.stg_orders)"},
            ]},
            {"type": "files", "title": "The fix and the write-up", "items": [
                {"label": "docs/DEFECTS.md (full write-up)", "path": "docs/DEFECTS.md"},
                {"label": "verify/repair_mapping.py (the five patches)", "path": "verify/repair_mapping.py"},
                {"label": "examples/REPAIR_CHANGES.txt (every change)", "path": "examples/REPAIR_CHANGES.txt"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 8
    {
        "id": "tests",
        "short": "Tests",
        "title": "Tests",
        "subtitle": "83 fast tests, most of them executing the generated SQL",
        "core": True,
        "minutes": 2,
        "points": [
            "**Unit tests build tiny PowerCenter exports in code** and run the generated SQL in an in-memory DuckDB, "
            "so each rule is checked by its result, not by comparing SQL text.",
            "**Golden-file tests** pin the sample's full output (and the repaired export's). A change shows up as a diff.",
            "The slow end-to-end check is separate (`make e2e`), because it runs dbt.",
            "Formula tests include execution checks: NULL handling, division, `IIF` defaults, 2-digit years.",
        ],
        "mention": [
            "**Mutation check:** the button below flips the Joiner direction in a temporary copy. The Joiner tests must "
            "fail; if they did not, the rule would not be protected.",
            "The tests prove our *reading* of Informatica's rules is implemented correctly. If the reading were wrong, the "
            "tests would be wrong the same way, which is why the independent e2e comparison exists.",
            "Only DuckDB is tested. Another warehouse needs a dialect layer.",
        ],
        "blocks": [
            {"type": "action", "action": "tests", "label": "Run: all unit tests", "command": "pytest -v"},
            {"type": "action", "action": "break-joiner", "label": "Run: break the Joiner rule (temporary copy) and watch the tests fail",
             "command": "flip Master Outer in a copy, then pytest -k joiner", "expectFail": True},
            {"type": "files", "title": "Files", "items": [
                {"label": "Test helpers: build a mapping in code, run it in DuckDB", "path": "tests/helpers.py"},
                {"label": "Joiner types test", "path": "tests/test_transformations.py", "lines": [30, 41]},
                {"label": "Test: variable-port order reproduced and flagged", "path": "tests/test_transformations.py",
                 "lines": [109, 118]},
                {"label": "Golden-file tests", "path": "tests/test_sample.py"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 9
    {
        "id": "limits",
        "short": "Limits",
        "title": "Limits and how it would scale",
        "subtitle": "What is handled, what is reported, what is not",
        "core": True,
        "minutes": 2,
        "points": [
            "The converter handles the transformation types the sample uses: Source Qualifier, Expression, Filter, Sorter, "
            "Joiner, Aggregator, Router, Union, Target.",
            "Everything else becomes a model that **fails at compile time** with a message naming the node, and is listed "
            "in the conversion report.",
            "Scaling to hundreds of mappings: output is deterministic, `--strict` exits non-zero when anything is "
            "unsupported (CI-friendly), and the report ranks what to implement next.",
        ],
        "mention": [
            "**Not handled:** Lookup, Update Strategy, Sequence Generator, Normalizer, Rank, Stored Procedure, mapplets "
            "and shortcuts, parameter files, session overrides, partitioning, orchestration and scheduling.",
            "**Not handled:** other SQL dialects (the function map is DuckDB), Oracle's empty-string-is-NULL, time zones.",
            "**Most likely wrong:** order-dependent logic with tied sort keys; numeric edge cases (PowerCenter high "
            "precision off); date formats; the source data contract (see P5).",
            "After a migration the legacy tables are the baseline: run both on the same data and diff, as parity testing.",
        ],
        "blocks": [
            {"type": "table", "head": ["PowerCenter", "Status", "dbt output"], "rows": [
                ["Source Qualifier", "handled", "`select cast(...) from source()` or `ref()`"],
                ["Expression (incl. variable ports)", "handled (2 variable shapes)", "`select expr`, `lag()`, running sums"],
                ["Filter, Sorter, Aggregator", "handled", "`where`, metadata only, `group by`"],
                ["Joiner (normal / master / detail / full)", "handled", "inner / left / full join"],
                ["Router + Union", "handled", "one CTE per group, `union all`"],
                ["Target", "handled", "cast to target types, `table` model"],
                ["Lookup", "not yet", "compile-error model; next to implement"],
                ["Update Strategy, Sequence Generator, Normalizer, Rank", "not yet", "compile-error model"],
                ["Mapplets, shortcuts", "not handled", "reported; must be expanded first"],
            ]},
            {"type": "qa", "title": "If asked", "items": [
                {"q": "How would you add Lookup?",
                 "a": "A new handler: a connected Lookup is a left join to the lookup table. The hard part is the policy "
                      "for multiple matches (first, last or error), which must come from the Lookup's settings. An "
                      "unconnected `:LKP` call would become a scalar subquery. Each needs execution tests."},
                {"q": "What about 500 mappings?",
                 "a": "Run with `--strict` in CI, read the report to see which unsupported types occur most, implement "
                      "those first, and keep golden files per export. Parity testing against legacy output decides "
                      "when each mapping is done."},
                {"q": "Does it work on Snowflake or BigQuery?",
                 "a": "Not yet. Function names (`strftime`, `strptime`, `date_diff`) are DuckDB's. A dialect layer "
                      "in the emitter is the next step."},
            ]},
            {"type": "files", "title": "Read more", "items": [
                {"label": "README: assumptions and what is not handled", "path": "README.md", "section": "Assumptions and what is not handled"},
                {"label": "README: where the output is most likely wrong", "path": "README.md", "section": "Where the output is most likely wrong"},
            ]},
        ],
    },
    # ------------------------------------------------------------------------------------------------ 10
    {
        "id": "decisions",
        "short": "Decisions",
        "title": "Decisions, process and what is next",
        "subtitle": "How it was built with a coding agent, and where the agent was wrong",
        "core": True,
        "minutes": 2,
        "points": [
            "Built with Claude Code: Sonnet 5.5 for investigation and the plan, then Opus 5.5 for the build. Prompts are in "
            "`docs/PROMPTS.md`, and the full conversation is in the repo.",
            "Process: investigate, decide with the options explained, fact-check the plan, build in layers, turn every diff "
            "into a converter fix or a documented mapping defect, then write tests.",
            "Every claim in the README is backed by something runnable: `make test`, `make e2e`, `make repair`.",
        ],
        "mention": [
            "**Where the agent got it wrong:** the P4 diagnosis was incomplete; a hard-coded schema survived a refactor of "
            "the compare script; the first workflow parser listed mappings in the wrong order; an expression put in a "
            "`--` comment could have leaked into live SQL; a stale `.pyc` made restored code look broken.",
            "**How each was caught:** by the repaired comparison, contradictory counts, the demo repo's README, reading the "
            "generated SQL, and checking behaviour against file content. Verification was designed to catch the agent, too.",
            "**Next steps:** Lookup, Update Strategy (incremental or snapshot), Sequence Generator, a dialect layer, "
            "parameter files, mapplet expansion.",
        ],
        "blocks": [
            {"type": "cards", "title": "Decisions and why", "items": [
                {"title": "Parser, not regex or an LLM",
                 "text": "Precedence and nesting need a real grammar; deterministic output can be tested and audited."},
                {"title": "Faithful, plus loud",
                 "text": "A migration must not change behavior silently. Reproduce, warn, let the owners decide."},
                {"title": "One CTE per box, one model per target",
                 "text": "Reviewable one-to-one against the mapping; shared logic becomes `int_*` views."},
                {"title": "Unsupported becomes a compile error",
                 "text": "A model that cannot be converted fails loudly and names the node; it is never silently wrong."},
                {"title": "Verify against the real jaffle shop",
                 "text": "An independent oracle, with every difference explained, plus a repaired run to prove the diagnosis."},
                {"title": "Tables, DuckDB dialect",
                 "text": "Sessions truncate and reload, so full-refresh tables. DuckDB keeps the demo local; a dialect layer comes later."},
            ]},
            {"type": "qa", "title": "Likely questions", "items": [
                {"q": "Why not just ask an LLM to convert it?",
                 "a": "Same input must give the same output, and every rule needs a test. An LLM is good for leftovers the "
                      "converter reports as unsupported, with a human review, not as the engine."},
                {"q": "Why reproduce the bugs?",
                 "a": "A migration tool that changes behavior silently hides it from the owners. We warn, prove, and offer "
                      "a repaired copy as a separate, reviewable step."},
                {"q": "How do you know it is correct without PowerCenter?",
                 "a": "Two independent checks: tests that encode Informatica's documented rules, and a row-by-row comparison "
                      "with the real jaffle shop. The limit: a real migration would diff against actual PowerCenter output."},
                {"q": "What would you do first on a real customer export?",
                 "a": "Run it with `--strict`, read the report for unsupported types, then set up parity tests against the "
                      "legacy tables before changing any logic."},
                {"q": "What did the agent get wrong?",
                 "a": "See the README section \"Working with the coding agent\": seven cases, each with how it was caught."},
            ]},
            {"type": "files", "title": "Files", "items": [
                {"label": "README: working with the coding agent", "path": "README.md", "section": "Working with the coding agent"},
                {"label": "Prompts, verbatim", "path": "docs/PROMPTS.md"},
                {"label": "Defects write-up", "path": "docs/DEFECTS.md"},
            ]},
        ],
    },
]
