"use strict";

// ---------------------------------------------------------------- helpers
const $ = (sel, root = document) => root.querySelector(sel);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
// Slide text is authored by us; `code`, **bold** and *italic* only, after escaping.
const md = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
  .replace(/\*([^*]+)\*/g, "<em>$1</em>");
const mdEl = (tag, s, cls) => h(tag, { class: cls, html: md(s) });

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = null;
  try { data = await r.json(); } catch { /* not json */ }
  if (!r.ok) throw new Error((data && data.error) || `HTTP ${r.status}`);
  return data;
}

// ---------------------------------------------------------------- code view (viewer, file blocks, SQL panes)
function codeView(lines, { from = 1, to = lines.length, hl = [], numbers = true } = {}) {
  const isHl = (n) => hl.some(([a, b]) => n >= a && n <= b);
  const box = h("div", { class: "code", role: "region", "aria-label": "code" });
  for (let n = from; n <= to; n++) {
    box.append(h("div", { class: "ln" + (isHl(n) ? " hl" : ""), "data-n": n },
      numbers ? h("span", { class: "no" }, n) : null, h("span", { class: "tx" }, lines[n - 1] ?? "")));
  }
  return box;
}

function scrollToFirstHighlight(box) {
  const first = box.querySelector(".ln.hl");
  if (first) box.scrollTop = Math.max(0, first.offsetTop - box.clientHeight / 3 - box.offsetTop);
}

// ---------------------------------------------------------------- file drawer
const drawer = $("#drawer");

function closeDrawer() { drawer.hidden = true; drawer.replaceChildren(); }

async function openFile(path, { lines = null, section = "" } = {}) {
  const q = new URLSearchParams({ path });
  if (section) q.set("section", section);
  drawer.hidden = false;
  drawer.replaceChildren(h("div", { class: "body" }, "Loading " + path + " ..."));
  let data;
  try { data = await api("/api/file?" + q); } catch (e) {
    drawer.replaceChildren(h("header", {}, h("span", { class: "path" }, path), h("button", { class: "ghost", onclick: closeDrawer }, "Close")),
      h("div", { class: "body err" }, e.message));
    return;
  }
  const all = data.text.split("\n");
  const range = lines || data.range;
  const body = h("div", { class: "body" });
  const header = h("header", {}, h("span", { class: "path" }, data.path + (range ? `  (lines ${range[0]}-${range[1]})` : "")),
    h("a", { href: data.github + (range ? `#L${range[0]}-L${range[1]}` : ""), target: "_blank", rel: "noopener", class: "chip" }, "Open on GitHub"),
    h("button", { class: "ghost", onclick: closeDrawer, "aria-label": "Close viewer" }, "Close"));
  drawer.replaceChildren(header, body);

  const render = (from, to) => {
    body.replaceChildren();
    const view = codeView(all, { from, to, hl: range ? [range] : [] });
    if (from > 1) body.append(h("button", { class: "linkbtn", onclick: () => render(Math.max(1, from - 200), to) }, `Show earlier lines (${from - 1} above)`));
    body.append(view);
    if (to < all.length) body.append(h("button", { class: "linkbtn", onclick: () => render(from, Math.min(all.length, to + 200)) }, `Show more lines (${all.length - to} below)`));
    body.append(h("p", { class: "note" }, `${all.length} lines. `,
      from > 1 || to < all.length ? h("button", { class: "linkbtn", onclick: () => render(1, all.length) }, "Show the whole file") : ""));
    requestAnimationFrame(() => {
      const first = view.querySelector(".ln.hl");
      if (first) body.scrollTop = Math.max(0, first.offsetTop - 120);
    });
  };
  if (range) render(Math.max(1, range[0] - 25), Math.min(all.length, range[1] + 25));
  else render(1, Math.min(all.length, 250));
}

function fileLink(item) {
  const where = item.lines ? (item.lines[0] === item.lines[1] ? `:${item.lines[0]}` : `:${item.lines[0]}-${item.lines[1]}`) : "";
  return h("button", { class: "linkbtn", onclick: () => openFile(item.path, { lines: item.lines, section: item.section }) },
    item.label || item.path, h("span", { class: "where" }, item.path + where));
}

