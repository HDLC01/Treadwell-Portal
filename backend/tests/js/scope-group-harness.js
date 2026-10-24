"use strict";
/* Run the real app.js / projects.js code for a scope of a multi-scope project, and report what a
 * customer would read. Lifted by brace counting, like portal-pricing-harness.js, so it executes
 * the shipped functions rather than a copy of them.
 *
 * Usage: node scope-group-harness.js <frontend-dir>   ->  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

function fn(name) {
  const m = new RegExp("\\nfunction " + name + "\\s*\\(").exec(src);
  if (!m) throw new Error(name + "() is gone from app.js — rewrite this harness, don't delete it");
  const i = src.indexOf("{", m.index + m[0].length - 1);
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    if (src[j] === "{") depth++;
    else if (src[j] === "}" && --depth === 0) return src.slice(m.index, j + 1);
  }
  throw new Error("unbalanced braces reading " + name);
}
function topConst(name) {
  const m = new RegExp("\\nconst " + name + " = [^\\n]*;").exec(src);
  return m ? m[0] : "";
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function nodes() {
  const all = {};
  const el = (id) => (all[id] = all[id] || {
    id, innerHTML: "", textContent: "", disabled: false, dataset: {}, _hidden: true,
    querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, setAttribute() {},
    classList: {
      add(c) { if (c === "hidden") all[id]._hidden = true; },
      remove(c) { if (c === "hidden") all[id]._hidden = false; },
      toggle() {},
    },
  });
  return el;
}

/** The kind badge on each pricing row, for a given STATE.scope. */
function kinds(scope) {
  const $ = nodes();
  const run = new Function(
    "options", "$", "esc", "money", "setEligible", "STATE", "depositApplies", "DEPOSIT_PCT",
    "veLabel",
    `let SELECTED = new Set(); let CUR_OPTIONS = [];
     ${topConst("isVE")}
     ${fn("renderOptions")}
     ${fn("updateSelectedTotal")}
     renderOptions(options, [], false);
     return $("options").innerHTML;`);
  const html = run(
    [{ label: "Lab 1 epoxy", total: 12000, is_base: true, price_mode: "total" },
     { label: "Cove base", total: 900, is_base: false, price_mode: "total" }],
    $, esc, (n) => "$" + n, () => {},
    { status: { proposal: "sent" }, scope: scope }, () => true, 0.25, (x) => String(x));
  const out = [];
  const re = /<span class="opt-kind ([^"]*)"[^>]*title="([^"]*)"[^>]*>([^<]*)</g;
  let m;
  while ((m = re.exec(html))) out.push({ cls: m[1], title: m[2], kind: m[3] });
  return out;
}

/** What the header's sibling strip renders for a given `scope`. */
function strip(scope) {
  const $ = nodes();
  const run = new Function("$", "esc", "scope",
    `${fn("renderScopeStrip")}
     renderScopeStrip(scope);
     return { html: $("pv-scopes").innerHTML, hidden: $("pv-scopes")._hidden };`);
  return run($, esc, scope);
}

/** The header title for a given view model. */
function title(vm) {
  const $ = nodes();
  const run = new Function("$", "esc", "vm",
    `${fn("renderScopeStrip")}
     ${fn("setHeader")}
     setHeader(vm, false);
     return { p: $("p-title").textContent, pv: $("pv-title").textContent };`);
  return run($, esc, vm);
}

const out = {};
out.kindsScoped = kinds({ group: "g", label: "Lab 1", siblings: [] });
out.kindsPlain = kinds(null);
const SIBS = [
  { token: "tok-a", scope_label: "Breakroom", proposal_status: "approved", current: false },
  { token: "tok-b", scope_label: "Lab <1>", proposal_status: "viewed", current: true },
  { token: "tok-c", scope_label: "Lab 2", proposal_status: "sent", current: false },
];
out.stripScoped = strip({ group: "g", label: "Lab <1>", siblings: SIBS });
out.stripNone = strip(null);
out.stripAlone = strip({ group: "g", label: "Lab 1", siblings: [SIBS[1]] });
out.titleScoped = title({ project_name: "Acme HQ", scope: { label: "Lab 1", siblings: [] } });
out.titlePlain = title({ project_name: "Acme HQ", scope: null });

// projects.js: the shared list renderer, evaluated with a stand-in window.
const projSrc = fs.readFileSync(path.join(ROOT, "projects.js"), "utf8");
const win = {};
new Function("window", "fetch", projSrc)(win, () => Promise.resolve({ json: () => ({}) }));
out.rows = win.TWProjects.rowsHtml([
  { token: "t1", project_name: "Acme HQ", display_name: "Acme HQ — Breakroom", scope_group: "g",
    proposal_status: "sent" },
  { token: "t2", project_name: "Warehouse", display_name: "Warehouse", scope_group: null,
    proposal_status: "viewed" },
  { token: "t3", project_name: "Old card" },   // an older payload with no display_name
], "t2");

console.log(JSON.stringify(out));
