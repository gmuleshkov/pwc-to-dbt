"""Browser tests for the demo page (Playwright). Not part of `make test`; run with `make ui`.

    uv run --with playwright pytest -m ui -s

Uses the installed Google Chrome (or Playwright's Chromium if you ran `playwright install chromium`).
The server is started on a free port. The end-to-end tests need verify/work/jaffle.duckdb unlocked.
"""

import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api", reason="run with: uv run --with playwright pytest -m ui")
expect = sync_api.expect

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "verify" / "work" / "jaffle.duckdb"
pytestmark = pytest.mark.ui

sys.path.insert(0, str(ROOT / "demo"))
import slides  # noqa: E402

SLIDE_COUNT = len(slides.SLIDES)
SLOW = 240_000  # ms, for the end-to-end run


# -- fixtures

@pytest.fixture(scope="module")
def url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, str(ROOT / "demo" / "server.py"), "--no-browser", "--port", str(port)],
                            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/api/status", timeout=1).read()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("demo server did not start")
    yield base
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Exception:  # noqa: BLE001 - fall back to Playwright's own Chromium
            try:
                b = p.chromium.launch()
            except Exception as e:  # noqa: BLE001
                pytest.skip(f"no browser: install Chrome or run `playwright install chromium` ({e})")
        yield b
        b.close()


class Seen:
    def __init__(self):
        self.js_errors, self.console_errors, self.bad_responses = [], [], []


@pytest.fixture
def page(browser, url):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
    pg = ctx.new_page()
    pg.set_default_timeout(20_000)
    pg.seen = Seen()
    pg.on("pageerror", lambda e: pg.seen.js_errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and "Failed to load resource" not in m.text
          and pg.seen.console_errors.append(m.text))
    pg.on("response", lambda r: r.status >= 400 and pg.seen.bad_responses.append(f"{r.status} {r.url}"))
    pg.goto(url)
    expect(pg.locator("#slide h1")).to_be_visible()
    pg.base = url
    yield pg
    ctx.close()


def go(page, n):
    page.locator("#steps button").nth(n - 1).click()
    expect(page.locator("#slide h1")).to_have_text(re.compile(rf"^{n}\. "))


def run_action(page, key, button_text, done=r"OK|FAILED|caught", timeout=30_000):
    card = page.locator(f'[data-action^="{key}"]')
    card.get_by_role("button", name=button_text).click()
    badge = card.locator(".badge")
    expect(badge).to_have_text(re.compile(done), timeout=timeout)
    return card, badge.inner_text(), card.locator(".term").inner_text()


def db_unlocked():
    if not DB.exists():
        return False
    import duckdb

    try:
        duckdb.connect(str(DB)).close()
        return True
    except duckdb.IOException:
        return False


needs_db = pytest.mark.skipif(not DB.exists(), reason="run `make e2e` once to build verify/work/jaffle.duckdb")


# -- every slide

def test_every_slide_renders_cleanly_with_hints_and_no_failed_requests(page):
    for n in range(1, SLIDE_COUNT + 1):
        go(page, n)
        expect(page.locator(".hints .card").nth(0)).to_contain_text("Key points")
        expect(page.locator(".hints .card.mention")).to_contain_text("Mention")
        assert page.locator(".hints li").count() >= 5, f"slide {n}: too few hints"
        page.wait_for_timeout(500)  # let fetches (graphs, tables) finish
    assert page.seen.js_errors == [] and page.seen.console_errors == []
    # slide 6/7 need the database; the others must never produce a failed request
    failed = [r for r in page.seen.bad_responses if "/api/compare" not in r]
    assert failed == []


def test_navigation_by_keys_buttons_hash_and_nav_bar(page):
    page.keyboard.press("ArrowRight")
    expect(page.locator("#slide h1")).to_have_text(re.compile(r"^2\. "))
    page.keyboard.press("ArrowLeft")
    expect(page.locator("#slide h1")).to_have_text(re.compile(r"^1\. "))
    page.keyboard.press("7")
    expect(page.locator("#slide h1")).to_have_text(re.compile(r"^7\. "))
    page.keyboard.press("0")
    expect(page.locator("#slide h1")).to_have_text(re.compile(rf"^{SLIDE_COUNT}\. "))
    page.get_by_role("button", name="Previous slide").click()
    expect(page.locator("#slide h1")).to_have_text(re.compile(rf"^{SLIDE_COUNT - 1}\. "))
    page.goto(page.base + "/#/6")
    page.reload()
    expect(page.locator("#slide h1")).to_have_text(re.compile(r"^6\. "))
    assert page.locator("#steps button.active").count() == 1


