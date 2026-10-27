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
    _scrolled: 0, scrollIntoView() { all[id]._scrolled++; },
    classList: {
      add(c) { if (c === "hidden") all[id]._hidden = true; },
      remove(c) { if (c === "hidden") all[id]._hidden = false; },
      contains(c) { return c === "hidden" ? all[id]._hidden : false; },
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

/** What the document card's proposal tabs render for a given `scope`. */
function tabs(scope) {
  const $ = nodes();
  const run = new Function("$", "esc", "scope",
    `${topConst("DOC_HASH")}
     ${fn("renderScopeTabs")}
     renderScopeTabs(scope);
     return { html: $("pv-scope-tabs").innerHTML, hidden: $("pv-scope-tabs")._hidden };`);
  return run($, esc, scope);
}

/** The header title, and the Status / Approve card headings, for a given view model. */
function title(vm) {
  const $ = nodes();
  const run = new Function("$", "esc", "vm",
    `${topConst("DOC_HASH")}
     ${fn("renderScopeTabs")}
     ${fn("labelScopeCards")}
     ${fn("setHeader")}
     setHeader(vm, false);
     return { p: $("p-title").textContent, pv: $("pv-title").textContent,
              status: $("status-title").textContent, approve: $("approve-title").textContent };`);
  return run($, esc, vm);
}

/** THE PAGE A TAB OPENS. A tab is a plain navigation, so "switching" is: the browser loads `href`,
 *  app.js reads its TOKEN off that URL, and the page renders that proposal's view model. This runs
 *  exactly that -- the real TOKEN line against the tab's URL, then the real header, signing and
 *  document functions -- and reports which proposal the document, the Status card and the Approve
 *  button belong to. */
function pageAt(href, vm) {
  const url = new URL(href, "https://portal.example");
  const location = { pathname: url.pathname, hash: url.hash };
  const $ = nodes();
  $("pdf-card")._hidden = false;   // on screen: the proposal step, with a document
  const run = new Function("$", "esc", "location", "vm",
    `${topConst("TOKEN")}
     ${topConst("DOC_HASH")}
     let STATE = vm, SELECTED = new Set(["Lab"]), PDF_MOUNTED = false, PDF_OPENED_IN_TAB = false;
     let DOC_JUMPED = false;
     ${fn("pdfUrl")}
     ${fn("renderScopeTabs")}
     ${fn("labelScopeCards")}
     ${fn("setHeader")}
     ${fn("signingView")}
     ${fn("proposalWasOpened")}
     ${fn("approveBlocker")}
     ${fn("updateApproveGate")}
     ${fn("renderSignaturePreview")}
     ${fn("renderSigning")}
     ${fn("jumpToDocument")}
     setHeader(vm, false);
     renderSigning();
     jumpToDocument();
     jumpToDocument();               // a poll re-render: must not move the page again
     return { token: TOKEN, pdf: pdfUrl(), tabs: $("pv-scope-tabs").innerHTML,
              status: $("status-title").textContent, approve: $("approve-title").textContent,
              button: $("approve-btn").textContent, scrolled: $("pdf-card")._scrolled };`);
  return run($, esc, location, vm);
}

/** Which view and step the real applyHashView opens for a hash. */
function hashView(hash) {
  const shown = {};
  const run = new Function("location", "$", "show", "hide", "signalProposalViewed",
    "applyStepPanel", "STEP_CARDS",
    `let ACTIVE_STEP = null;
     ${fn("applyHashView")}
     applyHashView(false);
     return ACTIVE_STEP;`);
  const step = run({ hash }, (id) => id, (id) => { shown[id] = true; }, (id) => { shown[id] = false; },
                   () => {}, () => {}, { proposal: [], deposit: [], contacts: [] });
  return { step, proposalView: shown["proposal-view"] === true };
}

const out = {};
out.kindsScoped = kinds({ group: "g", label: "Lab 1", siblings: [] });
out.kindsPlain = kinds(null);
const SIBS = [
  { token: "tok-a", scope_label: "Breakroom", proposal_status: "approved", current: false },
  { token: "tok-b", scope_label: "Lab <1>", proposal_status: "viewed", current: true },
  { token: "tok-c", scope_label: "Lab 2", proposal_status: "sent", current: false },
];
out.tabsScoped = tabs({ group: "g", label: "Lab <1>", siblings: SIBS });
out.tabsNone = tabs(null);
out.tabsAlone = tabs({ group: "g", label: "Lab 1", siblings: [SIBS[1]] });
out.titleScoped = title({ project_name: "Acme HQ", scope: { label: "Lab 1", siblings: [] } });
out.titlePlain = title({ project_name: "Acme HQ", scope: null });

// Switch from Lab <1>'s page to the Lab 2 tab: follow the href the tab row really rendered.
const lab2Href = /href="([^"]*tok-c[^"]*)"/.exec(out.tabsScoped.html)[1];
const SIGN = { required: true, consent_text: "I agree." };
out.switched = pageAt(lab2Href, {
  project_name: "Acme HQ", has_pdf: true, revision_no: 3, signing: SIGN,
  scope: { group: "g", label: "Lab 2",
           siblings: SIBS.map((s) => Object.assign({}, s, { current: s.token === "tok-c" })) },
});
out.plainPage = pageAt("/p/tok-solo", { project_name: "Warehouse", has_pdf: true, revision_no: 1,
                                        signing: SIGN, scope: null });
out.tabHashView = hashView(lab2Href.slice(lab2Href.indexOf("#")));

// projects.js: the shared list renderer, evaluated with a stand-in window.
const projSrc = fs.readFileSync(path.join(ROOT, "projects.js"), "utf8");
const win = {};
new Function("window", "fetch", projSrc)(win, () => Promise.resolve({ json: () => ({}) }));
const P = win.TWProjects;
// The server folds a project priced as several proposals into ONE card (main._project_cards).
const GROUPED = { token: "t1", project_name: "Acme <HQ>", display_name: "Acme <HQ>", scope_group: "g",
  proposal_status: "approved", deposit_status: "pending",
  scopes: [{ token: "t1", scope_label: "Breakroom", proposal_status: "approved",
             deposit_status: "pending" },
           { token: "t1b", scope_label: "Lab 1", proposal_status: "sent",
             deposit_status: "pending" }] };
const LIST = [
  GROUPED,
  { token: "t2", project_name: "Warehouse", display_name: "Warehouse", scope_group: null,
    proposal_status: "viewed" },
  { token: "t3", project_name: "Old card" },   // an older payload with no display_name
];
out.rows = P.rowsHtml(LIST, "t2");
out.rowsOnSecondProposal = P.rowsHtml(LIST, "t1b");
out.foundFromSecond = (P.find(LIST, "t1b") || {}).display_name || null;
out.foundSolo = (P.find(LIST, "t2") || {}).token || null;
out.groupStatus = P.status(GROUPED);
out.groupAllNew = P.status(Object.assign({}, GROUPED, { scopes: [
  { token: "a", proposal_status: "sent" }, { token: "b", proposal_status: "sent" }] }));
out.groupAllApproved = P.status(Object.assign({}, GROUPED, { scopes: [
  { token: "a", proposal_status: "approved", deposit_status: "received" },
  { token: "b", proposal_status: "approved", deposit_status: "pending" }] }));

console.log(JSON.stringify(out));
