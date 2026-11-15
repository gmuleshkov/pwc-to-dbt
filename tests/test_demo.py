"""The demo server (demo/): guards, endpoints, graphs, and that every slide link still resolves."""

import http.client
import json
import re
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "demo"))
sys.path.insert(0, str(ROOT / "verify"))

import server  # noqa: E402
import slides  # noqa: E402

needs_db = pytest.mark.skipif(not server.DB.exists(), reason="run `make e2e` once to build verify/work/jaffle.duckdb")


@pytest.fixture(scope="module")
def base():
    httpd = server.make_server(0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


def call(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    hdrs = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body)
        hdrs.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=hdrs)
    r = conn.getresponse()
    raw = r.read()
    conn.close()
    try:
        return r.status, json.loads(raw)
    except ValueError:
        return r.status, raw.decode()


# -- guards

def test_index_and_static_are_served(base):
    status, html = call(base, "GET", "/")
    assert status == 200 and "pwc-to-dbt" in html
    assert call(base, "GET", "/static/app.js")[0] == 200
    assert call(base, "GET", "/static/../server.py")[0] == 404


def test_wrong_host_and_origin_are_rejected(base):
    assert call(base, "GET", "/api/status", headers={"Host": "evil.example"})[0] == 403
    status, _ = call(base, "POST", "/api/sql", {"sql": "select 1"}, {"Origin": "http://evil.example"})
    assert status == 403


def test_post_must_be_json(base):
    conn = http.client.HTTPConnection("127.0.0.1", base)
    conn.request("POST", "/api/sql", body="sql=select 1", headers={"Content-Type": "text/plain"})
    assert conn.getresponse().status == 415


@pytest.mark.parametrize("path", [
    "../../etc/passwd", "/etc/passwd", ".git/config", "verify/work/jaffle.duckdb", "verify/reference/dbt_project.yml",
    ".venv/pyvenv.cfg", "verify/.user.yml",
])
def test_file_viewer_refuses_private_or_outside_paths(base, path):
    assert call(base, "GET", "/api/file?path=" + path)[0] in (403, 404)


def test_file_viewer_redacts_the_password_in_the_sample(base):
    status, data = call(base, "GET", "/api/file?path=examples/FLOWLINE_DEMO_JAFFLESHOP.xml")
    assert status == 200 and "admin123" not in data["text"] and "<redacted>" in data["text"]


def test_file_viewer_resolves_markdown_sections(base):
    section = quote("Assumptions and what is not handled")
    status, data = call(base, "GET", f"/api/file?path=README.md&section={section}")
    assert status == 200 and data["range"][0] < data["range"][1]


@pytest.mark.parametrize("sql", ["drop table x", "insert into x values (1)", "attach 'x.db'", "copy x to 'y'",
                                 "select 1; select 2", "", "pragma version"])
def test_sql_console_accepts_only_one_read_statement(sql):
    with pytest.raises(ValueError):
        server.check_sql(sql)


@needs_db
def test_sql_console_is_read_only_and_cannot_read_files(base):
    status, data = call(base, "POST", "/api/sql", {"sql": "select count(*) as n from reference.stg_orders"})
    assert status == 200 and data["rows"][0][0] > 0
    status, data = call(base, "POST", "/api/sql", {"sql": "select * from read_csv('/etc/passwd')"})
    assert status == 400 and "disabled" in data["error"]


def test_unknown_action_is_refused(base):
    assert call(base, "POST", "/api/run/rm-rf", {})[0] == 404


def test_only_one_action_runs_at_a_time():
    release = threading.Event()
    server.ACTIONS["_hold"] = lambda job, params: (release.wait(10), (True, "held"))[1]
    try:
        first = server.start_job("_hold", {})
        with pytest.raises(server.Busy):
            server.start_job("_hold", {})
        release.set()
        for _ in range(100):
            if first.done:
                break
            threading.Event().wait(0.05)
        assert first.done and first.ok
    finally:
        server.ACTIONS.pop("_hold", None)
        release.set()


# -- features