def test_typing_in_a_field_does_not_change_slides(page):
    go(page, 3)
    box = page.get_by_label("Informatica formula")
    box.click()
    box.press("End")
    box.type("12", delay=20)
    expect(page.locator("#slide h1")).to_have_text(re.compile(r"^3\. "))


def test_timer_runs_and_resets(page):
    page.wait_for_timeout(2200)
    assert page.locator("#timer").inner_text() != "0:00"
    page.locator("#timer").click()
    assert page.locator("#timer").inner_text() == "0:00"


# -- file viewer

def test_every_file_link_and_flow_box_opens_the_viewer(page):
    opened = 0
    for n in range(1, SLIDE_COUNT + 1):
        go(page, n)
        targets = page.locator("#slide .linkbtn, #slide .flow button.box")
        for i in range(targets.count()):
            btn = targets.nth(i)
            if "open in the file viewer" in btn.inner_text():
                continue  # the graph's own link; covered in the graph test
            btn.click()
            expect(page.locator("#drawer")).to_be_visible()
            expect(page.locator("#drawer .path")).not_to_be_empty()
            assert "not shown" not in page.locator("#drawer").inner_text()
            assert page.locator("#drawer .err").count() == 0, page.locator("#drawer").inner_text()[:200]
            opened += 1
            page.keyboard.press("Escape")
            expect(page.locator("#drawer")).to_be_hidden()
    assert opened >= 30
    assert page.seen.js_errors == []


def test_viewer_highlights_the_requested_lines_and_shows_the_whole_file_on_demand(page):
    go(page, 2)
    page.get_by_role("button", name="Open in the file viewer").first.click()
    expect(page.locator("#drawer .path")).to_contain_text("lines 106-124")
    assert page.locator("#drawer .ln.hl").count() == 19
    assert page.locator("#drawer a:has-text('Open on GitHub')").get_attribute("href").endswith("#L106-L124")
    page.get_by_role("button", name="Show the whole file").click()
    expect(page.locator("#drawer .ln").nth(4000)).to_be_attached(timeout=10_000)
    assert page.locator("#drawer .ln").count() == 4702
    assert "admin123" not in page.locator("#drawer").inner_text()


def test_markdown_section_links_highlight_the_section(page):
    go(page, 9)
    page.get_by_role("button", name="README: assumptions and what is not handled").click()
    expect(page.locator("#drawer .path")).to_contain_text("README.md")
    assert page.locator("#drawer .ln.hl").count() > 10


# -- graphs

def test_every_target_graph_draws_and_every_box_responds(page):
    go(page, 4)
    maps = page.get_by_label("Mapping", exact=True)
    expect(maps.locator("option")).to_have_count(3)
    mapping_names = maps.locator("option").all_inner_texts()
    assert len(mapping_names) == 3
    seen_targets = 0
    for m in mapping_names:
        maps.select_option(m)
        tgt = page.get_by_label("Target", exact=True)
        n_targets = tgt.locator("option").count()
        for i in range(n_targets):
            value = tgt.locator("option").nth(i).get_attribute("value")
            tgt.select_option(index=i)
            # wait for this target's graph (the previous one stays on screen until the fetch returns)
            expect(page.locator("svg.graph")).to_have_attribute("aria-label", f"Mapping graph for {value}")
            expect(page.locator("svg.graph .node").first).to_be_visible()
            nodes = page.locator("svg.graph .node")
            count = nodes.count()
            assert count >= 3
            seen_targets += 1
            for k in range(count):
                node = nodes.nth(k)
                node.click(force=True)
                info = page.locator(".card:has(svg.graph) p.note").first.inner_text()
                assert info and "Click a box" not in info
                kind = node.get_attribute("class")
                if "k-source" not in kind and "intermediate model" not in info and "read directly" not in info:
                    assert page.locator(".code .ln.hl").count() > 0, f"{m}/{i}/{k}: nothing highlighted ({info})"
    assert seen_targets == 16
    assert page.seen.js_errors == []