// ---------------------------------------------------------------- blocks
const jobCache = {};   // action key -> {lines, badge, running, paint}
const blocks = {};

blocks.compare = (b) => h("div", { class: "card two" },
  ...[b.left, b.right].map((side) => h("div", {}, h("h3", {}, side.title), h("ul", {}, side.items.map((i) => mdEl("li", i))))));

blocks.flow = (b) => h("div", { class: "card" }, h("div", { class: "flow" },
  b.steps.flatMap((s, i) => [
    i ? h("span", { class: "arrow", "aria-hidden": "true" }, "→") : null,
    h("button", { class: "box", onclick: () => openFile(s.path) }, h("strong", {}, s.label), h("span", { class: "sub" }, s.sub)),
  ])));

blocks.files = (b) => h("div", { class: "card" }, b.title ? h("h3", {}, b.title) : null, h("div", { class: "links" }, b.items.map(fileLink)));

blocks.table = (b) => h("div", { class: "card scroll" }, h("table", { class: "t" },
  h("thead", {}, h("tr", {}, b.head.map((x) => h("th", {}, x)))),
  h("tbody", {}, b.rows.map((r) => h("tr", {}, r.map((c) => h("td", { html: md(c) })))))));

blocks.cards = (b) => h("div", { class: "card" }, h("h3", {}, b.title), h("div", { class: "cards" },
  b.items.map((i) => h("div", { class: "item" }, h("b", {}, i.title), mdEl("span", i.text)))));

blocks.qa = (b) => h("div", { class: "card" }, h("h3", {}, b.title),
  b.items.map((i) => h("details", { class: "qa" }, h("summary", {}, i.q), mdEl("p", i.a))));

blocks.file = (b) => {
  const box = h("div", { class: "card" }, h("h3", {}, b.title), h("div", {}, "Loading ..."));
  api("/api/file?" + new URLSearchParams({ path: b.path })).then((data) => {
    const all = data.text.split("\n");
    const [a, z] = b.lines;
    const view = codeView(all, { from: Math.max(1, a - 2), to: Math.min(all.length, z + 2), hl: [[a, z]] });
    box.lastChild.replaceWith(view);
    if (b.note) box.append(mdEl("p", b.note, "note"));
    box.append(h("div", { class: "row" }, fileLink({ label: "Open in the file viewer", path: b.path, lines: b.lines })));
  }).catch((e) => box.lastChild.replaceWith(h("div", { class: "err" }, e.message)));
  return box;
};

// -- actions (run a fixed command, stream its output)
function termLine(text) {
  let cls = "l";
  if (/^\$ /.test(text)) cls += " cmd";
  else if (/PASSED|E2E OK|identical|caught/.test(text)) cls += " good";
  else if (/FAILED|ERROR|DIFFERS|UNEXPLAINED|locked/.test(text)) cls += " badl";
  return h("div", { class: cls }, text || " ");
}

blocks.action = (b, slide) => {
  const key = b.action + JSON.stringify(b.params || {});
  const cache = jobCache[key] || (jobCache[key] = { lines: [], badge: null, running: false, paint: null });
  const term = h("pre", { class: "term", "aria-live": "polite" });
  const badge = h("span", { class: "badge neutral" }, "not run yet");
  const btn = h("button", { class: "btn" }, b.label);
  const paint = (snap) => {
    if (snap) {
      for (const l of snap.lines) term.append(termLine(l));
      term.scrollTop = term.scrollHeight;
      if (snap.done) {
        cache.badge = { cls: snap.ok ? "ok" : "fail", text: `${snap.ok ? (b.expectFail ? "" : "OK: ") : "FAILED: "}${snap.summary} (${snap.seconds}s)` };
        cache.running = false;
      }
    }
    const bd = cache.running ? { cls: "run", text: "running ..." } : cache.badge || { cls: "neutral", text: "not run yet" };
    badge.className = "badge " + bd.cls;
    badge.textContent = bd.text;
    btn.disabled = anyRunning();
  };
  cache.lines.forEach((l) => term.append(termLine(l)));
  cache.paint = paint;
  paint(null);
  btn.addEventListener("click", async () => {
    cache.lines = []; cache.badge = null; term.replaceChildren();
    cache.running = true; refreshButtons();
    if (b.command) { const l = "$ " + b.command; cache.lines.push(l); term.append(termLine(l)); }
    let id;
    try { id = (await api("/api/run/" + b.action, b.params || {})).id; } catch (e) {
      cache.running = false; cache.badge = { cls: "fail", text: e.message };
      cache.lines.push(e.message); term.append(termLine(e.message)); cache.paint(null); return;
    }
    let from = 0;
    for (;;) {
      let snap;
      try { snap = await api(`/api/job?id=${id}&from=${from}`); } catch (e) { snap = { lines: [e.message], next: from, done: true, ok: false, summary: e.message, seconds: 0 }; }
      from = snap.next;
      cache.lines.push(...snap.lines);
      cache.paint(snap);
      if (snap.done) break;
      await sleep(300);
    }
    if (b.refresh) window.dispatchEvent(new Event("e2e-done"));
    refreshButtons();
  });
  const card = h("div", { class: "card", "data-action": key }, h("div", { class: "row" }, btn, badge),
    b.note ? mdEl("p", b.note, "note") : null, term);
  return card;
};

