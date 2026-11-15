"""Local demo server: an interactive walk-through of pwc-to-dbt (standard library only).

    make demo              # then open http://127.0.0.1:8765

It serves one page (demo/static) and a small JSON API. It can only do what is listed here: view repository
files, draw mapping graphs, run a fixed set of commands (tests, convert, e2e, ...), translate one formula and
run one read-only SQL statement on the demo database. It listens on 127.0.0.1 only.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import mimetypes
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEMO = Path(__file__).resolve().parent
ROOT = DEMO.parent
sys.path.insert(0, str(DEMO))
sys.path.insert(0, str(ROOT / "verify"))

import compare  # noqa: E402
import graph  # noqa: E402
import invariants  # noqa: E402
import slides  # noqa: E402

from pwc_to_dbt import expr, model  # noqa: E402
from pwc_to_dbt.convert import convert, redact  # noqa: E402

SAMPLE = ROOT / "examples" / "FLOWLINE_DEMO_JAFFLESHOP.xml"
GOLDEN = ROOT / "examples" / "jaffle_dbt"
DB = ROOT / "verify" / "work" / "jaffle.duckdb"
REPO_URL = "https://github.com/gmuleshkov/pwc-to-dbt"
STATIC = DEMO / "static"

MAX_FILE_BYTES = 2_000_000
MAX_ROWS = 200

# -- what the file viewer may show --------------------------------------------------------------------------------
DENY_DIRS = {".git", ".venv", "__pycache__", ".idea", ".pytest_cache", ".ruff_cache", "node_modules", "logs"}
DENY_PREFIXES = [("verify", "work"), ("verify", "reference")]
DENY_NAMES = {".user.yml", "profiles.yml.bak"}
DENY_SUFFIXES = {".duckdb", ".wal", ".pem", ".key", ".pyc"}


def safe_path(rel: str) -> Path:
    """Resolve a repo-relative path, or raise PermissionError/FileNotFoundError."""
    p = (ROOT / rel).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise PermissionError("outside the repository")
    parts = p.relative_to(ROOT).parts
    if (any(x in DENY_DIRS for x in parts) or any(parts[: len(d)] == d for d in DENY_PREFIXES)
            or p.name in DENY_NAMES or p.name.startswith(".env") or p.suffix in DENY_SUFFIXES):
        raise PermissionError("this file is not shown in the demo")
    if not p.is_file():
        raise FileNotFoundError(rel)
    if p.stat().st_size > MAX_FILE_BYTES:
        raise PermissionError("file too large for the viewer")
    return p


def section_range(text: str, heading: str) -> list[int]:
    """Lines (1-based) of a markdown `## heading` section, up to the next `## `."""
    lines = text.splitlines()
    heads = [i for i, line in enumerate(lines) if line.startswith("#") and line.strip("# ").strip() == heading]
    start = heads[0] if heads else None
    if start is None:
        raise KeyError(f"no section '{heading}'")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    return [start + 1, end]


def read_file(rel: str, section: str = "") -> dict:
    p = safe_path(rel)
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")  # the PowerCenter export is Windows-1252
    rel_posix = p.relative_to(ROOT).as_posix()
    out = {
        "path": rel_posix,
        "text": redact(text),  # same redaction as conversion_report.md: no password on screen
        "lines": text.count("\n") + 1,
        "github": f"{REPO_URL}/blob/main/{rel_posix}",
        "range": None,
    }
    if section:
        out["range"] = section_range(text, section)
    return out


# -- the sample, converted once ------------------------------------------------------------------------------------
@lru_cache(maxsize=1)
def folder():
    return model.load(SAMPLE)[0]


@lru_cache(maxsize=1)
def project():
    return convert(folder())


def graph_index() -> list[dict]:
    out = []
    for name in project().mapping_names:
        targets = []
        for m in project().models:
            if m.mapping == name and m.kind == "target":
                targets.append({"instance": m.instance, "model": m.name,
                                "path": f"examples/jaffle_dbt/models/{m.folder}/{m.name}.sql"})
        out.append({"mapping": name, "targets": targets})
    return out


def graph_for(mapping: str, target: str) -> dict:
    entry = next((t for m in graph_index() if m["mapping"] == mapping for t in m["targets"]
                  if t["instance"] == target), None)
    if entry is None:
        raise KeyError(f"unknown mapping/target {mapping}/{target}")
    conv = graph.converter(folder(), mapping)
    g = graph.target_graph(conv, target)
    sql = safe_path(entry["path"]).read_text()
    ranges = graph.cte_ranges(sql)
    elsewhere = {}  # nodes whose CTE lives in an intermediate model instead of this one
    for m in project().models:
        if m.mapping == mapping and m.kind == "intermediate":
            for inst, _ in m.transformations:
                path = f"examples/jaffle_dbt/models/{m.folder}/{m.name}.sql"
                elsewhere.setdefault(inst, {"model": m.name, "path": path})
    for node in g["nodes"]:
        node["cte"] = graph.cte_names_for(node["name"], ranges, conv.inst(node["name"]).type == "TARGET")
        node["elsewhere"] = elsewhere.get(node["name"]) if not node["cte"] and node["kind"] != "source" else None
    return {**entry, **g, "ranges": ranges}


# -- formula translator (the expression pipeline, one step at a time) ----------------------------------------------
def to_tree(node) -> dict:
    if isinstance(node, expr.Num):
        return {"label": f"Number {node.text}"}
    if isinstance(node, expr.Str):
        return {"label": f"Text '{node.value}'"}
    if isinstance(node, expr.Const):
        return {"label": f"Constant {node.name}"}
    if isinstance(node, expr.Name):
        return {"label": f"Column {node.name}"}
    if isinstance(node, expr.Call):
        label = f"Call {node.name}" + ("(*)" if node.star else "")
        return {"label": label, "children": [to_tree(a) for a in node.args]}
    if isinstance(node, expr.Unary):
        return {"label": f"Unary {node.op}", "children": [to_tree(node.operand)]}
    if isinstance(node, expr.Binary):
        return {"label": f"Operator {node.op}", "children": [to_tree(node.left), to_tree(node.right)]}
    return {"label": type(node).__name__}


TYPES = {"string", "integer", "bigint", "decimal", "double", "date", "boolean"}


def translate(formula: str, columns: str = "") -> dict:
    types = {}
    for part in columns.split(","):
        if ":" in part:
            name, _, t = part.partition(":")
            if t.strip().lower() in TYPES:
                types[name.strip().upper()] = t.strip().lower()
    warnings: list[str] = []

    def resolve(name: str):
        if name.startswith("$$"):
            return expr.Typed("{{ var('" + name[2:].lower() + "') }}", "string")
        return expr.Typed(name.lower(), types.get(name.upper(), "unknown"))

    out: dict = {"tokens": [], "tree": None, "sql": None, "type": None, "warnings": warnings, "error": None}
    try:
        out["tokens"] = [dataclasses.asdict(t) for t in expr.tokenize(formula) if t.kind != "end"]
        out["tree"] = to_tree(expr.parse(formula))
        typed = expr.to_sql(formula, resolve, warnings.append)
        out["sql"], out["type"] = typed.sql, typed.type
    except expr.Unsupported as e:
        out["error"] = f"Unsupported: {e}"
    except RecursionError:
        out["error"] = "Expression is nested too deeply"
    return out


# -- database access (read-only, one connection per request) -------------------------------------------------------
class Busy(Exception):
    """Another action is running."""


STATE = {"job": None, "db_busy": False}
STATE_LOCK = threading.Lock()
DB_LOCK = threading.Lock()  # readers (SQL console, comparison tables) and the e2e start take turns on the database

SQL_FIRST_WORDS = {"select", "with", "describe", "show", "summarize", "from"}


def check_sql(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    if not s:
        raise ValueError("empty query")
    if ";" in s:
        raise ValueError("one statement at a time")
    if s.split(None, 1)[0].lower() not in SQL_FIRST_WORDS:
        raise ValueError("read-only console: start with SELECT, WITH, DESCRIBE, SHOW or SUMMARIZE")
    return s


def jsonable(v):
    if v is None or isinstance(v, bool | int | float | str):
        return v
    return str(v)


def run_sql(sql: str) -> dict:
    import duckdb

    s = check_sql(sql)
    if not DB.exists():
        raise FileNotFoundError("verify/work/jaffle.duckdb does not exist yet: run the e2e step first")
    started = time.time()
    with DB_LOCK:
        if STATE["db_busy"]:
            raise Busy("the database is being rebuilt by an e2e run; try again when it finishes")
        # enable_external_access=False blocks read_csv('/etc/passwd') and friends; read_only blocks all writes.
        con = duckdb.connect(str(DB), read_only=True, config={"enable_external_access": False})
        timer = threading.Timer(15, con.interrupt)
        timer.start()
        try:
            cur = con.execute(s)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_ROWS + 1)
        except duckdb.InterruptException as e:
            raise ValueError("query took longer than 15 s and was stopped") from e
        except duckdb.Error as e:
            raise ValueError(str(e)) from e
        finally:
            timer.cancel()
            con.close()
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows[:MAX_ROWS]],
            "truncated": len(rows) > MAX_ROWS, "ms": round((time.time() - started) * 1000)}


def db_lock_free() -> bool:
    """True if nothing else (for example a duckdb shell) holds the database file."""
    import duckdb

    if not DB.exists():
        return True
    try:
        duckdb.connect(str(DB)).close()
        return True
    except duckdb.IOException:
        return False


def compare_summary(which: str) -> dict:
    schema, key = {"original": ("converted", "expected"), "repaired": ("repaired", "expected_repaired")}[which]
    if not DB.exists():
        raise FileNotFoundError("verify/work/jaffle.duckdb does not exist yet: run the e2e step first")
    with DB_LOCK:
        if STATE["db_busy"]:
            raise Busy("the database is being rebuilt by an e2e run; try again when it finishes")
        results = compare.run(DB, schema=schema, expected_key=key)
        inv = invariants.run(DB, schema) if which == "repaired" else None
    models = []
    for r in results:
        differing = [c for c in r.columns if c.status != "match"]
        models.append({
            "model": r.model, "reference": r.reference, "rows_ours": r.rows_ours, "rows_ref": r.rows_ref,
            "rows_status": r.rows_status, "rows_reason": r.rows_reason,
            "columns": len(r.columns), "matched": sum(c.status == "match" for c in r.columns),
            "differing": [{"column": c.ours, "mismatches": c.mismatches, "status": c.status,
                           "reason": c.expected_reason, "example": c.example} for c in differing],
        })
    return {"run": which, "schema": schema, "models": models, "failures": compare.failures(results),
            "invariants": inv}


# -- actions: a fixed list of commands, one at a time, output polled by the page ---------------------------------
class Job:
    def __init__(self, name: str):
        self.id = uuid.uuid4().hex[:10]
        self.name = name
        self.lines: list[str] = []
        self.done = False
        self.ok: bool | None = None
        self.summary = ""
        self.started = time.time()
        self.lock = threading.Lock()

    def add(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)

    def snapshot(self, start: int) -> dict:
        with self.lock:
            return {"id": self.id, "name": self.name, "lines": self.lines[start:], "next": len(self.lines),
                    "done": self.done, "ok": self.ok, "summary": self.summary,
                    "seconds": round(time.time() - self.started, 1)}

    def run_cmd(self, cmd: list[str], cwd: Path = ROOT, env: dict | None = None) -> int:
        import os

        e = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_COLOR": "1", **(env or {})}
        proc = subprocess.Popen(cmd, cwd=cwd, env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                errors="replace", bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            self.add(line.rstrip("\n"))
        return proc.wait()


PYTEST = [sys.executable, "-m", "pytest", "--color=no", "-p", "no:cacheprovider"]


def last_match(lines: list[str], words: tuple[str, ...]) -> str:
    for line in reversed(lines):
        if any(w in line for w in words):
            return line.strip("= ").strip()
    return ""


def act_tests(job: Job, params: dict) -> tuple[bool, str]:
    code = job.run_cmd([*PYTEST, "-v"])
    return code == 0, last_match(job.lines, ("passed", "failed", "error")) or f"exit code {code}"


def act_inspect(job: Job, params: dict) -> tuple[bool, str]:
    mapping = params.get("mapping") or "m_FL_JS_STAGING_FULL"
    if mapping not in folder().mappings:
        return False, f"unknown mapping {mapping}"
    code = job.run_cmd([sys.executable, "-m", "pwc_to_dbt.cli", "inspect", str(SAMPLE), "-m", mapping])
    return code == 0, f"inspected {mapping}"


def act_convert(job: Job, params: dict) -> tuple[bool, str]:
    out = Path(tempfile.mkdtemp(prefix="pwc-demo-convert-"))
    try:
        job.add("$ pwc-to-dbt convert examples/FLOWLINE_DEMO_JAFFLESHOP.xml -o <temporary folder>")
        code = job.run_cmd([sys.executable, "-m", "pwc_to_dbt.cli", "convert", str(SAMPLE), "-o", str(out)])
        if code != 0:
            return False, f"convert failed (exit {code})"
        produced = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
        golden = {p.relative_to(GOLDEN): p.read_bytes() for p in GOLDEN.rglob("*") if p.is_file()}
        job.add("")
        job.add(f"Generated {len(produced)} files; the committed golden project has {len(golden)}.")
        diff = [str(k) for k in sorted(set(produced) | set(golden)) if produced.get(k) != golden.get(k)]
        for k in diff:
            job.add(f"  DIFFERS: {k}")
        if not diff:
            job.add("Every file is byte-for-byte identical to examples/jaffle_dbt (the output is deterministic).")
        return not diff, "identical to the golden project" if not diff else f"{len(diff)} file(s) differ"
    finally:
        shutil.rmtree(out, ignore_errors=True)


def act_e2e(job: Job, params: dict) -> tuple[bool, str]:
    with DB_LOCK:  # waits for a running query or table load, then keeps new ones out
        if not db_lock_free():
            job.add("The demo database is locked by another process, probably a `duckdb` shell.")
            job.add("Type .quit in that shell (or close it) and run this step again.")
            return False, "database locked by another process"
        STATE["db_busy"] = True
    try:
        job.add("$ make e2e   (convert -> dbt build on DuckDB -> compare with dbt-labs/jaffle-shop)")
        code = job.run_cmd([*PYTEST, "-m", "e2e", "-s", "-q"])
        ok_line = last_match(job.lines, ("E2E OK", "E2E FAILED"))
        return code == 0, ok_line or last_match(job.lines, ("passed", "failed")) or f"exit code {code}"
    finally:
        STATE["db_busy"] = False


GOOD_JOIN = '"Master Outer Join": ("detail", "left join", "master"),'
BAD_JOIN = '"Master Outer Join": ("master", "left join", "detail"),'


def act_break_joiner(job: Job, params: dict) -> tuple[bool, str]:
    """Flip the Master Outer join direction in a temporary copy and run the Joiner tests: they must fail."""
    tmp = Path(tempfile.mkdtemp(prefix="pwc-demo-break-"))
    try:
        ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
        shutil.copytree(ROOT / "src", tmp / "src", ignore=ignore)
        shutil.copytree(ROOT / "tests", tmp / "tests", ignore=ignore)
        shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
        f = tmp / "src" / "pwc_to_dbt" / "convert.py"
        text = f.read_text()
        if GOOD_JOIN not in text:
            return False, "convert.py changed; update the mutation in demo/server.py"
        line_no = text[: text.index(GOOD_JOIN)].count("\n") + 1
        f.write_text(text.replace(GOOD_JOIN, BAD_JOIN))
        job.add("Working on a temporary COPY of the source; your files are not touched.")
        job.add(f"convert.py line {line_no}, before: {GOOD_JOIN}")
        job.add(f"convert.py line {line_no}, after:  {BAD_JOIN}")
        job.add("")
        env = {"PYTHONPATH": str(tmp / "src")}
        job.run_cmd([sys.executable, "-c", "import pwc_to_dbt; print('importing from', pwc_to_dbt.__file__)"],
                    cwd=tmp, env=env)
        job.add("")
        job.add("$ pytest tests/test_transformations.py -k joiner -v")
        code = job.run_cmd([*PYTEST, "-v", "tests/test_transformations.py", "-k", "joiner"], cwd=tmp, env=env)
        summary = last_match(job.lines, ("passed", "failed", "error"))
        if code != 0:
            return True, "the tests caught the broken rule: " + summary
        return False, "the tests did NOT fail, so the rule is not protected: " + summary
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ACTIONS = {"tests": act_tests, "inspect": act_inspect, "convert": act_convert, "e2e": act_e2e,
           "break-joiner": act_break_joiner}
JOBS: dict[str, Job] = {}


def start_job(name: str, params: dict) -> Job:
    if name not in ACTIONS:
        raise KeyError(name)
    with STATE_LOCK:
        current = STATE["job"]
        if current is not None and not current.done:
            raise Busy(f"'{current.name}' is still running")
        job = Job(name)
        STATE["job"] = job
        JOBS[job.id] = job

    def work():
        try:
            job.ok, job.summary = ACTIONS[name](job, params)
        except Exception as e:  # noqa: BLE001 - shown to the presenter, not swallowed
            job.add(f"internal error: {type(e).__name__}: {e}")
            job.ok, job.summary = False, "internal error"
        job.done = True

    threading.Thread(target=work, daemon=True).start()
    return job


# -- HTTP ----------------------------------------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "pwc-demo"

    def log_message(self, fmt, *args):  # quiet: the page polls every few hundred ms
        return

    # -- plumbing
    def send_bytes(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code: int = 200) -> None:
        self.send_bytes(code, json.dumps(data, default=str).encode(), "application/json; charset=utf-8")

    def error(self, code: int, message: str) -> None:
        self.send_json({"error": message}, code)

    def host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        port = self.server.server_address[1]
        return origin is None or origin in (f"http://127.0.0.1:{port}", f"http://localhost:{port}")

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 100_000:
            raise ValueError("request too large")
        if not length:
            return {}
        data = json.loads(self.rfile.read(length))
        return data if isinstance(data, dict) else {}

    # -- routes
    def do_GET(self):  # noqa: N802
        if not self.host_ok():
            return self.error(403, "bad Host header")
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path in ("/", "/index.html"):
                return self.static("index.html")
            if url.path.startswith("/static/"):
                return self.static(url.path[len("/static/"):])
            if url.path == "/api/slides":
                return self.send_json(slides.SLIDES)
            if url.path == "/api/status":
                job = STATE["job"]
                return self.send_json({"busy": job.name if job and not job.done else None, "db": DB.exists(),
                                       "repo": REPO_URL})
            if url.path == "/api/file":
                return self.send_json(read_file(q.get("path", ""), q.get("section", "")))
            if url.path == "/api/graph-index":
                return self.send_json(graph_index())
            if url.path == "/api/graph":
                return self.send_json(graph_for(q.get("mapping", ""), q.get("target", "")))
            if url.path == "/api/job":
                job = JOBS.get(q.get("id", ""))
                if job is None:
                    return self.error(404, "unknown job")
                return self.send_json(job.snapshot(int(q.get("from", 0))))
            if url.path == "/api/compare":
                if q.get("run") not in ("original", "repaired"):
                    return self.error(400, "run must be original or repaired")
                return self.send_json(compare_summary(q["run"]))
        except PermissionError as e:
            return self.error(403, str(e))
        except (FileNotFoundError, KeyError) as e:
            return self.error(404, str(e).strip("'"))
        except Busy as e:
            return self.error(409, str(e))
        except Exception as e:  # noqa: BLE001
            return self.error(500, f"{type(e).__name__}: {e}")
        self.error(404, "not found")

    def do_POST(self):  # noqa: N802
        if not self.host_ok() or not self.origin_ok():
            return self.error(403, "bad Host or Origin header")
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.error(415, "send application/json")
        url = urlparse(self.path)
        try:
            body = self.body_json()
            if url.path.startswith("/api/run/"):
                job = start_job(url.path[len("/api/run/"):], body)
                return self.send_json({"id": job.id})
            if url.path == "/api/sql":
                return self.send_json(run_sql(str(body.get("sql", ""))))
            if url.path == "/api/translate":
                formula, columns = str(body.get("formula", ""))[:2000], str(body.get("columns", ""))[:500]
                return self.send_json(translate(formula, columns))
        except Busy as e:
            return self.error(409, str(e))
        except KeyError:
            return self.error(404, "unknown action")
        except (ValueError, FileNotFoundError) as e:
            return self.error(400, str(e))
        except Exception as e:  # noqa: BLE001
            return self.error(500, f"{type(e).__name__}: {e}")
        self.error(404, "not found")

    def static(self, name: str) -> None:
        p = (STATIC / name).resolve()
        if STATIC.resolve() not in p.parents or not p.is_file():
            return self.error(404, "not found")
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self.send_bytes(200, p.read_bytes(), ctype)


def make_server(port: int = 8765) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


def main() -> int:
    ap = argparse.ArgumentParser(description="Interactive demo of pwc-to-dbt")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    try:
        httpd = make_server(args.port)
    except OSError as e:
        print(f"cannot listen on 127.0.0.1:{args.port}: {e}. Use --port to pick another.", file=sys.stderr)
        return 1
    url = f"http://127.0.0.1:{args.port}"
    print(f"pwc-to-dbt demo on {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