def test_clicking_a_selected_box_again_clears_the_highlight(page):
    go(page, 4)
    node = page.locator('svg.graph .node[data-node="EXP_ORDERS"]')
    node.click()
    assert page.locator(".code .ln.hl").count() == 15
    node.click()
    assert page.locator(".code .ln.hl").count() == 0


def test_a_shared_node_points_to_its_intermediate_model(page):
    go(page, 4)
    page.get_by_label("Mapping", exact=True).select_option("m_FL_JS_MARTS_ENRICHED")
    page.get_by_label("Target", exact=True).select_option("CUSTOMER_360")
    expect(page.locator("svg.graph .node").first).to_be_visible()
    names = page.locator("svg.graph .node").evaluate_all("els => els.map(e => e.dataset.node)")
    shared = [n for n in names if n.startswith("EXP_CUSTOMER_ORDER_NUM") or n.startswith("SQ_STG_ORDER_ITEMS")]
    assert shared
    page.locator(f'svg.graph .node[data-node="{shared[0]}"]').click(force=True)
    assert "intermediate model" in page.locator(".card:has(svg.graph) p.note").first.inner_text() or \
        page.locator(".code .ln.hl").count() > 0


# -- formula translator

def test_translator_examples_and_editing(page):
    go(page, 3)
    examples = page.locator('.card:has(input[aria-label="Informatica formula"]) button.ex')
    results = {}
    for i in range(examples.count()):
        examples.nth(i).click()
        page.wait_for_timeout(400)
        results[page.get_by_label("Informatica formula").input_value()] = (
            page.locator(".sqlout").inner_text() if page.locator(".sqlout").count() else "",
            page.locator(".err").inner_text() if page.locator(".err").count() else "")
    assert results["'a' || NULL"][0].startswith("concat(")
    assert "not supported" in results["SOUNDEX(NAME)"][1]
    assert "(not a) = b" in results["NOT A = B"][0]
    assert results["IIF(ISNULL(SUBTOTAL_IN), 0, SUBTOTAL_IN / 100)"][0] == \
        "case when (subtotal_in is null) then 0 else (subtotal_in / 100) end"
    box = page.get_by_label("Informatica formula")
    box.fill("TRUNC(ORDERED_AT)")
    expect(page.locator(".err")).to_contain_text("cannot tell date from number")
    page.get_by_label("Column types", exact=False).fill("ORDERED_AT:date")
    expect(page.locator(".sqlout")).to_have_text("date_trunc('day', ordered_at)")
    box.fill("IIF(")
    expect(page.locator(".err")).to_be_visible()
    out = page.locator(".card:has(.err)")
    assert "null" not in out.inner_text() and "[object" not in out.inner_text()
    box.fill("TO_DATE(TO_CHAR(ORDERED_AT, 'DD-MM-YY'), 'DD-MM-YY')")
    expect(page.locator(".warnline")).to_contain_text("2-digit year")


# -- SQL console and defects

@needs_db
def test_defect_queries_show_the_numbers_in_the_story(page):
    go(page, 7)
    expected = {0: "converted_subtotal", 1: "repaired_tax", 2: "converted_n", 3: "61948", 4: "converted_perishable"}
    for i, text in expected.items():
        d = page.locator(".defect").nth(i)
        d.get_by_role("button", name="Run the query").click()
        expect(d.locator("table")).to_be_visible()
        assert text in d.locator("table").inner_text().lower(), f"P{i + 1}"
    assert "61948" in page.locator(".defect").nth(3).locator("table").inner_text()
    assert "61465" in page.locator(".defect").nth(3).locator("table").inner_text()
    p5 = page.locator(".defect").nth(4).locator("tbody td").all_inner_texts()
    assert p5 == ["30", "0", "30"]