const anyRunning = () => Object.values(jobCache).some((c) => c.running);
const refreshButtons = () => Object.values(jobCache).forEach((c) => c.paint && c.paint(null));

// -- SQL results
function resultTable(res) {
  if (!res.rows.length) return h("p", { class: "note" }, "No rows.");
  const isNum = (v) => typeof v === "number" || (typeof v === "string" && /^-?\d+(\.\d+)?$/.test(v));
  return h("div", { class: "scroll" }, h("table", { class: "t" },
    h("thead", {}, h("tr", {}, res.columns.map((c) => h("th", {}, c)))),
    h("tbody", {}, res.rows.map((r) => h("tr", {}, r.map((v) => h("td", { class: isNum(v) ? "num" : "" }, v === null ? "NULL" : String(v))))))),
    res.truncated ? h("p", { class: "note" }, "Showing the first 200 rows.") : h("p", { class: "note" }, `${res.rows.length} row(s), ${res.ms} ms.`));
}

async function runQuery(sql, out) {
  out.replaceChildren(h("p", { class: "note" }, "Running ..."));
  try { out.replaceChildren(resultTable(await api("/api/sql", { sql }))); } catch (e) { out.replaceChildren(h("p", { class: "err" }, e.message)); }
}

blocks.sql = (b) => {
  const area = h("textarea", { rows: 5, spellcheck: "false", "aria-label": "SQL" }, b.queries[0].sql);
  const out = h("div");
  return h("div", { class: "card" }, h("h3", {}, "SQL console (read-only, on verify/work/jaffle.duckdb)"),
    h("div", { class: "row" }, b.queries.map((q) => h("button", { class: "ex", onclick: () => { area.value = q.sql; } }, q.label))),
    area, h("div", { class: "row" }, h("button", { class: "btn", onclick: () => runQuery(area.value, out) }, "Run query")), out);
};

// -- defects
blocks.defects = (b) => h("div", { class: "card" }, h("h3", {}, "Defects P1-P5"), b.items.map((d) => {
  const out = h("div");
  return h("div", { class: "defect" },
    h("div", { class: "top" }, h("span", { class: "pid" }, d.id), h("strong", {}, d.title),
      h("span", { class: "badge " + (d.warns ? "ok" : "neutral") }, d.warns ? "converter warns" : "no warning")),
    h("dl", {}, h("dt", {}, "In plain words"), mdEl("dd", d.plain), h("dt", {}, "Effect"), mdEl("dd", d.effect),
      h("dt", {}, "Fix"), mdEl("dd", d.fix)),
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => runQuery(d.query, out) }, "Run the query"),
      h("button", { class: "btn secondary", onclick: () => openFile(d.path, { lines: d.lines }) }, "Show in the XML")),
    h("details", { class: "qa" }, h("summary", {}, "Show the SQL"), h("pre", { class: "sqlout" }, d.query)), out);
}));