def test_translate_shows_tokens_tree_and_sql(base):
    status, r = call(base, "POST", "/api/translate", {"formula": "IIF(ISNULL(X), 0, X / 100)", "columns": "X:double"})
    assert status == 200 and r["sql"] == "case when (x is null) then 0 else (x / 100) end"
    assert r["tokens"][0]["text"] == "IIF" and r["tree"]["label"] == "Call IIF"
    status, r = call(base, "POST", "/api/translate", {"formula": "SOUNDEX(NAME)", "columns": ""})
    assert status == 200 and r["sql"] is None and "not supported" in r["error"]


def test_every_target_has_a_graph_whose_boxes_point_at_ctes():
    for entry in server.graph_index():
        for t in entry["targets"]:
            g = server.graph_for(entry["mapping"], t["instance"])
            assert g["svg"].startswith("<svg") and g["nodes"]
            for node in g["nodes"]:
                assert f'data-node="{node["name"]}"' in g["svg"]
                if node["kind"] != "source":
                    assert node["cte"] or node["elsewhere"], f"{t['instance']}: no CTE found for {node['name']}"
                for cte in node["cte"]:
                    assert cte in g["ranges"]


def test_break_joiner_mutation_still_matches_the_source():
    assert server.GOOD_JOIN in (ROOT / "src" / "pwc_to_dbt" / "convert.py").read_text()


# -- the slides

BLOCK_TYPES = set(re.findall(r'blocks(?:\.([\w]+)|\["([\w-]+)"\])\s*=', (ROOT / "demo/static/app.js").read_text()))
BLOCK_TYPES = {a or b for a, b in BLOCK_TYPES}


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def test_slides_are_well_formed():
    assert len(slides.SLIDES) == 10 and len({s["id"] for s in slides.SLIDES}) == 10
    for s in slides.SLIDES:
        assert s["points"] and s["mention"] and s["blocks"], s["id"]
        assert s["short"] and len(s["short"]) <= 12, s["id"]
        for b in s["blocks"]:
            assert b["type"] in BLOCK_TYPES, f"{s['id']}: no renderer for block type {b['type']}"
            if b["type"] == "action":
                assert b["action"] in server.ACTIONS


def test_every_file_link_in_the_slides_resolves():
    for node in walk(slides.SLIDES):
        path = node.get("path")
        if not isinstance(path, str):
            continue
        data = server.read_file(path, node.get("section", ""))
        lines = node.get("lines")
        if isinstance(lines, list):
            assert 1 <= lines[0] <= lines[1] <= data["lines"], f"{path}: lines {lines} out of range"


@needs_db
def test_every_canned_query_in_the_slides_runs():
    queries = [n["query"] for n in walk(slides.SLIDES) if "query" in n]
    for n in walk(slides.SLIDES):
        if isinstance(n.get("queries"), list):
            queries += [q["sql"] for q in n["queries"]]
    assert len(queries) >= 10
    for sql in queries:
        assert server.run_sql(sql)["columns"], sql


# -- private presenter notes (demo/local_notes.py is not in git)

def test_public_slides_work_without_local_notes(monkeypatch):
    monkeypatch.setitem(sys.modules, "local_notes", None)  # as if the file does not exist
    merged = server.load_slides()
    assert merged == slides.SLIDES
    assert not [b for s in merged for b in s["blocks"] if b["type"] == "qa"]


def test_local_notes_are_merged_into_the_slides_without_touching_the_public_ones(monkeypatch):
    import types

    notes = types.ModuleType("local_notes")
    notes.BLOCKS = {"decisions": [{"type": "qa", "title": "Notes", "items": [{"q": "q?", "a": "a"}]}]}
    notes.MENTION = {"defects": ["extra line"]}
    monkeypatch.setitem(sys.modules, "local_notes", notes)
    merged = {s["id"]: s for s in server.load_slides()}
    assert merged["decisions"]["blocks"][-1]["title"] == "Notes"
    assert merged["defects"]["mention"][-1] == "extra line"
    original = {s["id"]: s for s in slides.SLIDES}
    assert original["decisions"]["blocks"][-1]["type"] != "qa" and "extra line" not in original["defects"]["mention"]


def test_local_notes_file_is_ignored_by_git():
    import subprocess

    out = subprocess.run(["git", "check-ignore", "demo/local_notes.py"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, "demo/local_notes.py must stay out of the repository"