@needs_db
def test_sql_console_canned_queries_free_typing_and_refusals(page):
    go(page, 7)
    card = page.locator('.card:has(textarea[aria-label="SQL"])')
    chips = card.locator("button.ex")
    for i in range(chips.count()):
        chips.nth(i).click()
        card.get_by_role("button", name="Run query").click()
        expect(card.locator("table")).to_be_visible()
    area = card.get_by_label("SQL")
    for bad, message in [("drop table raw.raw_orders", "read-only"), ("select 1; select 2", "one statement"),
                         ("select * from read_csv('/etc/passwd')", "disabled"), ("select * from nope", "nope"),
                         ("", "empty")]:
        area.fill(bad)
        card.get_by_role("button", name="Run query").click()
        expect(card.locator(".err")).to_contain_text(message)
    area.fill("select count(*) as n from raw.raw_orders")
    card.get_by_role("button", name="Run query").click()
    expect(card.locator("table")).to_contain_text("n")
    # the data is still there after the refused statements
    assert int(card.locator("tbody td").first.inner_text()) > 1000


@needs_db
def test_query_with_many_rows_is_truncated(page):
    go(page, 7)
    card = page.locator('.card:has(textarea[aria-label="SQL"])')
    card.get_by_label("SQL").fill("select * from raw.raw_orders")
    card.get_by_role("button", name="Run query").click()
    expect(card.locator("p.note").last).to_contain_text("first 200 rows")
    assert card.locator("tbody tr").count() == 200


# -- live actions

def test_unit_tests_button(page):
    go(page, 8)
    card, badge, out = run_action(page, "tests", "Run: all unit tests")
    assert "OK:" in badge and "passed" in badge and "FAILED" not in badge
    assert "tests/test_expr.py" in out and "$ pytest -v" in out


def test_break_the_joiner_rule_is_caught_and_the_source_is_untouched(page):
    before = (ROOT / "src/pwc_to_dbt/convert.py").read_bytes()
    go(page, 8)
    card, badge, out = run_action(page, "break-joiner", "Run: break the Joiner rule")
    assert "caught the broken rule" in badge and "FAILED" not in badge
    assert "temporary COPY" in out and "importing from /tmp/" in out
    assert "test_joiner_types[Master Outer Join" in out
    assert (ROOT / "src/pwc_to_dbt/convert.py").read_bytes() == before


def test_convert_and_diff_button(page):
    go(page, 4)
    card, badge, out = run_action(page, "convert", "Run: convert the sample")
    assert "identical to the golden project" in badge
    assert "Generated 25 files" in out


def test_inspect_button(page):
    go(page, 2)
    card, badge, out = run_action(page, "inspect", "Run: inspect")
    assert "OK" in badge and "STG_ORDERS:" in out and "JNR_SUPPLIES_PRODUCTS" in out


def test_output_survives_leaving_and_returning_to_a_slide(page):
    go(page, 2)
    run_action(page, "inspect", "Run: inspect")
    go(page, 3)
    go(page, 2)
    card = page.locator('[data-action^="inspect"]')
    assert "STG_ORDERS:" in card.locator(".term").inner_text() and "OK" in card.locator(".badge").inner_text()


@needs_db
def test_end_to_end_from_the_ui_then_tables_refresh_and_buttons_lock_while_running(page):
    if not db_unlocked():
        pytest.skip("verify/work/jaffle.duckdb is locked by another process (close any duckdb shell)")
    go(page, 6)
    card = page.locator('[data-action^="e2e"]')
    card.get_by_role("button", name="Run: full end-to-end check").click()
    expect(card.locator(".badge")).to_have_text("running ...")
    go(page, 8)  # another action cannot start while this one runs
    expect(page.get_by_role("button", name="Run: all unit tests")).to_be_disabled()
    go(page, 6)
    expect(card.locator(".badge")).to_have_text(re.compile(r"OK|FAILED"), timeout=SLOW)
    assert "E2E OK" in card.locator(".badge").inner_text(), card.locator(".term").inner_text()[-800:]
    term = card.locator(".term").inner_text()
    assert "dbt.cli.main build" in term and "E2E OK" in term
    tables = page.locator(".card table.t")
    expect(tables.first.locator("tbody tr").first).to_be_visible()
    page.wait_for_function("document.querySelectorAll('.card table.t tbody tr').length > 20", timeout=30_000)
    assert page.get_by_text("Every difference is listed with its cause").count() >= 2
    go(page, 8)
    expect(page.get_by_role("button", name="Run: all unit tests")).to_be_enabled()