// -- e2e comparison tables
const e2eListeners = [];
blocks["compare-table"] = (b) => {
  const body = h("div", {}, h("p", { class: "note" }, "Loading ..."));
  const load = async () => {
    body.replaceChildren(h("p", { class: "note" }, "Loading ..."));
    let data;
    try { data = await api("/api/compare?run=" + b.run); } catch (e) {
      body.replaceChildren(h("p", { class: "note" }, e.message + " (run the end-to-end step above)")); return;
    }
    const rows = data.models.map((m) => {
      const cls = m.rows_status === "match" ? "status-match" : m.rows_status === "explained" ? "status-explained" : "status-bad";
      const diffs = m.differing.map((c) => h("details", { class: "qa" },
        h("summary", { class: c.status === "explained" ? "status-explained" : "status-bad" }, `${c.column}: ${c.mismatches} row(s) differ (${c.status})`),
        h("p", {}, c.reason || "no expectation listed"), c.example ? h("p", { class: "note" }, "e.g. " + c.example) : null));
      return h("tr", {},
        h("td", {}, h("code", {}, m.model), h("div", { class: "note" }, "vs " + m.reference)),
        h("td", { class: "num" }, `${m.rows_ours} / ${m.rows_ref}`),
        h("td", { class: cls }, m.rows_status + (m.rows_reason ? ": " + m.rows_reason.slice(0, 60) + (m.rows_reason.length > 60 ? "…" : "") : "")),
        h("td", { class: "num" }, `${m.matched} / ${m.columns}`),
        h("td", {}, diffs.length ? diffs : "-"));
    });
    const els = [h("div", { class: "scroll" }, h("table", { class: "t" },
      h("thead", {}, h("tr", {}, ["Model", "Rows ours / reference", "Row check", "Columns matching", "Differences"].map((x) => h("th", {}, x)))),
      h("tbody", {}, rows))),
      h("p", { class: data.failures.length ? "err" : "note" }, data.failures.length ? "UNEXPLAINED: " + data.failures.join("; ")
        : "Every difference is listed with its cause in verify/reference_map.toml.")];
    if (data.invariants) {
      els.push(h("h3", {}, "Invariants for the enriched marts (no reference model)"), h("div", { class: "scroll" }, h("table", { class: "t" },
        h("tbody", {}, Object.entries(data.invariants).map(([k, v]) => h("tr", {}, h("td", {}, k),
          h("td", { class: v === 0 ? "status-match" : "status-bad" }, v === 0 ? "pass" : `${v} violations`)))))));
    }
    body.replaceChildren(...els);
  };
  e2eListeners.push(load);
  window.addEventListener("e2e-done", load);
  load();
  return h("div", { class: "card" }, h("h3", {}, b.title), body,
    h("div", { class: "row" }, h("button", { class: "btn secondary", onclick: load }, "Reload")));
};

