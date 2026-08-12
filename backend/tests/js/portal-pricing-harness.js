"use strict";
/* Render the customer's Pricing block for real, out of app.js, and report what each row says.
 *
 * The claim is about what a CUSTOMER can tell from the screen, so the test has to look at the
 * rendered row — not at whether `is_base` appears somewhere in the source. It was in the payload
 * all along and rendered nowhere, which is exactly the bug.
 *
 * Usage: node portal-pricing-harness.js <frontend-dir>   →  one line of JSON
 */
const fs = require("fs");
const path = require("path");

const ROOT = process.argv[2];
const src = fs.readFileSync(path.join(ROOT, "app.js"), "utf8");

/** Lift a top-level `function name(...) {...}` by brace counting. */
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

const renderOptionsSrc = fn("renderOptions");
const updateTotalSrc = fn("updateSelectedTotal");

/** Render the options list and pull the facts out of the HTML string it produced. */
function render(options, opts) {
  const o = opts || {};
  const nodes = {};
  // `dataset` and `disabled` are here because updateSelectedTotal also drives the Approve
  // button. Stubbed rather than avoided: the real function is what renders the rows, and
  // calling a trimmed copy of it would be testing something the page does not run.
  const el = (id) => (nodes[id] = nodes[id] || {
    id, innerHTML: "", textContent: "", disabled: false, dataset: {},
    querySelectorAll: () => [], querySelector: () => null,
    addEventListener() {}, setAttribute() {}, classList: { add() {}, remove() {}, toggle() {} },
  });
  const scope = new Function(
    "options", "addons", "approved", "$", "esc", "money", "show", "hide", "setEligible",
    "CUR_OPTIONS_IN", "SELECTED_IN", "veLabel", "STATE", "depositApplies", "DEPOSIT_PCT",
    `let SELECTED = SELECTED_IN;
     let CUR_OPTIONS = CUR_OPTIONS_IN;
     ${topConst("isVE")}
     ${renderOptionsSrc}
     ${updateTotalSrc}
     renderOptions(options, addons, approved);
     return { html: $("options").innerHTML, total: $("selected-total").textContent,
              selected: Array.from(SELECTED) };`);

  return scope(
    options, o.addons || [], !!o.approved,
    el,
    (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
      (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])),
    (n) => "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: 2 }),
    () => {}, () => {}, () => {},
    options, new Set(o.selected || []),
    (x) => "Deduct (" + x.delta + ")",
    // The module-level STATE the page reads for the approved check. Only the one field matters.
    { status: { proposal: o.approved ? "approved" : "sent" } },
    // Whether this job takes a deposit, and at what rate. Real values are irrelevant to the
    // classification labels under test; both are stubbed so the total line still renders.
    () => o.deposit !== false, 0.25);
}

/** One entry per rendered .option, with the classification badge extracted. */
function rows(html) {
  const out = [];
  const re = /<label class="option[^"]*"[\s\S]*?<\/label>/g;
  let m;
  while ((m = re.exec(html))) {
    const block = m[0];
    const name = (/<span class="name">([^<]*)</.exec(block) || [])[1] || "";
    const kindM = /<span class="opt-kind ([^"]*)"[^>]*>([^<]*)</.exec(block);
    out.push({
      name,
      price: (/<span class="price">([^<]*)</.exec(block) || [])[1] || "",
      kind: kindM ? kindM[2] : null,
      kindClass: kindM ? kindM[1] : null,
      hasTitle: /class="opt-kind[^"]*"[^>]*title="[^"]+"/.test(block),
      checked: /checked/.test(block),
      metas: (block.match(/<span class="meta">([^<]*)</g) || [])
               .map((s) => /<span class="meta">([^<]*)</.exec(s)[1]),
    });
  }
  return out;
}

const out = {};

// The screenshot's exact case: a base bid plus a standalone option, both selectable, summing.
const TWO = [
  { label: "EPOXY", total: 29942, system_desc: "TREADWELL MICRO FLAKE DOUBLE BROADCAST",
    is_base: true, price_mode: "total" },
  { label: "ROOM 1", total: 15801, system_desc: "TREADWELL MICRO FLAKE DOUBLE BROADCAST",
    is_base: false, price_mode: "total" },
];
out.baseAndOption = rows(render(TWO, { selected: ["EPOXY", "ROOM 1"] }).html);

// A value-engineering alternative: it REPLACES the base, so it must not be called an "Option".
const VE = [
  { label: "Base Bid", total: 29942, is_base: true, price_mode: "total" },
  { label: "Thinner build", total: 24000, is_base: false, price_mode: "deduct", delta: -5942 },
];
out.withAlternative = rows(render(VE, { selected: ["Base Bid"] }).html);

// A single base-only proposal — the common case. Still says which it is.
out.baseOnly = rows(render(
  [{ label: "Base Bid", total: 15801, is_base: true, price_mode: "total" }], {}).html);

// After approval the checkboxes are disabled; the labels must still be there to read.
out.approved = rows(render(TWO, { approved: true, selected: ["EPOXY"] }).html);

// No options at all → the "being finalized" message, and no stray badges.
out.empty = rows(render([], {}).html);

// ── the deposit tabs ─────────────────────────────────────────────────────────
// The state was always set (aria-pressed) and never styled, so both buttons looked identical.
const css = fs.readFileSync(path.join(ROOT, "styles.css"), "utf8");
const index = fs.readFileSync(path.join(ROOT, "index.html"), "utf8");
const pressedRule = /\.btn\[aria-pressed="true"\]\s*\{([^}]*)\}/.exec(css);
out.depositTabs = {
  jsSetsPressed: /tabAch\.setAttribute\("aria-pressed", "true"\)/.test(src)
              && /tabCheck\.setAttribute\("aria-pressed", "true"\)/.test(src),
  markupHasPressed: /id="tab-ach"[^>]*aria-pressed/.test(index),
  cssStylesPressed: !!pressedRule,
  pressedBody: pressedRule ? pressedRule[1].replace(/\s+/g, " ").trim() : "",
};

console.log(JSON.stringify(out));