@needs_db
def test_locked_database_gives_a_clear_message_and_sql_console_still_reads(page):
    import duckdb

    if not db_unlocked():
        pytest.skip("database already locked by another process")
    holder = duckdb.connect(str(DB))  # a writer, like a forgotten duckdb shell
    try:
        go(page, 6)
        card = page.locator('[data-action^="e2e"]')
        card.get_by_role("button", name="Run: full end-to-end check").click()
        expect(card.locator(".badge")).to_contain_text("locked", timeout=20_000)
        assert ".quit" in card.locator(".term").inner_text()
    finally:
        holder.close()
    assert db_unlocked()


def test_status_endpoint_reports_busy_while_an_action_runs(page, url):
    import json

    go(page, 8)
    page.get_by_role("button", name="Run: all unit tests").click()
    status = json.loads(urllib.request.urlopen(url + "/api/status").read())
    assert status["busy"] in (None, "tests")
    expect(page.locator('[data-action^="tests"] .badge')).to_contain_text("OK", timeout=30_000)
    assert json.loads(urllib.request.urlopen(url + "/api/status").read())["busy"] is None


# -- appearance

@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_light_and_dark_themes(browser, url, scheme, tmp_path):
    ctx = browser.new_context(viewport={"width": 1600, "height": 1000}, color_scheme=scheme)
    pg = ctx.new_page()
    pg.goto(url + "/#/4")
    expect(pg.locator("svg.graph .node").first).to_be_visible()
    bg = pg.evaluate("getComputedStyle(document.body).backgroundColor")
    light = bg == "rgb(245, 247, 250)"
    assert light == (scheme == "light"), bg
    fill = pg.evaluate("getComputedStyle(document.querySelector('svg.graph .k-target rect')).fill")
    assert fill != "rgb(238, 238, 238)"  # the kind colour applies in both themes
    pg.screenshot(path=str(tmp_path / f"slide4-{scheme}.png"))
    ctx.close()


@pytest.mark.parametrize("width", [390, 820])
def test_no_horizontal_page_scroll_on_small_screens(browser, url, width):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    pg = ctx.new_page()
    pg.goto(url)
    problems = []
    for n in range(1, SLIDE_COUNT + 1):
        pg.locator("#steps button").nth(n - 1).click()
        pg.wait_for_timeout(500)
        overflow = pg.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        if overflow > 1:
            wide = pg.evaluate("""[...document.querySelectorAll('#slide *')]
                .filter(e => e.getBoundingClientRect().right > window.innerWidth + 1
                    && !e.closest('.scroll, .code, .graphbox, .term, #steps'))
                .slice(0, 3).map(e => e.tagName + '.' + e.className)""")
            problems.append(f"slide {n}: {overflow}px wider than the screen ({wide})")
    ctx.close()
    assert problems == []


def test_controls_have_accessible_names(page):
    for n in (1, 4, 7, 8):
        go(page, n)
        nameless = page.evaluate("""[...document.querySelectorAll('button, a, input, textarea, select')]
            .filter(e => !(e.textContent.trim() || e.getAttribute('aria-label') || e.getAttribute('title')
                || e.getAttribute('value')))
            .map(e => e.outerHTML.slice(0, 80))""")
        assert nameless == [], f"slide {n}: {nameless}"
    assert page.locator("main#slide").count() == 1 and page.locator("nav#steps[aria-label]").count() == 1


def test_all_slide_buttons_fit_in_the_top_bar_on_a_laptop_screen(browser, url):
    ctx = browser.new_context(viewport={"width": 1366, "height": 768})
    pg = ctx.new_page()
    pg.goto(url)
    expect(pg.locator("#steps button")).to_have_count(SLIDE_COUNT)
    sizes = pg.evaluate("[document.querySelector('#steps').scrollWidth, document.querySelector('#steps').clientWidth]")
    ctx.close()
    assert sizes[0] <= sizes[1], f"slide buttons are clipped: {sizes}"