// -- graph and generated SQL
let graphIndex = null;
blocks.graph = (b) => {
  const card = h("div", { class: "card" }, h("h3", {}, b.title || "Mapping graph and generated SQL"));
  const graphBox = h("div", { class: "graphbox" }, "Loading ...");
  const sqlBox = h("div");
  const info = h("p", { class: "note" }, "Click a box to highlight its CTE in the SQL.");
  const mapSel = h("select", { "aria-label": "Mapping" });
  const tgtSel = h("select", { "aria-label": "Target" });
  const fitBtn = h("button", { class: "ex" }, "Fit to width");
  const sel = { mapping: b.default.mapping, target: b.default.target };

  const showTarget = async () => {
    graphBox.textContent = "Loading ...";
    let g, file;
    try {
      g = await api(`/api/graph?mapping=${encodeURIComponent(sel.mapping)}&target=${encodeURIComponent(sel.target)}`);
      file = await api("/api/file?path=" + encodeURIComponent(g.path));
    } catch (e) { graphBox.textContent = e.message; return; }
    graphBox.innerHTML = g.svg;   // generated by demo/graph.py with every name escaped
    const svg = graphBox.querySelector("svg");
    const fit = (on) => { if (svg) { svg.style.width = on ? "100%" : ""; svg.style.height = on ? "auto" : ""; } fitBtn.textContent = on ? "Show actual size" : "Fit to width"; fitBtn.dataset.on = on ? "1" : ""; };
    fit(!!svg && Number(svg.getAttribute("width")) <= graphBox.clientWidth * 1.4);
    fitBtn.onclick = () => fit(!fitBtn.dataset.on);
    const lines = file.text.split("\n");
    let current = null;
    const drawSql = (ctes) => {
      const hl = ctes.map((c) => g.ranges[c]).filter(Boolean);
      const view = codeView(lines, { hl });
      sqlBox.replaceChildren(h("div", { class: "row" }, h("strong", {}, g.model + ".sql"),
        h("button", { class: "linkbtn", onclick: () => openFile(g.path) }, "open in the file viewer")), view);
      requestAnimationFrame(() => {
        const first = view.querySelector(".ln.hl");
        if (first) view.scrollTop = Math.max(0, first.offsetTop - 60);
      });
    };
    drawSql([]);
    graphBox.querySelectorAll(".node").forEach((node) => node.addEventListener("click", () => {
      const name = node.getAttribute("data-node");
      const entry = g.nodes.find((n) => n.name === name);
      graphBox.querySelectorAll(".node.sel").forEach((n) => n.classList.remove("sel"));
      if (current === name) { current = null; drawSql([]); info.replaceChildren("Click a box to highlight its CTE in the SQL."); return; }
      current = name; node.classList.add("sel");
      info.replaceChildren(entry.cte.length ? `${name} (${entry.type}) is the CTE "${entry.cte.join('", "')}".`
        : entry.elsewhere ? h("span", {}, `${name} (${entry.type}) is shared by several targets, so it lives in the intermediate model `,
          h("button", { class: "linkbtn", onclick: () => openFile(entry.elsewhere.path) }, entry.elsewhere.model), ".")
        : `${name} (${entry.type}) is read directly by its Source Qualifier; it has no CTE of its own.`);
      drawSql(entry.cte);
    }));
  };
  const fillTargets = () => {
    const m = graphIndex.find((x) => x.mapping === sel.mapping);
    tgtSel.replaceChildren(...m.targets.map((t) => h("option", { value: t.instance, selected: t.instance === sel.target }, `${t.instance} → ${t.model}`)));
    if (!m.targets.some((t) => t.instance === sel.target)) sel.target = m.targets[0].instance;
    tgtSel.value = sel.target;
  };
  mapSel.addEventListener("change", () => { sel.mapping = mapSel.value; sel.target = ""; fillTargets(); showTarget(); });
  tgtSel.addEventListener("change", () => { sel.target = tgtSel.value; showTarget(); });
  card.append(h("div", { class: "row" }, mapSel, tgtSel, fitBtn), graphBox, info, sqlBox);
  (graphIndex ? Promise.resolve(graphIndex) : api("/api/graph-index")).then((idx) => {
    graphIndex = idx;
    mapSel.replaceChildren(...idx.map((m) => h("option", { value: m.mapping, selected: m.mapping === sel.mapping }, m.mapping)));
    mapSel.value = sel.mapping;
    fillTargets();
    showTarget();
  }).catch((e) => { graphBox.textContent = e.message; });
  return card;
};

// -- live formula translator
blocks.translator = (b) => {
  const formula = h("input", { type: "text", class: "wide", value: b.formula, spellcheck: "false", "aria-label": "Informatica formula" });
  const columns = h("input", { type: "text", class: "wide", value: b.columns, spellcheck: "false", "aria-label": "Column types" });
  const out = h("div");
  let timer = null, seq = 0;
  const treeEl = (n) => h("li", {}, n.label, n.children ? h("ul", {}, n.children.map(treeEl)) : null);
  const run = async () => {
    const mine = ++seq;
    let r;
    try { r = await api("/api/translate", { formula: formula.value, columns: columns.value }); } catch (e) { out.replaceChildren(h("p", { class: "err" }, e.message)); return; }
    if (mine !== seq) return;
    out.replaceChildren(...[
      h("h3", {}, "1. Tokens (tokenizer)"), h("div", {}, r.tokens.map((t) => h("span", { class: "tok" }, h("small", {}, t.kind), t.text))),
      h("h3", {}, "2. Tree (parser)"), r.tree ? h("ul", { class: "tree" }, treeEl(r.tree)) : h("p", { class: "note" }, "no tree"),
      h("h3", {}, "3. SQL (emitter)"),
      r.error ? h("p", { class: "err" }, r.error) : h("pre", { class: "sqlout" }, r.sql),
      r.type ? h("p", { class: "note" }, "inferred type: " + r.type) : null,
      ...r.warnings.map((w) => h("p", { class: "warnline" }, "warning: " + w))].filter(Boolean));
  };
  const schedule = () => { clearTimeout(timer); timer = setTimeout(run, 200); };
  formula.addEventListener("input", schedule);
  columns.addEventListener("input", schedule);
  run();
  return h("div", { class: "card" }, h("h3", {}, "Try an Informatica formula"),
    h("div", { class: "row" }, b.examples.map((x) => h("button", { class: "ex", onclick: () => { formula.value = x; run(); } }, x.length > 34 ? x.slice(0, 33) + "…" : x))),
    h("label", { class: "note" }, "Formula"), formula,
    h("label", { class: "note" }, "Column types (name:type, comma separated; unlisted names have an unknown type)"), columns, out);
};

