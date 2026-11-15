# Demo: an interactive walk-through of pwc-to-dbt

A local web page that walks through the project in eleven slides (about 22 minutes): the data, the problem, the input XML, how
formulas are translated, the generated output, the proof against jaffle-shop, the five defects found in the
sample, the tests, limits, and the decisions behind it. Every slide has talking points beside it and live parts:
you can run the tests, convert the sample, run the end-to-end check, query the DuckDB database, translate a formula,
click through a mapping graph, and open any file at the right line.

It uses only the Python standard library and plain HTML/JS. Nothing is loaded from the internet.

## Run it

```bash
make install          # once: creates .venv with the converter, dbt-duckdb, duckdb and pytest
make e2e              # once: builds verify/work/jaffle.duckdb (needs internet the first time, to clone jaffle-shop)
make demo             # starts the server and opens http://127.0.0.1:8765
```

Options (run the server directly): `uv run python demo/server.py --port 9000 --no-browser`.
Stop it with Ctrl+C.

`make e2e` can be skipped if you only want the slides, the graphs, the file viewer, the formula translator,
convert, tests and the Joiner demo. The **proof**, **defects** and SQL console need the database from `make e2e`
(or the **Run: full end-to-end check** button on slide 7, which runs the same thing).

Tested with Chrome. Any current browser should work, and the page follows the system light or dark theme.

## Using it

| Key | Action |
|---|---|
| Left / right arrow, Page Up / Down | previous / next slide |
| 1 ... 9, 0 | jump to slide 1 ... 9, 10 (use the arrows for slide 11) |
| Esc | close the file viewer |

- **Key points** (what to say) and **Mention** (the bugs found, the decisions and why) are always visible beside each slide.
- Buttons named **Run: ...** execute a real command and stream its output. Only one runs at a time.
- File links open the file in a side panel with the relevant lines highlighted, and have an "Open on GitHub" link.
- The timer in the top bar is for rehearsal. Click it to reset.
- Slide 6 (the hard cases) is optional, so skip it when short of time.

| Button | What it runs |
|---|---|
| Run: inspect | `pwc-to-dbt inspect` on the staging mapping |
| Run: convert and diff | converts the sample into a temporary folder and compares every file with `examples/jaffle_dbt` |
| Run: full end-to-end check | the same as `make e2e` (about 30 s; first run needs internet) |
| Run: all unit tests | `pytest -v` |
| Run: break the Joiner rule | flips Master Outer in a **temporary copy** of the source and runs the Joiner tests, which must fail. Your files are never modified. |

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| "database locked by another process" when running e2e | A `duckdb` shell (or another tool) has `verify/work/jaffle.duckdb` open. Type `.quit` in it, or close it, and run again. A read-only shell is enough to block a rebuild. |
| "verify/work/jaffle.duckdb does not exist yet" | Run `make e2e` once (or the button on slide 7). |
| "cannot listen on 127.0.0.1:8765" | The port is in use. Use `--port`. |
| The browser does not open | Open the printed address yourself. |
| Slide text changed but the page still shows the old text | Slide text is loaded when the server starts. Restart it. Code and styles reload on refresh. |

## Private presenter notes (optional, not in git)

If `demo/local_notes.py` exists, the server adds its content to the slides. The file is listed in `.gitignore`, so it
stays on your machine. Use it for anything you want on screen while presenting but not in the repository, for example
rehearsed answers:

```python
BLOCKS = {  # slide id -> extra blocks, in the same format as demo/slides.py
    "decisions": [{"type": "qa", "title": "Notes", "items": [{"q": "A question?", "a": "The answer."}]}],
}
MENTION = {"defects": ["An extra line for the Mention panel"]}  # slide id -> extra lines
```

Slide ids are in `demo/slides.py`. Restart the server after editing the file. Without it the demo is complete.

## Testing the demo

```bash
make test    # includes tests/test_demo.py: guards, endpoints, every graph, every slide link and query (about 1 s)
make ui      # tests/test_demo_ui.py: drives the page in Chrome with Playwright (about 1 min)
```

`make ui` runs `uv run --with playwright pytest -m ui`, so Playwright is installed on the fly and nothing is added to
the project's dependencies. It uses your installed Google Chrome (or Playwright's Chromium after
`uv run --with playwright python -m playwright install chromium`). It clicks every button, opens every file link,
walks every mapping graph, runs the SQL console (including refused statements), runs the full end-to-end check from
the page, and checks light/dark themes and small screens. The end-to-end parts skip themselves if another process
(for example a `duckdb` shell) holds `verify/work/jaffle.duckdb`.

## Safety

- The server listens on `127.0.0.1` only and rejects other `Host` and `Origin` headers.
- It cannot run arbitrary commands: the buttons map to a fixed list (`ACTIONS` in `server.py`).
- The file viewer only shows files inside the repository, and refuses `.git`, `.venv`, `verify/work`, `verify/reference`,
  database files and similar. It redacts the sample's plaintext database password the same way the conversion report does.
- The SQL console runs one `SELECT`-style statement on a read-only connection with file access disabled, so it cannot
  change the database or read other files, and it never holds the database lock.

## How it is built

```
demo/server.py        HTTP server and the JSON API (files, graphs, actions, SQL, formula translator)
demo/graph.py         draws a mapping as an SVG from the converter's own data (one box per transformation)
demo/slides.py        the eleven slides as data: key points, mentions, links, buttons, queries (edit text here)
demo/static/          index.html, app.js, style.css (no dependencies)
tests/test_demo.py    guards, endpoints, every graph, and a check that every link and query in the slides still works
tests/test_demo_ui.py browser tests (Playwright), run with `make ui`
```

In PyCharm, run `demo/server.py` from the editor (right-click, Run) with the project interpreter `.venv`, or use the
Terminal tab: `make demo`.