// ---------------------------------------------------------------- slides
let slides = [];
let current = 0;

function renderSlide(i) {
  current = Math.max(0, Math.min(slides.length - 1, i));
  const s = slides[current];
  location.hash = "#/" + (current + 1);
  document.title = `${current + 1}. ${s.title} - pwc-to-dbt demo`;
  $("#steps").querySelectorAll("button").forEach((btn, k) => {
    btn.classList.toggle("active", k === current);
    btn.setAttribute("aria-current", k === current ? "page" : "false");
  });
  const done = slides.slice(0, current).reduce((a, x) => a + x.minutes, 0);
  const root = $("#slide");
  e2eListeners.splice(0).forEach((fn) => window.removeEventListener("e2e-done", fn));
  root.replaceChildren(
    h("div", { class: "head" }, h("h1", {}, `${current + 1}. ${s.title}`), h("p", {}, s.subtitle),
      h("div", { class: "chips" }, h("span", { class: "chip" }, s.core ? "core" : "optional"), h("span", { class: "chip" }, `about ${s.minutes} min`),
        h("span", { class: "chip" }, `~${Math.round(done)} min in`))),
    h("div", { class: "grid" },
      h("div", { class: "hints" },
        h("section", { class: "card" }, h("h2", {}, "Key points"), h("ul", {}, s.points.map((p) => mdEl("li", p)))),
        h("section", { class: "card mention" }, h("h2", {}, "Mention"), h("ul", {}, s.mention.map((p) => mdEl("li", p))))),
      h("div", { class: "blocks" }, s.blocks.map((b) => blocks[b.type](b, s)))));
  window.scrollTo(0, 0);
  refreshButtons();
}

function buildNav() {
  const nav = $("#steps");
  slides.forEach((s, i) => nav.append(h("button", { class: s.core ? "" : "optional", title: s.title + (s.core ? "" : " (optional)"), onclick: () => renderSlide(i) },
    h("span", { class: "n" }, i + 1), s.short)));
  $("#prev").addEventListener("click", () => renderSlide(current - 1));
  $("#next").addEventListener("click", () => renderSlide(current + 1));
}

document.addEventListener("keydown", (e) => {
  if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey) return;
  if (e.key === "Escape") closeDrawer();
  else if (e.key === "ArrowRight" || e.key === "PageDown") renderSlide(current + 1);
  else if (e.key === "ArrowLeft" || e.key === "PageUp") renderSlide(current - 1);
  else if (/^[1-9]$/.test(e.key)) renderSlide(Number(e.key) - 1);
  else if (e.key === "0") renderSlide(9);
});

// stopwatch for rehearsal
let t0 = Date.now();
const tick = () => {
  const s = Math.floor((Date.now() - t0) / 1000);
  $("#timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
};
setInterval(tick, 1000);
$("#timer").addEventListener("click", () => { t0 = Date.now(); tick(); });

(async function init() {
  try { slides = await api("/api/slides"); } catch (e) { $("#slide").textContent = "Cannot load the slides: " + e.message; return; }
  buildNav();
  const n = parseInt((location.hash.match(/#\/(\d+)/) || [])[1], 10);
  renderSlide(Number.isFinite(n) ? n - 1 : 0);
})();
